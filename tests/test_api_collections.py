"""tests for api/routes/collections.py"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
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
def mock_redis():
    r = AsyncMock()
    r.publish = AsyncMock()
    return r


@pytest.fixture
def app(db, mock_redis, test_config):
    return create_app(db=db, redis=mock_redis, config=test_config)


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _create(client, **kwargs):
    payload = {"name": "DL", "domain": "deep-learning"}
    payload.update(kwargs)
    resp = await client.post("/api/collections", json=payload)
    assert resp.status_code == 201
    return resp.json()


class TestCreateCollection:
    @pytest.mark.asyncio
    async def test_create_minimal(self, client):
        data = await _create(client)
        assert data["id"].startswith("c_")
        assert data["name"] == "DL"
        assert data["domain"] == "deep-learning"
        assert data["description"] == ""
        assert data["tags"] == []
        assert data["job_count"] == 0
        assert data["created_at"]

    @pytest.mark.asyncio
    async def test_create_with_tags_and_desc(self, client):
        data = await _create(
            client, description="notes", tags=["cv", "nlp"],
        )
        assert data["description"] == "notes"
        assert data["tags"] == ["cv", "nlp"]


class TestListCollections:
    @pytest.mark.asyncio
    async def test_empty(self, client):
        resp = await client.get("/api/collections")
        assert resp.status_code == 200
        assert resp.json() == []

    @pytest.mark.asyncio
    async def test_list_after_create(self, client):
        await _create(client, name="A", domain="d1")
        await _create(client, name="B", domain="d2")
        resp = await client.get("/api/collections")
        assert len(resp.json()) == 2

    @pytest.mark.asyncio
    async def test_filter_by_domain(self, client):
        await _create(client, name="A", domain="d1")
        await _create(client, name="B", domain="d2")
        resp = await client.get("/api/collections", params={"domain": "d1"})
        items = resp.json()
        assert len(items) == 1
        assert items[0]["name"] == "A"


class TestGetCollection:
    @pytest.mark.asyncio
    async def test_get_existing(self, client):
        cid = (await _create(client))["id"]
        resp = await client.get(f"/api/collections/{cid}")
        assert resp.status_code == 200
        assert resp.json()["id"] == cid

    @pytest.mark.asyncio
    async def test_get_nonexistent(self, client):
        resp = await client.get("/api/collections/nope")
        assert resp.status_code == 404


class TestUpdateCollection:
    @pytest.mark.asyncio
    async def test_update_name_only(self, client):
        cid = (await _create(client, description="keep", tags=["x"]))["id"]
        resp = await client.put(
            f"/api/collections/{cid}", json={"name": "renamed"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["name"] == "renamed"
        # None 字段不动：description/tags 保留。
        assert body["description"] == "keep"
        assert body["tags"] == ["x"]

    @pytest.mark.asyncio
    async def test_update_tags(self, client):
        cid = (await _create(client))["id"]
        resp = await client.put(
            f"/api/collections/{cid}", json={"tags": ["a", "b"]},
        )
        assert resp.json()["tags"] == ["a", "b"]

    @pytest.mark.asyncio
    async def test_update_nonexistent(self, client):
        resp = await client.put(
            "/api/collections/nope", json={"name": "x"},
        )
        assert resp.status_code == 404


class TestDeleteCollection:
    @pytest.mark.asyncio
    async def test_delete(self, client):
        cid = (await _create(client))["id"]
        resp = await client.delete(f"/api/collections/{cid}")
        assert resp.status_code == 204
        resp2 = await client.get(f"/api/collections/{cid}")
        assert resp2.status_code == 404

    @pytest.mark.asyncio
    async def test_delete_nonexistent(self, client):
        resp = await client.delete("/api/collections/nope")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_delete_unbinds_jobs_not_deletes(self, client, db):
        """删集合=解绑：job 保留但 collection_id 置 NULL，job_count 不再相关。"""
        cid = (await _create(client))["id"]
        job_resp = await client.post(
            "/api/jobs", json={"url": "BV1xx411c7mD", "collection_id": cid},
        )
        job_id = job_resp.json()["job_id"]

        del_resp = await client.delete(f"/api/collections/{cid}")
        assert del_resp.status_code == 204

        # job 仍存在（未被级联删除）。
        job_get = await client.get(f"/api/jobs/{job_id}")
        assert job_get.status_code == 200
        # collection_id 已解绑为 NULL。
        job = db.get_job(job_id)
        assert job.collection_id is None


class TestJobCount:
    @pytest.mark.asyncio
    async def test_count_increments_on_job_create(self, client):
        cid = (await _create(client))["id"]
        await client.post(
            "/api/jobs", json={"url": "BV1xx411c7mD", "collection_id": cid},
        )
        await client.post(
            "/api/jobs", json={"url": "BV2yy411c7mD", "collection_id": cid},
        )
        resp = await client.get(f"/api/collections/{cid}")
        assert resp.json()["job_count"] == 2

    @pytest.mark.asyncio
    async def test_count_decrements_on_job_delete(self, client):
        cid = (await _create(client))["id"]
        job_resp = await client.post(
            "/api/jobs", json={"url": "BV1xx411c7mD", "collection_id": cid},
        )
        job_id = job_resp.json()["job_id"]
        assert (await client.get(f"/api/collections/{cid}")).json()["job_count"] == 1

        await client.delete(f"/api/jobs/{job_id}")
        resp = await client.get(f"/api/collections/{cid}")
        assert resp.json()["job_count"] == 0


class TestListCollectionJobs:
    @pytest.mark.asyncio
    async def test_list_jobs_in_collection(self, client):
        cid = (await _create(client))["id"]
        await client.post(
            "/api/jobs", json={"url": "BV1xx411c7mD", "collection_id": cid},
        )
        # 不属于该集合的 job 不应出现。
        await client.post("/api/jobs", json={"url": "BV9zz411c7mD"})

        resp = await client.get(f"/api/collections/{cid}/jobs")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert len(body["items"]) == 1

    @pytest.mark.asyncio
    async def test_list_jobs_pagination(self, client):
        cid = (await _create(client))["id"]
        for _ in range(3):
            await client.post(
                "/api/jobs", json={"url": "BV1xx411c7mD", "collection_id": cid},
            )
        resp = await client.get(
            f"/api/collections/{cid}/jobs", params={"limit": 2, "offset": 0},
        )
        body = resp.json()
        assert body["total"] == 3
        assert len(body["items"]) == 2

    @pytest.mark.asyncio
    async def test_list_jobs_nonexistent_collection(self, client):
        resp = await client.get("/api/collections/nope/jobs")
        assert resp.status_code == 404
