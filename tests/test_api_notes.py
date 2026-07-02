"""api/routes/notes.py 测试。"""

from __future__ import annotations

import json

import pytest


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
        assert "# Mechanical" in resp.text   # 取到的是机械笔记内容,而非空/错文件

    @pytest.mark.asyncio
    async def test_transcript(self, client, test_config):
        _create_job_files(test_config.jobs_dir, "j_test")
        resp = await client.get("/api/jobs/j_test/notes/transcript")
        assert resp.status_code == 200
        assert "[00:00] Hello" in resp.text   # 取到逐字稿正文

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
        assert resp.content[:4] == b"\xff\xd8\xff\xe0"   # 取到的是真 JPEG 字节(非空/错文件)
        assert resp.headers["content-type"] in ("image/jpeg", "application/octet-stream")

    @pytest.mark.asyncio
    async def test_asset_path_traversal(self, client, test_config):
        # %2e%2e 解码为 ".." 仍在单段内,能真正到达守卫 → 严格断言 400(不接受 404 蒙混)
        _create_job_files(test_config.jobs_dir, "j_test")
        resp = await client.get("/api/jobs/j_test/assets/%2e%2e_passwd")
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_asset_null_byte(self, client, test_config):
        # 文件名含空字节(%00→\x00)会让 pathlib.resolve() 抛 ValueError;
        # _safe_path 拦下、_serve 映射为 400,不得裸 500(schemathesis fuzz 发现的回归)。
        _create_job_files(test_config.jobs_dir, "j_test")
        resp = await client.get("/api/jobs/j_test/assets/x%00")
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_note_versions_lists_with_overall(self, client, test_config):
        job_dir = _create_job_files(test_config.jobs_dir, "j_test")
        # 与该版笔记 1:1 配对的版本化评审,使 note-versions 能读到 overall
        paired = "output/versions/review_claude-cli_claude-opus-4-8_20260101-000000.json"
        (job_dir / paired).write_text('{"overall": 4.5}')
        resp = await client.get("/api/jobs/j_test/note-versions")
        assert resp.status_code == 200
        versions = resp.json()["versions"]
        assert len(versions) == 1
        v = versions[0]
        assert v["provider"] == "claude-cli" and v["version"] == "20260101-000000"
        assert v["review_file"] == paired and v["overall"] == 4.5

    @pytest.mark.asyncio
    async def test_smart_version_select_valid(self, client, test_config):
        _create_job_files(test_config.jobs_dir, "j_test")
        f = "output/versions/notes_smart_claude-cli_claude-opus-4-8_20260101-000000.md"
        resp = await client.get(f"/api/jobs/j_test/notes/smart?file={f}")
        assert resp.status_code == 200 and "Smart Notes" in resp.text

    @pytest.mark.asyncio
    async def test_smart_version_select_rejects_bad_file(self, client, test_config):
        _create_job_files(test_config.jobs_dir, "j_test")
        # 穿越
        r1 = await client.get("/api/jobs/j_test/notes/smart?file=output/versions/../../x.md")
        assert r1.status_code == 400
        # 不在 notes_smart 版本前缀
        r2 = await client.get("/api/jobs/j_test/notes/smart?file=output/notes_mechanical.md")
        assert r2.status_code == 400

    @pytest.mark.asyncio
    async def test_review_version_select_rejects_bad_file(self, client, test_config):
        _create_job_files(test_config.jobs_dir, "j_test")
        # review.json 不在 versions/review_ 版本前缀 → 400
        r = await client.get("/api/jobs/j_test/review?file=output/review.json")
        assert r.status_code == 400

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
