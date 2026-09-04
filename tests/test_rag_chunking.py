"""
rag.py 里纯文本解析函数的单元测试——chunk_private_md() / extract_scaffold_sections() /
_content_hash() / _doc_id()。全部是零成本的字符串处理，不涉及任何真实的 DashScope/
DashVector 调用，不需要网络、不需要凭据。

之所以重点测 chunk_private_md()：这次真实扩充 6 位专家知识框架时，"用非纯数字编号（如
'8a'）会被静默吞成上一条规则的续行、不会切成新 chunk"这个坑踩中过三次（strategy/
marketing/legal），每次都是事后手动发现。这里把这个行为固化成回归测试，以后再犯同样的
错不会等到真实建索引才发现。
"""
import rag


SAMPLE_PRIVATE_MD = """# demo agent

## 角色

这是角色说明文字，不应该被切成 chunk。

## 知识框架

### 分类A

1. **规则一标题**：规则一的正文，可能很长，
   跨了好几行也算同一个 chunk。

2. **规则二标题**：规则二的正文，只有一行。

### 分类B

3. **规则三标题**：规则三的正文。

## 输出要求

- 这是输出要求说明文字，同样不应该被切成 chunk。
"""


def test_chunk_private_md_splits_into_correct_count():
    chunks = rag.chunk_private_md(SAMPLE_PRIVATE_MD)
    assert len(chunks) == 3


def test_chunk_private_md_rule_numbers_are_sequential_strings():
    chunks = rag.chunk_private_md(SAMPLE_PRIVATE_MD)
    assert [c["rule_no"] for c in chunks] == ["1", "2", "3"]


def test_chunk_private_md_tracks_section_heading():
    chunks = rag.chunk_private_md(SAMPLE_PRIVATE_MD)
    assert chunks[0]["section"] == "分类A"
    assert chunks[1]["section"] == "分类A"
    assert chunks[2]["section"] == "分类B"


def test_chunk_private_md_keeps_multiline_rule_body_together():
    chunks = rag.chunk_private_md(SAMPLE_PRIVATE_MD)
    assert "跨了好几行也算同一个 chunk" in chunks[0]["text"]


def test_chunk_private_md_does_not_chunk_role_or_output_sections():
    chunks = rag.chunk_private_md(SAMPLE_PRIVATE_MD)
    joined = "\n".join(c["text"] for c in chunks)
    assert "角色说明文字" not in joined
    assert "输出要求说明文字" not in joined


def test_chunk_private_md_ignores_no_chunks_for_scaffold_only_doc():
    text = "# demo\n\n## 角色\n\n只有说明，没有编号规则。\n"
    assert rag.chunk_private_md(text) == []


def test_chunk_private_md_absorbs_alphanumeric_suffix_as_continuation():
    """回归测试：'8a. **...**' 这种非纯数字编号，本来是想插一条新规则，实际会被正则
    忽略、整行原样并入上一条规则（#8）的正文续行，不会产生编号为 '8a' 的新 chunk——
    这正是这次真实踩过三次的坑，这里把"这就是当前的真实行为"钉死，不是在断言"这样对"。
    """
    text = (
        "## 知识框架\n\n"
        "### 分类A\n\n"
        "8. **规则八**：正文。\n\n"
        "8a. **误加的规则**：本来想单独成一条，实际会被吞掉。\n\n"
        "9. **规则九**：正文。\n"
    )
    chunks = rag.chunk_private_md(text)
    rule_nos = [c["rule_no"] for c in chunks]
    assert "8a" not in rule_nos
    assert rule_nos == ["8", "9"]
    assert "误加的规则" in chunks[0]["text"]


def test_chunk_private_md_duplicate_rule_numbers_are_possible_if_source_is_wrong():
    """同样是钉死当前行为，不是背书——chunk_private_md() 本身不做去重/唯一性校验，
    源文档里如果手滑写重了编号，这里会原样切出两条相同编号的 chunk。唯一性必须在写
    private.md 内容、追加新规则时就人工/脚本保证（这次 marketing 那次真实撞过），
    build_index() 也不会在这一层帮忙查重。
    """
    text = (
        "## 知识框架\n\n"
        "1. **规则一**：正文。\n\n"
        "1. **重复编号的规则**：正文。\n"
    )
    chunks = rag.chunk_private_md(text)
    assert [c["rule_no"] for c in chunks] == ["1", "1"]


def test_extract_scaffold_sections_keeps_role_and_output_requirements():
    scaffold = rag.extract_scaffold_sections(SAMPLE_PRIVATE_MD)
    assert "## 角色" in scaffold
    assert "## 输出要求" in scaffold
    assert "角色说明文字" in scaffold
    assert "输出要求说明文字" in scaffold


def test_extract_scaffold_sections_excludes_knowledge_framework_heading():
    scaffold = rag.extract_scaffold_sections(SAMPLE_PRIVATE_MD)
    assert "## 知识框架" not in scaffold
    assert "规则一标题" not in scaffold


def test_content_hash_is_deterministic():
    assert rag._content_hash("同样的内容") == rag._content_hash("同样的内容")


def test_content_hash_changes_when_content_changes():
    assert rag._content_hash("内容A") != rag._content_hash("内容B")


def test_doc_id_is_agent_prefixed_and_globally_disambiguating():
    """所有专家共用同一个 DashVector collection，靠这个前缀避免规则号（'1'/'2'/...）
    在不同专家之间互相覆盖——这是绕开 DashVector 2-collection 配额限制那次改造的核心。
    """
    assert rag._doc_id("finance", "1") == "finance_1"
    assert rag._doc_id("legal", "1") == "legal_1"
    assert rag._doc_id("finance", "1") != rag._doc_id("legal", "1")
