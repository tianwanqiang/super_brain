"""
super_brain digest - "今日待你关注的事项"清单 + 每日批量汇总

两个不同成本量级的函数，别混用：
- build_today_digest()：零成本、只读本地文件，随时可以调（打开页面就能算一次）——
  给"主动触发层"用，不调用任何 LLM/外部 API。
- run_daily_batch()：会真的花 DeepSeek 额度（如果当天有 opc 笔记，会调用
  executors.execute_ops_assistant_full 生成头条/公众号草稿）——给 18 点定时批处理用，
  不应该被"打开页面"这种高频动作触发，只应该被真正的每日一次调度触发。
"""
import logging
from datetime import datetime
from pathlib import Path

import dispatcher
import roundtable
import tasks
from agent_registry import load_agent_registry
from paths import INBOX, OPC_ROOT, SUPER_BRAIN

logger = logging.getLogger("super_brain.digest")

DAILY_DIGESTS_DIR = SUPER_BRAIN / "daily_digests"


def build_today_digest() -> dict:
    """零成本聚合——只读 tasks.yaml / inbox.md / conversations/*.json，不调用任何 LLM。
    是"主动触发层"的核心：CEO 打开页面就能看到"今天有什么需要我看"，不用自己想起来去问。
    """
    today = datetime.now().strftime("%Y-%m-%d")

    pending = tasks.pending_tasks()

    pending_inbox: list[dict] = []
    if INBOX.exists():
        registry = load_agent_registry()
        pending_inbox = dispatcher.parse_pending_messages(INBOX.read_text(encoding="utf-8-sig"), registry)

    today_conversations = [
        c for c in roundtable.load_all_conversations()
        if (c.get("updated_at") or "").startswith(today)
    ]

    return {
        "date": today,
        "pending_tasks": pending,
        "pending_inbox": pending_inbox,
        "today_conversations": today_conversations,
    }


def _render_digest_markdown(digest: dict, ops_result: dict | None) -> str:
    lines = [f"# {digest['date']} 每日汇总", ""]

    lines.append(f"## 今日圆桌讨论（{len(digest['today_conversations'])} 场）")
    if digest["today_conversations"]:
        for c in digest["today_conversations"]:
            lines.append(f"- {c.get('title', '（无标题）')} · 会话 id: `{c['id']}`")
    else:
        lines.append("（今天没有召开圆桌讨论）")
    lines.append("")

    lines.append(f"## 待你确认的任务（{len(digest['pending_tasks'])} 条）")
    if digest["pending_tasks"]:
        for t in digest["pending_tasks"]:
            lines.append(f"- [{t['id']}] {t['description']}"
                          + (f" → {t['assignee_agent']}" if t.get("assignee_agent") else ""))
    else:
        lines.append("（没有待确认的任务）")
    lines.append("")

    lines.append(f"## 待处理的 inbox 留言（{len(digest['pending_inbox'])} 条）")
    if digest["pending_inbox"]:
        for m in digest["pending_inbox"]:
            lines.append(f"- [{m.get('to')}] {m.get('from', '?')} @ {m.get('time', '?')}：{m.get('message', '')}")
    else:
        lines.append("（没有待处理的留言）")
    lines.append("")

    if ops_result is not None:
        lines.append("## 今日内容分发结果")
        for key, value in ops_result.items():
            lines.append(f"- {key}: {value}")
        lines.append("")

    return "\n".join(lines)


def run_daily_batch(date: str | None = None, api_key: str | None = None) -> Path:
    """每日批处理——如果今天有 opc 笔记，先触发 ops-assistant 生成头条/公众号草稿
    （这一步会真的调用 DeepSeek），然后把结果和今天的任务/圆桌/inbox 状态汇总成一份
    可读文档，落盘到 daily_digests/{date}.md。

    只应该被真正的每日调度触发一次；重复调用不报错，但会重复花 DeepSeek 额度
    （如果当天 opc 笔记存在的话），调用方自己负责"今天是否已经跑过"这个判断
    （见 ui_app.py 里的调度线程实现）。
    """
    now = datetime.now()
    date = date or f"{now.month}_{now.day}"  # opc 文件用的日期格式：{月}_{日}
    display_date = now.strftime("%Y-%m-%d")

    digest = build_today_digest()

    ops_result = None
    opc_path = OPC_ROOT / f"opc_{date}.md"
    if opc_path.exists() and api_key:
        import executors
        try:
            ops_result = executors.execute_ops_assistant_full(date, api_key)
        except Exception:
            logger.exception("每日批处理：ops-assistant 执行失败，仍会继续生成汇总文档")
            ops_result = {"error": "ops-assistant 执行失败，详情看日志"}
    elif not opc_path.exists():
        logger.info(f"每日批处理：{opc_path} 不存在，跳过内容分发，只生成汇总")

    DAILY_DIGESTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = DAILY_DIGESTS_DIR / f"{display_date}.md"
    out_path.write_text(_render_digest_markdown(digest, ops_result), encoding="utf-8")
    logger.info(f"每日汇总已落盘：{out_path}")
    return out_path
