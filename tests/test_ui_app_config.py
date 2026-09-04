"""
ui_app.py 里 config.json 读写辅助函数的单元测试——_load_config_for_update() /
_write_config_with_backup() / _normalize_path_input()。

这三个函数是 2026-09-04 修的一个真实数据丢失 bug 的产物：之前"设置会议纪要目录"这类
保存动作，只要读 config.json 失败（文件损坏/临时读取异常）就静默当成空配置，然后把这份
假空配置整个写回文件，把已有的 DASHSCOPE/DASHVECTOR/DEEPSEEK 等 API_KEY 全部覆盖丢失。
这里全程只用临时目录里的假数据，不碰真实 config.json、不读取任何真实凭据。

注意：import ui_app 会触发 Flask app 创建和一长串子模块 import，但不会启动那个真实的
每日 18 点批量汇总后台线程——ui_app.py 里那行 threading.Thread(...).start() 用
`PYTEST_CURRENT_TEST` 环境变量做了跳过（pytest 运行时会自动设置这个变量），专门防止
测试进程意外触发一次真实的、要花钱的 DeepSeek 批量调用。
"""
import json

import pytest

import ui_app


@pytest.fixture
def fake_config_path(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    monkeypatch.setattr(ui_app, "CONFIG_PATH", path)
    return path


def test_load_config_for_update_missing_file_returns_empty_dict_no_error(fake_config_path):
    config, error = ui_app._load_config_for_update()
    assert config == {}
    assert error is None


def test_load_config_for_update_reads_existing_keys(fake_config_path):
    fake_config_path.write_text(
        json.dumps({"DEEPSEEK_API_KEY": "fake-key-123", "OTHER": "x"}), encoding="utf-8"
    )
    config, error = ui_app._load_config_for_update()
    assert error is None
    assert config["DEEPSEEK_API_KEY"] == "fake-key-123"
    assert config["OTHER"] == "x"


def test_load_config_for_update_corrupt_json_returns_none_not_empty_dict(fake_config_path):
    """核心回归断言：损坏的 config.json 必须返回 (None, 错误信息)，绝不能是 ({}, None)——
    返回空字典会被调用方当成"可以安全地在这个基础上写"，导致覆盖丢失真实数据。
    """
    fake_config_path.write_text("{ 这不是合法 json,,,", encoding="utf-8")
    config, error = ui_app._load_config_for_update()
    assert config is None
    assert error is not None


def test_write_config_with_backup_preserves_existing_keys(fake_config_path):
    fake_config_path.write_text(
        json.dumps({"DEEPSEEK_API_KEY": "fake-key-123"}), encoding="utf-8"
    )
    config, _ = ui_app._load_config_for_update()
    config["MEETING_MINUTES_DIR"] = "/opt/super_brain/meeting"
    ui_app._write_config_with_backup(config)

    after = json.loads(fake_config_path.read_text(encoding="utf-8"))
    assert after["DEEPSEEK_API_KEY"] == "fake-key-123"
    assert after["MEETING_MINUTES_DIR"] == "/opt/super_brain/meeting"


def test_write_config_with_backup_creates_bak_file_with_previous_content(fake_config_path):
    fake_config_path.write_text(
        json.dumps({"DEEPSEEK_API_KEY": "fake-key-123"}), encoding="utf-8"
    )
    ui_app._write_config_with_backup({"MEETING_MINUTES_DIR": "/opt/x"})

    backup_path = fake_config_path.parent / (fake_config_path.name + ".bak")
    assert backup_path.exists()
    backup_content = json.loads(backup_path.read_text(encoding="utf-8"))
    assert backup_content["DEEPSEEK_API_KEY"] == "fake-key-123"


def test_write_config_with_backup_first_write_has_no_backup_yet(fake_config_path):
    # config.json 还不存在，第一次写不应该报错，也不需要生成备份文件
    ui_app._write_config_with_backup({"MEETING_MINUTES_DIR": "/opt/x"})
    backup_path = fake_config_path.parent / (fake_config_path.name + ".bak")
    assert not backup_path.exists()
    assert json.loads(fake_config_path.read_text(encoding="utf-8"))["MEETING_MINUTES_DIR"] == "/opt/x"


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("'/opt/super_brain/meeting'", "/opt/super_brain/meeting"),
        ('"/opt/super_brain/meeting"', "/opt/super_brain/meeting"),
        ("/opt/super_brain/meeting", "/opt/super_brain/meeting"),
        ("  '/opt/x'  ", "/opt/x"),
        ("it's/weird", "it's/weird"),  # 中间的单引号不是"两端对称包裹"，不应该被剥掉
        ("'unmatched", "'unmatched"),  # 只有一侧有引号，不剥
        ("", ""),
    ],
)
def test_normalize_path_input_strips_only_matching_surrounding_quotes(raw, expected):
    assert ui_app._normalize_path_input(raw) == expected


class TestPersistenceWarning:
    """_persistence_warning_for_path()：docker-compose.yml 只把 SUPER_BRAIN 这一个目录
    挂载到宿主机磁盘上，配置成其它路径的话，内容会在下次部署重建容器时被清空。这是
    2026-09-04 真实发生过的事故——DEPLOYMENT.md 曾经给的示例路径本身就配错了（宿主机
    路径 /opt/super_brain/... 而不是容器内路径 /app/...）。
    """

    def test_path_under_super_brain_has_no_warning(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ui_app, "SUPER_BRAIN", tmp_path)
        warning = ui_app._persistence_warning_for_path(str(tmp_path / "meeting_minutes"))
        assert warning is None

    def test_super_brain_itself_has_no_warning(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ui_app, "SUPER_BRAIN", tmp_path)
        assert ui_app._persistence_warning_for_path(str(tmp_path)) is None

    def test_path_outside_super_brain_gets_a_warning(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ui_app, "SUPER_BRAIN", tmp_path / "app")
        outside = tmp_path / "somewhere-else" / "meeting_minutes"
        warning = ui_app._persistence_warning_for_path(str(outside))
        assert warning is not None
        assert "部署" in warning

