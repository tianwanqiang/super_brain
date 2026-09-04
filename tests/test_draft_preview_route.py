"""
/draft/preview 路由对 .html 文件（公众号草稿）的支持——2026-09-04 之前这个路由只认
.md/.txt（头条草稿，按纯文本转义展示）。公众号草稿本地留存后（executors.py 的改动，
见 test_executors_wechat_preview.py），需要这个路由能正确识别 .html 并按真实 HTML 渲染
（不是把标签当纯文本转义出来）。

头条、公众号、会议纪要各自有自己独立的目录（publishers.get_toutiao_drafts_dir() /
get_wechat_drafts_dir() / ui_app.get_meeting_minutes_dir()，互不依赖，见
test_publishers_drafts_dirs.py），这个路由要同时认这三个目录——落在其中任意一个都放行，
都不在的一律拒绝；会议纪要目录没配置时不参与判断（不是"放行更宽松"，是少一个允许的根）。
"""
import pytest

import publishers
import ui_app


@pytest.fixture
def fake_drafts_dirs(tmp_path, monkeypatch):
    toutiao_dir = tmp_path / "toutiao-drafts"
    wechat_dir = tmp_path / "wechat-drafts"
    toutiao_dir.mkdir()
    wechat_dir.mkdir()
    monkeypatch.setattr(publishers, "get_toutiao_drafts_dir", lambda: toutiao_dir)
    monkeypatch.setattr(publishers, "get_wechat_drafts_dir", lambda: wechat_dir)
    monkeypatch.setattr(ui_app, "get_meeting_minutes_dir", lambda: None)
    return toutiao_dir, wechat_dir


@pytest.fixture
def fake_meeting_minutes_dir(tmp_path, monkeypatch):
    minutes_dir = tmp_path / "meeting-minutes"
    minutes_dir.mkdir()
    monkeypatch.setattr(ui_app, "get_meeting_minutes_dir", lambda: minutes_dir)
    return minutes_dir


def _get(path):
    with ui_app.app.test_request_context(f"/draft/preview?path={path}"):
        return ui_app.draft_preview()


def test_html_file_in_wechat_dir_is_rendered_as_real_html(fake_drafts_dirs):
    _, wechat_dir = fake_drafts_dirs
    html_file = wechat_dir / "from_minutes_test.html"
    html_file.write_text('<h3 style="color:#b8681e">标题</h3><p>正文</p>', encoding="utf-8")

    response = _get(str(html_file))
    body = response.get_data(as_text=True) if hasattr(response, "get_data") else response

    # 真正渲染成 HTML 标签，而不是被转义成 &lt;h3&gt; 这种文本
    assert "<h3" in body
    assert "&lt;h3" not in body


def test_md_file_in_toutiao_dir_is_still_rendered_as_escaped_plaintext(fake_drafts_dirs):
    """回归测试：加了 .html 支持之后，原来 .md 头条草稿的纯文本展示行为不能变——
    如果草稿正文里恰好包含 <标签> 这种字符（比如引用了一段代码），必须原样转义显示，
    不能被当成真 HTML 渲染出来。
    """
    toutiao_dir, _ = fake_drafts_dirs
    md_file = toutiao_dir / "toutiao_test.md"
    md_file.write_text("正文提到了 <script>alert(1)</script> 这种字符，应该原样显示。", encoding="utf-8")

    response = _get(str(md_file))
    body = response.get_data(as_text=True) if hasattr(response, "get_data") else response

    assert "&lt;script&gt;" in body
    assert "<script>alert(1)</script>" not in body


def test_html_file_in_toutiao_dir_also_works(fake_drafts_dirs):
    """两个目录都要认——即使 .html 文件出现在头条目录（理论上不该发生，但路由本身
    不应该假设文件类型和目录是绑死的一一对应关系），只要落在允许的目录内就该放行。
    """
    toutiao_dir, _ = fake_drafts_dirs
    html_file = toutiao_dir / "weird.html"
    html_file.write_text("<p>正文</p>", encoding="utf-8")

    response = _get(str(html_file))
    body = response.get_data(as_text=True) if hasattr(response, "get_data") else response
    assert "<p>" in body


def test_file_outside_both_allowed_dirs_is_rejected(fake_drafts_dirs, tmp_path):
    outside_file = tmp_path / "outside.html"
    outside_file.write_text("<p>不该被预览到</p>", encoding="utf-8")

    response, status = _get(str(outside_file))
    assert status == 403


def test_unsupported_extension_is_rejected(fake_drafts_dirs):
    toutiao_dir, _ = fake_drafts_dirs
    bad_file = toutiao_dir / "not-allowed.exe"
    bad_file.write_bytes(b"\x00\x01")

    response, status = _get(str(bad_file))
    assert status == 403


def test_missing_file_returns_404(fake_drafts_dirs):
    _, wechat_dir = fake_drafts_dirs
    missing = wechat_dir / "does-not-exist.html"
    response, status = _get(str(missing))
    assert status == 404


def test_meeting_minutes_file_is_previewable_when_dir_is_configured(fake_drafts_dirs, fake_meeting_minutes_dir):
    minutes_file = fake_meeting_minutes_dir / "2026-09-04_1000_test.md"
    minutes_file.write_text("# 会议纪要\n\n正文内容", encoding="utf-8")

    response = _get(str(minutes_file))
    body = response.get_data(as_text=True) if hasattr(response, "get_data") else response
    assert "正文内容" in body


def test_meeting_minutes_preview_rejected_when_dir_not_configured(fake_drafts_dirs, tmp_path):
    """get_meeting_minutes_dir() 返回 None（没配置）时，任何路径都不应该因为"凑巧长得像
    会议纪要文件"就被放行——必须显式配置了目录，这个目录才参与安全边界判断。
    """
    somewhere = tmp_path / "somewhere" / "2026-09-04_1000_test.md"
    somewhere.parent.mkdir(parents=True)
    somewhere.write_text("正文", encoding="utf-8")

    response, status = _get(str(somewhere))
    assert status == 403


def test_stale_legacy_path_outside_current_meeting_minutes_dir_is_rejected(fake_drafts_dirs, fake_meeting_minutes_dir, tmp_path):
    """2026-09-04 真实场景：MEETING_MINUTES_DIR 曾经配错过（带引号/相对路径），旧会话
    记录里存着当时错误配置下生成的畸形路径。即使现在已经配好了正确的会议纪要目录，
    这类不在当前目录下的旧路径也应该继续被拒绝——不能因为"看起来像会议纪要"就放宽，
    这是刻意的安全边界，不是需要修的 bug。
    """
    stale_dir = tmp_path / "opt" / "super_brain" / "meeting"
    stale_dir.mkdir(parents=True)
    stale_file = stale_dir / "2026-09-04_0844_test.md"
    stale_file.write_text("旧配置下生成的内容", encoding="utf-8")

    response, status = _get(str(stale_file))
    assert status == 403
