"""
super_brain llm_client - 跟 DeepSeek / Tavily 打交道的所有底层调用，一处集中

从 dispatcher.py 拆出来的——这一块是纯粹的"跟大模型/搜索 API 通信"逻辑，跟 inbox
调度、agent 注册表管理完全是两回事，混在一起会让 dispatcher.py 读起来找不到重点。

四种调用形态，按需选：
- call_deepseek / call_deepseek_stream：单轮 system+user，阻塞 / 流式
- call_deepseek_messages：多轮对话（完整 messages 数组），阻塞
- call_deepseek_with_tools / call_deepseek_with_tools_stream：带 web_search 工具调用
  循环，阻塞 / 流式；tavily_api_key 为空时自动降级成普通调用，不报错
"""
import json
import logging
import urllib.request

from paths import DEEPSEEK_CONFIG_PATH, SUPER_BRAIN

logger = logging.getLogger("super_brain.llm_client")

TAVILY_CONFIG_PATH = SUPER_BRAIN / "config.json"


class DeepSeekConfigError(Exception):
    """DEEPSEEK_API_KEY 没配好时抛出——三种原因分开说清楚（文件不存在/JSON 格式错误/
    字段缺失或为空），不像 2026-09-04 之前 9 处调用点各自重复的 3 行代码那样，只检查了
    "文件存不存在"，格式错误或缺字段会直接冒出一段裸的 JSONDecodeError/KeyError 堆栈，
    没人看得出具体是哪种问题。
    """


def load_deepseek_api_key() -> str:
    """DEEPSEEK_API_KEY 是 super_brain 几乎所有功能（圆桌讨论、dispatcher 起草建议、
    专家私聊、video-prompt、定期复盘……）的核心依赖，缺了就没法用——这里跟
    load_tavily_api_key() 的"没配置就返回 None、调用方优雅降级"不一样：DeepSeek key
    缺失不是可以降级的场景，直接抛出一个原因清楚的异常，调用方各自按自己的错误展示
    习惯（RoundtableError/VideoPromptError/session 里的 roundtable_error 等）包一层。

    这是 2026-09-04 那次"服务器 config.json 缺 DEEPSEEK_API_KEY，圆桌讨论一调用就抛裸
    堆栈"事故之后，把原来分散在 9 个调用点的重复读取逻辑收拢到这一处的产物。
    """
    if not DEEPSEEK_CONFIG_PATH.exists():
        raise DeepSeekConfigError(f"找不到 DeepSeek 配置文件：{DEEPSEEK_CONFIG_PATH}")
    try:
        config = json.loads(DEEPSEEK_CONFIG_PATH.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError) as exc:
        raise DeepSeekConfigError(f"{DEEPSEEK_CONFIG_PATH} 读取/解析失败：{exc}") from exc
    if not isinstance(config, dict):
        raise DeepSeekConfigError(f"{DEEPSEEK_CONFIG_PATH} 内容不是一个 JSON 对象")
    api_key = config.get("DEEPSEEK_API_KEY")
    if not api_key or not isinstance(api_key, str) or not api_key.strip():
        raise DeepSeekConfigError(f"{DEEPSEEK_CONFIG_PATH} 里没有配置 DEEPSEEK_API_KEY 字段（或者是空值）")
    return api_key


DEEPSEEK_MODEL_DEFAULT = "deepseek-v4-pro"
DEEPSEEK_BASE_URL_DEFAULT = "https://api.deepseek.com/v1"
DEEPSEEK_MAX_TOKENS_DEFAULT = 8000


def load_deepseek_settings() -> dict:
    """跟头条 agent（G:\\code\\toutiao-agent\\Generate-ToutiaoDraft.ps1）读 Model/MaxTokens/
    BaseUrl 三个字段的逻辑完全一致，字段名也保持一致，不改名——config.json 里这三个都是
    可选字段，存在且非空就用配置值覆盖内置默认值，缺了/是空值就静默用内置默认值，不报错。
    跟 DEEPSEEK_API_KEY（硬性必需，缺了直接抛异常）不是一回事：模型/地址/token 上限
    没配置也能跑，只是用内置的默认组合。

    每次都重新读文件、不做进程内缓存——保持跟 load_deepseek_api_key()/load_tavily_api_key()
    同样的"配置改了不用重启也能生效"的行为，配置文件很小，重复读取的开销可以忽略。
    """
    settings = {
        "model": DEEPSEEK_MODEL_DEFAULT,
        "base_url": DEEPSEEK_BASE_URL_DEFAULT,
        "max_tokens": DEEPSEEK_MAX_TOKENS_DEFAULT,
    }
    if not DEEPSEEK_CONFIG_PATH.exists():
        return settings
    try:
        config = json.loads(DEEPSEEK_CONFIG_PATH.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError):
        return settings
    if not isinstance(config, dict):
        return settings

    if config.get("Model"):
        settings["model"] = config["Model"]
    if config.get("BaseUrl"):
        settings["base_url"] = config["BaseUrl"]
    if config.get("MaxTokens"):
        try:
            settings["max_tokens"] = int(config["MaxTokens"])
        except (TypeError, ValueError):
            logger.warning(
                f"config.json 里的 MaxTokens 不是合法数字：{config['MaxTokens']!r}，"
                f"用内置默认值 {DEEPSEEK_MAX_TOKENS_DEFAULT}"
            )
    return settings

WEB_SEARCH_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "搜索互联网获取真实、最新的外部信息（比如具体的市场数据、竞品动态、法规条文原文）。"
            "只在需要你已有知识框架里没有、且必须是最新/具体事实的信息时调用，不要用来查你已经"
            "知道的常识或者知识框架里已经覆盖的规则。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词，用简洁的查询词，不要用完整句子"},
            },
            "required": ["query"],
        },
    },
}


def _call_deepseek_core(messages: list[dict], api_key: str, model: str, base_url: str, max_tokens: int) -> str:
    body = json.dumps({"model": model, "max_tokens": max_tokens, "messages": messages}).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=body,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    logger.debug(f"DeepSeek 请求 -> model={model}, max_tokens={max_tokens}, messages数={len(messages)}")
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        logger.error(f"DeepSeek 调用失败：HTTP {exc.code}，model={model}，响应体：{error_body}")
        raise

    usage = data.get("usage", {})
    choice = data["choices"][0]
    finish_reason = choice.get("finish_reason")
    message = choice["message"]
    content = message["content"].strip()
    reasoning_content = message.get("reasoning_content", "")

    logger.info(
        f"DeepSeek 调用完成 -> model={model}, finish_reason={finish_reason}, "
        f"prompt_tokens={usage.get('prompt_tokens')}, "
        f"reasoning_tokens={usage.get('completion_tokens_details', {}).get('reasoning_tokens', 0)}, "
        f"completion_tokens={usage.get('completion_tokens')}, total_tokens={usage.get('total_tokens')}"
    )
    if not content:
        logger.warning(
            f"DeepSeek 返回的 content 是空的！finish_reason={finish_reason}，很可能是 max_tokens "
            f"不够、被截断在思考阶段。reasoning_content 摘要：{reasoning_content[:200]!r}"
        )
    logger.debug(f"DeepSeek 响应 content：\n{content}")
    return content


def call_deepseek(system_prompt: str, user_prompt: str, api_key: str,
                   model: str | None = None, base_url: str | None = None,
                   max_tokens: int | None = None) -> str:
    """model/base_url/max_tokens 不传（或传 None）时，从 config.json 的 Model/BaseUrl/
    MaxTokens 三个可选字段读（见 load_deepseek_settings()）；显式传参数的调用方（比如
    某些场景需要更小/更大的 max_tokens）优先级更高，配置值不会覆盖显式传入的值。
    """
    settings = load_deepseek_settings()
    return _call_deepseek_core(
        [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
        api_key, model or settings["model"], base_url or settings["base_url"],
        max_tokens or settings["max_tokens"],
    )


def call_deepseek_messages(messages: list[dict], api_key: str,
                            model: str | None = None, base_url: str | None = None,
                            max_tokens: int | None = None) -> str:
    """跟 call_deepseek 逻辑一致，只是直接接受完整的 messages 数组——多轮对话场景用
    （比如 video-prompt 的迭代修改），不是每次都只有一组 system+user。model/base_url/
    max_tokens 的配置覆盖规则跟 call_deepseek 一致。
    """
    settings = load_deepseek_settings()
    return _call_deepseek_core(
        messages, api_key, model or settings["model"], base_url or settings["base_url"],
        max_tokens or settings["max_tokens"],
    )


def call_deepseek_stream(system_prompt: str, user_prompt: str, api_key: str,
                          model: str | None = None, base_url: str | None = None,
                          max_tokens: int | None = None):
    """流式版本——逐块 yield {"type": "reasoning"|"content", "delta": str}，供 UI 实时渲染用。
    跟 call_deepseek() 是两条独立路径，不影响不需要实时展示的场景（lessons.md 写入、内部起草
    建议等）继续用阻塞版本。model/base_url/max_tokens 的配置覆盖规则跟 call_deepseek 一致。

    SSE 格式：每行 "data: {...}"，chunk 里 choices[0].delta 可能带 content 和/或
    reasoning_content（思考模型才有后者），最后一行是 "data: [DONE]"。
    """
    settings = load_deepseek_settings()
    model = model or settings["model"]
    base_url = base_url or settings["base_url"]
    max_tokens = max_tokens or settings["max_tokens"]
    body = json.dumps({
        "model": model,
        "max_tokens": max_tokens,
        "stream": True,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=body,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    logger.debug(
        f"DeepSeek 流式请求 -> model={model}, max_tokens={max_tokens}\n"
        f"--- system_prompt ---\n{system_prompt}\n--- user_prompt ---\n{user_prompt}"
    )

    full_content_parts: list[str] = []
    full_reasoning_parts: list[str] = []
    finish_reason = None

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            for raw_line in resp:
                line = raw_line.decode("utf-8").strip()
                if not line or not line.startswith("data:"):
                    continue
                payload = line[len("data:"):].strip()
                if payload == "[DONE]":
                    break
                chunk = json.loads(payload)
                choice = chunk["choices"][0]
                delta = choice.get("delta", {})
                finish_reason = choice.get("finish_reason") or finish_reason

                reasoning_delta = delta.get("reasoning_content")
                if reasoning_delta:
                    full_reasoning_parts.append(reasoning_delta)
                    yield {"type": "reasoning", "delta": reasoning_delta}

                content_delta = delta.get("content")
                if content_delta:
                    full_content_parts.append(content_delta)
                    yield {"type": "content", "delta": content_delta}
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        logger.error(f"DeepSeek 流式调用失败：HTTP {exc.code}，model={model}，响应体：{error_body}")
        raise

    full_content = "".join(full_content_parts).strip()
    logger.info(
        f"DeepSeek 流式调用完成 -> model={model}, finish_reason={finish_reason}, "
        f"content_chars={len(full_content)}, reasoning_chars={len(''.join(full_reasoning_parts))}"
    )
    if not full_content:
        logger.warning(
            f"DeepSeek 流式返回的 content 是空的！finish_reason={finish_reason}，"
            f"很可能是 max_tokens 不够、被截断在思考阶段。"
        )
    yield {"type": "done", "content": full_content}


def load_tavily_api_key() -> str | None:
    """没配置就返回 None——调用方应该优雅降级（不给模型 web_search 工具），不是报错。"""
    if not TAVILY_CONFIG_PATH.exists():
        return None
    try:
        config = json.loads(TAVILY_CONFIG_PATH.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError):
        return None
    key = config.get("TAVILY_API_KEY")
    return key or None


def tavily_search(query: str, api_key: str, max_results: int = 5) -> str:
    """调 Tavily API 搜索，把结果整理成一段可以直接作为工具执行结果喂回给模型的文本。"""
    body = json.dumps({
        "api_key": api_key,
        "query": query,
        "max_results": max_results,
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.tavily.com/search",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    results = data.get("results", [])
    if not results:
        return "没有搜到相关结果。"
    lines = [
        f"- {r.get('title', '')}：{r.get('content', '')[:300]}（来源：{r.get('url', '')}）"
        for r in results
    ]
    return "\n".join(lines)


def call_deepseek_with_tools(system_prompt: str, user_prompt: str, api_key: str,
                              tavily_api_key: str | None = None,
                              model: str | None = None, base_url: str | None = None,
                              max_tokens: int | None = None, max_tool_rounds: int = 3) -> str:
    """带工具调用循环的版本——tavily_api_key 为空时不给模型 web_search 工具，退化成普通调用，
    不报错。有 key 时，模型可以主动请求搜索，真实执行后把结果传回去继续对话，最多循环
    max_tool_rounds 次防止死循环（模型反复要求搜索、迟迟不给最终答案）。model/base_url/
    max_tokens 的配置覆盖规则跟 call_deepseek 一致。
    """
    settings = load_deepseek_settings()
    model = model or settings["model"]
    base_url = base_url or settings["base_url"]
    max_tokens = max_tokens or settings["max_tokens"]
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    tools = [WEB_SEARCH_TOOL_SCHEMA] if tavily_api_key else None
    message: dict = {}

    for round_num in range(max_tool_rounds + 1):
        body: dict = {"model": model, "max_tokens": max_tokens, "messages": messages}
        if tools:
            body["tools"] = tools
        req = urllib.request.Request(
            f"{base_url}/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            logger.error(f"DeepSeek 工具调用失败：HTTP {exc.code}，响应体：{error_body}")
            raise

        choice = data["choices"][0]
        message = choice["message"]
        usage = data.get("usage", {})
        tool_calls = message.get("tool_calls") or []
        logger.info(
            f"DeepSeek 工具调用轮次 {round_num} -> finish_reason={choice.get('finish_reason')}, "
            f"tool_calls={len(tool_calls)}, total_tokens={usage.get('total_tokens')}"
        )

        if not tool_calls:
            content = (message.get("content") or "").strip()
            if not content:
                logger.warning("DeepSeek 工具调用循环结束但 content 为空，可能被截断")
            return content

        messages.append(message)
        for call in tool_calls:
            func_name = call["function"]["name"]
            try:
                args = json.loads(call["function"]["arguments"])
            except json.JSONDecodeError:
                args = {}
            if func_name == "web_search" and tavily_api_key:
                query = args.get("query", "")
                logger.info(f"专家发起 web_search：{query!r}")
                try:
                    result_text = tavily_search(query, tavily_api_key)
                except Exception as exc:
                    result_text = f"搜索失败：{exc}"
                    logger.exception("Tavily 搜索失败")
            else:
                result_text = "这个工具当前不可用（未配置搜索 API Key）。"
            messages.append({
                "role": "tool",
                "tool_call_id": call["id"],
                "content": result_text,
            })

    logger.warning(f"工具调用循环达到最大轮数 {max_tool_rounds}，强制返回最后一次的内容")
    return (message.get("content") or "").strip()


def call_deepseek_with_tools_stream(system_prompt: str, user_prompt: str, api_key: str,
                                     tavily_api_key: str | None = None,
                                     model: str | None = None, base_url: str | None = None,
                                     max_tokens: int | None = None, max_tool_rounds: int = 3):
    """call_deepseek_with_tools 的流式版本——每一轮请求都用 stream=True，思考过程/正文
    逐块 yield（{"type": "reasoning"|"content", "delta": str}）；如果这一轮触发了工具调用
    （tool_calls 是分块传来的，按 index 累积拼成完整 JSON），真实执行后把结果传回去继续
    下一轮，不流式展示工具调用本身；直到模型给出不带 tool_calls 的最终答案，yield 一个
    {"type": "done", "content": 完整正文} 结束。model/base_url/max_tokens 的配置覆盖规则
    跟 call_deepseek 一致。
    """
    settings = load_deepseek_settings()
    model = model or settings["model"]
    base_url = base_url or settings["base_url"]
    max_tokens = max_tokens or settings["max_tokens"]
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    tools = [WEB_SEARCH_TOOL_SCHEMA] if tavily_api_key else None

    for round_num in range(max_tool_rounds + 1):
        body: dict = {"model": model, "max_tokens": max_tokens, "stream": True, "messages": messages}
        if tools:
            body["tools"] = tools
        req = urllib.request.Request(
            f"{base_url}/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            method="POST",
        )

        content_parts: list[str] = []
        tool_call_acc: dict[int, dict] = {}  # 按 index 累积，同一个 tool_call 的 arguments 分块传来
        finish_reason = None

        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                for raw_line in resp:
                    line = raw_line.decode("utf-8").strip()
                    if not line or not line.startswith("data:"):
                        continue
                    payload = line[len("data:"):].strip()
                    if payload == "[DONE]":
                        break
                    chunk = json.loads(payload)
                    choice = chunk["choices"][0]
                    delta = choice.get("delta", {})
                    finish_reason = choice.get("finish_reason") or finish_reason

                    reasoning_delta = delta.get("reasoning_content")
                    if reasoning_delta:
                        yield {"type": "reasoning", "delta": reasoning_delta}

                    content_delta = delta.get("content")
                    if content_delta:
                        content_parts.append(content_delta)
                        yield {"type": "content", "delta": content_delta}

                    for tc_delta in delta.get("tool_calls") or []:
                        idx = tc_delta.get("index", 0)
                        acc = tool_call_acc.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                        if tc_delta.get("id"):
                            acc["id"] = tc_delta["id"]
                        func = tc_delta.get("function") or {}
                        if func.get("name"):
                            acc["name"] += func["name"]
                        if func.get("arguments"):
                            acc["arguments"] += func["arguments"]
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            logger.error(f"DeepSeek 流式工具调用失败：HTTP {exc.code}，响应体：{error_body}")
            raise

        logger.info(
            f"DeepSeek 流式工具调用轮次 {round_num} -> finish_reason={finish_reason}, "
            f"tool_calls={len(tool_call_acc)}"
        )

        if not tool_call_acc:
            full_content = "".join(content_parts).strip()
            if not full_content:
                logger.warning("DeepSeek 流式工具调用循环结束但 content 为空，可能被截断")
            yield {"type": "done", "content": full_content}
            return

        # 触发了工具调用——真实执行，把结果传回去继续下一轮，不算最终答案
        messages.append({
            "role": "assistant",
            "content": "".join(content_parts) or None,
            "tool_calls": [
                {"id": acc["id"], "type": "function",
                 "function": {"name": acc["name"], "arguments": acc["arguments"]}}
                for acc in tool_call_acc.values()
            ],
        })
        for acc in tool_call_acc.values():
            try:
                args = json.loads(acc["arguments"])
            except json.JSONDecodeError:
                args = {}
            if acc["name"] == "web_search" and tavily_api_key:
                query = args.get("query", "")
                logger.info(f"专家发起 web_search（流式）：{query!r}")
                try:
                    result_text = tavily_search(query, tavily_api_key)
                except Exception as exc:
                    result_text = f"搜索失败：{exc}"
                    logger.exception("Tavily 搜索失败")
            else:
                result_text = "这个工具当前不可用（未配置搜索 API Key）。"
            messages.append({
                "role": "tool",
                "tool_call_id": acc["id"],
                "content": result_text,
            })

    logger.warning(f"流式工具调用循环达到最大轮数 {max_tool_rounds}，强制结束")
    yield {"type": "done", "content": "".join(content_parts).strip()}
