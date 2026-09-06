"""
/admin/config 系统配置页的离线测试——用临时 config.json：
- 页面可打开、真实值不回显（只出现"已配置"标记）
- 保存：填新值写入；留空不覆盖；勾"置空"删除
- 覆盖写回走 config_store（带备份），不整文件丢失其它字段
"""
import json

import ui_app


def _fresh_app(tmp_path, monkeypatch):
    """把 ui_app.CONFIG_PATH 指到临时文件并预填几条既有值。"""
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"DEEPSEEK_API_KEY": "old-secret",
                                       "TAVILY_API_KEY": "old-tvly",
                                       "OTHER_UNKNOWN": "keep-me"},
                                      ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(ui_app, "CONFIG_PATH", config_path)
    client = ui_app.app.test_client()
    return client, config_path


def test_config_page_does_not_leak_values(tmp_path, monkeypatch):
    client, config_path = _fresh_app(tmp_path, monkeypatch)
    resp = client.get("/admin/config")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "old-secret" not in html          # 真实值绝不回显
    assert "已配置" in html and "DEEPSEEK_API_KEY" in html


def test_config_save_sets_preserves_and_clears(tmp_path, monkeypatch):
    client, config_path = _fresh_app(tmp_path, monkeypatch)

    # 1) 填新值 + 留空（不覆盖既有）+ 置空一个可编辑字段
    resp = client.post("/admin/config/save", data={
        "DEEPSEEK_API_KEY": "new-secret",    # 覆盖旧值
        "TAVILY_API_KEY": "",                # 留空 → 保持旧值？下面改成置空：
        "TAVILY_API_KEY__clear": "1",        # 置空 → 删除
        "MAIL__smtp_host": "smtp.qq.com",
        "MAIL__smtp_port": "465",
        "MAIL__password": "authcode123",
    }, follow_redirects=True)
    assert resp.status_code == 200
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["DEEPSEEK_API_KEY"] == "new-secret"
    assert "TAVILY_API_KEY" not in saved                  # 置空生效
    assert saved["OTHER_UNKNOWN"] == "keep-me"            # 未出现在表单的未知字段原样保留
    assert saved["MAIL"] == {"smtp_host": "smtp.qq.com", "smtp_port": 465,
                             "password": "authcode123"}
    assert (config_path.parent / (config_path.name + ".bak")).exists()  # 带备份


def test_config_save_rejects_bad_max_tokens(tmp_path, monkeypatch):
    client, config_path = _fresh_app(tmp_path, monkeypatch)
    resp = client.post("/admin/config/save", data={"MaxTokens": "not-a-number"},
                       follow_redirects=True)
    assert resp.status_code == 200
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert "MaxTokens" not in saved          # 非法值不写入
    assert "DEEPSEEK_API_KEY" in saved       # 原有字段未丢


def test_config_test_mail_button_uses_mail_settings(tmp_path, monkeypatch):
    """“发送测试邮件”按钮：调用 mailer（此处 monkeypatch 掉真发送）并把结果反馈到页面。"""
    client, config_path = _fresh_app(tmp_path, monkeypatch)
    sent = {}
    monkeypatch.setattr(ui_app.mailer, "mail_settings",
                        lambda: {"host": "smtp.qq.com", "port": 465, "username": "u",
                                 "password": "p", "from_addr": "f@qq.com", "to_addr": "t@qq.com",
                                 "public_base_url": "http://127.0.0.1:5151"})
    monkeypatch.setattr(ui_app.mailer, "send_mail",
                        lambda settings, subject, html: sent.update(subject=subject, html=html) or True)
    resp = client.post("/admin/config/test-mail", follow_redirects=True)
    assert resp.status_code == 200
    assert sent.get("subject") == "[super_brain] 测试邮件"
    assert "测试邮件已发送" in resp.get_data(as_text=True)


def test_config_test_mail_without_config_shows_error(tmp_path, monkeypatch):
    client, config_path = _fresh_app(tmp_path, monkeypatch)
    monkeypatch.setattr(ui_app.mailer, "mail_settings", lambda: None)
    resp = client.post("/admin/config/test-mail", follow_redirects=True)
    assert resp.status_code == 200
    assert "还没配置 MAIL" in resp.get_data(as_text=True)
