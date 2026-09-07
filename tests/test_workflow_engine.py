"""
workflow.py（内容工作流引擎）的离线测试——全程假步骤处理器/隔离目录，零成本验证：
- 运行实例创建与默认步骤（首步=选题提案 topics）
- 手动给选题时自动跳过 topics；定时无选题时 topics 产出候选停在审批口
- 审批闸门：选题审批带 chosen_topic（否则报错）、逐步骤通过才推进
- requires_approval=False 自动继续；选题步骤无需审批时自动选第一个候选
- 打回停止 / 失败重试 / 异常处理
- publish 步骤真实交接成 autopublish 发布单（隔离队列目录）
- 调度：到点当天只启动一次，第一步（选题提案）停在审批口
- 邮件通知：进入审批口且配置了 MAIL 时调用 send_mail（monkeypatch 掉真发送）
"""
import json
from datetime import datetime

import pytest

import autopublish
import workflow as wf


@pytest.fixture
def env(tmp_path, monkeypatch):
    """隔离运行目录与 config：队列/工作流 runs/config.json 全指向 tmp。"""
    monkeypatch.setattr(wf, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(wf, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(autopublish, "QUEUE_DIR", tmp_path / "queue")
    monkeypatch.setattr(autopublish, "ARTIFACTS_DIR", tmp_path / "artifacts")
    monkeypatch.setattr(autopublish, "SOURCES_DIR", tmp_path / "sources")

    # 假步骤处理器（按步骤 id 精确分发——真实代码里 topics/research 都是 researcher，必须靠 id）
    monkeypatch.setattr(wf, "step_id_handlers", {
        "topics": lambda ctx: {"kind": "topics", "count": 2,
                               "candidates": [{"title": "候选选题A", "reason": "理由A"},
                                              {"title": "候选选题B", "reason": "理由B"}],
                               "payload": "", "preview": "- 候选选题A\n- 候选选题B"},
        "research": lambda ctx: {"kind": "research", "facts": 1, "dropped": 0,
                                 "payload": "事实A（来源 https://e.com/a）", "preview": "事实A"},
        "write": lambda ctx: {"kind": "draft", "title": "标题T", "payload": "正文T", "preview": "正文T"},
        "critic": lambda ctx: {"kind": "critic", "verdict": "revise",
                               "scores": {"hook": 3, "density": 3, "fact_risk": 4, "platform_fit": 3},
                               "must_fix": ["补个案例"], "payload": "正文T", "preview": "{}"},
        "publish": lambda ctx: {"kind": "publish_order", "handoff_order_id": "fake-order",
                                "payload": "正文T", "preview": "fake"},
    })
    monkeypatch.setattr(wf, "step_handlers", {})
    # 关键隔离：mailer.mail_settings 默认读真实 config.json——测试一律视为"没配邮件"
    monkeypatch.setattr(wf.mailer, "mail_settings", lambda: None)
    return tmp_path


def _write_config(env, workflows: dict):
    (env / "config.json").write_text(json.dumps({"CONTENT_WORKFLOWS": workflows}, ensure_ascii=False),
                                     encoding="utf-8")


def _defaults_all_approval(approval: bool, schedule=None) -> dict:
    defs = json.loads(json.dumps(wf.DEFAULT_WORKFLOWS))
    for wf_id in defs:
        for step in defs[wf_id]["steps"]:
            step["requires_approval"] = approval
        if schedule:
            defs[wf_id]["schedule"] = schedule
    return defs


def _step(run: dict, sid: str) -> dict:
    return next(s for s in run["steps"] if s["id"] == sid)


# ---------- 创建 ----------

def test_create_run_with_default_steps(env):
    _write_config(env, {})
    run = wf.create_run(wf.DEFAULT_WORKFLOW_ID, topic="某选题")
    assert run["workflow_id"] == wf.DEFAULT_WORKFLOW_ID
    assert [s["id"] for s in run["steps"]] == ["topics", "research", "write", "critic", "publish"]
    assert _step(run, "topics")["status"] == wf.ST_SKIPPED     # 手动给题 → 跳过选题提案
    assert _step(run, "research")["status"] == wf.ST_PENDING
    loaded = wf.load_run(run["run_id"])
    assert loaded is not None and loaded["topic"] == "某选题"


# ---------- 选题提案与审批 ----------

def test_scheduled_run_without_topic_pauses_at_topics_proposal(env):
    _write_config(env, {})
    run = wf.create_run(wf.DEFAULT_WORKFLOW_ID)                 # 定时触发：无 topic
    wf.advance(run["run_id"])
    r = wf.load_run(run["run_id"])
    assert _step(r, "topics")["status"] == wf.ST_AWAITING
    assert _step(r, "topics")["artifact"]["kind"] == "topics"
    assert r["status"] == wf.RUN_AWAITING


def test_approve_topic_without_choice_raises(env):
    _write_config(env, {})
    run = wf.create_run(wf.DEFAULT_WORKFLOW_ID)
    wf.advance(run["run_id"])
    with pytest.raises(ValueError, match="必须先确定选题"):
        wf.approve_step(run["run_id"], 0)


def test_approve_topic_with_chosen_topic_continues(env):
    _write_config(env, {})
    run = wf.create_run(wf.DEFAULT_WORKFLOW_ID)
    wf.advance(run["run_id"])
    wf.approve_step(run["run_id"], 0, chosen_topic="候选选题A")
    r = wf.load_run(run["run_id"])
    assert r["topic"] == "候选选题A"
    assert _step(r, "topics")["status"] == wf.ST_DONE
    assert _step(r, "research")["status"] == wf.ST_AWAITING     # 定题后自动跑了研究，等审批


def test_approve_topic_with_custom_topic(env):
    _write_config(env, {})
    run = wf.create_run(wf.DEFAULT_WORKFLOW_ID)
    wf.advance(run["run_id"])
    wf.approve_step(run["run_id"], 0, chosen_topic="我自己定的新选题")
    assert wf.load_run(run["run_id"])["topic"] == "我自己定的新选题"


def test_approval_gate_pauses_each_step_then_advances(env):
    _write_config(env, {})
    run = wf.create_run(wf.DEFAULT_WORKFLOW_ID, topic="手动选题X")
    wf.advance(run["run_id"])
    r = wf.load_run(run["run_id"])
    assert _step(r, "research")["status"] == wf.ST_AWAITING      # research 产物等审批
    assert _step(r, "write")["status"] == wf.ST_PENDING

    wf.approve_step(run["run_id"], _step(r, "research")["index"])
    r = wf.load_run(run["run_id"])
    assert _step(r, "write")["status"] == wf.ST_AWAITING          # 通过后自动跑 writer 又停

    wf.approve_step(run["run_id"], _step(r, "write")["index"])
    r = wf.load_run(run["run_id"])
    assert _step(r, "critic")["status"] == wf.ST_AWAITING
    wf.approve_step(run["run_id"], _step(r, "critic")["index"])   # critic 过 → publish 自动
    r = wf.load_run(run["run_id"])
    assert _step(r, "publish")["status"] == wf.ST_DONE
    assert r["status"] == wf.RUN_DONE
    assert all(s["status"] == wf.ST_DONE for s in r["steps"] if s["id"] != "topics")


def test_auto_advance_picks_first_topic_when_no_approval(env):
    _write_config(env, _defaults_all_approval(False))
    run = wf.create_run(wf.DEFAULT_WORKFLOW_ID)                   # 无选题 → topics 自动选第一个
    wf.advance(run["run_id"])
    r = wf.load_run(run["run_id"])
    assert r["status"] == wf.RUN_DONE
    assert r["topic"] == "候选选题A"
    assert all(s["status"] == wf.ST_DONE for s in r["steps"])


# ---------- 打回 / 失败 / 重试 ----------

def test_reject_stops_run_and_can_retry(env):
    _write_config(env, {})
    run = wf.create_run(wf.DEFAULT_WORKFLOW_ID, topic="选题Z")
    wf.advance(run["run_id"])
    wf.reject_step(run["run_id"], _step(wf.load_run(run["run_id"]), "research")["index"], reason="信息不够新")
    r = wf.load_run(run["run_id"])
    assert _step(r, "research")["status"] == wf.ST_REJECTED
    assert r["status"] == wf.RUN_STOPPED
    with pytest.raises(ValueError):
        wf.approve_step(run["run_id"], _step(r, "research")["index"])
    wf.retry_step(run["run_id"], _step(r, "research")["index"])
    r = wf.load_run(run["run_id"])
    assert _step(r, "research")["status"] == wf.ST_AWAITING


def test_step_failure_marks_failed_then_retry_recovers(env, monkeypatch):
    _write_config(env, {})
    run = wf.create_run(wf.DEFAULT_WORKFLOW_ID, topic="选题Q")

    def _boom(ctx):
        raise RuntimeError("额度用尽")

    monkeypatch.setitem(wf.step_id_handlers, "research", _boom)
    wf.advance(run["run_id"])
    r = wf.load_run(run["run_id"])
    assert _step(r, "research")["status"] == wf.ST_FAILED
    assert "额度用尽" in _step(r, "research")["error"]
    assert r["status"] == wf.RUN_ERROR

    monkeypatch.setitem(wf.step_id_handlers, "research",
                        lambda ctx: {"kind": "research", "facts": 1, "payload": "事实", "preview": "事实"})
    wf.retry_step(run["run_id"], _step(r, "research")["index"])
    r = wf.load_run(run["run_id"])
    assert _step(r, "research")["status"] == wf.ST_AWAITING


def test_delete_run_removes_record(env):
    _write_config(env, {})
    run = wf.create_run(wf.DEFAULT_WORKFLOW_ID, topic="待删")
    assert wf.load_run(run["run_id"]) is not None
    assert wf.delete_run(run["run_id"]) is True
    assert wf.load_run(run["run_id"]) is None
    assert wf.delete_run(run["run_id"]) is False   # 已删，再删返回 False


def test_rerun_run_reuses_inputs(env):
    _write_config(env, {})
    old = wf.create_run(wf.DEFAULT_WORKFLOW_ID, topic="旧选题", direction="旧方向")
    new = wf.rerun_run(old["run_id"])
    assert new["run_id"] != old["run_id"]
    assert new["topic"] == "旧选题" and new["direction"] == "旧方向"
    # 复用了选题 → 选题提案这一步按约定跳过，其余步骤回到 pending（状态从头开始）
    assert [s["status"] for s in new["steps"] if s["id"] != "topics"] == \
           [wf.ST_PENDING] * (len(new["steps"]) - 1)


def test_edit_run_fields_overrides_topic_and_direction(env):
    _write_config(env, {})
    run = wf.create_run(wf.DEFAULT_WORKFLOW_ID, topic="原选题")
    wf.edit_run_fields(run["run_id"], topic="新选题", direction="新方向")
    r = wf.load_run(run["run_id"])
    assert r["topic"] == "新选题" and r["direction"] == "新方向"
    assert any("修改选题" in h["message"] for h in r["history"])


def test_advance_with_fake_handlers_needs_no_deepseek_key(env, monkeypatch):
    """回归（CI Linux fresh checkout 无 config.json）：假处理器测试推进流程时，
    引擎绝不应该主动去加载 DeepSeek key（否则没 key 就 ValueError 崩掉）。"""
    config_path = env / "config.json"
    if config_path.exists():
        config_path.unlink()                       # 明确"没有配置文件"环境

    def _boom(*a, **k):
        raise AssertionError("不该触发 key 加载——本测试步骤全是假处理器")

    monkeypatch.setattr(wf.llm_client, "load_deepseek_api_key", _boom)
    run = wf.create_run(wf.DEFAULT_WORKFLOW_ID)
    wf.advance(run["run_id"])
    r = wf.load_run(run["run_id"])
    assert _step(r, "topics")["status"] == wf.ST_AWAITING   # 正常产出候选停在审批口


def test_redo_awaiting_step_runs_again(env, monkeypatch):
    """等待审批的步骤可"重跑该步"：覆盖旧产物再等审批（解决产物为空/不满意）。"""
    _write_config(env, {})
    calls = {"n": 0}

    def fake_research(ctx):
        calls["n"] += 1
        return {"kind": "research", "facts": calls["n"], "payload": "事实", "preview": "事实"}

    monkeypatch.setitem(wf.step_id_handlers, "research", fake_research)
    run = wf.create_run(wf.DEFAULT_WORKFLOW_ID, topic="选题R")
    wf.advance(run["run_id"])                      # 第 1 次执行 research（产物 1）
    assert _step(wf.load_run(run["run_id"]), "research")["status"] == wf.ST_AWAITING
    wf.redo_step(run["run_id"], _step(wf.load_run(run["run_id"]), "research")["index"])  # 重跑
    r = wf.load_run(run["run_id"])
    assert calls["n"] == 2                         # 覆盖旧产物
    assert _step(r, "research")["status"] == wf.ST_AWAITING
    assert _step(r, "research")["artifact"]["facts"] == 2
    assert any("重跑步骤" in m["message"] for m in r["history"])


def test_redo_publish_guard_raises(env):
    """publish 交接步不许重跑（避免重复建发布单）。"""
    _write_config(env, {})
    run = wf.create_run(wf.DEFAULT_WORKFLOW_ID, topic="选题P")
    # 把 publish 改成"需要审批"以便其停在审批口（正常默认不审批）
    for s in run["steps"]:
        if s["id"] == "publish":
            s["requires_approval"] = True
    wf.save_run(run)
    wf.advance(run["run_id"])                      # 先执行到 research 审批口
    # 逐级审批内容步骤（research/write/critic），使 publish 停在审批口
    for sid in ("research", "write", "critic"):
        r = wf.load_run(run["run_id"])
        s = _step(r, sid)
        if s["status"] == wf.ST_AWAITING:
            wf.approve_step(run["run_id"], s["index"])
    r = wf.load_run(run["run_id"])
    assert _step(r, "publish")["status"] == wf.ST_AWAITING
    with pytest.raises(ValueError, match="不可重跑"):
        wf.redo_step(run["run_id"], _step(r, "publish")["index"])


def test_skip_failed_step_continues_to_next(env, monkeypatch):
    """某步因额度/网络失败后，可"跳过该步继续"，流程不卡死。"""
    _write_config(env, {})
    run = wf.create_run(wf.DEFAULT_WORKFLOW_ID, topic="选题S")

    def _boom(ctx):
        raise RuntimeError("HTTP 402 余额不足")

    monkeypatch.setitem(wf.step_id_handlers, "research", _boom)
    wf.advance(run["run_id"])
    r = wf.load_run(run["run_id"])
    assert _step(r, "research")["status"] == wf.ST_FAILED
    wf.skip_step(run["run_id"], _step(r, "research")["index"], reason="先跳过，回头补研究")
    r = wf.load_run(run["run_id"])
    assert _step(r, "research")["status"] == wf.ST_SKIPPED
    assert _step(r, "write")["status"] == wf.ST_AWAITING      # 跳过后续流程继续



# ---------- publish 步骤真实交接发布单 ----------

def test_publish_step_handoff_creates_autopublish_order(env, monkeypatch):
    monkeypatch.setitem(wf.step_id_handlers, "publish", wf._handle_publish)
    defs = _defaults_all_approval(False)
    defs[wf.DEFAULT_WORKFLOW_ID]["steps"] = [{"id": "publish", "agent": "publisher",
                                              "requires_approval": False, "label": "交接发布"}]
    _write_config(env, defs)
    run = wf.create_run(wf.DEFAULT_WORKFLOW_ID, source_text="这是要发布的定稿正文")
    wf.advance(run["run_id"])
    r = wf.load_run(run["run_id"])
    assert r["status"] == wf.RUN_DONE
    order_id = _step(r, "publish")["artifact"]["handoff_order_id"]
    order = autopublish.load_order(order_id)
    assert order is not None
    assert order["source"]["kind"] == "workflow"
    assert "这是要发布的定稿正文" in order["source"]["text"]


# ---------- 邮件通知 ----------

def test_entering_approval_sends_mail_when_configured(env, monkeypatch):
    _write_config(env, {})
    sent = []
    monkeypatch.setattr(wf.mailer, "mail_settings",
                        lambda: {"host": "s", "port": 465, "username": "u", "password": "p",
                                 "from_addr": "f@x.com", "to_addr": "t@x.com",
                                 "public_base_url": "http://brain.local"})
    monkeypatch.setattr(wf.mailer, "send_mail",
                        lambda settings, subject, html: sent.append((subject, html)) or True)
    run = wf.create_run(wf.DEFAULT_WORKFLOW_ID)                  # 无选题：选题提案进审批口 → 发邮件
    wf.advance(run["run_id"])
    r = wf.load_run(run["run_id"])
    assert sent, "进入审批口应触发审核邮件"
    subject, html = sent[0]
    assert "待审核" in subject
    assert "/admin/workflows?run=" in html and "候选选题A" in html
    assert _step(r, "topics").get("mail_sent") is True


def test_no_mail_config_skips_silently(env):
    _write_config(env, {})   # 没配 MAIL
    run = wf.create_run(wf.DEFAULT_WORKFLOW_ID)
    wf.advance(run["run_id"])
    r = wf.load_run(run["run_id"])
    assert _step(r, "topics")["status"] == wf.ST_AWAITING        # 不影响工作流，等人在后台审
    assert _step(r, "topics").get("mail_sent") in (None, False)


# ---------- 调度 ----------

def test_scheduler_starts_once_per_day_and_pauses_at_first_gate(env):
    schedule = {"time": "20:00", "enabled": True}
    _write_config(env, _defaults_all_approval(True, schedule))
    now = datetime(2026, 9, 6, 20, 0)
    started1 = wf.scheduler_tick(now=now)
    assert len(started1) == 1
    runs = wf.load_all_runs()
    assert len(runs) == 1
    assert _step(runs[0], "topics")["status"] == wf.ST_AWAITING  # 选题提案停在审批口
    started2 = wf.scheduler_tick(now=datetime(2026, 9, 6, 20, 1))
    assert started2 == []                                        # 同一天不重复
    assert len(wf.load_all_runs()) == 1
    assert len(wf.scheduler_tick(now=datetime(2026, 9, 7, 20, 0))) == 1  # 次日再来一轮
    assert len(wf.load_all_runs()) == 2


def test_scheduler_disabled_does_nothing(env):
    _write_config(env, _defaults_all_approval(True, {"time": "20:00", "enabled": False}))
    assert wf.scheduler_tick(now=datetime(2026, 9, 6, 20, 0)) == []
    assert wf.load_all_runs() == []
