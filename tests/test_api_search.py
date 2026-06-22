"""tests for api/routes/search.py（FTS5 全文检索端点）。"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock

from shared.db import Database
from api.main import create_app


@pytest.fixture
def db(test_config):
    d = Database(test_config.db_path)
    d.init_schema()
    # 灌入若干中文笔记，覆盖不同 domain / content_type / collection。
    d.index_job_notes(
        "j_ml", "smart", "深度学习入门",
        "反向传播算法是神经网络训练的核心机制，用于计算梯度。",
        content_type="video", domain="ml", collection_id="c_ai",
    )
    d.index_job_notes(
        "j_ml", "mechanical", "深度学习入门",
        "逐字稿：今天我们讲反向传播算法的推导过程。",
        content_type="video", domain="ml", collection_id="c_ai",
    )
    d.index_job_notes(
        "j_paper", "paper", "Transformer 论文精读",
        "自注意力机制让模型并行处理序列，反向传播效率更高。",
        content_type="paper", domain="nlp", collection_id="c_ai",
    )
    d.index_job_notes(
        "j_cook", "smart", "红烧肉做法",
        "先焯水再炒糖色，小火慢炖一小时即可。",
        content_type="article", domain="food", collection_id="",
    )
    yield d
    d.close()


@pytest.fixture
def app(db, test_config):
    return create_app(db=db, redis=AsyncMock(), config=test_config)


class TestSearch:
    @pytest.mark.asyncio
    async def test_null_byte_query_returns_400(self, client):
        # q 含空字节(%00)曾让 sqlite3 FTS 绑定抛 "unterminated string" → 裸 500;
        # 现入口中间件统一拦成 400(回归:schemathesis fuzz seed=42 发现)。
        resp = await client.get("/api/search?q=%00")
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_chinese_substring_hit(self, client):
        # trigram 中文子串命中：3 个 job 含"反向传播"。
        resp = await client.get("/api/search", params={"q": "反向传播"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 3
        job_ids = {it["job_id"] for it in data["items"]}
        assert job_ids == {"j_ml", "j_paper"}

    @pytest.mark.asyncio
    async def test_snippet_highlight(self, client):
        # snippet 取自 body 列，故用 body 中出现的词（≥3 字）验证 <mark> 高亮。
        resp = await client.get("/api/search", params={"q": "炒糖色"})
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 1
        assert "<mark>" in items[0]["snippet"]
        assert items[0]["title"] == "红烧肉做法"
        assert items[0]["note_type"] == "smart"

    @pytest.mark.asyncio
    async def test_facet_domain(self, client):
        resp = await client.get("/api/search", params={"q": "反向传播", "domain": "nlp"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["job_id"] == "j_paper"
        assert data["items"][0]["domain"] == "nlp"

    @pytest.mark.asyncio
    async def test_facet_content_type(self, client):
        resp = await client.get("/api/search", params={"q": "反向传播", "content_type": "video"})
        data = resp.json()
        assert data["total"] == 2
        assert all(it["content_type"] == "video" for it in data["items"])

    @pytest.mark.asyncio
    async def test_facet_collection(self, client):
        resp = await client.get("/api/search", params={"q": "反向传播", "collection_id": "c_ai"})
        data = resp.json()
        assert data["total"] == 3
        assert all(it["collection_id"] == "c_ai" for it in data["items"])

    @pytest.mark.asyncio
    async def test_collection_empty_normalized_to_null(self, client):
        # 空串 collection_id 在 db 层归一为 None。
        resp = await client.get("/api/search", params={"q": "红烧肉"})
        assert resp.json()["items"][0]["collection_id"] is None

    @pytest.mark.asyncio
    async def test_pagination(self, client):
        page1 = (await client.get("/api/search", params={"q": "反向传播", "limit": 2, "offset": 0})).json()
        page2 = (await client.get("/api/search", params={"q": "反向传播", "limit": 2, "offset": 2})).json()
        assert page1["total"] == 3
        assert len(page1["items"]) == 2
        assert len(page2["items"]) == 1
        ids1 = {it["job_id"] + it["note_type"] for it in page1["items"]}
        ids2 = {it["job_id"] + it["note_type"] for it in page2["items"]}
        assert ids1.isdisjoint(ids2)

    @pytest.mark.asyncio
    async def test_empty_query(self, client):
        resp = await client.get("/api/search", params={"q": ""})
        assert resp.status_code == 200
        assert resp.json() == {"total": 0, "items": []}

    @pytest.mark.asyncio
    async def test_missing_query(self, client):
        # q 缺省默认空串，返回空结果不报错。
        resp = await client.get("/api/search")
        assert resp.status_code == 200
        assert resp.json()["total"] == 0

    @pytest.mark.asyncio
    async def test_no_match(self, client):
        resp = await client.get("/api/search", params={"q": "量子计算机"})
        assert resp.status_code == 200
        assert resp.json()["total"] == 0

    @pytest.mark.asyncio
    async def test_match_query_injection_safe(self, client):
        # 含 fts5 特殊语法的查询不应抛 500，只是按短语匹配（无结果）。
        resp = await client.get("/api/search", params={"q": '" OR job_id:"'})
        assert resp.status_code == 200
        assert resp.json()["total"] == 0
