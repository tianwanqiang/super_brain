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
from datetime import datetime, timezone, timedelta

from paths import SUPER_BRAIN

LOG_DIR = SUPER_BRAIN / "logs"
LOG_FILE = LOG_DIR / "super_brain.log"

# UTC+8 时区（中国标准时间）
UTC_PLUS_8 = timezone(timedelta(hours=8))

_configured = False


class UTC8Formatter(logging.Formatter):
    """日志时间戳固定用 UTC+8，不依赖服务器本地时区设置。
    部署到海外服务器（默认 UTC）时，日志时间也能直接看懂。"""

    def formatTime(self, record, datefmt=None): 
        # 把 UTC timestamp 转成 UTC+8
        dt = datetime.fromtimestamp(record.created, tz=UTC_PLUS_8)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.isoformat(sep=" ", timespec="seconds")


def configure_logging(console_level: int = logging.INFO, file_level: int = logging.DEBUG) -> None:
    """文件日志和终端日志级别分开——文件要能拿来做 token 用量/错误/完整对话内容的事后分析，
    所以文件用 DEBUG（记录一切细节，包括完整 prompt/response）；终端只给人看，保持 INFO，
    不然每次真实调用的完整对话内容都刷屏，没法用。
    """
    global _configured
    if _configured:
        return

    # parents=True：fresh checkout / CI（Linux 上默认 SUPER_BRAIN 也可能不存在 logs 目录）
    # 时也要能建出来；以前只有 mkdir(exist_ok=True) 在父目录缺失时会 FileNotFoundError。
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    formatter = UTC8Formatter(
        fmt="%(asctime)s %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setFormatter(formatter)
    file_handler.setLevel(file_level)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(logging.Formatter("%(message)s"))  # 终端保持简洁，不带时间戳前缀
    console_handler.setLevel(console_level)

    root = logging.getLogger("super_brain")
    root.setLevel(min(console_level, file_level))  # 根 logger 的门槛不能比任一 handler 更严，否则 DEBUG 记录到不了文件 handler
    root.addHandler(file_handler)
    root.addHandler(console_handler)
    root.propagate = False

    _configured = True
