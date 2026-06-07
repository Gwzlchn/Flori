"""tests for api/routes/glossary.py"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
import yaml
from httpx import ASGITransport, AsyncClient

from shared.config import load_config
from shared.db import Database
from api.main import create_app


@pytest.fixture
def test_config(tmp_path, configs_dir):
    cfg = load_config(config_dir=configs_dir, data_dir=tmp_path)
    cfg.jobs_dir = tmp_path / "jobs"
    cfg.jobs_dir.mkdir()
    cfg.prompts_dir = tmp_path / "prompts"
    cfg.prompts_dir.mkdir()
    return cfg


@pytest.fixture
def db(test_config):
    d = Database(test_config.db_path)
    d.init_schema()
    yield d
    d.close()


@pytest.fixture
def app(db, test_config):
    return create_app(db=db, redis=AsyncMock(), config=test_config)


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _read_profile_terms(prompts_dir, domain):
    path = prompts_dir / "profiles" / f"{domain}.yaml"
    if not path.exists():
        return None
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data.get("terminology", [])


class TestManualCRUD:
    @pytest.mark.asyncio
    async def test_create_term_accepted(self, client):
        resp = await client.post(
            "/api/glossary?domain=ml",
            json={"term": "梯度下降", "definition": "一种优化算法"},
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["term"] == "梯度下降"
        assert body["status"] == "accepted"
        assert body["source_type"] == "manual"
        assert body["definition"] == "一种优化算法"

    @pytest.mark.asyncio
    async def test_create_syncs_into_profile(self, client, test_config):
        await client.post(
            "/api/glossary?domain=ml",
            json={"term": "梯度下降", "definition": "优化算法"},
        )
        terms = _read_profile_terms(test_config.prompts_dir, "ml")
        assert "梯度下降: 优化算法" in terms

    @pytest.mark.asyncio
    async def test_create_empty_term_rejected(self, client):
        resp = await client.post("/api/glossary?domain=ml", json={"term": "  "})
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_list_terms(self, client):
        await client.post("/api/glossary?domain=ml", json={"term": "A"})
        await client.post("/api/glossary?domain=ml", json={"term": "B"})
        resp = await client.get("/api/glossary?domain=ml")
        assert resp.status_code == 200
        assert {t["term"] for t in resp.json()} == {"A", "B"}

    @pytest.mark.asyncio
    async def test_get_term_detail(self, client):
        await client.post(
            "/api/glossary?domain=ml", json={"term": "A", "definition": "d"}
        )
        resp = await client.get("/api/glossary/ml/A")
        assert resp.status_code == 200
        assert resp.json()["definition"] == "d"

    @pytest.mark.asyncio
    async def test_get_missing_term_404(self, client):
        resp = await client.get("/api/glossary/ml/nope")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_update_definition(self, client):
        await client.post(
            "/api/glossary?domain=ml", json={"term": "A", "definition": "旧"}
        )
        resp = await client.put(
            "/api/glossary/ml/A",
            json={"term": "A", "definition": "新", "related": ["B"]},
        )
        assert resp.status_code == 200
        assert resp.json()["definition"] == "新"
        assert resp.json()["related"] == ["B"]
        # status 不动，仍 accepted。
        assert resp.json()["status"] == "accepted"

    @pytest.mark.asyncio
    async def test_update_missing_term_404(self, client):
        resp = await client.put(
            "/api/glossary/ml/nope", json={"term": "nope", "definition": "x"}
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_delete_term(self, client):
        await client.post("/api/glossary?domain=ml", json={"term": "A"})
        resp = await client.delete("/api/glossary/ml/A")
        assert resp.status_code == 204
        assert (await client.get("/api/glossary/ml/A")).status_code == 404


class TestSuggestionFlow:
    @pytest.mark.asyncio
    async def test_suggestion_shows_in_suggested_list(self, client, db):
        db.add_glossary_suggestion("ml", "Transformer", "job-1", "review")
        db.add_glossary_suggestion("ml", "Transformer", "job-2", "review")
        resp = await client.get("/api/glossary?domain=ml&status=suggested")
        assert resp.status_code == 200
        items = resp.json()
        assert len(items) == 1
        assert items[0]["term"] == "Transformer"
        assert items[0]["status"] == "suggested"
        # sources 记录了来源 job，用于前端显示来源数。
        assert set(items[0]["sources"]) == {"job-1", "job-2"}

    @pytest.mark.asyncio
    async def test_accept_sets_status_and_writes_profile(
        self, client, db, test_config
    ):
        db.add_glossary_suggestion("ml", "注意力机制", "job-1", "review")
        resp = await client.post("/api/glossary/ml/注意力机制/accept")
        assert resp.status_code == 200
        assert resp.json()["status"] == "accepted"
        # 采纳后该词进入 Profile.terminology，AI 步骤可用。
        terms = _read_profile_terms(test_config.prompts_dir, "ml")
        assert "注意力机制" in terms

    @pytest.mark.asyncio
    async def test_accept_with_definition_writes_pair(
        self, client, db, test_config
    ):
        db.add_glossary_suggestion("ml", "注意力", "job-1", "review")
        db.upsert_glossary_term(
            "ml", "注意力", definition="加权聚合", status="suggested"
        )
        await client.post("/api/glossary/ml/注意力/accept")
        terms = _read_profile_terms(test_config.prompts_dir, "ml")
        assert "注意力: 加权聚合" in terms

    @pytest.mark.asyncio
    async def test_accept_missing_term_404(self, client):
        resp = await client.post("/api/glossary/ml/nope/accept")
        assert resp.status_code == 404


class TestFilters:
    @pytest.mark.asyncio
    async def test_filter_by_domain(self, client, db):
        db.upsert_glossary_term("ml", "A")
        db.upsert_glossary_term("dl", "C")
        resp = await client.get("/api/glossary?domain=ml")
        assert {t["term"] for t in resp.json()} == {"A"}

    @pytest.mark.asyncio
    async def test_filter_by_status(self, client, db):
        db.upsert_glossary_term("ml", "A")  # accepted
        db.add_glossary_suggestion("ml", "B", "j1")  # suggested
        accepted = await client.get("/api/glossary?status=accepted")
        suggested = await client.get("/api/glossary?status=suggested")
        assert {t["term"] for t in accepted.json()} == {"A"}
        assert {t["term"] for t in suggested.json()} == {"B"}

    @pytest.mark.asyncio
    async def test_list_sorted_by_term(self, client, db):
        db.upsert_glossary_term("ml", "z")
        db.upsert_glossary_term("ml", "a")
        resp = await client.get("/api/glossary?domain=ml")
        assert [t["term"] for t in resp.json()] == ["a", "z"]


class TestDomainValidation:
    @pytest.mark.asyncio
    async def test_create_traversal_domain_rejected(self, client):
        resp = await client.post(
            "/api/glossary?domain=../etc", json={"term": "A"}
        )
        assert resp.status_code in (400, 404, 422)
