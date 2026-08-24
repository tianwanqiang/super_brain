"""
super_brain review - 机制 2：定期复盘

机制 1（roundtable.append_lessons / agent_registry.log_execution）只负责攒素材，
从来不会自己改 private.md——这是设计上刻意留的口子：知识框架被谁改、怎么改，必须经过
人工审核，不能让 LLM 自己悄悄改自己的判断标准。

机制 2 就是把机制 1 攒的素材真正利用起来：读一个 agent 的 lessons.md（圆桌型）或
执行记录（执行型）+ 现有 private.md，生成一份"要不要调整判断规则"的建议。建议只是
建议——落盘成独立文件，不直接改 private.md；真正合并进 private.md 需要 CEO 在页面上
点"采纳"，而且只会追加（在 private.md 末尾加一个带日期的小节），不会覆写/删除已有内容，
保留可回溯的修改历史。
"""
import logging
import re
from datetime import datetime
from pathlib import Path

from agent_registry import load_agent_registry, load_private_context
from llm_client import call_deepseek
from paths import AGENTS_DIR

logger = logging.getLogger("super_brain.review")


class ReviewError(Exception):
    pass


def _reviews_dir(agent_name: str) -> Path:
    return AGENTS_DIR / agent_name / "reviews"


def _lessons_path(agent_name: str) -> Path:
    return AGENTS_DIR / agent_name / "lessons.md"


def has_lessons(agent_name: str) -> bool:
    path = _lessons_path(agent_name)
    return path.exists() and path.read_text(encoding="utf-8-sig").strip()


def list_reviews(agent_name: str) -> list[dict]:
    reviews_dir = _reviews_dir(agent_name)
    if not reviews_dir.exists():
        return []
    out = []
    for path in sorted(reviews_dir.glob("*.md"), reverse=True):
        out.append({
            "path": str(path),
            "filename": path.name,
            "applied": path.name.startswith("applied_"),
        })
    return out


def generate_review_suggestion(agent_name: str, api_key: str) -> Path:
    """读这个 agent 的 lessons.md（不存在就报错——没有素材复盘什么）+ 现有 private.md，
    生成一份结构化的调整建议，落盘成独立文件，不碰 private.md 本身。
    """
    registry = load_agent_registry()
    if agent_name not in registry:
        raise ReviewError(f"'{agent_name}' 不是已注册的 agent")

    lessons_path = _lessons_path(agent_name)
    if not lessons_path.exists() or not lessons_path.read_text(encoding="utf-8-sig").strip():
        raise ReviewError(f"'{agent_name}' 还没有 lessons.md 记录，没有素材可以复盘")

    lessons_text = lessons_path.read_text(encoding="utf-8-sig")
    private_context = load_private_context(agent_name, registry)
    agent_type = registry[agent_name].get("type")

    if agent_type == "roundtable":
        source_note = "下面的记录是每次圆桌讨论后 LLM 生成的自我反思（判断依据是否清晰、有没有被交叉校验点出盲点）。"
    else:
        source_note = (
            "下面的记录有两类：大部分是每次真实执行后的事实记录（做了什么、成功还是失败），"
            "不是 LLM 生成的反思；标题里带'人工反馈：产物质量问题'的条目是 CEO 对生成产物的"
            "真实不满，是这里面价值最高的信号——这类条目通常还附带了产物内容片段，可以直接"
            "对照 CEO 的反馈和实际产出，判断问题具体出在 private.md 里哪条规则模糊或缺失。"
            "优先围绕这类条目分析，事实记录只用来判断'这类问题是不是经常发生'这种频率问题。"
        )

    system_prompt = (
        f"你在给 super_brain 里的 '{agent_name}' agent 做定期复盘。{source_note}\n\n"
        f"这个 agent 现有的知识框架（private.md 原文）：\n\n{private_context}\n\n"
        f"这个 agent 累积的记录：\n\n{lessons_text}\n\n"
        "请分析这些记录，判断现有知识框架是否需要调整。只在有真实证据支撑时才提出建议——"
        "如果记录显示现有框架运用得很好、没有反复出现的盲点，直接说'现有框架运作良好，"
        "本次复盘不建议改动'，不要为了有产出而牵强提出建议。\n\n"
        "如果确实发现问题，按这个结构输出：\n"
        "## 发现的模式\n（具体是什么反复出现的情况，引用记录里的证据，不要泛泛而谈）\n\n"
        "## 建议的调整\n（具体建议往 private.md 里加一条什么规则/删除或修改哪条规则，"
        "给出可以直接使用的条目文字，不要只说方向）\n\n"
        "## 置信度\n（这个建议基于多少条独立证据，证据不足要如实说，不要显得比实际更确定）"
    )
    suggestion = call_deepseek(system_prompt, "请生成定期复盘建议。", api_key, max_tokens=3000)

    reviews_dir = _reviews_dir(agent_name)
    reviews_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    out_path = reviews_dir / f"{now:%Y-%m-%d_%H%M}.md"
    out_path.write_text(
        f"# {agent_name} 定期复盘建议 · {now:%Y-%m-%d %H:%M}\n\n{suggestion}\n", encoding="utf-8",
    )
    logger.info(f"{agent_name} 的定期复盘建议已生成：{out_path}")
    return out_path


def apply_review(agent_name: str, review_path: str) -> None:
    """CEO 点"采纳"——把建议追加进 private.md 末尾（带日期的小节，不覆写/删除已有内容），
    然后把建议文件重命名成 applied_ 前缀，标记这条建议已经处理过，避免重复采纳。
    """
    path = Path(review_path)
    if not path.exists():
        raise ReviewError(f"复盘建议文件不存在：{review_path}")
    if path.name.startswith("applied_"):
        raise ReviewError("这条建议已经采纳过了，不能重复采纳")

    registry = load_agent_registry()
    entry = registry.get(agent_name)
    if entry is None:
        raise ReviewError(f"'{agent_name}' 不是已注册的 agent")
    knowledge_path = entry.get("knowledge_path")
    if not knowledge_path:
        raise ReviewError(f"'{agent_name}' 在 agents.yaml 里没有登记 knowledge_path")

    from paths import SUPER_BRAIN
    private_path = SUPER_BRAIN / knowledge_path
    if not private_path.exists():
        raise ReviewError(f"private.md 不存在：{private_path}")

    suggestion_text = path.read_text(encoding="utf-8-sig")
    now = datetime.now()
    addition = f"\n\n## 定期复盘建议 · 采纳于 {now:%Y-%m-%d %H:%M}\n\n{suggestion_text}\n"
    with private_path.open("a", encoding="utf-8") as f:
        f.write(addition)

    applied_path = path.with_name(f"applied_{path.name}")
    path.rename(applied_path)
    logger.info(f"{agent_name} 的复盘建议已采纳，追加进 {private_path}，建议文件重命名为 {applied_path.name}")
