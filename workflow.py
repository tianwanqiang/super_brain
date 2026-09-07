"""
super_brain workflow - 内容工作流引擎（P4：定时 → agent1 → 预览 → 审批 → 依次执行）

背景：CEO 2026-09 拍板的新模型——不再靠 inbox 留言驱动，改成"定时任务 + 分步产物
预览 + 人工审批闸门"：到点先跑第一步（默认 researcher），产物进审批队列；你在 UI 点
"通过"才触发下一步（writer → critic → …），每步是否要审批由工作流定义里该步骤的
requires_approval 决定（可配，默认要）；最后一步把定稿交接成 autopublish 发布单
（渠道级审批仍在 /admin/autopublish 做，这里只负责内容生产侧）。

设计要点：
- 定义（Workflow Definition）存 config.json 的 CONTENT_WORKFLOWS 键（config_store 唯一
  读写），每个 workflow 有 label / schedule{time, enabled} / steps[ {id, agent,
  requires_approval, label} ]。默认给一条 default_daily：researcher → writer → critic
  → publisher（各步默认需审批，可改）。
- 运行实例（Run）落盘 workflow_runs/<run_id>.json：分步状态机 pending → running →
  awaiting_approval →(人批) approved → done；打回 rejected；异常 failed（可重试）。
  requires_approval=False 的步骤产完即 done 并自动继续下一 pending 步骤。
- 步骤处理器是确定性的函数表 step_handlers（agent 名 → 函数），真实调用 content_pipeline
  （researcher/writer/critic 会花 DeepSeek/Tavily 额度，只在执行那一刻发生）；
  测试用 monkeypatch 换假 handler，离线验证状态机。
- 调度：scheduler_tick() 每分钟由 ui_app 后台线程调用——到点且当天还没为这个 workflow
  开过 run 就 create_run + 自动推进第一步。
"""
import json
import logging
import uuid
from datetime import datetime
from pathlib import Path

import autopublish
import config_store
import content_pipeline
import llm_client
import mailer
from agent_registry import load_agent_registry, load_private_context
from paths import CONFIG_PATH, WORKFLOW_RUNS_DIR

logger = logging.getLogger("super_brain.workflow")

RUNS_DIR = WORKFLOW_RUNS_DIR
CONFIG_KEY = "CONTENT_WORKFLOWS"

# 步骤状态
ST_PENDING = "pending"
ST_RUNNING = "running"
ST_AWAITING = "awaiting_approval"
ST_APPROVED = "approved"      # 人已点通过（之后会被置为 done）
ST_DONE = "done"              # 终态：需审批的=人通过后；不需审批的=产出即 done
ST_REJECTED = "rejected"
ST_FAILED = "failed"
ST_SKIPPED = "skipped"

RUN_RUNNING = "running"
RUN_AWAITING = "awaiting_approval"
RUN_DONE = "done"
RUN_STOPPED = "stopped"
RUN_ERROR = "error"

DEFAULT_WORKFLOW_ID = "default_daily"

DEFAULT_WORKFLOWS = {
    DEFAULT_WORKFLOW_ID: {
        "label": "每日内容流水线（选题审核 → 研究 → 写作 → 点评 → 交接发布）",
        # direction：可选"运营方向预设"——定时自动选题时让 agent1 朝这个方向出候选；
        # 留空则按系统定位（一人公司决策陪练 / AI 圆桌）泛选。不强制用户提供。
        "direction": "",
        "schedule": {"time": "20:00", "enabled": False},
        "steps": [
            {"id": "topics", "agent": "researcher", "requires_approval": True,
             "label": "agent1·选题提案（生成候选，审核定题后继续）"},
            {"id": "research", "agent": "researcher", "requires_approval": True,
             "label": "agent1·信息搜集（按已定选题联网+RAG，红线去伪）"},
            {"id": "write", "agent": "writer", "requires_approval": True,
             "label": "agent2·成稿（writer 链）"},
            {"id": "critic", "agent": "critic", "requires_approval": True,
             "label": "agent3·评分卡点评（只评不改）"},
            {"id": "publish", "agent": "publisher", "requires_approval": False,
             "label": "agent4·交接发布单（渠道级审批在发布后台）"},
        ],
    }
}


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


# ---------- 定义（config.json 的 CONTENT_WORKFLOWS） ----------

def load_workflows() -> dict:
    """读工作流定义，缺的用默认补（返回全新 dict，不写回文件）。"""
    stored = config_store.read_config_soft(path=CONFIG_PATH, cached=True).get(CONFIG_KEY) or {}
    workflows = json.loads(json.dumps(DEFAULT_WORKFLOWS))
    for wf_id, stored_def in stored.items():
        if not isinstance(stored_def, dict):
            continue
        merged = json.loads(json.dumps(DEFAULT_WORKFLOWS.get(wf_id, {
            "label": wf_id, "direction": "", "schedule": {"time": "20:00", "enabled": False},
            "steps": []})))
        for key in ("label", "direction"):
            if key in stored_def:
                merged[key] = stored_def[key]
        if isinstance(stored_def.get("schedule"), dict):
            merged["schedule"].update(stored_def["schedule"])
        if isinstance(stored_def.get("steps"), list) and stored_def["steps"]:
            merged["steps"] = stored_def["steps"]
        workflows[wf_id] = merged
    return workflows


def save_workflows(workflows: dict) -> None:
    full = config_store.read_config_soft(path=CONFIG_PATH)
    full[CONFIG_KEY] = workflows
    config_store.write_config_with_backup(full, path=CONFIG_PATH)


# ---------- 运行实例 CRUD ----------

def _run_path(run_id: str) -> Path:
    return RUNS_DIR / f"{run_id}.json"


def save_run(run: dict) -> None:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    run["updated_at"] = _now()
    _run_path(run["run_id"]).write_text(json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8")


def load_run(run_id: str) -> dict | None:
    path = _run_path(run_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError):
        return None


def load_all_runs() -> list[dict]:
    if not RUNS_DIR.exists():
        return []
    runs = []
    for path in RUNS_DIR.glob("*.json"):
        try:
            runs.append(json.loads(path.read_text(encoding="utf-8-sig")))
        except (json.JSONDecodeError, OSError):
            continue
    runs.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    return runs


def create_run(workflow_id: str, topic: str = "", source_text: str = "",
               scheduled_date: str | None = None, direction: str = "") -> dict:
    """按定义实例化一个 run（不自动执行，调用方决定何时 advance）。
    direction：本次运行的"运营方向预设"（定时触发时从定义带下来，供选题提案参考）。"""
    workflows = load_workflows()
    definition = workflows.get(workflow_id) or DEFAULT_WORKFLOWS[DEFAULT_WORKFLOW_ID]
    run_id = f"wf_{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}"
    run = {
        "run_id": run_id,
        "workflow_id": workflow_id,
        "label": definition.get("label", workflow_id),
        "topic": (topic or "").strip(),
        "source_text": source_text,
        "direction": (direction or "").strip(),
        "status": RUN_RUNNING,
        "scheduled_date": scheduled_date,
        "created_at": _now(),
        "updated_at": _now(),
        "steps": [
            {"index": i, "id": s["id"], "agent": s["agent"], "label": s.get("label", s["id"]),
             "requires_approval": bool(s.get("requires_approval", True)),
             "status": ST_PENDING, "artifact": None, "note": "",
             "started_at": None, "finished_at": None, "approved_at": None,
             "approved_by": None, "rejected_reason": None, "error": None,
             "mail_sent": False}
            for i, s in enumerate(definition.get("steps", []))
        ],
        "history": [],
    }
    # 手动触发时已给了选题：跳过最前面的"选题提案"步骤（否则它没有存在的意义）
    if run["topic"] and run["steps"] and run["steps"][0]["id"] == "topics":
        run["steps"][0]["status"] = ST_SKIPPED
        run["steps"][0]["note"] = "手动已给定选题，跳过选题提案"
        _append_history(run, f"手动已定选题：{run['topic']}，跳过选题提案步骤")
    save_run(run)
    return run


def _append_history(run: dict, message: str) -> None:
    run.setdefault("history", []).append({"at": _now(), "message": message})


# ---------- 步骤处理器（agent 名 → 实际干活） ----------

def _prev_artifact(run: dict, index: int) -> dict | None:
    """取上一个已完成步骤的产物（writer 读 research、critic/publisher 读 draft 用）。"""
    for step in reversed(run["steps"][:index]):
        if step["status"] in (ST_DONE, ST_APPROVED) and step.get("artifact"):
            return step["artifact"]
    return None


def _handle_topics(ctx: dict) -> dict:
    """agent1 定时任务的第一个动作：生成候选选题清单，供人审核定题。
    输入方向优先级：手动素材(source_text) > 本次运行方向预设(direction) > 已有 topic。
    都没有就结合 researcher 的框架出通用候选。产出 candidates，不含任何落地动作。"""
    framework = load_private_context("researcher", load_agent_registry())
    direction = ((ctx.get("source_text") or ctx.get("direction") or ctx.get("topic")) or "").strip()
    user_prompt = (
        f"运营/内容方向线索：{direction}" if direction
        else "（未给既定方向：请结合 super_brain 的定位——一人公司决策陪练 / AI 圆桌——给出候选）"
    )
    system_prompt = (
        f"下面是你的知识框架（private.md 原文）：\n\n{framework}\n\n"
        "你是选题策划：根据方向线索提出 3-5 个**候选选题**，要求：有具体切入角度、"
        "现在值得写、目标读者（一人公司老板/内容创作者）会点开。\n"
        "严格按行输出，每行格式：- 标题 | 一句话理由\n不要输出任何其它文字。"
    )
    # key 只在此步骤真需要调用 LLM 时解析（失败→该步 failed 并给清楚原因）；
    # 这样无 LLM 的步骤 / 假处理器测试在无 key 环境（CI）也能跑。
    api_key = ctx.get("api_key")
    if not api_key:
        try:
            api_key = llm_client.load_deepseek_api_key()
        except llm_client.DeepSeekConfigError as exc:
            raise ValueError(f"DeepSeek 未配置：{exc}") from exc
    raw = llm_client.call_deepseek(system_prompt, user_prompt, api_key, max_tokens=3000,
                                   model=llm_client.structured_model_override())
    candidates = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        body = line[1:].strip() if line.startswith(("-", "*")) else line
        if "|" in body:
            title, reason = body.split("|", 1)
        elif "｜" in body:
            title, reason = body.split("｜", 1)
        else:
            title, reason = body, ""
        title = (title or "").strip()
        if title:
            candidates.append({"title": title, "reason": (reason or "").strip()})
        if len(candidates) >= 5:
            break
    if not candidates:
        raise ValueError("选题提案没有生成任何候选（模型输出格式不对或内容为空）")
    preview = "\n".join(f"- {c['title']}（{c['reason'] or '—'}）" for c in candidates)
    return {"kind": "topics", "candidates": candidates, "count": len(candidates),
            "payload": "", "preview": preview}


def _handle_research(ctx: dict) -> dict:
    """agent1：信息搜集 → 信息包。ctx: {topic, api_key}。"""
    topic = (ctx.get("topic") or "").strip()
    if not topic:
        raise ValueError("research 步骤需要 topic（选题）")
    package = content_pipeline.run_research(topic, api_key=ctx.get("api_key"))
    file_path = content_pipeline.persist_research(package)
    return {
        "kind": "research",
        "topic": topic,
        "facts": len(package["facts"]),
        "dropped": len(package["dropped"]),
        "conflicts": len(package["conflict_notes"]),
        "web_used": package["web_used"],
        "file": str(file_path),
        "payload": content_pipeline.materialize_source(package),
        "preview": content_pipeline.materialize_source(package)[:4000],
    }


def _handle_write(ctx: dict) -> dict:
    """agent2：把上个步骤的产物（或手动 source_text）写成定稿。"""
    material = ctx.get("source_text") or ""
    if not material:
        prev = _prev_artifact(ctx["run"], ctx["index"])
        material = (prev or {}).get("payload", "") or (prev or {}).get("preview", "")
    if not material.strip():
        raise ValueError("write 步骤没有可写的素材：上游没有产物也没有 source_text")
    out = content_pipeline.run_writer_draft(material, api_key=ctx.get("api_key"))
    return {
        "kind": "draft",
        "title": out["title"],
        "payload": out["draft"],
        "preview": out["draft"][:4000],
    }


def _handle_critic(ctx: dict) -> dict:
    """agent3：对上个步骤的定稿出评分卡（只评不改）。"""
    prev = _prev_artifact(ctx["run"], ctx["index"])
    draft = ((prev or {}).get("payload") or "").strip()
    title = (prev or {}).get("title", "工作流定稿")
    if not draft:
        raise ValueError("critic 步骤没有可点评的定稿：上游没有 draft 产物")
    note = content_pipeline.run_critic(f"workflow-{ctx['run']['run_id']}", title, draft,
                                       platform="toutiao", api_key=ctx.get("api_key"))
    file_path = content_pipeline.persist_critic(note, draft)
    return {
        "kind": "critic",
        "verdict": note["verdict"],
        "scores": note.get("scores", {}),
        "must_fix": note.get("must_fix", []),
        "parse_error": note.get("parse_error", False),
        "file": str(file_path),
        "payload": draft,  # 定稿继续往下传（发布用）
        "preview": json.dumps({"verdict": note["verdict"], "scores": note.get("scores", {})},
                              ensure_ascii=False),
    }


def _handle_publish(ctx: dict) -> dict:
    """agent4：把定稿交接成 autopublish 发布单（渠道级审批在 /admin/autopublish）。"""
    prev = _prev_artifact(ctx["run"], ctx["index"])
    final_text = ((prev or {}).get("payload") or "").strip() or (ctx.get("source_text") or "").strip()
    if not final_text:
        raise ValueError("publish 步骤没有可交接的定稿")
    title = (prev or {}).get("title") or "工作流产出" + datetime.now().strftime(" %m-%d")
    order = autopublish.new_order(
        title,
        {"kind": "workflow", "text": final_text, "workflow_run_id": ctx["run"]["run_id"]},
        channels=list(autopublish.CHANNELS),
        note=f"内容工作流 {ctx['run']['run_id']} 交接",
    )
    autopublish.save_order(order)
    return {"kind": "publish_order", "handoff_order_id": order["id"], "payload": final_text,
            "preview": f"已交接为发布单 {order['id']}（请在发布后台继续审批/发布）"}


# 步骤分发：优先按步骤 id（step_id_handlers 精确命中，如 topics/research/write/critic/publish），
# 未命中再按 agent 名兜底（自定义步骤用 step_handlers）。
step_handlers: dict[str, object] = {
    "writer": _handle_write,
    "critic": _handle_critic,
    "publisher": _handle_publish,
}
step_id_handlers: dict[str, object] = {
    "topics": _handle_topics,
    "research": _handle_research,
    "write": _handle_write,
    "critic": _handle_critic,
    "publish": _handle_publish,
}


# ---------- 执行 / 审批状态机 ----------

def _execute_step(run: dict, step_index: int, api_key: str | None = None) -> None:
    """执行单个步骤：running → 产出 → (需审批 awaiting / 不需审批 done 继续)。"""
    step = run["steps"][step_index]
    handler = step_id_handlers.get(step["id"]) or step_handlers.get(step["agent"])
    if handler is None:
        step["status"] = ST_FAILED
        step["error"] = f"没有定义 step_id={step['id']} / agent={step['agent']} 的步骤处理器"
        run["status"] = RUN_ERROR
        save_run(run)
        return
    step["status"] = ST_RUNNING
    step["started_at"] = _now()
    save_run(run)
    ctx = {
        "run": run, "index": step_index, "step": step,
        "topic": run.get("topic", ""), "source_text": run.get("source_text", ""),
        "direction": run.get("direction", ""),
        "api_key": api_key,
    }
    try:
        step["artifact"] = handler(ctx)
    except Exception as exc:
        logger.exception(f"[workflow] 步骤 {step['id']}（agent={step['agent']}）执行失败")
        step["status"] = ST_FAILED
        step["error"] = str(exc)
        step["finished_at"] = _now()
        run["status"] = RUN_ERROR
        save_run(run)
        return
    step["finished_at"] = _now()
    if step["requires_approval"]:
        step["status"] = ST_AWAITING
        run["status"] = RUN_AWAITING
        _append_history(run, f"步骤 {step['id']} 产物就绪，等待人工审批")
    else:
        # 无需审批：选题提案这一步也要给出"自动选第一个候选"的兜底，否则下游没有 topic
        if step["id"] == "topics" and not run.get("topic"):
            candidates = (step["artifact"] or {}).get("candidates") or []
            if candidates:
                run["topic"] = candidates[0]["title"]
                _append_history(run, f"选题步骤无需审批：自动选定候选 {run['topic']}")
        step["status"] = ST_DONE
        step["approved_at"] = _now()
        step["approved_by"] = "system(无需审批)"
        _append_history(run, f"步骤 {step['id']} 完成（requires_approval=False，自动通过）")
    save_run(run)
    if step["status"] == ST_AWAITING:
        _notify_step_review(run, step)   # 进入审批口：发邮件链接（没配邮箱则跳过）


def advance(run_id: str, api_key: str | None = None) -> dict:
    """从第一个 pending 步骤开始推进：产出→审批口/自动继续，直到需要审批、出错或全部完成。
    返回最新 run dict。api_key 可选：真正需要调 LLM 的步骤处理器会自行从 config 加载
    （加载失败 → 该步骤 failed 并给出"DeepSeek 未配置"原因），因此无 LLM 的步骤/假处理器
    测试在没有任何 key 的环境（如 CI fresh checkout）也能正常跑。调用方在 UI 里执行时传
    api_key（真实调用会花额度）。"""
    run = load_run(run_id)
    if run is None:
        raise ValueError(f"运行实例不存在：{run_id}")
    while True:
        pending = [s for s in run["steps"] if s["status"] == ST_PENDING]
        if not pending:
            if any(s["status"] == ST_AWAITING for s in run["steps"]):
                run["status"] = RUN_AWAITING
            elif any(s["status"] == ST_FAILED for s in run["steps"]):
                run["status"] = RUN_ERROR
            elif any(s["status"] == ST_REJECTED for s in run["steps"]):
                run["status"] = RUN_STOPPED
            else:
                run["status"] = RUN_DONE
                _append_history(run, "全部步骤完成")
            save_run(run)
            return run
        index = min(pending, key=lambda s: s["index"])["index"]
        _execute_step(run, index, api_key=api_key)
        run = load_run(run_id)  # 重读，防 handler 内部状态漂移
        if run is None:
            raise ValueError(f"运行实例丢失：{run_id}")
        executed = next(s for s in run["steps"] if s["index"] == index)
        if executed["status"] == ST_FAILED:
            # 某步失败：整条 run 停下等人重试，绝不自动跑它后面的步骤
            run["status"] = RUN_ERROR
            save_run(run)
            return run
        if executed["status"] == ST_AWAITING:
            run["status"] = RUN_AWAITING
            save_run(run)
            return run
        # executed 是 done（无需审批自动通过）→ 继续循环跑下一个 pending 步骤


def approve_step(run_id: str, step_index: int, note: str = "", approved_by: str = "CEO",
                 chosen_topic: str | None = None) -> dict:
    """人工审批闸门：点"通过" → 该步 done → 自动继续推进下一步。
    topics（选题提案）步骤必须先确定选题：chosen_topic 给候选标题或自定义文字。"""
    run = load_run(run_id)
    if run is None:
        raise ValueError(f"运行实例不存在：{run_id}")
    step = next((s for s in run["steps"] if s["index"] == step_index), None)
    if step is None:
        raise ValueError(f"步骤不存在：{step_index}")
    if step["status"] != ST_AWAITING:
        raise ValueError(f"步骤 {step['id']} 当前状态 {step['status']}，不需要/不能审批")
    artifact = step.get("artifact") or {}
    if artifact.get("kind") == "topics":
        topic = (chosen_topic or run.get("topic") or "").strip()
        if not topic:
            raise ValueError("选题提案步骤必须先确定选题：勾选一个候选，或填自定义选题")
        if not any(c.get("title") == topic for c in artifact.get("candidates", [])):
            _append_history(run, f"选题使用自定义：{topic}")
        run["topic"] = topic
        _append_history(run, f"选题确定：{topic}")
    step["status"] = ST_DONE
    step["approved_at"] = _now()
    step["approved_by"] = approved_by
    _append_history(run, f"步骤 {step['id']} 人工审批通过（{approved_by}）{('：' + note) if note else ''}")
    save_run(run)
    return advance(run_id)


def reject_step(run_id: str, step_index: int, reason: str = "") -> dict:
    """人工打回：该步 rejected，整条 run 停下等人处理（可改参数重试）。"""
    run = load_run(run_id)
    if run is None:
        raise ValueError(f"运行实例不存在：{run_id}")
    step = next((s for s in run["steps"] if s["index"] == step_index), None)
    if step is None:
        raise ValueError(f"步骤不存在：{step_index}")
    if step["status"] != ST_AWAITING:
        raise ValueError(f"步骤 {step['id']} 当前状态 {step['status']}，不能打回")
    step["status"] = ST_REJECTED
    step["rejected_reason"] = reason or "（未填原因）"
    run["status"] = RUN_STOPPED
    _append_history(run, f"步骤 {step['id']} 被打回：{reason}")
    save_run(run)
    return run


def retry_step(run_id: str, step_index: int, api_key: str | None = None) -> dict:
    """重试 failed/rejected 的步骤：重置为 pending 后重新推进。"""
    run = load_run(run_id)
    if run is None:
        raise ValueError(f"运行实例不存在：{run_id}")
    step = next((s for s in run["steps"] if s["index"] == step_index), None)
    if step is None or step["status"] not in (ST_FAILED, ST_REJECTED):
        raise ValueError(f"步骤 {step_index} 不可重试（仅 failed/rejected 可重试）")
    step["status"] = ST_PENDING
    step["error"] = None
    step["rejected_reason"] = None
    step["artifact"] = None
    run["status"] = RUN_RUNNING
    _append_history(run, f"重试步骤 {step['id']}")
    save_run(run)
    return advance(run_id, api_key=api_key)


def redo_step(run_id: str, step_index: int, api_key: str | None = None) -> dict:
    """已停在审批口但产物质量不行/内容为空时，重新执行该步（覆盖旧产物再等审批）。
    只允许 awaiting 状态的步骤；publish 交接类步骤不许重跑（避免重复建发布单）。"""
    run = load_run(run_id)
    if run is None:
        raise ValueError(f"运行实例不存在：{run_id}")
    step = next((s for s in run["steps"] if s["index"] == step_index), None)
    if step is None:
        raise ValueError(f"步骤不存在：{step_index}")
    if step["status"] != ST_AWAITING:
        raise ValueError(f"步骤 {step['id']} 当前状态 {step['status']}，不能重跑（仅等待审批时可重跑）")
    if step["id"] == "publish":
        raise ValueError("publish 交接步骤不可重跑（避免重复建发布单），请打回后人工处理")
    step["status"] = ST_PENDING
    step["artifact"] = None
    step["error"] = None
    step["rejected_reason"] = None
    step["approved_at"] = None
    run["status"] = RUN_RUNNING
    _append_history(run, f"等待审批中重跑步骤 {step['id']}（覆盖旧产物）")
    save_run(run)
    return advance(run_id, api_key=api_key)


def skip_step(run_id: str, step_index: int, reason: str = "") -> dict:
    """跳过某一步继续往下走：比如某步因额度/网络反复失败，你先让它过，人工后续补。
    只允许 failed/rejected/awaiting 状态的步骤；publish 交接步被跳过 = 该 run 不产生发布单。"""
    run = load_run(run_id)
    if run is None:
        raise ValueError(f"运行实例不存在：{run_id}")
    step = next((s for s in run["steps"] if s["index"] == step_index), None)
    if step is None:
        raise ValueError(f"步骤不存在：{step_index}")
    if step["status"] not in (ST_FAILED, ST_REJECTED, ST_AWAITING):
        raise ValueError(f"步骤 {step['id']} 当前状态 {step['status']}，不能跳过")
    step["status"] = ST_SKIPPED
    step["error"] = None
    step["rejected_reason"] = None
    step["note"] = (reason or step.get("note") or "").strip() or "人工跳过"
    _append_history(run, f"人工跳过步骤 {step['id']}：{reason or '（未填原因）'}")
    save_run(run)
    try:
        return advance(run_id)
    except Exception as exc:  # 后续步骤可能因同样问题失败——如实返回当前 run 让 UI 显示
        run = load_run(run_id)
        if run is not None:
            _append_history(run, f"跳过 {step['id']} 后推进报错：{exc}")
            save_run(run)
        return run or {}


def stop_run(run_id: str, reason: str = "") -> dict:
    """手动停止：未开始的步骤置 skipped。"""
    run = load_run(run_id)
    if run is None:
        raise ValueError(f"运行实例不存在：{run_id}")
    for step in run["steps"]:
        if step["status"] == ST_PENDING:
            step["status"] = ST_SKIPPED
    run["status"] = RUN_STOPPED
    _append_history(run, f"手动停止：{reason or '（未填原因）'}")
    save_run(run)
    return run


def delete_run(run_id: str) -> bool:
    """删除一个运行实例（连同其分步产物/审批记录一起从磁盘移除）。"""
    path = _run_path(run_id)
    if not path.exists():
        return False
    path.unlink()
    logger.info(f"运行实例已删除：{run_id}")
    return True


def rerun_run(run_id: str) -> dict:
    """按旧运行的选题/方向/素材，开一个全新的 run（复用输入，状态从头开始）。
    返回新 run，不自动推进（调用方决定何时 advance，通常紧接着 advance）。"""
    old = load_run(run_id)
    if old is None:
        raise ValueError(f"运行实例不存在：{run_id}")
    return create_run(
        old.get("workflow_id", DEFAULT_WORKFLOW_ID),
        topic=old.get("topic", ""),
        source_text=old.get("source_text", ""),
        direction=old.get("direction", ""),
    )


def edit_run_fields(run_id: str, topic: str | None = None, direction: str | None = None) -> dict:
    """人工临时修改 run 的选题/方向（不重启，只覆盖输入字段；已产出的步骤保留）。"""
    run = load_run(run_id)
    if run is None:
        raise ValueError(f"运行实例不存在：{run_id}")
    if topic is not None:
        run["topic"] = topic.strip()
        _append_history(run, f"人工修改选题为：{run['topic']}")
    if direction is not None:
        run["direction"] = direction.strip()
        _append_history(run, f"人工修改方向预设为：{run['direction']}")
    save_run(run)
    return run


# ---------- 调度（定时：到点先跑第一步，等审批再往下） ----------

def scheduler_tick(now: datetime | None = None) -> list[str]:
    """每分钟由 ui_app 后台线程调用：每个 enabled 且到点的 workflow，当天还没开过 run
    就 create_run + advance（第一步产出后通常停在审批口等你点通过）。"""
    now = now or datetime.now()
    today = now.strftime("%Y-%m-%d")
    started: list[str] = []
    runs = load_all_runs()
    for wf_id, definition in load_workflows().items():
        schedule = definition.get("schedule") or {}
        if not schedule.get("enabled"):
            continue
        try:
            hh, mm = str(schedule.get("time", "")).split(":")
            if now.strftime("%H:%M") != f"{int(hh):02d}:{int(mm):02d}":
                continue
        except (ValueError, AttributeError):
            continue
        if any(r["workflow_id"] == wf_id and r.get("scheduled_date") == today for r in runs):
            continue  # 今天已经为这个 workflow 开过 run，不再重复
        try:
            run = create_run(wf_id, scheduled_date=today,
                             direction=(definition.get("direction") or ""))
            advance(run["run_id"])
            started.append(wf_id)
            logger.info(f"[workflow] 定时触发 {wf_id}：run={run['run_id']}，"
                        f"第一步已执行，当前停在审批口等人工通过")
        except Exception:
            logger.exception(f"[workflow] 定时触发 {wf_id} 失败（不影响其它 workflow）")
    return started


def _notify_step_review(run: dict, step: dict) -> None:
    """步骤进入审批口时发一封审核邮件（含直达后台链接）。没配邮箱/发送失败都只是记一笔，
    人仍然可以直接在 /admin/workflows 审核——邮件是通知手段，不是审核的唯一入口。"""
    settings = mailer.mail_settings()
    if not settings:
        _append_history(run, "未配置邮件（MAIL 段），请在 /admin/workflows 直接审核")
        save_run(run)
        return
    if step.get("mail_sent"):
        return
    url = mailer.review_url(settings, f"/admin/workflows?run={run['run_id']}&focus={step['index']}")
    subject = f"[super_brain] {run['label']} · {step['label']} 待审核"
    lines = [f"<p><strong>{run['label']}</strong>（{run['run_id']}）的步骤"
             f"<strong>{step['label']}</strong> 已就绪，等待你的审核。</p>"]
    artifact = step.get("artifact") or {}
    if artifact.get("kind") == "topics":
        lines.append("<p>候选选题：</p><ul>")
        for c in artifact.get("candidates", []):
            lines.append(f"<li>{c.get('title', '')} —— {c.get('reason', '')}</li>")
        lines.append("</ul>")
    elif artifact.get("kind") == "research":
        lines.append(f"<p>信息包：facts={artifact.get('facts')}，dropped={artifact.get('dropped')}，"
                     f"冲突={artifact.get('conflicts')}</p>")
    # 链接做成显眼按钮、不在 HTML 里写 target（部分邮箱客户端会强制 _blank 且弹窗被拦）。
    # 同时附一行可复制的明文地址兜底：QQ 等客户端沙箱里点不动链接时，手动复制也能打开。
    lines.append(
        '<div style="margin:16px 0;">'
        f'<a href="{url}" style="display:inline-block;padding:10px 22px;background:#0e6e76;color:#ffffff;'
        'text-decoration:none;border-radius:6px;font-size:15px;">打开管理界面预览并审核 →</a>'
        "</div>"
    )
    lines.append(
        '<p style="color:#666;font-size:12px;">如果上面的按钮在邮件里点不动（部分邮箱会拦截跳转），'
        f'请复制下面这行地址，粘贴到浏览器地址栏打开：</p>'
        f'<p style="word-break:break-all;font-size:12px;color:#0e6e76;">{url}</p>'
    )
    lines.append("<p style='color:#888;font-size:12px'>未配置登录密码时直接可进；配置了 "
                 "SUPER_BRAIN_PASSWORD 则先登录。</p>")
    ok = mailer.send_mail(settings, subject, "".join(lines))
    step["mail_sent"] = ok or "failed"
    _append_history(run, "已发送审核邮件" if ok else "审核邮件发送失败（请直接在后台审核）")
    save_run(run)


def run_status_for_ui(run: dict) -> str:
    """从步骤状态推导展示用总状态。"""
    if any(s["status"] == ST_AWAITING for s in run["steps"]):
        return "等待审批"
    if any(s["status"] == ST_FAILED for s in run["steps"]):
        return "失败"
    if any(s["status"] == ST_REJECTED for s in run["steps"]):
        return "已打回"
    if all(s["status"] == ST_DONE for s in run["steps"]):
        return "完成"
    return run.get("status", "running")
