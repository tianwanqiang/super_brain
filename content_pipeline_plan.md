# 内容产出 4-agent 流水线 + 自动化发布 —— 方案文档（v1，待评审）

> 状态：**2026-09 已进入实现阶段（骨架版完成，离线测试全绿），真实模型评估待 CEO 运行。**
> 已落地代码：
> - 发布侧 agent4：`autopublish.py` 引擎 + `templates/autopublish.html` 后台页 +
>   4 个 agent 注册（publish-planner/media-maker/gatekeeper/publisher），测试全绿。
> - 内容侧 agent1/agent3：`content_pipeline.py`（信息包红线校验 / writer 复用 / critic
>   评分卡 / revise 迭代 / 交接发布单）+ `researcher`/`critic` 注册与 private.md，
>   `tests/test_content_pipeline.py` + `tests/test_autopublish_engine.py` 离线协作测试通过。
> - 技能评估：`content_pipeline_eval.py`（writer / critic / pipeline 三种模式，
>   默认 plan 不花钱，加 `--execute` 才发起真实 DeepSeek/Tavily 调用——按
>   `feedback_dev_workflow_cost.md` 的规矩，真实调用由 CEO 自己执行）。
> - 修了一处影响复盘的问题：`agent_registry.log_execution` 在 pytest 下不再写生产 lessons.md
>   （之前 ops-assistant/lessons.md 被测试数据污染到 37KB）。
> - **2026-09-06 P4 内容工作流已落地**：`workflow.py`（定义/运行实例/分步审批/调度 tick）+
>   `/admin/workflows` 后台页 + `tests/test_workflow_engine.py`（8 项离线）——定时到点先跑
>   第一步，产物进审批队列，点"通过"才触发下一步；每步 requires_approval 可在页面开关。
>
> 本文件仍是流程与契约的总设计；实现细节以代码与运行记录为准。

---

## 0. 摘要

一句话：**用一条带反馈环的多 agent 流水线，把"决策/工作沉淀 → 可发布内容 → 已发布 → 效果数据回灌选题"跑通；商业上先验证"谁愿意为什么付费"，内容流水线服务获客信任，不替代产品验证。**

四个 agent 与现有代码的映射：

| 编号 | 职责 | 复用/新增 | 对应现有资产 |
|---|---|---|---|
| agent1 | 搜集信息 | **新增 `researcher`** | Tavily/联网搜索 + RAG（`rag.py`、`llm_client` 已有 Tavily 支持） |
| agent2 | 撰写 | **复用** | `content-strategist` + `writer`（`executors.generate_writer_draft`） |
| agent3 | 改进点评 | **新增 `critic`** | 借鉴 `roundtable` 的独立并行与 `review.py` 的复盘思想 |
| agent4 | 自媒体运营 | **执行复用 + 补回采** | `publish-planner` / `gatekeeper` / `publisher`（agents.yaml 已注册）+ `autopublish.py` 骨架 |

---

## 1. 商业可行性评估

### 1.1 已成立

- **产品判断收敛质量高**：`tasks.yaml` + `meeting/` 纪要显示已完成"窄词化垂直场景 → 先测限次付费 → 决策陪练"的战略收敛，方向克制、可执行。
- **可审计决策记录是差异化点**：R1/R2 独立分析 + 收敛 + 结论标注依据（`output_requires_citation`）+ `lessons.md` 事实沉淀，构成"可回溯推演过程"这一难复制资产。
- **工程成本意识强**：云端向量化解决内存瓶颈、单 worker 防重复调度、配置事故有备份与回归测试。

### 1.2 未验证（按风险排序）

1. **付费意愿从未被真实市场验证**：metahub 是模拟支付。下一步唯一目标建议：5-10 个真实"一人公司老板"为一次决策陪练付真钱（10 次限次包），而不是继续加功能。
2. **决策品类低频 + 结果难证伪**："避坑"无法证明反事实 → 付费靠信任 → 信任靠内容 → 内容流水线是获客的必要非充分条件。别让"完善内容系统"推迟"去找愿意付费的人"。
3. **合规**：`legal` agent 的免责要求必须延伸到所有对外自媒体输出。

### 1.3 商业化阶段建议（roadmap）

- 阶段 A（验证）：内容流水线只服务一个窄词场景的内容供给，目标=真实付费转化 ≥1 例。
- 阶段 B（放大）：回采数据证明某类内容带来咨询/付费后，再放量 + 开更多渠道。
- 阶段 C（产品化）：决策陪练本身可交付（预约/限次/回访），内容变副产品。

---

## 2. 技术现状与改进清单

### 2.1 做得好的（保留，别重写）

- 模块边界清晰：`agent_registry`（元信息）/`llm_client`（调用）/`executors`（业务组装）/`publishers`（平台调用）/`dispatcher`（inbox 调度）。
- agents.yaml 是唯一注册源；工具类角色函数化（executor 字段）与推理类角色（roundtable）刻意区分。
- 测试 + GitHub Actions 部署 + 单 worker + 事故复盘文档化。

### 2.2 需要改进的（按优先级）

1. **对外内容幻觉无硬闸门**：writer 只有软约束"不编造"，没有机制校验草稿里的事实/数据/引用。→ researcher 信息包强制 citation + critic 意见单含"事实风险"维度 + gatekeeper 人工终审。
2. **无效果回采**：`draft_log` 只记生成不记发布效果。→ §5 设计最小回采。
3. **自我批判趋同**：同模型自评第二轮后多为措辞微调。→ critic 与 writer 异模型/异温度 + 评分卡 + 有限轮次。
4. **小内存服务器不做重活**：浏览器自动化、视频渲染不放 1.9G 容器。
5. **头条 PowerShell 依赖 Windows**：服务器上头条渠道只到"本地草稿"，除非以后走头条开放平台 API。

---

## 3. agent1 researcher —— 信息搜集（新增）

### 3.1 角色边界

输入 = 选题/素材；输出 = **信息包（Fact Package）**，不是文章。researcher 不做内容判断、不写观点。

### 3.2 信息包格式（数据契约，落地为 JSON schema）

```json
{
  "topic": "选题",
  "generated_at": "ISO 时间",
  "facts": [
    {
      "claim": "一句话事实陈述",
      "source_url": "https://…（必填，无来源的事实不允许进入下一环）",
      "source_name": "来源名",
      "retrieved_at": "抓取时间",
      "confidence": "high|medium|low",
      "conflicts": ["另一条相反事实的 id（没有就省略）"]
    }
  ],
  "conflict_notes": "冲突事实如何并列呈现的说明（不许 agent2 擅自抹平）",
  "coverage_gaps": ["该查但没查到/被 paywall 挡住的部分"],
  "cost": {"searches": 3, "tokens": 1234}
}
```

### 3.3 硬性红线

1. `facts[].source_url` 缺失 → 该条不进信息包（宁可少一条，不可多一条假的）。
2. 冲突事实必须并列交给 CEO/下一环裁决，不允许 researcher 或 writer 悄悄二选一。
3. 检索默认走 Tavily + 现有 RAG（`rag.py` 的向量检索 + 各 agent private.md 知识），`TAVILY_API_KEY` 未配时自动降级为仅 RAG（现有 `llm_client` 已有降级先例）。
4. 检索与引用留痕到 `agents/researcher/lessons.md` 与 RAG 检索日志，供复盘"经常在哪种选题上缺料"。

### 3.4 费用控制

- Tavily 每次搜索有成本 → 配置 `RESEARCH_MAX_SEARCHES`（默认 3）/ 每日上限；搜索 URL 结果做去重与缓存。
- 只在 agent1 被调用/调度时发生，不进"打开页面"这种高频路径。

---

## 4. agent2 writer —— 撰写（复用，不重建）

直接复用现有 `content-strategist` → `writer` → 分平台适配链（`executors.py`）：
`generate_writer_draft()`（平台无关定稿）→ `adapt_draft_to_toutiao/wechat`。

**增量要求（不在这次实现，作为调用约定写进 agent2 的 private.md 更新计划）：**
- 写作素材 = researcher 的信息包 + CEO 素材；writer 引用的每条关键事实要能回溯到信息包 id。
- 平台定稿前，内容判断只做一次（现有 `generate_writer_draft` 已保证双平台共享同一份定稿）。

---

## 5. agent3 critic —— 改进点评（新增）

### 5.1 角色边界

critic **只出"意见单"，不直接改稿**。改稿永远由 agent2 按意见单执行——职责分离是防止
"评论家重写导致风格漂移"的关键。意见单落盘成文件，供人工与复盘读取。

### 5.2 意见单格式（评分卡，量化到 1-5）

```json
{
  "draft_id": "指向被评的草稿",
  "critic_model": "deepseek-… / 其他模型（异模型要求见 5.3）",
  "scores": {
    "hook": 3,            // 开头钩子：是否有明确承诺/反常识/问题
    "density": 4,         // 信息密度：是否注水/空话
    "fact_risk": 2,        // 事实风险：低分=存在无来源断言/疑似编造
    "platform_fit": 3      // 平台适配：目标平台的标题/长度/排版
  },
  "must_fix": ["可执行的具体修改点，每条对应草稿位置"],
  "optional": ["可改可不改"],
  "verdict": "accept | revise | reject"   // reject = 事实风险无法通过，建议回到 researcher/人工
}
```

### 5.3 打破趋同的三招

1. critic 与 writer 用**不同模型或不同温度**（配置项 `CRITIC_MODEL_OVERRIDE`，缺省用同模型但高温度 + 不同 system 框架）。
2. 输出必须是**可量化评分卡**，"感觉更好"不算数；两轮 revise 后仍不达标 → 交人工裁决（模拟 roundtable 的"独立视角交叉"思想）。
3. 可配双 critic 并行（两个不同视角），MVP 阶段先单 critic + 人工。

### 5.4 轮次上限

`MAX_REVISE_ROUNDS = 2`：agent2 改完一轮 → critic 复评 → 二轮 → 仍不过交人工。每轮都是一次
真实 DeepSeek 调用，轮次上限同时是费用上限。

---

## 6. agent4 自媒体运营 = planner + gatekeeper + publisher + 回采

### 6.1 分工（已在 agents.yaml 注册 / private.md 就绪）

| 子角色 | 职责 | 闸门 |
|---|---|---|
| publish-planner | 选题/排期/渠道路由，产出发布单 | 人工建单（素材自动路由为扩展点） |
| gatekeeper | 发布前质检（含事实清单核验），可打回 | **批准发布永远人工按钮** |
| publisher | 执行发布（wechat freepublish 真发布 / 其余渠道 mock 或 needs_manual） | 全局主开关 + 渠道 mode + CEO 放行 三重闸门 |

### 6.2 效果回采（agent4 从"发布器"变"运营"的关键，MVP 最小版）

- 载体：`autopublish_queue/` 发布单的 `platform_ref` 已有字段；新增 `metrics` 回填。
- 最小做法：发布后人工（或半自动）把阅读/转发/涨粉数填回发布单；攒 2-4 周后，由复盘机制
  （参照 `review.py`）归纳"哪种选题/标题/渠道有效"，输出给 publish-planner 当排期权重。
- 完全自动回采（公众号数据 API / 头条后台）列作 phase 3 扩展点，不在 MVP。

---

## 7. 与现有 autopublish 骨架的关系（现状，未收尾）

上一轮已落代码（未完成验证，见 `autopublish.py` 顶部注释与 git 状态）：

- `paths.py`：`autopublish_queue/ artifacts/ sources/` 三个运行目录。
- `autopublish.py`：发布单状态机（queued→drafted→approved→published/needs_manual/failed）、
  渠道注册表（wechat/toutiao/video）、collect/draft/dispatch 三个动作、每分钟调度 tick、
  全局主开关 + 渠道 mode（manual/mock/api）三重闸门。
- `publishers.py`：新增 `publish_wechat_article()`（freepublish 真发布，认证账号前提）。
- agents.yaml：`publish-planner`/`media-maker`/`gatekeeper`/`publisher` 注册 + private.md；
  `media-maker`/`publisher` 有 executor。
- ui_app.py：`/admin/autopublish` 路由与调度线程已插入，**管理页模板 `autopublish.html` 未创建、pytest 未跑、整体未验证**。

本方案落地时：agent4 的发布执行 = 收尾并测试这套骨架；agent1/agent3 作为新的内容生产环节
插在骨架的"素材入池（collect）"之前（researcher 产出信息包 → writer 定稿 → critic 意见单 →
gatekeeper 放行 → publisher 发布）。

---

## 8. 端到端闭环图

```
选题池（CEO / 复盘产出）
  │
  ▼
[agent1 researcher]  → 信息包(facts+citation+conflicts)
  │
  ▼
[agent2 writer]      → 平台无关定稿（content-strategist 策划 → writer 成稿）
  │
  ▼
[agent3 critic]      → 意见单(评分卡) ──revise(≤2 轮)──▶ 回 agent2
  │                      │
  │                      ▼ accept / 超轮次
  ▼                    人工裁决(可选)
[gatekeeper]          → 合规/事实终审（人工按钮）
  │
  ▼
[agent4 publisher]    → 三重闸门发布（wechat 真发布 / 其余渠道 mock 或 needs_manual）
  │
  ▼
[回采] metrics 回填发布单 → 复盘（review.py 模式）→ 回灌选题池与排期权重
```

---

## 9. MVP 分期（每期可独立上线）

- **Phase 0**：本文档评审通过；冻结信息包/意见单 schema。
- **Phase 1 · 收尾发布底座**：完成 `autopublish.html` + pytest + 服务器部署验证（agent4 先可用）。
- **Phase 2 · researcher**：`researcher` agent 注册 + 信息包产出（Tavily 可开关降级）+ 红线校验 + 测试。
- **Phase 3 · critic**：`critic` agent 注册 + 意见单 + 轮次控制 + 与 writer 的 revise 循环（建议先手动驱动）。
- **Phase 4 · 打通与回采**：把 1→3 接到 autopublish 的素材入池；metrics 回填 + 复盘回灌。
- 每期之间：真实发布至少完成一次人工全流程，积累一次真实效果数据再进下一期。

## 10. 配置项草案（全部默认关/省，进 config.json 新键 `CONTENT_PIPELINE`）

```json
{
  "CONTENT_PIPELINE": {
    "researcher": {"enabled": false, "max_searches": 3, "use_web": true},
    "critic": {"enabled": false, "max_revise_rounds": 2, "model_override": null},
    "metrics_backfill": {"enabled": false}
  }
}
```

---

## 附：与本文档相关的仓库内参考

- 现状代码：`roundtable.py`（并行独立推理）、`executors.py`（内容链）、`rag.py`、
  `autopublish.py`（发布骨架）、agents.yaml 中四个发布角色 private.md。
- 历史判断：`tasks.yaml`、`meeting/*.md`（决策陪练战略收敛）、DEPLOYMENT.md（服务器约束）。
- 行为准则：`feedback_confirm_before_meta_execution.md`（先对齐再执行）、
  `feedback_dev_workflow_cost.md`（付费 API 先静态验证再让用户测试）。
