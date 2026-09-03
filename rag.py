"""
super_brain rag - 专家知识的检索基础设施

现状（在这个模块之前）：private.md 是整篇塞进 system prompt 的，不是真正的检索——
知识量一旦从现在的 10-20 条规则扩容到几十上百条，继续整篇注入会让每次调用的 prompt
越来越长、越来越贵。这个模块负责"把 private.md 切成一条条规则、向量化、按问题检索出
最相关的几条"，但目前只是基础设施——还没有接进 roundtable.py 的 prompt 构建逻辑
（那是下一步，需要先验证检索质量再决定怎么接，不能想当然直接换）。

嵌入模型用本地跑的 BAAI/bge-small-zh-v1.5（中文优化、体积小，~95MB），不调用任何付费
API——DeepSeek 官方文档确认过它没有 embeddings 接口，这是唯一不花钱的路径。索引存成
两个文件：<name>/rag_chunks.json（切块文本+元信息+源文件内容哈希）+
<name>/rag_index.npy（对应的向量矩阵）——都是从 private.md 派生出来的构建产物，不是
权威数据源，改了 private.md 之后旧索引会因为哈希对不上被自动判定过期。
"""
import hashlib
import json
import logging
import re
from pathlib import Path

import numpy as np

from paths import AGENTS_DIR

logger = logging.getLogger("super_brain.rag")

EMBEDDING_MODEL_NAME = "BAAI/bge-small-zh-v1.5"

_model = None  # 懒加载单例，避免每次调用都重新加载模型（加载本身有明显耗时）


def _get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        logger.info(f"加载本地嵌入模型 {EMBEDDING_MODEL_NAME}（第一次调用会下载，之后从本地缓存加载）")
        _model = SentenceTransformer(EMBEDDING_MODEL_NAME)
    return _model


def embed_texts(texts: list[str]) -> np.ndarray:
    """把一批文本编码成向量矩阵，shape=(len(texts), 向量维度)。"""
    if not texts:
        return np.zeros((0, 0), dtype=np.float32)
    model = _get_model()
    vectors = model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
    return np.asarray(vectors, dtype=np.float32)


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


def _index_paths(agent_name: str) -> tuple[Path, Path]:
    agent_dir = AGENTS_DIR / agent_name
    return agent_dir / "rag_chunks.json", agent_dir / "rag_index.npy"


def build_index(agent_name: str, force: bool = False) -> int:
    """给一个专家建（或重建）检索索引。返回切出来的 chunk 数量。

    force=False 时，如果 private.md 内容没变（哈希对得上），直接跳过不重建——避免每次
    调用都重新跑一遍嵌入模型；private.md 被改过之后（比如机制2的复盘建议被采纳追加），
    哈希会对不上，自动重建。
    """
    private_path = AGENTS_DIR / agent_name / "private.md"
    if not private_path.exists():
        raise FileNotFoundError(f"'{agent_name}' 没有 private.md：{private_path}")

    text = private_path.read_text(encoding="utf-8-sig")
    current_hash = _content_hash(text)

    chunks_path, index_path = _index_paths(agent_name)
    if not force and chunks_path.exists() and index_path.exists():
        try:
            existing = json.loads(chunks_path.read_text(encoding="utf-8-sig"))
            if existing.get("source_hash") == current_hash:
                logger.info(f"{agent_name} 的索引已是最新（private.md 内容哈希未变），跳过重建")
                return len(existing.get("chunks", []))
        except (json.JSONDecodeError, OSError):
            pass  # 索引文件损坏，走下面的重建逻辑

    chunks = chunk_private_md(text)
    if not chunks:
        logger.warning(f"{agent_name} 的 private.md 没有切出任何符合编号格式的规则，索引为空")

    texts = [f"{c['section']}\n{c['text']}" for c in chunks]
    vectors = embed_texts(texts)

    chunks_path.write_text(
        json.dumps({"source_hash": current_hash, "chunks": chunks}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    np.save(index_path, vectors)
    logger.info(f"{agent_name} 的 RAG 索引已建好：{len(chunks)} 条规则 -> {index_path}")
    return len(chunks)


def build_retrieved_context(agent_name: str, query: str, top_k: int = 8) -> str:
    """给 roundtable.py 用的组装函数——"## 角色"/"## 输出要求"整篇保留（不大，讲的是
    这个角色该怎么表现，每次都要用），"## 知识框架"下面的编号规则换成按当前问题检索出
    的 top_k 条，不再整篇塞。索引不存在时抛 FileNotFoundError（不静默降级成整篇注入，
    调用方应该显式决定"没有索引时怎么办"，不能在检索失败时悄悄改变行为）。
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


def _retrieval_log_path(agent_name: str) -> Path:
    return AGENTS_DIR / agent_name / "rag_retrieval_log.jsonl"


def _log_retrieval(agent_name: str, query: str, results: list[dict]) -> None:
    """每次真实检索都记一笔——不然分析看板永远没有数据可看。只记查询文本、命中的
    rule_no+相似度分数，不重复存规则正文本身（正文已经在 rag_chunks.json 里，这里
    存重复内容只会让日志文件膨胀）。这是纯本地使用数据，不是代码，进 .gitignore。
    """
    from datetime import datetime
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
    """检索一个专家知识库里跟 query 最相关的 top_k 条规则，按相似度降序返回，每条带
    similarity 分数（余弦相似度，0~1，越接近 1 越相关）。

    索引不存在时抛 FileNotFoundError，不自动帮你建索引——建索引是个明确的、有一次性
    嵌入计算成本的动作，应该由调用方显式决定"要不要现在建"，不能在一次查询请求里
    静默触发。

    log=True（默认）时会把这次检索记进 <name>/rag_retrieval_log.jsonl，供分析看板用；
    分析看板自己回放历史查询做统计时应该传 log=False，不然会把"分析"这个动作本身也
    记成一次"使用"，污染统计数据。
    """
    chunks_path, index_path = _index_paths(agent_name)
    if not chunks_path.exists() or not index_path.exists():
        raise FileNotFoundError(f"'{agent_name}' 还没有建 RAG 索引，先调用 build_index('{agent_name}')")

    data = json.loads(chunks_path.read_text(encoding="utf-8-sig"))
    chunks = data.get("chunks", [])
    if not chunks:
        return []

    vectors = np.load(index_path)
    query_vector = embed_texts([query])[0]

    # 向量已经归一化（normalize_embeddings=True），点积就是余弦相似度，不用再除以模长
    scores = vectors @ query_vector
    top_indices = np.argsort(-scores)[:top_k]

    results = [
        {**chunks[i], "similarity": float(scores[i])}
        for i in top_indices
    ]
    if log:
        _log_retrieval(agent_name, query, results)
    return results


def list_indexed_agents() -> list[str]:
    """列出所有已经建过 RAG 索引的 agent（有 rag_chunks.json 的），不依赖 agents.yaml
    的注册顺序，直接扫磁盘上真实存在的索引文件——分析看板要展示的是"实际建了什么"，
    不是"理论上该有什么"。
    """
    if not AGENTS_DIR.exists():
        return []
    return sorted(
        p.parent.name for p in AGENTS_DIR.glob("*/rag_chunks.json")
    )


def get_index_stats(agent_name: str) -> dict:
    """一个 agent 索引的静态统计——不依赖任何查询历史，索引建好当下就能算：
    规则条数、每条规则文本长度的分布（平均/最短/最长），按分类小标题分组的条数。
    用来判断"知识密度是不是均匀"——某个分类下只有 1 条、另一个分类下 15 条，
    说明这本书/这个框架的提炼深度不均衡，是切块阶段就该发现的问题。
    """
    chunks_path, _ = _index_paths(agent_name)
    if not chunks_path.exists():
        return {"exists": False}

    data = json.loads(chunks_path.read_text(encoding="utf-8-sig"))
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
    """读一个 agent 的完整检索历史（每条：时间戳、查询文本、命中的规则+相似度）。
    日志文件不存在（还没有发生过一次真实检索）时返回空列表，不报错——这是正常状态，
    不是异常。
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
    """统计每条规则（按 rule_no）在历史检索里被命中过多少次——找出"从来没被检索到过"
    的规则（可能是写得太生僻/太宽泛，跟实际问题匹配不上，或者这条规则本身就是冗余的），
    以及"被命中次数远超其他"的规则（可能太泛化，什么问题都沾边，检索区分度不够）。
    覆盖了所有已有 chunk，命中数为 0 的规则也会出现在结果里（不会被日志"从没出现过"
    这件事悄悄隐藏掉）。
    """
    chunks_path, _ = _index_paths(agent_name)
    counts: dict[str, int] = {}
    if chunks_path.exists():
        data = json.loads(chunks_path.read_text(encoding="utf-8-sig"))
        for c in data.get("chunks", []):
            counts[c["rule_no"]] = 0
    for entry in get_retrieval_log(agent_name):
        for hit in entry.get("hits", []):
            rule_no = hit.get("rule_no")
            if rule_no is not None:
                counts[rule_no] = counts.get(rule_no, 0) + 1
    return counts


def get_similarity_scores(agent_name: str) -> list[float]:
    """历史检索里所有命中条目的相似度分数拉平成一个列表，用来画分布直方图——
    整体分数普遍偏低，说明这个专家的知识库跟实际提问的匹配度不够，该往这个方向查；
    分数普遍偏高但回答质量仍然不好，问题更可能出在切块粒度或者规则写法本身。
    """
    scores = []
    for entry in get_retrieval_log(agent_name):
        for hit in entry.get("hits", []):
            if "similarity" in hit:
                scores.append(hit["similarity"])
    return scores
