"""
super_brain roundtable - 圆桌讨论的真实执行引擎

Python 后台直接调 DeepSeek API 唤起多个圆桌专家，不依赖 Claude Code 的 `Agent` 工具——
两者本质上是同一件事（N 次互相独立的 LLM 调用），这里用线程池做真并行，不是伪并行循环。

两轮协议，跟 2026-08-14 用 Agent 工具跑通、验证过确实能挖出真实分歧的那次完全一致：
1. Round 1：每个专家只拿到问题 + 自己的 private.md，互相看不见，真独立分析，给出明确倾向判断
2. Round 2：把全部专家 Round 1 的结论都亮给每一个专家，只让他们做一件事——挑风险/冲突，
   没有异议就明说，不重新分析原问题
3. 汇总：调用 writer agent 的知识框架，把两轮原始记录写成正式会议纪要，分歧不隐藏、不调和，
   落盘到 MEETING_MINUTES_DIR（config.json 里的持久化设置）

诚实的局限：这里默认用 DeepSeek（复用 dispatcher.py 已配好的 Key，成本低）。2026-08-14 那次
真正验证"圆桌讨论确实能挖出真实分歧"的实验用的是 Claude（通过 Agent 工具）——没有直接证据
证明 DeepSeek 在这个任务上效果一样好，这是一个待验证的假设，不是已经证实的结论。

用法：
    python roundtable.py "要不要涨价" strategy finance marketing
"""
import concurrent.futures
import json
import logging
import re
import sys
from datetime import datetime
from pathlib import Path

from dispatcher import (
    AGENTS_DIR,
    DEEPSEEK_CONFIG_PATH,
    SUPER_BRAIN,
    call_deepseek,
    call_deepseek_with_tools,
    load_agent_registry,
    load_private_context,
    load_tavily_api_key,
)
from functools import partial
from log_setup import configure_logging

logger = logging.getLogger("super_brain.roundtable")

CONFIG_PATH = SUPER_BRAIN / "config.json"
ROUNDTABLE_LOG_DIR = SUPER_BRAIN / "roundtable_log"  # 老的扁平存储，只用于一次性迁移
CONVERSATIONS_DIR = SUPER_BRAIN / "conversations"


class RoundtableError(Exception):
    pass


def _round1_system_prompt(agent_name: str, entry: dict, private_context: str,
                           web_search_enabled: bool, prior_turns: list[dict] | None = None) -> str:
    parts = [
        f"你是 super_brain 决策圆桌里的 '{agent_name}' 专家。角色定位：{entry.get('description', '')}",
        f"下面是你的专属知识框架（private.md 原文），分析必须显式引用其中的具体条目：\n\n{private_context}",
    ]
    sources = entry.get("sources") or []
    if sources:
        parts.append("知识框架来源（可审计）：\n" + "\n".join(f"- {s}" for s in sources))
    if entry.get("output_requires_citation"):
        parts.append("硬性要求：每条结论标注依据第几条规则，覆盖不到的问题明说'现有框架未覆盖，以下是推测'。")
    if entry.get("disclaimer_required"):
        parts.append("硬性要求：任何具体建议之后必须附一句——这不是正式专业意见，具体问题建议咨询执业人士。")
    if web_search_enabled:
        parts.append(
            "你有一个 web_search 工具——只在需要真实、最新、具体的外部事实（市场数据、竞品"
            "动态、法规原文）且你的知识框架里没有覆盖时才调用，不要用来查你已经知道的常识，"
            "更不要用它替代你自己知识框架里的判断规则。"
        )
    if prior_turns:
        history_text = "\n\n".join(
            f"[第 {i + 1} 轮] 问题：{t['question']}\n你当时的判断：" +
            (t.get("round1", {}).get(agent_name) or "（这一轮你没有参与）")
            for i, t in enumerate(prior_turns)
        )
        parts.append(
            f"这是同一个会话里的追问，之前已经讨论过：\n\n{history_text}\n\n"
            "请在这次分析时参考上面的历史脉络，保持连贯——但如果这次问题跟之前的讨论无关，"
            "不需要强行关联，独立判断优先。"
        )
    parts.append(
        "这是第一轮，你看不到其他专家这次的意见，独立给出你的分析，给一个明确的倾向性判断"
        "（做/不做/有条件地做），不要模棱两可，控制在 400 字以内。"
    )
    return "\n\n".join(parts)


def _round2_system_prompt(agent_name: str, entry: dict, private_context: str,
                           question: str, round1_results: dict[str, str]) -> str:
    others = "\n\n".join(
        f"【{name}】\n{text}" for name, text in round1_results.items() if name != agent_name
    )
    return "\n\n".join([
        f"你是 super_brain 决策圆桌里的 '{agent_name}' 专家。你在第一轮已经独立分析过问题："
        f"{question!r}，你当时的结论：\n{round1_results.get(agent_name, '')}",
        f"下面是你的专属知识框架：\n\n{private_context}",
        f"现在是第二轮——其他专家的独立分析都摆出来了：\n\n{others}",
        "只做一件事：从你的专业角度指出这些结论里有没有风险或冲突，如果没有异议就明确说"
        "'没有异议'。不要重新分析原问题，只做交叉校验，控制在 300 字以内。",
    ])


def _round3_system_prompt(agent_name: str, private_context: str, question: str,
                           round1_results: dict[str, str], round2_results: dict[str, str]) -> str:
    return "\n\n".join([
        f"你是 super_brain 决策圆桌里的 '{agent_name}' 专家。你刚参与完一场关于 {question!r} 的两轮讨论。",
        f"你的专属知识框架：\n\n{private_context}",
        f"你在第一轮的判断：\n{round1_results.get(agent_name, '')}",
        f"你在第二轮的交叉校验：\n{round2_results.get(agent_name, '')}",
        "现在做一次简短的自我复盘：这次运用你已有知识框架里的规则，判断依据是否清晰、"
        "有没有被交叉校验环节点出盲点、有没有触发'现有框架未覆盖'。\n"
        "硬性边界：只能反思你已有规则维度运用得够不够准确/完整，不能提议任何全新的分析领域"
        "或超出你现有专业范围的能力——如果发现某类场景反复出现却没有对应规则，只需如实指出"
        "'这类场景缺少判断标准'，不要越界建议其他领域的内容。\n"
        "控制在 150 字以内，不写场面话，没什么可反思的就直接说'本次判断依据充分，无需补充'。",
    ])


def append_lessons(agent_name: str, question: str, reflection: str) -> None:
    """自我反思结果追加进这个专家的经验教训文件——只追加不覆盖，供以后的定期复盘（机制 2）
    整体读取分析用。不直接改 private.md，那是需要人工审核的严肃操作，这里只做记录。
    """
    lessons_path = AGENTS_DIR / agent_name / "lessons.md"
    lessons_path.parent.mkdir(parents=True, exist_ok=True)
    if not lessons_path.exists():
        lessons_path.write_text(
            f"# {agent_name} 经验教训\n\n每次圆桌讨论后自动追加的自我反思，仅供以后定期复盘"
            f"参考，不会自动合并进 private.md——是否采纳、怎么改规则，需要人工审核决定。\n\n",
            encoding="utf-8",
        )
    now = datetime.now()
    entry = f"## {now:%Y-%m-%d %H:%M} · 问题：{question}\n{reflection.strip()}\n\n"
    with lessons_path.open("a", encoding="utf-8") as f:
        f.write(entry)
    logger.info(f"{agent_name} 的自我反思已追加：{lessons_path}")


def _run_round(agent_names: list[str], registry: dict, contexts: dict[str, str], api_key: str,
               build_system_prompt, user_prompt: str, round_label: str, call_fn=call_deepseek) -> dict[str, str]:
    logger.info(f"{round_label}：并行唤起 {len(agent_names)} 位专家")
    results: dict[str, str] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(agent_names)) as pool:
        futures = {
            pool.submit(
                call_fn,
                build_system_prompt(name),
                user_prompt,
                api_key,
                max_tokens=8000,
            ): name
            for name in agent_names
        }
        for future in concurrent.futures.as_completed(futures):
            name = futures[future]
            try:
                results[name] = future.result()
                logger.info(f"{round_label} 完成：{name}")
            except Exception:
                logger.exception(f"{round_label} 失败：{name}，这位专家本轮缺席")
                results[name] = "(本轮调用失败，这位专家缺席)"
    return results


def write_meeting_minutes(question: str, agent_names: list[str],
                           round1: dict[str, str], round2: dict[str, str], api_key: str) -> Path | None:
    """调 writer 的知识框架，把两轮原始记录写成正式会议纪要，落盘到持久化配置的目录。
    目录没配置就跳过落盘（不臆造路径），只把原始记录返回，不算失败。
    """
    if not CONFIG_PATH.exists():
        logger.warning("没有 config.json，跳过写会议纪要（MEETING_MINUTES_DIR 未配置）")
        return None
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    minutes_dir = config.get("MEETING_MINUTES_DIR")
    if not minutes_dir:
        logger.warning("config.json 里没有 MEETING_MINUTES_DIR，跳过写会议纪要——去 UI 的"
                        "\"首次配置\"区块设置一次")
        return None

    registry = load_agent_registry()
    writer_context = load_private_context("writer", registry)

    round1_text = "\n\n".join(f"【{n}】\n{t}" for n, t in round1.items())
    round2_text = "\n\n".join(f"【{n}】\n{t}" for n, t in round2.items())

    system_prompt = (
        f"下面是你的写作技能知识框架（private.md 原文）：\n\n{writer_context}\n\n"
        f"讨论的问题：{question}\n参与专家：{', '.join(agent_names)}\n\n"
        f"Round 1（独立分析，互不可见）原始记录：\n{round1_text}\n\n"
        f"Round 2（交叉校验）原始记录：\n{round2_text}\n\n"
        "请严格按你的写作技能框架（尤其是运营导向的取舍原则、具体场景优先、避免自证式空话"
        "这几条），把以上原始记录提炼成一份正式文档——不是有闻必录的决策记录，要主动做取舍。"
        "第一行输出一个简短的中文标题（不要任何前缀符号）。"
    )
    logger.info("调用 writer 生成正式会议纪要")
    try:
        raw = call_deepseek(system_prompt, "请生成会议纪要。", api_key, max_tokens=10000)
    except Exception:
        logger.exception("writer 生成会议纪要失败，原始记录仍然保留在返回值里，只是没有落盘")
        return None

    lines = raw.strip().split("\n", 1)
    title = lines[0].strip().lstrip("#").strip() or "会议纪要"
    slug = re.sub(r"[^\w一-鿿-]", "-", title)[:40].strip("-") or "untitled"

    now = datetime.now()
    filename = f"{now:%Y-%m-%d}_{now:%H%M}_{slug}.md"
    out_dir = Path(minutes_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / filename
    out_path.write_text(raw, encoding="utf-8")
    logger.info(f"会议纪要已落盘：{out_path}")
    return out_path


def _conversation_path(conversation_id: str) -> Path:
    return CONVERSATIONS_DIR / f"{conversation_id}.json"


def _new_conversation_id(question: str) -> str:
    now = datetime.now()
    slug = re.sub(r"[^\w一-鿿-]", "-", question)[:30].strip("-") or "untitled"
    return f"{now:%Y-%m-%d_%H%M%S}_{slug}"


def create_conversation(agent_names: list[str], question: str) -> str:
    """新建一个会话——一个会话是一个持续的话题，里面可以有多轮追问，不再是每次提问都
    生成一个互相无关的独立记录。"""
    CONVERSATIONS_DIR.mkdir(parents=True, exist_ok=True)
    conversation_id = _new_conversation_id(question)
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    data = {
        "id": conversation_id,
        "title": question[:40],
        "agents": agent_names,
        "created_at": now,
        "updated_at": now,
        "turns": [],
    }
    _conversation_path(conversation_id).write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info(f"新建会话：{conversation_id}")
    return conversation_id


def load_conversation(conversation_id: str) -> dict | None:
    path = _conversation_path(conversation_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError):
        logger.warning(f"会话读取失败：{path}")
        return None


def append_turn(conversation_id: str, turn: dict) -> None:
    data = load_conversation(conversation_id)
    if data is None:
        raise RoundtableError(f"会话不存在，没法追加轮次：{conversation_id}")
    data["turns"].append(turn)
    data["updated_at"] = turn.get("timestamp", datetime.now().strftime("%Y-%m-%d %H:%M"))
    _conversation_path(conversation_id).write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def delete_conversation(conversation_id: str) -> bool:
    path = _conversation_path(conversation_id)
    if not path.exists():
        return False
    path.unlink()
    logger.info(f"会话已删除：{conversation_id}")
    return True


def _migrate_legacy_roundtable_log() -> None:
    """一次性迁移——老的 roundtable_log/*.json（一次讨论一个文件，互相没有会话归属）转成
    会话结构，每个老文件变成一个只有 1 轮的独立会话，不丢历史数据。用一个 .migrated 标记
    文件防止重复迁移。
    """
    if not ROUNDTABLE_LOG_DIR.exists():
        return
    migrated_marker = ROUNDTABLE_LOG_DIR / ".migrated"
    if migrated_marker.exists():
        return
    CONVERSATIONS_DIR.mkdir(parents=True, exist_ok=True)
    count = 0
    for path in sorted(ROUNDTABLE_LOG_DIR.glob("*.json")):
        try:
            entry = json.loads(path.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, OSError):
            continue
        conversation_id = path.stem
        if _conversation_path(conversation_id).exists():
            continue
        turn = {
            "question": entry.get("question", ""),
            "agents": entry.get("agents", []),
            "round1": entry.get("round1", {}),
            "round2": entry.get("round2", {}),
            "minutes_path": entry.get("minutes_path"),
            "timestamp": entry.get("timestamp", ""),
        }
        data = {
            "id": conversation_id,
            "title": entry.get("question", "")[:40],
            "agents": entry.get("agents", []),
            "created_at": entry.get("timestamp", ""),
            "updated_at": entry.get("timestamp", ""),
            "turns": [turn],
        }
        _conversation_path(conversation_id).write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        count += 1
    migrated_marker.write_text(
        f"迁移完成：{count} 条老记录转成独立会话，{datetime.now():%Y-%m-%d %H:%M}\n", encoding="utf-8"
    )
    logger.info(f"roundtable_log 迁移完成：{count} 条老记录 -> conversations/")


def load_all_conversations() -> list[dict]:
    """按最后更新时间倒序返回所有会话（最新的在前，适合列表展示）。"""
    _migrate_legacy_roundtable_log()
    if not CONVERSATIONS_DIR.exists():
        return []
    conversations = []
    for path in CONVERSATIONS_DIR.glob("*.json"):
        try:
            conversations.append(json.loads(path.read_text(encoding="utf-8-sig")))
        except (json.JSONDecodeError, OSError):
            logger.warning(f"会话读取失败，跳过：{path}")
    conversations.sort(key=lambda c: c.get("updated_at", ""), reverse=True)
    return conversations


def run_roundtable(agent_names: list[str], question: str, conversation_id: str | None = None) -> dict:
    """conversation_id 为 None 时新建一个会话；传入已有会话 id 时，是在这个会话里追问——
    追问会带上这个会话之前的历史脉络（见 _round1_system_prompt 的 prior_turns），不是
    每次提问都从零开始、互相无关的独立记录。
    """
    registry = load_agent_registry()

    invalid = [n for n in agent_names if registry.get(n, {}).get("type") != "roundtable"]
    if invalid:
        raise RoundtableError(f"这些不是 roundtable 类型的 agent，不能进圆桌：{invalid}")
    if len(agent_names) < 2:
        raise RoundtableError("圆桌至少需要 2 位专家，一个人自己跟自己交叉校验没有意义")

    if not DEEPSEEK_CONFIG_PATH.exists():
        raise RoundtableError(f"找不到 DeepSeek 配置（{DEEPSEEK_CONFIG_PATH}）")
    api_key = json.loads(DEEPSEEK_CONFIG_PATH.read_text(encoding="utf-8-sig"))["DEEPSEEK_API_KEY"]

    prior_turns: list[dict] = []
    if conversation_id:
        existing = load_conversation(conversation_id)
        if existing is None:
            raise RoundtableError(f"会话不存在：{conversation_id}")
        prior_turns = existing.get("turns", [])
    else:
        conversation_id = create_conversation(agent_names, question)

    logger.info(f"===== 圆桌讨论启动：{question!r}，专家：{agent_names}，会话：{conversation_id} =====")
    contexts = {name: load_private_context(name, registry) for name in agent_names}

    tavily_api_key = load_tavily_api_key()
    web_search_enabled = bool(tavily_api_key)
    logger.info(f"web_search 工具：{'已启用（Tavily key 已配置）' if web_search_enabled else '未启用（没配置 TAVILY_API_KEY，自动降级）'}")

    round1 = _run_round(
        agent_names, registry, contexts, api_key,
        build_system_prompt=lambda name: _round1_system_prompt(
            name, registry[name], contexts[name], web_search_enabled, prior_turns
        ),
        user_prompt=question,
        round_label="Round 1",
        call_fn=partial(call_deepseek_with_tools, tavily_api_key=tavily_api_key),
    )
    round2 = _run_round(
        agent_names, registry, contexts, api_key,
        build_system_prompt=lambda name: _round2_system_prompt(
            name, registry[name], contexts[name], question, round1
        ),
        user_prompt="请给出交叉校验意见。",
        round_label="Round 2",
    )

    minutes_path = write_meeting_minutes(question, agent_names, round1, round2, api_key)

    round3 = _run_round(
        agent_names, registry, contexts, api_key,
        build_system_prompt=lambda name: _round3_system_prompt(
            name, contexts[name], question, round1, round2
        ),
        user_prompt="请做自我复盘。",
        round_label="Round 3（自我反思）",
    )
    for name, reflection in round3.items():
        try:
            append_lessons(name, question, reflection)
        except OSError:
            logger.exception(f"{name} 的自我反思写入 lessons.md 失败，不影响本次讨论结果")

    logger.info("===== 圆桌讨论结束 =====")
    turn = {
        "question": question,
        "agents": agent_names,
        "round1": round1,
        "round2": round2,
        "minutes_path": str(minutes_path) if minutes_path else None,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    append_turn(conversation_id, turn)
    return {**turn, "conversation_id": conversation_id}


if __name__ == "__main__":
    configure_logging()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    if len(sys.argv) < 3:
        print('用法：python roundtable.py "问题内容" agent1 agent2 [agent3 ...]')
        sys.exit(1)

    result = run_roundtable(sys.argv[2:], sys.argv[1])
    print(json.dumps(result, ensure_ascii=False, indent=2))
