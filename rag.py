"""
super_brain rag - 专家知识的检索基础设施

现状（在这个模块之前）：private.md 是整篇塞进 system prompt 的，不是真正的检索——
知识量一旦从最初的 10-20 条规则扩容到几十上百条，继续整篇注入会让每次调用的 prompt
越来越长、越来越贵。这个模块负责"把 private.md 切成一条条规则、向量化、按问题检索出
最相关的几条"。

2026-09-03 从本地嵌入模型（sentence-transformers/torch）改成阿里云两个云端服务，原因：
服务器只有 1.9G 内存，torch 常驻占用几百 MB 到 1G+，跟一次真实的 DeepSeek 流式请求
叠加直接把 gunicorn worker 挤到被系统 OOM Kill，圆桌讨论内容当场丢失（服务器真实日志
确认过）。换成云端方案后，服务器本地不再有任何机器学习依赖，内存压力从根上解决。

两个独立的阿里云服务，凭据/职责完全分开，别混着看：
- **DashScope 文本向量化**（把文字转成向量）：sk 开头的 API Key + WorkspaceId，见
  `_dashscope_config()`
- **DashVector 向量数据库**（存向量 + 按相似度检索）：另一把独立的 API Key + 集群
  endpoint，见 `_dashvector_config()`

本地仍然保留一份不含向量的元数据文件（<name>/rag_chunks.json：切块文本 + 源文件
哈希），只是为了不用每次要看"这个专家有多少条规则、平均多长"这类统计信息时都跑去
问云端——这份文件很小（没有向量数据），不会带来内存问题。真正的向量存储和相似度检索
全部在 DashVector 那边完成，不在本地做任何向量数学运算。
"""
import hashlib
import json
import logging
import re
import urllib.request
from datetime import datetime
from pathlib import Path

from paths import AGENTS_DIR, SUPER_BRAIN

logger = logging.getLogger("super_brain.rag")

CONFIG_PATH = SUPER_BRAIN / "config.json"

EMBEDDING_MODEL = "text-embedding-v3"
EMBEDDING_DIMENSION = 1024
EMBEDDING_BATCH_SIZE = 10  # DashScope 各模型里最小的单批次限制，统一按这个切，安全

_dashvector_client = None  # 懒加载单例


class RagConfigError(Exception):
    """DashScope/DashVector 凭据没配全时抛出，提示去 config.json 补哪个字段。"""


def _load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError):
        return {}


def _dashscope_config() -> tuple[str, str]:
    """返回 (api_key, workspace_id)，任一没配就抛 RagConfigError。"""
    config = _load_config()
    api_key = config.get("DASHSCOPE_API_KEY")
    workspace_id = config.get("DASHSCOPE_WORKSPACE_ID")
    if not api_key or not workspace_id:
        raise RagConfigError(
            "config.json 里没配全 DASHSCOPE_API_KEY / DASHSCOPE_WORKSPACE_ID，"
            "文本向量化用不了。"
        )
    return api_key, workspace_id


def _dashvector_config() -> tuple[str, str]:
    """返回 (api_key, endpoint)，任一没配就抛 RagConfigError。"""
    config = _load_config()
    api_key = config.get("DASHVECTOR_API_KEY")
    endpoint = config.get("DASHVECTOR_ENDPOINT")
    if not api_key or not endpoint:
        raise RagConfigError(
            "config.json 里没配全 DASHVECTOR_API_KEY / DASHVECTOR_ENDPOINT，"
            "向量检索用不了。"
        )
    return api_key, endpoint


def _get_dashvector_client():
    global _dashvector_client
    if _dashvector_client is None:
        import dashvector
        api_key, endpoint = _dashvector_config()
        _dashvector_client = dashvector.Client(api_key=api_key, endpoint=endpoint)
    return _dashvector_client


def embed_texts(texts: list[str], text_type: str = "document") -> list[list[float]]:
    """把一批文本编码成向量列表——真实调用 DashScope 云端 API，每次都是真实的付费调用
    （极便宜，text-embedding-v3 约 ¥0.0005/千 token，且有 50 万 token 免费额度）。

    text_type："document"（存入知识库的文本，默认）或 "query"（用户提问）——两者生成的
    向量侧重点不同，检索时应该保持 query 用 query、被检索内容用 document，不能混用。

    自动按 EMBEDDING_BATCH_SIZE 分批调用，调用方不用关心批次限制。
    """
    if not texts:
        return []
    api_key, workspace_id = _dashscope_config()
    url = f"https://{workspace_id}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/embeddings"

    vectors: list[list[float]] = []
    for i in range(0, len(texts), EMBEDDING_BATCH_SIZE):
        batch = texts[i:i + EMBEDDING_BATCH_SIZE]
        body = json.dumps({
            "model": EMBEDDING_MODEL,
            "input": batch,
            "dimensions": EMBEDDING_DIMENSION,
        }).encode("utf-8")
        req = urllib.request.Request(
            url, data=body,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RagConfigError(f"DashScope 向量化调用失败（HTTP {exc.code}）：{detail[:300]}") from exc
        # OpenAI 兼容格式：data.data 是按输入顺序排列的 {embedding, index} 列表
        batch_vectors = [item["embedding"] for item in sorted(data["data"], key=lambda x: x["index"])]
        vectors.extend(batch_vectors)
    return vectors


def chunk_private_md(text: str) -> list[dict]:
    """把 private.md 切成一条条规则——每个 chunk 是一条完整的编号规则（"N. **标题**：正文"，
    正文可能跨多行），带上它所属的 "### 分类小标题" 作为上下文。不是整篇文档、也不是任意
    长度的滑动窗口切块，是跟着文档本身的结构走，保证每个检索出来的片段语义完整。

    不符合这个编号格式的内容（比如"## 角色"这类说明性段落）不会被切成 chunk——RAG 检索
    要召回的是可复用的判断规则，不是角色说明文字，这些说明文字本来就会跟着系统提示词的
    其他部分一起发给模型，不需要重复进检索索引。
    """
    lines = text.split("\n")
    chunks: list[dict] = []
    current_section = ""
    current_lines: list[str] = []
    current_rule_no: str | None = None

    def _flush():
        if current_rule_no is not None and current_lines:
            body = "\n".join(current_lines).strip()
            if body:
                chunks.append({
                    "rule_no": current_rule_no,
                    "section": current_section,
                    "text": body,
                })

    for line in lines:
        heading_match = re.match(r"^#{2,3}\s+(.+)$", line)
        rule_match = re.match(r"^(\d+)\.\s+\*\*(.+)$", line)

        if heading_match:
            _flush()
            current_section = heading_match.group(1).strip()
            current_lines = []
            current_rule_no = None
        elif rule_match:
            _flush()
            current_rule_no = rule_match.group(1)
            current_lines = [line]
        elif current_rule_no is not None:
            # 规则正文的续行（缩进延续），空行也保留，直到遇到下一条规则/标题才切断
            current_lines.append(line)

    _flush()
    return chunks


def extract_scaffold_sections(text: str) -> str:
    """从 private.md 里抽出"## 角色"和"## 输出要求"这两段说明性文字，不含"## 知识框架"
    下面那些编号规则本身。RAG 模式下，这两段每次都整篇注入（不大，讲的是"这个角色是干
    什么的、该怎么输出"，不是可检索的领域知识），编号规则改成按问题检索最相关的几条，
    不再把全部规则一起塞进去。
    """
    sections = re.split(r"(?m)^(#{2,3}\s+.+)$", text)
    # re.split 加了捕获组之后，结果是 [前导内容, 标题1, 内容1, 标题2, 内容2, ...]
    keep_titles = {"角色", "输出要求"}
    kept = [sections[0]] if sections and sections[0].strip().startswith("#") else []
    for i in range(1, len(sections), 2):
        title_line = sections[i].strip()
        title_text = re.sub(r"^#{2,3}\s+", "", title_line).strip()
        if title_text in keep_titles and i + 1 < len(sections):
            kept.append(title_line)
            kept.append(sections[i + 1])
    return "\n".join(kept).strip()


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _meta_path(agent_name: str) -> Path:
    """本地元数据文件——只存切块文本+哈希，不存向量。向量全部在 DashVector 里。"""
    return AGENTS_DIR / agent_name / "rag_chunks.json"


SHARED_COLLECTION_NAME = "super_brain_knowledge"


def _doc_id(agent_name: str, rule_no: str) -> str:
    """所有专家共用一个 collection，规则号本身不是全局唯一的（每个专家都有"规则1"），
    拼上专家名做前缀才是真正的全局唯一 id。"""
    return f"{agent_name}_{rule_no}"


def _ensure_shared_collection(client) -> None:
    """确保共享 collection 存在，建过就跳过。所有专家的向量都存进同一个 collection，
    靠 agent 字段区分——不是每个专家一个 collection：真实调用中撞到过 DashVector 集群
    "collection 数量不能超过 2 个"的配额限制，改成共享 collection 从根上绕开这个限制，
    以后加多少个专家都不会再受这个数量上限影响。
    """
    try:
        existing = client.list()
        if existing and SHARED_COLLECTION_NAME in (existing.output or []):
            return
    except Exception:
        pass
    create_rsp = client.create(SHARED_COLLECTION_NAME, dimension=EMBEDDING_DIMENSION)
    if not create_rsp and "exist" not in (create_rsp.message or "").lower():
        raise RagConfigError(f"DashVector 建共享 collection 失败：{create_rsp.message}")


def build_index(agent_name: str, force: bool = False) -> int:
    """给一个专家建（或重建）检索索引。返回切出来的 chunk 数量。

    force=False 时，如果 private.md 内容没变（哈希对得上），直接跳过不重建——避免每次
    调用都重新跑一遍向量化（真实付费调用）；private.md 被改过之后（比如机制2的复盘
    建议被采纳追加），哈希会对不上，自动重建。

    重建时只删这个专家自己上一版的向量（按本地元数据里记的旧规则号精确构造 id 列表
    删除），不会动共享 collection 里其他专家的数据。
    """
    private_path = AGENTS_DIR / agent_name / "private.md"
    if not private_path.exists():
        raise FileNotFoundError(f"'{agent_name}' 没有 private.md：{private_path}")

    text = private_path.read_text(encoding="utf-8-sig")
    current_hash = _content_hash(text)

    meta_path = _meta_path(agent_name)
    old_chunks: list[dict] = []
    if meta_path.exists():
        try:
            existing = json.loads(meta_path.read_text(encoding="utf-8-sig"))
            old_chunks = existing.get("chunks", [])
            if not force and existing.get("source_hash") == current_hash:
                logger.info(f"{agent_name} 的索引已是最新（private.md 内容哈希未变），跳过重建")
                return len(old_chunks)
        except (json.JSONDecodeError, OSError):
            pass  # 元数据文件损坏，走下面的重建逻辑

    chunks = chunk_private_md(text)
    if not chunks:
        logger.warning(f"{agent_name} 的 private.md 没有切出任何符合编号格式的规则，索引为空")
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        meta_path.write_text(
            json.dumps({"source_hash": current_hash, "chunks": []}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return 0

    texts = [f"{c['section']}\n{c['text']}" for c in chunks]
    vectors = embed_texts(texts, text_type="document")

    import dashvector
    client = _get_dashvector_client()
    _ensure_shared_collection(client)
    collection = client.get(SHARED_COLLECTION_NAME)
    if not collection:
        raise RagConfigError(f"DashVector 获取共享 collection 失败：{collection.message}")

    if old_chunks:
        old_ids = [_doc_id(agent_name, c["rule_no"]) for c in old_chunks]
        try:
            collection.delete(ids=old_ids)
        except Exception:
            logger.warning(f"{agent_name} 删除旧向量时出错，继续往下建（可能会有少量残留旧数据）")

    docs = [
        dashvector.Doc(
            id=_doc_id(agent_name, c["rule_no"]),
            vector=vectors[i],
            fields={"agent": agent_name, "section": c["section"], "text": c["text"]},
        )
        for i, c in enumerate(chunks)
    ]
    insert_rsp = collection.insert(docs)
    if not insert_rsp:
        raise RagConfigError(f"DashVector 插入向量失败：{insert_rsp.message}")

    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(
        json.dumps({"source_hash": current_hash, "chunks": chunks}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info(f"{agent_name} 的 RAG 索引已建好：{len(chunks)} 条规则 -> DashVector 共享 collection '{SHARED_COLLECTION_NAME}'")
    return len(chunks)


def _retrieval_log_path(agent_name: str) -> Path:
    return AGENTS_DIR / agent_name / "rag_retrieval_log.jsonl"


def _log_retrieval(agent_name: str, query: str, results: list[dict]) -> None:
    """每次真实检索都记一笔——不然分析看板永远没有数据可看。只记查询文本、命中的
    rule_no+相似度分数，不重复存规则正文本身。这是纯本地使用数据，不是代码，进 .gitignore。
    """
    log_path = _retrieval_log_path(agent_name)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "query": query,
        "hits": [{"rule_no": r["rule_no"], "section": r["section"], "similarity": r["similarity"]} for r in results],
    }
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def search(agent_name: str, query: str, top_k: int = 5, log: bool = True) -> list[dict]:
    """检索一个专家知识库里跟 query 最相关的 top_k 条规则——真实调用 DashScope（把 query
    转成向量）+ DashVector（按相似度检索），都是真实付费调用（都很便宜）。按相似度降序
    返回，每条带 similarity 分数（0~1，越接近 1 越相关）。

    注意：DashVector 的 `cosine` 度量原生返回的是**距离**（0 = 完全相同），不是相似度——
    这是拿一条规则的原文去检索它自己、结果确认是 0.0 实测出来的，不是猜的。这里统一转成
    "1 - 距离"，让 similarity 这个字段在全项目里（日志、分析看板、检索结果）语义一致，
    跟换 DashVector 之前用本地模型时的行为保持一样，上层代码不用因为换了向量库而跟着改。

    索引不存在（对应 collection 在 DashVector 里没建过）时抛异常，不自动帮你建索引——
    建索引是个明确的、有真实调用成本的动作，应该由调用方显式决定"要不要现在建"。

    log=True（默认）时会把这次检索记进 <name>/rag_retrieval_log.jsonl，供分析看板用；
    分析看板自己回放历史查询做统计时应该传 log=False，避免"分析"这个动作本身也被记成
    一次"使用"，污染统计数据。
    """
    meta_path = _meta_path(agent_name)
    if not meta_path.exists():
        raise FileNotFoundError(f"'{agent_name}' 还没有建过索引，先调用 build_index('{agent_name}')")

    client = _get_dashvector_client()
    collection = client.get(SHARED_COLLECTION_NAME)
    if not collection:
        raise RagConfigError(f"DashVector 获取共享 collection 失败：{collection.message}")

    query_vector = embed_texts([query], text_type="query")[0]
    # 所有专家共用一个 collection，用 filter 只检索这个专家自己的规则，不会检索到别的
    # 专家的数据——filter 语法是类 SQL 的 WHERE 子句，agent 是建索引时存进去的字段。
    rsp = collection.query(
        query_vector, topk=top_k, filter=f"agent = '{agent_name}'", output_fields=["section", "text"],
    )
    if not rsp:
        raise RagConfigError(f"DashVector 检索失败：{rsp.message}")

    # doc.id 是 "{agent_name}_{rule_no}" 这种带前缀的全局唯一 id，这里剥掉前缀还原成
    # 原始 rule_no，保持跟本地元数据/日志/分析看板里的 rule_no 格式一致。
    prefix = f"{agent_name}_"
    results = [
        {"rule_no": doc.id[len(prefix):] if doc.id.startswith(prefix) else doc.id,
         "section": doc.fields.get("section", ""), "text": doc.fields.get("text", ""),
         "similarity": 1.0 - float(doc.score)}
        for doc in rsp.output
    ]
    if log:
        _log_retrieval(agent_name, query, results)
    return results


def build_retrieved_context(agent_name: str, query: str, top_k: int = 8) -> str:
    """给 roundtable.py 用的组装函数——"## 角色"/"## 输出要求"整篇保留（不大，讲的是
    这个角色该怎么表现，每次都要用），"## 知识框架"下面的编号规则换成按当前问题检索出
    的 top_k 条，不再整篇塞。索引不存在时抛异常（不静默降级成整篇注入，调用方应该显式
    决定"没有索引时怎么办"，不能在检索失败时悄悄改变行为）。
    """
    private_path = AGENTS_DIR / agent_name / "private.md"
    full_text = private_path.read_text(encoding="utf-8-sig")
    scaffold = extract_scaffold_sections(full_text)

    results = search(agent_name, query, top_k=top_k)
    if not results:
        rules_block = "（检索没有返回任何规则——知识库可能是空的）"
    else:
        rules_block = "\n\n".join(
            f"[{r['section']}]\n{r['text']}" for r in results
        )

    return (
        f"{scaffold}\n\n"
        f"## 知识框架（RAG 检索出的、跟当前问题最相关的 {len(results)} 条规则，"
        f"不是这个专家掌握的全部规则）\n\n{rules_block}"
    )


def list_indexed_agents() -> list[str]:
    """列出所有已经建过 RAG 索引的 agent（本地有 rag_chunks.json 元数据文件的），
    不依赖 agents.yaml 的注册顺序，直接扫磁盘上真实存在的元数据文件。
    """
    if not AGENTS_DIR.exists():
        return []
    return sorted(p.parent.name for p in AGENTS_DIR.glob("*/rag_chunks.json"))


def get_index_stats(agent_name: str) -> dict:
    """一个 agent 索引的静态统计——读本地元数据文件，不调用任何云端 API，零成本。
    规则条数、每条规则文本长度的分布、按分类小标题分组的条数，用来判断"知识密度是不是
    均匀"。
    """
    meta_path = _meta_path(agent_name)
    if not meta_path.exists():
        return {"exists": False}

    data = json.loads(meta_path.read_text(encoding="utf-8-sig"))
    chunks = data.get("chunks", [])
    if not chunks:
        return {"exists": True, "chunk_count": 0}

    lengths = [len(c["text"]) for c in chunks]
    by_section: dict[str, int] = {}
    for c in chunks:
        by_section[c["section"]] = by_section.get(c["section"], 0) + 1

    return {
        "exists": True,
        "chunk_count": len(chunks),
        "avg_length": round(sum(lengths) / len(lengths)),
        "min_length": min(lengths),
        "max_length": max(lengths),
        "by_section": by_section,
    }


def get_retrieval_log(agent_name: str) -> list[dict]:
    """读一个 agent 的完整检索历史，零成本（读本地文件）。日志文件不存在时返回空列表，
    这是正常状态（还没发生过真实检索），不是异常。
    """
    log_path = _retrieval_log_path(agent_name)
    if not log_path.exists():
        return []
    entries = []
    for line in log_path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            logger.warning(f"{agent_name} 的检索日志有一行解析失败，跳过：{line[:80]}")
    return entries


def get_chunk_hit_counts(agent_name: str) -> dict[str, int]:
    """统计每条规则（按 rule_no）在历史检索里被命中过多少次——找出从没被检索到过的
    规则（可能写得太生僻/太宽泛，或者本身就是冗余的），以及命中次数远超其他的规则
    （可能太泛化，检索区分度不够）。覆盖所有已有 chunk，命中数为 0 的规则也会出现在
    结果里，不会被日志"从没出现过"这件事悄悄隐藏掉。
    """
    meta_path = _meta_path(agent_name)
    counts: dict[str, int] = {}
    if meta_path.exists():
        data = json.loads(meta_path.read_text(encoding="utf-8-sig"))
        for c in data.get("chunks", []):
            counts[c["rule_no"]] = 0
    for entry in get_retrieval_log(agent_name):
        for hit in entry.get("hits", []):
            rule_no = hit.get("rule_no")
            if rule_no is not None:
                counts[rule_no] = counts.get(rule_no, 0) + 1
    return counts


def get_similarity_scores(agent_name: str) -> list[float]:
    """历史检索里所有命中条目的相似度分数拉平成一个列表，用来画分布直方图。"""
    scores = []
    for entry in get_retrieval_log(agent_name):
        for hit in entry.get("hits", []):
            if "similarity" in hit:
                scores.append(hit["similarity"])
    return scores
