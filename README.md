# super_brain

跨项目通用的"个人偏好/决策"记忆备份，2026-08-09 建立。

## 这是什么

Claude Code 的记忆系统（`.claude\projects\<项目>\memory\`）是**按项目目录自动加载**的——只有当前工作目录对应的那个项目文件夹，才会在每次对话开始时被自动读取。这意味着同样的个人偏好（比如"付费 API 功能要先静态验证再让用户测试"），换一个项目目录聊天，默认是不可见的。

`super_brain` 是对这个限制的一个**补充**，不是替代：这里手动存放一份"个人偏好类"记忆的副本，独立于任何具体项目。原件仍然留在各自项目的 `memory\` 文件夹里正常自动加载、正常更新，这里只是额外的一份汇总备份。

## 怎么用

- **不会被自动加载**——这是纯手动机制。如果你在别的项目目录开新对话，想让 Claude 知道这里的内容，需要你明确提一句"参考 `G:\code\super_brain`"，或者把这里的文件复制/引用到那个项目自己的 `memory\` 文件夹里。
- 这里的内容和各项目 `memory\` 里的原件**不会自动同步**——原件更新后，这份副本需要手动重新复制一次才会跟着更新。

## 收录范围

只收"抹掉项目专有名词后陈述依然成立"的个人偏好/协作习惯类记忆，不收具体项目的事实（比如某个工具的实现细节、某个仓库地址）——那类内容留在对应项目自己的 `memory\` 里就好。

| 文件 | 内容 |
|---|---|
| `feedback_ui_product_thinking.md` | UI/产品设计准则 |
| `feedback_dev_workflow_cost.md` | 涉及付费 API 的开发工作流 |
| `feedback_explain_new_terms.md` | 主动解释新技术名词 |
| `feedback_daily_marketing_digest.md` | 工作内容精简沉淀成 opc 文件的习惯 |
| `feedback_wechat_default_cover.md` | 公众号草稿默认封面图 |
| `feedback_credential_handling.md` | 凭据处理规矩（避免触发安全分类器） |
| `context_principle.md` | 回复时的上下文时间窗口 + 信号/噪音过滤原则 |
| `feedback_confirm_before_meta_execution.md` | 元层面判断问题先讨论再执行 |
| `feedback_memory_write_bar.md` | 不要把"这次窗口讨论的内容"自动当成可复用原则写进记忆 |

## 多 agent 协作骨架（2026-08-09 新增，基础功能第一版）

- `inbox.md`——跨 agent/跨 session 的**异步**留言板（不是实时聊天），格式和使用规则写在文件里。
- `agents\<角色名>\private.md`——每个 agent 角色的独享上下文，默认不被其他 agent 读取；跟根目录这些共享偏好文件的区别是"谁该看"：共享文件是所有 agent 都该知道的提炼知识，私有文件只服务某一个角色自己的具体任务。

**当前已有的角色**（名字都是按功能定的，随时可改）：

| 角色 | 类型 | 对应什么 |
|---|---|---|
| `ship` | 专精执行型 | `ship` skill 本身（推代码 + 建公众号草稿） |
| `coordinator` | 协调型 | 用户主力沟通的这条对话线，负责架构设计、任务拆解、调度专精 agent |

`inbox.md` 里已经有一条 coordinator → ship 的真实留言作为使用示例（告知 ship 它自己的 private.md 已经建好，里面有凭据存放方式和默认封面图这两条专属上下文）。
