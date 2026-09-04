"""
config_check.py 的测试，分两类，边界要说清楚，别再让"测试通过"听起来像是在担保一件它
没有能力担保的事：

1. validate_config_dict() 的纯逻辑测试——传入编造的 dict，测的是"规则本身对不对"
   （缺 DEEPSEEK_API_KEY 该报 error、缺 DASHSCOPE/DASHVECTOR 该报 warning、字段是空
   字符串要当成缺失处理），不碰任何真实文件，这部分历来就是这么测的。

2. test_real_local_config_if_present——这条不用假数据，直接读这台机器上真实的
   config.json（CONFIG_PATH，未被 monkeypatch）。如果这台机器上真的有 config.json
   （比如本机开发环境、或者真的在服务器上跑 pytest），这条测试会对着真实文件跑，
   真实反映它现在健不健康。如果这台机器上根本没有 config.json（GitHub Actions 的云端
   runner 就是这种情况——它检出的是一份干净仓库，config.json 从来不会、也不应该进
   版本控制），这条测试会跳过，并且跳过原因写得很明确，不是静默通过。

   真正能保证"部署前这一步不会被跳过"的，不是这条 pytest 测试（它在 GitHub Actions
   跑的环境里，物理上不可能拿到真实文件），而是 .github/workflows/deploy.yml 里新加的
   那一步——直接在服务器上、用服务器上真实的 config.json 跑 config_check.py，检查不过
   就不会往下执行 docker compose 重启服务。这条 pytest 测试只是给本机开发/手动 SSH
   上服务器跑 pytest 时提供的一个额外信号，不是主要的把关点。
"""
import pytest

from config_check import CONFIG_PATH, load_and_validate, validate_config_dict


def test_all_fields_present_produces_no_issues():
    config = {
        "DEEPSEEK_API_KEY": "x",
        "DASHSCOPE_API_KEY": "x",
        "DASHSCOPE_WORKSPACE_ID": "x",
        "DASHVECTOR_API_KEY": "x",
        "DASHVECTOR_ENDPOINT": "x",
    }
    assert validate_config_dict(config) == []


def test_missing_deepseek_api_key_is_error_severity():
    issues = validate_config_dict({})
    deepseek_issues = [i for i in issues if i.field == "DEEPSEEK_API_KEY"]
    assert len(deepseek_issues) == 1
    assert deepseek_issues[0].severity == "error"


def test_missing_dashscope_field_is_warning_severity_not_error():
    config = {"DEEPSEEK_API_KEY": "x"}  # 只缺 RAG 相关的可选字段
    issues = validate_config_dict(config)
    assert all(i.severity == "warning" for i in issues)
    assert {i.field for i in issues} == {
        "DASHSCOPE_API_KEY", "DASHSCOPE_WORKSPACE_ID",
        "DASHVECTOR_API_KEY", "DASHVECTOR_ENDPOINT",
    }


@pytest.mark.parametrize("empty_value", ["", "   ", None])
def test_empty_or_none_value_counts_as_missing(empty_value):
    config = {"DEEPSEEK_API_KEY": empty_value}
    issues = validate_config_dict(config)
    assert any(i.field == "DEEPSEEK_API_KEY" for i in issues)


def test_non_string_value_counts_as_missing():
    """真实发生过手动改坏 JSON 的先例——防一手比如把值写成数字/布尔/数组这类明显不对的类型。"""
    config = {"DEEPSEEK_API_KEY": 12345}
    issues = validate_config_dict(config)
    assert any(i.field == "DEEPSEEK_API_KEY" for i in issues)


def test_real_local_config_if_present():
    """不 monkeypatch CONFIG_PATH——直接读这台机器上真实的 config.json。"""
    if not CONFIG_PATH.exists():
        pytest.skip(
            f"{CONFIG_PATH} 在这台机器上不存在（预期情况，比如 CI 云端 runner 上本来就不会有"
            "真实凭据文件）——这条测试没有验证任何东西，不代表配置是健康的，"
            "真正的把关点在服务器部署脚本里对真实文件的检查。"
        )
    issues, load_error = load_and_validate()
    assert load_error is None, f"config.json 读取/解析失败：{load_error}"
    error_issues = [i for i in issues if i.severity == "error"]
    assert not error_issues, f"这台机器上真实的 config.json 缺少必需字段：{[i.field for i in error_issues]}"
