"""
autopublish.py（agent4 发布骨架）的离线引擎测试——全部隔离到 tmp 目录：
发布单 CRUD/状态机、物料自动生成、gatekeeper 放行/打回规则、
dispatch 的三重闸门（主开关/渠道 mode/CEO 放行）。
不调用任何真实 API / 微信接口。
"""
import json

import pytest

import autopublish
import publishers


@pytest.fixture
def env(tmp_path, monkeypatch):
    """把队列/物料/配置文件全部指向 tmp，并写入默认配置。"""
    monkeypatch.setattr(autopublish, "QUEUE_DIR", tmp_path / "queue")
    monkeypatch.setattr(autopublish, "ARTIFACTS_DIR", tmp_path / "artifacts")
    config_path = tmp_path / "config.json"
    monkeypatch.setattr(autopublish, "CONFIG_PATH", config_path)
    return {"tmp": tmp_path, "config_path": config_path}


def write_config(env, master=False, channels=None):
    cfg = {
        "AUTOPUBLISH": {
            "master_enabled": master,
            "channels": channels or {
                "wechat": {"mode": "manual"},
                "toutiao": {"mode": "manual"},
                "video": {"mode": "manual"},
            },
        }
    }
    env["config_path"].write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")


def make_order(env, channels=("wechat", "toutiao"), text="素材正文"):
    order = autopublish.new_order("测试标题", {"kind": "text", "text": text}, list(channels))
    autopublish.save_order(order)
    return order


# ---------- CRUD / 状态机 ----------

def test_order_crud_and_defaults(env):
    order = make_order(env)
    loaded = autopublish.load_order(order["id"])
    assert loaded is not None and loaded["title"] == "测试标题"
    assert autopublish.order_summary(loaded) == "drafted"
    assert autopublish.delete_order(order["id"]) is True
    assert autopublish.load_order(order["id"]) is None


def test_approve_requires_drafted(env):
    order = make_order(env)
    loaded = autopublish.load_order(order["id"])
    # 物料在创建时已自动生成，状态为 drafted
    assert loaded["channels"]["wechat"]["status"] == "drafted"
    autopublish.approve_channel(order["id"], "wechat")
    assert autopublish.load_order(order["id"])["channels"]["wechat"]["status"] == "approved"


def test_cancel_sets_all_channels(env):
    order = make_order(env)
    autopublish.cancel_order(order["id"], "不发了")
    loaded = autopublish.load_order(order["id"])
    assert all(st["status"] == "cancelled" for st in loaded["channels"].values())


# ---------- 物料自动生成（创建时触发） ----------

def test_auto_draft_on_creation(env):
    order = make_order(env, channels=("wechat", "toutiao", "video"))
    loaded = autopublish.load_order(order["id"])
    assert loaded["channels"]["wechat"]["status"] == "drafted"
    assert loaded["channels"]["wechat"]["artifact"]["path"].endswith("_wechat.md")
    assert loaded["channels"]["video"]["artifact"]["kind"] == "video_manifest_mock"
    assert loaded["channels"]["video"]["status"] == "drafted"


# ---------- gatekeeper ----------

def test_full_gatekeeper_flow(env):
    write_config(env, master=True, channels={
        "wechat": {"mode": "mock"}, "toutiao": {"mode": "manual"}, "video": {"mode": "manual"}})
    order = make_order(env)
    autopublish.approve_channel(order["id"], "wechat")      # 放行
    autopublish.reject_channel(order["id"], "toutiao", "内容要再改")  # 打回
    loaded = autopublish.load_order(order["id"])
    assert loaded["channels"]["wechat"]["status"] == "approved"
    assert loaded["channels"]["toutiao"]["status"] == "drafted"
    assert "打回" in loaded["channels"]["toutiao"]["log"][-1]["message"]


# ---------- dispatch 三重闸门 ----------

def test_dispatch_master_switch_blocks_everything(env):
    write_config(env, master=False)  # 主开关关
    order = make_order(env)
    autopublish.approve_channel(order["id"], "wechat")
    result = autopublish.run_dispatch_due()
    assert result["blocked_by_master_switch"] is True
    assert autopublish.load_order(order["id"])["channels"]["wechat"]["status"] == "approved"


def test_dispatch_mock_publishes_without_external_action(env):
    write_config(env, master=True, channels={
        "wechat": {"mode": "mock"}, "toutiao": {"mode": "mock"}, "video": {"mode": "mock"}})
    order = make_order(env, channels=("wechat", "toutiao", "video"))
    for ch in ("wechat", "toutiao", "video"):
        autopublish.approve_channel(order["id"], ch)
    results = autopublish.dispatch_order(autopublish.load_order(order["id"]))
    assert all(v["status"] == "published" for v in results.values())
    loaded = autopublish.load_order(order["id"])
    assert all(st["status"] == "published" for st in loaded["channels"].values())


def test_dispatch_manual_channel_marks_needs_manual(env):
    write_config(env, master=True)  # 默认全 manual
    order = make_order(env)
    autopublish.approve_channel(order["id"], "toutiao")
    results = autopublish.dispatch_order(autopublish.load_order(order["id"]), force=True)
    assert results["toutiao"]["status"] == "needs_manual"
    assert autopublish.load_order(order["id"])["channels"]["toutiao"]["status"] == "needs_manual"


def test_unapproved_channel_never_dispatched(env):
    write_config(env, master=True, channels={
        "wechat": {"mode": "mock"}, "toutiao": {"mode": "mock"}, "video": {"mode": "mock"}})
    order = make_order(env)
    # 只放行 wechat，toutiao 保持 drafted（未放行）
    autopublish.approve_channel(order["id"], "wechat")
    results = autopublish.dispatch_order(autopublish.load_order(order["id"]), force=True)
    assert results["wechat"]["status"] == "published"
    assert results["toutiao"]["status"] == "skipped"  # 未放行：硬规则不动


def test_wechat_api_mode_pushes_to_draft_box(env, monkeypatch):
    """未认证个人号：mode=api 到点走"推公众号草稿箱"，成功置 draft_pushed（不是 published），
    并把 draft_media_id 落进 artifact/platform_ref。真实微信/DeepSeek 调用被隔离掉。"""
    write_config(env, master=True, channels={
        "wechat": {"mode": "api"}, "toutiao": {"mode": "manual"}, "video": {"mode": "manual"}})
    order = make_order(env)
    autopublish.approve_channel(order["id"], "wechat")

    def fake_push(o, st):
        st["artifact"] = st.get("artifact") or {}
        st["artifact"]["draft_media_id"] = "FAKE_MEDIA_ID"
        st["status"] = autopublish.CH_DRAFT_PUSHED
        st["platform_ref"] = "FAKE_MEDIA_ID"
        return "FAKE_MEDIA_ID"

    monkeypatch.setattr(autopublish, "_push_wechat_draft", fake_push)
    results = autopublish.dispatch_order(autopublish.load_order(order["id"]), force=True)
    assert results["wechat"]["status"] == "draft_pushed"
    loaded = autopublish.load_order(order["id"])
    assert loaded["channels"]["wechat"]["status"] == "draft_pushed"
    assert loaded["channels"]["wechat"]["artifact"]["draft_media_id"] == "FAKE_MEDIA_ID"


def test_wechat_api_mode_push_failure_marks_failed(env, monkeypatch):
    """mode=api 推草稿失败（如没配 WECHAT_* 凭据）→ failed + error 详情，绝不假装成功。"""
    write_config(env, master=True, channels={
        "wechat": {"mode": "api"}, "toutiao": {"mode": "manual"}, "video": {"mode": "manual"}})
    order = make_order(env)
    autopublish.approve_channel(order["id"], "wechat")

    def boom(o, st):
        raise publishers.PublishError("缺少 WECHAT_APP_ID / WECHAT_APP_KEY 凭据")

    monkeypatch.setattr(autopublish, "_push_wechat_draft", boom)
    results = autopublish.dispatch_order(autopublish.load_order(order["id"]), force=True)
    assert results["wechat"]["status"] == "failed"
    loaded = autopublish.load_order(order["id"])
    assert loaded["channels"]["wechat"]["error"]
