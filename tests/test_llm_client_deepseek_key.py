"""
llm_client.load_deepseek_api_key() 的单元测试——2026-09-04 把原来分散在 9 个调用点
（roundtable.py x2、ui_app.py x4、video_prompt.py、private_chat.py、dispatcher.py）
的重复读取逻辑收拢到这一处之后的回归测试。全程用临时目录里的假 config.json，不碰真实
凭据，也不发起任何真实的 DeepSeek 调用——这里只测"key 加载对不对"，不测"key 能不能
真的用"（那部分必须用真实 key、由用户自己验证）。
"""
import json

import pytest

import llm_client


@pytest.fixture
def fake_config_path(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    monkeypatch.setattr(llm_client, "DEEPSEEK_CONFIG_PATH", path)
    return path


def test_returns_the_configured_key(fake_config_path):
    fake_config_path.write_text(json.dumps({"DEEPSEEK_API_KEY": "fake-key-abc"}), encoding="utf-8")
    assert llm_client.load_deepseek_api_key() == "fake-key-abc"


def test_missing_file_raises_deepseek_config_error(fake_config_path):
    with pytest.raises(llm_client.DeepSeekConfigError, match="找不到"):
        llm_client.load_deepseek_api_key()


def test_corrupt_json_raises_deepseek_config_error_not_a_raw_traceback(fake_config_path):
    """回归这次真实事故的核心诉求：不能让 JSONDecodeError 原样冒出来，必须是清楚的
    DeepSeekConfigError，调用方才知道具体是"文件坏了"而不是别的问题。
    """
    fake_config_path.write_text("{ 手滑改坏的 json,,,", encoding="utf-8")
    with pytest.raises(llm_client.DeepSeekConfigError, match="读取/解析失败"):
        llm_client.load_deepseek_api_key()


def test_missing_key_field_raises_deepseek_config_error_not_keyerror(fake_config_path):
    """回归这次真实撞上的具体场景：合法 JSON，但没有 DEEPSEEK_API_KEY 这个字段——
    之前会抛裸的 KeyError，现在必须是带清楚原因的 DeepSeekConfigError。
    """
    fake_config_path.write_text(json.dumps({"OTHER_FIELD": "x"}), encoding="utf-8")
    with pytest.raises(llm_client.DeepSeekConfigError, match="DEEPSEEK_API_KEY"):
        llm_client.load_deepseek_api_key()


def test_empty_string_key_value_raises_deepseek_config_error(fake_config_path):
    fake_config_path.write_text(json.dumps({"DEEPSEEK_API_KEY": ""}), encoding="utf-8")
    with pytest.raises(llm_client.DeepSeekConfigError):
        llm_client.load_deepseek_api_key()


def test_non_dict_json_raises_deepseek_config_error(fake_config_path):
    fake_config_path.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
    with pytest.raises(llm_client.DeepSeekConfigError, match="不是一个 JSON 对象"):
        llm_client.load_deepseek_api_key()
