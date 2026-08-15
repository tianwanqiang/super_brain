"""
super_brain 共享日志配置。

目的：之前全靠 print()，输出只在当次终端里，跑完就没了——没法回头看"上一次到底哪一步
出问题了"。现在改成结构化日志，同时输出到终端（跟以前体验一样）和 logs/super_brain.log
（持久化，能追溯），日志格式带时间戳、级别、来源模块，方便定位业务流程里不通顺的环节。

用法：入口脚本（目前是 dispatcher.py）在最开始调一次 configure_logging()，其他模块
（publishers.py 等）直接 logging.getLogger("super_brain.<模块名>") 拿 logger 用，
不用重复配置——Python logging 的子 logger 会自动继承根配置。
"""
import logging
import sys
from pathlib import Path

LOG_DIR = Path(r"G:\code\super_brain\logs")
LOG_FILE = LOG_DIR / "super_brain.log"

_configured = False


def configure_logging(level: int = logging.INFO) -> None:
    global _configured
    if _configured:
        return

    LOG_DIR.mkdir(exist_ok=True)

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(logging.Formatter("%(message)s"))  # 终端保持简洁，不带时间戳前缀

    root = logging.getLogger("super_brain")
    root.setLevel(level)
    root.addHandler(file_handler)
    root.addHandler(console_handler)
    root.propagate = False

    _configured = True
