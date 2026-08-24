"""
super_brain paths - 所有模块共享的路径常量，集中定义一处

以前这些常量散落定义在 dispatcher.py 里，其他模块（roundtable.py/video_prompt.py/
ui_app.py/publishers.py）各自 import 一遍——拆分 dispatcher.py 成多个模块之后，如果
每个模块各自重新定义一份，改一个路径要改好几处、容易出现不一致。集中放这里，谁要用
就从这里 import，只有一份权威定义。

2026-08-22 改成环境变量可覆盖、带本机默认值——之前这几个路径是写死的 Windows 绝对路径
（`G:\\code\\...`），部署到腾讯云 Linux 服务器上会直接找不到路径、启动失败。改成
`os.environ.get(env_name, 本机默认值)` 之后：本机不设置这几个环境变量，行为跟以前完全
一样；Docker/服务器部署时通过环境变量覆盖成容器内的路径（见 Dockerfile / docker-compose）。
"""
import os
from pathlib import Path

SUPER_BRAIN = Path(os.environ.get("SUPER_BRAIN_DIR", r"G:\code\super_brain"))
INBOX = SUPER_BRAIN / "inbox.md"
AGENTS_DIR = SUPER_BRAIN / "agents"
AGENTS_CONFIG_PATH = SUPER_BRAIN / "agents.yaml"
DISPATCH_LOG_DIR = SUPER_BRAIN / "dispatch_log"
OPC_ROOT = Path(os.environ.get("OPC_ROOT_DIR", r"G:\code"))

# 本机默认复用 toutiao-agent 已经配好的 DeepSeek Key，避免重复要用户再配一份。
# 服务器部署场景下 toutiao-agent 这个兄弟项目不存在，通过 DEEPSEEK_CONFIG_PATH 环境变量
# 指向 super_brain 自己目录下的 config.json（跟 ui_app.py/publishers.py 用的是同一个文件，
# 只要在里面补一个 DEEPSEEK_API_KEY 字段）。
DEEPSEEK_CONFIG_PATH = Path(os.environ.get("DEEPSEEK_CONFIG_PATH", r"G:\code\toutiao-agent\config.json"))
