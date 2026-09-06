"""
config_store.py（config.json 唯一读写实现）的单元测试——全程 tmp 假文件：
严格读/宽容读/缓存失效/带备份写回/缓存清空。
"""
import json

import pytest

import config_store


@pytest.fixture
def cfg(tmp_path):
    return tmp_path / "config.json"


def test_strict_read_returns_dict(cfg):
    cfg.write_text(json.dumps({"A": 1}), encoding="utf-8")
    assert config_store.read_config(cfg) == {"A": 1}


def test_strict_read_missing_raises_with_kind(cfg):
    with pytest.raises(config_store.ConfigReadError) as excinfo:
        config_store.read_config(cfg)
    assert excinfo.value.kind == "missing"
    assert "找不到配置文件" in str(excinfo.value)


def test_strict_read_corrupt_raises_parse_kind(cfg):
    cfg.write_text("{ 手滑改坏的 json,,,", encoding="utf-8")
    with pytest.raises(config_store.ConfigReadError) as excinfo:
        config_store.read_config(cfg)
    assert excinfo.value.kind == "parse"
    assert "读取/解析失败" in str(excinfo.value)


def test_strict_read_non_object_raises(cfg):
    cfg.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
    with pytest.raises(config_store.ConfigReadError) as excinfo:
        config_store.read_config(cfg)
    assert excinfo.value.kind == "not_object"
    assert "不是一个 JSON 对象" in str(excinfo.value)


def test_soft_read_returns_empty_on_any_problem(cfg):
    assert config_store.read_config_soft(cfg) == {}
    cfg.write_text("{ bad json", encoding="utf-8")
    assert config_store.read_config_soft(cfg) == {}


def test_cached_read_same_object_then_invalidated_on_change(cfg):
    config_store.clear_cache()
    cfg.write_text(json.dumps({"K": "v1"}), encoding="utf-8")
    first = config_store.read_config_cached(cfg)
    assert config_store.read_config_cached(cfg) is first       # 命中缓存：同一对象，零 IO
    cfg.write_text(json.dumps({"K": "v2-much-longer"}), encoding="utf-8")
    second = config_store.read_config_cached(cfg)
    assert second is not first and second["K"] == "v2-much-longer"


def test_cached_read_missing_clears_cache_and_raises(cfg):
    config_store.clear_cache()
    cfg.write_text(json.dumps({"K": "v1"}), encoding="utf-8")
    config_store.read_config_cached(cfg)
    cfg.unlink()
    with pytest.raises(config_store.ConfigReadError):
        config_store.read_config_cached(cfg)
    cfg.write_text(json.dumps({"K": "v2"}), encoding="utf-8")   # 恢复后能重新读
    assert config_store.read_config_cached(cfg)["K"] == "v2"


def test_write_config_returns_latest_on_next_cached_read(cfg):
    config_store.clear_cache()
    cfg.write_text(json.dumps({"K": "old"}), encoding="utf-8")
    config_store.read_config_cached(cfg)
    config_store.write_config({"K": "new", "L": 2}, cfg)
    data = config_store.read_config_cached(cfg)                # 自己写完后缓存被失效
    assert data == {"K": "new", "L": 2}


def test_write_with_backup_keeps_previous_content(cfg):
    cfg.write_text(json.dumps({"K": "old"}), encoding="utf-8")
    config_store.write_config_with_backup({"K": "new"}, cfg)
    backup = cfg.parent / (cfg.name + ".bak")
    assert backup.exists()
    assert json.loads(backup.read_text(encoding="utf-8-sig")) == {"K": "old"}
    assert json.loads(cfg.read_text(encoding="utf-8-sig")) == {"K": "new"}


def test_clear_cache_removes_entries(cfg):
    config_store.clear_cache()
    cfg.write_text(json.dumps({"K": 1}), encoding="utf-8")
    config_store.read_config_cached(cfg)
    config_store.clear_cache()
    assert config_store._config_cache == {}
