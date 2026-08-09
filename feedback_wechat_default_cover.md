---
name: feedback-wechat-default-cover
description: 微信公众号草稿的默认封面图直链，以后 /ship wechat 不用再问
metadata: 
  node_type: memory
  type: feedback
  scope: personal
  originSessionId: 38c3ca82-ccc3-4ec1-acc5-08349daf0e17
  modified: 2026-08-09T06:28:39.066Z
---

用户指定：以后创建微信公众号草稿（`mcp__wechat__create_wechat_draft` 的 `image_url` 参数）统一用这张封面图，不用每次都问用户要链接：

```
https://mmbiz.qpic.cn/sz_mmbiz_png/zxyiabDHTT9BoXia4OSAJVImNkIPB7Fq6ZdIkKhZvicQAbJ4jPLTE0n7pMCWRojGwibnZZNhtiahcuQJzrJ2VEibHbywOvyGjOibBoHh0c5rHmXJnA/640?wx_fmt=png&from=appmsg
```

**Why:** 微信 CDN 上的图片链接是 ship skill 里唯一"亲测有效"的封面图来源类型（临时/自编域名会 403 或 502），用户干脆固定了一张长期通用的封面图，省得每次都要临时找一张。

**How to apply:**
- 跑 ship 的 wechat 任务、需要 `image_url` 时，直接用上面这个链接，不用再询问用户要封面图。
- 如果这次提交失败（微信接口报图片下载失败之类的错误），才需要跟用户说明情况、问要不要换一张——不要假设链接永久有效。
- 如果用户明确说"这次换一张图"或给了新链接，以当次指定的为准，不要强行用默认的。
