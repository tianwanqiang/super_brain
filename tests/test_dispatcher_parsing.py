"""
dispatcher.parse_pending_messages() 的单元测试——纯文本解析 + 校验逻辑，不碰真实
inbox.md、不调用 DeepSeek。这个函数专门防"打错字的孤儿留言也会真的花一次 DeepSeek
调用"这类问题，所以测试重点是校验规则本身有没有正确生效，而不是"能不能解析出正常格式"。
"""
from dispatcher import MESSAGE_LOG_HEADING, parse_pending_messages

REGISTRY = {
    "ship": {"name": "ship"},
    "writer": {"name": "writer"},
}


def _inbox(body: str) -> str:
    return f"# inbox\n\n## 使用约定\n\n(说明文字，包含一个 --- 分隔符示例也不该被解析)\n\n{MESSAGE_LOG_HEADING}\n\n{body}"


def test_parses_single_valid_pending_message():
    text = _inbox(
        "From: coordinator\n"
        "To: ship\n"
        "Time: 2026-09-04 10:00\n"
        "Status: pending\n"
        "Message: 推送代码到 GitHub"
    )
    entries = parse_pending_messages(text, REGISTRY)
    assert len(entries) == 1
    assert entries[0]["to"] == "ship"
    assert entries[0]["message"] == "推送代码到 GitHub"


def test_content_before_heading_is_never_parsed():
    """使用约定"部分里的示例 `---` 不能被误当成留言分隔符解析出内容。"""
    text = (
        "# inbox\n\n## 使用约定\n\n"
        "From: 示例\nTo: 示例\nTime: 示例\nStatus: pending\nMessage: 这是格式示例，不是真留言\n"
        f"\n{MESSAGE_LOG_HEADING}\n\n"
    )
    entries = parse_pending_messages(text, REGISTRY)
    assert entries == []


def test_missing_heading_returns_empty_and_does_not_crash():
    text = "# inbox\n\n没有留言记录标题的一份文档\n"
    assert parse_pending_messages(text, REGISTRY) == []


def test_done_status_is_excluded_from_pending_results():
    text = _inbox(
        "From: coordinator\nTo: ship\nTime: 2026-09-04 10:00\nStatus: done\nMessage: 已经处理过了"
    )
    assert parse_pending_messages(text, REGISTRY) == []


def test_status_case_mismatch_is_rejected_not_silently_treated_as_pending():
    text = _inbox(
        "From: coordinator\nTo: ship\nTime: 2026-09-04 10:00\nStatus: Pending\nMessage: 大小写错了"
    )
    assert parse_pending_messages(text, REGISTRY) == []


def test_unknown_to_field_is_rejected_to_avoid_wasting_a_paid_call():
    text = _inbox(
        "From: coordinator\nTo: not-a-real-agent\nTime: 2026-09-04 10:00\nStatus: pending\nMessage: 打错字的收件人"
    )
    assert parse_pending_messages(text, REGISTRY) == []


def test_literal_all_is_accepted_as_to_value():
    text = _inbox(
        "From: coordinator\nTo: All\nTime: 2026-09-04 10:00\nStatus: pending\nMessage: 群发给所有人"
    )
    entries = parse_pending_messages(text, REGISTRY)
    assert len(entries) == 1
    assert entries[0]["to"] == "All"


def test_multiline_message_is_rejected_not_silently_truncated():
    text = _inbox(
        "From: coordinator\nTo: ship\nTime: 2026-09-04 10:00\nStatus: pending\n"
        "Message: 第一行\n第二行不应该被吞掉，而是整条留言被拒绝"
    )
    assert parse_pending_messages(text, REGISTRY) == []


def test_message_containing_a_standalone_dash_line_is_split_not_rejected():
    """钉死当前的真实（有缺陷的）行为，不是背书——docstring 里写明"Message 内容本身
    不能包含这样一行"是对使用者的硬约束，但 parse_pending_messages() 本身并不会检测
    并拒绝这种输入：'---' 会被当成留言分隔符，把这条留言从中间切开，前半段因为凑巧
    自己就有完整的 From/To/Status/单行 Message，会被当成一条独立的（内容被截断的）
    合法留言解析出来，后半段则因为缺 From/To 被丢弃。这里测的是"这就是现状"，防止
    以后不知情地改动行为却没人发现。
    """
    text = _inbox(
        "From: coordinator\nTo: ship\nTime: 2026-09-04 10:00\nStatus: pending\n"
        "Message: 前半段\n---\n后半段"
    )
    entries = parse_pending_messages(text, REGISTRY)
    assert len(entries) == 1
    assert entries[0]["message"] == "前半段"


def test_multiple_messages_separated_by_dash_line():
    text = _inbox(
        "From: coordinator\nTo: ship\nTime: 2026-09-04 10:00\nStatus: pending\nMessage: 第一条\n"
        "---\n"
        "From: coordinator\nTo: writer\nTime: 2026-09-04 10:05\nStatus: pending\nMessage: 第二条"
    )
    entries = parse_pending_messages(text, REGISTRY)
    assert len(entries) == 2
    assert {e["to"] for e in entries} == {"ship", "writer"}
