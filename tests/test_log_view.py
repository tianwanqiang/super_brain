"""log_view.read_module_logs 的离线测试：
按 logger 前缀过滤 / 多行合并 / 最新在前 / limit / 大文件尾部读取 / 异常安全。
不依赖真实日志文件，全部用 tmp_path 造样本。"""
import log_view


def _write(tmp_path, text):
    p = tmp_path / "super_brain.log"
    p.write_text(text, encoding="utf-8")
    return p


def test_filters_by_module_logger_prefix(tmp_path):
    log = _write(tmp_path, "\n".join([
        "2026-09-10 09:00:01 super_brain.autopublish | 调度事件执行完成：dispatch | 发布 1 单，跳过 0 单",
        "2026-09-10 09:00:02 super_brain.workflow | [workflow] 定时触发 default_daily",
        "2026-09-10 09:00:03 super_brain.roundtable | 无关模块的日志",
    ]))
    ap = log_view.read_module_logs("autopublish", log_file=log)
    assert len(ap) == 1
    assert ap[0]["logger"] == "super_brain.autopublish"
    wf = log_view.read_module_logs("workflow", log_file=log)
    assert len(wf) == 1
    assert wf[0]["logger"] == "super_brain.workflow"


def test_includes_secondary_logger(tmp_path):
    """自动发布视图并入 publishers；工作流视图并入 content_pipeline。"""
    log = _write(tmp_path, "\n".join([
        "2026-09-10 09:00:01 super_brain.autopublish | dispatch 开始",
        "2026-09-10 09:00:05 super_brain.publishers | 公众号草稿已建 draft_media_id=xxx",
        "2026-09-10 09:00:06 super_brain.content_pipeline | 这条属于工作流视图，不该出现在自动发布",
    ]))
    ap = log_view.read_module_logs("autopublish", log_file=log)
    assert {r["logger"] for r in ap} == {"super_brain.autopublish", "super_brain.publishers"}


def test_newest_first(tmp_path):
    log = _write(tmp_path, "\n".join([
        "2026-09-10 08:00:00 super_brain.workflow | 早",
        "2026-09-10 09:00:00 super_brain.workflow | 晚",
    ]))
    wf = log_view.read_module_logs("workflow", log_file=log)
    assert wf[0]["message"] == "晚"
    assert wf[1]["message"] == "早"


def test_merges_multiline_traceback(tmp_path):
    log = _write(tmp_path, "\n".join([
        "2026-09-10 09:00:01 super_brain.autopublish | 调度事件执行失败：dispatch",
        "Traceback (most recent call last):",
        '  File "x.py", line 1, in <module>',
        "ValueError: boom",
        "2026-09-10 09:01:00 super_brain.autopublish | 下一分钟又跑了一次",
    ]))
    ap = log_view.read_module_logs("autopublish", log_file=log)
    assert len(ap) == 2
    assert ap[0]["message"] == "下一分钟又跑了一次"          # 最新在前
    assert "Traceback" in ap[1]["message"]                    # traceback 合并进上一条
    assert "ValueError: boom" in ap[1]["message"]


def test_continuation_after_nonmatching_head_is_dropped(tmp_path):
    """非匹配记录（如 llm_client 的 DEBUG dump）后面的多行内容不该被并进来。"""
    log = _write(tmp_path, "\n".join([
        "2026-09-10 09:00:01 super_brain.llm_client | 调用 DeepSeek（完整内容如下）",
        "这是一大段 prompt dump 第一行",
        "这是 dump 第二行",
        "2026-09-10 09:00:05 super_brain.autopublish | 调度事件执行完成：dispatch",
    ]))
    ap = log_view.read_module_logs("autopublish", log_file=log)
    assert len(ap) == 1
    assert "dump" not in ap[0]["message"]


def test_limit(tmp_path):
    lines = [f"2026-09-10 09:{i:02d}:00 super_brain.workflow | 第{i}条" for i in range(50)]
    log = _write(tmp_path, "\n".join(lines))
    wf = log_view.read_module_logs("workflow", limit=10, log_file=log)
    assert len(wf) == 10
    assert wf[0]["message"] == "第49条"                       # 最新在前


def test_missing_file_returns_empty(tmp_path):
    assert log_view.read_module_logs("workflow", log_file=tmp_path / "nope.log") == []


def test_unknown_module_returns_empty(tmp_path):
    log = _write(tmp_path, "2026-09-10 09:00:00 super_brain.workflow | x")
    assert log_view.read_module_logs("nonexistent", log_file=log) == []


def test_tail_read_handles_large_file(tmp_path, monkeypatch):
    """文件超过尾部窗口时，仍能正确解析出最新记录，且不会把半行当记录头。
    缩小窗口到 2000 字节，避免在测试里造 8MB 大文件。"""
    monkeypatch.setattr(log_view, "_MAX_BYTES", 2000)
    filler = "2026-09-10 01:00:00 super_brain.workflow | 填充行 padding\n"
    log = tmp_path / "super_brain.log"
    log.write_text(filler * 200 + "2026-09-10 09:00:00 super_brain.workflow | 最新的尾部行\n",
                   encoding="utf-8")
    assert log.stat().st_size > log_view._MAX_BYTES
    wf = log_view.read_module_logs("workflow", limit=5, log_file=log)
    assert wf[0]["message"] == "最新的尾部行"
    assert all(r["time"].startswith("2026-09-10") for r in wf)  # 全部是完整解析的记录
