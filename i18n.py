"""
super_brain i18n - 轻量翻译层

agents.yaml 里的 name（strategy/finance/...）同时也是表单 value、URL 参数、文件名的一部分，
不能改成中文——这层只负责"展示给人看的名字"，业务逻辑各处继续用原始英文 name。

结构上留出扩展空间：以后要支持别的语言，往对应语言的字典里加一份、在 agent_label() 里按
lang 分支即可，不需要动任何业务逻辑代码或模板结构。
"""

AGENT_LABELS_ZH = {
    "strategy": "战略专家",
    "finance": "财务专家",
    "marketing": "营销专家",
    "legal": "法务专家",
    "toutiao": "头条助手",
    "writer": "会议纪要撰写",
    "ops-assistant": "运营助手",
    "ship": "代码 & 公众号发布",
    "coordinator": "协调者",
}

_CATALOG = {
    "zh": AGENT_LABELS_ZH,
}

DEFAULT_LANG = "zh"


def agent_label(name: str, lang: str = DEFAULT_LANG) -> str:
    """agent 的中文展示名，查不到就原样返回英文 name（不报错，不留空白）。"""
    return _CATALOG.get(lang, {}).get(name, name)
