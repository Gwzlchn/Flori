"""Flori MCP server 测试:services 读层 + MCP 工具注册/委托。

不 spawn stdio 子进程(进程隔离会脱离内存 fixture DB);MCP 层用 in-process
list_tools/call_tool 验证。trigram 检索需 ≥3 字符,故查询词都取 3+ 字。
"""

from __future__ import annotations

import pytest

from api.mcp_server.server import build_server
from api.services import kb
from shared.models import Collection, Job
from shared.storage import LocalStorage


def _seed(db):
    db.create_collection(
        Collection(id="c1", name="UP", domain="finance",
                   source_type="bilibili_up", source_id="1")
    )
    db.create_job(Job(id="j1", content_type="video", pipeline="video",
                      domain="finance", collection_id="c1", title="庄家坐庄解析"))
    db.create_job(Job(id="j2", content_type="paper", pipeline="paper",
                      domain="deep-learning", title="注意力机制"))
    db.index_job_notes("j1", "smart", "庄家坐庄解析",
                       "讲庄家如何坐庄收割散户的手法", domain="finance", collection_id="c1")
    db.index_job_notes("j2", "smart", "注意力机制",
                       "transformer 注意力机制详解", domain="deep-learning")
    db.add_glossary_suggestion("finance", "坐庄", "j1", "review")


class TestKbServices:
    def test_list_knowledge_bases(self, db):
        _seed(db)
        kbs = kb.list_knowledge_bases(db)
        names = {k["domain"] for k in kbs}
        assert {"finance", "deep-learning"} <= names
        fin = next(k for k in kbs if k["domain"] == "finance")
        assert fin["job_count"] == 1 and fin["collection_count"] == 1

    def test_search_hits_and_normalizes(self, db):
        _seed(db)
        res = kb.search(db, "坐庄收割", limit=10)
        assert res and res[0]["job_id"] == "j1"
        assert set(res[0]) == {"title", "snippet", "job_id", "domain", "kind"}
        assert res[0]["domain"] == "finance" and res[0]["kind"] == "smart"

    def test_search_domain_scope(self, db):
        _seed(db)
        assert kb.search(db, "注意力", domain="finance") == []
        assert kb.search(db, "注意力", domain="deep-learning")

    @pytest.mark.asyncio
    async def test_get_note_markdown(self, db, test_config):
        _seed(db)
        storage = LocalStorage(test_config.jobs_dir)
        await storage.write_file(
            "j1", "output/versions/notes_smart_anthropic_opus_20260101-000000.md",
            "# 庄家解析\n正文".encode("utf-8"),
        )
        note = await kb.get_note(db, storage, "j1")
        assert note["job_id"] == "j1" and note["domain"] == "finance"
        assert note["markdown"].startswith("# 庄家解析")
        assert note["note_file"].endswith(".md")

    @pytest.mark.asyncio
    async def test_get_note_missing_md_is_none(self, db, test_config):
        _seed(db)
        storage = LocalStorage(test_config.jobs_dir)
        note = await kb.get_note(db, storage, "j1")
        assert note["markdown"] is None  # 智能笔记未生成

    @pytest.mark.asyncio
    async def test_get_note_unknown_job_raises(self, db, test_config):
        storage = LocalStorage(test_config.jobs_dir)
        with pytest.raises(KeyError):
            await kb.get_note(db, storage, "nope")


class TestKbServicesV2:
    def test_list_collections(self, db):
        _seed(db)
        cols = kb.list_collections(db, "finance")
        assert cols and cols[0]["id"] == "c1"
        assert cols[0]["source_type"] == "bilibili_up"  # 订阅集合带 source
        assert kb.list_collections(db, "deep-learning") == []  # 该库无集合

    def test_get_glossary_and_term(self, db):
        _seed(db)
        g = kb.get_glossary(db, "finance")
        row = next(t for t in g if t["term"] == "坐庄")
        assert set(row) == {"term", "definition", "status", "is_topic", "occurrence_count"}
        assert row["occurrence_count"] >= 1
        term = kb.get_term(db, "finance", "坐庄")
        assert term and term["term"] == "坐庄" and "occurrences" in term
        assert kb.get_term(db, "finance", "不存在的词") is None

    def test_concept_timeline(self, db):
        _seed(db)
        tl = kb.concept_timeline(db, "finance", "month")
        assert tl["granularity"] == "month"


class TestMcpServer:
    @pytest.mark.asyncio
    async def test_tools_registered(self, db, test_config):
        mcp = build_server(db, LocalStorage(test_config.jobs_dir))
        names = {t.name for t in await mcp.list_tools()}
        assert names == {
            "list_knowledge_bases", "search", "get_note",
            "list_collections", "get_glossary", "get_term", "concept_timeline",
        }

    @pytest.mark.asyncio
    async def test_tool_delegates_to_service(self, db, test_config):
        _seed(db)
        mcp = build_server(db, LocalStorage(test_config.jobs_dir))
        result = await mcp.call_tool("search", {"query": "坐庄收割"})
        # FastMCP.call_tool 返回 (content_blocks, structured);命中的 j1 应出现在结果里
        assert result is not None and "j1" in str(result)


def test_stdio_logging_to_stderr_keeps_stdout_clean(capsys):
    """MCP stdio:stdout 必须是纯 JSON-RPC。_configure_stdio_logging 后 structlog 日志须走 stderr,
    否则 tool 调用时的 log 行会污染协议流(回归保护:之前默认 PrintLogger 写 stdout)。"""
    import structlog

    from api.mcp_server.__main__ import _configure_stdio_logging

    try:
        _configure_stdio_logging()
        structlog.get_logger().info("probe_event_xyz", k=1)
    finally:
        structlog.reset_defaults()
    out, err = capsys.readouterr()
    assert "probe_event_xyz" not in out  # stdout 不被日志污染
    assert "probe_event_xyz" in err      # 日志落在 stderr
