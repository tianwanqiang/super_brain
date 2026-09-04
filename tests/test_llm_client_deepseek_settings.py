"""
llm_client.load_deepseek_settings() 的测试——Model/BaseUrl/MaxTokens 三个可选字段，
配置逻辑直接照抄头条 agent（G:\\code\\toutiao-agent\\Generate-ToutiaoDraft.ps1）：
字段存在且非空就用配置值覆盖内置默认值，缺了/是空值就静默用内置默认值。

分两类，边界跟 test_config_validation.py 一样说清楚：
1. 纯逻辑测试——编造的假 config，测规则本身对不对。
2. test_real_local_config_settings_are_loadable_without_error——不 mock 任何东西，直接
   对这台机器上真实的 config.json 跑 load_deepseek_settings()，确认"agent 运行时真的能
   从这份真实文件里取到配置"，而不是只在编造数据上测试通过。这条不会因为 Model/BaseUrl/
   MaxTokens 是可选字段就跳过——不管配没配，都应该能拿到一组可用的设置，不会报错、
   不会返回 None/空值；只有在真实 config.json 完全不存在这台机器上时才跳过（比如 CI
   云端 runner），原因写得很明确，不是静默通过。
"""
import json

import pytest

import llm_client


@pytest.fixture
def fake_config_path(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    monkeypatch.setattr(llm_client, "DEEPSEEK_CONFIG_PATH", path)
    return path


def test_no_config_file_returns_all_defaults(fake_config_path):
    settings = llm_client.load_deepseek_settings()
    assert settings == {
        "model": llm_client.DEEPSEEK_MODEL_DEFAULT,
        "base_url": llm_client.DEEPSEEK_BASE_URL_DEFAULT,
        "max_tokens": llm_client.DEEPSEEK_MAX_TOKENS_DEFAULT,
    }


def test_configured_model_overrides_default(fake_config_path):
    fake_config_path.write_text(json.dumps({"Model": "deepseek-chat"}), encoding="utf-8")
    settings = llm_client.load_deepseek_settings()
    assert settings["model"] == "deepseek-chat"
    # 没配置的字段仍然是默认值，不会因为配了 Model 就一起变
    assert settings["base_url"] == llm_client.DEEPSEEK_BASE_URL_DEFAULT
    assert settings["max_tokens"] == llm_client.DEEPSEEK_MAX_TOKENS_DEFAULT


def test_configured_base_url_and_max_tokens_override_defaults(fake_config_path):
    fake_config_path.write_text(
        json.dumps({"BaseUrl": "https://example.com/v1", "MaxTokens": 4000}), encoding="utf-8"
    )
    settings = llm_client.load_deepseek_settings()
    assert settings["base_url"] == "https://example.com/v1"
    assert settings["max_tokens"] == 4000


def test_empty_string_fields_fall_back_to_defaults(fake_config_path):
    """空字符串按头条 agent 的逻辑（$config.Model 的真值判断）应该被当成"没配置"，
    不能把空字符串当成真的模型名传给 API。
    """
    fake_config_path.write_text(json.dumps({"Model": "", "BaseUrl": ""}), encoding="utf-8")
    settings = llm_client.load_deepseek_settings()
    assert settings["model"] == llm_client.DEEPSEEK_MODEL_DEFAULT
    assert settings["base_url"] == llm_client.DEEPSEEK_BASE_URL_DEFAULT


def test_non_numeric_max_tokens_falls_back_to_default_not_a_crash(fake_config_path):
    fake_config_path.write_text(json.dumps({"MaxTokens": "not-a-number"}), encoding="utf-8")
    settings = llm_client.load_deepseek_settings()
    assert settings["max_tokens"] == llm_client.DEEPSEEK_MAX_TOKENS_DEFAULT


def test_corrupt_json_falls_back_to_all_defaults_not_a_crash(fake_config_path):
    fake_config_path.write_text("{ 手滑改坏的 json,,,", encoding="utf-8")
    settings = llm_client.load_deepseek_settings()
    assert settings["model"] == llm_client.DEEPSEEK_MODEL_DEFAULT


def test_max_tokens_as_string_number_is_coerced(fake_config_path):
    """JSON 里数字被误写成字符串（比如 "8000"）是常见的手改失误，应该能兼容，不是直接
    当成非法值丢弃——跟头条 agent 那边 [int]$config.MaxTokens 的强制转换语义一致。
    """
    fake_config_path.write_text(json.dumps({"MaxTokens": "6000"}), encoding="utf-8")
    settings = llm_client.load_deepseek_settings()
    assert settings["max_tokens"] == 6000


def test_real_local_config_settings_are_loadable_without_error():
    """不 monkeypatch DEEPSEEK_CONFIG_PATH——直接对这台机器上真实的 config.json 跑。
    Model/BaseUrl/MaxTokens 都是可选字段，所以哪怕真实文件里完全没配这三个，这条测试
    也应该通过（拿到一组内置默认值）；只有在这台机器上真的没有 config.json 文件本身
    时才跳过（预期情况，比如 GitHub Actions 云端 runner）。
    """
    if not llm_client.DEEPSEEK_CONFIG_PATH.exists():
        pytest.skip(
            f"{llm_client.DEEPSEEK_CONFIG_PATH} 在这台机器上不存在（预期情况，比如 CI 云端"
            "runner 上本来就不会有真实凭据文件）——这条测试没有验证任何东西，真正的把关点"
            "在服务器部署脚本对真实文件的检查（config_check.py）。"
        )
    settings = llm_client.load_deepseek_settings()
    assert isinstance(settings["model"], str) and settings["model"]
    assert isinstance(settings["base_url"], str) and settings["base_url"]
    assert isinstance(settings["max_tokens"], int) and settings["max_tokens"] > 0


def test_call_deepseek_explicit_argument_wins_over_config(fake_config_path, monkeypatch):
    """显式传参数的调用方（比如 Round 3 结论收敛用 max_tokens=3000）优先级必须高于
    config.json 里的值，不能被配置覆盖——这是这次重构最容易破坏的行为，专门钉一条。
    """
    fake_config_path.write_text(json.dumps({"Model": "from-config", "MaxTokens": 9999}), encoding="utf-8")

    captured = {}

    def fake_core(messages, api_key, model, base_url, max_tokens):
        captured["model"] = model
        captured["max_tokens"] = max_tokens
        return "ok"

    monkeypatch.setattr(llm_client, "_call_deepseek_core", fake_core)
    llm_client.call_deepseek("sys", "user", "fake-api-key", model="explicit-model", max_tokens=1234)

    assert captured["model"] == "explicit-model"
    assert captured["max_tokens"] == 1234


def test_call_deepseek_falls_back_to_config_when_not_passed(fake_config_path, monkeypatch):
    fake_config_path.write_text(json.dumps({"Model": "from-config", "MaxTokens": 9999}), encoding="utf-8")

    captured = {}

    def fake_core(messages, api_key, model, base_url, max_tokens):
        captured["model"] = model
        captured["max_tokens"] = max_tokens
        return "ok"

    monkeypatch.setattr(llm_client, "_call_deepseek_core", fake_core)
    llm_client.call_deepseek("sys", "user", "fake-api-key")

    assert captured["model"] == "from-config"
    assert captured["max_tokens"] == 9999
