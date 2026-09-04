"""
/config/set 和 /config/set-toutiao-drafts-dir 的操作结果反馈——2026-09-04 被要求：
"点击了保存后，需要提示操作结果"、"如果报错需要返回错误信息"、"每个接口调用必须返回
操作结果，有问题则返回，没问题返回成功"。之前这两个路由保存成功后是纯静默重定向，
用户没有任何反馈；空值提交、mkdir 失败也只是写日志、页面上什么都不显示。

这里用 Flask 测试客户端跑真实的 HTTP 请求（不是直接调用视图函数），因为要验证的是
"session 里到底有没有被设进正确的提示信息"这个跨请求的行为，用测试客户端比直接调用
视图函数更贴近真实场景。全程用临时 config.json，不碰真实文件。
"""
import json

import pytest

import ui_app


@pytest.fixture
def client():
    ui_app.app.config["TESTING"] = True
    with ui_app.app.test_client() as c:
        yield c


@pytest.fixture
def fake_config_path(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    monkeypatch.setattr(ui_app, "CONFIG_PATH", path)
    return path


@pytest.fixture
def under_super_brain(tmp_path, monkeypatch):
    """_persistence_warning_for_path() 要求路径落在 SUPER_BRAIN 之下才不报警告——
    测试用的目录也放在这个假的 SUPER_BRAIN 下面，避免每条用例都被那条警告污染。
    """
    fake_super_brain = tmp_path / "super_brain_root"
    fake_super_brain.mkdir()
    monkeypatch.setattr(ui_app, "SUPER_BRAIN", fake_super_brain)
    return fake_super_brain


def test_meeting_minutes_dir_save_success_shows_success_banner(client, fake_config_path, under_super_brain):
    target = under_super_brain / "meeting"
    resp = client.post("/config/set", data={"meeting_minutes_dir": str(target)}, follow_redirects=False)
    assert resp.status_code == 302

    with client.session_transaction() as sess:
        assert "已保存" in (sess.get("config_success") or "")
        assert not sess.get("roundtable_error")

    assert target.is_dir()
    saved = json.loads(fake_config_path.read_text(encoding="utf-8"))
    assert saved["MEETING_MINUTES_DIR"] == str(target)


def test_meeting_minutes_dir_empty_value_returns_error(client, fake_config_path, under_super_brain):
    resp = client.post("/config/set", data={"meeting_minutes_dir": "  "}, follow_redirects=False)
    assert resp.status_code == 302
    with client.session_transaction() as sess:
        assert "不能为空" in (sess.get("roundtable_error") or "")
        assert not sess.get("config_success")


def test_meeting_minutes_dir_mkdir_failure_returns_error_not_silent(client, fake_config_path, under_super_brain, monkeypatch):
    """mkdir 失败（比如路径非法/没权限）之前完全没被捕获过，会员直接抛出 500——现在必须
    变成一条清楚的错误提示，且不能把这个坏值写进 config.json。
    """
    def _raise_oserror(*args, **kwargs):
        raise OSError("模拟权限不足")

    from pathlib import Path
    monkeypatch.setattr(Path, "mkdir", _raise_oserror)

    resp = client.post("/config/set", data={"meeting_minutes_dir": "/whatever"}, follow_redirects=False)
    assert resp.status_code == 302
    with client.session_transaction() as sess:
        assert "创建失败" in (sess.get("roundtable_error") or "")
        assert not sess.get("config_success")
    assert not fake_config_path.exists(), "mkdir 失败时不应该写入 config.json"


def test_toutiao_drafts_dir_save_success_shows_success_banner(client, fake_config_path, under_super_brain):
    target = under_super_brain / "toutiao"
    resp = client.post("/config/set-toutiao-drafts-dir", data={"toutiao_drafts_dir": str(target)}, follow_redirects=False)
    assert resp.status_code == 302
    with client.session_transaction() as sess:
        assert "已保存" in (sess.get("config_success") or "")


def test_toutiao_drafts_dir_empty_value_returns_error(client, fake_config_path, under_super_brain):
    resp = client.post("/config/set-toutiao-drafts-dir", data={"toutiao_drafts_dir": ""}, follow_redirects=False)
    assert resp.status_code == 302
    with client.session_transaction() as sess:
        assert "不能为空" in (sess.get("roundtable_error") or "")


def test_meeting_minutes_dir_outside_super_brain_shows_persistence_warning_not_success(client, fake_config_path, tmp_path, under_super_brain):
    """路径合法、能创建成功，但不在持久化目录下——应该是警告（roundtable_error 复用这个
    展示位），而不是普通的成功提示，避免用户误以为"保存了就万事大吉"。
    """
    outside = tmp_path / "somewhere-outside"
    resp = client.post("/config/set", data={"meeting_minutes_dir": str(outside)}, follow_redirects=False)
    assert resp.status_code == 302
    with client.session_transaction() as sess:
        assert "部署" in (sess.get("roundtable_error") or "")
        assert not sess.get("config_success")
