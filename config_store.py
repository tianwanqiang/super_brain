"""
super_brain config_store - config.json 的唯一读写实现 + 配置字段的唯一定义

背景（2026-09 收敛）：以前"读 config.json"这件事散在 7 个模块里，每个模块自己拼一份
`CONFIG_PATH = SUPER_BRAIN / "config.json"`、自己 json.loads——命名繁杂且容易不一致。
收敛后：
- 路径的唯一权威定义在 paths.CONFIG_PATH（只支持 SUPER_BRAIN_CONFIG_PATH 一个环境变量
  覆盖），各模块 `from paths import CONFIG_PATH`，不再自拼；
- 读/写 JSON 的唯一实现在这一个模块，其它模块只在这里 import 函数，不再自己 read_text/
  json.loads/write_text（llm_client 的"mtime 缓存"、ui_app 的"写前备份"都是这里的标准能力）；
- 配置**字段名与无配置时的默认值**也在这里唯一定义（DeepSeek 的
  DEEPSEEK_API_KEY/Model/BaseUrl/MaxTokens 等），消费方只引用常量、不再各自写死字符串。

对外函数：
    read_config(path=None)                    严格读：读不出/不是 JSON 对象就抛 ConfigReadError
    read_config_soft(path=None, cached=False) 宽容读：任何问题返回 {}（原样保留个别模块
                                              "读不到就当没配置"的语义），可选 mtime 缓存
    read_config_cached(path=None)             缓存读：文件没变返回同一份 dict（零 IO），
                                              变了自动重读；失败抛 ConfigReadError（缓存失效）
    write_config(data, path=None)             整文件写回（不备份）
    write_config_with_backup(data, path=None) 写回前先把旧文件备份成 <name>.bak（ui 保存用）

path 参数默认取 paths.CONFIG_PATH；各调用模块把自己模块级的 CONFIG_PATH 传进来（它们
通常是同一个值），这样测试里 monkeypatch 模块的 CONFIG_PATH 仍然能定向到假文件。
ConfigReadError 带 kind（missing/parse/not_object）和 detail，方便上层区分原因给文案。
"""
import json
import logging
from pathlib import Path

from paths import CONFIG_PATH

logger = logging.getLogger("super_brain.config_store")

# ---- 配置字段与默认值的唯一定义（谁要读 config 的某个字段都从常量引用，不写死字符串）----
# DeepSeek 相关（llm_client 消费；其它模块要校验/展示同名 key 也从这里引用）
DEEPSEEK_API_KEY_FIELD = "DEEPSEEK_API_KEY"
DEEPSEEK_MODEL_FIELD = "Model"          # 字段名跟头条 agent（Generate-ToutiaoDraft.ps1）
DEEPSEEK_BASE_URL_FIELD = "BaseUrl"     # 读取的字段名保持一致，改名会影响老配置兼容
DEEPSEEK_MAX_TOKENS_FIELD = "MaxTokens"
MODEL_STRUCTURED_FIELD = "ModelStructured"  # 可选：结构化任务（选题/提取/评分/格式改写）专用模型
DEEPSEEK_MODEL_DEFAULT = "deepseek-v4-pro"
DEEPSEEK_BASE_URL_DEFAULT = "https://api.deepseek.com/v1"
DEEPSEEK_MAX_TOKENS_DEFAULT = 8000

logger = logging.getLogger("super_brain.config_store")

# read_config_cached 的进程内缓存：path -> (签名(mtime_ns,size), dict)
_config_cache: dict[str, tuple[tuple[int, int], dict]] = {}


class ConfigReadError(Exception):
    """config.json 读不出来时抛，按原因区分三类（跟事故复盘定的一致）：

    kind='missing'    → 文件不存在
    kind='parse'      → 文件在但 JSON 解析失败/IO 错误（detail 带原始异常）
    kind='not_object' → 顶层不是 JSON 对象
    """

    def __init__(self, kind: str, path: Path, detail: str = "") -> None:
        self.kind = kind
        self.detail = detail
        self.path = path
        if kind == "missing":
            message = f"找不到配置文件：{path}"
        elif kind == "not_object":
            message = f"{path} 内容不是一个 JSON 对象"
        else:
            message = f"{path} 读取/解析失败：{detail}"
        super().__init__(message)


def _parse_file(path: Path) -> dict:
    if not path.exists():
        raise ConfigReadError("missing", path)
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError) as exc:
        raise ConfigReadError("parse", path, str(exc)) from exc
    if not isinstance(data, dict):
        raise ConfigReadError("not_object", path)
    return data


def read_config(path: Path | str | None = None) -> dict:
    """严格读：读不出就抛 ConfigReadError（上层要"缺配置=报错/给默认"时自行 catch）。"""
    return _parse_file(Path(path or CONFIG_PATH))


def read_config_soft(path: Path | str | None = None, cached: bool = False) -> dict:
    """宽容读：任何问题返回 {}（不抛）——供"读不到就当作没配置、优雅降级"的调用方。
    cached=True 时文件没变化直接命中进程内缓存（只读 stat，不读盘不解析）。"""
    target = Path(path or CONFIG_PATH)
    if cached:
        try:
            return read_config_cached(target)
        except ConfigReadError:
            return {}
    try:
        return _parse_file(target)
    except ConfigReadError:
        return {}


def read_config_cached(path: Path | str | None = None) -> dict:
    """缓存读：mtime+size 没变就返回内存里同一份 dict（一次解析、多处调用，零 IO）；
    文件变了自动重读；读失败抛 ConfigReadError 并清掉该路径缓存，行为等同"每次都现读"
    在坏文件下的表现。这是 llm_client 原来那套缓存逻辑的公共化版本。"""
    target = Path(path or CONFIG_PATH)
    key = str(target)
    try:
        st = target.stat()
    except OSError:
        _config_cache.pop(key, None)
        raise ConfigReadError("missing", target)
    sig = (st.st_mtime_ns, st.st_size)
    if key in _config_cache and _config_cache[key][0] == sig:
        return _config_cache[key][1]
    data = _parse_file(target)  # 失败会抛 ConfigReadError；先清缓存再抛
    _config_cache.pop(key, None)
    _config_cache[key] = (sig, data)
    return data


def write_config(data: dict, path: Path | str | None = None) -> None:
    """整文件写回 config.json（只写 data，不备份）。调用方负责先读后改、不要覆盖丢字段。"""
    target = Path(path or CONFIG_PATH)
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    # 自己刚写过，让缓存失效——下次 cached 读会因 mtime/size 变化自动重读新内容
    _config_cache.pop(str(target), None)
    logger.info(f"config 已写回：{target}")


def write_config_with_backup(data: dict, path: Path | str | None = None) -> None:
    """写回前先把当前文件备份成 <name>.bak（只留最近一份），防止写坏/写丢时还有得救。
    这是 ui_app 原来 _write_config_with_backup 的公共化版本。"""
    target = Path(path or CONFIG_PATH)
    if target.exists():
        try:
            target.replace(target.parent / (target.name + ".bak"))
        except OSError:
            logger.warning(f"备份 {target} 失败，继续写入（不阻塞正常保存流程）")
    write_config(data, target)
    _config_cache.pop(str(target), None)


# 测试友好：缓存可整体清空（monkeypatch 换路径后不会串数据）
def clear_cache() -> None:
    _config_cache.clear()


__all__: list[str] = [
    "CONFIG_PATH", "ConfigReadError", "read_config", "read_config_soft",
    "read_config_cached", "write_config", "write_config_with_backup", "clear_cache",
]
