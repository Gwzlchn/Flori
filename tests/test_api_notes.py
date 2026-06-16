"""tests for api/routes/notes.py"""

from __future__ import annotations

import json

import pytest
from httpx import ASGITransport, AsyncClient

from shared.config import load_config
from shared.db import Database
from api.main import create_app
from unittest.mock import AsyncMock


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


def _create_job_files(jobs_dir, job_id):
    job_dir = jobs_dir / job_id
    job_dir.mkdir(parents=True)
    (job_dir / "output").mkdir()
    (job_dir / "assets").mkdir()
    (job_dir / "input").mkdir()
    # 智能笔记已版本化:/notes/smart 默认取最新版本(output/versions/notes_smart_*.md)。
    (job_dir / "output" / "versions").mkdir()
    smart_ver = "output/versions/notes_smart_claude-cli_claude-opus-4-8_20260101-000000.md"
    (job_dir / smart_ver).write_text("# Smart Notes\n")
    (job_dir / "output" / "notes_mechanical.md").write_text("# Mechanical\n")
    (job_dir / "output" / "transcript.md").write_text("[00:00] Hello\n")
    (job_dir / "output" / "review.json").write_text(f'{{"overall": 4.0, "note_file": "{smart_ver}"}}')
    (job_dir / "assets" / "scene_0001.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 100)
    return job_dir


class TestNotes:
    @pytest.mark.asyncio
    async def test_smart_notes(self, client, test_config):
        _create_job_files(test_config.jobs_dir, "j_test")
        resp = await client.get("/api/jobs/j_test/notes/smart")
        assert resp.status_code == 200
        assert "Smart Notes" in resp.text

    @pytest.mark.asyncio
    async def test_mechanical_notes(self, client, test_config):
        _create_job_files(test_config.jobs_dir, "j_test")
        resp = await client.get("/api/jobs/j_test/notes/mechanical")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_transcript(self, client, test_config):
        _create_job_files(test_config.jobs_dir, "j_test")
        resp = await client.get("/api/jobs/j_test/notes/transcript")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_review(self, client, test_config):
        _create_job_files(test_config.jobs_dir, "j_test")
        resp = await client.get("/api/jobs/j_test/review")
        assert resp.status_code == 200
        assert resp.json()["overall"] == 4.0

    @pytest.mark.asyncio
    async def test_asset(self, client, test_config):
        _create_job_files(test_config.jobs_dir, "j_test")
        resp = await client.get("/api/jobs/j_test/assets/scene_0001.jpg")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_asset_path_traversal(self, client, test_config):
        # %2e%2e 解码为 ".." 仍在单段内,能真正到达守卫 → 严格断言 400(不接受 404 蒙混)
        _create_job_files(test_config.jobs_dir, "j_test")
        resp = await client.get("/api/jobs/j_test/assets/%2e%2e_passwd")
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_not_found(self, client):
        resp = await client.get("/api/jobs/nonexistent/notes/smart")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_notes_not_ready(self, client, test_config):
        job_dir = test_config.jobs_dir / "j_empty"
        job_dir.mkdir()
        (job_dir / "output").mkdir()
        resp = await client.get("/api/jobs/j_empty/notes/smart")
        assert resp.status_code == 404
