"""
super_brain agent_registry - "有哪些合法 agent、各自的知识框架是什么"这一件事

从 dispatcher.py 拆出来的——这一块管的是 agent 的元信息（agents.yaml 是唯一权威来源，
目录 agents/<name>/ 存在不代表已注册），跟 inbox 调度、DeepSeek 调用是两个不同的关注点。
"""
import logging
from datetime import datetime
from pathlib import Path

import yaml

from paths import AGENTS_CONFIG_PATH, AGENTS_DIR, SUPER_BRAIN

logger = logging.getLogger("super_brain.agent_registry")


def load_agent_registry() -> dict[str, dict]:
    """从 agents.yaml 读取 agent 注册表，按 name 建索引。

    这是"合法 agent 有哪些"的唯一权威来源——目录 agents/<name>/ 存在不代表已注册，
    必须在 agents.yaml 里登记过才算数（避免有人手滑建了个目录就被当成真实 agent）。
    """
    if not AGENTS_CONFIG_PATH.exists():
        logger.error(f"找不到 {AGENTS_CONFIG_PATH}，没有它就不知道有哪些合法 agent，直接退出。")
        return {}
    try:
        config = yaml.safe_load(AGENTS_CONFIG_PATH.read_text(encoding="utf-8-sig"))
    except yaml.YAMLError as exc:
        logger.error(f"{AGENTS_CONFIG_PATH} 不是合法 YAML，解析失败：{exc}")
        return {}

    registry = {}
    for entry in (config or {}).get("agents", []):
        name = entry.get("name")
        if not name:
            logger.warning(f"agents.yaml 里有一条记录没有 name 字段，已跳过：{entry}")
            continue
        registry[name] = entry
    logger.info(f"agents.yaml 加载完成，注册了 {len(registry)} 个 agent：{sorted(registry.keys())}")
    return registry


def known_agent_names(registry: dict[str, dict]) -> set[str]:
    return set(registry.keys()) | {"All"}


def load_private_context(agent_name: str, registry: dict[str, dict]) -> str:
    entry = registry.get(agent_name)
    knowledge_path = entry.get("knowledge_path") if entry else None
    if not knowledge_path:
        return "(agents.yaml 里没有为这个 agent 登记 knowledge_path，没有专属上下文)"
    private_file = SUPER_BRAIN / knowledge_path
    if private_file.exists():
        return private_file.read_text(encoding="utf-8-sig")
    return f"(agents.yaml 里登记的 knowledge_path 指向 {private_file}，但文件不存在)"


def log_execution(agent_name: str, action: str, detail: str, status: str = "ok") -> None:
    """执行类 agent（ops-assistant/toutiao/ship/writer/video-prompt）的 lessons.md 记录。

    跟 roundtable.append_lessons() 不是一回事，刻意不共用——那边记的是 LLM 生成的自我
    反思（"这次判断依据够不够"），这里记的是纯事实（做了什么、结果如何），不额外调用
    DeepSeek。原因见 agents.yaml 顶部的设计原则：执行类 agent 的动作是确定性的、机械的，
    没有"判断依据"可反思，硬要生成反思文字只是在浪费一次调用换一段正确的废话。真正有用
    的是留下事实记录，供机制 2（定期复盘）以后用规则或者 LLM 去归纳"这类任务经常在哪失败"。
    """
    lessons_path = AGENTS_DIR / agent_name / "lessons.md"
    lessons_path.parent.mkdir(parents=True, exist_ok=True)
    if not lessons_path.exists():
        lessons_path.write_text(
            f"# {agent_name} 执行记录\n\n每次真实执行后追加的事实记录（不是 LLM 生成的反思——"
            f"执行类 agent 的动作是确定性的，没有'判断依据'可反思，只记录做了什么、结果如何）。"
            f"仅供以后定期复盘（机制 2）参考，不会自动改 private.md。\n\n",
            encoding="utf-8",
        )
    now = datetime.now()
    status_tag = "✓" if status == "ok" else "✗"
    entry = f"## {now:%Y-%m-%d %H:%M} · {action} [{status_tag} {status}]\n{detail.strip()}\n\n"
    with lessons_path.open("a", encoding="utf-8") as f:
        f.write(entry)
    logger.info(f"{agent_name} 执行记录已追加：{lessons_path}")


def log_artifact_feedback(agent_name: str, task_description: str, artifact_path: str | None, feedback: str) -> None:
    """CEO 对已生成产物的负面反馈——跟 log_execution() 的"纯事实记录"不是一回事：那边记的是
    "做了什么、成不成功"，这里记的是"做出来的东西好不好"，是真正有判断价值的人类信号。
    刻意写进同一个 lessons.md（不单开文件）——机制 2 的 generate_review_suggestion() 会
    整篇读这个文件，不需要额外改它就能自动吃到这类反馈，只要格式上用一个显眼的标题区分开。

    产物如果是本地文件（writer/toutiao 生成的草稿），顺手把内容片段也记进去——只有反馈
    文字、没有对照的产物内容，复盘时没法判断问题出在哪，这个片段就是那个对照。
    """
    lessons_path = AGENTS_DIR / agent_name / "lessons.md"
    lessons_path.parent.mkdir(parents=True, exist_ok=True)
    if not lessons_path.exists():
        lessons_path.write_text(
            f"# {agent_name} 执行记录\n\n每次真实执行后追加的事实记录，以及 CEO 对产物质量的"
            f"人工反馈。事实记录不是 LLM 生成的反思；人工反馈是真正的判断信号，机制 2 定期"
            f"复盘应该优先看这部分。仅供以后定期复盘参考，不会自动改 private.md。\n\n",
            encoding="utf-8",
        )

    artifact_excerpt = ""
    if artifact_path and not artifact_path.startswith("video-prompt-conversation:"):
        artifact_file = Path(artifact_path)
        if artifact_file.exists():
            try:
                text = artifact_file.read_text(encoding="utf-8-sig")
                snippet = text[:800] + ("...(截断)" if len(text) > 800 else "")
                artifact_excerpt = f"\n\n产物内容片段：\n{snippet}"
            except OSError:
                logger.warning(f"反馈记录时读取产物文件失败，跳过内容片段：{artifact_file}")

    now = datetime.now()
    entry = (
        f"## {now:%Y-%m-%d %H:%M} · 人工反馈：产物质量问题\n"
        f"任务：{task_description}\n"
        f"CEO 反馈：{feedback.strip()}"
        f"{artifact_excerpt}\n\n"
    )
    with lessons_path.open("a", encoding="utf-8") as f:
        f.write(entry)
    logger.info(f"{agent_name} 的产物质量反馈已追加：{lessons_path}")


def build_system_prompt(agent_name: str, registry: dict[str, dict], private_context: str) -> str:
    """按 agents.yaml 里的元信息（type/sources/output_requires_citation/disclaimer_required）
    拼装 system prompt，而不是每种 agent 手写一份——这是"统一格式"真正发挥作用的地方。

    这是给 inbox+dispatcher 那条"没有 executor，只起草建议"的老路径用的，供 dispatcher.py
    的 main() 调用。
    """
    entry = registry.get(agent_name, {})
    agent_type = entry.get("type", "unknown")
    description = entry.get("description", "")

    parts = [
        f"你是 super_brain 多 agent 协作体系里名叫 '{agent_name}' 的角色（类型：{agent_type}）。",
        f"角色定位：{description}" if description else "",
        f"下面是你的专属上下文（private.md 原文）：\n\n{private_context}",
    ]

    if agent_type == "roundtable":
        sources = entry.get("sources") or []
        if sources:
            parts.append("你的知识框架来源（可审计，回答时可以引用）：\n" + "\n".join(f"- {s}" for s in sources))
        if entry.get("output_requires_citation"):
            parts.append(
                "硬性要求：用自然口语把建议讲清楚，不要写成'依据#X，……'这种编号领起的条款体——"
                "每条建议讲完后，在句尾用简短括号标注依据私有上下文里的第几条规则（如'……"
                "（依据 #3）'）。覆盖不到的问题要明说'现有框架未覆盖，以下是推测'。"
            )
        if entry.get("disclaimer_required"):
            parts.append("硬性要求：任何具体、可执行的建议之后，必须附一句提醒——"
                          "这不是正式专业意见，具体问题建议咨询相应领域的执业人士确认。")

    parts.append(
        "现在收到了若干条待处理留言。请判断接下来该做什么，给出简短、具体的行动建议。"
        "注意：你只是在起草建议，不是真的在执行——不要假装已经做了什么，"
        "输出格式：先一句话总结判断，再列出具体建议步骤。"
    )
    final_prompt = "\n\n".join(p for p in parts if p)
    logger.debug(f"build_system_prompt({agent_name}) 拼装完成：\n{final_prompt}")
    return final_prompt
