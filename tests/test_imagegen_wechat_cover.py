"""
公众号 MVP：封面生图 + 公网链接 + 草稿发布带上封面 —— 离线测试
- 纯 Python PNG 生成（文件头合法、尺寸正确）
- public_url_for 拼公网链接
- ensure_wechat_cover：已配封面 URL → 直接用；未配 → 自动生成本地封面+链接
- publishers.publish_wechat_draft 支持显式 cover_url（微信上传/建稿用 monkeypatch，零网络）
"""
import json

import pytest

import config_store
import imagegen
import publishers


@pytest.fixture(autouse=True)
def isolated_covers(tmp_path, monkeypatch):
    monkeypatch.setattr(imagegen, "COVERS_DIR", tmp_path / "covers")
    return tmp_path


# ---------- PNG 生成 ----------

def test_generate_cover_produces_valid_png(isolated_covers):
    path = imagegen.generate_cover("测试封面标题")
    assert path.exists()
    header = path.read_bytes()[:8]
    assert header == b"\x89PNG\r\n\x1a\n"          # PNG 魔数
    assert path.stat().st_size < 100_000


def test_public_url_for_uses_covers_path():
    url = imagegen.public_url_for(imagegen.COVERS_DIR / "a.png",
                                  {"PUBLIC_BASE_URL": "https://brain.example.com"})
    assert url == "https://brain.example.com/static/covers/a.png"


# ---------- 封面选择规则 ----------

def test_ensure_wechat_cover_uses_configured_url(monkeypatch, isolated_covers):
    monkeypatch.setattr(config_store, "read_config_soft",
                        lambda path=None, cached=False: {"WECHAT_DEFAULT_COVER_URL": "https://cdn/x.png"})
    monkeypatch.setattr(imagegen, "generate_cover",
                        lambda title="", slug="": (_ for _ in ()).throw(AssertionError("不应生图")))
    local, url = imagegen.ensure_wechat_cover("标题")
    assert local is None and url == "https://cdn/x.png"


def test_ensure_wechat_cover_generates_when_missing(monkeypatch, isolated_covers):
    monkeypatch.setattr(config_store, "read_config_soft",
                        lambda path=None, cached=False: {"PUBLIC_BASE_URL": "http://pub:5151"})
    local, url = imagegen.ensure_wechat_cover("自动标题")
    assert local is not None and local.exists()
    assert url.startswith("http://pub:5151/static/covers/cover_")
    assert url.endswith(".png")


def test_public_base_url_falls_back_to_mail_then_localhost(monkeypatch):
    monkeypatch.setattr(config_store, "read_config_soft",
                        lambda path=None, cached=False: {"MAIL": {"public_base_url": "http://mail:1"}})
    assert imagegen.public_base_url() == "http://mail:1"
    monkeypatch.setattr(config_store, "read_config_soft",
                        lambda path=None, cached=False: {})
    assert imagegen.public_base_url() == "http://127.0.0.1:5151"


# ---------- 草稿发布带上封面 ----------

def _patch_wechat_network(monkeypatch, tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"WECHAT_APP_ID": "id", "WECHAT_APP_KEY": "key"}),
                           encoding="utf-8")
    monkeypatch.setattr(publishers, "CONFIG_PATH", config_path)
    captured = {}
    monkeypatch.setattr(publishers, "wechat_get_access_token", lambda *a, **k: "tok")
    monkeypatch.setattr(publishers, "wechat_upload_thumb",
                        lambda tok, url: captured.update(cover=url) or "thumb-id")
    monkeypatch.setattr(publishers, "wechat_create_draft",
                        lambda tok, thumb, title, html: captured.update(title=title) or "draft-1")
    return captured


def test_publish_wechat_draft_uses_explicit_cover_url(monkeypatch, tmp_path):
    captured = _patch_wechat_network(monkeypatch, tmp_path)
    result = publishers.publish_wechat_draft("标题", "<p>正文</p>", cover_url="http://pub/cover.png")
    assert captured["cover"] == "http://pub/cover.png"
    assert result["draft_media_id"] == "draft-1"


def test_publish_wechat_draft_raises_when_no_cover_anywhere(monkeypatch, tmp_path):
    _patch_wechat_network(monkeypatch, tmp_path)   # config 里没 WECHAT_DEFAULT_COVER_URL
    with pytest.raises(publishers.PublishError, match="需要封面"):
        publishers.publish_wechat_draft("标题", "<p>正文</p>")
