"""
content_pipeline.py 的离线协作测试——全程用假 LLM/假搜索，零成本验证：
- agent1 researcher 的信息包契约 + 红线校验（无来源事实被拦截）
- agent3 critic 的评分卡解析（含坏 JSON 时如实报 parse_error）
- writer 包装（首行标题约定）
- 全流程编排：critic accept / revise 迭代 / 轮次耗尽三种终态
- 交接 agent4：自动建成 autopublish 发布单
不调用任何真实 API（fake llm_call / search_call / writer_fn / critic_fn 全部注入）。
"""
import json

import pytest

import autopublish
import content_pipeline as cp


# ---------- 假实现 ----------

def _fake_llm(system_prompt: str, user_prompt: str, api_key: str, max_tokens: int = 0) -> str:
    """根据 system prompt 内容返回对应阶段的假输出。
    注意：不能用太宽泛的词做分支（researcher 的 private.md 框架里也含"搜索问题清单"）。"""
    if "只输出为了核实" in system_prompt:  # plan_queries 步骤的独有指令
        return "- 查证 A 的最新数据\n- 查证 B 的实际做法\n这个背景太旧了不用查（非列表行，应被过滤）"
    if "把检索结果里可核实的" in system_prompt:
        return (
            "FACT||AI 生成内容市场规模 2025 年约 X 亿美元||https://example.com/report-a||报告A||high\n"
            "FACT||某平台宣布上线新功能||https://example.com/news-b||新闻B||medium\n"
            "FACT||编造的假数据 80% 用户都…||https://fake-not-in-retrieval.com/x||编造源||low\n"
            "CONFLICT||报告A 说市场在涨，新闻B 暗示增速放缓\n"
            "GAP||没有找到细分赛道 C 的官方数字\n"
        )
    if "输出 JSON（不要其它文字）" in system_prompt:
        return json.dumps({
            "scores": {"hook": 4, "density": 3, "fact_risk": 5, "platform_fit": 4},
            "evidence": {"hook": "开头有具体场景", "density": "中段略散",
                         "fact_risk": "所有数字可回溯信息包", "platform_fit": "适合头条"},
            "must_fix": ["中段第三段压缩一半"], "optional": ["结尾可加一句互动"],
            "verdict": "accept",
        })
    return "（fake：无匹配分支）"


def _fake_search(query: str, api_key: str, max_results: int = 5) -> str:
    return (
        f"- 标题A：内容片段……（来源：https://example.com/report-a）\n"
        f"- 标题B：内容片段……（来源：https://example.com/news-b）"
    )


def _fake_writer(content: str, api_key: str, user_instruction: str | None = None) -> str:
    note = f"（用户补充要求：{user_instruction[:60]}）" if user_instruction else ""
    return f"# 测试文章标题{note}\n\n第一段正文：…\n第二段正文：…"


def _fake_critic_accept(title: str, draft: str, platform: str) -> dict:
    return {"scores": {"hook": 4, "density": 4, "fact_risk": 5, "platform_fit": 4},
            "evidence": {}, "must_fix": [], "optional": [], "verdict": "accept", "parse_error": False}


def _fake_critic_always_revise(title: str, draft: str, platform: str) -> dict:
    return {"scores": {"hook": 2, "density": 2, "fact_risk": 5, "platform_fit": 2},
            "evidence": {}, "must_fix": ["开头重写，加具体场景"], "optional": [],
            "verdict": "revise", "parse_error": False}


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """把运行目录全部隔离到 tmp（不碰真实仓库数据）。"""
    monkeypatch.setattr(autopublish, "QUEUE_DIR", tmp_path / "queue")
    monkeypatch.setattr(autopublish, "ARTIFACTS_DIR", tmp_path / "artifacts")
    monkeypatch.setattr(autopublish, "SOURCES_DIR", tmp_path / "sources")
    monkeypatch.setattr(cp, "RUNS_DIR", tmp_path / "runs")
    return tmp_path


# ---------- agent1 researcher ----------

def test_research_fact_redline_drops_fabricated(isolated):
    """红线：source_url 不在检索结果集合里的事实必须进 dropped，不能混进 facts。"""
    pkg = cp.run_research("某选题", api_key="k", llm_call=_fake_llm, search_call=_fake_search,
                          search_api_key="t", use_web=True)
    assert pkg["web_used"] is True
    assert len(pkg["queries"]) == 2  # 第三行不是 "- " 开头，被过滤
    assert len(pkg["facts"]) == 2    # 两条带真实 URL
    assert all(f["source_url"] in {"https://example.com/report-a", "https://example.com/news-b"}
               for f in pkg["facts"])
    assert len(pkg["dropped"]) == 1
    assert "疑似编造" in pkg["dropped"][0]["reason"]
    assert len(pkg["conflict_notes"]) == 1
    assert len(pkg["coverage_gaps"]) == 1


def test_research_web_disabled_uses_local_context(isolated):
    """没 Tavily key / use_web=False 时降级：只吃 rag_context，且 local:// 来源被放行。"""
    pkg = cp.run_research("本地素材", api_key="k", llm_call=_fake_llm,
                          search_call=_fake_search, use_web=False, rag_context="本地知识：…")
    assert pkg["web_used"] is False


def test_local_url_allowed(isolated):
    """local:// 前缀来源不算编造（素材自带信息），不被红线拦截。"""
    def local_llm(system_prompt, user_prompt, api_key, max_tokens=0):
        if "只输出为了核实" in system_prompt:
            return "- 查A"
        if "把检索结果里可核实的" in system_prompt:
            return "FACT||用户自己的项目事实||local://meeting/xxx.md||会议纪要||high"
        return ""
    pkg = cp.run_research("x", api_key="k", llm_call=local_llm, search_call=_fake_search,
                          use_web=False, rag_context="本地")
    assert len(pkg["facts"]) == 1
    assert pkg["facts"][0]["source_url"].startswith("local://")
    assert pkg["dropped"] == []


# ---------- agent3 critic 解析 ----------

def test_critic_parse_good_json():
    note = cp._parse_review(json.dumps({
        "scores": {"hook": 4, "density": 2, "fact_risk": 1, "platform_fit": 5},
        "evidence": {"fact_risk": "数字无来源"}, "must_fix": ["a"], "optional": ["b"],
        "verdict": "reject",
    }), "d1", "t1")
    assert note["parse_error"] is False
    assert note["verdict"] == "reject"
    assert note["scores"]["fact_risk"] == 1
    assert note["must_fix"] == ["a"]


def test_critic_parse_bad_json_flags_error():
    """模型吐了非 JSON 时必须如实标 parse_error，不许悄悄给假通过。"""
    note = cp._parse_review("不好意思我今天状态不好先不评了", "d1", "t1")
    assert note["parse_error"] is True
    assert note["verdict"] == "revise"


def test_critic_score_clamped_to_1_5():
    note = cp._parse_review('{"scores": {"hook": 99, "density": -3}, "verdict": "accept"}', "d1", "t1")
    assert note["scores"]["hook"] == 5      # 超上限钳到 5
    assert note["scores"]["density"] == 1   # 低于下限钳到 1


# ---------- writer 包装 ----------

def test_writer_split_title_first_line():
    out = cp.run_writer_draft("素材", api_key="k", writer_fn=_fake_writer)
    assert out["title"] == "测试文章标题"
    assert "第一段正文" in out["draft"]


def test_materialize_source_renders_facts_with_links():
    pkg = {"topic": "T", "facts": [{"claim": "C", "source_url": "https://x.com", "confidence": "high"}],
           "conflict_notes": ["冲突1"], "coverage_gaps": ["缺口1"]}
    text = cp.materialize_source(pkg)
    assert "https://x.com" in text and "冲突1" in text and "缺口1" in text


# ---------- 全流程协作 ----------

def test_pipeline_accept_first_round_handoff_to_publish(isolated):
    rec = cp.run_pipeline("某选题", api_key="k", llm_call=_fake_llm, search_call=_fake_search,
                          search_api_key="t", use_web=True, writer_fn=_fake_writer,
                          critic_fn=_fake_critic_accept, save=True)
    assert rec["verdict"] == "accept"
    assert rec["rounds"] == 1
    assert len(rec["drafts"]) == 1
    assert len(rec["reviews"]) == 1
    assert rec["research"]["facts"]
    # 交接 agent4：发布单真的落盘在隔离队列里
    order = autopublish.load_order(rec["handoff_order_id"])
    assert order is not None
    assert order["source"]["kind"] == "content_pipeline"
    assert (isolated / "runs").exists()
    assert rec["run_path"]


def test_pipeline_revise_loop_then_accept(isolated):
    calls = {"n": 0}

    def critic_flip(title, draft, platform):
        calls["n"] += 1
        if calls["n"] >= 2:
            return _fake_critic_accept(title, draft, platform)
        return _fake_critic_always_revise(title, draft, platform)

    rec = cp.run_pipeline("素材驱动", source="已有素材文本", api_key="k",
                          writer_fn=_fake_writer, critic_fn=critic_flip, save=False)
    assert rec["rounds"] == 2
    assert rec["verdict"] == "accept"
    assert len(rec["drafts"]) == 2          # 初稿 + 1 次修改
    assert len(rec["reviews"]) == 2
    assert "用户补充要求" in rec["drafts"][-1]["title"]  # revise 把 must_fix 传给了 writer（标题行带注记）


def test_pipeline_revise_exhausted_after_max_rounds(isolated, monkeypatch):
    monkeypatch.setattr(cp, "MAX_REVISE_ROUNDS", 2)
    rec = cp.run_pipeline("素材驱动", source="素材", api_key="k",
                          writer_fn=_fake_writer, critic_fn=_fake_critic_always_revise, save=False)
    assert rec["rounds"] == 2
    assert rec["verdict"] == "revise_exhausted"   # 超过轮次上限，交人工裁决
    assert len(rec["drafts"]) == 3
    assert rec["handoff_order_id"]               # 仍然交接（交给人而不是丢稿）


def test_pipeline_source_given_skips_research(isolated):
    rec = cp.run_pipeline("素材驱动", source="直接给的素材", api_key="k",
                          writer_fn=_fake_writer, critic_fn=_fake_critic_accept, save=False)
    assert rec["research"] is None
    assert rec["verdict"] == "accept"


# ---------- executor/inbox 入口（researcher / critic 落地为可调用 agent） ----------

def _fake_package(topic="某选题"):
    return {"topic": topic, "generated_at": "now", "web_used": True, "queries": ["q1"],
            "facts": [{"claim": "f1", "source_url": "https://example.com/a", "confidence": "high"}],
            "conflict_notes": [], "coverage_gaps": [], "dropped": []}


def _fake_note(title="留言草稿"):
    return {"draft_id": "d1", "title": title, "scores": {"hook": 3, "density": 3, "fact_risk": 5,
                                                         "platform_fit": 3},
            "evidence": {}, "must_fix": ["补一个具体案例"], "optional": [], "verdict": "revise",
            "parse_error": False}


def test_executor_researcher_uses_inbox_message_as_topic(monkeypatch, tmp_path):
    import executors
    seen = {}
    monkeypatch.setattr(executors.content_pipeline, "run_research",
                        lambda topic, api_key=None: seen.update(topic=topic) or _fake_package(topic))
    monkeypatch.setattr(executors.content_pipeline, "persist_research",
                        lambda pkg: tmp_path / f"pkg_{pkg['topic']}.json")
    out = executors.execute_researcher_apply("9_6", "k", messages=[{"message": "帮我查 AI 内容创作趋势"}])
    assert seen["topic"] == "帮我查 AI 内容创作趋势"
    item = out["researcher"]["帮我查 AI 内容创作趋势"]
    assert item["facts"] == 1 and item["artifact"].endswith(".json")


def test_executor_researcher_empty_message_returns_error_without_calling(monkeypatch):
    import executors
    called = {"n": 0}
    monkeypatch.setattr(executors.content_pipeline, "run_research",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    out = executors.execute_researcher_apply("9_6", "k", messages=[])
    assert "researcher_error" in out and called["n"] == 0


def test_executor_critic_accepts_draft_file_path(monkeypatch, tmp_path):
    import executors
    draft_file = tmp_path / "一篇稿子.md"
    draft_file.write_text("# 标题\n\n正文…", encoding="utf-8")
    captured = {}
    monkeypatch.setattr(executors.content_pipeline, "run_critic",
                        lambda draft_id, title, draft, platform, api_key=None:
                            captured.update(title=title, draft=draft) or _fake_note(title))
    monkeypatch.setattr(executors.content_pipeline, "persist_critic",
                        lambda note, draft: tmp_path / "note.json")
    out = executors.execute_critic_review("9_6", "k", messages=[{"message": str(draft_file)}])
    assert captured["title"] == "一篇稿子"          # 按文件路径读，标题=文件名
    assert "正文…" in captured["draft"]
    assert out["critic"]["一篇稿子"]["verdict"] == "revise"


def test_executor_critic_treats_non_path_message_as_draft_text(monkeypatch, tmp_path):
    import executors
    captured = {}
    monkeypatch.setattr(executors.content_pipeline, "run_critic",
                        lambda draft_id, title, draft, platform, api_key=None:
                            captured.update(title=title, draft=draft) or _fake_note(title))
    monkeypatch.setattr(executors.content_pipeline, "persist_critic",
                        lambda note, draft: tmp_path / "note.json")
    executors.execute_critic_review("9_6", "k", messages=[{"message": "这是一段直接粘贴的正文，不是路径"}])
    assert captured["title"] == "留言草稿"
    assert captured["draft"] == "这是一段直接粘贴的正文，不是路径"


def test_executor_error_keeps_pending_semantics(monkeypatch, tmp_path):
    """执行器里出现异常要返回 *_error 键——dispatcher 据此不自动标记 done，留言留在 pending。"""
    import executors

    def _boom(topic, api_key=None):
        raise RuntimeError("额度用尽")

    monkeypatch.setattr(executors.content_pipeline, "run_research", _boom)
    out = executors.execute_researcher_apply("9_6", "k", messages=[{"message": "某选题"}])
    assert "researcher_error" in out
    assert "researcher" not in out
