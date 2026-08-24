"""
super_brain executors - "有 executor 字段的 agent 该怎么真实执行"这一件事

从 dispatcher.py 拆出来的——这一块是具体的业务逻辑（生成头条/公众号草稿、ops-assistant
分发），依赖 agent_registry（拿 writer 的写作技能框架）、llm_client（真实调用 DeepSeek）、
publishers（真实发布接口），是三者的组装层，不是三者本身。

EXECUTORS 字典的 key 对应 agents.yaml 里的 executor 字段，dispatcher.py 的 main() 从
这里查表调用。
"""
import logging
import re
from pathlib import Path

import publishers
import video_prompt
from agent_registry import load_agent_registry, load_private_context, log_execution
from llm_client import call_deepseek
from paths import OPC_ROOT, SUPER_BRAIN

logger = logging.getLogger("super_brain.executors")

TASK_DRAFTS_DIR = SUPER_BRAIN / "task_drafts"


def read_opc_content(date: str) -> str | None:
    opc_path = OPC_ROOT / f"opc_{date}.md"
    if not opc_path.exists():
        return None
    return opc_path.read_text(encoding="utf-8-sig")


def generate_content_brief(content: str, api_key: str) -> str:
    """content-strategist 的上游规划步骤——正式生成文案之前，先产出一份策划简报
    （钩子/节奏/取舍/风险提示），供下面 writer 框架的实际写作参照。这一步让每次生成
    头条/公众号文案的 DeepSeek 调用从 1 次变成 2 次，是已经确认接受的成本取舍
    （见 agents.yaml 里 content-strategist 的注册说明）。
    """
    registry = load_agent_registry()
    strategist_context = load_private_context("content-strategist", registry)
    system_prompt = (
        f"下面是你的策划知识框架（private.md 原文）：\n\n{strategist_context}\n\n"
        "请根据我提供的素材，按你私有上下文里定义的'输出格式'产出一份策划简报，"
        "不要输出成品文案，也不要输出简报格式之外的任何解释文字。"
    )
    logger.info("调用 content-strategist 生成策划简报")
    brief = call_deepseek(system_prompt, content, api_key, max_tokens=1500)
    log_execution("content-strategist", "生成策划简报", f"素材长度={len(content)}字")
    return brief


def generate_wechat_html(opc_content: str, api_key: str) -> tuple[str, str]:
    """调 DeepSeek 把 opc 笔记转成公众号标题 + 带内联 style 的 HTML 正文（微信不支持外部 CSS）。
    先跑 content-strategist 的上游策划简报，再接上 writer 的运营写作技能框架（取舍原则、
    具体场景优先、避免自证式空话这几条），最后叠加公众号这个平台的具体格式规则——
    三层不是互相独立的写作哲学，策划简报负责"怎么设计抓人"，writer 框架负责"怎么取舍
    表达"，平台规则负责"具体格式"。
    """
    brief = generate_content_brief(opc_content, api_key)

    registry = load_agent_registry()
    writer_context = load_private_context("writer", registry)
    system_prompt = (
        f"下面是 content-strategist 给出的策划简报，正式写作时要落实简报里的钩子/节奏/"
        f"取舍建议（跨媒介类比的规则已经在简报里标注过，公众号是文字内容，直接按简报的"
        f"文字化建议执行即可）：\n\n{brief}\n\n"
        f"下面是通用的运营写作技能框架，写之前先参照这里的取舍原则（具体场景优先、避免自证式"
        f"空话、读者预期对齐）：\n\n{writer_context}\n\n"
        "你现在具体要做的是公众号排版：把用户给的 Markdown 笔记转换成可以直接提交给微信公众号"
        "草稿接口的 HTML。\n"
        "格式规则：只能用 <h3>/<p>/<blockquote>/<strong>/<code> 这几个标签，每个标签都必须带"
        "内联 style 属性（微信不支持 <style> 块或外部 CSS），字号 15-16px、行高 1.8-1.9，正文"
        "颜色 #2e3a46，标题/重点用 #b8681e 做分隔线或强调色。公众号读者是主动点开、愿意深度"
        "阅读，可以有更完整的叙事弧线，不需要每段都抓眼球。\n"
        "输出格式：第一行是文章标题（不要任何前缀符号），空一行，然后是完整 HTML 正文。"
        "不要输出除此之外的任何解释文字。"
    )
    raw = call_deepseek(system_prompt, opc_content, api_key, max_tokens=12000)
    parts = raw.strip().split("\n", 2)
    title = parts[0].strip().lstrip("#").strip()
    html = parts[-1].strip() if len(parts) > 1 else ""
    if not title or not html:
        raise publishers.PublishError(f"DeepSeek 生成的公众号内容格式不对，无法拆出标题/正文：{raw[:200]}")
    return title, html


def execute_toutiao_draft(date: str, api_key: str) -> dict:
    path = publishers.publish_toutiao_draft(date)
    if path is None:
        log_execution("toutiao", "生成头条草稿", f"opc_{date}.md 不存在，安全跳过", status="skipped")
        return {"toutiao_skipped": f"opc_{date}.md 不存在，安全跳过（不算失败）"}
    log_execution("toutiao", "生成头条草稿", f"日期={date}，产物：{path}")
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

    ok = not any(k.endswith("_error") for k in results)
    log_execution(
        "ops-assistant", "分发当天内容到头条+公众号", f"日期={date}，结果：{results}",
        status="ok" if ok else "partial_error",
    )
    return results


def generate_toutiao_article(content: str, api_key: str) -> str:
    """跟 toutiao-agent 的 Generate-ToutiaoDraft.ps1 用同一套核心改写逻辑，输入源从固定的
    opc_{date}.md 换成任意内容（这里是会议纪要）。先跑 content-strategist 的上游策划简报，
    再接上 writer 的运营写作技能框架（同一套取舍原则），最后叠加头条这个平台的具体格式规则。
    """
    brief = generate_content_brief(content, api_key)

    registry = load_agent_registry()
    writer_context = load_private_context("writer", registry)
    system_prompt = (
        f"下面是 content-strategist 给出的策划简报，正式写作时要落实简报里的钩子/节奏/"
        f"取舍建议（跨媒介类比的规则已经在简报里标注过，头条是文字内容，直接按简报的"
        f"文字化建议执行即可）：\n\n{brief}\n\n"
        f"下面是通用的运营写作技能框架，写之前先参照这里的取舍原则（具体场景优先、避免自证式"
        f"空话、读者预期对齐）：\n\n{writer_context}\n\n"
        "你现在具体要做的是把工作素材改写成适合头条号发布的图文文章。\n\n"
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
    return call_deepseek(system_prompt, content, api_key, max_tokens=12000)


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
        draft_path = publishers.get_toutiao_drafts_dir() / f"toutiao_from_minutes_{slug}.md"
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

    ok = not any(k.endswith("_error") for k in results)
    log_execution(
        "ops-assistant", "从会议纪要分发草稿", f"来源={minutes_path}，结果：{results}",
        status="ok" if ok else "partial_error",
    )
    return results


def _generate_writer_draft(description: str, api_key: str) -> str:
    """Round 3 任务确认后，派给 writer 的步骤——用它的运营写作技能框架，从一句任务描述
    直接产出一份独立文档草稿（不是会议纪要，输入源不是圆桌讨论原始记录，是 Round 3 收敛
    出的具体行动描述）。落盘到 task_drafts/，不发布、不群发，只到"草稿已生成"为止。
    """
    registry = load_agent_registry()
    writer_context = load_private_context("writer", registry)
    system_prompt = (
        f"下面是你的运营写作技能框架（private.md 原文），写作时必须遵循里面的取舍原则："
        f"\n\n{writer_context}\n\n"
        "你现在要做的不是写会议纪要，是根据一句具体的行动描述，直接写出对应的成品文档"
        "（比如一份公告、一封说明、一段说明文案——文体由这句描述本身决定，不要额外套用"
        "会议纪要的格式）。第一行输出一个简短标题（不要任何前缀符号），空一行，然后是正文。"
    )
    raw = call_deepseek(system_prompt, description, api_key, max_tokens=6000)
    lines = raw.strip().split("\n", 1)
    title = lines[0].strip().lstrip("#").strip() or "未命名草稿"
    slug = re.sub(r"[^\w一-鿿-]", "-", title)[:40].strip("-") or "untitled"
    TASK_DRAFTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = TASK_DRAFTS_DIR / f"{slug}.md"
    out_path.write_text(raw, encoding="utf-8")
    logger.info(f"writer 任务草稿已生成：{out_path}")
    log_execution("writer", "生成任务草稿", f"任务：{description[:60]}，产物：{out_path}")
    return str(out_path)


def _generate_toutiao_draft_from_task(description: str, api_key: str) -> str:
    """复用跟会议纪要生成头条草稿完全同一套改写逻辑（generate_toutiao_article），
    只是输入源换成 Round 3 的任务描述。"""
    article = generate_toutiao_article(description, api_key)
    slug = re.sub(r"[^\w一-鿿-]", "-", description)[:30].strip("-") or "untitled"
    draft_path = publishers.get_toutiao_drafts_dir() / f"toutiao_task_{slug}.md"
    draft_path.parent.mkdir(parents=True, exist_ok=True)
    header = "本文由 DeepSeek 根据圆桌决策任务自动生成草稿，发布前请人工审阅。\n\n---\n\n"
    draft_path.write_text(header + article, encoding="utf-8")
    logger.info(f"头条任务草稿已生成：{draft_path}")
    log_execution("toutiao", "生成任务草稿", f"任务：{description[:60]}，产物：{draft_path}")
    return str(draft_path)


def _generate_video_prompt_draft(description: str) -> str:
    """派给 video-prompt 的步骤——开一个新的 video-prompt 会话，把任务描述当第一条消息发
    进去，产出的是提示词本身，落在 video_prompt_conversations/ 里，不是文件草稿。返回值是
    这个会话的引用（不是文件路径），前端可以据此跳转到 /video-prompt?conversation=<id>。
    """
    conversation_id = video_prompt.create_conversation(description)
    video_prompt.send_message(conversation_id, description)
    logger.info(f"video-prompt 任务会话已生成：{conversation_id}")
    log_execution("video-prompt", "生成任务提示词", f"任务：{description[:60]}，会话：{conversation_id}")
    return f"video-prompt-conversation:{conversation_id}"


def generate_task_artifact(task: dict, api_key: str) -> str:
    """Round 3 任务被 CEO 点"确认"之后，真正触发产物生成——按 assignee_agent 分发。
    返回产物的路径/引用字符串，调用方负责写回 task 的 artifact_path。

    只覆盖目前真的有能力自动做到的几类。'ship'（本地 git commit）和邮件草稿明确不支持：
    ship 的 git 任务从设计上就"不绑定固定仓库，以当前工作目录的 git remote 为准"（见
    agents/ship/private.md），自动化场景下没有"当前工作目录"这个概念，瞎猜目标仓库风险
    太高，宁可不做；邮件发送目前完全没有配置（收件人/SMTP/API key 都不存在）。这两类
    抛 NotImplementedError，不假装做了——调用方应该提示"需要人工完成"，而不是吞掉异常。
    """
    agent = task.get("assignee_agent")
    description = (task.get("description") or "").strip()
    if not description:
        raise ValueError("任务没有描述内容，没法生成产物")

    if agent == "writer":
        return _generate_writer_draft(description, api_key)
    if agent == "toutiao":
        return _generate_toutiao_draft_from_task(description, api_key)
    if agent == "video-prompt":
        return _generate_video_prompt_draft(description)

    raise NotImplementedError(
        f"assignee_agent={agent!r} 目前没有自动生成产物的能力"
        "（ship 提交代码的目标仓库不确定、邮件发送目前没有任何配置），需要人工手动完成这一步，"
        "完成后可以在页面上把任务手动标记为已完成。"
    )


# key 对应 agents.yaml 里的 executor 字段
EXECUTORS = {
    "toutiao_draft": execute_toutiao_draft,
    "ops_assistant_full": execute_ops_assistant_full,
}
