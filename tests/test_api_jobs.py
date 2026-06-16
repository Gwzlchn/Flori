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
from api.routes.jobs import _detect_content_type, _pipeline_for


class TestDetectContentType:
    def test_pdf_file_is_paper(self):
        assert _detect_content_type(None, "x.pdf") == "paper"

    def test_video_file_is_video(self):
        assert _detect_content_type(None, "x.mkv") == "video"

    def test_audio_file_is_audio(self):
        for name in ("x.mp3", "x.m4a", "x.wav", "x.aac"):
            assert _detect_content_type(None, name) == "audio"

    def test_html_txt_file_is_article(self):
        assert _detect_content_type(None, "x.html") == "article"
        assert _detect_content_type(None, "x.txt") == "article"

    def test_filename_case_insensitive(self):
        assert _detect_content_type(None, "X.MP3") == "audio"

    def test_arxiv_url_is_paper(self):
        assert _detect_content_type("https://arxiv.org/abs/2301.00001") == "paper"

    def test_http_article_url_is_article(self):
        assert _detect_content_type("https://example.com/post") == "article"

    def test_podcast_url_is_audio(self):
        assert _detect_content_type("https://cdn.example.com/ep/1.mp3") == "audio"

    def test_video_url_defaults_video(self):
        assert _detect_content_type("https://www.bilibili.com/video/BV1xx411c7mD") == "video"


class TestPipelineFor:
    def test_known_mappings(self):
        assert _pipeline_for("video") == "video"
        assert _pipeline_for("paper") == "paper"
        assert _pipeline_for("article") == "article"
        assert _pipeline_for("audio") == "audio"

    def test_unknown_defaults_video(self):
        assert _pipeline_for("mystery") == "video"


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

    @pytest.mark.asyncio
    async def test_create_unknown_collection_rejected(self, client):
        # collection_id 不存在 → 400(防孤儿绑定 + job_count 漂移)
        resp = await client.post("/api/jobs", json={
            "url": "BV1xx411c7mD",
            "collection_id": "c_does_not_exist",
        })
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_create_article_job(self, client):
        resp = await client.post("/api/jobs", json={
            "url": "https://example.com/post/intro",
        })
        assert resp.status_code == 201
        assert resp.json()["content_type"] == "article"

    @pytest.mark.asyncio
    async def test_create_podcast_job(self, client):
        resp = await client.post("/api/jobs", json={
            "url": "https://cdn.example.com/ep/1.mp3",
        })
        assert resp.status_code == 201
        assert resp.json()["content_type"] == "audio"

    @pytest.mark.asyncio
    async def test_create_publishes_article_pipeline(self, client, mock_redis):
        await client.post("/api/jobs", json={"url": "https://example.com/p"})
        args = mock_redis.publish.call_args
        assert args[0][1]["pipeline"] == "article"


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
        body = resp.json()
        assert body["job_id"] == job_id
        # 详情契约:新增 url / updated_at,以及每步的 label(中文名)/起止时间。
        assert "url" in body and "updated_at" in body
        for s in body["steps"]:
            assert "label" in s and "started_at" in s and "finished_at" in s

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
            json={"from_step": "10_smart"},
        )
        assert resp.status_code == 200
        assert resp.json()["from_step"] == "10_smart"

    @pytest.mark.asyncio
    async def test_resubmit(self, client, mock_redis):
        create_resp = await client.post("/api/jobs", json={"url": "BV1xx411c7mD"})
        job_id = create_resp.json()["job_id"]
        resp = await client.post(f"/api/jobs/{job_id}/resubmit")
        assert resp.status_code == 200


class TestListByCollection:
    @pytest.mark.asyncio
    async def test_list_filters_by_collection(self, client, app):
        from shared.models import Collection, Job
        db = app.state.db
        db.create_collection(Collection(id="c_x", name="X", domain="general"))
        db.create_job(Job(id="j_in", content_type="video", pipeline="video", collection_id="c_x"))
        db.create_job(Job(id="j_out", content_type="video", pipeline="video"))
        resp = await client.get("/api/jobs?collection_id=c_x")
        assert resp.status_code == 200
        items = resp.json()["items"]
        ids = {i["job_id"] for i in items}
        assert "j_in" in ids and "j_out" not in ids
        assert items[0]["collection_id"] == "c_x"  # 响应含 collection_id


class TestProviderVersions:
    @pytest.mark.asyncio
    async def test_list_providers_marks_availability(self, client):
        # claude-cli(cli 类型)应可用;anthropic 等无 key 应不可用
        resp = await client.get("/api/providers")
        assert resp.status_code == 200
        provs = {p["name"]: p for p in resp.json()["providers"]}
        assert provs["claude-cli"]["available"] is True
        assert provs["anthropic"]["available"] is False  # 测试环境无 key

    @pytest.mark.asyncio
    async def test_rerun_smart_unavailable_provider_rejected(self, client):
        await client.post("/api/jobs", json={"url": "BV1xx411c7mD"})
        jid = (await client.get("/api/jobs")).json()["items"][0]["job_id"]
        resp = await client.post(f"/api/jobs/{jid}/rerun-smart", json={"provider": "anthropic"})
        assert resp.status_code == 400  # 无 key 不可用

    @pytest.mark.asyncio
    async def test_rerun_smart_claude_writes_override(self, client, app, mock_redis):
        await client.post("/api/jobs", json={"url": "BV1xx411c7mD"})
        jid = (await client.get("/api/jobs")).json()["items"][0]["job_id"]
        # 预置 job.json(storage 本地)
        storage = app.state.storage
        await storage.write_file(jid, "job.json", b'{"id":"x"}')
        resp = await client.post(f"/api/jobs/{jid}/rerun-smart", json={"provider": "claude-cli"})
        assert resp.status_code == 200 and resp.json()["provider"] == "claude-cli"
        import json as _j
        doc = _j.loads((await storage.read_file(jid, "job.json")).decode())
        assert doc["ai_overrides"]["10_smart"] == "claude-cli"
        assert doc["ai_overrides"]["11_review"] == "claude-cli"
