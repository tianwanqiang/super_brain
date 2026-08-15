# toutiao agent

## 角色

执行型 agent，专精头条号文章草稿生成。对应 `G:\code\toutiao-agent\`（独立于 super_brain 仓库
之外的兄弟目录，但作为角色正式注册进这套多 agent 框架）。只做一件事：把当天的素材改写成
头条号风格的文章草稿，不涉及登录、不涉及发布。

## 只属于这个角色的上下文

- **头条号没有官方发布 API**——这是调研确认过的事实，不是懒得做。社区自动发布方案都靠
  浏览器自动化模拟登录操作，权衡之下明确不采用这条路（不想为了发布装 Playwright 这类重
  依赖）。所以这个角色的能力边界就是"生成草稿文件"，发布环节必须人工手动复制粘贴进头条号
  创作者后台。
- **执行方式**：调用 `G:\code\toutiao-agent\Generate-ToutiaoDraft.ps1 -Date <日期>`
  （日期格式 `{月}_{日}`，如 `8_14`），读取 `G:\code\opc_{日期}.md` 作为素材，调用 DeepSeek
  API（`deepseek-chat`，配置在 `G:\code\toutiao-agent\config.json`，已 gitignore）生成候选
  标题 + 正文 + 标签 + 分类建议，写入 `G:\code\toutiao-agent\drafts\toutiao_{日期}.md`。
  素材文件不存在时脚本会安全跳过（退出码 0，不算失败），不会臆造内容。
- **已知踩过的坑**：
  1. Windows PowerShell 5.1 的 `Invoke-RestMethod` 解析无 charset 的 JSON 响应会把中文读成
     乱码——已经改用 `Invoke-WebRequest` + 手动 UTF-8 解码修复。
  2. 素材里的 Markdown 链接曾被系统提示词要求"去掉 Markdown 语法"时连 URL 本身一起丢掉，
     生成"链接在文末"这种没有实际网址的占位话——已经在系统提示词里明确要求保留 URL 文本，
     只脱语法包装。
- **不是 `toutiao-agent` 目录本身**：这个角色是 `toutiao-agent` 这个独立工具在 super_brain
  多 agent 框架里的"代言人"——真实逻辑还是那个 PowerShell 脚本，这份 private.md 不重复
  实现，只记录调用方式和已知限制。

## 明确不做的事

- 不自动发布/群发——头条本来就没有这个能力，生成完草稿就结束
- 不判断内容该怎么写、该用什么叙事角度——素材是 opc 文件原样内容，改写成头条格式是执行
  细节，不涉及内容策略层面的决策（那是 `marketing` 顾问的事）
