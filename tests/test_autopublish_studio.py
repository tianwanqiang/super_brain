"""内容创作台（autopublish studio）的离线测试。

覆盖：
- _studio_extract_source：上传文件路径 / 粘贴 textarea 路径 / 上传优先 / 空文件名回落 / 空输入报错 / 超限报错；
- _studio_run_generate：monkeypatch content_pipeline.run_writer_draft，断言 _studio_draft 被写入；未配 key 抛 ValueError；
- generate 路由：合法输入启动 job（同步替身，不起线程）/ 上传文件内容真的流到 run_generate / 空输入不启动 job；
- publish 路由：隔离 QUEUE_DIR/ARTIFACTS_DIR，POST title/draft/channels → 队列生成 kind=content_studio 的发布单；缺字段报错；
- clear / status 路由。

全程不联网、不碰真实队列目录：真实 LLM 调用被 monkeypatch 掉，发布单目录指到 tmp_path。
"""
import io
import json

import pytest

import autopublish
import ui_app


# ---------- fixtures ----------

@pytest.fixture
def client():
    ui_app.app.config["TESTING"] = True
    with ui_app.app.test_client() as c:
        yield c


@pytest.fixture
def isolated_dirs(tmp_path, monkeypatch):
    """把发布单队列/物料目录指到 tmp，避免测试写进真实 autopublish 运行目录。"""
    q = tmp_path / "queue"
    a = tmp_path / "artifacts"
    monkeypatch.setattr(autopublish, "QUEUE_DIR", q)
    monkeypatch.setattr(autopublish, "ARTIFACTS_DIR", a)
    return q, a


@pytest.fixture(autouse=True)
def reset_studio_state():
    """每条用例前后清空 studio 全局态，避免相互串味。"""
    ui_app._studio_draft = None
    ui_app._studio_job_active = False
    ui_app._studio_job_label = ""
    ui_app._studio_job_msg = None
    ui_app._studio_job_error = None
    yield
    ui_app._studio_draft = None
    ui_app._studio_job_active = False
    ui_app._studio_job_msg = None
    ui_app._studio_job_error = None


# ---------- 替身：够 _studio_extract_source 用的最小请求/上传对象 ----------

class _FakeUpload:
    def __init__(self, filename, content):
        self.filename = filename
        self._b = content.encode("utf-8") if isinstance(content, str) else content

    def read(self, n=-1):
        return self._b if (n is None or n < 0) else self._b[:n]


class _FakeReq:
    def __init__(self, files=None, form=None):
        self.files = files or {}
        self.form = form or {}


# ---------- _studio_extract_source ----------

def test_extract_from_upload():
    text, err = ui_app._studio_extract_source(_FakeReq(files={"doc": _FakeUpload("a.md", "文件内容")}))
    assert err is None
    assert text == "文件内容"


def test_extract_from_textarea():
    text, err = ui_app._studio_extract_source(_FakeReq(form={"source_text": "  粘贴的素材  "}))
    assert err is None
    assert text == "粘贴的素材"


def test_extract_upload_takes_priority():
    text, _ = ui_app._studio_extract_source(
        _FakeReq(files={"doc": _FakeUpload("a.md", "文件")}, form={"source_text": "粘贴"}))
    assert text == "文件"


def test_extract_empty_filename_falls_back_to_textarea():
    """浏览器未选文件时会提交一个 filename 为空的 doc 部分——应回落到 textarea。"""
    text, err = ui_app._studio_extract_source(
        _FakeReq(files={"doc": _FakeUpload("", "")}, form={"source_text": "回落内容"}))
    assert err is None
    assert text == "回落内容"


def test_extract_empty_returns_error():
    text, err = ui_app._studio_extract_source(_FakeReq())
    assert text == ""
    assert err


def test_extract_oversize_upload(monkeypatch):
    monkeypatch.setattr(ui_app, "_STUDIO_SOURCE_MAX_BYTES", 10)
    text, err = ui_app._studio_extract_source(_FakeReq(files={"doc": _FakeUpload("a.md", "x" * 50)}))
    assert text == ""
    assert err and "过大" in err


def test_extract_oversize_textarea(monkeypatch):
    monkeypatch.setattr(ui_app, "_STUDIO_SOURCE_MAX_BYTES", 10)
    text, err = ui_app._studio_extract_source(_FakeReq(form={"source_text": "y" * 50}))
    assert text == ""
    assert err and "过大" in err


# ---------- _studio_run_generate ----------

def test_run_generate_writes_studio_draft(monkeypatch):
    monkeypatch.setattr(ui_app.llm_client, "load_deepseek_api_key", lambda: "sk-test")
    monkeypatch.setattr(
        ui_app.content_pipeline, "run_writer_draft",
        lambda source, api_key=None, user_instruction=None, title_hint="":
            {"title": "生成标题", "draft": "生成正文"})
    ui_app._studio_run_generate("素材文本", "写作指令", "标题方向")
    d = ui_app._studio_draft
    assert d is not None
    assert d["title"] == "生成标题"
    assert d["draft"] == "生成正文"
    assert d["source_len"] == len("素材文本")
    assert d["created_at"]


def test_run_generate_falls_back_title(monkeypatch):
    """模型没给标题时用兜底标题，不留空。"""
    monkeypatch.setattr(ui_app.llm_client, "load_deepseek_api_key", lambda: "sk-test")
    monkeypatch.setattr(
        ui_app.content_pipeline, "run_writer_draft",
        lambda source, api_key=None, user_instruction=None, title_hint="":
            {"title": "  ", "draft": "正文"})
    ui_app._studio_run_generate("素材", "指令", "")
    assert ui_app._studio_draft["title"] == "内容创作台产出"


def test_run_generate_raises_when_key_missing(monkeypatch):
    def _boom():
        raise ui_app.llm_client.DeepSeekConfigError("没配 key")
    monkeypatch.setattr(ui_app.llm_client, "load_deepseek_api_key", _boom)
    with pytest.raises(ValueError):
        ui_app._studio_run_generate("素材", "指令", "")
    assert ui_app._studio_draft is None


# ---------- generate 路由 ----------

def test_generate_route_starts_job_on_valid_input(client, monkeypatch):
    captured = {}
    monkeypatch.setattr(ui_app, "_start_studio_action",
                        lambda label, fn, ok_msg: captured.update(label=label, ok=ok_msg) or True)
    resp = client.post("/admin/autopublish/studio/generate",
                       data={"source_text": "一些素材内容"}, follow_redirects=False)
    assert resp.status_code == 302
    assert captured.get("label") == "生成博文"


def test_generate_route_uploaded_file_reaches_run_generate(client, monkeypatch):
    """同步替身：让 start 直接执行 fn（不起线程），并记录 run_generate 入参，
    验证上传文件内容确实被抽出并流到成稿函数。"""
    recorded = {}
    monkeypatch.setattr(ui_app, "_start_studio_action", lambda label, fn, ok_msg: (fn(), True)[1])
    monkeypatch.setattr(ui_app, "_studio_run_generate",
                        lambda src, instr, hint: recorded.update(src=src, instr=instr, hint=hint))
    data = {"doc": (io.BytesIO("上传文档内容".encode("utf-8")), "note.md"), "title_hint": "方向X"}
    resp = client.post("/admin/autopublish/studio/generate", data=data,
                       content_type="multipart/form-data", follow_redirects=False)
    assert resp.status_code == 302
    assert recorded.get("src") == "上传文档内容"
    assert recorded.get("hint") == "方向X"


def test_generate_route_empty_input_does_not_start_job(client, monkeypatch):
    started = {"called": False}
    monkeypatch.setattr(ui_app, "_start_studio_action",
                        lambda label, fn, ok_msg: started.update(called=True) or True)
    resp = client.post("/admin/autopublish/studio/generate",
                       data={"source_text": "   "}, follow_redirects=False)
    assert resp.status_code == 302
    assert started["called"] is False
    assert ui_app._studio_job_active is False
    with client.session_transaction() as sess:
        assert sess.get("autopublish_error")


# ---------- publish 路由 ----------

def test_publish_route_creates_content_studio_order(client, isolated_dirs):
    q, _ = isolated_dirs
    resp = client.post("/admin/autopublish/studio/publish", data={
        "title": "推送标题", "draft": "推送正文内容", "channels": ["wechat", "toutiao"],
    }, follow_redirects=False)
    assert resp.status_code == 302
    files = list(q.glob("*.json"))
    assert len(files) == 1
    order = json.loads(files[0].read_text(encoding="utf-8"))
    assert order["source"]["kind"] == "content_studio"
    assert order["source"]["text"] == "推送正文内容"
    assert set(order["channels"].keys()) == {"wechat", "toutiao"}
    with client.session_transaction() as sess:
        assert "已推送为发布单" in (sess.get("autopublish_msg") or "")


def test_publish_route_requires_channel(client, isolated_dirs):
    q, _ = isolated_dirs
    resp = client.post("/admin/autopublish/studio/publish",
                       data={"title": "T", "draft": "D"}, follow_redirects=False)
    assert resp.status_code == 302
    with client.session_transaction() as sess:
        assert "至少勾选一个发布渠道" in (sess.get("autopublish_error") or "")
    assert list(q.glob("*.json")) == []


def test_publish_route_requires_title(client, isolated_dirs):
    resp = client.post("/admin/autopublish/studio/publish",
                       data={"draft": "D", "channels": ["wechat"]}, follow_redirects=False)
    assert resp.status_code == 302
    with client.session_transaction() as sess:
        assert "标题不能为空" in (sess.get("autopublish_error") or "")


def test_publish_route_ignores_unknown_channel(client, isolated_dirs):
    """勾了不存在的渠道应被过滤掉，只用合法渠道建单。"""
    q, _ = isolated_dirs
    resp = client.post("/admin/autopublish/studio/publish", data={
        "title": "T", "draft": "D", "channels": ["wechat", "不存在的渠道"],
    }, follow_redirects=False)
    assert resp.status_code == 302
    order = json.loads(list(q.glob("*.json"))[0].read_text(encoding="utf-8"))
    assert set(order["channels"].keys()) == {"wechat"}


# ---------- clear / status 路由 ----------

def test_clear_route_resets_draft(client):
    ui_app._studio_draft = {"title": "x", "draft": "y", "source_len": 1, "created_at": "t"}
    resp = client.post("/admin/autopublish/studio/clear", data={}, follow_redirects=False)
    assert resp.status_code == 302
    assert ui_app._studio_draft is None


def test_status_route_returns_json(client):
    resp = client.get("/admin/autopublish/studio/status")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["active"] is False
    assert {"label", "msg", "error"} <= set(data)


# ---------- 来源标签 ----------

def test_source_label_registered():
    """发布单队列里 content_studio 来源要显示友好名。"""
    assert ui_app._SOURCE_LABELS["content_studio"] == "内容创作台"
