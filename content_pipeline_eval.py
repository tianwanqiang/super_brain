"""
content_pipeline_eval - agent 技能水平评估 CLI（真实模型，用户自己决定何时跑）

按 feedback_dev_workflow_cost.md 的规矩：本脚本默认只做"计划打印/离线自检"，只有加
--execute 才会发起真实 DeepSeek/Tavily 调用。所有输出落在 content_pipeline_runs/eval_*。

三种模式：

1) writer 技能评估（评估 agent2/写作链在真实素材上的水平）
   python content_pipeline_eval.py writer --source <素材文件.md> [--platform toutiao|wechat] [--execute]
   流程：writer 链(content-strategist→writer)成稿 → critic 评分卡 → 按 verdict 迭代 ≤N 轮。
   产出：定稿 + 每轮评分 + 终态（accept/revise_exhausted），落 eval_<ts> 目录供人读。

2) critic 敏感度评估（评估 agent3 能不能找出"真实缺陷"——植入缺陷检出率）
   python content_pipeline_eval.py critic --source <一篇稿子.md> [--execute]
   做法：同一篇稿子生成 4 个变体，分别植入已知缺陷（无来源数字断言 / 开头变空洞 /
   结尾拖沓 / 平台不适配），critic 逐一评分，统计"植入点是否被点名/给低分"。

3) 端到端流水线（agent1→2→3→4 一次跑通，含联网 research）
   python content_pipeline_eval.py pipeline --topic <选题> [--execute]
   注意：会先花 Tavily 搜索费再花 DeepSeek；想省搜索费就先人工把素材写成 md 走 writer 模式。

默认不 --execute 时只打印每步会发生的真实调用数量与估算（不联网、不花钱）。
"""
import json
import sys
import time
from log_setup import Clock
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

import content_pipeline as cp


def _ts() -> str:
    return Clock.now().strftime("%Y%m%d_%H%M%S")


def _out_dir(tag: str) -> Path:
    d = cp.RUNS_DIR / f"eval_{_ts()}_{tag}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _save(out_dir: Path, name: str, obj) -> Path:
    path = out_dir / name
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


# ---------- writer 技能评估 ----------

def plan_writer(source_text: str, platform: str, rounds: int) -> dict:
    n = 2                      # content-strategist + writer
    n += (1 + 2) * rounds      # critic 每轮 1 次 + 修改轮 writer 2 次
    return {
        "mode": "writer", "llm_calls_estimate": n,
        "steps": ["agent2 writer 链(2 次)成稿", *(f"critic 点评 + writer 修改（第{i+1} 轮）" for i in range(rounds))],
    }


def run_writer(source_text: str, platform: str, rounds: int = 2) -> dict:
    out_dir = _out_dir("writer")
    record = cp.run_pipeline("素材驱动", source=source_text, platform=platform, save=False)
    # 上面已经跑过默认轮数；这里把轨迹落盘 + 打印摘要
    record["run_path"] = str(_save(out_dir, "record.json", record))
    drafts_dir = out_dir / "drafts"
    drafts_dir.mkdir(exist_ok=True)
    for i, d in enumerate(record["drafts"], 1):
        (drafts_dir / f"draft_r{i}_{d['title'][:20]}.md").write_text(
            f"# {d['title']}\n\n{d['draft']}", encoding="utf-8")
    print(f"\n===== writer 技能评估完成 =====")
    print(f"终态 verdict：{record['verdict']}（轮次 {record['rounds']}）")
    for i, rv in enumerate(record["reviews"], 1):
        print(f"Round{i} 评分：{rv['scores']} verdict={rv['verdict']}")
        for fix in rv.get("must_fix", [])[:3]:
            print(f"  - 必改：{fix[:100]}")
    print(f"产物目录：{out_dir}")
    return record


# ---------- critic 敏感度 ----------

PLANTED_DEFECTS = [
    {
        "id": "fake_number", "label": "无来源数字断言",
        "plant": lambda t: t.replace(
            "\n\n", "\n\n有权威统计显示，超过 80% 的创作者已经在用 AI 生产内容，这个比例还在快速增长。\n\n", 1),
        "keywords": ["80%", "权威统计", "来源"],
    },
    {
        "id": "vague_opening", "label": "开头空洞化",
        "plant": lambda t: "随着时代的发展与科技的进步，AI 正在深刻地改变着内容创作的方方面面，"
                           "其影响是巨大而深远的，值得我们每一个人认真思考。\n\n" + t,
        "keywords": ["随着", "方方面面", "巨大而深远"],
    },
    {
        "id": "trailing", "label": "结尾拖沓",
        "plant": lambda t: t.rstrip() +
                           "\n\n最后我想说的是，以上只是我的一些粗浅看法，可能还有很多不足，"
                           "希望大家多多批评指正，谢谢大家，我们下次再见。",
        "keywords": ["粗浅看法", "批评指正"],
    },
]


def run_critic_probe(base_text: str) -> dict:
    out_dir = _out_dir("critic")
    results = []
    # 变体 0：原稿（无植入），作为对照
    variants = [{"id": "baseline", "label": "原稿（无植入）", "text": base_text}]
    for d in PLANTED_DEFECTS:
        variants.append({"id": d["id"], "label": d["label"], "text": d["plant"](base_text),
                         "keywords": d["keywords"]})

    for v in variants:
        note = cp.run_critic(f"probe-{v['id']}", "评估样本", v["text"], platform="toutiao")
        blob = json.dumps(note, ensure_ascii=False)
        if v["id"] == "baseline":
            v["result"] = {"verdict": note["verdict"], "scores": note["scores"],
                           "baseline_scores": note["scores"]}
        else:
            # 检出判定：verdict 不是 accept，或 must_fix/evidence 命中植入关键词
            hit = any(k in blob for k in v.get("keywords", []))
            flagged = note["verdict"] != "accept" or hit
            v["result"] = {
                "verdict": note["verdict"], "scores": note["scores"],
                "keyword_hit": hit, "defect_flagged": flagged,
                "must_fix": note.get("must_fix", [])[:5],
            }
        results.append(v)
        print(f"[{v['label']}] verdict={note['verdict']} scores={note['scores']} "
              f"flagged={v['result'].get('defect_flagged')}")
        time.sleep(1)  # 别把请求打太密

    save_path = _save(out_dir, "critic_probe.json", results)
    detected = [r for r in results if r["id"] != "baseline" and r["result"].get("defect_flagged")]
    print(f"\n===== critic 敏感度 =====")
    print(f"植入缺陷 {len(results) - 1} 个，检出 {len(detected)} 个："
          f"{[r['label'] for r in detected] or '（无——需要看原始评分判断 critic 是否太宽松）'}")
    print(f"结果：{save_path}")
    return results


# ---------- 端到端 ----------

def run_pipeline_full(topic: str) -> dict:
    record = cp.run_pipeline(topic, source=None)   # 联网 research → writer → critic 迭代 → 交接发布单
    print(f"\n===== 端到端完成 =====")
    print(f"run_id={record['run_id']} verdict={record['verdict']} rounds={record['rounds']}")
    if record.get("research"):
        r = record["research"]
        print(f"research：facts={len(r['facts'])} dropped={len(r['dropped'])} "
              f"conflicts={len(r['conflict_notes'])} gaps={len(r['coverage_gaps'])}")
    print(f"发布单已入池：{record.get('handoff_order_id')}（发布前记得去后台人工放行）")
    print(f"运行记录：{record.get('run_path')}")
    return record


# ---------- main ----------

def main() -> None:
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return
    execute = "--execute" in args
    tokens = [a for a in args if not a.startswith("--")]  # 非 flag 的裸参数
    mode = tokens[0] if tokens else ""

    def _flag_value(name: str) -> str | None:
        if name in args:
            i = args.index(name)
            if i + 1 < len(args) and not args[i + 1].startswith("--"):
                return args[i + 1]
        return None

    if mode not in ("writer", "critic", "pipeline"):
        print(f"未知模式 {mode!r}；支持 writer / critic / pipeline（加 --execute 才真调用）")
        return

    def _source_text() -> str:
        raw = _flag_value("--source")
        if not raw:
            print("缺 --source <文件路径或文本>")
            sys.exit(1)
        path = Path(raw)
        return path.read_text(encoding="utf-8-sig") if path.exists() else raw

    if mode == "writer":
        source_text = _source_text()
        platform = "wechat" if _flag_value("--platform") == "wechat" else "toutiao"
        plan = plan_writer(source_text, platform, rounds=cp.MAX_REVISE_ROUNDS)
        print(f"[writer 评估] 计划真实 LLM 调用约 {plan['llm_calls_estimate']} 次。步骤：")
        for s in plan["steps"]:
            print(f"  - {s}")
        if execute:
            run_writer(source_text, platform)
        else:
            print("\n加 --execute 后才会真调用 DeepSeek（会花额度）。")
    elif mode == "critic":
        base_text = _source_text()
        variants = len(PLANTED_DEFECTS) + 1
        print(f"[critic 评估] 将对 {variants} 个变体各做 1 次 critic 点评，共 {variants} 次真实 LLM 调用。")
        if execute:
            run_critic_probe(base_text)
        else:
            print("\n加 --execute 后才会真调用 DeepSeek（会花额度）。")
    elif mode == "pipeline":
        topic = _flag_value("--topic")
        if not topic:
            print("pipeline 模式需要 --topic <选题>")
            sys.exit(1)
        print(f"[端到端] 计划调用：research(1 计划 + ≤3 搜索 + 1 提取) + writer 链(2) + critic/迭代。"
              f"搜索会花 Tavily 额度。")
        if execute:
            run_pipeline_full(topic)
        else:
            print("\n加 --execute 后才会真调用（DeepSeek + Tavily，会花额度）。")


if __name__ == "__main__":
    main()
