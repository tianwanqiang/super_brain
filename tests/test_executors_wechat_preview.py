"""
executors.execute_ops_assistant_from_minutes() 里公众号草稿本地留存（供预览）这部分的
测试——2026-09-04 被指出：头条草稿一直有"预览"能力（本地存文件 + /draft/preview 路由），
公众号草稿只调了微信真实接口、结果只有 draft_media_id，本地完全没留内容，导致用户只能
跳出 super_brain 去微信公众号后台才能看到写了什么。这里先独立验证后台逻辑本身对不对，
不依赖任何 UI/模板；UI 部分（templates/index.html 的预览链接）另外验证。

全程 monkeypatch 掉 generate_writer_draft/adapt_draft_to_toutiao/adapt_draft_to_wechat/
publishers.publish_wechat_draft 这几个会真的花钱调用 DeepSeek/微信接口的函数，不发起任何
真实网络请求。
"""
import pytest

import executors
import publishers


@pytest.fixture
def fake_drafts_dir(tmp_path, monkeypatch):
    """头条、公众号各自独立的假目录——两者互不依赖，这里也分开 mock，避免测试本身
    悄悄假设两者共用一个目录（这正是这次要拆开的耦合）。
    """
    toutiao_dir = tmp_path / "toutiao-drafts"
    wechat_dir = tmp_path / "wechat-drafts"
    monkeypatch.setattr(publishers, "get_toutiao_drafts_dir", lambda: toutiao_dir)
    monkeypatch.setattr(publishers, "get_wechat_drafts_dir", lambda: wechat_dir)
    return wechat_dir


@pytest.fixture
def fake_minutes_file(tmp_path):
    minutes_path = tmp_path / "minutes.md"
    minutes_path.write_text("# 会议纪要\n\n这是一份测试用的会议纪要正文。", encoding="utf-8")
    return minutes_path


def test_wechat_html_is_saved_locally_before_publish_succeeds(fake_drafts_dir, fake_minutes_file, monkeypatch):
    monkeypatch.setattr(executors, "generate_writer_draft", lambda content, api_key, user_instruction=None: "定稿正文")
    monkeypatch.setattr(executors, "adapt_draft_to_toutiao", lambda draft, api_key: "头条正文")
    monkeypatch.setattr(
        executors, "adapt_draft_to_wechat",
        lambda draft, api_key: ("公众号标题", "<h3 style=\"color:#b8681e\">公众号标题</h3><p>正文内容</p>"),
    )
    monkeypatch.setattr(
        publishers, "publish_wechat_draft",
        lambda title, html: {"draft_media_id": "fake-media-id", "thumb_media_id": "fake-thumb-id"},
    )

    results = executors.execute_ops_assistant_from_minutes(str(fake_minutes_file), "fake-api-key")

    assert "wechat_preview_path" in results
    preview_content = open(results["wechat_preview_path"], encoding="utf-8").read()
    assert "公众号标题" in preview_content
    assert "正文内容" in preview_content
    assert results["wechat"]["draft_media_id"] == "fake-media-id"


def test_wechat_html_is_still_saved_locally_when_publish_fails(fake_drafts_dir, fake_minutes_file, monkeypatch):
    """核心诉求：即使真实的微信发布调用失败（网络/凭据/素材上传任何一环），本地这份预览
    内容也不应该跟着丢——用户至少能看到"生成了什么"，不是只剩一段报错文字。
    """
    monkeypatch.setattr(executors, "generate_writer_draft", lambda content, api_key, user_instruction=None: "定稿正文")
    monkeypatch.setattr(executors, "adapt_draft_to_toutiao", lambda draft, api_key: "头条正文")
    monkeypatch.setattr(
        executors, "adapt_draft_to_wechat",
        lambda draft, api_key: ("公众号标题", "<p>即使发布失败也应该留着的内容</p>"),
    )

    def _raise_publish_error(title, html):
        raise publishers.PublishError("模拟微信接口调用失败（比如凭据过期）")

    monkeypatch.setattr(publishers, "publish_wechat_draft", _raise_publish_error)

    results = executors.execute_ops_assistant_from_minutes(str(fake_minutes_file), "fake-api-key")

    assert "wechat_error" in results
    assert "wechat_preview_path" in results, "发布失败时本地预览文件也不应该丢"
    preview_content = open(results["wechat_preview_path"], encoding="utf-8").read()
    assert "即使发布失败也应该留着的内容" in preview_content


def test_wechat_preview_path_is_inside_wechat_drafts_dir_not_toutiao(fake_drafts_dir, fake_minutes_file, monkeypatch):
    """核心诉求：公众号预览文件必须落在它自己独立的目录（get_wechat_drafts_dir()）里，
    不能出现在头条的目录下——两者不该再共用一个目录，这是这次拆开耦合要钉死的前提。
    """
    monkeypatch.setattr(executors, "generate_writer_draft", lambda content, api_key, user_instruction=None: "定稿正文")
    monkeypatch.setattr(executors, "adapt_draft_to_toutiao", lambda draft, api_key: "头条正文")
    monkeypatch.setattr(executors, "adapt_draft_to_wechat", lambda draft, api_key: ("标题", "<p>正文</p>"))
    monkeypatch.setattr(publishers, "publish_wechat_draft", lambda title, html: {"draft_media_id": "x", "thumb_media_id": "y"})

    results = executors.execute_ops_assistant_from_minutes(str(fake_minutes_file), "fake-api-key")

    from pathlib import Path
    preview_path = Path(results["wechat_preview_path"]).resolve()
    preview_path.relative_to(publishers.get_wechat_drafts_dir().resolve())  # 不抛异常就是通过
    with pytest.raises(ValueError):
        preview_path.relative_to(publishers.get_toutiao_drafts_dir().resolve())


def test_toutiao_and_wechat_both_generated_from_the_same_shared_draft(fake_drafts_dir, fake_minutes_file, monkeypatch):
    """头条和公众号必须共享同一份 writer 定稿——只调一次 generate_writer_draft，不是各自
    独立判断内容，这是这个函数最核心的设计约束（避免两个平台版本互相打架）。
    """
    call_count = {"generate_writer_draft": 0}

    def _fake_generate_writer_draft(content, api_key, user_instruction=None):
        call_count["generate_writer_draft"] += 1
        return "共享定稿正文"

    monkeypatch.setattr(executors, "generate_writer_draft", _fake_generate_writer_draft)
    monkeypatch.setattr(executors, "adapt_draft_to_toutiao", lambda draft, api_key: f"头条版：{draft}")
    monkeypatch.setattr(executors, "adapt_draft_to_wechat", lambda draft, api_key: ("标题", f"<p>公众号版：{draft}</p>"))
    monkeypatch.setattr(publishers, "publish_wechat_draft", lambda title, html: {"draft_media_id": "x", "thumb_media_id": "y"})

    executors.execute_ops_assistant_from_minutes(str(fake_minutes_file), "fake-api-key")

    assert call_count["generate_writer_draft"] == 1
