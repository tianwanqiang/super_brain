"""
super_brain dispatcher - v2

真正的调度程序：从 agents.yaml 读取"合法 agent 有哪些"（统一注册表，不再靠扫描目录），
扫描 inbox.md 里 Status: pending 的留言，按 To 字段分组给对应 agent。

两种处理方式，按 agents.yaml 里有没有 `executor` 字段区分：
1. **有 executor**（目前是 toutiao / ops-assistant）：真的调用 publishers.py 里对应的函数
   落地动作——生成头条草稿、建公众号草稿。只做到"草稿"，绝不调用群发/发布接口。成功后会把
   inbox 里对应的留言自动标记成 done（这类动作已经有明确、安全的边界，不需要每次都等人工
   确认才敢标记完成）。
2. **没有 executor**（ship、roundtable 类型等）：保持 v1 的行为——加载 private.md 作为专属
   上下文，调用 DeepSeek 起草"接下来该做什么"的建议，写进 dispatch_log/，不自动执行、不自动
   改 inbox 状态。这类 agent 涉及的动作（git push、真正的专家分析）还没有对应的真实执行器。

新增一个 agent：在 agents.yaml 里加一条记录 + 建对应的 agents/<name>/private.md；如果还想要
真实执行能力，额外在 publishers.py 里写一个函数、在 EXECUTORS 里注册。

用法：
    python G:\\code\\super_brain\\dispatcher.py --dry-run   # 只解析+校验+打印，不花钱，先跑这个
    python G:\\code\\super_brain\\dispatcher.py             # 真的执行/调用，会消耗额度或产生真实草稿
"""
import json
import logging
import re
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

import yaml

import publishers
from log_setup import configure_logging

SUPER_BRAIN = Path(r"G:\code\super_brain")
INBOX = SUPER_BRAIN / "inbox.md"
AGENTS_DIR = SUPER_BRAIN / "agents"
AGENTS_CONFIG_PATH = SUPER_BRAIN / "agents.yaml"
DISPATCH_LOG_DIR = SUPER_BRAIN / "dispatch_log"
OPC_ROOT = Path(r"G:\code")

# 复用 toutiao-agent 已经配好的 DeepSeek Key，避免重复要用户再配一份。
# 如果以后想让 dispatcher 独立于 toutiao-agent，把这里改成 super_brain 自己的 config.json。
DEEPSEEK_CONFIG_PATH = Path(r"G:\code\toutiao-agent\config.json")

logger = logging.getLogger("super_brain.dispatcher")

VALID_STATUS_VALUES = {"pending", "done"}


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


def load_private_context(agent_name: str, registry: dict[str, dict]) -> str:
    entry = registry.get(agent_name)
    knowledge_path = entry.get("knowledge_path") if entry else None
    if not knowledge_path:
        return "(agents.yaml 里没有为这个 agent 登记 knowledge_path，没有专属上下文)"
    private_file = SUPER_BRAIN / knowledge_path
    if private_file.exists():
        return private_file.read_text(encoding="utf-8-sig")
    return f"(agents.yaml 里登记的 knowledge_path 指向 {private_file}，但文件不存在)"


def build_system_prompt(agent_name: str, registry: dict[str, dict], private_context: str) -> str:
    """按 agents.yaml 里的元信息（type/sources/output_requires_citation/disclaimer_required）
    拼装 system prompt，而不是每种 agent 手写一份——这是"统一格式"真正发挥作用的地方。
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
            parts.append("硬性要求：每条结论必须标注依据私有上下文里的第几条规则，"
                          "覆盖不到的问题要明说'现有框架未覆盖，以下是推测'。")
        if entry.get("disclaimer_required"):
            parts.append("硬性要求：任何具体、可执行的建议之后，必须附一句提醒——"
                          "这不是正式专业意见，具体问题建议咨询相应领域的执业人士确认。")

    parts.append(
        "现在收到了若干条待处理留言。请判断接下来该做什么，给出简短、具体的行动建议。"
        "注意：你只是在起草建议，不是真的在执行——不要假装已经做了什么，"
        "输出格式：先一句话总结判断，再列出具体建议步骤。"
    )
    return "\n\n".join(p for p in parts if p)


def call_deepseek(system_prompt: str, user_prompt: str, api_key: str,
                   model: str = "deepseek-chat", base_url: str = "https://api.deepseek.com/v1",
                   max_tokens: int = 1500) -> str:
    body = json.dumps({
        "model": model,
        "max_tokens": max_tokens,
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


# ---------- 真实执行器（有 executor 字段的 agent 走这里，不再只是起草建议） ----------

def read_opc_content(date: str) -> str | None:
    opc_path = OPC_ROOT / f"opc_{date}.md"
    if not opc_path.exists():
        return None
    return opc_path.read_text(encoding="utf-8-sig")


def generate_wechat_html(opc_content: str, api_key: str) -> tuple[str, str]:
    """调 DeepSeek 把 opc 笔记转成公众号标题 + 带内联 style 的 HTML 正文（微信不支持外部 CSS）。"""
    system_prompt = (
        "你是公众号排版助手。把用户给的 Markdown 笔记转换成可以直接提交给微信公众号草稿接口的 HTML。\n"
        "规则：只能用 <h3>/<p>/<blockquote>/<strong>/<code> 这几个标签，每个标签都必须带内联 "
        "style 属性（微信不支持 <style> 块或外部 CSS），字号 15-16px、行高 1.8-1.9，正文颜色 "
        "#2e3a46，标题/重点用 #b8681e 做分隔线或强调色。\n"
        "输出格式：第一行是文章标题（不要任何前缀符号），空一行，然后是完整 HTML 正文。"
        "不要输出除此之外的任何解释文字。"
    )
    raw = call_deepseek(system_prompt, opc_content, api_key, max_tokens=4000)
    parts = raw.strip().split("\n", 2)
    title = parts[0].strip().lstrip("#").strip()
    html = parts[-1].strip() if len(parts) > 1 else ""
    if not title or not html:
        raise publishers.PublishError(f"DeepSeek 生成的公众号内容格式不对，无法拆出标题/正文：{raw[:200]}")
    return title, html


def execute_toutiao_draft(date: str, api_key: str) -> dict:
    path = publishers.publish_toutiao_draft(date)
    if path is None:
        return {"toutiao_skipped": f"opc_{date}.md 不存在，安全跳过（不算失败）"}
    return {"toutiao": str(path)}


def execute_ops_assistant_full(date: str, api_key: str) -> dict:
    results: dict = {}

    try:
        path = publishers.publish_toutiao_draft(date)
        if path is None:
            results["toutiao_skipped"] = f"opc_{date}.md 不存在，安全跳过（不算失败）"
        else:
            results["toutiao"] = str(path)
    except publishers.PublishError as exc:
        results["toutiao_error"] = str(exc)

    opc_content = read_opc_content(date)
    if opc_content is None:
        results["wechat_skipped"] = f"opc_{date}.md 不存在，安全跳过（不臆造素材，不算失败）"
    else:
        try:
            title, html = generate_wechat_html(opc_content, api_key)
            results["wechat"] = publishers.publish_wechat_draft(title, html)
        except publishers.PublishError as exc:
            results["wechat_error"] = str(exc)

    return results


def generate_toutiao_article(content: str, api_key: str) -> str:
    """跟 toutiao-agent 的 Generate-ToutiaoDraft.ps1 用同一套 system prompt——只是输入源从
    固定的 opc_{date}.md 换成任意内容（这里是会议纪要），保持同样的候选标题/正文/标签/分类
    结构，不重新发明改写逻辑。
    """
    system_prompt = (
        "你是一名资深今日头条（头条号）内容运营，负责把工作素材改写成适合头条号发布的图文文章。\n\n"
        "头条读者和平台特点：\n"
        "- 标题要直白、有信息增量，避免\"震惊体\"和标题党，但要有一个清晰的钩子（数字/对比/反常识/疑问）\n"
        "- 正文段落要短（2-4行一段），信息密度高，少用书面语和空话\n"
        "- 避免营销号浮夸语气，观点要有具体案例或数据支撑（只使用素材里真实提到的信息，不要编造）\n"
        "- 结尾可以留一个自然的互动引导（提问/求关注），不要生硬\n\n"
        "请基于我提供的素材，输出以下结构（用于人工审阅后手动粘贴进头条号后台，不要输出多余的解释文字）：\n\n"
        "## 候选标题\n给出 3 个不同角度的候选标题（编号列出）。\n\n"
        "## 正文\n完整正文（800-1500字），用纯文本自然段落输出，段落之间空一行分隔，"
        "不要使用任何 Markdown 语法符号（不要 #、不要 **加粗**、不要项目符号），因为这段文字会被"
        "直接复制粘贴进头条号网页编辑器。如果素材里包含 URL 链接，去掉 Markdown 链接语法包装，"
        "但必须把完整的 URL 文本原样保留在正文里，绝对不能写\"链接在文末\"这类没有实际网址的占位说法。\n\n"
        "## 建议标签\n3-5 个适合头条号的标签，逗号分隔。\n\n"
        "## 建议分类\n给出一个最贴合的头条号内容分类（如：职场、科技、创业、AI、数码等）。"
    )
    return call_deepseek(system_prompt, content, api_key, max_tokens=4000)


def execute_ops_assistant_from_minutes(minutes_path: str, api_key: str) -> dict:
    """圆桌讨论产出会议纪要之后，直接调 ops-assistant 把这份纪要写成头条 + 公众号草稿——
    跟 execute_ops_assistant_full 是同一个角色，只是输入源从"当天 opc"换成"某一份具体的
    会议纪要文件"，这条路径由 UI 直接触发，不经过 inbox。
    """
    path = Path(minutes_path)
    if not path.exists():
        raise FileNotFoundError(f"会议纪要文件不存在：{minutes_path}")
    content = path.read_text(encoding="utf-8-sig")

    results: dict = {}

    try:
        article = generate_toutiao_article(content, api_key)
        slug = re.sub(r"[^\w一-鿿-]", "-", path.stem)[:40].strip("-") or "untitled"
        draft_path = publishers.TOUTIAO_DRAFTS_DIR / f"toutiao_from_minutes_{slug}.md"
        draft_path.parent.mkdir(parents=True, exist_ok=True)
        header = f"本文由 DeepSeek 根据会议纪要 {path.name} 自动生成草稿，发布前请人工审阅。\n\n---\n\n"
        draft_path.write_text(header + article, encoding="utf-8")
        results["toutiao"] = str(draft_path)
        logger.info(f"[ops-assistant] 从会议纪要生成头条草稿成功：{draft_path}")
    except Exception as exc:
        logger.exception("[ops-assistant] 从会议纪要生成头条草稿失败")
        results["toutiao_error"] = str(exc)

    try:
        title, html = generate_wechat_html(content, api_key)
        results["wechat"] = publishers.publish_wechat_draft(title, html)
        logger.info(f"[ops-assistant] 从会议纪要生成公众号草稿成功：{title!r}")
    except publishers.PublishError as exc:
        logger.warning(f"[ops-assistant] 从会议纪要生成公众号草稿失败：{exc}")
        results["wechat_error"] = str(exc)

    return results


# key 对应 agents.yaml 里的 executor 字段
EXECUTORS = {
    "toutiao_draft": execute_toutiao_draft,
    "ops_assistant_full": execute_ops_assistant_full,
}


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
                                f"agents.yaml 和 dispatcher.py 的 EXECUTORS 字典没同步更新，是需要修的不一致。")
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
