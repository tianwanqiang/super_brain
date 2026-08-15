"""
super_brain 控制台 - 最小可用 UI

目的：之前"新建 inbox 留言、跑 dispatcher、看结果"这条业务流程，全靠手动编辑 inbox.md +
敲命令行——这个 UI 把这条流程摆到网页上，一是方便用，二是拿它来真实走一遍完整流程，
看哪里不通顺。

不是给外部客户用的产品界面，是本机调试/操作工具，默认只监听 127.0.0.1。

运行：
    python G:\\code\\super_brain\\ui_app.py
    然后打开 http://127.0.0.1:5151
"""
import logging
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from flask import Flask, redirect, render_template, request, url_for

import dispatcher
from log_setup import configure_logging

configure_logging()
logger = logging.getLogger("super_brain.ui")

app = Flask(__name__)

SUPER_BRAIN = Path(r"G:\code\super_brain")
INBOX = SUPER_BRAIN / "inbox.md"
LOG_FILE = SUPER_BRAIN / "logs" / "super_brain.log"
DISPATCHER_SCRIPT = SUPER_BRAIN / "dispatcher.py"


def parse_all_messages_for_display() -> list[dict]:
    """跟 dispatcher.parse_pending_messages 不同——这个是给 UI 展示用的，不过滤 pending/done，
    也不因为格式错误就丢弃（只是标记出来），方便在页面上看到 inbox 里真实的全貌，包括坏数据。
    """
    if not INBOX.exists():
        return []
    text = INBOX.read_text(encoding="utf-8-sig")
    if dispatcher.MESSAGE_LOG_HEADING not in text:
        return []
    text = text.split(dispatcher.MESSAGE_LOG_HEADING, 1)[1]

    messages = []
    for raw_block in text.split("\n---\n"):
        block = raw_block.strip()
        if "From:" not in block or "To:" not in block:
            continue
        entry: dict[str, str] = {}
        for line in block.splitlines():
            m = re.match(r"^(From|To|Time|Status|Message):\s*(.*)$", line.strip())
            if m:
                entry[m.group(1).lower()] = m.group(2).strip()
        messages.append(entry)
    messages.reverse()  # 最新的留言排最前面，方便看
    return messages


def append_inbox_message(to: str, message: str, sender: str = "用户") -> None:
    """按 inbox.md 的硬约束格式追加一条留言（追加，不修改历史）。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    block = (
        f"\n---\n"
        f"From: {sender}\n"
        f"To: {to}\n"
        f"Time: {now}\n"
        f"Status: pending\n"
        f"Message: {message}\n"
    )
    with INBOX.open("a", encoding="utf-8") as f:
        f.write(block)
    logger.info(f"UI：新增 inbox 留言 -> To={to}, Message={message!r}")


def run_dispatcher(dry_run: bool) -> str:
    """真的跑一次 dispatcher.py（子进程，而不是在同一个 Flask 进程里 import 调用），
    这样跟命令行用户实际会经历的路径完全一致，UI 只是换了个触发入口，不是另一套逻辑。
    """
    args = [sys.executable, str(DISPATCHER_SCRIPT)]
    if dry_run:
        args.append("--dry-run")
    logger.info(f"UI：触发 dispatcher.py（{'dry-run' if dry_run else '真实执行'}）")
    result = subprocess.run(
        args, capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(SUPER_BRAIN),
    )
    output = result.stdout or ""
    if result.stderr:
        output += "\n--- stderr ---\n" + result.stderr
    logger.info(f"UI：dispatcher.py 运行结束，exit={result.returncode}")
    return output


@app.route("/")
def index():
    registry = dispatcher.load_agent_registry()
    agents = sorted(registry.values(), key=lambda a: a.get("name", ""))
    messages = parse_all_messages_for_display()
    pending_count = sum(1 for m in messages if m.get("status") == "pending")

    recent_log = ""
    if LOG_FILE.exists():
        lines = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
        recent_log = "\n".join(lines[-40:])

    return render_template(
        "index.html",
        agents=agents,
        messages=messages,
        pending_count=pending_count,
        recent_log=recent_log,
    )


@app.route("/inbox/new", methods=["POST"])
def inbox_new():
    to = request.form.get("to", "").strip()
    message = request.form.get("message", "").strip()

    if not to or not message:
        logger.warning(f"UI：新建留言表单缺字段（to={to!r}, message={message!r}），已拒绝")
        return redirect(url_for("index"))

    # 单行硬约束——UI 层就该挡住，不要指望 dispatcher 兜底
    if "\n" in message or "\r" in message:
        message = message.replace("\r", " ").replace("\n", " ")
        logger.warning("UI：留言内容包含换行，已自动压成单行（inbox.md 的硬约束：Message 必须单行）")

    append_inbox_message(to, message)
    return redirect(url_for("index"))


@app.route("/dispatcher/run", methods=["POST"])
def dispatcher_run():
    dry_run = request.form.get("mode") == "dry-run"
    output = run_dispatcher(dry_run)
    registry = dispatcher.load_agent_registry()
    agents = sorted(registry.values(), key=lambda a: a.get("name", ""))
    messages = parse_all_messages_for_display()
    pending_count = sum(1 for m in messages if m.get("status") == "pending")
    return render_template(
        "index.html",
        agents=agents,
        messages=messages,
        pending_count=pending_count,
        recent_log=output,
        just_ran=True,
        dry_run=dry_run,
    )


if __name__ == "__main__":
    logger.info("===== super_brain UI 启动，http://127.0.0.1:5151 =====")
    app.run(host="127.0.0.1", port=5151, debug=False)
