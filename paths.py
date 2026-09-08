"""
super_brain paths - 所有模块共享的路径常量，集中定义一处

以前这些常量散落定义在 dispatcher.py 里，其他模块（roundtable.py/video_prompt.py/
ui_app.py/publishers.py）各自 import 一遍——拆分 dispatcher.py 成多个模块之后，如果
每个模块各自重新定义一份，改一个路径要改好几处、容易出现不一致。集中放这里，谁要用
就从这里 import，只有一份权威定义。

2026-08-22 改成环境变量可覆盖、带本机默认值——之前这几个路径是写死的 Windows 本机绝对
路径，部署到腾讯云 Linux 服务器上会直接找不到路径、启动失败。改成
`os.environ.get(env_name, 本机默认值)` 之后：本机不设置这几个环境变量，行为跟以前完全
一样；Docker/服务器部署时通过环境变量覆盖成容器内的路径（见 Dockerfile / docker-compose）。
2026-09 进一步：本仓库根路径的默认值改成"由本文件位置推导"，不再在代码里写死任何盘符。
"""
import os
from pathlib import Path

# 本仓库根目录：优先环境变量（服务器/容器部署必设）；没设就用"本文件所在目录"推导——
# 不再硬编码 Windows 盘符路径（G:\code\... 曾在 Linux/CI 上漏到日志里，也容易失效）。
# 本机、Linux、CI 都能自洽：仓库在哪，SUPER_BRAIN 就在哪。
SUPER_BRAIN = Path(os.environ.get("SUPER_BRAIN_DIR") or Path(__file__).resolve().parent)

# config.json 的唯一权威路径（2026-09 收敛）——**全仓库只有一个名字：CONFIG_PATH**。
# 历史上同名出现过 DEEPSEEK_CONFIG_PATH（Python 符号 + 环境变量两个形态），已全部删除：
# 同一路径只允许一个名字，避免"名字不同、改一处漏一处"的维护成本。
# 运行时只支持一个环境变量覆盖（本机调试指向别的配置文件时用）：SUPER_BRAIN_CONFIG_PATH。
# 这个文件承载全部配置（DeepSeek/微信/Tavily/DashScope 凭据 + 各种目录字段），不专属
# DeepSeek，所以不带 "DEEPSEEK" 前缀。
CONFIG_PATH = Path(
    os.environ.get("SUPER_BRAIN_CONFIG_PATH")
    or str(SUPER_BRAIN / "config.json")
)

INBOX = SUPER_BRAIN / "inbox.md"
AGENTS_DIR = SUPER_BRAIN / "agents"
AGENTS_CONFIG_PATH = SUPER_BRAIN / "agents.yaml"
DISPATCH_LOG_DIR = SUPER_BRAIN / "dispatch_log"
# OPC 素材根目录（默认=仓库上一级，与历史上 G:\code 相对 G:\code\super_brain 的关系一致；
# 服务器/容器部署通过环境变量 OPC_ROOT_DIR 覆盖）。
OPC_ROOT = Path(os.environ.get("OPC_ROOT_DIR") or SUPER_BRAIN.parent)

# 自动化媒体发布流水线（autopublish.py）的三个运行目录——跟 dispatch_log 一样都是运行时
# 产物/输入，不属于版本控制（见 .gitignore）。按"同一个仓库里只有一份权威定义"的原则集中
# 放这里，autopublish.py 直接 import，不要自己再拼一遍路径。
AUTOPUBLISH_QUEUE_DIR = SUPER_BRAIN / "autopublish_queue"      # 发布单（持久化状态）
AUTOPUBLISH_ARTIFACTS_DIR = SUPER_BRAIN / "autopublish_artifacts"  # 物料（定稿/清单）

# 内容工作流（workflow.py）的运行实例目录——每个"定时→agent1→审批→…"的运行留痕。
WORKFLOW_RUNS_DIR = SUPER_BRAIN / "workflow_runs"

# 历史：本机曾默认复用兄弟项目 toutiao-agent 的 config.json，导致"检查的/读的"不是同一份
# 文件（2026-09-04 事故）。现在路径层面只有 CONFIG_PATH 一个定义（上面），不再讨论环境变量。

# 读写 config.json 的唯一实现入口在 config_store.py；路径层面的唯一权威定义就是上面的
# CONFIG_PATH。谁要"读配置/改配置"都走 config_store，不要自己 json.loads / write_text。

