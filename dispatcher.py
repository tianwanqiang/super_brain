"""
super_brain dispatcher - v3（瘦身版）

只做一件事：调度——扫描 inbox.md 里 Status: pending 的留言，按 To 字段分组，
分发给对应 agent 处理。DeepSeek/Tavily 调用逻辑在 llm_client.py，agent 注册表/
private.md 管理在 agent_registry.py，真实执行器业务逻辑在 executors.py——
这里不再混杂这些内容，只剩"调度"本身。

两种处理方式，按 agents.yaml 里有没有 `executor` 字段区分：
1. **有 executor**（目前是 toutiao / ops-assistant）：真的调用 executors.py 里对应的函数
   落地动作——生成头条草稿、建公众号草稿。只做到"草稿"，绝不调用群发/发布接口。成功后会把
   inbox 里对应的留言自动标记成 done（这类动作已经有明确、安全的边界，不需要每次都等人工
   确认才敢标记完成）。
2. **没有 executor**（ship、roundtable 类型等）：加载 private.md 作为专属上下文，调用
   DeepSeek 起草"接下来该做什么"的建议，写进 dispatch_log/，不自动执行、不自动改 inbox
   状态。这类 agent 涉及的动作（git push、真正的专家分析）还没有对应的真实执行器。

新增一个 agent：在 agents.yaml 里加一条记录 + 建对应的 agents/<name>/private.md；如果还想要
真实执行能力，额外在 executors.py 里写一个函数、在 EXECUTORS 里注册。

用法：
    python G:\\code\\super_brain\\dispatcher.py --dry-run   # 只解析+校验+打印，不花钱，先跑这个
    python G:\\code\\super_brain\\dispatcher.py             # 真的执行/调用，会消耗额度或产生真实草稿
"""
import json
import logging
import re
import sys
from datetime import datetime

from agent_registry import build_system_prompt, load_agent_registry, load_private_context, known_agent_names
from executors import EXECUTORS
from llm_client import call_deepseek
from log_setup import configure_logging
from paths import DEEPSEEK_CONFIG_PATH, DISPATCH_LOG_DIR, INBOX

logger = logging.getLogger("super_brain.dispatcher")

VALID_STATUS_VALUES = {"pending", "done"}
MESSAGE_LOG_HEADING = "## 留言记录"


def parse_pending_messages(inbox_text: str, registry: dict[str, dict]) -> list[dict]:
    """按 --- 分块解析 inbox.md 里 `## 留言记录` 标题之后的内容，只返回校验通过、且 Status: pending 的留言。

    硬约束（见 inbox.md 的"使用约定"）：
    - 只解析 `## 留言记录` 标题之后的内容——标题之前的"使用约定"部分即使包含格式示例
      （示例本身也用 `---` 分隔），也不会被误当成真留言解析。
    - 每条留言用独占一行的 `---` 分隔，Message 内容本身不能包含这样一行。
    - Message 必须单行——多行内容从第二行起会被当成格式错误丢弃并警告，不会静默吞掉。
    - Status 精确匹配小写 pending/done，其他大小写变体会被警告并跳过。
    - To 必须是 agents.yaml 里登记过的 agent name，或字面量 All，否则警告并跳过
      （避免为打错字/未注册的孤儿留言白花一次 DeepSeek 调用）。
    """
    if MESSAGE_LOG_HEADING in inbox_text:
        inbox_text = inbox_text.split(MESSAGE_LOG_HEADING, 1)[1]
    else:
        logger.warning(f"inbox.md 里没找到 {MESSAGE_LOG_HEADING!r} 这个标题，"
                        f"没法区分'使用约定'和真实留言，本次不解析任何留言。")
        return []

    entries = []
    valid_agents = known_agent_names(registry)
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
            logger.warning(f"{label} 的留言疑似多行或包含独占一行的 '---'，"
                            f"整条留言已跳过、不会处理（硬约束：Message 必须单行）：{unrecognized_lines}")
            continue

        status = entry.get("status", "")
        if status not in VALID_STATUS_VALUES:
            logger.warning(f"{label} 的 Status 值 {status!r} 不是合法的 pending/done（大小写敏感），已跳过。")
            continue
        if status != "pending":
            continue

        to = entry.get("to", "")
        if to not in valid_agents:
            logger.warning(f"{label} 的 To 字段 {to!r} 不匹配任何已知 agent（{sorted(valid_agents)}），"
                            f"已跳过，不会为这条孤儿留言调用 DeepSeek。")
            continue

        logger.debug(f"{label} 通过校验，加入待处理队列")
        entries.append(entry)
    logger.info(f"inbox 解析完成，{len(entries)} 条留言通过校验、进入待处理队列")
    return entries


def mark_message_done(entry: dict) -> None:
    """把 inbox.md 里跟 entry 完全匹配（From/To/Time/Message 全部一致）的那条留言状态改成 done。
    只在真实执行成功后调用——避免匹配到内容恰好相同的另一条留言，用全部字段做精确匹配。
    """
    text = INBOX.read_text(encoding="utf-8-sig")
    old_block = (
        f"From: {entry.get('from', '')}\n"
        f"To: {entry.get('to', '')}\n"
        f"Time: {entry.get('time', '')}\n"
        f"Status: pending\n"
        f"Message: {entry.get('message', '')}"
    )
    new_block = old_block.replace("Status: pending", "Status: done", 1)
    if old_block not in text:
        logger.warning(f"没能在 inbox.md 里精确匹配到这条留言，状态未自动更新：{entry}")
        return
    INBOX.write_text(text.replace(old_block, new_block, 1), encoding="utf-8")
    logger.info(f"inbox.md 已写回：留言标记为 done -> {entry.get('to')} @ {entry.get('time')}")


def main():
    configure_logging()

    # Windows 控制台默认代码页往往不是 UTF-8，直接 print 中文会乱码——强制 stdout 用 UTF-8。
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    dry_run = "--dry-run" in sys.argv
    logger.info(f"===== dispatcher 启动（{'dry-run' if dry_run else '真实执行'}） =====")

    registry = load_agent_registry()
    if not registry:
        logger.error("registry 为空，终止本次运行。")
        return

    if not INBOX.exists():
        logger.error(f"找不到 {INBOX}，没什么可调度的。")
        return

    pending = parse_pending_messages(INBOX.read_text(encoding="utf-8-sig"), registry)
    if not pending:
        logger.info("inbox 里没有校验通过的 pending 留言，没什么可调度的。")
        return

    by_agent: dict[str, list[dict]] = {}
    for e in pending:
        by_agent.setdefault(e["to"], []).append(e)
    logger.info(f"待处理留言按 agent 分组：{ {k: len(v) for k, v in by_agent.items()} }")

    api_key = None
    if not dry_run:
        if not DEEPSEEK_CONFIG_PATH.exists():
            logger.error(f"找不到 DeepSeek 配置（{DEEPSEEK_CONFIG_PATH}），无法起草建议，仅列出待处理留言：")
            for e in pending:
                logger.info(f"  [{e['to']}] {e.get('from', '?')} @ {e.get('time', '?')}: {e.get('message', '')}")
            return
        api_key = json.loads(DEEPSEEK_CONFIG_PATH.read_text(encoding="utf-8-sig"))["DEEPSEEK_API_KEY"]
        DISPATCH_LOG_DIR.mkdir(exist_ok=True)

    for agent, messages in by_agent.items():
        executor_key = registry.get(agent, {}).get("executor")
        logger.debug(f"[{agent}] executor 字段：{executor_key!r}")

        # ---- 有 executor：真实执行，不再走"起草建议"那一套 ----
        if executor_key:
            executor_fn = EXECUTORS.get(executor_key)
            _now = datetime.now()
            today = f"{_now.month}_{_now.day}"  # opc 文件用的日期格式：{月}_{日}，不带前导零

            if dry_run:
                logger.info(f"[{agent}] {len(messages)} 条待处理留言 -> 会真实调用 executor "
                            f"'{executor_key}'（日期参数：{today}），dry-run 不会真的执行。")
                continue

            if not executor_fn:
                logger.warning(f"[{agent}] 在 agents.yaml 里登记的 executor '{executor_key}' "
                                f"在 EXECUTORS 里找不到对应函数，跳过，不会误当成起草模式处理。"
                                f"（已知 executor：{sorted(EXECUTORS.keys())}）——这种情况通常是"
                                f"agents.yaml 和 executors.py 的 EXECUTORS 字典没同步更新，是需要修的不一致。")
                continue

            logger.info(f"[{agent}] {len(messages)} 条待处理留言，真实执行 '{executor_key}'（日期：{today}）...")
            try:
                results = executor_fn(today, api_key)
            except Exception:
                logger.exception(f"[{agent}] executor '{executor_key}' 执行时抛出未捕获异常，"
                                  f"完整堆栈见上——这类异常说明 publishers.py 里少了一层针对性的错误处理，"
                                  f"应该补一个具体的 except 分支，而不是让它一路冒到这里。")
                continue

            for key, value in results.items():
                level = logger.warning if key.endswith(("_error", "_skipped")) else logger.info
                level(f"  -> {key}: {value}")

            # 只有真实执行成功（没有任何 *_error 字段）才自动标记 done，
            # 部分失败时留着 pending，方便人工看 dispatch 输出后决定要不要重跑。
            if not any(k.endswith("_error") for k in results):
                for m in messages:
                    mark_message_done(m)
                logger.info(f"  -> inbox 里这 {len(messages)} 条留言已标记为 done")
            else:
                logger.warning("  -> 有步骤失败，留言状态保持 pending，未自动标记 done")
            continue

        # ---- 没有 executor：保持 v1 行为，只起草建议 ----
        logger.info(f"[{agent}] {len(messages)} 条待处理留言"
                     f"{'（dry-run，不会真的调用）' if dry_run else '，起草建议中...'}")
        private_context = load_private_context(agent, registry)
        messages_text = "\n".join(
            f"- ({m.get('from', '?')} @ {m.get('time', '?')}) {m.get('message', '')}"
            for m in messages
        )
        system_prompt = build_system_prompt(agent, registry, private_context)

        if dry_run:
            logger.info("  --- 将会发送的 system prompt ---")
            logger.info("  " + system_prompt.replace("\n", "\n  "))
            logger.info("  --- 将会发送的 user prompt（留言内容）---")
            logger.info("  " + messages_text.replace("\n", "\n  "))
            continue

        try:
            suggestion = call_deepseek(system_prompt, messages_text, api_key)
        except Exception as exc:
            logger.exception(f"[{agent}] 调用 DeepSeek 起草建议失败")
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
        logger.info(f"  -> 已写入 {log_file}")

    logger.info("===== dispatcher 本次运行结束 =====")


if __name__ == "__main__":
    main()
