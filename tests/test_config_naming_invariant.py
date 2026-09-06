"""
config 命名统一的不变量测试（静态扫描源码 AST，不碰真实文件/凭据）。

钉死的规则（2026-09 收敛目标，防止回潮）：
1. 全仓库只允许 paths.CONFIG_PATH 这一个指向 config.json 的 Python 名字：
   - 不允许任何地方再出现 `CONFIG_PATH = SUPER_BRAIN / "config.json"` 式的自拼；
   - 不允许再出现 DEEPSEEK_CONFIG_PATH 这个 Python 符号（历史别名已删除；
     同名**环境变量**仍受支持，但那只是运行时配置，不是代码里的变量）。
2. 读取 config.json 的 JSON 解析只允许发生在 config_store.py（唯一实现）。
   （第二点在 test_config_store 的行为测试里覆盖；这里覆盖"命名"这一层。）

注释/文档字符串里的历史描述不算数，只扫可执行代码。
"""
import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# 允许出现构造逻辑/历史描述的文件：paths.py 是唯一定义 CONFIG_PATH 的地方（它的默认值
# 当然要拼一次 SUPER_BRAIN / "config.json"，这是定义本身）；config_store 模块 docstring
# 里保留了历史描述。除此之外任何地方都不允许重复定义/自拼/旧符号。
_DEFINITION_ALLOWLIST = {"paths.py", "config_store.py"}


def _py_files() -> list[Path]:
    files = sorted(REPO_ROOT.glob("*.py")) + sorted((REPO_ROOT / "tests").glob("*.py"))
    return [p for p in files if p.name not in ("__init__.py",)]


def _is_module_docstring(node: ast.AST, lines: list[str]) -> bool:
    """粗略判断该节点是否落在模块 docstring 范围内（只用于减少误报，规则本身看 AST 更严）。"""
    # 简单实现：凡第一段连续字符串表达式（Expr/Constant str）都算文档——够用即可
    return isinstance(node, ast.Expr) and isinstance(getattr(node, "value", None), ast.Constant) \
        and isinstance(node.value.value, str)


def test_no_self_built_config_path_and_no_legacy_symbol():
    hits: list[str] = []
    for path in _py_files():
        if path.name in _DEFINITION_ALLOWLIST:
            continue  # paths.py：唯一定义处；config_store.py：docstring 含历史描述
        try:
            tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            # 规则 1a：CONFIG_PATH = <除法拼路径>（SUPER_BRAIN / "config.json" 之类）
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "CONFIG_PATH":
                        value = node.value
                        if isinstance(value, ast.BinOp) and isinstance(value.op, ast.Div):
                            hits.append(f"{path.name}: CONFIG_PATH 被重新拼路径（应 import paths.CONFIG_PATH）")
            # 规则 1b：DEEPSEEK_CONFIG_PATH 作为符号出现（import/属性/变量）
            if isinstance(node, ast.Attribute) and node.attr == "DEEPSEEK_CONFIG_PATH":
                hits.append(f"{path.name}: 仍引用已删除的 DEEPSEEK_CONFIG_PATH 符号")
            if isinstance(node, ast.Name) and node.id == "DEEPSEEK_CONFIG_PATH":
                hits.append(f"{path.name}: 仍出现 DEEPSEEK_CONFIG_PATH 变量名")
            if isinstance(node, ast.ImportFrom) and any(
                a.name == "DEEPSEEK_CONFIG_PATH" for a in node.names
            ):
                hits.append(f"{path.name}: 仍在 import DEEPSEEK_CONFIG_PATH")
    assert not hits, "\n".join(hits)


def test_no_raw_config_json_path_construction_outside_config_store():
    """即使不叫 CONFIG_PATH，也不允许别处再写死 SUPER_BRAIN / "config.json" 构造路径。"""
    hits: list[str] = []
    for path in _py_files():
        if path.name in _DEFINITION_ALLOWLIST:
            continue  # paths.py：唯一定义处；config_store.py：docstring 含历史描述
        try:
            tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
                left = node.left
                if (isinstance(left, ast.Name) and left.id == "SUPER_BRAIN"
                        and isinstance(node.right, ast.Constant)
                        and node.right.value == "config.json"):
                    hits.append(f"{path.name}: 直接拼 SUPER_BRAIN / 'config.json'（应 import paths.CONFIG_PATH）")
    assert not hits, "\n".join(hits)
