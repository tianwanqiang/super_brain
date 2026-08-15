"""
super_brain roundtable - 圆桌讨论的真实执行引擎

Python 后台直接调 DeepSeek API 唤起多个圆桌专家，不依赖 Claude Code 的 `Agent` 工具——
两者本质上是同一件事（N 次互相独立的 LLM 调用），这里用线程池做真并行，不是伪并行循环。

两轮协议，跟 2026-08-14 用 Agent 工具跑通、验证过确实能挖出真实分歧的那次完全一致：
1. Round 1：每个专家只拿到问题 + 自己的 private.md，互相看不见，真独立分析，给出明确倾向判断
2. Round 2：把全部专家 Round 1 的结论都亮给每一个专家，只让他们做一件事——挑风险/冲突，
   没有异议就明说，不重新分析原问题
3. 汇总：调用 writer agent 的知识框架，把两轮原始记录写成正式会议纪要，分歧不隐藏、不调和，
   落盘到 MEETING_MINUTES_DIR（config.json 里的持久化设置）

诚实的局限：这里默认用 DeepSeek（复用 dispatcher.py 已配好的 Key，成本低）。2026-08-14 那次
真正验证"圆桌讨论确实能挖出真实分歧"的实验用的是 Claude（通过 Agent 工具）——没有直接证据
证明 DeepSeek 在这个任务上效果一样好，这是一个待验证的假设，不是已经证实的结论。

用法：
    python roundtable.py "要不要涨价" strategy finance marketing
"""
import concurrent.futures
import json
import logging
import re
import sys
from datetime import datetime
from pathlib import Path

from dispatcher import (
    DEEPSEEK_CONFIG_PATH,
    SUPER_BRAIN,
    call_deepseek,
    load_agent_registry,
    load_private_context,
)
from log_setup import configure_logging

logger = logging.getLogger("super_brain.roundtable")

CONFIG_PATH = SUPER_BRAIN / "config.json"


class RoundtableError(Exception):
    pass


def _round1_system_prompt(agent_name: str, entry: dict, private_context: str) -> str:
    parts = [
        f"你是 super_brain 决策圆桌里的 '{agent_name}' 专家。角色定位：{entry.get('description', '')}",
        f"下面是你的专属知识框架（private.md 原文），分析必须显式引用其中的具体条目：\n\n{private_context}",
    ]
    sources = entry.get("sources") or []
    if sources:
        parts.append("知识框架来源（可审计）：\n" + "\n".join(f"- {s}" for s in sources))
    if entry.get("output_requires_citation"):
        parts.append("硬性要求：每条结论标注依据第几条规则，覆盖不到的问题明说'现有框架未覆盖，以下是推测'。")
    if entry.get("disclaimer_required"):
        parts.append("硬性要求：任何具体建议之后必须附一句——这不是正式专业意见，具体问题建议咨询执业人士。")
    parts.append(
        "这是第一轮，你看不到其他专家的意见，独立给出你的分析，给一个明确的倾向性判断"
        "（做/不做/有条件地做），不要模棱两可，控制在 400 字以内。"
    )
    return "\n\n".join(parts)


def _round2_system_prompt(agent_name: str, entry: dict, private_context: str,
                           question: str, round1_results: dict[str, str]) -> str:
    others = "\n\n".join(
        f"【{name}】\n{text}" for name, text in round1_results.items() if name != agent_name
    )
    return "\n\n".join([
        f"你是 super_brain 决策圆桌里的 '{agent_name}' 专家。你在第一轮已经独立分析过问题："
        f"{question!r}，你当时的结论：\n{round1_results.get(agent_name, '')}",
        f"下面是你的专属知识框架：\n\n{private_context}",
        f"现在是第二轮——其他专家的独立分析都摆出来了：\n\n{others}",
        "只做一件事：从你的专业角度指出这些结论里有没有风险或冲突，如果没有异议就明确说"
        "'没有异议'。不要重新分析原问题，只做交叉校验，控制在 300 字以内。",
    ])


def _run_round(agent_names: list[str], registry: dict, contexts: dict[str, str], api_key: str,
               build_system_prompt, user_prompt: str, round_label: str) -> dict[str, str]:
    logger.info(f"{round_label}：并行唤起 {len(agent_names)} 位专家")
    results: dict[str, str] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(agent_names)) as pool:
        futures = {
            pool.submit(
                call_deepseek,
                build_system_prompt(name),
                user_prompt,
                api_key,
                max_tokens=1200,
            ): name
            for name in agent_names
        }
        for future in concurrent.futures.as_completed(futures):
            name = futures[future]
            try:
                results[name] = future.result()
                logger.info(f"{round_label} 完成：{name}")
            except Exception:
                logger.exception(f"{round_label} 失败：{name}，这位专家本轮缺席")
                results[name] = "(本轮调用失败，这位专家缺席)"
    return results


def write_meeting_minutes(question: str, agent_names: list[str],
                           round1: dict[str, str], round2: dict[str, str], api_key: str) -> Path | None:
    """调 writer 的知识框架，把两轮原始记录写成正式会议纪要，落盘到持久化配置的目录。
    目录没配置就跳过落盘（不臆造路径），只把原始记录返回，不算失败。
    """
    if not CONFIG_PATH.exists():
        logger.warning("没有 config.json，跳过写会议纪要（MEETING_MINUTES_DIR 未配置）")
        return None
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    minutes_dir = config.get("MEETING_MINUTES_DIR")
    if not minutes_dir:
        logger.warning("config.json 里没有 MEETING_MINUTES_DIR，跳过写会议纪要——去 UI 的"
                        "\"首次配置\"区块设置一次")
        return None

    registry = load_agent_registry()
    writer_context = load_private_context("writer", registry)

    round1_text = "\n\n".join(f"【{n}】\n{t}" for n, t in round1.items())
    round2_text = "\n\n".join(f"【{n}】\n{t}" for n, t in round2.items())

    system_prompt = (
        f"下面是你的写作技能知识框架（private.md 原文）：\n\n{writer_context}\n\n"
        f"讨论的问题：{question}\n参与专家：{', '.join(agent_names)}\n\n"
        f"Round 1（独立分析，互不可见）原始记录：\n{round1_text}\n\n"
        f"Round 2（交叉校验）原始记录：\n{round2_text}\n\n"
        "请按你的写作技能，把以上原始记录写成一份正式会议纪要：先给共识，再给分歧（原样呈现，"
        "不调和），最后给执行建议清单。第一行输出一个简短的中文标题（不要任何前缀符号）。"
    )
    logger.info("调用 writer 生成正式会议纪要")
    try:
        raw = call_deepseek(system_prompt, "请生成会议纪要。", api_key, max_tokens=2500)
    except Exception:
        logger.exception("writer 生成会议纪要失败，原始记录仍然保留在返回值里，只是没有落盘")
        return None

    lines = raw.strip().split("\n", 1)
    title = lines[0].strip().lstrip("#").strip() or "会议纪要"
    slug = re.sub(r"[^\w一-鿿-]", "-", title)[:40].strip("-") or "untitled"

    now = datetime.now()
    filename = f"{now:%Y-%m-%d}_{now:%H%M}_{slug}.md"
    out_dir = Path(minutes_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / filename
    out_path.write_text(raw, encoding="utf-8")
    logger.info(f"会议纪要已落盘：{out_path}")
    return out_path


def run_roundtable(agent_names: list[str], question: str) -> dict:
    registry = load_agent_registry()

    invalid = [n for n in agent_names if registry.get(n, {}).get("type") != "roundtable"]
    if invalid:
        raise RoundtableError(f"这些不是 roundtable 类型的 agent，不能进圆桌：{invalid}")
    if len(agent_names) < 2:
        raise RoundtableError("圆桌至少需要 2 位专家，一个人自己跟自己交叉校验没有意义")

    if not DEEPSEEK_CONFIG_PATH.exists():
        raise RoundtableError(f"找不到 DeepSeek 配置（{DEEPSEEK_CONFIG_PATH}）")
    api_key = json.loads(DEEPSEEK_CONFIG_PATH.read_text(encoding="utf-8-sig"))["DEEPSEEK_API_KEY"]

    logger.info(f"===== 圆桌讨论启动：{question!r}，专家：{agent_names} =====")
    contexts = {name: load_private_context(name, registry) for name in agent_names}

    round1 = _run_round(
        agent_names, registry, contexts, api_key,
        build_system_prompt=lambda name: _round1_system_prompt(name, registry[name], contexts[name]),
        user_prompt=question,
        round_label="Round 1",
    )
    round2 = _run_round(
        agent_names, registry, contexts, api_key,
        build_system_prompt=lambda name: _round2_system_prompt(
            name, registry[name], contexts[name], question, round1
        ),
        user_prompt="请给出交叉校验意见。",
        round_label="Round 2",
    )

    minutes_path = write_meeting_minutes(question, agent_names, round1, round2, api_key)

    logger.info("===== 圆桌讨论结束 =====")
    return {
        "question": question,
        "agents": agent_names,
        "round1": round1,
        "round2": round2,
        "minutes_path": str(minutes_path) if minutes_path else None,
    }


if __name__ == "__main__":
    configure_logging()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    if len(sys.argv) < 3:
        print('用法：python roundtable.py "问题内容" agent1 agent2 [agent3 ...]')
        sys.exit(1)

    result = run_roundtable(sys.argv[2:], sys.argv[1])
    print(json.dumps(result, ensure_ascii=False, indent=2))
