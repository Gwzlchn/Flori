"""领域 API（派生视图）：总览聚合 / 工作台 / 术语 / 主题 / 归类(PATCH job)。"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from shared.config import load_config
from shared.db import Database
from shared.models import Collection, Job
from api.main import create_app


@pytest.fixture
def test_config(tmp_path, configs_dir):
    cfg = load_config(config_dir=configs_dir, data_dir=tmp_path)
    cfg.jobs_dir = tmp_path / "jobs"; cfg.jobs_dir.mkdir()
    cfg.prompts_dir = tmp_path / "prompts"; cfg.prompts_dir.mkdir()
    return cfg


@pytest.fixture
def db(test_config):
    d = Database(test_config.db_path); d.init_schema()
    yield d; d.close()


@pytest.fixture
def app(db, test_config):
    return create_app(db=db, redis=AsyncMock(), config=test_config)


@pytest.fixture
async def client(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def _seed(db):
    db.create_collection(Collection(id="col_bili_up_1", name="UP", domain="finance",
                                    source_type="bilibili_up", source_id="1"))
    db.create_job(Job(id="j1", content_type="video", pipeline="video", domain="finance",
                      collection_id="col_bili_up_1", style_tags=["宏观经济", "利率"]))
    db.create_job(Job(id="j2", content_type="video", pipeline="video", domain="finance",
                      style_tags=["宏观经济"]))   # 未分类
    db.create_job(Job(id="j3", content_type="paper", pipeline="paper", domain="deep-learning"))
    db.add_glossary_suggestion("finance", "国债期货", "j1", "review")


class TestDomains:
    @pytest.mark.asyncio
    async def test_overview(self, client, app):
        _seed(app.state.db)
        data = (await client.get("/api/domains")).json()["domains"]
        by = {d["domain"]: d for d in data}
        assert by["finance"]["job_count"] == 2
        assert by["finance"]["collection_count"] == 1
        assert by["finance"]["subscription_count"] == 1
        assert by["finance"]["concept_count"] == 1
        assert by["deep-learning"]["job_count"] == 1

    @pytest.mark.asyncio
    async def test_workspace(self, client, app):
        _seed(app.state.db)
        ws = (await client.get("/api/domains/finance")).json()
        assert ws["stats"]["job_count"] == 2
        assert len(ws["collections"]) == 1 and ws["collections"][0]["is_subscription"]
        assert len(ws["recent_jobs"]) == 2
        topics = {t["topic"]: t["count"] for t in ws["topics"]}
        assert topics["宏观经济"] == 2 and topics["利率"] == 1
        assert ws["top_concepts"][0]["term"] == "国债期货"

    @pytest.mark.asyncio
    async def test_workspace_404(self, client):
        assert (await client.get("/api/domains/nope")).status_code == 404

    @pytest.mark.asyncio
    async def test_term_detail(self, client, app):
        _seed(app.state.db)
        assert (await client.get("/api/domains/finance/terms/国债期货")).status_code == 200
        assert (await client.get("/api/domains/finance/terms/不存在")).status_code == 404

    @pytest.mark.asyncio
    async def test_topic_page(self, client, app):
        _seed(app.state.db)
        r = (await client.get("/api/domains/finance/topics/宏观经济")).json()
        assert r["total"] == 2
        r2 = (await client.get("/api/domains/finance/topics/利率")).json()
        assert r2["total"] == 1

    @pytest.mark.asyncio
    async def test_topic_concepts(self, client, app):
        db = app.state.db
        # 两个出现的主题概念。
        db.add_glossary_suggestion("finance", "通胀", "j1", "video")
        db.add_glossary_suggestion("finance", "通胀", "j2", "video")
        db.set_glossary_topic("finance", "通胀", True)
        # 一个出现的主题概念。
        db.add_glossary_suggestion("finance", "汇率", "j1", "video")
        db.set_glossary_topic("finance", "汇率", True)
        # 非主题概念（不应返回）。
        db.add_glossary_suggestion("finance", "成交量", "j1", "video")

        data = (await client.get("/api/domains/finance/topic-concepts")).json()
        terms = [c["term"] for c in data]
        assert terms == ["通胀", "汇率"]  # 按 occurrence_count 降序
        assert all(c["is_topic"] is True for c in data)
        by = {c["term"]: c for c in data}
        assert by["通胀"]["occurrence_count"] == 2
        assert by["汇率"]["occurrence_count"] == 1
        assert "成交量" not in terms
        assert isinstance(by["通胀"]["related"], list)

    @pytest.mark.asyncio
    async def test_topic_concepts_empty(self, client, app):
        _seed(app.state.db)  # 无 is_topic=1 的词。
        data = (await client.get("/api/domains/finance/topic-concepts")).json()
        assert data == []

    @pytest.mark.asyncio
    async def test_jobs_domain_filter_internal(self, client, app):
        # /api/jobs 不再暴露 domain/uncategorized 查询(无前端消费方，已移除)；
        # domain 过滤仅供领域工作台内部用，经 /api/domains/:d 验证(test_workspace)。
        _seed(app.state.db)
        assert (await client.get("/api/jobs")).json()["total"] == 3
