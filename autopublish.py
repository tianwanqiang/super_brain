"""
super_brain autopublish - 自动化媒体发布流水线

定位：这是"多 agent 自动媒体发布"的调度/状态中枢。内容生产那一侧（content-strategist /
writer / video-prompt / ops-assistant）项目里已经有了；这里补的是"发布侧"的骨架：
    创建发布单（物料自动生成）-> 人工质检放行(approve) -> 定时发布(dispatch)

跟项目其余部分的衔接方式（保持一致的原则）：
- 发布单（PublishOrder）是唯一事实来源，落盘在 autopublish_queue/（运行时数据，gitignore）。
- 每个"动作"都是确定性的、机械的 Python 函数，不套 inbox 异步留言协议（跟 agents.yaml
  顶部"工具类助手直接调函数"的原则一致）。
- 真正花钱/产生对外影响的步骤全部有显式闸门：
    * 全局主开关 AUTOPUBLISH.master_enabled（默认关）
    * 每个渠道自己的 auto_publish / mode 开关（默认 mock/manual，绝不默认真实发布）
    * 每个发布单要 CEO 逐渠道点"批准发布"（gatekeeper 的人工放行闸门）
- 物料在创建发布单时自动生成（文本定稿 / mock 视频清单），无需手动触发。
- 视频生成默认走 mock provider（零成本、落一个 manifest 占位），真实生成 API
  （火山方舟 Seedance 等）以后按 VIDEO_GENERATORS 注册表插进来，配好 key 才生效。
- 渠道发布默认 mock/manual；真实发布 API 只接入了公众号 freepublish（publishers.py），
  且只在渠道 mode='api' + 全局开关都打开时才真的对外发布。

本模块不 import executors（executors.py 反过来 import 本模块做 executor 注册），避免循环依赖。
"""
import json
import logging
import re
import uuid
from datetime import datetime, time as dtime, timedelta
from pathlib import Path

import config_store
import publishers
from agent_registry import load_agent_registry, log_execution
from paths import (
    AUTOPUBLISH_ARTIFACTS_DIR,
    AUTOPUBLISH_QUEUE_DIR,
    CONFIG_PATH,
)

logger = logging.getLogger("super_brain.autopublish")

# ---- 路径（monkeypatch 友好的模块级常量，测试可以替换） ----
# CONFIG_PATH 从 paths.py import（统一权威常量），不是本模块自拼的。
QUEUE_DIR = AUTOPUBLISH_QUEUE_DIR
ARTIFACTS_DIR = AUTOPUBLISH_ARTIFACTS_DIR

# ---- 状态定义（单一事实来源，别处不要另造词） ----
# 渠道级状态
CH_QUEUED = "queued"          # 入池，还没生产物料
CH_DRAFTED = "drafted"        # 物料已生成，等人审
CH_APPROVED = "approved"      # CEO 已放行，可以发布
CH_PUBLISHING = "publishing"  # 正在调发布接口
CH_PUBLISHED = "published"    # 已发布
CH_NEEDS_MANUAL = "needs_manual"  # 该渠道没有可用的自动发布能力，需要人工发布
CH_FAILED = "failed"          # 尝试过但失败（保留 error 详情）
CH_SKIPPED = "skipped"        # CEO 或 planner 决定这个渠道不发
CH_CANCELLED = "cancelled"    # 订单被取消

VALID_CHANNEL_STATUSES = {
    CH_QUEUED, CH_DRAFTED, CH_APPROVED, CH_PUBLISHING, CH_PUBLISHED,
    CH_NEEDS_MANUAL, CH_FAILED, CH_SKIPPED, CH_CANCELLED,
}

# 渠道注册表：capabilities 描述这个渠道在"当前代码"里真实具备的能力
#   draft_file    能生成本地定稿文件（零成本）
#   wechat_draft  能调真实微信 API 建公众号草稿（需要 WECHAT_* 凭据）
#   freepublish   能调公众号发布接口对外发布（需要认证账号 + 渠道 mode='api'）
#   video_mock    视频只能走 mock 占位（真实生成 provider 未接入前）
# 说明字段给后台页面展示"这条渠道现在到底能做到哪一步"用，防止误以为 mock 就是真发布。
CHANNELS: dict[str, dict] = {
    "wechat": {
        "label": "微信公众号",
        "capabilities": ["draft_file", "wechat_draft", "freepublish"],
        "publish_note": "自动对外发布=调 freepublish 接口。仅已开通发布能力的认证公众号可用，"
                        "且有每日次数限制；需渠道 mode='api' + 主开关 + CEO 放行三者同时满足。",
    },
    "toutiao": {
        "label": "头条号",
        "capabilities": ["draft_file"],
        "publish_note": "无官方个人发布 API，只生成本地草稿文件（发布前人工粘贴）。"
                        "以后接头条开放平台/浏览器自动化时再扩展。",
    },
    "video": {
        "label": "视频（生成→发布）",
        "capabilities": ["video_mock"],
        "publish_note": "当前只有 mock 生成占位；真实生成 API（如火山方舟 Seedance）与平台"
                        "发布适配器未接入前，视频渠道只能停在 needs_manual。",
    },
}

# 渠道配置只有一个字段 mode（语义集中在一处，别再造冗余开关）：
#   manual —— 没有可用的自动发布执行器，发布时如实标记 needs_manual，等 CEO 人工发布
#   mock   —— 模拟发布（占位演示，不产生任何真实外部动作）
#   api    —— 走真实发布接口（目前只有 wechat 接入了 freepublish）
DEFAULT_CHANNEL_CONF = {
    "wechat": {"mode": "manual"},
    "toutiao": {"mode": "manual"},
    "video": {"mode": "manual"},
}

# 调度事件的动作类型（跟后台"立即执行"按钮共用同一套 key）
ACTION_LABELS = {
    "dispatch": "发布（把已放行且到点的内容发出去）",
}

DEFAULT_AUTOPUBLISH_CONFIG = {
    "master_enabled": False,   # 全局主开关。所有自动发布动作都受它约束，关着等于只读演示。
    "events": [                # 调度事件：time 是 HH:MM（24 小时制，服务器时区）
        {"id": "dispatch", "label": ACTION_LABELS["dispatch"], "time": "19:30",
         "action": "dispatch", "enabled": False},
    ],
    "channels": DEFAULT_CHANNEL_CONF,
    "note": "AUTOPUBLISH 配置。master_enabled/事件/渠道全部默认关闭——部署后需要人工在后台逐个打开。",
}

CONFIG_KEY = "AUTOPUBLISH"

# ---- 通用工具 ----

def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _ts_id() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def load_full_config() -> dict:
    """读整个 config.json（走 config_store 统一实现）；文件不存在/损坏返回 {}——跟
    publishers.load_config 的硬错误行为不同，调度扫描更宽容：没有配置就用默认值，
    保持"不开任何真实发布"的安全默认。"""
    return config_store.read_config_soft(path=CONFIG_PATH, cached=True)


def load_autopublish_config() -> dict:
    """读 AUTOPUBLISH 段，缺字段用默认值补全（返回的是合并后的新 dict，不会自动写回文件——
    写回只发生在后台保存配置那条路由，避免每次 tick 都动磁盘）。"""
    merged = json.loads(json.dumps(DEFAULT_AUTOPUBLISH_CONFIG))
    stored = (load_full_config().get(CONFIG_KEY) or {})
    for key in ("master_enabled", "note"):
        if key in stored:
            merged[key] = stored[key]
    if isinstance(stored.get("events"), list):
        merged["events"] = stored["events"]
    if isinstance(stored.get("channels"), dict):
        for ch, conf in DEFAULT_CHANNEL_CONF.items():
            if isinstance(stored["channels"].get(ch), dict):
                merged["channels"][ch].update(stored["channels"][ch])
    return merged


def save_autopublish_config(cfg: dict) -> None:
    """把 AUTOPUBLISH 段写回 config.json（只改这一个键，其他字段原样保留，绝不整文件覆盖）。
    返回前自动合并默认值，防止把结构写坏。写回走 config_store（带 .bak 备份）。"""
    full = load_full_config()
    full[CONFIG_KEY] = cfg
    config_store.write_config_with_backup(full, path=CONFIG_PATH)
    logger.info("AUTOPUBLISH 配置已写回 config.json")


def channel_mode(channel: str) -> str:
    return load_autopublish_config()["channels"].get(channel, {}).get("mode", "manual")


def master_enabled() -> bool:
    return bool(load_autopublish_config().get("master_enabled"))


# ---- 发布单 CRUD / 状态机 ----

def empty_channel_state(channel: str) -> dict:
    return {
        "enabled": True,
        "status": CH_QUEUED,
        "artifact": None,      # {"kind":..., "path":..., "ref":..., "note":...} 见各动作写入
        "approved_at": None,
        "published_at": None,
        "platform_ref": None,  # 发布后平台给的 id/url
        "error": None,
        "log": [],
    }


def new_order(title: str, source: dict, channels: list[str], publish_at: str | None = None,
              note: str = "") -> dict:
    """建一个新的发布单。source 形如 {"kind": "text"|"file"|"manual", "text":..., "path":...}。
    创建时自动为每个渠道生成物料（文本定稿 / mock 视频清单），不需要手动触发物料制作。
    返回订单 dict（调用方负责 save_order 落盘）。"""
    order_id = f"autopub_{_ts_id()}_{uuid.uuid4().hex[:6]}"
    order = {
        "id": order_id,
        "title": (title or "未命名素材").strip(),
        "source": source,
        "plan": {
            "publish_at": publish_at,  # 可选 "HH:MM"；不填则只在"发布"事件/按钮触发时发布
            "note": note,
        },
        "channels": {},
        "created_at": _now_str(),
        "updated_at": _now_str(),
        "history": [],
    }
    for ch in CHANNELS:
        if ch in channels:
            order["channels"][ch] = empty_channel_state(ch)

    # 物料自动生成：创建时即为每个渠道生成定稿文件，无需手动触发
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    text = (source or {}).get("text", "")
    for ch_name, st in order["channels"].items():
        if ch_name in ("wechat", "toutiao"):
            header = (
                f"# {order['title']}\n\n"
                f"> {CHANNELS[ch_name]['label']} · 本地定稿（骨架版：纯文本，未做平台排版）\n"
                f"> 发布单：{order['id']} 来源：{(source or {}).get('kind')}\n\n---\n\n"
            )
            path = ARTIFACTS_DIR / f"{order['id']}_{ch_name}.md"
            path.write_text(header + text, encoding="utf-8")
            st["artifact"] = {"kind": "text_draft", "path": str(path),
                              "note": "本地定稿（零成本骨架版，未做平台排版）"}
        elif ch_name == "video":
            manifest = {
                "order_id": order["id"], "title": order["title"],
                "provider": "mock", "status": "generated_mock",
                "video_file": None,
                "message": "mock 模式：没有调用真实视频生成 API。",
                "generated_at": _now_str(),
            }
            path = ARTIFACTS_DIR / f"{order['id']}_video_manifest.json"
            path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            st["artifact"] = {"kind": "video_manifest_mock", "path": str(path),
                              "note": "mock 视频清单（未真实生成）"}
        set_channel_status(order, ch_name, CH_DRAFTED, "物料已自动生成（创建时触发）")

    _append_history(order, "planner", "发布单创建（物料自动生成）",
                    f"渠道：{sorted(order['channels'])}，计划发布：{publish_at or '跟随事件'}")
    return order


def _order_path(order_id: str) -> Path:
    # id 是我们自己生成的（_ts_id + hex），不含路径分隔符，直接拼安全
    return QUEUE_DIR / f"{order_id}.json"


def save_order(order: dict) -> None:
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    order["updated_at"] = _now_str()
    _order_path(order["id"]).write_text(json.dumps(order, ensure_ascii=False, indent=2), encoding="utf-8")


def load_order(order_id: str) -> dict | None:
    path = _order_path(order_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError):
        logger.warning(f"发布单读取失败：{path}")
        return None


def load_all_orders() -> list[dict]:
    if not QUEUE_DIR.exists():
        return []
    orders = []
    for path in QUEUE_DIR.glob("*.json"):
        try:
            orders.append(json.loads(path.read_text(encoding="utf-8-sig")))
        except (json.JSONDecodeError, OSError):
            logger.warning(f"发布单读取失败，跳过：{path}")
    orders.sort(key=lambda o: o.get("created_at", ""), reverse=True)
    return orders


def delete_order(order_id: str) -> bool:
    path = _order_path(order_id)
    if not path.exists():
        return False
    path.unlink()
    logger.info(f"发布单已删除：{order_id}")
    return True


def _append_history(order: dict, actor: str, action: str, detail: str) -> None:
    order.setdefault("history", []).append({
        "at": _now_str(), "actor": actor, "action": action, "detail": detail,
    })


def _ch_log(order: dict, channel: str, message: str) -> None:
    st = order["channels"].get(channel)
    if st is None:
        return
    st.setdefault("log", []).append({"at": _now_str(), "message": message})


def set_channel_status(order: dict, channel: str, status: str, message: str = "") -> None:
    st = order["channels"].get(channel)
    if st is None:
        return
    if status not in VALID_CHANNEL_STATUSES:
        raise ValueError(f"非法渠道状态：{status!r}")
    st["status"] = status
    if message:
        _ch_log(order, channel, message)
    order["updated_at"] = _now_str()


def approve_channel(order_id: str, channel: str) -> dict:
    """gatekeeper 的人工放行闸门——CEO 在后台对某个渠道点"批准发布"。
    只放行 drafted/needs_manual 状态的渠道；已经放行/发布过的不重复放行。"""
    order = load_order(order_id)
    if order is None:
        raise ValueError(f"发布单不存在：{order_id}")
    st = order["channels"].get(channel)
    if st is None:
        raise ValueError(f"发布单 {order_id} 没有渠道 {channel}")
    if st["status"] not in (CH_DRAFTED, CH_NEEDS_MANUAL):
        raise ValueError(f"渠道 {channel} 当前状态 {st['status']}，不能批准（只接受 drafted/needs_manual）")
    st["status"] = CH_APPROVED
    st["approved_at"] = _now_str()
    _ch_log(order, channel, "CEO 人工放行（gatekeeper 闸门通过），等待发布调度")
    _append_history(order, "gatekeeper", "批准发布", f"渠道 {channel}")
    save_order(order)
    log_execution("gatekeeper", "批准发布", f"发布单 {order_id} 渠道 {channel}")
    return order


def reject_channel(order_id: str, channel: str, reason: str) -> dict:
    """gatekeeper 打回——退回 drafted 并记录原因（CEO 编辑后可以重新批准）。"""
    order = load_order(order_id)
    if order is None:
        raise ValueError(f"发布单不存在：{order_id}")
    st = order["channels"].get(channel)
    if st is None:
        raise ValueError(f"发布单 {order_id} 没有渠道 {channel}")
    st["status"] = CH_DRAFTED
    _ch_log(order, channel, f"CEO 打回：{reason or '（未填原因）'}")
    _append_history(order, "gatekeeper", "打回", f"渠道 {channel}：{reason}")
    save_order(order)
    return order


def cancel_order(order_id: str, reason: str) -> dict:
    order = load_order(order_id)
    if order is None:
        raise ValueError(f"发布单不存在：{order_id}")
    for st in order["channels"].values():
        if st["status"] not in (CH_PUBLISHED, CH_PUBLISHING):
            st["status"] = CH_CANCELLED
    _append_history(order, "coordinator", "取消发布单", reason or "（未填原因）")
    save_order(order)
    return order


def order_summary(order: dict) -> str:
    """从渠道状态推导订单总状态（只给 UI 展示用，判定逻辑永远看渠道状态本身）。"""
    statuses = [st["status"] for st in order["channels"].values()]
    if not statuses:
        return CH_CANCELLED
    if all(s in (CH_PUBLISHED, CH_SKIPPED, CH_CANCELLED) for s in statuses):
        return "published" if CH_PUBLISHED in statuses else CH_CANCELLED
    if any(s == CH_PUBLISHING for s in statuses):
        return "publishing"
    if any(s == CH_FAILED for s in statuses):
        return CH_FAILED
    if any(s == CH_NEEDS_MANUAL for s in statuses):
        return "needs_manual"
    if all(s == CH_APPROVED for s in statuses):
        return CH_APPROVED
    if any(s == CH_APPROVED for s in statuses):
        return "approved"
    if all(s == CH_DRAFTED for s in statuses):
        return CH_DRAFTED
    return CH_QUEUED


def run_draft_wechat_push(order_id: str) -> dict:
    """把某个发布单的公众号正文推成真实公众号草稿（调 publishers.publish_wechat_draft）。
    这是真实外部动作（微信草稿箱里会多一篇草稿），所以只由后台按钮显式触发，不进自动调度。
    需要 config.json 配好 WECHAT_* 凭据。排版走 adapt_draft_to_wechat（AI 排版，跟
    ops-assistant 路径一致），不再用纯文本包 <p> 的骨架版。"""
    order = load_order(order_id)
    if order is None:
        raise ValueError(f"发布单不存在：{order_id}")
    st = order["channels"].get("wechat")
    if st is None:
        raise ValueError(f"发布单 {order_id} 没启用公众号渠道")
    artifact = st.get("artifact") or {}
    raw_text = ""
    if artifact.get("path") and Path(artifact["path"]).exists():
        raw = Path(artifact["path"]).read_text(encoding="utf-8-sig")
        if "---" in raw:
            raw_text = raw.split("---", 1)[1].strip()
        else:
            raw_text = raw.strip()
    if not raw_text:
        raw_text = ((order.get("source") or {}).get("text") or "").strip()
    if not raw_text:
        raise ValueError("没有可推送的内容（本地定稿和发布单正文都为空）")

    import executors
    from llm_client import load_deepseek_api_key
    api_key = load_deepseek_api_key()
    title, html = executors.adapt_draft_to_wechat(raw_text, api_key)

    import imagegen
    cover_path, cover_url = imagegen.ensure_wechat_cover(title)
    if cover_path:
        artifact["cover_path"] = str(cover_path)
    artifact["cover_url"] = cover_url
    st["artifact"] = artifact
    result = publishers.publish_wechat_draft(title, html, cover_url=cover_url)
    st["artifact"]["draft_media_id"] = result["draft_media_id"]
    _ch_log(order, "wechat", f"已推送到公众号草稿箱：draft_media_id={result['draft_media_id']}")
    _append_history(order, "media-maker", "推送公众号草稿", result["draft_media_id"])
    save_order(order)
    log_execution("media-maker", "推送公众号草稿", f"发布单 {order_id}：{result}")
    return result


# ---- 动作：dispatch（发布执行 = publisher） ----

def _due_now(order: dict, now: datetime | None = None) -> bool:
    """订单计划了 publish_at（HH:MM）时，只在 ±5 分钟窗口内算"到点"；没计划则随时可发
    （由事件/按钮触发的 dispatch 执行）。"""
    plan_at = (order.get("plan") or {}).get("publish_at")
    if not plan_at:
        return True
    try:
        target = datetime.strptime(plan_at, "%H:%M").time()
    except ValueError:
        return True  # 配错了就当不限制，别把订单卡死
    now = now or datetime.now()
    cur = now.time().replace(second=0, microsecond=0)
    low = (datetime.combine(dtime.min, target) - timedelta(minutes=5)).time()
    high = (datetime.combine(dtime.min, target) + timedelta(minutes=5)).time()
    return low <= cur <= high


def _publish_wechat(order: dict, st: dict) -> str:
    """真实公众号发布：把草稿 draft_media_id 通过 freepublish 发布出去。
    前置：公众号草稿 media_id 存在 + 渠道 mode='api'。失败抛 publishers.PublishError。"""
    media_id = (st.get("artifact") or {}).get("draft_media_id")
    if not media_id:
        raise publishers.PublishError(
            "公众号没有草稿 media_id（先在后台点'推送到公众号草稿箱'，或把已有草稿 media_id 填进来）"
        )
    res = publishers.publish_wechat_article(media_id)
    return res.get("publish_id", "")


def dispatch_order(order: dict, force: bool = False) -> dict:
    """发布一个订单里所有 approved 的渠道。返回每个渠道的结果（模拟/真实/需人工）。
    规则：
    - approved + 渠道 mode='mock'   -> 标记 published（占位，不产生任何真实外部动作）
    - approved + wechat mode='api'  -> 调真实 freepublish（主开关在调用方已校验）
    - approved + 渠道 mode='manual' -> needs_manual（该渠道没有可用自动发布能力）
    - 其他状态一律不动（不是 approved 就不发，这是硬规则）。
    force=True 用于后台"立即发布"按钮：忽略订单的 publish_at 时间窗口（用户显式意图），
    但 CEO 放行 + 渠道 mode 这两个闸门仍然生效。"""
    results: dict = {}
    for channel, st in order["channels"].items():
        if st["status"] != CH_APPROVED:
            results[channel] = {"status": "skipped", "reason": f"状态 {st['status']} 未放行"}
            continue
        if not force and not _due_now(order):
            results[channel] = {"status": "skipped", "reason": "未到计划发布时间窗口"}
            continue
        mode = channel_mode(channel)
        try:
            if mode == "mock":
                st["status"] = CH_PUBLISHED
                st["published_at"] = _now_str()
                _ch_log(order, channel, "mock 模式：模拟发布完成（没有真实外部动作）")
                results[channel] = {"status": "published", "mode": "mock"}
            elif channel == "wechat" and mode == "api":
                st["status"] = CH_PUBLISHING
                save_order(order)
                publish_id = _publish_wechat(order, st)
                st["status"] = CH_PUBLISHED
                st["published_at"] = _now_str()
                st["platform_ref"] = publish_id
                _ch_log(order, channel, f"公众号已发布：publish_id={publish_id}")
                results[channel] = {"status": "published", "mode": "api", "publish_id": publish_id}
            else:
                st["status"] = CH_NEEDS_MANUAL
                _ch_log(order, channel,
                        f"渠道 mode={mode} 没有可用的自动发布执行器，需要人工发布"
                        f"（{CHANNELS[channel]['publish_note']}）")
                results[channel] = {"status": "needs_manual", "mode": mode}
        except publishers.PublishError as exc:
            st["status"] = CH_FAILED
            st["error"] = str(exc)
            _ch_log(order, channel, f"发布失败：{exc}")
            results[channel] = {"status": "failed", "error": str(exc)}
        except Exception as exc:
            logger.exception(f"[publisher] 渠道 {channel} 发布异常")
            st["status"] = CH_FAILED
            st["error"] = str(exc)
            results[channel] = {"status": "failed", "error": str(exc)}
        save_order(order)
    _append_history(order, "publisher", "发布执行", str(results))
    save_order(order)
    log_execution("publisher", "发布执行", f"发布单 {order['id']}：{results}")
    return results


def run_dispatch_due(now: datetime | None = None) -> dict:
    """调度/按钮统一入口：对每个有 approved 渠道的订单执行发布。
    返回 {"orders": {order_id: results}, "skipped": n}。"""
    if not master_enabled():
        logger.info("[dispatch] 全局主开关 master_enabled=False，跳过发布执行（安全默认）")
        return {"orders": {}, "skipped": 0, "blocked_by_master_switch": True}
    dispatched: dict = {}
    skipped = 0
    for order in load_all_orders():
        if any(st["status"] == CH_APPROVED for st in order["channels"].values()):
            dispatched[order["id"]] = dispatch_order(order)
        else:
            skipped += 1
    return {"orders": dispatched, "skipped": skipped}


# ---- 调度 tick（后台线程每分钟调一次） ----

def event_action_map() -> dict[str, dict]:
    cfg = load_autopublish_config()
    return {ev.get("id"): ev for ev in cfg.get("events", []) if ev.get("action")}


def scheduler_tick(now: datetime | None = None) -> list[str]:
    """每分钟由 ui_app 的后台线程调用。只处理 dispatch 事件（collect/draft 已删除，物料在
    创建发布单时自动生成）。dispatch 受主开关约束。"""
    now = now or datetime.now()
    today = now.strftime("%Y-%m-%d")
    cfg = load_autopublish_config()
    ran: list[str] = []
    for event in cfg.get("events", []):
        if not event.get("enabled"):
            continue
        try:
            hh, mm = str(event.get("time", "")).split(":")
            if now.strftime("%H:%M") != f"{int(hh):02d}:{int(mm):02d}":
                continue
        except (ValueError, AttributeError):
            logger.warning(f"调度事件 {event.get('id')} 的 time 格式不对：{event.get('time')!r}，跳过")
            continue
        action = event.get("action")
        if action != "dispatch":
            # 兼容旧配置中可能存在的 collect/draft 事件，直接跳过
            continue
        if not master_enabled():
            logger.info(f"调度 {today} {event.get('time')}：dispatch 被主开关拦截")
            continue
        try:
            run_dispatch_due(now=now)
            ran.append(action)
            logger.info(f"调度事件执行完成：{action}")
        except Exception:
            logger.exception(f"调度事件执行失败：{action}（已捕获，不影响下一分钟）")
    return ran


def run_action_once(action: str) -> dict:
    """后台"立即执行"按钮共用入口（admin 页面）。dispatch 会被主开关拦截时如实返回。"""
    if action == "dispatch":
        return {"dispatch": run_dispatch_due()}
    raise ValueError(f"未知动作：{action!r}")


# ---- 命令行冒烟入口（不花钱，随时可以本地验证） ----

def main() -> None:
    """python autopublish.py dispatch|status —— 手动跑一次 dispatch，或不传参数打印概况。"""
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    if len(sys.argv) >= 2 and sys.argv[1] == "dispatch":
        print("===== autopublish 手动执行：dispatch =====")
        print(json.dumps(run_action_once("dispatch"), ensure_ascii=False, indent=2))
        return

    cfg = load_autopublish_config()
    orders = load_all_orders()
    print("===== autopublish 概况 =====")
    print(f"主开关 master_enabled：{cfg['master_enabled']}")
    print("调度事件：")
    for ev in cfg["events"]:
        print(f"  - [{ev['id']}] {ev['time']} {ev['label']} enabled={ev['enabled']}")
    print(f"发布单：{len(orders)} 条")
    for o in orders[:20]:
        summary = order_summary(o)
        print(f"  - {o['id']} [{summary}] {o['title'][:40]}")
    print(f"渠道配置：{json.dumps(cfg['channels'], ensure_ascii=False)}")
    print(f"队列目录：{QUEUE_DIR}")


if __name__ == "__main__":
    main()
