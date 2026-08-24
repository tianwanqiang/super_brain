"""
super_brain paths - 所有模块共享的路径常量，集中定义一处

以前这些常量散落定义在 dispatcher.py 里，其他模块（roundtable.py/video_prompt.py/
ui_app.py/publishers.py）各自 import 一遍——拆分 dispatcher.py 成多个模块之后，如果
每个模块各自重新定义一份，改一个路径要改好几处、容易出现不一致。集中放这里，谁要用
就从这里 import，只有一份权威定义。
"""
from pathlib import Path

SUPER_BRAIN = Path(r"G:\code\super_brain")
INBOX = SUPER_BRAIN / "inbox.md"
AGENTS_DIR = SUPER_BRAIN / "agents"
AGENTS_CONFIG_PATH = SUPER_BRAIN / "agents.yaml"
DISPATCH_LOG_DIR = SUPER_BRAIN / "dispatch_log"
OPC_ROOT = Path(r"G:\code")

# 复用 toutiao-agent 已经配好的 DeepSeek Key，避免重复要用户再配一份。
# 如果以后想让 super_brain 独立于 toutiao-agent，把这里改成 super_brain 自己的 config.json。
DEEPSEEK_CONFIG_PATH = Path(r"G:\code\toutiao-agent\config.json")
