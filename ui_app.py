"""
super_brain 控制台

主界面 = 圆桌讨论聊天窗口（GPT 类对话窗口的形态）：勾专家、提问题、看 Round 1/Round 2/
会议纪要，就是这个产品的核心业务，不是一堆功能入口里的一项。

inbox/dispatcher/自动化通道这些"助手·执行工具"相关的运维操作，全部挪到 /admin 二级页面——
它们是配角，不该占主界面的版面。

调用机制：圆桌讨论全程走 roundtable.py 的纯 Python 实现（urllib 直连 DeepSeek API + 线程池
并行），不经过 Claude Code 的 Agent 工具，UI 进程本身就是唤起圆桌的主体。

不是给外部客户用的产品界面，是本机调试/操作工具，默认只监听 127.0.0.1。

运行：
    python G:\\code\\super_brain\\ui_app.py
    然后打开 http://127.0.0.1:5151
"""
import json
import logging
import re
import secrets
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, session, url_for

import dispatcher
import i18n
import publishers
import roundtable
from log_setup import configure_logging

configure_logging()
logger = logging.getLogger("super_brain.ui")

app = Flask(__name__)
app.secret_key = secrets.token_hex(16)  # 仅本机单进程用，重启就换，不需要跨进程持久
app.jinja_env.globals["agent_label"] = i18n.agent_label  # 模板里到处能用，不用每次显式传

# 2026-08-15 修复：根因不只是"按钮没反馈"——之前 /roundtable/run 是同步阻塞处理，圆桌讨论
# 跑多久（15-40 秒）请求就挂多久，而 Flask 开发服务器默认单线程，这期间整个服务器无法响应
# 任何其他请求（连刷新页面都会卡住）。用户点了发送以为无响应，其实是页面真的进不来。
# 修法：圆桌讨论放到后台线程执行，请求立刻返回，页面用 _current_run 状态显示"进行中"，
# 前端轮询 /roundtable/status，跑完自动刷新展示结果。_roundtable_lock 保证同一时间只有
# 一场真实讨论在跑，防止重复点击/多标签页把钱重复花出去。
_roundtable_lock = threading.Lock()
_state_lock = threading.Lock()
_current_run: dict | None = None  # {"question", "agents", "started_at"}
_last_run_error: str | None = None


def _set_current_run(value: dict | None) -> None:
    global _current_run
    with _state_lock:
        _current_run = value


def _get_current_run() -> dict | None:
    with _state_lock:
        return _current_run


def _set_last_error(message: str | None) -> None:
    global _last_run_error
    with _state_lock:
        _last_run_error = message


def _pop_last_error() -> str | None:
    """跟 flask session 的 flash 效果一样——读一次就清空，不重复展示。"""
    global _last_run_error
    with _state_lock:
        message, _last_run_error = _last_run_error, None
        return message


# 2026-08-15 新增：圆桌讨论出会议纪要之后，直接调 ops-assistant 写头条+公众号草稿——
# 跟圆桌讨论一样是真实付费调用（DeepSeek + 微信 API），同样的教训，同样的模式：
# 后台线程执行 + 锁防重复点击 + 状态轮询，不再犯"同步阻塞卡住整个服务器"的错误。
_draft_lock = threading.Lock()
_current_draft: dict | None = None  # {"minutes_path", "started_at"}
_last_draft_error: str | None = None


def _set_current_draft(value: dict | None) -> None:
    global _current_draft
    with _state_lock:
        _current_draft = value


def _get_current_draft() -> dict | None:
    with _state_lock:
        return _current_draft


def _set_draft_error(message: str | None) -> None:
    global _last_draft_error
    with _state_lock:
        _last_draft_error = message


def _pop_draft_error() -> str | None:
    global _last_draft_error
    with _state_lock:
        message, _last_draft_error = _last_draft_error, None
        return message


SUPER_BRAIN = Path(r"G:\code\super_brain")
INBOX = SUPER_BRAIN / "inbox.md"
LOG_FILE = SUPER_BRAIN / "logs" / "super_brain.log"
DISPATCHER_SCRIPT = SUPER_BRAIN / "dispatcher.py"
CONFIG_PATH = SUPER_BRAIN / "config.json"
DRAFT_LOG_DIR = SUPER_BRAIN / "draft_log"


def categorize_agents(registry: dict[str, dict]) -> tuple[list, list, list]:
    """跟 2026-08-15 定的原则对齐：不是简单按 type 分组，是按"该怎么被唤起"分组。
    - roundtable：核心圆桌决策，主界面的聊天窗口就是它的真实调用入口（Python 直连，非 inbox）
    - assistant：有 executor，走 inbox+dispatcher 是真实执行，是 admin 页的自动化通道
    - conversation：其余（ship/coordinator/writer 这类）——没有 executor，只能对话内 @ 唤起
    """
    roundtable_agents, conversation, assistant = [], [], []
    for entry in sorted(registry.values(), key=lambda a: a.get("name", "")):
        if entry.get("type") == "roundtable":
            roundtable_agents.append(entry)
        elif entry.get("executor"):
            assistant.append(entry)
        else:
            conversation.append(entry)
    return roundtable_agents, conversation, assistant


def load_config_safe() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning(f"UI：读取 config.json 失败，当作空配置处理：{exc}")
        return {}


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


def load_draft_log() -> dict[str, dict]:
    """按 minutes_path 建索引——history 渲染时按会议纪要路径查有没有生成过草稿，
    刷新页面/重启服务都不丢，跟 roundtable_log 是同一个持久化思路。
    """
    if not DRAFT_LOG_DIR.exists():
        return {}
    index: dict[str, dict] = {}
    for path in DRAFT_LOG_DIR.glob("*.json"):
        try:
            entry = json.loads(path.read_text(encoding="utf-8-sig"))
            index[entry["minutes_path"]] = entry
        except (json.JSONDecodeError, OSError, KeyError):
            logger.warning(f"UI：草稿记录读取失败，跳过：{path}")
    return index


def persist_draft_log(minutes_path: str, result: dict) -> None:
    DRAFT_LOG_DIR.mkdir(parents=True, exist_ok=True)
    entry = {
        "minutes_path": minutes_path,
        "result": result,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    slug = re.sub(r"[^\w一-鿿-]", "-", Path(minutes_path).stem)[:40].strip("-") or "untitled"
    out_path = DRAFT_LOG_DIR / f"{slug}.json"
    out_path.write_text(json.dumps(entry, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"UI：草稿生成记录已落盘：{out_path}")


def render_chat(**extra):
    """主界面：圆桌讨论聊天窗口。"""
    registry = dispatcher.load_agent_registry()
    roundtable_agents, _, _ = categorize_agents(registry)
    config = load_config_safe()
    # 表单校验/锁冲突这类错误在原始请求里就能立刻判断，走 session flash；
    # 后台线程执行途中失败的错误，原始请求早就返回了，走 _last_run_error / _last_draft_error。
    error = session.pop("roundtable_error", None) or _pop_last_error()
    draft_error = session.pop("draft_error", None) or _pop_draft_error()

    return render_template(
        "index.html",
        roundtable_agents=roundtable_agents,
        history=roundtable.load_roundtable_history(),
        meeting_minutes_dir=config.get("MEETING_MINUTES_DIR"),
        roundtable_error=error,
        current_run=_get_current_run(),
        draft_log=load_draft_log(),
        current_draft=_get_current_draft(),
        draft_error=draft_error,
        **extra,
    )


def render_admin(**extra):
    """二级页面：inbox / dispatcher / 自动化通道这些运维操作，不是主界面。"""
    registry = dispatcher.load_agent_registry()
    roundtable_agents, conversation_agents, assistant_agents = categorize_agents(registry)
    messages = parse_all_messages_for_display()
    pending_count = sum(1 for m in messages if m.get("status") == "pending")
    config = load_config_safe()

    recent_log = extra.pop("recent_log", None)
    if recent_log is None:
        recent_log = "\n".join(
            LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()[-40:]
        ) if LOG_FILE.exists() else ""

    return render_template(
        "admin.html",
        roundtable_agents=roundtable_agents,
        conversation_agents=conversation_agents,
        assistant_agents=assistant_agents,
        messages=messages,
        pending_count=pending_count,
        recent_log=recent_log,
        meeting_minutes_dir=config.get("MEETING_MINUTES_DIR"),
        **extra,
    )


@app.route("/")
def index():
    return render_chat()


@app.route("/roundtable/run", methods=["POST"])
def roundtable_run():
    """圆桌讨论的真实调用入口——直接调 roundtable.run_roundtable()（纯 Python，线程池并行
    唤起多个 agent，直连 DeepSeek API），不经过 inbox，不经过 dispatcher.py，也不经过
    Claude Code 的 Agent 工具。这是会花 DeepSeek 额度的真实调用，不是预览。

    实际执行放到后台线程——本请求只负责校验+起线程，立刻返回，不阻塞 Flask 主线程。
    这样"讨论进行中"这件事本身能被页面如实展示出来，而不是让整个服务器卡 15-40 秒。
    """
    agent_names = request.form.getlist("agents")
    question = request.form.get("question", "").strip()

    if len(agent_names) < 2 or not question:
        logger.warning(
            f"UI：圆桌讨论表单校验失败（至少选 2 位专家 + 填问题），agents={agent_names}, question={question!r}"
        )
        session["roundtable_error"] = "至少选 2 位专家，并填写讨论的问题。"
        return redirect(url_for("index"))

    if not _roundtable_lock.acquire(blocking=False):
        logger.warning(
            f"UI：圆桌讨论请求被拒绝——已有一场讨论正在进行中（大概率是重复点击/多标签页），"
            f"本次提交的问题：{question!r}"
        )
        session["roundtable_error"] = "已经有一场圆桌讨论正在进行中，请等它结束（通常 15-40 秒）再提交，不用重复点击。"
        return redirect(url_for("index"))

    _set_current_run({
        "question": question,
        "agents": agent_names,
        "started_at": datetime.now().strftime("%H:%M:%S"),
    })
    logger.info(f"UI：触发圆桌讨论（后台线程）-> question={question!r}, agents={agent_names}")

    def _worker():
        try:
            roundtable.run_roundtable(agent_names, question)
        except roundtable.RoundtableError as exc:
            logger.warning(f"UI：圆桌讨论参数错误：{exc}")
            _set_last_error(str(exc))
        except Exception:
            logger.exception("UI：圆桌讨论执行失败")
            _set_last_error("圆桌讨论执行失败，详情看 logs/super_brain.log")
        finally:
            _set_current_run(None)
            _roundtable_lock.release()

    threading.Thread(target=_worker, daemon=True).start()
    return redirect(url_for("index"))


@app.route("/roundtable/status")
def roundtable_status():
    """前端轮询用——讨论进行中时如实告知，跑完了就让前端自动刷新展示结果。"""
    return jsonify({"running": _get_current_run() is not None})


@app.route("/minutes/draft", methods=["POST"])
def minutes_draft():
    """圆桌讨论出会议纪要之后，直接调助手 agent（ops-assistant）把它写成头条 + 公众号草稿。
    跟圆桌讨论一样：后台线程执行、锁防重复点击、状态轮询，避免重复付费调用。
    """
    minutes_path = request.form.get("minutes_path", "").strip()
    if not minutes_path:
        session["draft_error"] = "没有会议纪要路径，没法生成草稿。"
        return redirect(url_for("index"))

    if not _draft_lock.acquire(blocking=False):
        logger.warning(f"UI：草稿生成请求被拒绝——已有一份在生成中（大概率重复点击），minutes_path={minutes_path!r}")
        session["draft_error"] = "已经有一份草稿正在生成中，请等它结束再提交。"
        return redirect(url_for("index"))

    _set_current_draft({
        "minutes_path": minutes_path,
        "started_at": datetime.now().strftime("%H:%M:%S"),
    })
    logger.info(f"UI：触发助手 agent 生成草稿（后台线程）-> minutes_path={minutes_path!r}")

    def _worker():
        try:
            api_key = json.loads(
                dispatcher.DEEPSEEK_CONFIG_PATH.read_text(encoding="utf-8-sig")
            )["DEEPSEEK_API_KEY"]
            result = dispatcher.execute_ops_assistant_from_minutes(minutes_path, api_key)
            persist_draft_log(minutes_path, result)
        except FileNotFoundError as exc:
            logger.warning(f"UI：草稿生成失败：{exc}")
            _set_draft_error(str(exc))
        except Exception:
            logger.exception("UI：草稿生成失败")
            _set_draft_error("草稿生成失败，详情看 logs/super_brain.log")
        finally:
            _set_current_draft(None)
            _draft_lock.release()

    threading.Thread(target=_worker, daemon=True).start()
    return redirect(url_for("index"))


@app.route("/minutes/draft/status")
def minutes_draft_status():
    return jsonify({"running": _get_current_draft() is not None})


@app.route("/admin")
def admin():
    return render_admin()


@app.route("/inbox/new", methods=["POST"])
def inbox_new():
    to = request.form.get("to", "").strip()
    message = request.form.get("message", "").strip()

    if not to or not message:
        logger.warning(f"UI：新建留言表单缺字段（to={to!r}, message={message!r}），已拒绝")
        return redirect(url_for("admin"))

    # 单行硬约束——UI 层就该挡住，不要指望 dispatcher 兜底
    if "\n" in message or "\r" in message:
        message = message.replace("\r", " ").replace("\n", " ")
        logger.warning("UI：留言内容包含换行，已自动压成单行（inbox.md 的硬约束：Message 必须单行）")

    append_inbox_message(to, message)
    return redirect(url_for("admin"))


@app.route("/dispatcher/run", methods=["POST"])
def dispatcher_run():
    dry_run = request.form.get("mode") == "dry-run"
    output = run_dispatcher(dry_run)
    return render_admin(recent_log=output, just_ran=True, dry_run=dry_run)


@app.route("/config/set", methods=["POST"])
def config_set():
    value = request.form.get("meeting_minutes_dir", "").strip()
    if not value:
        logger.warning("UI：会议纪要目录表单提交了空值，已忽略")
        return redirect(request.referrer or url_for("index"))

    config = load_config_safe()
    config["MEETING_MINUTES_DIR"] = value
    CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    Path(value).mkdir(parents=True, exist_ok=True)
    logger.info(f"UI：MEETING_MINUTES_DIR 已设置为 {value}（首次配置，之后不再需要重复问）")
    return redirect(request.referrer or url_for("index"))


if __name__ == "__main__":
    logger.info("===== super_brain UI 启动，http://127.0.0.1:5151 =====")
    app.run(host="127.0.0.1", port=5151, debug=False, threaded=True)
