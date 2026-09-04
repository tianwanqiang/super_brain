"""
executors.py 里"CEO 补充要求"（user_instruction）这条参数的测试——2026-09-04 新增，
让手动触发生成草稿时可以额外给一句补充要求（比如"这次重点讲价格策略"），要求：
只影响这次生成的用户素材，不能改动 content-strategist/writer 自己的 system prompt
（那是 agent 的私有知识框架，代表这个角色本身的判断标准，不应该被一次性要求覆盖）。

全程 mock 掉 call_deepseek，不发起任何真实调用；断言重点是"user_instruction 出现在
调用参数的哪个位置"，不是真的验证 DeepSeek 会不会听话（那部分需要真实调用，不归这里管）。
"""
import pytest

import executors


def test_apply_user_instruction_returns_content_unchanged_when_none():
    assert executors._apply_user_instruction("素材正文", None) == "素材正文"


def test_apply_user_instruction_returns_content_unchanged_when_empty_string():
    assert executors._apply_user_instruction("素材正文", "") == "素材正文"


def test_apply_user_instruction_returns_content_unchanged_when_whitespace_only():
    assert executors._apply_user_instruction("素材正文", "   ") == "素材正文"


def test_apply_user_instruction_appends_instruction_to_content():
    result = executors._apply_user_instruction("素材正文", "这次重点讲价格策略")
    assert "素材正文" in result
    assert "这次重点讲价格策略" in result
    # 补充要求必须在素材正文之后，不能替换掉素材本身
    assert result.index("素材正文") < result.index("这次重点讲价格策略")


def test_generate_content_brief_passes_user_instruction_into_user_turn_not_system_prompt(monkeypatch):
    captured = {}

    def fake_call_deepseek(system_prompt, user_prompt, api_key, max_tokens=None):
        captured["system_prompt"] = system_prompt
        captured["user_prompt"] = user_prompt
        return "假简报"

    monkeypatch.setattr(executors, "call_deepseek", fake_call_deepseek)
    monkeypatch.setattr(executors, "load_agent_registry", lambda: {})
    monkeypatch.setattr(executors, "load_private_context", lambda name, registry: "content-strategist 的知识框架")

    executors.generate_content_brief("会议纪要正文", "fake-key", user_instruction="重点讲价格策略")

    assert "重点讲价格策略" not in captured["system_prompt"], "补充要求不应该混进 system prompt"
    assert "重点讲价格策略" in captured["user_prompt"]
    assert "会议纪要正文" in captured["user_prompt"]


def test_generate_writer_draft_passes_same_user_instruction_to_both_stages(monkeypatch):
    """content-strategist（生成简报）和 writer（正式执笔）两步必须看到同一份补充要求，
    不能只有一步知道。
    """
    captured_calls = []

    def fake_call_deepseek(system_prompt, user_prompt, api_key, max_tokens=None):
        captured_calls.append({"system_prompt": system_prompt, "user_prompt": user_prompt})
        return "假输出"

    monkeypatch.setattr(executors, "call_deepseek", fake_call_deepseek)
    monkeypatch.setattr(executors, "load_agent_registry", lambda: {})
    monkeypatch.setattr(executors, "load_private_context", lambda name, registry: f"{name} 的知识框架")

    executors.generate_writer_draft("会议纪要正文", "fake-key", user_instruction="换个轻松点的语气")

    assert len(captured_calls) == 2, "应该调用两次：content-strategist 生成简报 + writer 正式执笔"
    for call in captured_calls:
        assert "换个轻松点的语气" not in call["system_prompt"]
        assert "换个轻松点的语气" in call["user_prompt"]


def test_generate_writer_draft_without_user_instruction_behaves_exactly_as_before(monkeypatch):
    """不传 user_instruction 时，行为必须跟这个参数完全不存在时一样——回归测试，
    防止这次改动悄悄影响了没有补充要求的默认路径。
    """
    captured_calls = []

    def fake_call_deepseek(system_prompt, user_prompt, api_key, max_tokens=None):
        captured_calls.append(user_prompt)
        return "假输出"

    monkeypatch.setattr(executors, "call_deepseek", fake_call_deepseek)
    monkeypatch.setattr(executors, "load_agent_registry", lambda: {})
    monkeypatch.setattr(executors, "load_private_context", lambda name, registry: "知识框架")

    executors.generate_writer_draft("会议纪要正文", "fake-key")

    for user_prompt in captured_calls:
        assert user_prompt == "会议纪要正文", "没有补充要求时，用户素材应该原样传递，不带任何额外文本"


def test_execute_ops_assistant_from_minutes_records_user_instruction_in_results(fake_minutes_file_factory, monkeypatch):
    minutes_file = fake_minutes_file_factory("正文内容")
    monkeypatch.setattr(executors, "generate_writer_draft", lambda content, api_key, user_instruction=None: "定稿")
    monkeypatch.setattr(executors, "adapt_draft_to_toutiao", lambda draft, api_key: "头条正文")
    monkeypatch.setattr(executors, "adapt_draft_to_wechat", lambda draft, api_key: ("标题", "<p>正文</p>"))
    import publishers
    monkeypatch.setattr(publishers, "publish_wechat_draft", lambda title, html: {"draft_media_id": "x", "thumb_media_id": "y"})
    monkeypatch.setattr(publishers, "get_toutiao_drafts_dir", lambda: minutes_file.parent / "toutiao")
    monkeypatch.setattr(publishers, "get_wechat_drafts_dir", lambda: minutes_file.parent / "wechat")

    results = executors.execute_ops_assistant_from_minutes(
        str(minutes_file), "fake-key", user_instruction="这次重点讲价格策略",
    )
    assert results.get("user_instruction") == "这次重点讲价格策略"


def test_execute_ops_assistant_from_minutes_omits_user_instruction_key_when_not_given(fake_minutes_file_factory, monkeypatch):
    minutes_file = fake_minutes_file_factory("正文内容")
    monkeypatch.setattr(executors, "generate_writer_draft", lambda content, api_key, user_instruction=None: "定稿")
    monkeypatch.setattr(executors, "adapt_draft_to_toutiao", lambda draft, api_key: "头条正文")
    monkeypatch.setattr(executors, "adapt_draft_to_wechat", lambda draft, api_key: ("标题", "<p>正文</p>"))
    import publishers
    monkeypatch.setattr(publishers, "publish_wechat_draft", lambda title, html: {"draft_media_id": "x", "thumb_media_id": "y"})
    monkeypatch.setattr(publishers, "get_toutiao_drafts_dir", lambda: minutes_file.parent / "toutiao")
    monkeypatch.setattr(publishers, "get_wechat_drafts_dir", lambda: minutes_file.parent / "wechat")

    results = executors.execute_ops_assistant_from_minutes(str(minutes_file), "fake-key")
    assert "user_instruction" not in results


@pytest.fixture
def fake_minutes_file_factory(tmp_path):
    def _make(content: str):
        path = tmp_path / "minutes.md"
        path.write_text(content, encoding="utf-8")
        return path
    return _make
