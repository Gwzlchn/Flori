"""tests for api/routes/jobs.py"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from shared.config import AppConfig, load_config
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


class TestCreateJob:
    @pytest.mark.asyncio
    async def test_create_url_job(self, client, mock_redis):
        resp = await client.post("/api/jobs", json={
            "url": "https://www.bilibili.com/video/BV1xx411c7mD",
            "domain": "deep-learning",
        })
        assert resp.status_code == 201
        data = resp.json()
        assert "job_id" in data
        assert data["content_type"] == "video"
        assert data["status"] == "pending"
        mock_redis.publish.assert_called_once()
        args = mock_redis.publish.call_args
        assert args[0][0] == "job_command"  # channel name
        assert args[0][1]["action"] == "new_job"  # data content

    @pytest.mark.asyncio
    async def test_create_arxiv_job(self, client):
        resp = await client.post("/api/jobs", json={
            "url": "https://arxiv.org/abs/2301.00001",
            "domain": "ml",
        })
        assert resp.status_code == 201
        assert resp.json()["content_type"] == "paper"

    @pytest.mark.asyncio
    async def test_create_with_style_tags(self, client):
        resp = await client.post("/api/jobs", json={
            "url": "BV1xx411c7mD",
            "style_tags": ["lecture", "case-study"],
        })
        assert resp.status_code == 201


class TestListJobs:
    @pytest.mark.asyncio
    async def test_empty_list(self, client):
        resp = await client.get("/api/jobs")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["items"] == []

    @pytest.mark.asyncio
    async def test_list_after_create(self, client):
        await client.post("/api/jobs", json={"url": "BV1xx411c7mD"})
        resp = await client.get("/api/jobs")
        assert resp.json()["total"] == 1


class TestGetJob:
    @pytest.mark.asyncio
    async def test_get_existing(self, client):
        create_resp = await client.post("/api/jobs", json={"url": "BV1xx411c7mD"})
        job_id = create_resp.json()["job_id"]
        resp = await client.get(f"/api/jobs/{job_id}")
        assert resp.status_code == 200
        assert resp.json()["job_id"] == job_id

    @pytest.mark.asyncio
    async def test_get_nonexistent(self, client):
        resp = await client.get("/api/jobs/nonexistent")
        assert resp.status_code == 404


class TestDeleteJob:
    @pytest.mark.asyncio
    async def test_delete(self, client):
        create_resp = await client.post("/api/jobs", json={"url": "BV1xx411c7mD"})
        job_id = create_resp.json()["job_id"]
        resp = await client.delete(f"/api/jobs/{job_id}")
        assert resp.status_code == 204
        resp2 = await client.get(f"/api/jobs/{job_id}")
        assert resp2.status_code == 404


class TestPathTraversal:
    @pytest.mark.asyncio
    async def test_job_id_with_dots_rejected(self, client):
        resp = await client.get("/api/jobs/..%2F..%2Fetc%2Fpasswd")
        assert resp.status_code in (400, 404, 422)

    @pytest.mark.asyncio
    async def test_job_id_with_slash_rejected(self, client):
        resp = await client.delete("/api/jobs/j_test/../secrets")
        assert resp.status_code in (400, 404, 422)

    @pytest.mark.asyncio
    async def test_retry_nonexistent_job(self, client):
        resp = await client.post("/api/jobs/nonexistent_id/retry")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_rerun_nonexistent_job(self, client):
        resp = await client.post(
            "/api/jobs/nonexistent_id/rerun",
            json={"from_step": "A"},
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_resubmit_nonexistent_job(self, client):
        resp = await client.post("/api/jobs/nonexistent_id/resubmit")
        assert resp.status_code == 404


class TestGetStepLog:
    @pytest.mark.asyncio
    async def test_log_not_found(self, client):
        resp = await client.get("/api/jobs/j_nope/steps/A/log")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_log_truncated_by_default(self, client, test_config):
        job_id = "j_log_trunc"
        log_dir = test_config.jobs_dir / job_id / "logs"
        log_dir.mkdir(parents=True)
        big = ("x" * 1000 + "\n") * 400  # ~400KB > 256KB
        (log_dir / "A.log").write_text(big)

        resp = await client.get(f"/api/jobs/{job_id}/steps/A/log")
        assert resp.status_code == 200
        text = resp.text
        assert "truncated" in text
        assert len(text.encode("utf-8")) < len(big.encode("utf-8"))

    @pytest.mark.asyncio
    async def test_log_raw_not_truncated(self, client, test_config):
        job_id = "j_log_raw"
        log_dir = test_config.jobs_dir / job_id / "logs"
        log_dir.mkdir(parents=True)
        big = ("x" * 1000 + "\n") * 400  # ~400KB > 256KB
        (log_dir / "A.log").write_text(big)

        resp = await client.get(f"/api/jobs/{job_id}/steps/A/log?raw=1")
        assert resp.status_code == 200
        assert "truncated" not in resp.text
        assert resp.text == big

    @pytest.mark.asyncio
    async def test_log_step_path_traversal_rejected(self, client):
        resp = await client.get("/api/jobs/j1/steps/..%2F..%2Fsecret/log")
        assert resp.status_code in (400, 404, 422)


class TestRetryRerunResubmit:
    @pytest.mark.asyncio
    async def test_retry_non_failed(self, client):
        create_resp = await client.post("/api/jobs", json={"url": "BV1xx411c7mD"})
        job_id = create_resp.json()["job_id"]
        resp = await client.post(f"/api/jobs/{job_id}/retry")
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_rerun(self, client, mock_redis):
        create_resp = await client.post("/api/jobs", json={"url": "BV1xx411c7mD"})
        job_id = create_resp.json()["job_id"]
        resp = await client.post(
            f"/api/jobs/{job_id}/rerun",
            json={"from_step": "08_smart"},
        )
        assert resp.status_code == 200
        assert resp.json()["from_step"] == "08_smart"

    @pytest.mark.asyncio
    async def test_resubmit(self, client, mock_redis):
        create_resp = await client.post("/api/jobs", json={"url": "BV1xx411c7mD"})
        job_id = create_resp.json()["job_id"]
        resp = await client.post(f"/api/jobs/{job_id}/resubmit")
        assert resp.status_code == 200
