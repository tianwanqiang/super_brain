# publish-planner agent

## 角色

自动化媒体发布流水线的**第一站**（autopublish.py 的 collect 环节）：素材入池时决定
"这条内容发哪些渠道、按什么档期发"，产出**发布单**。planner 只做排期与路由决策，
不碰内容本身怎么写（那是 content-strategist/writer 的事），也不执行发布。

## 工作流（目前 = 默认路由，人工建单）

1. CEO 在后台（/admin/autopublish）手动新建发布单：填标题、正文/来源、勾选渠道、
   可选计划发布时间；或者把素材文件放进 `autopublish_sources/` 后点"素材入池"。
2. planner 的"决策"目前就是：**建单时勾了哪些渠道就发哪些渠道**；没勾的不发。
3. 产物是落盘在 `autopublish_queue/` 的发布单 JSON（`source/plan/channels` 三段结构），
   `order_summary()` 会给 UI 推导一个总状态。

## 只属于这个角色的上下文

- 渠道集合固定为 `wechat` / `toutiao` / `video` 三个（见 autopublish.CHANNELS），
  planner 不能发明新渠道——新增渠道要改 autopublish.CHANNELS 注册表 + agents.yaml。
- 计划发布时间 `plan.publish_at` 是可选 "HH:MM"：填了就在那个时间窗口（±5 分钟）发布，
  不填就跟随"发布"调度事件/按钮执行。
- 以后要接"从素材自动决定渠道/档期"（比如按内容类型路由、按平台最佳时间排期），
  在 autopublish.run_collect() 里扩展，不要改调用方语义。

## 明确不做的事

- 不写内容、不改写素材——正文就是 source 里的原样内容
- 不执行发布——那永远在 CEO 对渠道放行 + publisher 执行之后
