"""
super_brain log_view - 按模块切分运行日志，供 UI 展示各自的运行痕迹

背景：所有模块的日志都汇入同一个 logs/super_brain.log（用 logger 名区分来源，行格式
`YYYY-MM-DD HH:MM:SS <logger名> | <消息>`，见 log_setup.py）。运维时想单独看"内容工作流"
或"自动发布"到底跑没跑、结果如何，得从这一大份日志里按 logger 名手工筛，页面上又没有入口
——比如自动发布配了早上 9 点跑，到点没看到结果，却无处可查。这个模块就负责按模块过滤：

- 按 logger 名前缀归组（一个模块可对应多个 logger，如自动发布含 publishers 的渠道推送）；
- 合并多行记录（异常 traceback 的后续行不带时间戳，归到上一条记录）；
- 只读文件末尾一段 + 只保留匹配的记录（日志因 DEBUG 级别可能很大，不整份载入、不逐条建对象）；
- 全程防御式：文件不存在/读取出错都当"暂无日志"返回空列表，绝不因为看日志把页面搞 500。
"""
from __future__ import annotations

import re
from pathlib import Path

from log_setup import LOG_FILE

# 每个 UI 模块对应哪些 logger 前缀。
# - workflow：内容工作流引擎（super_brain.workflow）+ 其真实执行链 content_pipeline；
# - autopublish：自动发布调度/派发（super_brain.autopublish）+ 渠道推送实现（super_brain.publishers）。
# llm_client 是所有模块共用的底层调用日志（且 DEBUG 下会记完整对话，量极大），
# 跨模块又吵，不并入任何单一视图。
MODULE_LOGGERS: dict[str, tuple[str, ...]] = {
    "workflow": ("super_brain.workflow", "super_brain.content_pipeline"),
    "autopublish": ("super_brain.autopublish", "super_brain.publishers"),
}

# 一条日志记录的开头：`YYYY-MM-DD HH:MM:SS logger名 | 消息`。
# 不匹配的行（如 traceback、DEBUG  dumps 的后续行）视为上一条记录的延续。
_RECORD_HEAD = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) (\S+) \| (.*)$")

_DEFAULT_LIMIT = 200          # 页面最多展示多少条（最新在前）
_MAX_BYTES = 8 * 1024 * 1024  # 只读日志末尾这么多字节，够覆盖一天里稀疏的模块日志
_MSG_CAP = 4000               # 单条记录消息上限，防止某个 DEBUG dump 把面板撑爆


def read_module_logs(module: str, limit: int = _DEFAULT_LIMIT,
                     log_file: Path | None = None) -> list[dict]:
    """返回某模块最近的运行日志，最新在前（页面顶部即可看到最近一次运行）。

    每条：{"time", "logger", "message"}，message 可能是多行（合并了 traceback）。
    module 不认识、文件不存在、读取/解码出错——一律返回空列表。
    """
    prefixes = MODULE_LOGGERS.get(module)
    if not prefixes:
        return []
    path = log_file or LOG_FILE
    try:
        raw = _read_tail(path, _MAX_BYTES)
    except OSError:
        return []

    matched: list[dict] = []
    cur: dict | None = None  # 当前正在累积的“匹配”记录；不匹配时为 None，其后续行直接丢弃
    for line in raw.splitlines():
        # 只有以数字（年份）开头的行才可能是记录头，跳过对大量 dump 行做正则，省时间
        head = _RECORD_HEAD.match(line) if line[:1].isdigit() else None
        if head:
            if cur is not None:
                matched.append(cur)
            logger_name = head.group(2)
            if logger_name.startswith(prefixes):
                cur = {"time": head.group(1), "logger": logger_name,
                       "message": head.group(3)[:_MSG_CAP]}
            else:
                cur = None
        elif cur is not None and len(cur["message"]) < _MSG_CAP:
            cur["message"] += "\n" + line
    if cur is not None:
        matched.append(cur)

    matched.reverse()  # 文件里旧→新，反转成新→旧
    return matched[:limit]


def _read_tail(path: Path, max_bytes: int) -> str:
    """只读文件末尾 max_bytes 字节。用二进制定位（文本模式不支持任意偏移 seek）；
    从中间截断的第一行可能是半行，读掉丢弃，剩余交给记录头正则自然对齐。"""
    if not path.exists():
        return ""
    size = path.stat().st_size
    with path.open("rb") as f:
        if size > max_bytes:
            f.seek(size - max_bytes)
            f.readline()  # 丢弃可能被截断的半行
        data = f.read()
    return data.decode("utf-8", errors="replace")
