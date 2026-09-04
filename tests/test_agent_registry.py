"""
agent_registry.py 的单元测试。load_agent_registry()/load_private_context() 会真的读
磁盘文件，用 monkeypatch 把模块里的路径常量指向 tmp_path 下的假文件，不碰真实的
agents.yaml / agents/*/private.md。
"""
import agent_registry


def test_known_agent_names_includes_literal_all():
    registry = {"finance": {}, "legal": {}}
    names = agent_registry.known_agent_names(registry)
    assert names == {"finance", "legal", "All"}


def test_load_agent_registry_indexes_by_name(tmp_path, monkeypatch):
    yaml_path = tmp_path / "agents.yaml"
    yaml_path.write_text(
        "agents:\n"
        "  - name: finance\n"
        "    type: roundtable\n"
        "  - name: legal\n"
        "    type: roundtable\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(agent_registry, "AGENTS_CONFIG_PATH", yaml_path)

    registry = agent_registry.load_agent_registry()
    assert set(registry.keys()) == {"finance", "legal"}
    assert registry["finance"]["type"] == "roundtable"


def test_load_agent_registry_missing_file_returns_empty_dict(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_registry, "AGENTS_CONFIG_PATH", tmp_path / "does-not-exist.yaml")
    assert agent_registry.load_agent_registry() == {}


def test_load_agent_registry_skips_entries_without_name(tmp_path, monkeypatch):
    yaml_path = tmp_path / "agents.yaml"
    yaml_path.write_text(
        "agents:\n"
        "  - type: roundtable\n"  # 没有 name 字段
        "  - name: finance\n"
        "    type: roundtable\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(agent_registry, "AGENTS_CONFIG_PATH", yaml_path)
    registry = agent_registry.load_agent_registry()
    assert set(registry.keys()) == {"finance"}


def test_load_agent_registry_invalid_yaml_returns_empty_dict(tmp_path, monkeypatch):
    yaml_path = tmp_path / "agents.yaml"
    yaml_path.write_text("agents: [this is not: valid: yaml:::", encoding="utf-8")
    monkeypatch.setattr(agent_registry, "AGENTS_CONFIG_PATH", yaml_path)
    assert agent_registry.load_agent_registry() == {}


def test_load_private_context_reads_real_file(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_registry, "SUPER_BRAIN", tmp_path)
    private_dir = tmp_path / "agents" / "finance"
    private_dir.mkdir(parents=True)
    (private_dir / "private.md").write_text("# finance agent\n\n知识框架内容", encoding="utf-8")

    registry = {"finance": {"knowledge_path": "agents/finance/private.md"}}
    context = agent_registry.load_private_context("finance", registry)
    assert "知识框架内容" in context


def test_load_private_context_missing_knowledge_path_field():
    registry = {"finance": {}}
    context = agent_registry.load_private_context("finance", registry)
    assert "没有为这个 agent 登记 knowledge_path" in context


def test_load_private_context_knowledge_path_points_to_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_registry, "SUPER_BRAIN", tmp_path)
    registry = {"finance": {"knowledge_path": "agents/finance/private.md"}}
    context = agent_registry.load_private_context("finance", registry)
    assert "但文件不存在" in context


def test_build_system_prompt_includes_citation_requirement_when_flagged():
    registry = {
        "finance": {
            "type": "roundtable",
            "description": "财务专家",
            "output_requires_citation": True,
        }
    }
    prompt = agent_registry.build_system_prompt("finance", registry, "私有知识框架正文")
    assert "依据#X" in prompt or "依据" in prompt
    assert "私有知识框架正文" in prompt


def test_build_system_prompt_includes_disclaimer_when_flagged():
    registry = {
        "legal": {
            "type": "roundtable",
            "description": "法律专家",
            "disclaimer_required": True,
        }
    }
    prompt = agent_registry.build_system_prompt("legal", registry, "正文")
    assert "咨询" in prompt


def test_build_system_prompt_omits_roundtable_only_sections_for_non_roundtable_agent():
    registry = {
        "writer": {
            "type": "executor",
            "description": "写作助手",
            "output_requires_citation": True,  # 非 roundtable 类型，这个字段不应该生效
        }
    }
    prompt = agent_registry.build_system_prompt("writer", registry, "正文")
    assert "依据#X" not in prompt
