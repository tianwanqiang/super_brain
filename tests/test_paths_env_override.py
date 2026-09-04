"""
paths.py 环境变量覆盖行为 + log_setup.py 路径联动的回归测试。

用子进程（全新解释器）而不是在同一进程里 monkeypatch 环境变量再 importlib.reload——
paths.py 的常量是模块导入时算好的，同进程里 reload 顺序很容易因为其他模块已经拿着旧
引用而产生假阳性/假阴性，子进程最干净、最接近真实的"设了环境变量再启动进程"场景（对应
Dockerfile 里 `ENV SUPER_BRAIN_DIR=/app` 的真实用法）。

背景：log_setup.py 曾经有一行硬编码的 `Path(r"G:\\code\\super_brain\\logs")`，完全不跟
SUPER_BRAIN_DIR 联动——部署到 Linux 服务器后，因为反斜杠在 Linux 上不是路径分隔符，
真的建出了一个文件名字面包含反斜杠的怪目录（ls 默认引号显示风格会把这种文件名用单引号
包起来，这正是 2026-09-04 真实报出来的现象）。这里把"LOG_DIR 必须跟随 SUPER_BRAIN_DIR"
钉成回归测试。
"""
import os
import subprocess
import sys

import pytest

from paths import SUPER_BRAIN


def _run_snippet(code: str, env_overrides: dict[str, str]) -> str:
    env = os.environ.copy()
    env.update(env_overrides)
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(SUPER_BRAIN),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"子进程执行失败：{result.stderr}"
    return result.stdout.strip()


def test_super_brain_dir_env_var_overrides_default():
    out = _run_snippet(
        "import paths; print(str(paths.SUPER_BRAIN))",
        {"SUPER_BRAIN_DIR": "/opt/super_brain"},
    )
    assert out.replace("\\", "/") == "/opt/super_brain"


def test_super_brain_dir_default_used_when_env_var_unset():
    env = os.environ.copy()
    env.pop("SUPER_BRAIN_DIR", None)
    out = subprocess.run(
        [sys.executable, "-c", "import paths; print(str(paths.SUPER_BRAIN))"],
        cwd=str(SUPER_BRAIN),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()
    assert out  # 没设环境变量时应该有一个非空的本机默认值兜底，不能是空路径


def test_log_dir_follows_super_brain_dir_not_hardcoded():
    """回归测试：log_setup.LOG_DIR 必须等于 SUPER_BRAIN / 'logs'，不能是写死的路径。"""
    out = _run_snippet(
        "import paths, log_setup; "
        "print(str(log_setup.LOG_DIR) == str(paths.SUPER_BRAIN / 'logs'))",
        {"SUPER_BRAIN_DIR": "/opt/super_brain"},
    )
    assert out == "True"


@pytest.mark.skipif(
    os.name != "posix",
    reason="pathlib.Path 在 Windows 上即使传入正斜杠路径，str() 出来也会用反斜杠——"
           "这条断言只有在真正的 POSIX 系统（比如部署目标：Linux 服务器）上才有意义，"
           "在本机 Windows 开发环境跑这条会产生假失败，不是代码有问题。",
)
def test_log_dir_does_not_contain_backslash_when_super_brain_dir_is_posix_style():
    """这是直接对应真实事故现象的断言：Linux 风格的 SUPER_BRAIN_DIR 下，LOG_DIR 里
    不应该出现反斜杠（反斜杠出现说明又变回硬编码 Windows 路径或者路径拼接方式错了）。
    """
    out = _run_snippet(
        "import log_setup; print(str(log_setup.LOG_DIR))",
        {"SUPER_BRAIN_DIR": "/opt/super_brain"},
    )
    assert "\\" not in out
    assert out == "/opt/super_brain/logs"


def test_log_file_is_inside_log_dir():
    out = _run_snippet(
        "import log_setup; print(str(log_setup.LOG_FILE).startswith(str(log_setup.LOG_DIR)))",
        {"SUPER_BRAIN_DIR": "/opt/super_brain"},
    )
    assert out == "True"


def test_deepseek_config_path_defaults_to_super_brains_own_config_json():
    """回归测试：2026-09-04 之前，DEEPSEEK_CONFIG_PATH 本机默认指向兄弟项目 toutiao-agent
    的 config.json，跟 super_brain 自己的 config.json（CONFIG_PATH/config_check.py 校验
    的那份）是两个不同的文件——本机测试"DEEPSEEK_API_KEY 字段是否存在"测的其实是不会被
    真正用到的文件，容易产生假安全感。改成默认就是 SUPER_BRAIN / "config.json"，不再
    隐式依赖另一个项目是否存在/是否配置过。
    """
    out = _run_snippet(
        "import paths; "
        "print(str(paths.DEEPSEEK_CONFIG_PATH) == str(paths.SUPER_BRAIN / 'config.json'))",
        {"SUPER_BRAIN_DIR": "/opt/super_brain"},
    )
    assert out == "True"


def test_deepseek_config_path_still_overridable_via_env_var():
    """显式设置的 DEEPSEEK_CONFIG_PATH 环境变量必须继续生效——不能因为改了默认值就把
    "可以覆盖成别的文件"这个能力也一并丢掉。
    """
    out = _run_snippet(
        "import paths; print(str(paths.DEEPSEEK_CONFIG_PATH))",
        {"SUPER_BRAIN_DIR": "/opt/super_brain", "DEEPSEEK_CONFIG_PATH": "/somewhere/else/config.json"},
    )
    assert out.replace("\\", "/") == "/somewhere/else/config.json"
