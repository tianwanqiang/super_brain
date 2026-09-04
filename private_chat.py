"""
super_brain private_chat - CEO 跟单个圆桌专家的隔离私聊

跟圆桌讨论里的 "@ 单独提问"（roundtable.ask_agent_mention）不是一回事：那个刻意不隔离，
回复留在圆桌讨论的正式记录里；这里刻意隔离——CEO 想追着一个专家深挖，又不想让这些追问
弄乱圆桌讨论本身的记录，所以每次私聊都是独立的会话文件，互相之间、跟源头圆桌讨论之间都
不回写。

结构上直接照抄 video_prompt.py 的隔离会话模式（同一个问题的解法，没必要另造一套）：
一个会话一个 JSON 文件，存在 private_chat_conversations/{id}.json，多轮对话是标准的
chat messages 数组。跟 video_prompt.py 的区别只在于"种子上下文"——私聊创建时会把源头
圆桌讨论里这位专家自己的 Round 1/2 发言 + 原始问题，压成一段种子上下文存进会话，
每次调用都会带上，但只在创建时算一次，不会每轮追问都重新读一次源头会话。
"""
import json
import logging
import re
from datetime import datetime
from pathlib import Path

from agent_registry import load_agent_registry, load_private_context, log_execution
from llm_client import DeepSeekConfigError, call_deepseek_messages, load_deepseek_api_key
from paths import SUPER_BRAIN

logger = logging.getLogger("super_brain.private_chat")

CONVERSATIONS_DIR = SUPER_BRAIN / "private_chat_conversations"


class PrivateChatError(Exception):
    pass


def _conversation_path(conversation_id: str) -> Path:
    return CONVERSATIONS_DIR / f"{conversation_id}.json"


def _build_seed_context(agent_name: str, source_turn: dict) -> str:
    """从源头圆桌讨论的某一轮里，抽出这位专家自己的发言 + 原始问题，压成一段种子上下文。
    只带这位专家自己说过的话，不夹带其他专家的发言——避免私聊一开始就被别人的观点污染。
    """
    question = source_turn.get("question", "")
    round1 = (source_turn.get("round1") or {}).get(agent_name, "（这一轮没有参与）")
    round2 = (source_turn.get("round2") or {}).get(agent_name, "（这一轮没有参与）")
    return (
        f"这场私聊是从一次圆桌讨论里跳出来的追问。原始问题：{question!r}\n"
        f"你当时（Round 1 独立分析）的判断：\n{round1}\n\n"
        f"你当时（Round 2 交叉校验）的意见：\n{round2}"
    )


def create_conversation(agent_name: str, source_conversation_id: str, source_turn: dict,
                         first_message: str) -> str:
    registry = load_agent_registry()
    if registry.get(agent_name, {}).get("type") != "roundtable":
        raise PrivateChatError(f"'{agent_name}' 不是 roundtable 类型的专家，不能私聊")

    CONVERSATIONS_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    slug = re.sub(r"[^\w一-鿿-]", "-", first_message)[:30].strip("-") or "untitled"
    conversation_id = f"{now:%Y-%m-%d_%H%M%S}_{agent_name}_{slug}"
    data = {
        "id": conversation_id,
        "agent": agent_name,
        "title": first_message[:40],
        "source_conversation_id": source_conversation_id,
        "seed_context": _build_seed_context(agent_name, source_turn),
        "created_at": now.strftime("%Y-%m-%d %H:%M"),
        "updated_at": now.strftime("%Y-%m-%d %H:%M"),
        "messages": [],
    }
    _conversation_path(conversation_id).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"新建私聊会话：{conversation_id}（专家={agent_name}，来源会话={source_conversation_id}）")
    return conversation_id


def load_conversation(conversation_id: str) -> dict | None:
    path = _conversation_path(conversation_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError):
        logger.warning(f"私聊会话读取失败：{path}")
        return None


def load_all_conversations() -> list[dict]:
    if not CONVERSATIONS_DIR.exists():
        return []
    conversations = []
    for path in CONVERSATIONS_DIR.glob("*.json"):
        try:
            conversations.append(json.loads(path.read_text(encoding="utf-8-sig")))
        except (json.JSONDecodeError, OSError):
            logger.warning(f"私聊会话读取失败，跳过：{path}")
    conversations.sort(key=lambda c: c.get("updated_at", ""), reverse=True)
    return conversations


def delete_conversation(conversation_id: str) -> bool:
    path = _conversation_path(conversation_id)
    if not path.exists():
        return False
    path.unlink()
    logger.info(f"私聊会话已删除：{conversation_id}")
    return True


def send_message(conversation_id: str, user_message: str) -> dict:
    data = load_conversation(conversation_id)
    if data is None:
        raise PrivateChatError(f"私聊会话不存在：{conversation_id}")

    agent_name = data["agent"]
    try:
        api_key = load_deepseek_api_key()
    except DeepSeekConfigError as exc:
        raise PrivateChatError(str(exc)) from exc

    registry = load_agent_registry()
    entry = registry.get(agent_name, {})
    private_context = load_private_context(agent_name, registry)
    system_prompt = (
        f"你是 super_brain 决策圆桌里的 '{agent_name}' 专家。角色定位：{entry.get('description', '')}\n\n"
        f"下面是你的专属知识框架（private.md 原文）：\n\n{private_context}\n\n"
        f"{data['seed_context']}\n\n"
        "CEO 现在跳出圆桌讨论，单独找你深聊——这是一场隔离的私聊，不会回流进原来的圆桌"
        "讨论记录，你可以更自由地展开、被追问细节，不用像圆桌那样控制在几百字以内。"
    )

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    data["messages"].append({"role": "user", "content": user_message, "timestamp": now})

    messages = [{"role": "system", "content": system_prompt}] + [
        {"role": m["role"], "content": m["content"]} for m in data["messages"]
    ]
    try:
        reply = call_deepseek_messages(messages, api_key)
    except Exception:
        logger.exception(f"私聊回复失败：{conversation_id}")
        raise

    now2 = datetime.now().strftime("%Y-%m-%d %H:%M")
    data["messages"].append({"role": "assistant", "content": reply, "timestamp": now2})
    data["updated_at"] = now2
    _conversation_path(conversation_id).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"私聊会话已更新：{conversation_id}")
    log_execution(agent_name, "私聊回复", f"会话：{conversation_id}，问题：{user_message[:60]}")
    return data
