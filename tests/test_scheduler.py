"""
scheduler.py（统一 APScheduler 调度中枢）的离线测试。

验证目标 = 用户要求"新的定时逻辑要全面接管 UI 上承诺的功能入口"：
- 每个 enabled 的 dispatch 事件 → 一个 cron job（到点跑 run_dispatch_due）；
- 每个"有 publish_at + 含 approved 渠道 + 计划时间还在未来"的发布单 → 一个一次性 date job
  （到点只推这一个单）；publish_at 今天已过 → 不排（过点不补推）；没放行 → 不排；
- 每个 enabled 的工作流 → 一个 cron job；
- 每日批处理 → 固定在东八区 18:00 的 cron job。

安全隔离：
- 把 scheduler 看到的"现在"钉死在 2030 年（_FixedClock），让 date job 的 run_date 落在真实
  未来，既不会被 misfire 立即移除、也不会在测试期间真的触发；
- 四个 job 函数全部 monkeypatch 成 no-op——就算触发器到点也绝不执行任何真实发布/DeepSeek 调用，
  本测试只断言"该注册的 job 注册了没、触发器算得对不对"。
"""
import datetime as dt
import json

import pytest
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler

import autopublish
import config_store
import scheduler
import workflow
from log_setup import Clock


class _FixedClock:
    """把 scheduler 的"现在"钉死在 2030-01-01 13:00（东八区），让 publish_at 的未来/过去
    判断可复现，且 date job 的 run_date 一定落在真实未来（不会被 misfire 清掉）。"""
    TZ = Clock.TZ
    fixed = dt.datetime(2030, 1, 1, 13, 0, 0)

    @classmethod
    def now(cls):
        return cls.fixed

    @classmethod
    def today(cls):
        return cls.fixed.strftime("%Y-%m-%d")

    @classmethod
    def stamp(cls):
        return cls.fixed.strftime("%Y-%m-%d %H:%M:%S")


def _noop(*_args, **_kwargs):
    return None


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(autopublish, "QUEUE_DIR", tmp_path / "queue")
    monkeypatch.setattr(autopublish, "ARTIFACTS_DIR", tmp_path / "artifacts")
    monkeypatch.setattr(autopublish, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(workflow, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(workflow, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(scheduler, "Clock", _FixedClock)
    # job 函数全部 no-op：即便触发器到点也不执行任何真实动作
    monkeypatch.setattr(scheduler, "_job_dispatch_event", _noop)
    monkeypatch.setattr(scheduler, "_job_order_dispatch", _noop)
    monkeypatch.setattr(scheduler, "_job_workflow", _noop)
    monkeypatch.setattr(scheduler, "_job_daily_batch", _noop)

    sched = BackgroundScheduler(
        executors={"default": ThreadPoolExecutor(max_workers=1)},
        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 3600},
        timezone=Clock.TZ,
    )
    sched.start()
    monkeypatch.setattr(scheduler, "_scheduler", sched)
    yield {"tmp": tmp_path, "config_path": tmp_path / "config.json", "sched": sched}
    sched.shutdown(wait=False)


def _write_config(env, autopublish_cfg=None, workflows_cfg=None):
    data = {}
    if autopublish_cfg is not None:
        data["AUTOPUBLISH"] = autopublish_cfg
    if workflows_cfg is not None:
        data["CONTENT_WORKFLOWS"] = workflows_cfg
    env["config_path"].write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    config_store.clear_cache()


def _job_ids(env):
    return {j.id for j in env["sched"].get_jobs()}


def _make_order(publish_at=None, approve=True, channels=("wechat",)):
    order = autopublish.new_order("定时单", {"kind": "text", "text": "正文"},
                                  list(channels), publish_at=publish_at)
    autopublish.save_order(order)
    if approve:
        for ch in channels:
            autopublish.approve_channel(order["id"], ch)
    return order


# ---------- dispatch 事件 cron ----------

def test_enabled_dispatch_event_registers_cron(env):
    _write_config(env, autopublish_cfg={
        "master_enabled": True,
        "events": [{"id": "dispatch", "action": "dispatch", "time": "19:30", "enabled": True}],
        "channels": {"wechat": {"mode": "api"}},
    })
    scheduler.reschedule_autopublish()
    assert "autopub_dispatch_dispatch" in _job_ids(env)


def test_disabled_dispatch_event_not_registered(env):
    _write_config(env, autopublish_cfg={
        "master_enabled": True,
        "events": [{"id": "dispatch", "action": "dispatch", "time": "19:30", "enabled": False}],
        "channels": {},
    })
    scheduler.reschedule_autopublish()
    assert "autopub_dispatch_dispatch" not in _job_ids(env)


def test_dispatch_event_cron_fires_at_configured_time_utc8(env):
    _write_config(env, autopublish_cfg={
        "master_enabled": True,
        "events": [{"id": "dispatch", "action": "dispatch", "time": "19:30", "enabled": True}],
        "channels": {},
    })
    scheduler.reschedule_autopublish()
    job = env["sched"].get_job("autopub_dispatch_dispatch")
    assert job.next_run_time.utcoffset() == dt.timedelta(hours=8)
    assert (job.next_run_time.hour, job.next_run_time.minute) == (19, 30)


# ---------- 发布单 publish_at 一次性触发 ----------

def test_order_with_future_publish_at_gets_date_job(env):
    _write_config(env, autopublish_cfg={
        "master_enabled": True, "events": [], "channels": {"wechat": {"mode": "api"}}})
    order = _make_order(publish_at="15:00")  # 15:00 > 固定现在 13:00 → 未来
    scheduler.reschedule_autopublish()
    assert f"order_{order['id']}" in _job_ids(env)


def test_order_date_job_fires_at_publish_at_utc8(env):
    _write_config(env, autopublish_cfg={
        "master_enabled": True, "events": [], "channels": {"wechat": {"mode": "api"}}})
    order = _make_order(publish_at="15:00")
    scheduler.reschedule_autopublish()
    job = env["sched"].get_job(f"order_{order['id']}")
    assert job.next_run_time.utcoffset() == dt.timedelta(hours=8)
    assert (job.next_run_time.hour, job.next_run_time.minute) == (15, 0)


def test_order_with_past_publish_at_not_scheduled(env):
    """过点不补推：publish_at 今天已过（10:00 < 固定现在 13:00）→ 不排 date job。"""
    _write_config(env, autopublish_cfg={
        "master_enabled": True, "events": [], "channels": {"wechat": {"mode": "api"}}})
    order = _make_order(publish_at="10:00")
    scheduler.reschedule_autopublish()
    assert f"order_{order['id']}" not in _job_ids(env)


def test_order_without_publish_at_not_scheduled(env):
    """没有 publish_at 的单由 dispatch 事件统一处理，不单独排 date job。"""
    _write_config(env, autopublish_cfg={
        "master_enabled": True, "events": [], "channels": {"wechat": {"mode": "api"}}})
    order = _make_order(publish_at=None)
    scheduler.reschedule_autopublish()
    assert f"order_{order['id']}" not in _job_ids(env)


def test_unapproved_order_not_scheduled(env):
    """有 publish_at 但还没放行（drafted）→ 不排；放行是排程的前提。"""
    _write_config(env, autopublish_cfg={
        "master_enabled": True, "events": [], "channels": {"wechat": {"mode": "api"}}})
    order = _make_order(publish_at="15:00", approve=False)
    scheduler.reschedule_autopublish()
    assert f"order_{order['id']}" not in _job_ids(env)


def test_reschedule_removes_stale_order_job(env):
    """取消发布单后再 reschedule，之前的 date job 应被移除（不残留幽灵触发）。"""
    _write_config(env, autopublish_cfg={
        "master_enabled": True, "events": [], "channels": {"wechat": {"mode": "api"}}})
    order = _make_order(publish_at="15:00")
    scheduler.reschedule_autopublish()
    assert f"order_{order['id']}" in _job_ids(env)
    autopublish.cancel_order(order["id"], "不发了")
    scheduler.reschedule_autopublish()
    assert f"order_{order['id']}" not in _job_ids(env)


# ---------- 工作流 cron ----------

def test_enabled_workflow_registers_cron(env):
    _write_config(env, workflows_cfg={
        "default_daily": {"label": "每日", "direction": "",
                          "schedule": {"time": "20:00", "enabled": True},
                          "steps": [{"id": "topics", "agent": "researcher"}]},
    })
    scheduler.reschedule_workflows()
    assert "workflow_default_daily" in _job_ids(env)


def test_disabled_workflow_not_registered(env):
    _write_config(env, workflows_cfg={
        "default_daily": {"label": "每日", "direction": "",
                          "schedule": {"time": "20:00", "enabled": False},
                          "steps": [{"id": "topics", "agent": "researcher"}]},
    })
    scheduler.reschedule_workflows()
    assert "workflow_default_daily" not in _job_ids(env)


# ---------- reschedule_all / 每日批处理 ----------

def test_reschedule_all_registers_daily_batch_at_18_utc8(env):
    _write_config(env, autopublish_cfg={"master_enabled": False, "events": [], "channels": {}})
    scheduler.reschedule_all()
    job = env["sched"].get_job("daily_batch")
    assert job is not None
    assert job.next_run_time.utcoffset() == dt.timedelta(hours=8)
    assert (job.next_run_time.hour, job.next_run_time.minute) == (18, 0)


def test_reschedule_all_covers_autopublish_and_workflow(env):
    _write_config(env,
                  autopublish_cfg={"master_enabled": True,
                                   "events": [{"id": "dispatch", "action": "dispatch",
                                               "time": "19:30", "enabled": True}],
                                   "channels": {"wechat": {"mode": "api"}}},
                  workflows_cfg={"default_daily": {"label": "每日", "direction": "",
                                                   "schedule": {"time": "20:00", "enabled": True},
                                                   "steps": [{"id": "topics", "agent": "researcher"}]}})
    scheduler.reschedule_all()
    ids = _job_ids(env)
    assert {"daily_batch", "autopub_dispatch_dispatch", "workflow_default_daily"} <= ids


# ---------- 未启动时 reschedule 是安全 no-op ----------

def test_reschedule_noop_when_scheduler_absent(monkeypatch):
    """调度器没起来（如测试/未 start）时，reschedule_* 不应抛异常——ui_app 可无脑调用。"""
    monkeypatch.setattr(scheduler, "_scheduler", None)
    scheduler.reschedule_autopublish()
    scheduler.reschedule_workflows()
    scheduler.reschedule_all()
    assert scheduler.job_snapshot() == []
