"""
publishers.get_wechat_drafts_dir() / get_toutiao_drafts_dir() 的测试——2026-09-04 被指出：
公众号预览文件之前借用了头条的目录（get_toutiao_drafts_dir()），而头条目录没配置时的
默认值依赖兄弟项目 toutiao-agent 的目录结构（OPC_ROOT/toutiao-agent/drafts），服务器上
这个兄弟项目根本不存在，公众号内容因此被存进一个语义上不相关、服务器上凭空造出来的
目录里。这里把公众号拆成独立的 get_wechat_drafts_dir()，默认值只依赖 SUPER_BRAIN（保证
本机/服务器都能正确解析，不依赖任何兄弟项目是否存在），两者互不影响。
"""
import json

import publishers


def test_wechat_drafts_dir_default_does_not_depend_on_opc_root_or_toutiao():
    """核心诉求：默认值不能包含 'toutiao' 这个词，也不能挂在 OPC_ROOT 下面——这两者都是
    头条特有的概念，公众号不应该跟它们扯上关系。
    """
    default_path = str(publishers.WECHAT_DRAFTS_DIR_DEFAULT)
    assert "toutiao" not in default_path.lower()


def test_wechat_drafts_dir_default_is_under_super_brain():
    from paths import SUPER_BRAIN
    assert publishers.WECHAT_DRAFTS_DIR_DEFAULT.resolve() == (SUPER_BRAIN / "wechat_drafts").resolve()


def test_get_wechat_drafts_dir_uses_default_when_not_configured(tmp_path, monkeypatch):
    monkeypatch.setattr(publishers, "CONFIG_PATH", tmp_path / "does-not-exist.json")
    assert publishers.get_wechat_drafts_dir() == publishers.WECHAT_DRAFTS_DIR_DEFAULT


def test_get_wechat_drafts_dir_respects_config_override(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"WECHAT_DRAFTS_DIR": "/custom/wechat/path"}), encoding="utf-8")
    monkeypatch.setattr(publishers, "CONFIG_PATH", config_path)

    from pathlib import Path
    assert publishers.get_wechat_drafts_dir() == Path("/custom/wechat/path")


def test_get_wechat_drafts_dir_does_not_share_config_key_with_toutiao(tmp_path, monkeypatch):
    """只配了 TOUTIAO_DRAFTS_DIR，不应该影响公众号目录的解析结果——两个字段完全独立。"""
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"TOUTIAO_DRAFTS_DIR": "/custom/toutiao/path"}), encoding="utf-8")
    monkeypatch.setattr(publishers, "CONFIG_PATH", config_path)

    assert publishers.get_wechat_drafts_dir() == publishers.WECHAT_DRAFTS_DIR_DEFAULT
    from pathlib import Path
    assert publishers.get_toutiao_drafts_dir() == Path("/custom/toutiao/path")
