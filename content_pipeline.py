"""
super_brain content_pipeline - 内容产出多 agent 流水线（agent1→agent2→agent3→agent4）

把四个 agent 串成一条带质检/迭代的流水线：
    agent1 researcher   选题 → 信息包（逐条带来源，代码层红线去伪）
    agent2 writer       信息包/素材 → 平台无关定稿（复用 executors.generate_writer_draft）
    agent3 critic       定稿 → 评分卡意见单（只点评不改稿）
    agent4 publisher    定稿交接给 autopublish 发布单（人工放行后才发）

设计要点：
- 每个环节是"一次思考一次产出"的 LLM 调用，不是常驻 agent；本模块是编排者。
- 付费 API 全部可注入（llm_call / search_call / writer_fn）：测试与离线评估用假实现，
  零成本跑通整条链路；真实调用只在显式传入或 CLI --live 时发生。
- researcher 的红线是**代码层硬校验**：提取出的事实若 source_url 不在本次检索结果集合里
  （或不是 local:// 本地来源），直接丢进 dropped，不允许进入下一环。
- critic 的 verdict 驱动迭代：revise 最多 MAX_REVISE_ROUNDS 轮，超限/accept 后进交接。
- 交接 agent4：把定稿建成 autopublish 发布单（artifacts 由 media-maker 步骤补），发布受
  主开关+渠道 mode+CEO 放行三重闸门约束。

不 import executors（executors 反向要引用本模块时会循环；writer 步骤在函数内延迟 import，
并通过 writer_fn 参数可注入）。

max_tokens 说明：当前线上配置的是推理型模型（thinking 占同一预算），结构化输出调用
（策划简报/事实提取/评分卡）的 max_tokens 都按"留足推理余量"放大过；若换回非推理模型
可调小以省钱。
"""
import json
import logging
import os
import re
import uuid
from datetime import datetime
from pathlib import Path

import autopublish
import llm_client
from agent_registry import load_agent_registry, load_private_context, log_execution
from paths import SUPER_BRAIN

logger = logging.getLogger("super_brain.content_pipeline")

RUNS_DIR = SUPER_BRAIN / "content_pipeline_runs"
MAX_REVISE_ROUNDS = int(os.environ.get("CONTENT_PIPELINE_MAX_REVISIONS", "2"))
MAX_QUERIES = int(os.environ.get("RESEARCH_MAX_QUERIES", "3"))
MAX_SEARCHES = int(os.environ.get("RESEARCH_MAX_SEARCHES", "3"))

AGENT_RESEARCHER = "researcher"
AGENT_CRITIC = "critic"
AGENT_WRITER = "writer"
AGENT_PLANNER = "publish-planner"

SCORE_DIMS = ["hook", "density", "fact_risk", "platform_fit"]


class PipelineError(Exception):
    pass


def _structured_llm_or_default(llm_call):
    """结构化输出任务默认走主模型；若配置了 ModelStructured（更快的非推理模型）则用
    它——结构化任务不需要长篇思考，能明显省 token 且输出稳定。测试注入的假实现原样保留。"""
    if llm_call is not None:
        return llm_call
    override = llm_client.structured_model_override()
    if override:
        return lambda system, user, api_key, **kw: llm_client.call_deepseek(
            system, user, api_key, model=override, **kw)
    return llm_client.call_deepseek


# ---------- 公共工具 ----------

def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _run_id() -> str:
    return f"cp_{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}"


def _resolve_api_key(api_key: str | None) -> str:
    if api_key:
        return api_key
    try:
        return llm_client.load_deepseek_api_key()
    except llm_client.DeepSeekConfigError as exc:
        raise PipelineError(str(exc)) from exc


def _load_framework(agent_name: str) -> str:
    registry = load_agent_registry()
    context = load_private_context(agent_name, registry)
    if context.startswith("("):
        raise PipelineError(f"agent {agent_name} 没有可用 private.md 框架：{context}")
    return context


def _write_run(record: dict) -> Path:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    path = RUNS_DIR / f"{record['run_id']}.json"
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def persist_research(package: dict) -> Path:
    """把单次信息搜集落盘（executor/inbox 触发时用，独立于整条流水线的 record）。"""
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^\w一-鿿-]", "-", str(package.get("topic") or "research"))[:40].strip("-") or "research"
    path = RUNS_DIR / f"research_{datetime.now():%Y%m%d_%H%M%S}_{slug}.json"
    path.write_text(json.dumps(package, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"信息包已落盘：{path}")
    return path


def persist_critic(note: dict, draft_excerpt: str = "") -> Path:
    """把单次点评意见单落盘（executor/inbox 触发时用）。附一份被评稿件的摘录。"""
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^\w一-鿿-]", "-", str(note.get("title") or "critic"))[:40].strip("-") or "critic"
    path = RUNS_DIR / f"critic_{datetime.now():%Y%m%d_%H%M%S}_{slug}.json"
    path.write_text(json.dumps({"note": note, "draft_excerpt": draft_excerpt[:2000]},
                               ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"意见单已落盘：{path}")
    return path


def _trace(record: dict, agent: str, stage: str, note: str, ref: str = "") -> None:
    record.setdefault("trace", []).append({
        "at": _now(), "agent": agent, "stage": stage, "note": note, "ref": ref,
    })
    logger.info(f"[content_pipeline:{record['run_id']}] {agent} · {stage}：{note}")


# ---------- agent1 researcher ----------

def _extract_urls(raw_text: str) -> set[str]:
    """从检索返回文本里抽 URL（真实 Tavily 的每一行结尾带 （来源：https://…））。"""
    return set(re.findall(r"https?://[^\s）)】>\"']+", raw_text))


def plan_queries(topic: str, api_key: str | None = None, llm_call=None,
                 max_queries: int = MAX_QUERIES) -> list[str]:
    """researcher 第一步：由模型把选题展开成搜索问题清单（默认 ≤3 个）。"""
    llm_call = _structured_llm_or_default(llm_call)
    framework = _load_framework(AGENT_RESEARCHER)
    system_prompt = (
        f"下面是你的知识框架（private.md 原文）：\n\n{framework}\n\n"
        "现在用户给了你一个选题。第一步：只输出为了核实/充实这个选题你需要的搜索问题清单，"
        "每行一条，格式 '- 问题'，不要输出别的任何文字（不要输出正文、不要给结论）。"
    )
    raw = llm_call(system_prompt, f"选题：{topic}", _resolve_api_key(api_key), max_tokens=2000)
    queries = [ln.strip().lstrip("-").strip() for ln in raw.splitlines()
               if ln.strip().startswith("-") and len(ln.strip()) > 2]
    return [q for q in queries[:max_queries] if q] or [f"{topic} 最新情况与关键事实"]


def _parse_facts_from_text(raw: str) -> list[dict]:
    """解析 researcher 的提取结果。约定严格的行格式（比让模型吐 JSON 稳）：
        FACT||事实陈述||来源URL||来源名||high|medium|low
    解析不了的行丢弃并计数，不允许半行数据混进来。"""
    facts = []
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("FACT||"):
            continue
        parts = [p.strip() for p in line.split("||")]
        if len(parts) < 4:
            continue
        confidence = parts[4] if len(parts) > 4 else "low"
        if confidence not in ("high", "medium", "low"):
            confidence = "low"
        facts.append({
            "claim": parts[1],
            "source_url": parts[2],
            "source_name": parts[3],
            "confidence": confidence,
        })
    return facts


def run_research(topic: str, api_key: str | None = None, queries: list[str] | None = None,
                 llm_call=None, search_call=None, search_api_key: str | None = None,
                 use_web: bool | None = None, rag_context: str = "") -> dict:
    """researcher 主流程：plan_queries → 逐条搜索 → 模型提取事实 → 代码层红线校验。

    红线校验规则（硬，不是提示词软约束）：
    - 事实的 source_url 必须出现在本次检索返回的 URL 集合里；允许 local:// 前缀表示
      "本地知识库/素材来源"（web 关闭或素材自带时用）。
    - 违规事实 → dropped（注明原因），不进 facts。"""
    llm_call = _structured_llm_or_default(llm_call)
    search_call = search_call or llm_client.tavily_search
    api_key = _resolve_api_key(api_key)

    if search_api_key is None and use_web is not False:
        search_api_key = llm_client.load_tavily_api_key()
    web_used = bool(use_web if use_web is not None else search_api_key)
    if not web_used and not rag_context:
        logger.warning("researcher：无 Tavily key 且无本地素材上下文，检索将为空")

    queries = queries if queries else plan_queries(topic, api_key, llm_call=llm_call)
    queries = [q for q in queries[:MAX_SEARCHES] if q][:MAX_SEARCHES]

    retrieval = []
    retrieved_urls: set[str] = set()
    for q in queries:
        if web_used:
            try:
                raw = search_call(q, search_api_key, max_results=5)
            except Exception as exc:
                logger.warning(f"researcher：搜索失败 query={q!r}：{exc}")
                raw = f"（搜索失败：{exc}）"
        else:
            raw = rag_context
        retrieval.append({"query": q, "raw": raw})
        retrieved_urls |= _extract_urls(raw)

    material = "\n\n".join(f"--- 检索：{r['query']} ---\n{r['raw']}" for r in retrieval)
    framework = _load_framework(AGENT_RESEARCHER)
    system_prompt = (
        f"下面是你的知识框架（private.md 原文）：\n\n{framework}\n\n"
        "现在把检索结果里可核实的**事实**提取出来。硬性要求：\n"
        "1. source_url 只能来自检索结果里真实出现的 URL（原样抄），或 local:// 开头的本地来源；\n"
        "2. 检索结果里没有的信息不要编造，写不出就少写；\n"
        "3. 每行严格输出一条：FACT||事实陈述||来源URL||来源名||confidence(high/medium/low)；\n"
        "4. 另起一行输出 CONFLICT||冲突说明（有多条来源说法冲突时才输出，没有就省略）；\n"
        "5. 另起一行输出 GAP||想查没查到的内容。\n不要输出任何其它文字。"
    )
    raw_facts = llm_call(system_prompt, material, api_key, max_tokens=12000)

    facts, dropped = [], []
    for fact in _parse_facts_from_text(raw_facts):
        url = (fact.get("source_url") or "").strip()
        if url.startswith("local://") or url in retrieved_urls:
            facts.append(fact)
        else:
            dropped.append({
                "claim": fact.get("claim", ""),
                "reason": f"source_url={url!r} 不在本次检索结果里（疑似编造），红线拦截",
            })
    conflict_notes = [ln.split("||", 1)[1].strip() for ln in raw_facts.splitlines()
                      if ln.startswith("CONFLICT||") and len(ln.split("||", 1)) > 1]
    gaps = [ln.split("||", 1)[1].strip() for ln in raw_facts.splitlines()
            if ln.startswith("GAP||") and len(ln.split("||", 1)) > 1]

    package = {
        "topic": topic,
        "generated_at": _now(),
        "web_used": web_used,
        "queries": queries,
        "retrieval": retrieval,
        "facts": facts,
        "conflict_notes": conflict_notes,
        "coverage_gaps": gaps,
        "dropped": dropped,
    }
    log_execution(AGENT_RESEARCHER, "产出信息包", f"选题={topic}，facts={len(facts)}，dropped={len(dropped)}")
    return package


def materialize_source(package_or_text) -> str:
    """把信息包/素材转成 writer 的输入文本（纯函数，无调用）。"""
    if isinstance(package_or_text, dict):
        lines = [f"# 选题：{package_or_text.get('topic', '')}"]
        for f in package_or_text.get("facts", []):
            lines.append(f"- {f['claim']}（来源：{f['source_url']}，置信度 {f['confidence']}）")
        for c in package_or_text.get("conflict_notes", []):
            lines.append(f"- [冲突待裁决] {c}")
        for g in package_or_text.get("coverage_gaps", []):
            lines.append(f"- [未覆盖] {g}")
        return "\n".join(lines)
    return str(package_or_text)


# ---------- agent2 writer（复用现有链，只做包装） ----------

def _split_title_draft(raw: str) -> tuple[str, str]:
    parts = raw.strip().split("\n", 1)
    title = parts[0].strip().lstrip("#").strip() or "未命名"
    body = parts[1].strip() if len(parts) > 1 else ""
    return title, body


def run_writer_draft(source, api_key: str | None = None, user_instruction: str | None = None,
                     writer_fn=None, title_hint: str = "") -> dict:
    """agent2：把信息包/素材写成平台无关定稿。默认走 executors.generate_writer_draft
    （content-strategist 策划 → writer 成稿，真实调用两次 LLM）；离线测试注入 writer_fn。"""
    content = materialize_source(source)
    if title_hint:
        content = f"标题方向：{title_hint}\n\n{content}"
    if writer_fn is None:
        import executors  # 延迟 import，避免顶层循环依赖
        writer_fn = executors.generate_writer_draft
    api_key = _resolve_api_key(api_key)
    raw = writer_fn(content, api_key, user_instruction=user_instruction)
    title, draft = _split_title_draft(raw)
    log_execution(AGENT_WRITER, "流水线成稿", f"标题={title[:40]}，正文 {len(draft)} 字")
    return {"title": title, "draft": draft}


# ---------- agent3 critic ----------

def _coerce_score(value) -> int:
    """把模型给的分数钳进 1-5：数字越界就钳到边界，不是数字才用默认 3。"""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 3
    return max(1, min(5, n))


def run_critic(draft_id: str, title: str, draft: str, platform: str = "toutiao",
               api_key: str | None = None, llm_call=None) -> dict:
    """agent3：对定稿出评分卡意见单。返回 dict（scores/evidence/must_fix/optional/verdict/
    parse_error）。verdict 归一化为 accept/revise/reject；JSON 解析失败如实标记 parse_error。"""
    llm_call = _structured_llm_or_default(llm_call)
    framework = _load_framework(AGENT_CRITIC)
    system_prompt = (
        f"下面是你的知识框架（private.md 原文）：\n\n{framework}\n\n"
        f"目标平台：{platform}\n"
        "请对下面的稿件输出 JSON（不要其它文字）：\n"
        '{"scores": {"hook": 1-5, "density": 1-5, "fact_risk": 1-5, "platform_fit": 1-5}, '
        '"evidence": {"hook": "...引用原文或位置...", "density": "...", "fact_risk": "...", "platform_fit": "..."}, '
        '"must_fix": ["可执行修改点（指向稿子位置）"], "optional": ["..."], '
        '"verdict": "accept|revise|reject"}'
    )
    raw = llm_call(system_prompt, f"稿件标题：{title}\n\n稿件正文：\n{draft}",
                   _resolve_api_key(api_key), max_tokens=12000)
    note = _parse_review(raw, draft_id, title)
    log_execution(AGENT_CRITIC, "点评稿件", f"draft={draft_id}，verdict={note.get('verdict')}，"
                                           f"scores={note.get('scores')}，parse_error={note.get('parse_error')}")
    return note


def _parse_review(raw: str, draft_id: str, title: str) -> dict:
    note = {
        "draft_id": draft_id, "title": title, "scores": {},
        "evidence": {}, "must_fix": [], "optional": [], "verdict": "revise",
        "parse_error": True, "raw_tail": raw[-300:],
    }
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end <= start:
        return note
    try:
        data = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return note
    scores = data.get("scores") or {}
    note["scores"] = {d: _coerce_score(scores.get(d)) for d in SCORE_DIMS}
    note["evidence"] = {d: str((data.get("evidence") or {}).get(d, ""))[:300] for d in SCORE_DIMS}
    note["must_fix"] = [str(x) for x in (data.get("must_fix") or []) if str(x).strip()][:8]
    note["optional"] = [str(x) for x in (data.get("optional") or []) if str(x).strip()][:8]
    verdict = str(data.get("verdict") or "revise").strip().lower()
    note["verdict"] = verdict if verdict in ("accept", "revise", "reject") else "revise"
    note["parse_error"] = False
    note.pop("raw_tail", None)
    return note


# ---------- 全流程编排（agent1→2→3 迭代 → 交接 agent4） ----------

def run_pipeline(topic: str, source=None, api_key: str | None = None,
                 queries: list[str] | None = None, platform: str = "toutiao",
                 llm_call=None, search_call=None, search_api_key: str | None = None,
                 use_web: bool | None = None, writer_fn=None, critic_fn=None,
                 channels=("wechat", "toutiao"), publish_at: str | None = None,
                 title_hint: str = "", save: bool = True) -> dict:
    """端到端跑一次内容流水线。返回 record（含 trace / research / drafts / reviews / verdict /
    handoff）。source 为 None 时用 topic 自动 research；给 dict/str 则跳过 research 直接用素材。"""
    api_key = _resolve_api_key(api_key)
    record = {
        "run_id": _run_id(), "topic": topic, "platform": platform,
        "created_at": _now(), "trace": [], "verdict": None,
    }

    if source is None:
        research = run_research(topic, api_key, queries=queries, llm_call=llm_call,
                                search_call=search_call, search_api_key=search_api_key,
                                use_web=use_web)
        record["research"] = research
        _trace(record, AGENT_RESEARCHER, "research",
               f"facts={len(research['facts'])}，dropped={len(research['dropped'])}")
        source_material = research
    else:
        record["research"] = None
        source_material = source

    first = run_writer_draft(source_material, api_key, title_hint=title_hint, writer_fn=writer_fn)
    drafts = [first]
    reviews: list[dict] = []
    record["drafts"] = drafts
    verdict = "revise"
    rounds = 0
    while verdict == "revise" and rounds < MAX_REVISE_ROUNDS:
        rounds += 1
        last = drafts[-1]
        review = run_critic(f"draft#{rounds}", last["title"], last["draft"], platform=platform,
                            api_key=api_key, llm_call=llm_call) if critic_fn is None \
                 else critic_fn(last["title"], last["draft"], platform)
        reviews.append(review)
        _trace(record, AGENT_CRITIC, f"critic/r{rounds}",
               f"verdict={review['verdict']} scores={review['scores']} "
               f"parse_error={review.get('parse_error')}")
        verdict = review.get("verdict", "revise")
        if verdict == "accept" or review.get("parse_error"):
            break
        fix_notes = "；".join(review.get("must_fix") or [])[:800]
        revised = run_writer_draft(
            drafts[0]["draft"], api_key,
            user_instruction=f"上一版评论家要求修改：{fix_notes}（只按这些点改，不要整体重写风格）",
            writer_fn=writer_fn,
        )
        drafts.append(revised)
        _trace(record, AGENT_WRITER, f"revise/r{rounds}", f"rounds_used={rounds}")

    record["reviews"] = reviews
    record["rounds"] = rounds
    final = drafts[-1]
    record["verdict"] = verdict if verdict != "revise" else "revise_exhausted"

    order = autopublish.new_order(
        final["title"],
        {"kind": "content_pipeline", "text": final["draft"], "pipeline_run_id": record["run_id"]},
        channels=list(channels), publish_at=publish_at,
        note=f"content_pipeline 产出（platform={platform}，verdict={record['verdict']}）",
    )
    autopublish.save_order(order)
    record["handoff_order_id"] = order["id"]
    _trace(record, AGENT_PLANNER, "handoff", f"发布单 {order['id']} 已入池", ref=order["id"])

    if save:
        path = _write_run(record)
        record["run_path"] = str(path)
    log_execution("content-pipeline", "端到端跑通",
                  f"run={record['run_id']}，verdict={record['verdict']}，轮次={rounds}，发布单={order['id']}")
    return record


# ---------- 命令行入口（真实调用由用户自己决定何时跑） ----------

def main() -> None:
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        print("\n用法：")
        print("  python content_pipeline.py research <选题>            # 只跑 agent1，落盘信息包")
        print("  python content_pipeline.py run <素材文件或选题>        # 端到端（会用真实 DeepSeek）")
        print("  python content_pipeline.py run --topic <选题>         # 先联网 research 再跑全流程")
        print("\n⚠ 真实调用会花 DeepSeek/Tavily 额度，先想清楚再跑。离线测试请用 pytest。")
        return

    import json as _json

    mode = args[0]
    if mode == "research":
        topic = args[1] if len(args) > 1 else "（空选题）"
        package = run_research(topic)
        print(_json.dumps(package, ensure_ascii=False, indent=2))
        return
    if mode == "run":
        rest = args[1:]
        topic_arg = None
        source = None
        if rest and rest[0] == "--topic":
            topic_arg = rest[1] if len(rest) > 1 else ""
            record = run_pipeline(topic_arg, source=None)
        elif rest:
            path = Path(rest[0])
            source = path.read_text(encoding="utf-8-sig") if path.exists() else rest[0]
            record = run_pipeline(topic_arg or "素材驱动", source=source)
        else:
            print("run 需要 --topic <选题> 或一个素材文件路径/文本")
            return
        print(_json.dumps(record, ensure_ascii=False, indent=2, default=str))
        return
    print(f"未知命令：{mode}（用 --help 看用法）")


if __name__ == "__main__":
    main()
