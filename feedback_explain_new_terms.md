---
name: feedback-explain-new-terms
description: 引入新技术名词/工具（库、框架、协议等）时要主动解释其作用，不要假设用户已知
metadata: 
  node_type: memory
  type: feedback
  scope: personal
  originSessionId: b45a08b3-2564-4e6c-aef5-813d705bc621
  modified: 2026-08-09T06:26:20.656Z
---

在对话或方案里第一次提到一个新的技术名词、工具、库时（例如 Alembic、Flask-Migrate 这类），必须顺带简要说明它是什么、解决什么问题，不能只提名字就往下继续操作。

**Why:** 用户在 Alembic 迁移工具的操作被我直接提出（未加解释）后打断提问"Alembic 是什么？"，说明默认对方已经了解某个名词是不安全的假设，哪怕这个名词在技术圈很常见。

**How to apply:** 每次引入新概念（新依赖、新架构模式、新协议等）第一次出现时，用 1-2 句话说明"这是什么 / 为什么现在要用它"，再继续技术细节或操作。已经解释过的概念在同一会话里不用重复解释。这条原则对所有类型的新名词都适用，不局限于本次的 Alembic 场景。
