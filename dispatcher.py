"""
super_brain dispatcher - v1

真正的调度程序：扫描 inbox.md 里 Status: pending 的留言，按 To 字段分组给对应 agent，
加载该 agent 的 private.md 作为专属上下文，调用 DeepSeek 起草"接下来该做什么"的建议，
写进 dispatch_log/ 里。

明确的边界（v1 只做到这里，不做更多）：
- 不会真的去执行任何操作（不推代码、不建公众号草稿）——只起草建议，供人工或 Claude Code
  会话看了之后自己决定要不要照做、怎么做。真正有后果的动作，还是需要一个有工具调用能力的
  agent（Claude Code 本身）去落地，纯 Python 脚本没有 git/MCP 工具的调用权限。
- 不会自动把留言标记成 done——状态变更代表"这件事真的处理完了"，脚本只是起草建议，
  不代表处理完了，所以状态由人工/Claude Code 会话确认后再手动改。

用法：
    python G:\\code\\super_brain\\dispatcher.py --dry-run   # 只解析+校验+打印，不花钱，先跑这个
    python G:\\code\\super_brain\\dispatcher.py             # 真的调用 DeepSeek，会消耗额度
"""
import json
import re
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

SUPER_BRAIN = Path(r"G:\code\super_brain")
INBOX = SUPER_BRAIN / "inbox.md"
AGENTS_DIR = SUPER_BRAIN / "agents"
DISPATCH_LOG_DIR = SUPER_BRAIN / "dispatch_log"

# 复用 toutiao-agent 已经配好的 DeepSeek Key，避免重复要用户再配一份。
# 如果以后想让 dispatcher 独立于 toutiao-agent，把这里改成 super_brain 自己的 config.json。
DEEPSEEK_CONFIG_PATH = Path(r"G:\code\toutiao-agent\config.json")


VALID_STATUS_VALUES = {"pending", "done"}


def known_agent_names() -> set[str]:
    if not AGENTS_DIR.exists():
        return {"All"}
    return {p.name for p in AGENTS_DIR.iterdir() if p.is_dir()} | {"All"}


MESSAGE_LOG_HEADING = "## 留言记录"


def parse_pending_messages(inbox_text: str) -> list[dict]:
    """按 --- 分块解析 inbox.md 里 `## 留言记录` 标题之后的内容，只返回校验通过、且 Status: pending 的留言。

    硬约束（见 inbox.md 的"使用约定"）：
    - 只解析 `## 留言记录` 标题之后的内容——标题之前的"使用约定"部分即使包含格式示例
      （示例本身也用 `---` 分隔），也不会被误当成真留言解析。
    - 每条留言用独占一行的 `---` 分隔，Message 内容本身不能包含这样一行。
    - Message 必须单行——多行内容从第二行起会被当成格式错误丢弃并警告，不会静默吞掉。
    - Status 精确匹配小写 pending/done，其他大小写变体会被警告并跳过。
    - To 必须是 agents\\ 下真实存在的角色目录名，或字面量 All，否则警告并跳过
      （避免为打错字的孤儿留言白花一次 DeepSeek 调用）。
    """
    if MESSAGE_LOG_HEADING in inbox_text:
        inbox_text = inbox_text.split(MESSAGE_LOG_HEADING, 1)[1]
    else:
        print(f"警告：inbox.md 里没找到 {MESSAGE_LOG_HEADING!r} 这个标题，"
              f"没法区分'使用约定'和真实留言，本次不解析任何留言。")
        return []

    entries = []
    valid_agents = known_agent_names()
    for raw_block in inbox_text.split("\n---\n"):
        block = raw_block.strip()
        if "From:" not in block or "To:" not in block:
            continue

        entry: dict[str, str] = {}
        unrecognized_lines = []
        for line in block.splitlines():
            m = re.match(r"^(From|To|Time|Status|Message):\s*(.*)$", line.strip())
            if m:
                entry[m.group(1).lower()] = m.group(2).strip()
            elif line.strip():
                unrecognized_lines.append(line)

        label = f"[{entry.get('from', '?')} -> {entry.get('to', '?')} @ {entry.get('time', '?')}]"

        if unrecognized_lines:
            print(f"警告：{label} 的留言疑似多行或包含独占一行的 '---'，"
                  f"整条留言已跳过、不会处理（硬约束：Message 必须单行）：{unrecognized_lines}")
            continue

        status = entry.get("status", "")
        if status not in VALID_STATUS_VALUES:
            print(f"警告：{label} 的 Status 值 {status!r} 不是合法的 pending/done（大小写敏感），已跳过。")
            continue
        if status != "pending":
            continue

        to = entry.get("to", "")
        if to not in valid_agents:
            print(f"警告：{label} 的 To 字段 {to!r} 不匹配任何已知 agent（{sorted(valid_agents)}），"
                  f"已跳过，不会为这条孤儿留言调用 DeepSeek。")
            continue

        entries.append(entry)
    return entries


def load_private_context(agent_name: str) -> str:
    private_file = AGENTS_DIR / agent_name / "private.md"
    if private_file.exists():
        return private_file.read_text(encoding="utf-8-sig")
    return "(这个 agent 还没有 private.md，没有专属上下文)"


def call_deepseek(system_prompt: str, user_prompt: str, api_key: str,
                   model: str = "deepseek-chat", base_url: str = "https://api.deepseek.com/v1") -> str:
    body = json.dumps({
        "model": model,
        "max_tokens": 1500,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=body,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"].strip()


def main():
    # Windows 控制台默认代码页往往不是 UTF-8，直接 print 中文会乱码——强制 stdout 用 UTF-8。
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    dry_run = "--dry-run" in sys.argv
    if dry_run:
        print("=== --dry-run 模式：只解析+校验+打印，不会真的调用 DeepSeek ===\n")

    if not INBOX.exists():
        print(f"找不到 {INBOX}，没什么可调度的。")
        return

    pending = parse_pending_messages(INBOX.read_text(encoding="utf-8-sig"))
    if not pending:
        print("inbox 里没有校验通过的 pending 留言，没什么可调度的。")
        return

    by_agent: dict[str, list[dict]] = {}
    for e in pending:
        by_agent.setdefault(e["to"], []).append(e)

    api_key = None
    if not dry_run:
        if not DEEPSEEK_CONFIG_PATH.exists():
            print(f"找不到 DeepSeek 配置（{DEEPSEEK_CONFIG_PATH}），无法起草建议，仅打印待处理留言：")
            for e in pending:
                print(f"  [{e['to']}] {e.get('from', '?')} @ {e.get('time', '?')}: {e.get('message', '')}")
            return
        api_key = json.loads(DEEPSEEK_CONFIG_PATH.read_text(encoding="utf-8-sig"))["DEEPSEEK_API_KEY"]
        DISPATCH_LOG_DIR.mkdir(exist_ok=True)

    for agent, messages in by_agent.items():
        print(f"[{agent}] {len(messages)} 条待处理留言{'（dry-run，不会真的调用）' if dry_run else '，起草建议中...'}")
        private_context = load_private_context(agent)
        messages_text = "\n".join(
            f"- ({m.get('from', '?')} @ {m.get('time', '?')}) {m.get('message', '')}"
            for m in messages
        )
        system_prompt = (
            f"你是 super_brain 多 agent 协作体系里名叫 '{agent}' 的角色。"
            f"下面是你的专属上下文（private.md 原文）：\n\n{private_context}\n\n"
            "现在收到了若干条待处理留言。请判断接下来该做什么，给出简短、具体的行动建议。"
            "注意：你只是在起草建议，不是真的在执行——不要假装已经做了什么，"
            "输出格式：先一句话总结判断，再列出具体建议步骤。"
        )

        if dry_run:
            print("  --- 将会发送的 system prompt ---")
            print("  " + system_prompt.replace("\n", "\n  "))
            print("  --- 将会发送的 user prompt（留言内容）---")
            print("  " + messages_text.replace("\n", "\n  "))
            print()
            continue

        try:
            suggestion = call_deepseek(system_prompt, messages_text, api_key)
        except Exception as exc:
            suggestion = f"(DeepSeek 调用失败：{exc})"

        log_file = DISPATCH_LOG_DIR / f"{agent}_{datetime.now():%Y%m%d_%H%M%S}.md"
        log_file.write_text(
            "\n".join([
                f"# {agent} 待处理建议",
                "",
                "## 收到的留言",
                messages_text,
                "",
                "## AI 起草的行动建议（未执行，需人工/Claude Code 会话确认后落地）",
                suggestion,
                "",
            ]),
            encoding="utf-8",
        )
        print(f"  -> 已写入 {log_file}")


if __name__ == "__main__":
    main()
