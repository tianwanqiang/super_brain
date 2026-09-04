"""
/roundtable/stream/<id> 的 SSE 保活逻辑测试——2026-09-04 真实事故：gunicorn 的
--timeout（Dockerfile 里当时配的 120 秒）比这个路由原来单次 q.get(timeout=180) 的
等待时间还短，LLM 思考阶段/web_search 往返这类合理的静默间隔一旦超过 120 秒没有任何
字节发给 gunicorn，gunicorn 自己的看门狗会把整个 worker 杀掉（SIGABRT），这个服务只有
1 个 worker，worker 被杀等于整场圆桌讨论内容从 UI 上消失。

修法是短轮询 + 保活字节，这里测的就是这个行为本身：队列没有真消息时，每隔
KEEPALIVE_INTERVAL_SECONDS 秒该吐出一行 SSE 注释（保证 gunicorn 持续看到字节），累计
静默到 OVERALL_IDLE_LIMIT_SECONDS 秒才真的放弃；真消息到达时立刻原样吐出，不等下一个
轮询周期。全程用 monkeypatch 把两个时间常量调到很小，测试本身跑得很快，不真的等几十秒。

注意：generate() 直接传给 Response() 构造，Response.response 迭代出来的是生成器原样
yield 的 str（编码成 bytes 是更下游、WSGI server 层面的事），这里断言都按 str 比较。
"""
import json

import ui_app


def _iter_lines(monkeypatch, keepalive_interval, idle_limit, conversation_id):
    monkeypatch.setattr(ui_app, "KEEPALIVE_INTERVAL_SECONDS", keepalive_interval)
    monkeypatch.setattr(ui_app, "OVERALL_IDLE_LIMIT_SECONDS", idle_limit)
    with ui_app.app.test_request_context():
        response = ui_app.roundtable_stream(conversation_id)
        return iter(response.response)


def test_missing_queue_yields_error_immediately(monkeypatch):
    lines = _iter_lines(monkeypatch, 0.01, 1, "no-such-conversation")
    chunk = next(lines)
    assert "error" in chunk
    assert "没有找到这场讨论的流" in chunk


def test_empty_queue_yields_keepalive_before_any_real_message(monkeypatch):
    conversation_id = "test-keepalive-1"
    ui_app._create_stream_queue(conversation_id)
    try:
        lines = _iter_lines(monkeypatch, 0.02, 5, conversation_id)
        first_chunk = next(lines)
        assert first_chunk == ": keepalive\n\n"
    finally:
        ui_app._remove_stream_queue(conversation_id)


def test_real_message_is_yielded_without_waiting_for_keepalive_interval(monkeypatch):
    """队列里已经有真消息时，应该立刻拿到它，不需要先经过一轮保活。"""
    conversation_id = "test-keepalive-2"
    q = ui_app._create_stream_queue(conversation_id)
    q.put({"type": "content", "delta": "hello"})
    try:
        lines = _iter_lines(monkeypatch, 5, 60, conversation_id)  # 保活间隔调很大，确保不会先等到保活
        first_chunk = next(lines)
        assert "hello" in first_chunk
        assert "keepalive" not in first_chunk
    finally:
        ui_app._remove_stream_queue(conversation_id)


def test_run_done_message_ends_the_stream(monkeypatch):
    conversation_id = "test-keepalive-3"
    q = ui_app._create_stream_queue(conversation_id)
    q.put({"type": "run_done", "minutes_path": None})
    try:
        lines = _iter_lines(monkeypatch, 5, 60, conversation_id)
        chunk = next(lines)
        assert json.loads(chunk.removeprefix("data: ").strip())["type"] == "run_done"
        # 生成器应该在 run_done 之后结束，不会再有下一条
        try:
            next(lines)
            assert False, "run_done 之后不应该还有更多输出"
        except StopIteration:
            pass
    finally:
        ui_app._remove_stream_queue(conversation_id)


def test_overall_idle_limit_eventually_gives_up_gracefully(monkeypatch):
    """一直没有真消息、也没等到 OVERALL_IDLE_LIMIT_SECONDS 之前不会放弃——保留原来
    "防止某个环节卡死导致连接永远挂着"这条设计意图，只是拆成短轮询实现。
    """
    conversation_id = "test-keepalive-4"
    ui_app._create_stream_queue(conversation_id)
    try:
        # 保活间隔 0.01 秒，总共等 0.03 秒（3 轮）就放弃——测试能在几十毫秒内跑完
        lines = _iter_lines(monkeypatch, 0.01, 0.03, conversation_id)
        chunks = []
        for chunk in lines:
            chunks.append(chunk)
            if "error" in chunk:
                break
        assert any("keepalive" in c for c in chunks), "放弃之前应该先发过至少一次保活"
        assert "error" in chunks[-1] and "等待超时" in chunks[-1]
    finally:
        ui_app._remove_stream_queue(conversation_id)


def test_queue_is_removed_after_stream_ends(monkeypatch):
    conversation_id = "test-keepalive-5"
    q = ui_app._create_stream_queue(conversation_id)
    q.put({"type": "run_done"})
    lines = _iter_lines(monkeypatch, 5, 60, conversation_id)
    list(lines)  # 耗尽生成器，触发 finally 里的清理
    assert ui_app._get_stream_queue(conversation_id) is None
