"""
super_brain tasks - 项目进度管理的轻量存储

一条 task 对应圆桌讨论 Round 3（决策收敛）产出的一个可执行步骤。不是通用项目管理工具，
只服务于"圆桌结论 -> 可追踪的执行状态"这一件事。存成单个 tasks.yaml，不是数据库——
量级是一人公司场景，YAML 人可读、可以手动改、方便审计。

状态机（一律从 pending_confirmation 起步——Round 3 只负责生成结论和产物草稿，
不代表 CEO 已经批准，"停在产物已生成、等你确认"是明确定过的边界）：
    pending_confirmation -> confirmed -> in_progress -> done
                          -> rejected（CEO 否掉这条）
"""
import logging
from datetime import datetime

import yaml

from paths import SUPER_BRAIN

logger = logging.getLogger("super_brain.tasks")

TASKS_PATH = SUPER_BRAIN / "tasks.yaml"

STATUS_VALUES = {"pending_confirmation", "confirmed", "in_progress", "done", "rejected"}


def load_tasks() -> list[dict]:
    if not TASKS_PATH.exists():
        return []
    data = yaml.safe_load(TASKS_PATH.read_text(encoding="utf-8-sig")) or {}
    return data.get("tasks") or []


def save_tasks(tasks: list[dict]) -> None:
    TASKS_PATH.write_text(
        yaml.safe_dump({"tasks": tasks}, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def add_tasks_from_decision(conversation_id: str, question: str, decision: dict) -> list[dict]:
    """decision 是 Round 3 综合出的结构化结论（见 roundtable._round3_synthesis_prompt 的
    JSON 约定），把里面的 steps 落地成任务条目。不做去重/合并——同一个会话多轮追问，
    每轮的结论都各自落地成新任务，任务本身通过 conversation_id 可以追溯到源头讨论。
    """
    tasks = load_tasks()
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    existing_ids = {t["id"] for t in tasks}
    new_tasks = []
    for i, step in enumerate(decision.get("steps") or []):
        task_id = f"{conversation_id}_{i + 1}"
        suffix = 1
        while task_id in existing_ids:
            suffix += 1
            task_id = f"{conversation_id}_{i + 1}_{suffix}"
        task = {
            "id": task_id,
            "conversation_id": conversation_id,
            "question": question,
            "decision": decision.get("decision", ""),
            "description": (step.get("description") or "").strip(),
            "assignee_agent": step.get("assignee_agent") or None,
            "artifact_path": None,
            "status": "pending_confirmation",
            "created_at": now,
            "updated_at": now,
        }
        existing_ids.add(task_id)
        new_tasks.append(task)
    if new_tasks:
        tasks.extend(new_tasks)
        save_tasks(tasks)
        logger.info(f"新增 {len(new_tasks)} 条任务，来自会话 {conversation_id}")
    return new_tasks


def tasks_for_conversation(conversation_id: str) -> list[dict]:
    return [t for t in load_tasks() if t.get("conversation_id") == conversation_id]


def update_task_status(task_id: str, status: str, artifact_path: str | None = None) -> bool:
    if status not in STATUS_VALUES:
        raise ValueError(f"非法状态：{status}（合法值：{sorted(STATUS_VALUES)}）")
    tasks = load_tasks()
    for task in tasks:
        if task["id"] == task_id:
            task["status"] = status
            task["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
            if artifact_path is not None:
                task["artifact_path"] = artifact_path
            save_tasks(tasks)
            logger.info(f"任务 {task_id} 状态更新为 {status}")
            return True
    logger.warning(f"任务不存在，更新状态失败：{task_id}")
    return False


def pending_tasks() -> list[dict]:
    """给"今日待确认"这类主动触发/汇总场景用——只要还没被 CEO 确认或否掉的任务。"""
    return [t for t in load_tasks() if t.get("status") == "pending_confirmation"]
