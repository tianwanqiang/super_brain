"""
/draft/preview 路由对 .html 文件（公众号草稿）的支持——2026-09-04 之前这个路由只认
.md/.txt（头条草稿，按纯文本转义展示）。公众号草稿本地留存后（executors.py 的改动，
见 test_executors_wechat_preview.py），需要这个路由能正确识别 .html 并按真实 HTML 渲染
（不是把标签当纯文本转义出来）。

头条、公众号各自有自己独立的草稿目录（publishers.get_toutiao_drafts_dir() /
get_wechat_drafts_dir()，两者默认值互不依赖，见 test_publishers_drafts_dirs.py），这个
路由要同时认这两个目录——落在其中任意一个都放行，都不在的一律拒绝。
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
    return toutiao_dir, wechat_dir


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
