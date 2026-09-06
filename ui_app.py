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
import os
import queue
import re
import secrets
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from flask import Flask, Response, jsonify, redirect, render_template, request, session, url_for

import agent_registry
import autopublish
import config_check
import config_store
import digest
import dispatcher
import executors
import i18n
import llm_client
import mailer
import private_chat
import publishers
import rag
import review
import roundtable
import tasks
import video_prompt
import workflow as workflow_engine
from log_setup import LOG_FILE, configure_logging
from paths import AGENTS_DIR, SUPER_BRAIN

configure_logging()
logger = logging.getLogger("super_brain.ui")

app = Flask(__name__)
# 本机开发默认随机生成、重启就换（单进程用，够了）；部署到服务器后如果设了
# SUPER_BRAIN_SECRET_KEY，用固定值——不然每次 CI/CD 重新部署容器都会让所有人重新登录。
app.secret_key = os.environ.get("SUPER_BRAIN_SECRET_KEY") or secrets.token_hex(16)

# 登录密码——不设置这个环境变量就完全不启用登录（本机开发保持现在"直接打开就能用"的行为）。
# 部署到服务器、对公网开放时，必须设置这个环境变量，否则任何人都能看到真实的圆桌讨论记录、
# 私聊内容、任务清单，还能点按钮触发真实的 DeepSeek 付费调用。
SUPER_BRAIN_PASSWORD = os.environ.get("SUPER_BRAIN_PASSWORD")


@app.before_request
def _require_login():
    if not SUPER_BRAIN_PASSWORD:
        return  # 没配密码，本机开发场景，不拦
    if request.endpoint in ("login", "static") or request.path.startswith("/static/"):
        return
    if not session.get("authenticated"):
        return redirect(url_for("login", next=request.path))


@app.route("/login", methods=["GET", "POST"])
def login():
    if not SUPER_BRAIN_PASSWORD:
        return redirect(url_for("index"))
    error = None
    if request.method == "POST":
        if request.form.get("password") == SUPER_BRAIN_PASSWORD:
            session["authenticated"] = True
            next_path = request.args.get("next") or url_for("index")
            return redirect(next_path)
        error = "密码不对"
    return render_template("login.html", error=error)


@app.route("/logout", methods=["POST"])
def logout():
    session.pop("authenticated", None)
    return redirect(url_for("login"))
app.jinja_env.globals["agent_label"] = i18n.agent_label  # 模板里到处能用，不用每次显式传
app.jinja_env.globals["get_task"] = lambda tid: next(
    (t for t in tasks.load_tasks() if t["id"] == tid), None
)  # Round 3 结论落地的任务条目，按 id 查——个人量级，直接线性查找不需要建索引

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


# 2026-08-18 新增：流式渲染——跟老的"提交+轮询整页刷新"路径完全独立并存，不是替换关系。
# 前端优先走这条（composer 提交 -> /roundtable/run-stream 拿 conversation_id -> 建立
# SSE 连接实时渲染），SSE 连接失败/浏览器不支持时自动退化回老的轮询路径，两条路径共用
# 同一个 _roundtable_lock，不会互相冲突或重复扣费。
_stream_queues_lock = threading.Lock()
_stream_queues: dict[str, "queue.Queue"] = {}


def _create_stream_queue(conversation_id: str) -> "queue.Queue":
    q: "queue.Queue" = queue.Queue()
    with _stream_queues_lock:
        _stream_queues[conversation_id] = q
    return q


def _get_stream_queue(conversation_id: str) -> "queue.Queue | None":
    with _stream_queues_lock:
        return _stream_queues.get(conversation_id)


def _remove_stream_queue(conversation_id: str) -> None:
    with _stream_queues_lock:
        _stream_queues.pop(conversation_id, None)


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


INBOX = SUPER_BRAIN / "inbox.md"
DISPATCHER_SCRIPT = SUPER_BRAIN / "dispatcher.py"
CONFIG_PATH = config_store.CONFIG_PATH  # 统一权威常量（paths.CONFIG_PATH），不再本模块自拼
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
    """读 config.json（走 config_store 统一实现，缓存读）；任何读取问题都当"没配置"处理
    返回 {}，并打一条日志说明原因（不吞静默）。"""
    try:
        return config_store.read_config_cached(path=CONFIG_PATH)
    except config_store.ConfigReadError as exc:
        logger.warning(f"UI：读取 config.json 失败，当作空配置处理：{exc}")
        return {}


def get_meeting_minutes_dir() -> Path | None:
    """会议纪要预览（/draft/preview）需要知道当前配置的存放目录，作为允许预览的安全边界——
    跟 roundtable.write_meeting_minutes() 读的是同一个 config.json 字段。没配置就返回
    None，预览路由据此判断"当前完全不允许预览任何会议纪要"，不是报错，是正常的未配置状态。

    只认**当前**这个配置值，不兼容历史上曾经配置过、后来又改掉的旧目录——这是刻意的：
    如果什么旧目录都放行，安全边界会越放越松，且没有办法判断"这个旧目录是不是还归
    super_brain 管"。历史上因为 MEETING_MINUTES_DIR 配错（带引号/相对路径那次事故）
    产生的、記在旧会话记录里的畸形路径，不会因为这个函数而变得可预览，需要的话手动
    把物理文件挪到现在配置的目录下。
    """
    value = load_config_safe().get("MEETING_MINUTES_DIR")
    return Path(value) if value else None


def _load_config_for_update() -> tuple[dict | None, str | None]:
    """给"改一个字段、写回整个文件"这类保存路径用——跟 load_config_safe() 不一样，
    不能把"文件存在但读取失败"悄悄当成空配置返回，否则调用方会把这份假的空配置整个写回去，
    把 config.json 里已有的其他字段（尤其是各种 API_KEY）全部覆盖丢失。这是 2026-09-04
    真实发生过的事故：设置会议纪要目录时把已有的 API_KEY 覆盖掉了。

    返回 (config, None) 表示可以安全地在这份 config 基础上改字段再写回；
    返回 (None, 错误信息) 表示不能继续写，调用方应该原样中止、不碰 config.json。
    """
    if not CONFIG_PATH.exists():
        return {}, None
    try:
        return config_store.read_config(path=CONFIG_PATH), None
    except config_store.ConfigReadError as exc:
        return None, str(exc)


def _write_config_with_backup(config: dict) -> None:
    """写 config.json 前先把当前文件备份成 config.json.bak（只保留最近一份，不是历史
    版本链）——防止这次写入内容本身有问题、或者以后又出现类似覆盖丢失的 bug 时还有得救。
    实现委托 config_store（统一读写实现的唯一入口）。"""
    config_store.write_config_with_backup(config, path=CONFIG_PATH)


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


def render_chat(conversation_id: str | None = None, force_new: bool = False, **extra):
    """主界面：圆桌讨论聊天窗口，以会话为单位管理——不再是所有讨论堆在一条流水账里。"""
    registry = agent_registry.load_agent_registry()
    roundtable_agents, _, _ = categorize_agents(registry)
    config = load_config_safe()
    # 表单校验/锁冲突这类错误在原始请求里就能立刻判断，走 session flash；
    # 后台线程执行途中失败的错误，原始请求早就返回了，走 _last_run_error / _last_draft_error。
    error = session.pop("roundtable_error", None) or _pop_last_error()
    draft_error = session.pop("draft_error", None) or _pop_draft_error()
    config_success = session.pop("config_success", None)

    conversations = roundtable.load_all_conversations()

    active_conversation = None
    active_id = conversation_id
    if not force_new:
        if active_id:
            active_conversation = next((c for c in conversations if c["id"] == active_id), None)
        elif conversations:
            active_conversation = conversations[0]
            active_id = active_conversation["id"]
    if active_conversation is None:
        active_id = None  # 新建状态 / 会话不存在，都归一成"没有选中会话"

    return render_template(
        "index.html",
        roundtable_agents=roundtable_agents,
        conversations=conversations,
        active_conversation=active_conversation,
        active_conversation_id=active_id,
        meeting_minutes_dir=config.get("MEETING_MINUTES_DIR"),
        roundtable_error=error,
        config_success=config_success,
        current_run=_get_current_run(),
        draft_log=load_draft_log(),
        current_draft=_get_current_draft(),
        draft_error=draft_error,
        agent_labels_json=json.dumps(i18n.AGENT_LABELS_ZH, ensure_ascii=False),
        pending_tasks=tasks.pending_tasks(),
        sidebar_tab=request.args.get("tab", "conversations"),
        **extra,
    )


def render_admin(**extra):
    """二级页面：inbox / dispatcher / 自动化通道这些运维操作，不是主界面。"""
    registry = agent_registry.load_agent_registry()
    roundtable_agents, conversation_agents, assistant_agents = categorize_agents(registry)
    messages = parse_all_messages_for_display()
    pending_count = sum(1 for m in messages if m.get("status") == "pending")
    config = load_config_safe()

    recent_log = extra.pop("recent_log", None)
    if recent_log is None:
        recent_log = "\n".join(
            LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()[-40:]
        ) if LOG_FILE.exists() else ""

    # 机制 2·定期复盘——coordinator 没有 lessons.md 记录机制，不参与复盘列表
    review_agents = [a for a in sorted(registry.values(), key=lambda a: a.get("name", ""))
                      if a.get("name") != "coordinator"]
    review_state = {
        a["name"]: {"has_lessons": review.has_lessons(a["name"]), "reviews": review.list_reviews(a["name"])}
        for a in review_agents
    }

    return render_template(
        "admin.html",
        roundtable_agents=roundtable_agents,
        conversation_agents=conversation_agents,
        assistant_agents=assistant_agents,
        messages=messages,
        pending_count=pending_count,
        recent_log=recent_log,
        meeting_minutes_dir=config.get("MEETING_MINUTES_DIR"),
        toutiao_drafts_dir_configured=config.get("TOUTIAO_DRAFTS_DIR"),
        toutiao_drafts_dir_effective=str(publishers.get_toutiao_drafts_dir()),
        review_agents=review_agents,
        review_state=review_state,
        last_daily_batch_date=_last_daily_batch_date,
        rag_rebuild_results=session.pop("rag_rebuild_results", None),
        roundtable_error=session.pop("roundtable_error", None),
        config_success=session.pop("config_success", None),
        **extra,
    )


@app.route("/")
def index():
    conversation_id = request.args.get("conversation")
    force_new = request.args.get("new") == "1"
    return render_chat(conversation_id=conversation_id, force_new=force_new)


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
    conversation_id = request.form.get("conversation_id", "").strip() or None
    back_to = (lambda: redirect(url_for("index", conversation=conversation_id))
               if conversation_id else redirect(url_for("index")))

    if len(agent_names) < 2 or not question:
        logger.warning(
            f"UI：圆桌讨论表单校验失败（至少选 2 位专家 + 填问题），agents={agent_names}, question={question!r}"
        )
        session["roundtable_error"] = "至少选 2 位专家，并填写讨论的问题。"
        return back_to()

    if not _roundtable_lock.acquire(blocking=False):
        logger.warning(
            f"UI：圆桌讨论请求被拒绝——已有一场讨论正在进行中（大概率是重复点击/多标签页），"
            f"本次提交的问题：{question!r}"
        )
        session["roundtable_error"] = "已经有一场圆桌讨论正在进行中，请等它结束（通常 15-40 秒）再提交，不用重复点击。"
        return back_to()

    # 新会话场景：同步先建好会话（本地文件 IO，很快，不涉及网络），这样能立刻拿到
    # conversation_id 用于跳转；已有会话的追问场景直接复用传进来的 id。
    if not conversation_id:
        conversation_id = roundtable.create_conversation(agent_names, question)

    _set_current_run({
        "conversation_id": conversation_id,
        "question": question,
        "agents": agent_names,
        "started_at": datetime.now().strftime("%H:%M:%S"),
    })
    logger.info(f"UI：触发圆桌讨论（后台线程）-> conversation_id={conversation_id}, question={question!r}, agents={agent_names}")

    def _worker():
        try:
            roundtable.run_roundtable(agent_names, question, conversation_id=conversation_id)
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
    return redirect(url_for("index", conversation=conversation_id))


@app.route("/roundtable/status")
def roundtable_status():
    """前端轮询用——讨论进行中时如实告知，跑完了就让前端自动刷新展示结果。"""
    return jsonify({"running": _get_current_run() is not None})


@app.route("/roundtable/run-stream", methods=["POST"])
def roundtable_run_stream():
    """跟 /roundtable/run 逻辑基本一致（同样的校验、同样的锁、同样的后台线程执行），
    区别只在于：会创建一个流式队列，Round 1/2/3 的每个 chunk 实时推进去，供前端 SSE
    连接（/roundtable/stream/<id>）实时渲染。返回 JSON 而不是 redirect，因为这是给
    JS fetch 用的，不是普通表单提交——普通表单提交走 /roundtable/run 那条老路径。
    """
    agent_names = request.form.getlist("agents")
    question = request.form.get("question", "").strip()
    conversation_id = request.form.get("conversation_id", "").strip() or None

    if len(agent_names) < 2 or not question:
        return jsonify({"error": "至少选 2 位专家，并填写讨论的问题。"}), 400

    if not _roundtable_lock.acquire(blocking=False):
        return jsonify({"error": "已经有一场圆桌讨论正在进行中，请等它结束再提交。"}), 409

    if not conversation_id:
        conversation_id = roundtable.create_conversation(agent_names, question)

    stream_queue = _create_stream_queue(conversation_id)

    _set_current_run({
        "conversation_id": conversation_id,
        "question": question,
        "agents": agent_names,
        "started_at": datetime.now().strftime("%H:%M:%S"),
    })
    logger.info(f"UI：触发圆桌讨论（流式）-> conversation_id={conversation_id}, question={question!r}, agents={agent_names}")

    def _worker():
        try:
            roundtable.run_roundtable(agent_names, question, conversation_id=conversation_id, stream_queue=stream_queue)
        except roundtable.RoundtableError as exc:
            logger.warning(f"UI：圆桌讨论参数错误：{exc}")
            _set_last_error(str(exc))
            stream_queue.put({"type": "error", "message": str(exc)})
        except Exception:
            logger.exception("UI：圆桌讨论执行失败")
            _set_last_error("圆桌讨论执行失败，详情看 logs/super_brain.log")
            stream_queue.put({"type": "error", "message": "执行失败，详情看 logs/super_brain.log"})
        finally:
            _set_current_run(None)
            _roundtable_lock.release()

    threading.Thread(target=_worker, daemon=True).start()
    return jsonify({"conversation_id": conversation_id})


# 2026-09-04 真实事故：圆桌讨论中途，gunicorn 的 --timeout（Dockerfile 里配的 120 秒）
# 比这里原来单次 q.get(timeout=180) 的等待时间还短——LLM 思考阶段/web_search 往返这类
# 合理的静默间隔一旦超过 120 秒没有任何字节发给 gunicorn，gunicorn 自己的看门狗会直接
# 把整个 worker 杀掉（SIGABRT -> SystemExit），跟这里"等够 180 秒再优雅报超时"的设计完全
# 来不及触发——这个服务只有 1 个 worker（Dockerfile 的 -w 1），worker 被杀等于整个服务
# 中断重启，正在进行的圆桌讨论内容当场从 UI 上消失。
# 修法：短轮询 + 保活字节——每隔 KEEPALIVE_INTERVAL_SECONDS 秒轮询一次队列，没有真消息
# 就发一行 SSE 注释（EventSource 客户端会忽略 ":" 开头的行，但字节本身已经发给了 gunicorn，
# 足以让它认为这个 worker 还活着），累计静默时间到 OVERALL_IDLE_LIMIT_SECONDS 才真的放弃——
# 保留原来"防止某个环节卡死导致连接永远挂着"的设计意图，只是不再靠一次性长阻塞实现。
KEEPALIVE_INTERVAL_SECONDS = 20
OVERALL_IDLE_LIMIT_SECONDS = 180


@app.route("/roundtable/stream/<conversation_id>")
def roundtable_stream(conversation_id):
    """SSE 端点——前端建立连接后持续收到 Round 1/2/3 的实时 chunk，直到收到
    run_done/error 类型的消息为止。累计静默 OVERALL_IDLE_LIMIT_SECONDS 秒收不到任何
    真消息就放弃，防止某个环节卡死导致连接永远挂着不释放；静默期间按
    KEEPALIVE_INTERVAL_SECONDS 秒的间隔发保活字节，避免被 gunicorn 的 --timeout 误杀
    （见上面的事故记录）。
    """
    def generate():
        q = _get_stream_queue(conversation_id)
        if q is None:
            yield f"data: {json.dumps({'type': 'error', 'message': '没有找到这场讨论的流，可能已经结束或从没开始过'}, ensure_ascii=False)}\n\n"
            return
        idle_elapsed = 0
        try:
            while True:
                try:
                    msg = q.get(timeout=KEEPALIVE_INTERVAL_SECONDS)
                except queue.Empty:
                    idle_elapsed += KEEPALIVE_INTERVAL_SECONDS
                    if idle_elapsed >= OVERALL_IDLE_LIMIT_SECONDS:
                        yield f"data: {json.dumps({'type': 'error', 'message': '等待超时'}, ensure_ascii=False)}\n\n"
                        break
                    yield ": keepalive\n\n"
                    continue
                idle_elapsed = 0
                yield f"data: {json.dumps(msg, ensure_ascii=False)}\n\n"
                if msg.get("type") in ("run_done", "error"):
                    break
        finally:
            _remove_stream_queue(conversation_id)

    return Response(
        generate(), mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/minutes/draft", methods=["POST"])
def minutes_draft():
    """圆桌讨论出会议纪要之后，直接调助手 agent（ops-assistant）把它写成头条 + 公众号草稿。
    跟圆桌讨论一样：后台线程执行、锁防重复点击、状态轮询，避免重复付费调用。
    """
    minutes_path = request.form.get("minutes_path", "").strip()
    user_instruction = request.form.get("user_instruction", "").strip() or None
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
    logger.info(
        f"UI：触发助手 agent 生成草稿（后台线程）-> minutes_path={minutes_path!r}, "
        f"user_instruction={user_instruction!r}"
    )

    def _worker():
        try:
            api_key = llm_client.load_deepseek_api_key()
            result = executors.execute_ops_assistant_from_minutes(minutes_path, api_key, user_instruction=user_instruction)
            persist_draft_log(minutes_path, result)
        except (FileNotFoundError, llm_client.DeepSeekConfigError) as exc:
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


@app.route("/conversations/<conversation_id>/delete", methods=["POST"])
def conversation_delete(conversation_id):
    """删除一个会话——不可恢复，前端已经有确认弹窗兜底，这里不再二次确认。"""
    if _get_current_run() and _get_current_run().get("conversation_id") == conversation_id:
        session["roundtable_error"] = "这个会话正在进行讨论，不能删除，请等它结束。"
        return redirect(url_for("index", conversation=conversation_id))

    ok = roundtable.delete_conversation(conversation_id)
    if not ok:
        logger.warning(f"UI：尝试删除不存在的会话：{conversation_id}")
    else:
        logger.info(f"UI：会话已删除：{conversation_id}")
    return redirect(url_for("index"))


@app.route("/roundtable/mention", methods=["POST"])
def roundtable_mention():
    """CEO 在圆桌讨论里 @ 某个专家单独提问——同步调用（比一整场圆桌快得多，不需要走
    流式/后台线程那一套），回复直接追加进这场会话的记录。跟"私聊"是两个不同机制，
    这里刻意不隔离，问答会留在会话里供后续参考。
    """
    conversation_id = request.form.get("conversation_id", "").strip()
    agent_name = request.form.get("agent", "").strip()
    message = request.form.get("message", "").strip()
    if not conversation_id or not agent_name or not message:
        session["roundtable_error"] = "@ 提问需要选定专家、填写问题。"
        return redirect(url_for("index", conversation=conversation_id or None))

    try:
        roundtable.ask_agent_mention(conversation_id, agent_name, message)
    except roundtable.RoundtableError as exc:
        logger.warning(f"UI：@ 提问参数错误：{exc}")
        session["roundtable_error"] = str(exc)
    except Exception:
        logger.exception("UI：@ 提问调用失败")
        session["roundtable_error"] = "@ 提问失败，详情看 logs/super_brain.log"
    return redirect(url_for("index", conversation=conversation_id))


@app.route("/tasks/<task_id>/status", methods=["POST"])
def task_status_update(task_id):
    """CEO 对 Round 3 收敛出的任务条目做确认/否掉/手动标记完成。

    先预览、CEO 点头之后才花这次调用——确认（confirmed）会真正触发对应 agent 生成产物，
    不是 Round 3 阶段就抢先生成好。生成成功后状态会再往前推进到 done、写入
    artifact_path；如果这类任务目前没有自动生成能力（比如 ship 的代码提交、邮件发送），
    状态停在 confirmed，人工完成后自己点"标记为已完成"（直接提交 status=done）。
    """
    status = request.form.get("status", "").strip()
    conversation_id = request.form.get("conversation_id", "").strip()
    tab = request.form.get("tab", "").strip() or None  # "todo" 表示从侧边栏待办 tab 触发，操作完要留在待办 tab

    def _back(**extra_args):
        return redirect(url_for("index", conversation=conversation_id or None, tab=tab, **extra_args))

    try:
        ok = tasks.update_task_status(task_id, status)
    except ValueError as exc:
        session["roundtable_error"] = str(exc)
        return _back()
    if not ok:
        session["roundtable_error"] = f"任务不存在：{task_id}"
        return _back()

    if status == "confirmed":
        task = next((t for t in tasks.load_tasks() if t["id"] == task_id), None)
        if task is not None:
            try:
                api_key = llm_client.load_deepseek_api_key()
            except llm_client.DeepSeekConfigError as exc:
                session["roundtable_error"] = f"{exc}，产物没有生成，任务停在 confirmed。"
            else:
                try:
                    artifact = executors.generate_task_artifact(task, api_key)
                    tasks.update_task_status(task_id, "done", artifact_path=artifact)
                    logger.info(f"UI：任务 {task_id} 产物已生成：{artifact}")
                except NotImplementedError as exc:
                    logger.info(f"UI：任务 {task_id} 暂不支持自动生成产物：{exc}")
                    session["roundtable_error"] = str(exc)
                except Exception:
                    logger.exception(f"UI：任务 {task_id} 生成产物失败")
                    session["roundtable_error"] = "生成产物失败，详情看 logs/super_brain.log，任务停在 confirmed。"

    return _back()


@app.route("/tasks/<task_id>/feedback", methods=["POST"])
def task_feedback(task_id):
    """CEO 对已生成产物的质量反馈——追加进对应 agent 的 lessons.md，喂给机制 2（定期复盘）
    用来判断要不要调整 private.md。这是纯记录动作，不会重新生成产物、不会改任务状态。
    """
    feedback = request.form.get("feedback", "").strip()
    conversation_id = request.form.get("conversation_id", "").strip()
    tab = request.form.get("tab", "").strip() or None

    if not feedback:
        session["roundtable_error"] = "反馈内容不能为空。"
        return redirect(url_for("index", conversation=conversation_id or None, tab=tab))

    task = next((t for t in tasks.load_tasks() if t["id"] == task_id), None)
    if task is None:
        session["roundtable_error"] = f"任务不存在：{task_id}"
        return redirect(url_for("index", conversation=conversation_id or None, tab=tab))

    agent_name = task.get("assignee_agent")
    if not agent_name:
        session["roundtable_error"] = "这条任务没有对应的执行 agent，没法记录反馈。"
        return redirect(url_for("index", conversation=conversation_id or None, tab=tab))

    agent_registry.log_artifact_feedback(agent_name, task.get("description", ""), task.get("artifact_path"), feedback)
    logger.info(f"UI：任务 {task_id} 的产物质量反馈已记录到 {agent_name} 的 lessons.md")
    return redirect(url_for("index", conversation=conversation_id or None, tab=tab))


def render_private_chat(conversation_id: str | None = None, **extra):
    conversations = private_chat.load_all_conversations()
    active = None
    active_id = conversation_id
    if active_id:
        active = next((c for c in conversations if c["id"] == active_id), None)
    if active is None:
        active_id = None

    error = session.pop("private_chat_error", None) or _pop_private_chat_error()

    return render_template(
        "private_chat.html",
        conversations=conversations,
        active_conversation=active,
        active_conversation_id=active_id,
        current_run=_get_current_private_chat_run(),
        private_chat_error=error,
        **extra,
    )


@app.route("/private-chat/start", methods=["POST"])
def private_chat_start():
    """从一场圆桌讨论的某一轮里，针对某个专家开一个新的隔离私聊——种子上下文只带这位
    专家自己在那一轮的发言，不夹带其他专家的意见。"""
    source_conversation_id = request.form.get("conversation_id", "").strip()
    agent_name = request.form.get("agent", "").strip()
    turn_index = request.form.get("turn_index", "").strip()
    message = request.form.get("message", "").strip()

    if not source_conversation_id or not agent_name or not message:
        session["roundtable_error"] = "开始私聊需要来源会话、专家、第一条消息。"
        return redirect(url_for("index", conversation=source_conversation_id or None))

    source = roundtable.load_conversation(source_conversation_id)
    if source is None:
        session["roundtable_error"] = f"来源会话不存在：{source_conversation_id}"
        return redirect(url_for("index"))
    try:
        idx = int(turn_index)
        source_turn = source["turns"][idx]
    except (ValueError, IndexError):
        session["roundtable_error"] = "找不到对应的圆桌讨论轮次，没法开始私聊。"
        return redirect(url_for("index", conversation=source_conversation_id))

    try:
        conversation_id = private_chat.create_conversation(agent_name, source_conversation_id, source_turn, message)
    except private_chat.PrivateChatError as exc:
        session["roundtable_error"] = str(exc)
        return redirect(url_for("index", conversation=source_conversation_id))

    if not _private_chat_lock.acquire(blocking=False):
        session["roundtable_error"] = "已经有一次私聊生成正在进行中，请等它结束再提交。"
        return redirect(url_for("index", conversation=source_conversation_id))

    _set_current_private_chat_run({"conversation_id": conversation_id, "started_at": datetime.now().strftime("%H:%M:%S")})

    def _worker():
        try:
            private_chat.send_message(conversation_id, message)
        except private_chat.PrivateChatError as exc:
            _set_private_chat_error(str(exc))
        except Exception:
            logger.exception("UI：私聊生成失败")
            _set_private_chat_error("生成失败，详情看 logs/super_brain.log")
        finally:
            _set_current_private_chat_run(None)
            _private_chat_lock.release()

    threading.Thread(target=_worker, daemon=True).start()
    return redirect(url_for("private_chat_page", conversation=conversation_id))


@app.route("/private-chat/<conversation_id>")
def private_chat_page(conversation_id):
    return render_private_chat(conversation_id=conversation_id)


@app.route("/private-chat")
def private_chat_list():
    return render_private_chat()


@app.route("/private-chat/send", methods=["POST"])
def private_chat_send():
    message = request.form.get("message", "").strip()
    conversation_id = request.form.get("conversation_id", "").strip()
    if not message or not conversation_id:
        session["private_chat_error"] = "请填写要问的问题。"
        return redirect(url_for("private_chat_page", conversation_id=conversation_id))

    if not _private_chat_lock.acquire(blocking=False):
        session["private_chat_error"] = "已经有一次生成正在进行中，请等它结束再提交。"
        return redirect(url_for("private_chat_page", conversation_id=conversation_id))

    _set_current_private_chat_run({"conversation_id": conversation_id, "started_at": datetime.now().strftime("%H:%M:%S")})

    def _worker():
        try:
            private_chat.send_message(conversation_id, message)
        except private_chat.PrivateChatError as exc:
            _set_private_chat_error(str(exc))
        except Exception:
            logger.exception("UI：私聊生成失败")
            _set_private_chat_error("生成失败，详情看 logs/super_brain.log")
        finally:
            _set_current_private_chat_run(None)
            _private_chat_lock.release()

    threading.Thread(target=_worker, daemon=True).start()
    return redirect(url_for("private_chat_page", conversation_id=conversation_id))


@app.route("/private-chat/status")
def private_chat_status():
    return jsonify({"running": _get_current_private_chat_run() is not None})


@app.route("/private-chat/<conversation_id>/delete", methods=["POST"])
def private_chat_delete(conversation_id):
    private_chat.delete_conversation(conversation_id)
    return redirect(url_for("private_chat_list"))


# 2026-08-18 新增：video-prompt 是"正式角色"——独立的多轮对话，走真实 DeepSeek 程序化
# 调用（不依赖 Claude Code 在场），admin 页面新增一块功能区，不占主界面。同样的教训、
# 同样的模式：后台线程执行 + 锁防重复点击 + 轮询状态，不会重蹈"同步阻塞卡死服务器"的错。
_video_prompt_lock = threading.Lock()
_current_video_prompt_run: dict | None = None  # {"conversation_id", "started_at"}
_last_video_prompt_error: str | None = None


def _set_current_video_prompt_run(value: dict | None) -> None:
    global _current_video_prompt_run
    with _state_lock:
        _current_video_prompt_run = value


def _get_current_video_prompt_run() -> dict | None:
    with _state_lock:
        return _current_video_prompt_run


def _set_video_prompt_error(message: str | None) -> None:
    global _last_video_prompt_error
    with _state_lock:
        _last_video_prompt_error = message


def _pop_video_prompt_error() -> str | None:
    global _last_video_prompt_error
    with _state_lock:
        message, _last_video_prompt_error = _last_video_prompt_error, None
        return message


# 专家私聊——跟 video-prompt 完全同一套模式（后台线程 + 锁防重复点击 + 轮询状态），
# 私聊本身走的是 private_chat.py 的隔离会话存储，不复用 video_prompt 的存储/状态。
_private_chat_lock = threading.Lock()
_current_private_chat_run: dict | None = None
_last_private_chat_error: str | None = None


def _set_current_private_chat_run(value: dict | None) -> None:
    global _current_private_chat_run
    with _state_lock:
        _current_private_chat_run = value


def _get_current_private_chat_run() -> dict | None:
    with _state_lock:
        return _current_private_chat_run


def _set_private_chat_error(message: str | None) -> None:
    global _last_private_chat_error
    with _state_lock:
        _last_private_chat_error = message


def _pop_private_chat_error() -> str | None:
    global _last_private_chat_error
    with _state_lock:
        message, _last_private_chat_error = _last_private_chat_error, None
        return message


# 每日 18 点批量汇总——后台线程每分钟检查一次，一旦当天首次过了 18 点就跑一次
# digest.run_daily_batch()，一天只跑一次（_last_daily_batch_date 记住今天跑过没）。
# 老实说清楚：这是真实会花 DeepSeek 额度的调用（如果当天有 opc 笔记的话）——服务器一旦
# 启动，这个线程就是活的，18 点之后自动触发，不需要也不会再问一遍。
_last_daily_batch_date: str | None = None


def _run_daily_batch_once(trigger_label: str) -> None:
    global _last_daily_batch_date
    try:
        api_key = llm_client.load_deepseek_api_key()
    except llm_client.DeepSeekConfigError as exc:
        logger.warning(f"每日批处理（{trigger_label}）：{exc}，跳过")
        return
    try:
        out_path = digest.run_daily_batch(api_key=api_key)
        logger.info(f"每日批处理（{trigger_label}）完成：{out_path}")
    except Exception:
        logger.exception(f"每日批处理（{trigger_label}）失败")
    finally:
        _last_daily_batch_date = datetime.now().strftime("%Y-%m-%d")


def _daily_batch_scheduler_loop() -> None:
    while True:
        time.sleep(60)
        now = datetime.now()
        today_str = now.strftime("%Y-%m-%d")
        if now.hour >= 18 and _last_daily_batch_date != today_str:
            logger.info("每日批处理：过了 18 点且今天还没跑过，自动触发")
            _run_daily_batch_once("18点自动触发")


def render_video_prompt(conversation_id: str | None = None, **extra):
    conversations = video_prompt.load_all_conversations()
    active = None
    active_id = conversation_id
    if active_id:
        active = next((c for c in conversations if c["id"] == active_id), None)
    elif conversations:
        active = conversations[0]
        active_id = active["id"]
    if active is None:
        active_id = None

    error = session.pop("video_prompt_error", None) or _pop_video_prompt_error()

    return render_template(
        "video_prompt.html",
        conversations=conversations,
        active_conversation=active,
        active_conversation_id=active_id,
        current_run=_get_current_video_prompt_run(),
        video_prompt_error=error,
        **extra,
    )


@app.route("/video-prompt")
def video_prompt_page():
    conversation_id = request.args.get("conversation") or None
    return render_video_prompt(conversation_id=conversation_id)


@app.route("/video-prompt/send", methods=["POST"])
def video_prompt_send():
    message = request.form.get("message", "").strip()
    conversation_id = request.form.get("conversation_id", "").strip() or None

    if not message:
        session["video_prompt_error"] = "请填写要生成/修改的描述。"
        return redirect(url_for("video_prompt_page", conversation=conversation_id))

    if not _video_prompt_lock.acquire(blocking=False):
        logger.warning(f"UI：video-prompt 请求被拒绝——已有一次生成正在进行中")
        session["video_prompt_error"] = "已经有一次生成正在进行中，请等它结束再提交。"
        return redirect(url_for("video_prompt_page", conversation=conversation_id))

    if not conversation_id:
        conversation_id = video_prompt.create_conversation(message)

    _set_current_video_prompt_run({
        "conversation_id": conversation_id,
        "started_at": datetime.now().strftime("%H:%M:%S"),
    })
    logger.info(f"UI：触发 video-prompt 生成（后台线程）-> conversation_id={conversation_id}")

    def _worker():
        try:
            video_prompt.send_message(conversation_id, message)
        except video_prompt.VideoPromptError as exc:
            logger.warning(f"UI：video-prompt 参数错误：{exc}")
            _set_video_prompt_error(str(exc))
        except Exception:
            logger.exception("UI：video-prompt 生成失败")
            _set_video_prompt_error("生成失败，详情看 logs/super_brain.log")
        finally:
            _set_current_video_prompt_run(None)
            _video_prompt_lock.release()

    threading.Thread(target=_worker, daemon=True).start()
    return redirect(url_for("video_prompt_page", conversation=conversation_id))


@app.route("/video-prompt/status")
def video_prompt_status():
    return jsonify({"running": _get_current_video_prompt_run() is not None})


@app.route("/video-prompt/<conversation_id>/delete", methods=["POST"])
def video_prompt_delete(conversation_id):
    video_prompt.delete_conversation(conversation_id)
    return redirect(url_for("video_prompt_page"))


@app.route("/admin")
def admin():
    return render_admin()


@app.route("/today")
def today_digest():
    """主动触发层——零成本聚合视图，不调用任何 LLM，随便刷新都不花钱。
    汇总今天还没处理的东西：待确认任务、待处理 inbox 留言、今天开过的圆桌讨论。
    """
    d = digest.build_today_digest()
    return render_template("today.html", digest=d)


@app.route("/admin/daily-batch/run", methods=["POST"])
def daily_batch_run_now():
    """手动立即跑一次每日批处理——测试用，不用等到真的 18 点。会真的花 DeepSeek 额度
    （如果今天的 opc 笔记存在的话），点这个按钮就是在做那次真实调用。
    """
    _run_daily_batch_once("手动触发")
    return redirect(url_for("admin"))


@app.route("/admin/review/generate", methods=["POST"])
def review_generate():
    """机制 2·定期复盘——真实调用 DeepSeek，读这个 agent 的 lessons.md + 现有 private.md，
    生成一份调整建议，落盘成独立文件，不会碰 private.md 本身。"""
    agent_name = request.form.get("agent", "").strip()
    try:
        api_key = llm_client.load_deepseek_api_key()
    except llm_client.DeepSeekConfigError as exc:
        session["roundtable_error"] = str(exc)
        return redirect(url_for("admin"))
    try:
        review.generate_review_suggestion(agent_name, api_key)
    except review.ReviewError as exc:
        session["roundtable_error"] = str(exc)
    except Exception:
        logger.exception(f"UI：{agent_name} 定期复盘生成失败")
        session["roundtable_error"] = "定期复盘生成失败，详情看 logs/super_brain.log"
    return redirect(url_for("admin"))


@app.route("/admin/review/apply", methods=["POST"])
def review_apply():
    """CEO 点"采纳"——把这条复盘建议追加进 private.md 末尾（只追加，不覆写），
    是这条建议唯一会真正改变 agent 行为的动作，必须人工点一下才会发生。"""
    agent_name = request.form.get("agent", "").strip()
    review_path = request.form.get("path", "").strip()
    try:
        review.apply_review(agent_name, review_path)
    except review.ReviewError as exc:
        session["roundtable_error"] = str(exc)
    return redirect(url_for("admin"))


@app.route("/admin/rag/rebuild", methods=["POST"])
def rag_rebuild_all():
    """给所有已注册 agent 建/重建 RAG 索引——零成本（本地嵌入模型，不调用任何付费 API），
    但第一次调用会触发模型下载（~95MB，从 Hugging Face），需要服务器有出网能力；如果连不上，
    这里会捕获异常按 agent 逐个报告，不会因为一个失败就中断其余 agent 的建索引。
    这是新部署到一台机器后必须手动点一次的步骤——RAG 索引是从 private.md 派生出来的构建
    产物，特意没有进 git（见 .gitignore），git pull 不会把它带过来，只能在目标机器上现建。
    """
    registry = agent_registry.load_agent_registry()
    results = {}
    for name, entry in registry.items():
        private_path = AGENTS_DIR / name / "private.md"
        if not private_path.exists():
            continue
        try:
            n = rag.build_index(name, force=True)
            results[name] = f"{n} 条规则"
        except Exception as exc:
            logger.exception(f"UI：{name} 的 RAG 索引重建失败")
            results[name] = f"失败：{exc}"
    session["rag_rebuild_results"] = results
    return redirect(url_for("admin"))


@app.route("/admin/rag/analytics")
def rag_analytics():
    """RAG 系统分析看板——总览页：每个已建索引的 agent 的规则条数、历史检索次数，
    零成本（全是读本地文件+算统计，不调用任何模型）。
    """
    agents = rag.list_indexed_agents()
    overview = []
    for name in agents:
        stats = rag.get_index_stats(name)
        log = rag.get_retrieval_log(name)
        overview.append({
            "name": name,
            "label": i18n.agent_label(name),
            "chunk_count": stats.get("chunk_count", 0),
            "avg_length": stats.get("avg_length", 0),
            "query_count": len(log),
        })
    return render_template("rag_analytics.html", overview=overview)


@app.route("/admin/rag/analytics/<agent_name>")
def rag_analytics_detail(agent_name):
    """单个 agent 的检索质量细节：按分类的规则密度分布、每条规则被命中次数、
    历史检索相似度分数分布。log=False 的这次读取本身不计入统计（见 rag.search 的
    log 参数说明），避免"打开分析页"这个动作污染了它自己要分析的数据。
    """
    stats = rag.get_index_stats(agent_name)
    hit_counts = rag.get_chunk_hit_counts(agent_name)
    scores = rag.get_similarity_scores(agent_name)
    log = rag.get_retrieval_log(agent_name)

    # 相似度分布分箱（0.0-1.0，每 0.1 一箱），Chart.js 直接吃这个结构画柱状图
    bins = [0] * 10
    for s in scores:
        idx = min(int(s * 10), 9)
        bins[idx] += 1

    chunk_hits_sorted = sorted(hit_counts.items(), key=lambda kv: -kv[1])

    return render_template(
        "rag_analytics_detail.html",
        agent_name=agent_name,
        agent_label_text=i18n.agent_label(agent_name),
        stats=stats,
        chunk_hits_sorted=chunk_hits_sorted,
        similarity_bins=bins,
        query_count=len(log),
        recent_queries=list(reversed(log))[:20],
    )


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


def _normalize_path_input(value: str) -> str:
    """去掉路径输入两端多余的引号——真实发生过的问题：从 `ls` 输出（GNU coreutils 对含
    反斜杠等特殊字符的文件名默认会加单引号）或 Windows"复制为路径"里粘贴过来的值，会带上
    字面意义上的引号字符，直接存进 config.json 会变成路径的一部分，导致目录建错地方。
    只剥一层两端对称的引号（'...' 或 "..."），不处理路径中间的引号。
    """
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1].strip()
    return value


def _persistence_warning_for_path(value: str) -> str | None:
    """docker-compose.yml 只把整个仓库目录挂载成容器里的 SUPER_BRAIN（本机是 G:\\code\\
    super_brain，服务器是 /app）——只有这条路径下的内容会落在宿主机磁盘上，CI/CD 每次
    `docker compose up -d --build` 都会重建容器，容器自己文件系统里其它地方新建的目录
    会被整个清空。这是真实发生过的事故：DEPLOYMENT.md 曾经给的示例路径是宿主机路径
    `/opt/super_brain/...`，但这个值是在容器里的 Python 进程读的，容器里根本没有
    `/opt/super_brain` 这个路径，实际建到了容器临时文件系统里，每次部署都被清空。

    这里只做温和提醒，不阻止保存——不排除少数场景下用户确实配了别的持久化挂载点。
    """
    try:
        resolved = Path(value).resolve()
        root = SUPER_BRAIN.resolve()
    except OSError:
        return None
    if resolved == root or root in resolved.parents:
        return None
    return (
        f"已保存，但这个路径（{value}）不在 {root} 之下——docker-compose.yml 只把这个目录"
        "挂载到了宿主机磁盘上，其它位置的内容会在下次部署（docker compose up -d --build）"
        "时被清空。如果这是在服务器上配置，建议改成 /app 开头的路径（比如 /app/meeting-minutes）。"
    )


@app.route("/config/set", methods=["POST"])
def config_set():
    value = _normalize_path_input(request.form.get("meeting_minutes_dir", ""))
    if not value:
        session["roundtable_error"] = "会议纪要目录不能为空，没有保存。"
        return redirect(request.referrer or url_for("index"))

    config, load_error = _load_config_for_update()
    if load_error is not None:
        logger.error(f"UI：config.json 读取失败，拒绝写入以免覆盖已有配置：{load_error}")
        session["roundtable_error"] = (
            f"config.json 读取失败（{load_error}），为了不覆盖已有的 API_KEY 等配置，"
            "这次的目录设置没有保存——请先手动检查/修复服务器上的 config.json。"
        )
        return redirect(request.referrer or url_for("index"))

    try:
        Path(value).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.error(f"UI：会议纪要目录创建失败：{value!r}，{exc}")
        session["roundtable_error"] = f"目录创建失败（{exc}），没有保存这次设置，检查一下路径是否合法。"
        return redirect(request.referrer or url_for("index"))

    config["MEETING_MINUTES_DIR"] = value
    _write_config_with_backup(config)
    logger.info(f"UI：MEETING_MINUTES_DIR 已设置为 {value}（首次配置，之后不再需要重复问）")

    warning = _persistence_warning_for_path(value)
    if warning:
        logger.warning(f"UI：{warning}")
        session["roundtable_error"] = warning
    else:
        session["config_success"] = f"会议纪要目录已保存：{value}"
    return redirect(request.referrer or url_for("index"))


@app.route("/config/set-toutiao-drafts-dir", methods=["POST"])
def config_set_toutiao_drafts_dir():
    """头条草稿存放目录——不像会议纪要目录那样是硬性必填项，没设置就用历史默认值兜底，
    设置了就迁移过去（publishers.get_toutiao_drafts_dir() 读的是这同一个 key）。
    """
    value = _normalize_path_input(request.form.get("toutiao_drafts_dir", ""))
    if not value:
        session["roundtable_error"] = "头条草稿目录不能为空，没有保存。"
        return redirect(request.referrer or url_for("admin"))

    config, load_error = _load_config_for_update()
    if load_error is not None:
        logger.error(f"UI：config.json 读取失败，拒绝写入以免覆盖已有配置：{load_error}")
        session["roundtable_error"] = (
            f"config.json 读取失败（{load_error}），为了不覆盖已有的 API_KEY 等配置，"
            "这次的目录设置没有保存——请先手动检查/修复服务器上的 config.json。"
        )
        return redirect(request.referrer or url_for("admin"))

    try:
        Path(value).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.error(f"UI：头条草稿目录创建失败：{value!r}，{exc}")
        session["roundtable_error"] = f"目录创建失败（{exc}），没有保存这次设置，检查一下路径是否合法。"
        return redirect(request.referrer or url_for("admin"))

    config["TOUTIAO_DRAFTS_DIR"] = value
    _write_config_with_backup(config)
    logger.info(f"UI：TOUTIAO_DRAFTS_DIR 已设置为 {value}")

    warning = _persistence_warning_for_path(value)
    if warning:
        logger.warning(f"UI：{warning}")
        session["roundtable_error"] = warning
    else:
        session["config_success"] = f"头条草稿目录已保存：{value}"
    return redirect(request.referrer or url_for("admin"))


DRAFT_PREVIEW_PLAINTEXT_EXTS = {".md", ".txt"}
DRAFT_PREVIEW_HTML_EXTS = {".html"}


@app.route("/draft/preview")
def draft_preview():
    """快捷预览头条/公众号草稿、正式会议纪要——安全边界很关键：path 参数完全来自用户输入
    （哪怕是本机单用户工具），必须校验解析后的真实路径确实落在"允许预览的目录"内，不然
    就是一个任意文件读取漏洞。三类内容各有自己的目录（publishers.get_toutiao_drafts_dir()
    / get_wechat_drafts_dir() / ui_app.get_meeting_minutes_dir()，互不依赖），这里全都认，
    落在其中任意一个目录内就放行；会议纪要目录没配置（get_meeting_minutes_dir() 返回
    None）时不参与判断，不代表放行更宽松。

    只认**当前**配置的会议纪要目录，不兼容历史上配错、后来改掉的旧路径（畸形路径见
    get_meeting_minutes_dir() 的说明）——旧会话记录里可能存着当时错误配置下生成的
    minutes_path，这类文件不会因为这里放宽而变得可预览，这是刻意的，不是遗漏。

    .md/.txt（头条草稿、会议纪要）按纯文本展示，原样转义，不解释里面的任何标记；
    .html（公众号草稿）这份内容本身是 DeepSeek 按受限标签+内联样式生成的（见
    adapt_draft_to_wechat 的 prompt 约束：只允许 h3/p/blockquote/strong/code 几个标签），
    不是任意用户上传的 HTML，直接渲染成真实排版效果比展示一堆转义后的标签文字更有用——
    单用户内部工具，这个信任边界是合理的。
    """
    raw_path = request.args.get("path", "")
    if not raw_path:
        return "缺少 path 参数", 400

    target = Path(raw_path).resolve()
    allowed_roots = [publishers.get_toutiao_drafts_dir().resolve(), publishers.get_wechat_drafts_dir().resolve()]
    meeting_minutes_dir = get_meeting_minutes_dir()
    if meeting_minutes_dir is not None:
        allowed_roots.append(meeting_minutes_dir.resolve())
    allowed_roots.append(autopublish.ARTIFACTS_DIR.resolve())  # 自动化发布流水线的本地定稿/清单
    if not any(target == root or root in target.parents for root in allowed_roots):
        logger.warning(f"UI：草稿预览请求被拒绝——路径不在允许的目录内：{raw_path!r}")
        return "只能预览头条/公众号草稿、当前配置的会议纪要目录内的文件", 403

    suffix = target.suffix.lower()
    if suffix not in DRAFT_PREVIEW_PLAINTEXT_EXTS and suffix not in DRAFT_PREVIEW_HTML_EXTS:
        return "只能预览 .md / .txt / .html 文件", 403
    if not target.is_file():
        return "文件不存在（可能已被移动或删除）", 404

    content = target.read_text(encoding="utf-8", errors="replace")
    if suffix in DRAFT_PREVIEW_HTML_EXTS:
        return render_template("draft_preview_wechat.html", path=str(target), content=content)
    return render_template("draft_preview.html", path=str(target), content=content)


# ==================== 自动化媒体发布流水线（/admin/autopublish） ====================
# 页面 + 路由 + 调度线程都在这一个区块。调度逻辑本身在 autopublish.py，这里只负责
# "Flask 怎么把后台页面和按钮接到 autopublish.py 的函数上"，不重复实现业务判断。
# 防重入锁：调度 tick 和"立即执行"按钮共用同一把锁，避免两个动作同时写发布单。
_autopublish_lock = threading.Lock()


def _autopublish_scheduler_loop() -> None:
    while True:
        time.sleep(60)
        try:
            if _autopublish_lock.acquire(blocking=False):
                try:
                    autopublish.scheduler_tick()
                finally:
                    _autopublish_lock.release()
        except Exception:
            logger.exception("自动化发布调度 tick 异常（已捕获，不影响下一分钟）")


def _order_view(order: dict) -> dict:
    return {
        "order": order,
        "summary": autopublish.order_summary(order),
        "json": json.dumps(order, ensure_ascii=False, indent=2),
    }


@app.route("/admin/autopublish")
def autopublish_page():
    cfg = autopublish.load_autopublish_config()
    view_orders = [_order_view(o) for o in autopublish.load_all_orders()]
    return render_template(
        "autopublish.html",
        cfg=cfg,
        action_labels=autopublish.ACTION_LABELS,
        channels_meta=autopublish.CHANNELS,
        channel_conf=cfg.get("channels", {}),
        view_orders=view_orders,
        master_enabled=bool(cfg.get("master_enabled")),
        sources_dir=str(autopublish.SOURCES_DIR),
        queue_dir=str(autopublish.QUEUE_DIR),
        artifacts_dir=str(autopublish.ARTIFACTS_DIR),
        msg=session.pop("autopublish_msg", None),
        error=session.pop("autopublish_error", None),
    )


@app.route("/admin/autopublish/save", methods=["POST"])
def autopublish_save_config():
    """保存主开关 + 调度事件（时间/启用）+ 各渠道 mode。只改 config.json 的 AUTOPUBLISH
    键，其它字段原样保留（复用 _write_config_with_backup 的备份语义）。"""
    cfg = autopublish.load_autopublish_config()
    cfg["master_enabled"] = bool(request.form.get("master_enabled"))
    new_events = []
    for ev in cfg.get("events", []):
        eid = ev.get("id")
        raw_time = (request.form.get(f"event_{eid}_time") or "").strip()
        if not re.fullmatch(r"\d{1,2}:\d{2}", raw_time):
            session["autopublish_error"] = f"事件 {eid} 的时间格式不对：{raw_time!r}（要 HH:MM）"
            return redirect(url_for("autopublish_page"))
        action = ev.get("action")
        new_events.append({
            "id": eid,
            "label": ev.get("label") or autopublish.ACTION_LABELS.get(action, action),
            "time": raw_time,
            "action": action,
            "enabled": bool(request.form.get(f"event_{eid}_enabled")),
        })
    cfg["events"] = new_events
    for ch in autopublish.DEFAULT_CHANNEL_CONF:
        mode = (request.form.get(f"ch_{ch}_mode") or "manual").strip()
        if mode not in ("manual", "mock", "api"):
            mode = "manual"
        cfg["channels"].setdefault(ch, {})["mode"] = mode
    full = autopublish.load_full_config()
    full[autopublish.CONFIG_KEY] = cfg
    _write_config_with_backup(full)  # 备份 + 写回，不动 config.json 其它字段
    logger.info(f"UI：AUTOPUBLISH 配置已保存（master={cfg['master_enabled']}）")
    session["autopublish_msg"] = "调度与渠道配置已保存。"
    return redirect(url_for("autopublish_page"))


@app.route("/admin/autopublish/run", methods=["POST"])
def autopublish_run_now():
    """立即执行某个动作（对应调度事件的同款函数）——测试用，不用等真的到点。"""
    action = (request.form.get("action") or "").strip()
    if action not in autopublish.ACTION_LABELS:
        session["autopublish_error"] = f"未知动作：{action!r}"
        return redirect(url_for("autopublish_page"))
    if not _autopublish_lock.acquire(blocking=False):
        session["autopublish_error"] = "已有调度/按钮动作正在执行，请稍等再试。"
        return redirect(url_for("autopublish_page"))
    try:
        result = autopublish.run_action_once(action)
    except Exception as exc:
        logger.exception(f"UI：立即执行 {action} 失败")
        session["autopublish_error"] = f"执行 {action} 失败：{exc}"
    else:
        summary = json.dumps(result, ensure_ascii=False)
        session["autopublish_msg"] = f"「{autopublish.ACTION_LABELS[action]}」执行完成：{summary[:500]}"
    finally:
        _autopublish_lock.release()
    return redirect(url_for("autopublish_page"))


@app.route("/admin/autopublish/order/new", methods=["POST"])
def autopublish_order_new():
    title = (request.form.get("title") or "").strip()
    text = (request.form.get("text") or "").strip()
    publish_at = (request.form.get("publish_at") or "").strip() or None
    if publish_at and not re.fullmatch(r"\d{1,2}:\d{2}", publish_at):
        session["autopublish_error"] = f"计划发布时间格式不对：{publish_at!r}（要 HH:MM 或留空）"
        return redirect(url_for("autopublish_page"))
    if not title:
        session["autopublish_error"] = "标题不能为空。"
        return redirect(url_for("autopublish_page"))
    if not text:
        session["autopublish_error"] = "正文不能为空（骨架版发布单直接贴内容；素材自动导入以后接）。"
        return redirect(url_for("autopublish_page"))
    channels = [c for c in request.form.getlist("channels") if c in autopublish.CHANNELS]
    if not channels:
        session["autopublish_error"] = "至少勾选一个发布渠道。"
        return redirect(url_for("autopublish_page"))
    order = autopublish.new_order(title, {"kind": "text", "text": text[:20000]}, channels, publish_at=publish_at)
    autopublish.save_order(order)
    logger.info(f"UI：新建发布单 {order['id']}")
    session["autopublish_msg"] = f"发布单已创建：{order['id']}。下一步对它执行「物料制作」。"
    return redirect(url_for("autopublish_page"))


def _load_order_or_error(order_id: str):
    order = autopublish.load_order(order_id)
    if order is None:
        session["autopublish_error"] = f"发布单不存在：{order_id}"
        return None
    return order


@app.route("/admin/autopublish/order/<order_id>/draft", methods=["POST"])
def autopublish_order_draft(order_id):
    if _load_order_or_error(order_id) is None:
        return redirect(url_for("autopublish_page"))
    try:
        result = autopublish.run_draft_pending([order_id])
        session["autopublish_msg"] = f"物料制作完成：produced={len(result['produced'])}，skipped={len(result['skipped'])}"
    except Exception as exc:
        logger.exception(f"UI：发布单 {order_id} 物料制作失败")
        session["autopublish_error"] = f"物料制作失败：{exc}"
    return redirect(url_for("autopublish_page"))


@app.route("/admin/autopublish/order/<order_id>/push-wechat", methods=["POST"])
def autopublish_order_push_wechat(order_id):
    """真实外部动作：把这份发布单推送到公众号草稿箱（不是发布）。需要 WECHAT_* 凭据。"""
    if _load_order_or_error(order_id) is None:
        return redirect(url_for("autopublish_page"))
    try:
        result = autopublish.run_draft_wechat_push(order_id)
        session["autopublish_msg"] = f"已推送到公众号草稿箱：draft_media_id={result['draft_media_id']}"
    except Exception as exc:
        logger.warning(f"UI：发布单 {order_id} 推送公众号草稿失败：{exc}")
        session["autopublish_error"] = f"推送公众号草稿失败：{exc}"
    return redirect(url_for("autopublish_page"))


@app.route("/admin/autopublish/order/<order_id>/approve", methods=["POST"])
def autopublish_order_approve(order_id):
    channel = (request.form.get("channel") or "").strip()
    if _load_order_or_error(order_id) is None:
        return redirect(url_for("autopublish_page"))
    try:
        autopublish.approve_channel(order_id, channel)
        session["autopublish_msg"] = f"已放行渠道 {channel}（gatekeeper 通过）。到点后 publisher 才会执行。"
    except ValueError as exc:
        session["autopublish_error"] = str(exc)
    return redirect(url_for("autopublish_page"))


@app.route("/admin/autopublish/order/<order_id>/reject", methods=["POST"])
def autopublish_order_reject(order_id):
    channel = (request.form.get("channel") or "").strip()
    reason = (request.form.get("reason") or "").strip()
    if _load_order_or_error(order_id) is None:
        return redirect(url_for("autopublish_page"))
    autopublish.reject_channel(order_id, channel, reason)
    session["autopublish_msg"] = f"渠道 {channel} 已打回。"
    return redirect(url_for("autopublish_page"))


@app.route("/admin/autopublish/order/<order_id>/publish", methods=["POST"])
def autopublish_order_publish(order_id):
    """对单个发布单立即执行发布（force：忽略该单的发布时间窗口）。三重闸门仍生效：
    全局主开关 + 渠道 mode + CEO 放行。"""
    order = _load_order_or_error(order_id)
    if order is None:
        return redirect(url_for("autopublish_page"))
    if not autopublish.master_enabled():
        session["autopublish_error"] = "全局主开关未打开，不会执行任何真实/模拟发布。先到本页顶部打开再试。"
        return redirect(url_for("autopublish_page"))
    if not _autopublish_lock.acquire(blocking=False):
        session["autopublish_error"] = "已有动作正在执行，请稍等再试。"
        return redirect(url_for("autopublish_page"))
    try:
        results = autopublish.dispatch_order(order, force=True)
        session["autopublish_msg"] = f"发布执行完成：{json.dumps(results, ensure_ascii=False)}"
    except Exception as exc:
        logger.exception(f"UI：发布单 {order_id} 发布执行失败")
        session["autopublish_error"] = f"发布执行失败：{exc}"
    finally:
        _autopublish_lock.release()
    return redirect(url_for("autopublish_page"))


@app.route("/admin/autopublish/order/<order_id>/cancel", methods=["POST"])
def autopublish_order_cancel(order_id):
    reason = (request.form.get("reason") or "").strip()
    if _load_order_or_error(order_id) is None:
        return redirect(url_for("autopublish_page"))
    autopublish.cancel_order(order_id, reason)
    session["autopublish_msg"] = "发布单已取消。"
    return redirect(url_for("autopublish_page"))


@app.route("/admin/autopublish/order/<order_id>/delete", methods=["POST"])
def autopublish_order_delete(order_id):
    autopublish.delete_order(order_id)
    session["autopublish_msg"] = "发布单已删除。"
    return redirect(url_for("autopublish_page"))


# ==================== 系统配置（/admin/config：网页填密钥/目录/邮件，不回显真实值） ====================
# 设计：config.json 里的配置绝大多数都能在这里网页维护，避免"贴 JSON / 贴 key"。
# 安全约定：已配置的值**绝不回显到页面**；输入框留空=保持原值不动；想清空某项就勾"置空"。
CONFIG_UI_GROUPS = [
    ("DeepSeek（模型）", [
        ("DEEPSEEK_API_KEY", "API Key（必填，几乎全部 AI 功能依赖）", True),
        ("Model", "模型名（留空=内置 deepseek-v4-pro）", False),
        ("BaseUrl", "接口地址（留空=内置 https://api.deepseek.com/v1）", False),
        ("MaxTokens", "最大 token（留空=内置 8000）", False),
        ("ModelStructured", "结构化任务模型（可选）：选题/事实提取/评分/格式改写专用；填非推理快模型（如 deepseek-chat）更省钱更稳", False),
    ]),
    ("联网搜索（可选）", [
        ("TAVILY_API_KEY", "Tavily API Key（空=自动降级，不联网）", True),
    ]),
    ("微信公众号（草稿/发布用则必填）", [
        ("WECHAT_APP_ID", "AppID", True),
        ("WECHAT_APP_KEY", "AppSecret", True),
        ("WECHAT_DEFAULT_COVER_URL", "默认封面图 URL", False),
    ]),
    ("RAG 检索（DashScope + DashVector，可选）", [
        ("DASHSCOPE_API_KEY", "DashScope API Key", True),
        ("DASHSCOPE_WORKSPACE_ID", "DashScope WorkspaceId", True),
        ("DASHVECTOR_API_KEY", "DashVector API Key", True),
        ("DASHVECTOR_ENDPOINT", "DashVector Endpoint", True),
    ]),
    ("目录（可选，留空用默认）", [
        ("MEETING_MINUTES_DIR", "会议纪要目录", False),
        ("TOUTIAO_DRAFTS_DIR", "头条草稿目录", False),
        ("WECHAT_DRAFTS_DIR", "公众号草稿预览目录", False),
    ]),
    ("对外访问地址（封面图/链接用，可选）", [
        ("PUBLIC_BASE_URL", "公网可达地址，如 http://IP:5151 或 https://域名——公众号封面图与邮件链接都要用它", False),
    ]),
]

MAIL_UI_FIELDS = [
    ("smtp_host", "SMTP 服务器（QQ 填 smtp.qq.com）", False),
    ("smtp_port", "端口（465=SSL / 587=STARTTLS）", False),
    ("username", "账号（QQ 填 完整邮箱）", False),
    ("password", "授权码/密码（QQ 用授权码，不是登录密码）", True),
    ("from_addr", "发件地址", False),
    ("to_addr", "收件地址（审核通知发到这里）", False),
    ("public_base_url", "对外访问地址（收件人可访问，用于拼审核链接）", False),
]

_CONFIG_MAIL_INT_FIELDS = {"smtp_port"}


def _config_page_view():
    """渲染数据：当前值存在性（不回显值本身）+ config_check 体检结果。"""
    config = load_config_safe()
    mail = config.get("MAIL") if isinstance(config.get("MAIL"), dict) else {}
    issues, load_error = config_check.load_and_validate()
    return {
        "groups": CONFIG_UI_GROUPS,
        "has": set(config.keys()),
        "mail_fields": MAIL_UI_FIELDS,
        "mail_has": set(mail.keys()),
        "issues": issues,
        "load_error": load_error,
        "msg": session.pop("config_page_msg", None),
        "error": session.pop("config_page_error", None),
    }


@app.route("/admin/config")
def config_page():
    return render_template("config.html", **_config_page_view())


@app.route("/admin/config/save", methods=["POST"])
def config_save():
    """保存表单里的字段。约定：留空=保持原值；勾了"置空"=删除该项；密钥不回显也不回写空串。"""
    config, load_error = _load_config_for_update()
    if config is None:
        session["config_page_error"] = f"config.json 读取失败（{load_error}），拒绝写入以免覆盖已有配置。"
        return redirect(url_for("config_page"))
    config = config if isinstance(config, dict) else {}

    changed: list[str] = []
    try:
        for _group, fields in CONFIG_UI_GROUPS:
            for key, _label, _secret in fields:
                clear = bool(request.form.get(f"{key}__clear"))
                if clear:
                    config.pop(key, None)
                    changed.append(f"{key}（已置空）")
                    continue
                new_value = (request.form.get(key) or "").strip()
                if new_value:
                    if key == "MaxTokens":
                        int(new_value)  # 校验，抛错进 except
                    config[key] = new_value
                    changed.append(key)
        mail = config.get("MAIL") if isinstance(config.get("MAIL"), dict) else {}
        mail_changed = False
        for mf, _label, _secret in MAIL_UI_FIELDS:
            clear = bool(request.form.get(f"MAIL__{mf}__clear"))
            if clear:
                mail.pop(mf, None)
                mail_changed = True
                changed.append(f"MAIL.{mf}（已置空）")
                continue
            new_value = (request.form.get(f"MAIL__{mf}") or "").strip()
            if new_value:
                if mf in _CONFIG_MAIL_INT_FIELDS:
                    mail[mf] = int(new_value)
                else:
                    mail[mf] = new_value
                mail_changed = True
                changed.append(f"MAIL.{mf}")
        if mail_changed:
            config["MAIL"] = mail
        _write_config_with_backup(config)
    except ValueError as exc:
        session["config_page_error"] = f"保存失败，格式不对：{exc}"
        return redirect(url_for("config_page"))
    session["config_page_msg"] = "配置已保存。" + (f" 更新：{', '.join(changed)}" if changed else "（本次没有改动）")
    return redirect(url_for("config_page"))


@app.route("/admin/config/test-mail", methods=["POST"])
def config_test_mail():
    """立即发一封测试邮件验证 MAIL 配置（不用等工作流触发）。失败原因显示在页面上。"""
    settings = mailer.mail_settings()
    if not settings:
        session["config_page_error"] = "还没配置 MAIL：先在系统配置里填好邮箱并保存。"
        return redirect(url_for("config_page"))
    ok = mailer.send_mail(settings, "[super_brain] 测试邮件",
                          "<p>配置成功 ✅ 这是一封来自 super_brain 的测试邮件。</p>")
    if ok:
        session["config_page_msg"] = "测试邮件已发送，请到收件箱确认。"
    else:
        session["config_page_error"] = "测试邮件发送失败：" + (mailer.last_error() or "未知错误，看日志 logs/super_brain.log")
    return redirect(url_for("config_page"))


# ==================== 内容工作流（P4：定时 → agent1 → 预览 → 审批 → 依次执行） ====================

_workflow_lock = threading.Lock()


def _workflow_scheduler_loop() -> None:
    while True:
        time.sleep(60)
        try:
            if _workflow_lock.acquire(blocking=False):
                try:
                    workflow_engine.scheduler_tick()
                finally:
                    _workflow_lock.release()
        except Exception:
            logger.exception("内容工作流调度 tick 异常（已捕获，不影响下一分钟）")


def _workflow_step_pill(status: str) -> str:
    css = {
        workflow_engine.ST_PENDING: "pill-queued",
        workflow_engine.ST_RUNNING: "pill-drafted",
        workflow_engine.ST_AWAITING: "pill-approved",
        workflow_engine.ST_DONE: "pill-published",
        workflow_engine.ST_APPROVED: "pill-published",
        workflow_engine.ST_REJECTED: "pill-cancelled",
        workflow_engine.ST_FAILED: "pill-failed",
        workflow_engine.ST_SKIPPED: "pill-skipped",
    }
    return css.get(status, "pill-queued")


def _workflow_msg_set(message: str | None, error: str | None = None) -> None:
    if message:
        session["workflows_msg"] = message
    if error:
        session["workflows_error"] = error


STEP_STATUS_ZH = {
    workflow_engine.ST_PENDING: "待执行",
    workflow_engine.ST_RUNNING: "执行中",
    workflow_engine.ST_AWAITING: "等你审核",
    workflow_engine.ST_DONE: "已通过",
    workflow_engine.ST_APPROVED: "已通过",
    workflow_engine.ST_REJECTED: "已打回",
    workflow_engine.ST_FAILED: "失败",
    workflow_engine.ST_SKIPPED: "跳过",
}


@app.route("/admin/workflows")
def workflows_page():
    defs = workflow_engine.load_workflows()
    runs = []
    for run in workflow_engine.load_all_runs():
        steps = run.get("steps", [])
        waiting = next((s for s in steps if s["status"] == workflow_engine.ST_AWAITING), None)
        view_steps = []
        for s in steps:
            art = s.get("artifact")
            artifact_json = json.dumps(art, ensure_ascii=False, indent=1)[:12000] if art else ""
            payload = (art or {}).get("payload") or (art or {}).get("preview") or ""
            empty_hint = bool(art) and art.get("kind") in ("research", "draft", "critic") \
                         and not str(payload).strip()
            view_steps.append({**s, "artifact_json": artifact_json, "empty_hint": empty_hint})
        runs.append({
            "run": run,
            "status": workflow_engine.run_status_for_ui(run),
            "steps": view_steps,
            "run_id": run["run_id"],
            "waiting": waiting,
            "zh": STEP_STATUS_ZH,
        })
    highlight_run = (request.args.get("run") or "").strip()   # 邮件链接带 run=xxx 直达该单
    return render_template(
        "workflows.html",
        workflows=defs,
        runs=runs,
        highlight_run=highlight_run,
        default_workflow_id=workflow_engine.DEFAULT_WORKFLOW_ID,
        msg=session.pop("workflows_msg", None),
        error=session.pop("workflows_error", None),
        pill=_workflow_step_pill,
    )


@app.route("/admin/workflows/save", methods=["POST"])
def workflows_save():
    """保存各工作流定义的调度时间/开关 + 每一步是否要审批（requires_approval 可配）。"""
    workflows = workflow_engine.load_workflows()
    for wf_id in workflows:
        workflows[wf_id]["direction"] = (request.form.get(f"wf_{wf_id}_direction") or "").strip()
        schedule = workflows[wf_id].setdefault("schedule", {})
        raw_time = (request.form.get(f"wf_{wf_id}_time") or "").strip()
        if raw_time and not re.fullmatch(r"\d{1,2}:\d{2}", raw_time):
            _workflow_msg_set(None, f"工作流 {wf_id} 的时间格式不对：{raw_time!r}（要 HH:MM）")
            return redirect(url_for("workflows_page"))
        schedule["time"] = raw_time or schedule.get("time", "20:00")
        schedule["enabled"] = bool(request.form.get(f"wf_{wf_id}_enabled"))
        steps = workflows[wf_id].get("steps", [])
        for i, step in enumerate(steps):
            step["requires_approval"] = bool(request.form.get(f"wf_{wf_id}_s{i}_approval"))
    workflow_engine.save_workflows(workflows)
    _workflow_msg_set("工作流定义已保存（含每步审批开关）。")
    return redirect(url_for("workflows_page"))


def _run_with_lock(fn, ok_msg):
    if not _workflow_lock.acquire(blocking=False):
        _workflow_msg_set(None, "已有工作流动作在执行，请稍等再试。")
        return
    try:
        fn()
        if ok_msg:
            _workflow_msg_set(ok_msg)
    except Exception as exc:
        logger.exception("工作流操作失败")
        _workflow_msg_set(None, f"操作失败：{exc}")
    finally:
        _workflow_lock.release()


@app.route("/admin/workflows/run/new", methods=["POST"])
def workflows_run_new():
    """手动触发一次完整工作流（真实调用 DeepSeek/Tavily）。第一步骤产出后停在审批口。"""
    wf_id = (request.form.get("workflow_id") or "").strip()
    topic = (request.form.get("topic") or "").strip()
    source_text = (request.form.get("source_text") or "").strip()

    def _do():
        if wf_id not in workflow_engine.load_workflows():
            raise ValueError(f"未知工作流：{wf_id}")
        run = workflow_engine.create_run(wf_id, topic=topic, source_text=source_text)
        try:
            api_key = llm_client.load_deepseek_api_key()
        except llm_client.DeepSeekConfigError as exc:
            workflow_engine.save_run(run)  # 先留着空 run，方便之后补 key 重试
            raise ValueError(f"DeepSeek 未配置：{exc}")
        workflow_engine.advance(run["run_id"], api_key=api_key)
        _workflow_msg_set(f"工作流已启动：{run['run_id']}。第一步已执行，去下面审批队列查看/通过。")

    _run_with_lock(_do, None)
    return redirect(url_for("workflows_page"))


@app.route("/admin/workflows/run/<run_id>/step/<int:step_index>/approve", methods=["POST"])
def workflows_step_approve(run_id, step_index):
    note = (request.form.get("note") or "").strip()
    radio_topic = (request.form.get("topic") or "").strip()
    custom_topic = (request.form.get("topic_custom") or "").strip()
    chosen_topic = custom_topic if (radio_topic == "__custom__" or custom_topic) else radio_topic

    def _do():
        try:
            api_key = llm_client.load_deepseek_api_key()
        except llm_client.DeepSeekConfigError:
            api_key = None  # 后续步骤没有需要 LLM 的就不用 key；需要时会在引擎里报错可重试
        workflow_engine.approve_step(run_id, step_index, note=note, approved_by="CEO",
                                     chosen_topic=chosen_topic)
        _workflow_msg_set("审批通过，已自动推进下一步。")

    _run_with_lock(_do, None)
    return redirect(url_for("workflows_page"))


@app.route("/admin/workflows/run/<run_id>/step/<int:step_index>/reject", methods=["POST"])
def workflows_step_reject(run_id, step_index):
    reason = (request.form.get("reason") or "").strip()
    _run_with_lock(lambda: workflow_engine.reject_step(run_id, step_index, reason=reason),
                   "已打回，该 run 停止。可在该步上改参数后重试。")
    return redirect(url_for("workflows_page"))


@app.route("/admin/workflows/run/<run_id>/step/<int:step_index>/retry", methods=["POST"])
def workflows_step_retry(run_id, step_index):
    def _do():
        try:
            api_key = llm_client.load_deepseek_api_key()
        except llm_client.DeepSeekConfigError:
            api_key = None
        workflow_engine.retry_step(run_id, step_index, api_key=api_key)
        _workflow_msg_set("已重试该步骤。")

    _run_with_lock(_do, None)
    return redirect(url_for("workflows_page"))


@app.route("/admin/workflows/run/<run_id>/step/<int:step_index>/redo", methods=["POST"])
def workflows_step_redo(run_id, step_index):
    """等待审批的步骤重新执行（覆盖旧产物再等审批），用于产物为空/不满意时。"""
    def _do():
        try:
            api_key = llm_client.load_deepseek_api_key()
        except llm_client.DeepSeekConfigError:
            api_key = None
        workflow_engine.redo_step(run_id, step_index, api_key=api_key)
        _workflow_msg_set("已重新执行该步，等待审核。")

    _run_with_lock(_do, None)
    return redirect(url_for("workflows_page"))


@app.route("/admin/workflows/run/<run_id>/step/<int:step_index>/skip", methods=["POST"])
def workflows_step_skip(run_id, step_index):
    """跳过该步继续（用于某步因额度/网络反复失败等）——跳过内容步骤=人工后续补，跳过 publish=不产生发布单。"""
    reason = (request.form.get("reason") or "").strip()
    _run_with_lock(lambda: workflow_engine.skip_step(run_id, step_index, reason=reason),
                   "已跳过该步，尝试继续推进。")
    return redirect(url_for("workflows_page"))


@app.route("/admin/workflows/run/<run_id>/stop", methods=["POST"])
def workflows_run_stop(run_id):
    _run_with_lock(lambda: workflow_engine.stop_run(run_id, reason="CEO 手动停止"), "已停止该 run。")
    return redirect(url_for("workflows_page"))


# pytest 会自动设置 PYTEST_CURRENT_TEST 这个环境变量——测试文件 import ui_app 时必须
# 跳过这一步，否则会启动一个真实的后台线程，一旦测试恰好在过了本机 18 点之后运行，
# 会触发真实的、要花钱的 DeepSeek 批量调用，不是测试应该产生的副作用。正常运行（gunicorn/
# 本机 python ui_app.py）不会有这个环境变量，行为不受影响。
if not os.environ.get("PYTEST_CURRENT_TEST"):
    threading.Thread(target=_daily_batch_scheduler_loop, daemon=True).start()
    logger.info("每日 18 点批量汇总的调度线程已启动（每分钟检查一次）")
    threading.Thread(target=_autopublish_scheduler_loop, daemon=True).start()
    logger.info("自动化媒体发布调度线程已启动（每分钟检查一次，事件默认全部关闭）")
    threading.Thread(target=_workflow_scheduler_loop, daemon=True).start()
    logger.info("内容工作流调度线程已启动（每分钟检查一次，工作流默认全部关闭）")

if __name__ == "__main__":
    # 本机开发默认只监听 127.0.0.1（不对局域网/公网开放）；容器部署时 Dockerfile 会把
    # SUPER_BRAIN_HOST 设成 0.0.0.0，否则容器外访问不到。生产环境走 gunicorn（见
    # Dockerfile），不会执行这个 __main__ 分支，这里只是本机 `python ui_app.py` 的入口。
    host = os.environ.get("SUPER_BRAIN_HOST", "127.0.0.1")
    port = int(os.environ.get("SUPER_BRAIN_PORT", "5151"))
    logger.info(f"===== super_brain UI 启动，http://{host}:{port} =====")
    app.run(host=host, port=port, debug=False, threaded=True)
