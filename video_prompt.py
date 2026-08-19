"""
super_brain video_prompt - AIGC 提示词专家的多轮对话执行引擎

跟 roundtable.py 的圆桌讨论完全不同的结构——这里只有一个 agent（video-prompt），是
标准的单 agent 多轮对话（用户消息 -> 助手回复 -> 用户追问 -> ...），不需要 Round 1/2/3
那套独立分析+交叉校验协议，也不需要多路并行。会话存储成
video_prompt_conversations/{id}.json，格式类似标准的 chat messages 数组。
"""
import json
import logging
import re
from datetime import datetime
from pathlib import Path

from dispatcher import (
    DEEPSEEK_CONFIG_PATH,
    SUPER_BRAIN,
    call_deepseek_messages,
    load_agent_registry,
    load_private_context,
)

logger = logging.getLogger("super_brain.video_prompt")

CONVERSATIONS_DIR = SUPER_BRAIN / "video_prompt_conversations"


class VideoPromptError(Exception):
    pass


def _conversation_path(conversation_id: str) -> Path:
    return CONVERSATIONS_DIR / f"{conversation_id}.json"


def create_conversation(first_message: str) -> str:
    CONVERSATIONS_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    slug = re.sub(r"[^\w一-鿿-]", "-", first_message)[:30].strip("-") or "untitled"
    conversation_id = f"{now:%Y-%m-%d_%H%M%S}_{slug}"
    data = {
        "id": conversation_id,
        "title": first_message[:40],
        "created_at": now.strftime("%Y-%m-%d %H:%M"),
        "updated_at": now.strftime("%Y-%m-%d %H:%M"),
        "messages": [],
    }
    _conversation_path(conversation_id).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"新建 video-prompt 会话：{conversation_id}")
    return conversation_id


def load_conversation(conversation_id: str) -> dict | None:
    path = _conversation_path(conversation_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError):
        logger.warning(f"video-prompt 会话读取失败：{path}")
        return None


def load_all_conversations() -> list[dict]:
    if not CONVERSATIONS_DIR.exists():
        return []
    conversations = []
    for path in CONVERSATIONS_DIR.glob("*.json"):
        try:
            conversations.append(json.loads(path.read_text(encoding="utf-8-sig")))
        except (json.JSONDecodeError, OSError):
            logger.warning(f"video-prompt 会话读取失败，跳过：{path}")
    conversations.sort(key=lambda c: c.get("updated_at", ""), reverse=True)
    return conversations


def delete_conversation(conversation_id: str) -> bool:
    path = _conversation_path(conversation_id)
    if not path.exists():
        return False
    path.unlink()
    logger.info(f"video-prompt 会话已删除：{conversation_id}")
    return True


def send_message(conversation_id: str, user_message: str) -> dict:
    """在指定会话里追加一条用户消息，调用 DeepSeek 生成回复，追加进会话历史并落盘。
    完整对话历史都会传给模型，这样"运镜感不够强，再改改"这类追问才能基于之前生成的
    版本做针对性调整，而不是每次都从零生成一份无关的新提示词。
    """
    data = load_conversation(conversation_id)
    if data is None:
        raise VideoPromptError(f"会话不存在：{conversation_id}")

    if not DEEPSEEK_CONFIG_PATH.exists():
        raise VideoPromptError(f"找不到 DeepSeek 配置（{DEEPSEEK_CONFIG_PATH}）")
    api_key = json.loads(DEEPSEEK_CONFIG_PATH.read_text(encoding="utf-8-sig"))["DEEPSEEK_API_KEY"]

    registry = load_agent_registry()
    system_context = load_private_context("video-prompt", registry)
    system_prompt = (
        f"下面是你的专业知识框架（private.md 原文）：\n\n{system_context}\n\n"
        "这是一个可以多轮追问、迭代修改提示词的对话——如果用户是在要求修改之前生成的提示词"
        "（比如'运镜感不够强'、'再加点转场'），基于对话历史里已经给出的版本做针对性调整，"
        "不要每次都重新生成一份无关的新提示词。"
    )

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    data["messages"].append({"role": "user", "content": user_message, "timestamp": now})

    messages = [{"role": "system", "content": system_prompt}] + [
        {"role": m["role"], "content": m["content"]} for m in data["messages"]
    ]
    try:
        reply = call_deepseek_messages(messages, api_key)
    except Exception:
        logger.exception("video-prompt 生成回复失败")
        raise

    now2 = datetime.now().strftime("%Y-%m-%d %H:%M")
    data["messages"].append({"role": "assistant", "content": reply, "timestamp": now2})
    data["updated_at"] = now2
    _conversation_path(conversation_id).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"video-prompt 会话已更新：{conversation_id}")
    return data
