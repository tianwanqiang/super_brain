"""
super_brain config_check - 校验 config.json 结构，不读取/不打印任何真实凭据的值。

背景：2026-09-04 真实发生过一次事故——服务器上的 config.json 被手动改坏之后，
没有任何环节提前发现，直到用户在前台真的触发一次圆桌讨论，程序才在 8 处
"读 DEEPSEEK_API_KEY 直接拿去用"的代码里随便撞上一处，抛出一段裸的 Python 堆栈。
之前的单元测试全部用临时目录里编造的假 config.json 验证代码逻辑，从没有验证过
"服务器上这份真实文件现在到底是不是健康的"——这是两件不同的事，缺了后者。

这个模块只做一件事：读真实的 config.json（CONFIG_PATH，服务器上是 /app/config.json），
检查它是不是合法 JSON、必需字段是否存在且非空。只输出"存在/缺失/格式对不对"这种是非
结论，绝不输出任何字段的实际值——不违反"不允许读配置文件内容"这条规矩，因为这里
"读"的目的和产出都只是结构性判断，不是把内容暴露给人看。

用法（在服务器/本机 SSH 终端直接跑，不需要真实调用任何付费 API）：
    python config_check.py
退出码：0 = 没有 error 级别的问题（可能有 warning）；1 = 至少一个 error 级别的问题。
"""
import sys
from dataclasses import dataclass

import config_store
from paths import CONFIG_PATH

# (字段名, 严重级别, 缺失时影响的说明) —— error：核心功能会直接崩掉（比如这次真实出问题的
# DEEPSEEK_API_KEY）；warning：对应功能会优雅降级/跳过，不影响其它功能。
REQUIRED_FIELDS = [
    (config_store.DEEPSEEK_API_KEY_FIELD, "error", "圆桌讨论、dispatcher 起草建议、专家私聊、video-prompt 等核心功能全部依赖这个字段，缺失会导致这些功能一调用就报错"),
    ("DASHSCOPE_API_KEY", "warning", "缺失时 RAG 检索用不了，自动降级成整篇 private.md 注入，不影响圆桌讨论本身"),
    ("DASHSCOPE_WORKSPACE_ID", "warning", "同上，跟 DASHSCOPE_API_KEY 是一对，两个都要配才能用 RAG"),
    ("DASHVECTOR_API_KEY", "warning", "同上，RAG 向量数据库那一半凭据"),
    ("DASHVECTOR_ENDPOINT", "warning", "同上，RAG 向量数据库那一半凭据"),
]


@dataclass
class ConfigIssue:
    field: str
    severity: str  # "error" | "warning"
    reason: str


def validate_config_dict(config: dict) -> list[ConfigIssue]:
    """纯逻辑校验，不碰任何文件——传进来的 config 可以是真实文件解析出来的，也可以是
    测试里编造的，这个函数本身不关心来源，方便单独做逻辑单元测试。
    """
    issues: list[ConfigIssue] = []
    for field, severity, impact in REQUIRED_FIELDS:
        value = config.get(field)
        if not value or not isinstance(value, str) or not value.strip():
            issues.append(ConfigIssue(field=field, severity=severity, reason=impact))
    return issues


def load_and_validate() -> tuple[list[ConfigIssue], str | None]:
    """返回 (issues, load_error)。load_error 非空时 issues 一定是空列表——文件都读不出来，
    没法做字段级别的校验，这种情况本身就是最高优先级的问题，直接把读取错误原样报出去。
    读取走 config_store 统一实现（这是唯一真源），按读失败的原因种类给出对应文案。
    """
    try:
        config = config_store.read_config(path=CONFIG_PATH)
    except config_store.ConfigReadError as exc:
        if exc.kind == "missing":
            return [], f"config.json 不存在：{CONFIG_PATH}"
        if exc.kind == "parse":
            return [], f"config.json 读取/解析失败：{exc.detail}"
        return [], "config.json 内容不是一个 JSON 对象（顶层应该是 {...}）"
    return validate_config_dict(config), None


def main() -> int:
    issues, load_error = load_and_validate()

    if load_error:
        print(f"[FAIL] {load_error}")
        return 1

    if not issues:
        print(f"[OK] {CONFIG_PATH} 结构正常，所有必需字段都存在。")
        return 0

    has_error = False
    for issue in issues:
        tag = "FAIL" if issue.severity == "error" else "WARN"
        if issue.severity == "error":
            has_error = True
        print(f"[{tag}] 缺少字段 {issue.field}：{issue.reason}")

    return 1 if has_error else 0


if __name__ == "__main__":
    sys.exit(main())
