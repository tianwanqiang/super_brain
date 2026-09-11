"""
super_brain scheduler - 统一定时调度中枢（APScheduler BackgroundScheduler）

背景（2026-09 收敛）：以前有三条各自 `while True: time.sleep(60)` 的轮询线程散在
ui_app.py 里——每日批处理 / 自动发布 dispatch / 内容工作流。它们各查各的表、各记各的
"今天跑过没"，问题有三：
  1. 每分钟空转，最坏差 1 分钟才触发（不准点）；
  2. "发布单 publish_at 到点自动推草稿"这种一次性触发根本没法用轮询线程优雅接管——
     dispatch 事件只在它自己那个固定时刻（如 19:30）跑一次 run_dispatch_due，
     一个 publish_at=11:00 的发布单永远等不到属于它的那次触发（UI 承诺了、后端没接上）；
  3. 三条线程三套时间判断，改一处容易漏另外两处。

收敛后：所有"定时 / 到点"逻辑都注册到这唯一的 BackgroundScheduler，ui_app 不再自己起
线程，只在配置变更时调 reschedule_*。

设计要点：
- 时区固定东八区（Clock.TZ），不依赖容器 TZ（python:3.13-slim 无 tzdata，设 TZ 会静默失效）。
- 单线程 executor（max_workers=1）：所有 job 串行执行，天然避免两个定时任务同时写发布单/
  跑工作流。但 UI 按钮（立即执行 / 立即发布 / 工作流操作）跑在 Flask 请求线程里，与 job
  并发，故仍需 DISPATCH_LOCK / WORKFLOW_LOCK 两把锁——锁定义在本模块（与具体 domain 无关），
  ui_app 的按钮和这里的 job 共用同一把，避免 scheduler.py ↔ ui_app.py 循环导入。
- MemoryJobStore（不持久化）：进程重启后按 config.json + 发布单现状重建全部 job（reschedule_all）。
- misfire 策略：cron 类（每日批处理 / 工作流 / dispatch 事件）允许当天补跑（coalesce 合并
  漏掉的多次触发）；发布单 publish_at 用一次性 DateTrigger + 小 misfire_grace_time，
  **过点不自动补推**（避免半夜服务恢复突然把一堆草稿推进公众号）。

部署约束：gunicorn 必须 `-w 1` 且不能 `--preload`（见 Dockerfile）。多 worker 会让每个进程
各起一个调度器、到点重复触发（重复花 DeepSeek 额度 / 重复推草稿）；--preload 会让调度器在
master 进程 start() 后被 fork，子进程里线程不存活、job 永不触发（静默失效）。

本模块不 import ui_app（ui_app import 本模块）；业务逻辑全部下沉在 autopublish/workflow/digest。
"""
import logging
import os
import threading

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

import autopublish
import digest
import llm_client
import workflow
from log_setup import Clock

logger = logging.getLogger("super_brain.scheduler")

# ---- UI 按钮与定时 job 共用的两把并发锁 ----
# 定义在这里（而不是 ui_app），是为了打破 scheduler ↔ ui_app 的循环导入：ui_app import
# scheduler 拿这两把锁给按钮用，scheduler 的 job 也用同一把，两边天然互斥。
DISPATCH_LOCK = threading.Lock()   # 发布执行（dispatch 事件 / 立即发布 / 发布单到点推草稿）
WORKFLOW_LOCK = threading.Lock()   # 内容工作流（定时触发 / 手动推进 / 审批）

# ---- job id 约定（按前缀批量移除，做定向 reschedule）----
_JOB_DAILY = "daily_batch"
_PREFIX_DISPATCH = "autopub_dispatch_"   # + event_id
_PREFIX_ORDER = "order_"                 # + order_id
_PREFIX_WORKFLOW = "workflow_"           # + wf_id

# 每日批处理的固定触发时刻（东八区墙钟）
_DAILY_BATCH_HOUR = 18
_DAILY_BATCH_MINUTE = 0

_scheduler: BackgroundScheduler | None = None
_started = False
# 每日批处理"今天跑过没"的进程内记忆（cron / 启动补跑 / UI 手动按钮共用，当天不重复花额度）
_last_daily_batch_date: str | None = None


# ==================== 通用工具 ====================

def _parse_hhmm(value) -> tuple[int | None, int | None]:
    """把 "HH:MM" 解析成 (hour, minute)；格式不对/越界返回 (None, None)。"""
    try:
        hh, mm = str(value).split(":")
        hh, mm = int(hh), int(mm)
    except (ValueError, AttributeError):
        return None, None
    if 0 <= hh <= 23 and 0 <= mm <= 59:
        return hh, mm
    return None, None


def _next_hhmm(hh: int, mm: int):
    """返回"今天 hh:mm"这个东八区时刻（aware datetime）；如果已经过了就返回 None
    （发布单 publish_at 过点不补推）。"""
    now = Clock.now()  # naive 东八区墙钟
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if target <= now:
        return None
    return target.replace(tzinfo=Clock.TZ)


def _remove_by_prefix(prefix: str) -> None:
    if _scheduler is None:
        return
    for job in list(_scheduler.get_jobs()):
        if job.id.startswith(prefix):
            try:
                job.remove()
            except Exception:  # job 可能刚好在执行/已被移除，忽略
                pass


def _remove_job(job_id: str) -> None:
    if _scheduler is None:
        return
    try:
        _scheduler.remove_job(job_id)
    except Exception:  # JobLookupError：本来就没有，忽略
        pass


# ==================== 每日批处理 ====================

def run_daily_batch_once(trigger_label: str) -> None:
    """跑一次每日批处理（如果今天有 opc 笔记，会真实花 DeepSeek 额度）。
    cron job / 启动补跑 / UI"手动触发"按钮共用这一份实现。跑完（无论成败）都把
    _last_daily_batch_date 记成今天，cron 当天不再重复。"""
    global _last_daily_batch_date
    try:
        api_key = llm_client.load_deepseek_api_key()
    except llm_client.DeepSeekConfigError as exc:
        logger.warning(f"每日批处理（{trigger_label}）：{exc}，跳过")
        return
    try:
        out_path = digest.run_daily_batch(api_key=api_key)
        if out_path:
            logger.info(f"每日批处理（{trigger_label}）完成：{out_path}")
        else:
            logger.info(f"每日批处理（{trigger_label}）：今天没有 opc 笔记，跳过（不生成汇总）")
    except Exception:
        logger.exception(f"每日批处理（{trigger_label}）失败")
    finally:
        _last_daily_batch_date = Clock.today()


def _job_daily_batch() -> None:
    """cron（每天 18:00）+ 启动补跑共用的 job 体。当天已跑过就直接返回。"""
    if _last_daily_batch_date == Clock.today():
        return
    run_daily_batch_once("18点自动触发")


# ==================== 自动发布 ====================

def _job_dispatch_event(event_id: str) -> None:
    """dispatch 事件到点：跑一次 run_dispatch_due（处理所有 approved 且"到点"的发布单，
    主要是没有 publish_at 的单）。run_dispatch_due 内部已受全局主开关约束。"""
    with DISPATCH_LOCK:
        try:
            result = autopublish.run_dispatch_due()
            logger.info(f"[dispatch {event_id}] 执行完成：{result}")
        except Exception:
            logger.exception(f"[dispatch {event_id}] 执行失败（已捕获，不影响下次触发）")


def _job_order_dispatch(order_id: str) -> None:
    """发布单 publish_at 到点：只推这一个单。dispatch_order 不自带主开关判断，这里显式挡一道。
    到点时若该单已无 approved 渠道（被取消/打回/已发），静默跳过。"""
    with DISPATCH_LOCK:
        if not autopublish.master_enabled():
            logger.info(f"[order {order_id}] 到点但全局主开关关闭，跳过发布")
            return
        order = autopublish.load_order(order_id)
        if order is None:
            return
        if not any(st.get("status") == autopublish.CH_APPROVED
                   for st in order.get("channels", {}).values()):
            return
        try:
            results = autopublish.dispatch_order(order)
            logger.info(f"[order {order_id}] 到点发布完成：{results}")
        except Exception:
            logger.exception(f"[order {order_id}] 到点发布失败")


def _schedule_order(order: dict) -> None:
    """给一个发布单安排 publish_at 的一次性触发（满足条件且 publish_at 还在未来才排）。
    - 没有 publish_at 的单：不排（由 dispatch 事件统一处理）；
    - 没有任何 approved 渠道：不排（还没放行 / 已终结）；
    - publish_at 今天已过：不排（过点不补推）。"""
    if _scheduler is None:
        return
    order_id = order.get("id")
    job_id = f"{_PREFIX_ORDER}{order_id}"
    _remove_job(job_id)  # 状态/时间可能变了，先清旧的

    plan_at = (order.get("plan") or {}).get("publish_at")
    if not plan_at:
        return
    hh, mm = _parse_hhmm(plan_at)
    if hh is None:
        logger.warning(f"发布单 {order_id} 的 publish_at 格式不对：{plan_at!r}，不排定时")
        return
    if not any(st.get("status") == autopublish.CH_APPROVED
               for st in order.get("channels", {}).values()):
        return
    run_date = _next_hhmm(hh, mm)
    if run_date is None:
        logger.info(f"发布单 {order_id} 的计划时间 {plan_at} 今天已过，不补推（等下次手动/次日重排）")
        return
    _scheduler.add_job(
        _job_order_dispatch, DateTrigger(run_date=run_date),
        id=job_id, args=[order_id], replace_existing=True,
        # 过点 5 分钟以上就不推了——跟 dispatch_order 里 _due_now 的 ±5 分钟窗口保持一致，
        # 也兑现"发布单过点不自动补推"的策略（服务半夜恢复不会突然推一堆草稿）。
        misfire_grace_time=300,
    )
    logger.info(f"发布单 {order_id} 已排定 {run_date:%Y-%m-%d %H:%M} 到点推草稿/发布")


def reschedule_autopublish() -> None:
    """按当前 config + 发布单现状，重建"自动发布"相关的全部 job：
    - 每个 enabled 的 dispatch 事件 → cron（到点跑 run_dispatch_due）；
    - 每个有 publish_at 且含 approved 渠道的发布单 → 一次性 DateTrigger（到点推该单）。
    在"保存调度配置 / 新建发布单 / 放行 / 打回 / 取消 / 删除 / 立即发布"之后调用。"""
    if _scheduler is None:
        return
    _remove_by_prefix(_PREFIX_DISPATCH)
    _remove_by_prefix(_PREFIX_ORDER)

    cfg = autopublish.load_autopublish_config()
    for event in cfg.get("events", []):
        if not event.get("enabled") or event.get("action") != "dispatch":
            continue
        hh, mm = _parse_hhmm(event.get("time"))
        if hh is None:
            logger.warning(f"调度事件 {event.get('id')} 时间格式不对：{event.get('time')!r}，跳过")
            continue
        _scheduler.add_job(
            _job_dispatch_event, CronTrigger(hour=hh, minute=mm),
            id=f"{_PREFIX_DISPATCH}{event.get('id')}", args=[event.get("id")],
            replace_existing=True,
        )

    for order in autopublish.load_all_orders():
        _schedule_order(order)
    logger.info("自动发布调度已重建")


# ==================== 内容工作流 ====================

def _job_workflow(wf_id: str) -> None:
    """工作流到点：开一个当天的 run 并推进到第一个审批口。run_scheduled_once 内部做当天去重。"""
    with WORKFLOW_LOCK:
        try:
            run_id = workflow.run_scheduled_once(wf_id)
            if run_id:
                logger.info(f"[workflow {wf_id}] 定时触发：run={run_id}，第一步已执行，停在审批口")
        except Exception:
            logger.exception(f"[workflow {wf_id}] 定时触发失败（已捕获，不影响其它工作流）")


def reschedule_workflows() -> None:
    """按当前 config 重建每个 enabled 工作流的 cron job。在"保存工作流定义"之后调用。"""
    if _scheduler is None:
        return
    _remove_by_prefix(_PREFIX_WORKFLOW)
    for wf_id, definition in workflow.load_workflows().items():
        schedule = definition.get("schedule") or {}
        if not schedule.get("enabled"):
            continue
        hh, mm = _parse_hhmm(schedule.get("time"))
        if hh is None:
            logger.warning(f"工作流 {wf_id} 时间格式不对：{schedule.get('time')!r}，跳过")
            continue
        _scheduler.add_job(
            _job_workflow, CronTrigger(hour=hh, minute=mm),
            id=f"{_PREFIX_WORKFLOW}{wf_id}", args=[wf_id], replace_existing=True,
        )
    logger.info("内容工作流调度已重建")


# ==================== 生命周期 ====================

def reschedule_all() -> None:
    """重建全部定时 job。每日批处理 cron 是静态的（每天 18:00），在这里加一次；
    autopublish / workflow 按 config 与发布单现状加。启动时调用。"""
    if _scheduler is None:
        return
    _remove_job(_JOB_DAILY)
    _scheduler.add_job(
        _job_daily_batch, CronTrigger(hour=_DAILY_BATCH_HOUR, minute=_DAILY_BATCH_MINUTE),
        id=_JOB_DAILY, replace_existing=True,
    )
    reschedule_autopublish()
    reschedule_workflows()


def start() -> None:
    """启动统一调度器（进程内只启动一次）。测试环境（PYTEST_CURRENT_TEST）跳过，避免
    import ui_app 时真的起线程、到点花 DeepSeek 额度。"""
    global _scheduler, _started
    if _started or _scheduler is not None:
        return
    if os.environ.get("PYTEST_CURRENT_TEST"):
        logger.info("检测到 PYTEST_CURRENT_TEST，跳过启动真实调度器（测试不应产生定时副作用）")
        return

    _scheduler = BackgroundScheduler(
        executors={"default": ThreadPoolExecutor(max_workers=1)},  # 单线程串行，job 之间不并发
        job_defaults={
            "coalesce": True,           # 漏掉的多次触发合并成一次（服务停机后恢复不狂补）
            "max_instances": 1,         # 同一 job 上一轮没跑完，下一轮不叠加
            "misfire_grace_time": 3600,  # cron 类：过点 1 小时内仍补跑一次（发布单 job 单独收窄）
        },
        timezone=Clock.TZ,              # 固定东八区，不看容器 TZ
    )
    reschedule_all()
    _scheduler.start()
    _started = True

    # 启动补跑：服务在 18 点之后才起来、今天还没跑过每日批处理 → 立即补一次。
    # 排成"现在触发"的一次性 job，让它落在调度器 worker 线程里跑，不阻塞 ui_app 导入/启动。
    now = Clock.now()
    if now.hour >= _DAILY_BATCH_HOUR and _last_daily_batch_date != now.strftime("%Y-%m-%d"):
        _scheduler.add_job(
            _job_daily_batch, DateTrigger(run_date=now.replace(tzinfo=Clock.TZ)),
            id=_JOB_DAILY + "_catchup", replace_existing=True,
        )
        logger.info("每日批处理：启动时已过 18 点且今天没跑过，已排入补跑")

    logger.info(f"统一定时调度器已启动（东八区 · 单线程串行 · 当前 job 数={len(_scheduler.get_jobs())}）")


def shutdown() -> None:
    """关停调度器（进程退出/测试清理用）。"""
    global _scheduler, _started
    if _scheduler is not None:
        try:
            _scheduler.shutdown(wait=False)
        except Exception:
            pass
    _scheduler = None
    _started = False


def is_running() -> bool:
    return _scheduler is not None and _scheduler.running


def last_daily_batch_date() -> str | None:
    """给后台页面展示“上次每日批处理是哪天跑的”（进程内记忆，重启后为 None）。"""
    return _last_daily_batch_date


def job_snapshot() -> list[dict]:
    """给后台页面/排查用：列出当前所有已排定的 job（id / 下次触发时间 / 触发器）。
    调度器没启动时返回空列表。"""
    if _scheduler is None:
        return []
    out = []
    for job in _scheduler.get_jobs():
        out.append({
            "id": job.id,
            "trigger": str(job.trigger),
            "next_run_time": job.next_run_time.strftime("%Y-%m-%d %H:%M:%S")
            if job.next_run_time else None,
        })
    out.sort(key=lambda j: j["next_run_time"] or "9999")
    return out
