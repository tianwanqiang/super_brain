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
import digest
import dispatcher
import executors
import i18n
import llm_client
import private_chat
import publishers
import rag
import review
import roundtable
import tasks
import video_prompt
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
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig")), None
    except (json.JSONDecodeError, OSError) as exc:
        return None, str(exc)


def _write_config_with_backup(config: dict) -> None:
    """写 config.json 前先把当前文件备份成 config.json.bak（只保留最近一份，不是历史
    版本链）——防止这次写入内容本身有问题、或者以后又出现类似覆盖丢失的 bug 时还有得救。
    """
    if CONFIG_PATH.exists():
        try:
            CONFIG_PATH.replace(CONFIG_PATH.parent / (CONFIG_PATH.name + ".bak"))
        except OSError:
            logger.warning("UI：备份 config.json 失败，继续写入（不阻塞正常保存流程）")
    CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")


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
            api_key = llm_client.load_deepseek_api_key()
            result = executors.execute_ops_assistant_from_minutes(minutes_path, api_key)
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
        logger.warning("UI：会议纪要目录表单提交了空值，已忽略")
        return redirect(request.referrer or url_for("index"))

    config, load_error = _load_config_for_update()
    if load_error is not None:
        logger.error(f"UI：config.json 读取失败，拒绝写入以免覆盖已有配置：{load_error}")
        session["roundtable_error"] = (
            f"config.json 读取失败（{load_error}），为了不覆盖已有的 API_KEY 等配置，"
            "这次的目录设置没有保存——请先手动检查/修复服务器上的 config.json。"
        )
        return redirect(request.referrer or url_for("index"))

    config["MEETING_MINUTES_DIR"] = value
    _write_config_with_backup(config)
    Path(value).mkdir(parents=True, exist_ok=True)
    logger.info(f"UI：MEETING_MINUTES_DIR 已设置为 {value}（首次配置，之后不再需要重复问）")
    warning = _persistence_warning_for_path(value)
    if warning:
        logger.warning(f"UI：{warning}")
        session["roundtable_error"] = warning
    return redirect(request.referrer or url_for("index"))


@app.route("/config/set-toutiao-drafts-dir", methods=["POST"])
def config_set_toutiao_drafts_dir():
    """头条草稿存放目录——不像会议纪要目录那样是硬性必填项，没设置就用历史默认值兜底，
    设置了就迁移过去（publishers.get_toutiao_drafts_dir() 读的是这同一个 key）。
    """
    value = _normalize_path_input(request.form.get("toutiao_drafts_dir", ""))
    if not value:
        logger.warning("UI：头条草稿目录表单提交了空值，已忽略")
        return redirect(request.referrer or url_for("admin"))

    config, load_error = _load_config_for_update()
    if load_error is not None:
        logger.error(f"UI：config.json 读取失败，拒绝写入以免覆盖已有配置：{load_error}")
        session["roundtable_error"] = (
            f"config.json 读取失败（{load_error}），为了不覆盖已有的 API_KEY 等配置，"
            "这次的目录设置没有保存——请先手动检查/修复服务器上的 config.json。"
        )
        return redirect(request.referrer or url_for("admin"))

    config["TOUTIAO_DRAFTS_DIR"] = value
    _write_config_with_backup(config)
    Path(value).mkdir(parents=True, exist_ok=True)
    logger.info(f"UI：TOUTIAO_DRAFTS_DIR 已设置为 {value}")
    warning = _persistence_warning_for_path(value)
    if warning:
        logger.warning(f"UI：{warning}")
        session["roundtable_error"] = warning
    return redirect(request.referrer or url_for("admin"))


DRAFT_PREVIEW_EXTS = {".md", ".txt"}


@app.route("/draft/preview")
def draft_preview():
    """快捷预览头条草稿——安全边界很关键：path 参数完全来自用户输入（哪怕是本机单用户工具），
    必须校验解析后的真实路径确实落在头条草稿目录内，不然就是一个任意文件读取漏洞。
    """
    raw_path = request.args.get("path", "")
    if not raw_path:
        return "缺少 path 参数", 400

    target = Path(raw_path).resolve()
    allowed_root = publishers.get_toutiao_drafts_dir().resolve()
    try:
        target.relative_to(allowed_root)
    except ValueError:
        logger.warning(f"UI：草稿预览请求被拒绝——路径不在允许的目录内：{raw_path!r}")
        return "只能预览头条草稿目录内的文件", 403

    if target.suffix.lower() not in DRAFT_PREVIEW_EXTS:
        return "只能预览 .md / .txt 文件", 403
    if not target.is_file():
        return "文件不存在（可能已被移动或删除）", 404

    content = target.read_text(encoding="utf-8", errors="replace")
    return render_template("draft_preview.html", path=str(target), content=content)


# pytest 会自动设置 PYTEST_CURRENT_TEST 这个环境变量——测试文件 import ui_app 时必须
# 跳过这一步，否则会启动一个真实的后台线程，一旦测试恰好在过了本机 18 点之后运行，
# 会触发真实的、要花钱的 DeepSeek 批量调用，不是测试应该产生的副作用。正常运行（gunicorn/
# 本机 python ui_app.py）不会有这个环境变量，行为不受影响。
if not os.environ.get("PYTEST_CURRENT_TEST"):
    threading.Thread(target=_daily_batch_scheduler_loop, daemon=True).start()
    logger.info("每日 18 点批量汇总的调度线程已启动（每分钟检查一次）")

if __name__ == "__main__":
    # 本机开发默认只监听 127.0.0.1（不对局域网/公网开放）；容器部署时 Dockerfile 会把
    # SUPER_BRAIN_HOST 设成 0.0.0.0，否则容器外访问不到。生产环境走 gunicorn（见
    # Dockerfile），不会执行这个 __main__ 分支，这里只是本机 `python ui_app.py` 的入口。
    host = os.environ.get("SUPER_BRAIN_HOST", "127.0.0.1")
    port = int(os.environ.get("SUPER_BRAIN_PORT", "5151"))
    logger.info(f"===== super_brain UI 启动，http://{host}:{port} =====")
    app.run(host=host, port=port, debug=False, threaded=True)
