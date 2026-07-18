"""api/routes/jobs.py 测试。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from shared.config import AppConfig, load_config
from shared.models import Collection, Job, JobPart, JobStatus, Step, StepStatus
from shared.step_scope import part_scope
from api.main import create_app
from api.routes.jobs import _detect_content_type, _pipeline_for


def _video_request(url: str = "BV1xx411c7mD", **values) -> dict:
    return {"content_type": "video", "parts": [{"url": url}], **values}


class TestDetectContentType:
    def test_pdf_file_is_document(self):
        assert _detect_content_type(None, "x.pdf") == "document"

    def test_video_file_is_video(self):
        assert _detect_content_type(None, "x.mkv") == "video"

    def test_audio_file_is_audio(self):
        for name in ("x.mp3", "x.m4a", "x.wav", "x.aac"):
            assert _detect_content_type(None, name) == "audio"

    def test_html_txt_file_is_document(self):
        assert _detect_content_type(None, "x.html") == "document"
        assert _detect_content_type(None, "x.txt") == "document"

    def test_filename_case_insensitive(self):
        assert _detect_content_type(None, "X.MP3") == "audio"

    def test_unknown_file_has_no_default_pipeline(self):
        assert _detect_content_type(None, "payload.zip") is None

    def test_arxiv_url_is_document(self):
        assert _detect_content_type("https://arxiv.org/abs/2301.00001") == "document"

    def test_http_article_url_is_document(self):
        assert _detect_content_type("https://example.com/post") == "document"

    def test_podcast_url_is_audio(self):
        assert _detect_content_type("https://cdn.example.com/ep/1.mp3") == "audio"

    def test_video_url_defaults_video(self):
        assert _detect_content_type("https://www.bilibili.com/video/BV1xx411c7mD") == "video"


class TestPipelineFor:
    def test_known_mappings(self):
        assert _pipeline_for("video") == "video"
        assert _pipeline_for("document") == "document"
        assert _pipeline_for("audio") == "audio"

    def test_unknown_has_no_pipeline(self):
        assert _pipeline_for("mystery") is None


@pytest.fixture
def mock_redis():
    from tests.conftest import make_redis_mock

    r = make_redis_mock()
    r.publish = AsyncMock()
    r.get_all_step_statuses = AsyncMock(return_value={})
    return r


@pytest.fixture
def app(db, mock_redis, test_config):
    return create_app(db=db, redis=mock_redis, config=test_config)


class TestCreateJob:
    async def _complete_mechanical_job(self, app, job_id: str) -> None:
        job = app.state.db.get_job(job_id)
        for cfg in app.state.config.pipelines[job.pipeline]["steps"]:
            is_ai = cfg["pool"] == "ai"
            status = StepStatus.SKIPPED if is_ai else StepStatus.DONE
            scopes = (
                [part_scope(part.id) for part in app.state.db.get_parts(job_id)]
                if cfg.get("scope") == "part" else ["job"]
            )
            for scope_key in scopes:
                app.state.db.upsert_step(Step(
                    job_id=job_id, scope_key=scope_key,
                    name=cfg["name"], status=status,
                    pool=cfg["pool"], input_hash=f"parent:{cfg['name']}",
                ))
                if not is_ai:
                    part_id = scope_key.removeprefix("part:")
                    prefix = f"parts/{part_id}/" if scope_key != "job" else ""
                    await app.state.storage.write_file(
                        job_id, f"{prefix}.{cfg['name']}.done",
                        b'{"def_digest":"sha256:old"}',
                    )
        app.state.db.update_job(job_id, status=JobStatus.DONE)

    @pytest.mark.asyncio
    async def test_create_mechanical_only_persists_visible_mode(self, client, app):
        resp = await client.post(
            "/api/jobs",
            json=_video_request(
                "https://www.bilibili.com/video/BV1xx411c7mD",
                mechanical_only=True,
            ),
        )

        assert resp.status_code == 201
        job_id = resp.json()["job_id"]
        job = app.state.db.get_job(job_id)
        assert job.meta["flags"]["mechanical_only"] is True
        detail = (await client.get(f"/api/jobs/{job_id}")).json()
        assert detail["processing_mode"] == "mechanical_only"
        assert detail["completion_scope"] == "mechanical"
        raw = await app.state.storage.read_file(job_id, "job.json")
        assert json.loads(raw)["flags"]["mechanical_only"] is True

    @pytest.mark.asyncio
    async def test_continue_ai_forks_idempotent_full_snapshot(
        self, client, app, mock_redis
    ):
        created = await client.post(
            "/api/jobs",
            json=_video_request(
                "https://www.bilibili.com/video/BV1xx411c7mD",
                mechanical_only=True,
            ),
        )
        job_id = created.json()["job_id"]
        await self._complete_mechanical_job(app, job_id)
        mock_redis.append_lifecycle_event.reset_mock()

        resp = await client.post(f"/api/jobs/{job_id}/continue-ai")

        assert resp.status_code == 200
        new_id = resp.json()["job_id"]
        assert new_id != job_id
        assert resp.json()["status"] == "pending"
        assert app.state.db.get_job(job_id).meta["flags"]["mechanical_only"] is True
        assert app.state.db.get_job(job_id).is_current is False
        assert app.state.db.get_job(new_id).meta["flags"]["mechanical_only"] is False
        raw = await app.state.storage.read_file(new_id, "job.json")
        assert json.loads(raw)["flags"]["mechanical_only"] is False
        command = mock_redis.append_lifecycle_event.await_args.args[1]
        assert command["action"] == "new_job"
        assert command["job_id"] == new_id

        repeated = await client.post(f"/api/jobs/{job_id}/continue-ai")
        assert repeated.status_code == 200
        assert repeated.json()["job_id"] == new_id
        mock_redis.append_lifecycle_event.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_continue_ai_rejects_inflight_mechanical_snapshot(self, client):
        created = await client.post(
            "/api/jobs",
            json=_video_request(
                "https://www.bilibili.com/video/BV1xx411c7mD",
                mechanical_only=True,
            ),
        )
        resp = await client.post(f"/api/jobs/{created.json()['job_id']}/continue-ai")
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_continue_ai_repairs_post_promotion_event_failure(
        self, client, app, mock_redis,
    ):
        created = await client.post(
            "/api/jobs",
            json=_video_request(
                "https://www.bilibili.com/video/BV1xx411c7mD",
                mechanical_only=True,
            ),
        )
        parent_id = created.json()["job_id"]
        await self._complete_mechanical_job(app, parent_id)
        mock_redis.append_lifecycle_event.side_effect = RuntimeError("event failed")

        with pytest.raises(RuntimeError, match="event failed"):
            await client.post(f"/api/jobs/{parent_id}/continue-ai")
        child = next(
            item for item in app.state.db.lineage_versions(parent_id)
            if item.id != parent_id
        )
        assert child.meta["rebuild_request"]["event_published"] is False

        mock_redis.append_lifecycle_event.side_effect = None
        mock_redis.list_worker_ids.return_value = []
        repaired = await client.post(f"/api/jobs/{parent_id}/continue-ai")
        assert repaired.status_code == 200
        assert repaired.json()["job_id"] == child.id
        assert app.state.db.get_job(child.id).meta["rebuild_request"]["event_published"] is True

    @pytest.mark.asyncio
    async def test_create_url_job(self, client, mock_redis):
        resp = await client.post(
            "/api/jobs",
            json=_video_request(
                "https://www.bilibili.com/video/BV1xx411c7mD",
                domain="deep-learning",
            ),
        )
        assert resp.status_code == 201
        data = resp.json()
        assert "job_id" in data
        assert data["content_type"] == "video"
        assert data["status"] == "pending"
        mock_redis.append_lifecycle_event.assert_called_once()
        args = mock_redis.append_lifecycle_event.call_args
        assert args[0][0] == "job_command"  # channel name
        assert args[0][1]["action"] == "new_job"  # data content

    @pytest.mark.asyncio
    async def test_create_nas_source_part_persists_immutable_reference(
        self, client, app, monkeypatch, tmp_path,
    ):
        root = tmp_path / "source-library"
        source = root / "20250914-交易节奏" / "P01.mkv"
        source.parent.mkdir(parents=True)
        payload = b"trusted-video"
        source.write_bytes(payload)
        monkeypatch.setenv(
            "FLORI_SOURCE_ROOTS_JSON",
            json.dumps({"zg-library": str(root)}),
        )
        digest = hashlib.sha256(payload).hexdigest()

        response = await client.post("/api/jobs", json={
            "content_type": "video",
            "title": "一场直播",
            "parts": [{
                "title": "第一部分",
                "source": {
                    "root_id": "zg-library",
                    "relative_path": "20250914-交易节奏/P01.mkv",
                    "sha256": digest,
                    "size_bytes": len(payload),
                },
            }],
        })

        assert response.status_code == 201, response.text
        job_id = response.json()["job_id"]
        part = app.state.db.get_parts(job_id)[0]
        assert part.source_url is None
        assert part.source_ref.startswith("nas://zg-library/")
        assert part.source_digest == f"sha256:{digest}"
        assert part.size_bytes == len(payload)
        detail = (await client.get(f"/api/jobs/{job_id}")).json()
        assert detail["parts"][0]["source"] == {
            "root_id": "zg-library",
            "relative_path": "20250914-交易节奏/P01.mkv",
            "sha256": digest,
            "size_bytes": len(payload),
            "status": "available",
        }
        assert str(root) not in json.dumps(detail, ensure_ascii=False)

    @pytest.mark.asyncio
    async def test_create_nas_source_rejects_digest_mismatch_without_side_effect(
        self, client, app, monkeypatch, tmp_path,
    ):
        root = tmp_path / "source-library"
        root.mkdir()
        (root / "P01.mkv").write_bytes(b"trusted-video")
        monkeypatch.setenv(
            "FLORI_SOURCE_ROOTS_JSON",
            json.dumps({"zg-library": str(root)}),
        )
        before = len(app.state.db.list_jobs())

        response = await client.post("/api/jobs", json={
            "content_type": "video",
            "parts": [{
                "source": {
                    "root_id": "zg-library",
                    "relative_path": "P01.mkv",
                    "sha256": "0" * 64,
                    "size_bytes": len(b"trusted-video"),
                },
            }],
        })

        assert response.status_code == 422
        assert len(app.state.db.list_jobs()) == before

    @pytest.mark.asyncio
    async def test_video_requires_parts_and_rejects_legacy_url(self, client):
        response = await client.post("/api/jobs", json={
            "content_type": "video",
            "url": "BV1xx411c7mD",
        })
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_create_multipart_video_without_workers_is_one_ordered_job(
        self, client, mock_redis,
    ):
        mock_redis.list_worker_ids.return_value = []
        payload = {
            "content_type": "video",
            "title": "一场直播",
            "parts": [
                {"url": "BV1xx411c7mD", "title": "开场"},
                {"url": "BV1yy422d8nE", "title": "正文"},
                {"url": "https://youtu.be/dQw4w9WgXcQ", "title": "答疑"},
            ],
        }
        created = await client.post("/api/jobs", json=payload)
        replay = await client.post("/api/jobs", json=payload)

        assert created.status_code == 201
        assert replay.status_code == 201
        assert replay.json()["job_id"] == created.json()["job_id"]
        assert [item["part_index"] for item in created.json()["parts"]] == [1, 2, 3]
        listed = (await client.get("/api/jobs")).json()
        assert listed["total"] == 1
        detail = (
            await client.get(f"/api/jobs/{created.json()['job_id']}")
        ).json()
        assert detail["title"] == "一场直播"
        assert detail["steps"] == []
        assert [item["title"] for item in detail["parts"]] == ["开场", "正文", "答疑"]
        assert all(item["status"] == "pending" for item in detail["parts"])

    @pytest.mark.asyncio
    async def test_video_replay_identity_includes_processing_context(self, client):
        base = {
            "content_type": "video",
            "parts": [{"url": "BV1xx411c7mD", "title": "P01"}],
            "domain": "finance",
        }
        first = await client.post("/api/jobs", json=base)
        replay = await client.post("/api/jobs", json=base)
        changed = await client.post("/api/jobs", json={**base, "domain": "general"})

        assert first.status_code == replay.status_code == changed.status_code == 201
        assert replay.json()["job_id"] == first.json()["job_id"]
        assert changed.json()["job_id"] != first.json()["job_id"]

    @pytest.mark.asyncio
    async def test_create_rejects_unsupported_later_video_part(self, client, app):
        response = await client.post("/api/jobs", json={
            "content_type": "video",
            "parts": [
                {"url": "BV1xx411c7mD"},
                {"url": "ftp://example.test/hidden.mp4"},
            ],
        })

        assert response.status_code == 422
        assert "P02" in response.text
        total, _ = app.state.db.list_jobs(limit=10)
        assert total == 0

    @pytest.mark.asyncio
    async def test_create_arxiv_job(self, client):
        resp = await client.post("/api/jobs", json={
            "url": "https://arxiv.org/abs/2301.00001",
            "domain": "ml",
        })
        assert resp.status_code == 201
        assert resp.json()["content_type"] == "document"
        assert resp.json()["document_kind"] == "research_paper"
        assert resp.json()["pipeline"] == "document"

    @pytest.mark.asyncio
    async def test_create_with_style_tags(self, client):
        resp = await client.post(
            "/api/jobs",
            json=_video_request(style_tags=["lecture", "case-study"]),
        )
        assert resp.status_code == 201

    @pytest.mark.asyncio
    async def test_create_unknown_collection_rejected(self, client):
        # collection_id 不存在 → 400(防孤儿绑定 + job_count 漂移)
        resp = await client.post(
            "/api/jobs",
            json=_video_request(collection_id="c_does_not_exist"),
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_create_article_job(self, client):
        resp = await client.post("/api/jobs", json={
            "url": "https://example.com/post/intro",
        })
        assert resp.status_code == 201
        assert resp.json()["content_type"] == "document"
        assert resp.json()["document_kind"] == "article"
        assert resp.json()["pipeline"] == "document"

    @pytest.mark.asyncio
    async def test_create_podcast_job(self, client):
        resp = await client.post("/api/jobs", json={
            "url": "https://cdn.example.com/ep/1.mp3",
        })
        assert resp.status_code == 201
        assert resp.json()["content_type"] == "audio"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("url", ["ftp://example.com/file", "not-a-supported-id"])
    async def test_create_unknown_source_rejected_before_enqueue(self, client, mock_redis, url):
        resp = await client.post("/api/jobs", json={"url": url})
        assert resp.status_code == 422
        assert "unsupported_source" in resp.json()["message"]
        mock_redis.publish.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_explicit_source_content_type_mismatch_rejected(self, client, mock_redis):
        resp = await client.post("/api/jobs", json={
            "url": "https://youtu.be/dQw4w9WgXcQ", "content_type": "document",
            "document_kind": "article",
        })
        assert resp.status_code == 422
        mock_redis.publish.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unknown_content_type_is_openapi_422(self, client):
        resp = await client.post("/api/jobs", json={
            "url": "https://example.com/post", "content_type": "mystery",
        })
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_source_catalog_and_openapi_enums_follow_registry(self, client):
        catalog = (await client.get("/api/sources")).json()
        assert {item["type"] for item in catalog["content_types"]} == {
            "video", "document", "audio",
        }
        assert "book_toc" in {
            item["type"] for item in catalog["subscription_sources"]
        }
        openapi = (await client.get("/openapi.json")).json()
        schemas = openapi["components"]["schemas"]
        assert set(schemas["ContentType"]["enum"]) == {
            "video", "document", "audio",
        }
        assert "book_toc" in schemas["SubscriptionSourceType"]["enum"]
        assert "youtube_playlist" in schemas["SubscriptionSourceType"]["enum"]
        assert schemas["JobCreateRequest"]["properties"]["mechanical_only"]["default"] is False
        assert "/api/jobs/{job_id}/continue-ai" in openapi["paths"]

    @pytest.mark.asyncio
    async def test_unsupported_upload_extension_rejected(self, client, mock_redis):
        resp = await client.post(
            "/api/jobs/upload?content_type=document&document_kind=unknown",
            files={"file": ("payload.zip", b"not-media", "application/zip")},
        )
        assert resp.status_code == 422
        mock_redis.publish.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_direct_file_url_is_not_a_publicly_creatable_source(self, client, mock_redis):
        resp = await client.post(
            "/api/jobs",
            json={
                "url": "file:///data/private.txt", "content_type": "document",
                "document_kind": "article",
            },
        )
        assert resp.status_code == 422
        mock_redis.publish.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_create_publishes_document_pipeline(self, client, mock_redis):
        await client.post("/api/jobs", json={"url": "https://example.com/p"})
        args = mock_redis.append_lifecycle_event.call_args
        assert args[0][1]["pipeline"] == "document"


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
        await client.post("/api/jobs", json=_video_request())
        resp = await client.get("/api/jobs")
        assert resp.json()["total"] == 1


class TestGetJob:
    @pytest.mark.asyncio
    async def test_get_existing(self, client):
        create_resp = await client.post("/api/jobs", json=_video_request())
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

    @pytest.mark.asyncio
    async def test_document_language_comes_from_structured_metadata(self, client, app):
        created = await client.post("/api/jobs", json={
            "url": "https://arxiv.org/abs/2301.00001",
        })
        job_id = created.json()["job_id"]
        storage = app.state.storage
        await storage.write_file(
            job_id, "intermediate/document.json",
            json.dumps({"source_profile": "scholarly_html", "metadata": {"lang": "en"}}).encode(),
        )

        body = (await client.get(f"/api/jobs/{job_id}")).json()

        assert body["media"]["lang"] == "en"
        assert body["update_available"] is False

    @pytest.mark.asyncio
    async def test_detail_reports_pipeline_update_only_when_done_digest_is_stale(self, client, app):
        created = await client.post("/api/jobs", json={
            "url": "https://arxiv.org/abs/2301.00001",
        })
        job_id = created.json()["job_id"]
        first_step = app.state.config.pipelines["document"]["steps"][0]["name"]
        await app.state.storage.write_file(
            job_id, f".{first_step}.done", b'{"def_digest":"sha256:stale"}',
        )

        body = (await client.get(f"/api/jobs/{job_id}")).json()

        assert body["update_available"] is True
        assert body["update_from_step"] == first_step

    @pytest.mark.asyncio
    async def test_detail_reports_prompt_update_after_job_snapshot(self, client, app):
        created = await client.post("/api/jobs", json=_video_request())
        job_id = created.json()["job_id"]
        app.state.db.set_prompt_override(
            "global", None, "video", "11_smart", "新的智能笔记提示词",
        )

        body = (await client.get(f"/api/jobs/{job_id}")).json()

        assert body["update_available"] is True
        assert body["update_from_step"] == "11_smart"


class TestDeleteJob:
    @pytest.mark.asyncio
    async def test_delete(self, client):
        create_resp = await client.post("/api/jobs", json=_video_request())
        job_id = create_resp.json()["job_id"]
        resp = await client.delete(f"/api/jobs/{job_id}")
        assert resp.status_code == 204
        resp2 = await client.get(f"/api/jobs/{job_id}")
        assert resp2.status_code == 404

    @pytest.mark.asyncio
    async def test_delete_purges_artifacts(self, client, app):
        # 删 job 必须经 storage 清产物(本地删目录 / MinIO 删 {job_id}/ 前缀),否则对象存储留孤儿。
        create_resp = await client.post("/api/jobs", json=_video_request())
        job_id = create_resp.json()["job_id"]
        storage = app.state.storage
        await storage.write_file(job_id, "output/notes.md", b"note")
        assert await storage.read_file(job_id, "output/notes.md") == b"note"
        resp = await client.delete(f"/api/jobs/{job_id}")
        assert resp.status_code == 204
        assert await storage.read_file(job_id, "output/notes.md") is None  # 产物已清

    @pytest.mark.asyncio
    async def test_delete_nas_source_job_never_deletes_original(
        self, client, monkeypatch, tmp_path,
    ):
        root = tmp_path / "source-library"
        root.mkdir()
        source = root / "P01.mkv"
        payload = b"trusted-video"
        source.write_bytes(payload)
        monkeypatch.setenv(
            "FLORI_SOURCE_ROOTS_JSON", json.dumps({"library": str(root)}),
        )
        created = await client.post("/api/jobs", json={
            "content_type": "video",
            "parts": [{"source": {
                "root_id": "library",
                "relative_path": "P01.mkv",
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size_bytes": len(payload),
            }}],
        })

        response = await client.delete(f"/api/jobs/{created.json()['job_id']}")

        assert response.status_code == 204
        assert source.read_bytes() == payload

    @pytest.mark.asyncio
    async def test_delete_clears_ai_usage_and_calls_queue_cleanup(self, client, app, db, mock_redis):
        from shared.models import AIUsage
        create_resp = await client.post("/api/jobs", json=_video_request())
        job_id = create_resp.json()["job_id"]
        db.record_ai_usage(AIUsage(exec_id="e9", provider="claude-cli", model="sonnet",
                                   job_id=job_id, cost_usd=0.3))
        assert db.list_usage_by_job(job_id)
        resp = await client.delete(f"/api/jobs/{job_id}")
        assert resp.status_code == 204
        assert db.list_usage_by_job(job_id) == []            # ai_usage 级联清
        mock_redis.remove_job_tasks.assert_awaited_with(job_id)  # 队列清理被调用


class TestPathTraversal:
    @pytest.mark.asyncio
    async def test_job_id_with_dots_rejected(self, client):
        # %2e%2e 解码为 ".." 且仍在单段内,真正到达 _validate_job_id 守卫 → 严格 400(不接受 404 蒙混)。
        # 若用 ..%2F..,会被路由折叠成"未匹配 404",守卫根本不执行,删掉守卫测试照样绿。
        resp = await client.get("/api/jobs/%2e%2e_passwd")
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_job_id_with_slash_rejected(self, client):
        # DELETE 同守卫:含 ".." 的 job_id 段必须 400。
        resp = await client.delete("/api/jobs/%2e%2e_passwd")
        assert resp.status_code == 400


class TestJobFiltersAndFacets:
    @pytest.mark.asyncio
    async def test_filter_domain_source(self, client, app):
        db = app.state.db
        db.create_job(Job(id="j1", content_type="video", pipeline="video", domain="finance", source="bilibili"))
        db.create_job(Job(
            id="j2", content_type="document", pipeline="document",
            document_kind="research_paper", domain="ml", source="arxiv",
        ))
        db.create_job(Job(id="j3", content_type="video", pipeline="video", domain="finance", source="bilibili"))
        assert (await client.get("/api/jobs?domain=finance")).json()["total"] == 2
        assert (await client.get("/api/jobs?source=arxiv")).json()["total"] == 1
        assert (await client.get("/api/jobs?domain=finance&source=bilibili")).json()["total"] == 2

    @pytest.mark.asyncio
    async def test_facets(self, client, app):
        db = app.state.db
        db.create_job(Job(id="j1", content_type="video", pipeline="video", domain="finance", source="bilibili"))
        db.create_job(Job(
            id="j2", content_type="document", pipeline="document",
            document_kind="research_paper", domain="ml", source="arxiv",
        ))
        db.create_job(Job(id="j3", content_type="video", pipeline="video", domain="finance", source="bilibili"))
        f = (await client.get("/api/jobs/facets")).json()       # 须未被 /{job_id} 捕获
        assert f["source"]["bilibili"] == 2 and f["source"]["arxiv"] == 1
        assert f["domain"]["finance"] == 2 and f["domain"]["ml"] == 1


class TestJobConcepts:
    @pytest.mark.asyncio
    async def test_reverse_lookup(self, client, app):
        db = app.state.db
        db.create_job(Job(id="jx", content_type="video", pipeline="video", domain="finance"))
        db.add_glossary_suggestion("finance", "坐庄", "jx", "video", "scene-3")
        db.add_glossary_suggestion("finance", "无关概念", "jother", "video")
        body = (await client.get("/api/jobs/jx/concepts")).json()
        terms = {c["term"] for c in body}
        assert "坐庄" in terms and "无关概念" not in terms       # 只返命中本 job 的概念
        c = next(c for c in body if c["term"] == "坐庄")
        assert c["job_occurrences"][0]["job_id"] == "jx" and c["job_occurrences"][0]["location"] == "scene-3"

    @pytest.mark.asyncio
    async def test_concepts_404(self, client):
        assert (await client.get("/api/jobs/nope/concepts")).status_code == 404


class TestCollectionName:
    @pytest.mark.asyncio
    async def test_collection_name_in_detail(self, client, app):
        db = app.state.db
        db.create_collection(Collection(id="c1", name="我的合集", domain="finance"))
        db.create_job(Job(id="jc", content_type="video", pipeline="video", domain="finance", collection_id="c1"))
        assert (await client.get("/api/jobs/jc")).json()["collection_name"] == "我的合集"

    @pytest.mark.asyncio
    async def test_collection_name_null_when_unassigned(self, client, app):
        app.state.db.create_job(Job(id="ju", content_type="video", pipeline="video", domain="finance"))
        assert (await client.get("/api/jobs/ju")).json()["collection_name"] is None

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
        # step 段含 ".." 直达 validate_path_segment(step) 守卫,严格 400;若用 ..%2F.. 会被路由折叠成 404,到不了守卫。
        resp = await client.get("/api/jobs/j1/steps/%2e%2e_secret/log")
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_part_log_and_ai_log_require_explicit_part_scope(
        self, client, app, test_config,
    ):
        job_id = "j_part_logs"
        part = JobPart("pt_a", job_id, 1, source_url="BV1xx411c7mD")
        app.state.db.create_job(
            Job(id=job_id, content_type="video", pipeline="video"), [part],
        )
        part_dir = test_config.jobs_dir / job_id / "parts" / part.id
        (part_dir / "logs").mkdir(parents=True)
        (part_dir / "logs" / "08_punctuate.log").write_text("part-log")
        (part_dir / "output" / "ai_logs").mkdir(parents=True)
        (part_dir / "output" / "ai_logs" / "08_punctuate.jsonl").write_text(
            '{"call_index":0,"ok":true}\n',
        )

        part_log = await client.get(
            f"/api/jobs/{job_id}/parts/{part.id}/steps/08_punctuate/log",
        )
        assert part_log.status_code == 200
        assert part_log.text == "part-log"
        assert (await client.get(
            f"/api/jobs/{job_id}/steps/08_punctuate/log",
        )).status_code == 404
        assert (await client.get(
            f"/api/jobs/{job_id}/parts/pt_other/steps/08_punctuate/log",
        )).status_code == 404

        root_logs = (await client.get(
            f"/api/jobs/{job_id}/ai-logs?step=08_punctuate",
        )).json()
        assert root_logs["steps"] == []
        part_logs = (await client.get(
            f"/api/jobs/{job_id}/ai-logs?step=08_punctuate&part_id={part.id}",
        )).json()
        assert part_logs["steps"] == [{
            "scope_key": "part:pt_a",
            "part_id": "pt_a",
            "step": "08_punctuate",
            "calls": [{"call_index": 0, "ok": True}],
        }]


class TestRetryRerunResubmit:
    @pytest.mark.asyncio
    async def test_retry_non_failed(self, client):
        create_resp = await client.post("/api/jobs", json=_video_request())
        job_id = create_resp.json()["job_id"]
        resp = await client.post(f"/api/jobs/{job_id}/retry")
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_rerun(self, client, app, mock_redis):
        create_resp = await client.post(
            "/api/jobs",
            json=_video_request(title="Authoritative course title"),
        )
        job_id = create_resp.json()["job_id"]
        job_doc = json.loads(await app.state.storage.read_file(job_id, "job.json"))
        assert job_doc["title"] == "Authoritative course title"
        job_doc.pop("title")
        await app.state.storage.write_file(
            job_id,
            "job.json",
            json.dumps(job_doc).encode("utf-8"),
        )
        resp = await client.post(
            f"/api/jobs/{job_id}/rerun",
            json={"from_step": "11_smart"},
        )
        assert resp.status_code == 200
        assert resp.json()["from_step"] == "11_smart"
        job_doc = json.loads(await app.state.storage.read_file(job_id, "job.json"))
        assert job_doc["title"] == "Authoritative course title"
        # 真实副作用:job 以 rerun 命令被重新派发(而非只回显入参)。最后一次 publish 是 rerun。
        ch, payload = mock_redis.append_lifecycle_event.call_args[0]
        assert ch == "job_command"
        assert payload["action"] == "rerun" and payload["job_id"] == job_id
        assert payload["from_step"] == "11_smart"

    @pytest.mark.asyncio
    async def test_part_rerun_uses_scoped_execution_step(self, client, mock_redis):
        create_resp = await client.post("/api/jobs", json=_video_request())
        body = create_resp.json()
        job_id = body["job_id"]
        part_id = body["parts"][0]["part_id"]

        rejected = await client.post(
            f"/api/jobs/{job_id}/rerun",
            json={"from_step": "02_whisper"},
        )
        assert rejected.status_code == 422
        resp = await client.post(
            f"/api/jobs/{job_id}/parts/{part_id}/rerun",
            json={"from_step": "02_whisper"},
        )

        assert resp.status_code == 200
        assert resp.json()["part_id"] == part_id
        _, payload = mock_redis.append_lifecycle_event.call_args[0]
        assert payload == {
            "action": "rerun",
            "job_id": job_id,
            "part_id": part_id,
            "from_step": f"part:{part_id}::02_whisper",
        }

    @pytest.mark.asyncio
    async def test_resubmit(self, client, mock_redis):
        create_resp = await client.post("/api/jobs", json=_video_request())
        job_id = create_resp.json()["job_id"]
        resp = await client.post(f"/api/jobs/{job_id}/resubmit")
        assert resp.status_code == 200
        # 真实副作用:job 以 resubmit 命令被重新派发。
        ch, payload = mock_redis.append_lifecycle_event.call_args[0]
        assert ch == "job_command" and payload == {"action": "resubmit", "job_id": job_id}

    @pytest.mark.asyncio
    async def test_retry_all_failed(self, client, mock_redis, db):
        from shared.models import JobStatus
        ids = []
        for u in ("BV1xx411c7mD", "BV1yy422d8nE"):
            jid = (await client.post("/api/jobs", json=_video_request(u))).json()["job_id"]
            db.update_job(jid, status=JobStatus.FAILED)
            ids.append(jid)
        resp = await client.post("/api/jobs/retry-failed")
        assert resp.status_code == 200
        assert resp.json()["retried"] >= 2
        retried = {c[0][1]["job_id"] for c in mock_redis.append_lifecycle_event.call_args_list
                   if c[0][0] == "job_command" and c[0][1].get("action") == "retry"}
        assert set(ids) <= retried

    @pytest.mark.asyncio
    async def test_retry_all_failed_scoped_by_collection(self, client, mock_redis, db):
        # retry-failed?collection_id 只重试该集合的失败 job。
        from shared.models import Collection, Job, JobStatus
        db.create_collection(Collection(id="c_a", name="A", domain="general"))
        db.create_collection(Collection(id="c_b", name="B", domain="general"))
        db.create_job(Job(id="ja_fail", content_type="video", pipeline="video",
                          collection_id="c_a", status=JobStatus.FAILED))
        db.create_job(Job(id="ja_done", content_type="video", pipeline="video",
                          collection_id="c_a", status=JobStatus.DONE))
        db.create_job(Job(id="jb_fail", content_type="video", pipeline="video",
                          collection_id="c_b", status=JobStatus.FAILED))
        resp = await client.post("/api/jobs/retry-failed?collection_id=c_a")
        assert resp.status_code == 200
        assert resp.json()["retried"] == 1   # 仅 c_a 的 1 个失败
        retried = {c[0][1]["job_id"] for c in mock_redis.append_lifecycle_event.call_args_list
                   if c[0][0] == "job_command" and c[0][1].get("action") == "retry"}
        assert retried == {"ja_fail"}        # 不含 c_b 的失败、不含 c_a 的 done


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
    async def test_list_providers_marks_live_worker_capability(self, client):
        resp = await client.get("/api/providers")
        assert resp.status_code == 200
        provs = {p["name"]: p for p in resp.json()["providers"]}
        assert provs["claude-cli"]["available"] is True
        assert provs["anthropic"]["available"] is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize("worker", [
        None,
        {"pools": "ai", "tags": "claude-cli", "status": "offline"},
        {"pools": "ai", "tags": "claude-cli", "admin_status": "paused"},
        {"pools": "cpu", "tags": "claude-cli", "status": "idle"},
        {"pools": "ai", "tags": "openai-api", "status": "idle"},
    ])
    async def test_provider_unavailable_without_matching_live_ai_worker(
        self, client, mock_redis, worker,
    ):
        mock_redis.get_worker_info.return_value = worker
        providers = {
            item["name"]: item
            for item in (await client.get("/api/providers")).json()["providers"]
        }
        assert providers["claude-cli"]["available"] is False

    @pytest.mark.asyncio
    async def test_rerun_smart_unavailable_provider_rejected(self, client):
        await client.post("/api/jobs", json=_video_request())
        jid = (await client.get("/api/jobs")).json()["items"][0]["job_id"]
        resp = await client.post(f"/api/jobs/{jid}/rerun-smart", json={"provider": "anthropic"})
        assert resp.status_code == 400  # 无 key 不可用

    @pytest.mark.asyncio
    async def test_rerun_rejects_unknown_provider_even_with_matching_api_env(
        self, client, app, mock_redis, monkeypatch,
    ):
        await client.post("/api/jobs", json=_video_request())
        jid = (await client.get("/api/jobs")).json()["items"][0]["job_id"]
        storage = app.state.storage
        await storage.write_file(jid, "job.json", b'{"id":"x"}')
        before = await storage.read_file(jid, "job.json")
        monkeypatch.setenv("TYPO-PROVIDER_API_KEY", "must-not-make-it-configured")
        mock_redis.publish.reset_mock()

        resp = await client.post(
            f"/api/jobs/{jid}/rerun-smart", json={"provider": "typo-provider"},
        )

        assert resp.status_code == 400
        assert await storage.read_file(jid, "job.json") == before
        mock_redis.publish.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("raw", [
        b"",
        b"{",
        b"null",
        b"[]",
        b'"job"',
        b'{"ai_overrides":null}',
        b'{"ai_overrides":[]}',
        b'{"ai_overrides":false}',
        b'{"ai_overrides":"claude-cli"}',
    ])
    async def test_rerun_malformed_job_metadata_is_stable_4xx_without_side_effects(
        self, client, app, mock_redis, raw,
    ):
        await client.post("/api/jobs", json=_video_request())
        jid = (await client.get("/api/jobs")).json()["items"][0]["job_id"]
        storage = app.state.storage
        await storage.write_file(jid, "job.json", raw)
        mock_redis.publish.reset_mock()

        resp = await client.post(
            f"/api/jobs/{jid}/rerun-smart", json={"provider": "claude-cli"},
        )

        assert 400 <= resp.status_code < 500
        assert await storage.read_file(jid, "job.json") == raw
        mock_redis.publish.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_rerun_requires_workers_for_both_target_step_tag_profiles(
        self, client, app, mock_redis,
    ):
        await client.post("/api/jobs", json=_video_request())
        jid = (await client.get("/api/jobs")).json()["items"][0]["job_id"]
        await app.state.storage.write_file(jid, "job.json", b'{"id":"x"}')
        mock_redis.get_worker_info.return_value = {
            "pools": "ai", "tags": "claude-cli", "status": "idle",
        }
        mock_redis.publish.reset_mock()

        resp = await client.post(
            f"/api/jobs/{jid}/rerun-smart", json={"provider": "claude-cli"},
        )

        assert resp.status_code == 400
        mock_redis.publish.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_rerun_smart_claude_writes_override(self, client, app, mock_redis):
        await client.post("/api/jobs", json=_video_request())
        jid = (await client.get("/api/jobs")).json()["items"][0]["job_id"]
        # 预置 job.json(storage 本地)
        storage = app.state.storage
        await storage.write_file(jid, "job.json", b'{"id":"x"}')
        resp = await client.post(f"/api/jobs/{jid}/rerun-smart", json={"provider": "claude-cli"})
        assert resp.status_code == 200 and resp.json()["provider"] == "claude-cli"
        import json as _j
        doc = _j.loads((await storage.read_file(jid, "job.json")).decode())
        assert doc["ai_overrides"]["11_smart"] == "claude-cli"
        assert doc["ai_overrides"]["12_review"] == "claude-cli"

    @pytest.mark.asyncio
    @pytest.mark.asyncio
    @pytest.mark.parametrize(("pipeline", "smart", "review"), [
        ("video", "11_smart", "12_review"),
        ("document", "05_smart", "08_review"),
        ("audio", "04_smart_podcast", "05_review"),
    ])
    async def test_rerun_smart_uses_pipeline_roles(
        self, pipeline, smart, review, client, app, db, mock_redis,
    ):
        jid = f"j_role_{pipeline}"
        db.create_job(Job(
            id=jid, content_type=pipeline, pipeline=pipeline,
            document_kind="research_paper" if pipeline == "document" else "",
        ))
        storage = app.state.storage
        await storage.write_file(jid, "job.json", b'{"id":"x"}')
        resp = await client.post(f"/api/jobs/{jid}/rerun-smart", json={"provider": "claude-cli"})
        assert resp.status_code == 200
        assert resp.json()["from_step"] == smart and resp.json()["review_step"] == review
        doc = json.loads((await storage.read_file(jid, "job.json")).decode())
        assert doc["ai_overrides"] == {smart: "claude-cli", review: "claude-cli"}


class TestRebuildP2c:
    @pytest.mark.asyncio
    async def test_rebuild_rejects_active_parent(self, client, app):
        parent_id = "jobs_rebuild_active"
        app.state.db.create_job(Job(
            id=parent_id, content_type="document", pipeline="document",
            document_kind="research_paper", source="arxiv",
            meta={"flags": {"mechanical_only": True}},
            status=JobStatus.PROCESSING,
        ))
        response = await client.post(f"/api/jobs/{parent_id}/rebuild")
        assert response.status_code == 409
        assert len(app.state.db.lineage_versions(parent_id)) == 1

    def test_late_heartbeat_cannot_overwrite_ready_event_checkpoint(self, app):
        db = app.state.db
        owner = "owner-a"
        job = Job(
            id="jr_heartbeat_cas", content_type="document", pipeline="document",
            document_kind="research_paper", source="arxiv", is_current=False,
            status=JobStatus.PROCESSING,
            meta={"rebuild_request": {
                "owner_token": owner, "phase": "cloning", "event_published": False,
            }},
        )
        db.create_job(job)
        ready_meta = {"rebuild_request": {
            "owner_token": owner, "phase": "ready", "event_published": True,
        }}
        assert db._update_rebuild_reservation(
            job.id, owner, ready_meta, status=JobStatus.PENDING, is_current=True,
        ) is True

        assert db._heartbeat_rebuild_reservation(job.id, owner, "later") is False
        assert db.get_job(job.id).meta["rebuild_request"] == ready_meta["rebuild_request"]

    @pytest.mark.asyncio
    async def test_empty_rebuild_body_uses_stable_server_operation_key(
        self, client, app,
    ):
        db, storage = app.state.db, app.state.storage
        parent_id = "jobs_rebuild_default_key"
        db.create_job(Job(
            id=parent_id, content_type="document", pipeline="document",
            document_kind="research_paper", source="arxiv",
            meta={"flags": {"mechanical_only": True}}, status=JobStatus.DONE,
        ))
        await storage.write_file(parent_id, "job.json", b"{}")

        first = await client.post(f"/api/jobs/{parent_id}/rebuild")
        second = await client.post(f"/api/jobs/{parent_id}/rebuild")

        assert first.status_code == second.status_code == 200
        assert first.json()["job_id"] == second.json()["job_id"]
        assert len(db.lineage_versions(parent_id)) == 2

        app.state.config.pipelines["document"]["steps"][0]["version"] = "operation-v2"
        after_definition_change = await client.post(f"/api/jobs/{parent_id}/rebuild")
        assert after_definition_change.status_code == 200
        assert after_definition_change.json()["job_id"] != first.json()["job_id"]
        assert len(db.lineage_versions(parent_id)) == 3

    @pytest.mark.asyncio
    async def test_rebuild_reservation_keeps_parent_current_until_clone_finishes(
        self, client, app, monkeypatch,
    ):
        db, storage = app.state.db, app.state.storage
        parent_id, key = "jobs_rebuild_reservation", "reservation-proof"
        target_id = "jr_" + hashlib.sha256(f"{parent_id}\0{key}".encode()).hexdigest()[:24]
        db.create_job(Job(
            id=parent_id, content_type="document", pipeline="document",
            document_kind="research_paper", source="arxiv",
            meta={"flags": {"mechanical_only": True}}, status=JobStatus.DONE,
        ))
        await storage.write_file(parent_id, "job.json", b"{}")
        clone_started, release_clone = asyncio.Event(), asyncio.Event()
        original_clone = storage.clone

        async def slow_clone(source, target):
            clone_started.set()
            await release_clone.wait()
            await original_clone(source, target)

        monkeypatch.setattr(storage, "clone", slow_clone)
        request = asyncio.create_task(client.post(
            f"/api/jobs/{parent_id}/rebuild",
            json={"mechanical_only": True, "idempotency_key": key},
        ))
        await clone_started.wait()

        assert db.get_job(parent_id).is_current is True
        reservation = db.get_job(target_id)
        assert reservation is not None
        assert reservation.is_current is False
        assert reservation.status == JobStatus.PROCESSING

        release_clone.set()
        response = await request
        assert response.status_code == 200
        assert db.get_job(target_id).is_current is True

    """/rebuild 建新快照(fork:clone 产物+.done、父降级、新版 current);/rebuild-stale 只挑过期。"""

    @pytest.mark.asyncio
    async def test_rebuild_creates_snapshot(self, client, app):
        db, storage, config = app.state.db, app.state.storage, app.state.config
        first = config.pipelines["document"]["steps"][0]["name"]
        db.create_job(Job(
            id="jobs_paper_p1", content_type="document", pipeline="document",
            document_kind="research_paper", url="https://arxiv.org/abs/1810.04805",
            source="arxiv", lineage_key="jobs_paper_p1", status=JobStatus.DONE,
        ))
        await storage.write_file(
            "jobs_paper_p1", "job.json",
            b'{"id":"jobs_paper_p1","content_type":"paper","pipeline":"paper"}',
        )
        await storage.write_file("jobs_paper_p1", "output/note.md", b"hello")
        await storage.write_file("jobs_paper_p1", f".{first}.done", b'{"def_digest":"sha256:old"}')
        resp = await client.post("/api/jobs/jobs_paper_p1/rebuild")
        assert resp.status_code == 200
        body = resp.json()
        new_id = body["job_id"]
        assert new_id != "jobs_paper_p1"
        assert body["parent_job_id"] == "jobs_paper_p1"
        assert body["lineage_key"] == "jobs_paper_p1"
        new = db.get_job(new_id)
        assert new is not None and new.parent_job_id == "jobs_paper_p1"
        assert new.is_current is True
        assert db.get_job("jobs_paper_p1").is_current is False        # 父降级
        assert await storage.read_file(new_id, "output/note.md") == b"hello"   # 产物 clone
        assert await storage.read_file(new_id, f".{first}.done") is not None   # .done 播种
        rebuilt_doc = json.loads((await storage.read_file(new_id, "job.json")).decode())
        assert rebuilt_doc["content_type"] == "document"
        assert rebuilt_doc["document_kind"] == "research_paper"
        assert rebuilt_doc["source_profile"] == "scholarly_html"
        assert rebuilt_doc["source"] == "arxiv"
        assert "pipeline" not in rebuilt_doc

    @pytest.mark.asyncio
    async def test_video_partial_rebuild_preserves_each_part_scope(
        self, client, app,
    ):
        db, storage, config = app.state.db, app.state.storage, app.state.config
        created = await client.post("/api/jobs", json={
            "content_type": "video",
            "title": "跨 Part 重建",
            "mechanical_only": True,
            "parts": [
                {"url": "BV1xx411c7mD", "title": "上半场"},
                {"url": "BV1Q541167Qg", "title": "下半场"},
            ],
        })
        assert created.status_code == 201
        parent_id = created.json()["job_id"]
        parts = db.get_parts(parent_id)
        part_steps = [
            cfg for cfg in config.pipelines["video"]["steps"]
            if cfg.get("scope") == "part"
        ]
        for part in parts:
            scope_key = part_scope(part.id)
            for cfg in part_steps:
                db.upsert_step(Step(
                    job_id=parent_id,
                    scope_key=scope_key,
                    name=cfg["name"],
                    status=StepStatus.DONE,
                    pool=cfg["pool"],
                    input_hash=f"{part.id}:{cfg['name']}",
                ))
                await storage.write_file(
                    parent_id,
                    f"parts/{part.id}/.{cfg['name']}.done",
                    b'{"def_digest":"sha256:old"}',
                )
            await storage.write_file(
                parent_id,
                f"parts/{part.id}/input/subtitle.srt",
                f"subtitle:{part.id}".encode(),
            )
        db.update_job(parent_id, status=JobStatus.DONE)

        rebuilt = await client.post(f"/api/jobs/{parent_id}/rebuild", json={
            "mechanical_only": True,
            "from_step": "02_whisper",
            "idempotency_key": "video-partial-rebuild",
        })

        assert rebuilt.status_code == 200
        target_id = rebuilt.json()["job_id"]
        target_parts = db.get_parts(target_id)
        assert [part.id for part in target_parts] == [part.id for part in parts]
        root_doc = json.loads((await storage.read_file(target_id, "job.json")).decode())
        assert root_doc["url"] is None
        assert [item["part_id"] for item in root_doc["parts"]] == [
            part.id for part in parts
        ]
        child_steps = {
            (step.scope_key, step.name): step for step in db.get_steps(target_id)
        }
        for part in parts:
            scope_key = part_scope(part.id)
            assert child_steps[(scope_key, "01_download")].input_hash == (
                f"{part.id}:01_download"
            )
            assert child_steps[(scope_key, "03_scene")].input_hash == (
                f"{part.id}:03_scene"
            )
            assert (scope_key, "02_whisper") not in child_steps
            assert await storage.read_file(
                target_id, f"parts/{part.id}/.01_download.done",
            ) is not None
            assert await storage.read_file(
                target_id, f"parts/{part.id}/.02_whisper.done",
            ) is None
            assert await storage.read_file(
                target_id, f"parts/{part.id}/input/subtitle.srt",
            ) is None
            part_doc = json.loads((await storage.read_file(
                target_id, f"parts/{part.id}/job.json",
            )).decode())
            assert part_doc["job_id"] == target_id

    @pytest.mark.asyncio
    async def test_rebuild_can_switch_snapshot_to_mechanical_from_validated_step(
        self, client, app, mock_redis,
    ):
        db, storage, config = app.state.db, app.state.storage, app.state.config
        held: set[tuple[str, str]] = set()

        async def acquire(job_id, action, _token, ttl_sec=30):
            key = (job_id, action)
            if key in held:
                return False
            held.add(key)
            return True

        async def release(job_id, action, _token):
            held.discard((job_id, action))
            return True

        mock_redis.acquire_job_control_lock.side_effect = acquire
        mock_redis.release_job_control_lock.side_effect = release
        steps = [item["name"] for item in config.pipelines["document"]["steps"]]
        parent_flags = {"smart_note": True, "mechanical_only": False}
        db.create_job(Job(
            id="jobs_paper_full", content_type="document", pipeline="document",
            document_kind="research_paper", url="https://arxiv.org/abs/1810.04805",
            source="arxiv", lineage_key="jobs_paper_full",
            meta={"flags": parent_flags}, status=JobStatus.DONE,
        ))
        await storage.write_file(
            "jobs_paper_full", "job.json",
            json.dumps({"id": "jobs_paper_full", "flags": parent_flags}).encode(),
        )
        for name in steps:
            await storage.write_file(
                "jobs_paper_full", f".{name}.done", b'{"def_digest":"sha256:old"}',
            )
            cfg = next(
                item for item in config.pipelines["document"]["steps"]
                if item["name"] == name
            )
            db.upsert_step(Step(
                job_id="jobs_paper_full", name=name, status=StepStatus.DONE,
                pool=cfg["pool"], input_hash=f"parent:{name}",
            ))
        await storage.write_file("jobs_paper_full", "input/source.pdf", b"source")
        await storage.write_file("jobs_paper_full", "intermediate/document.json", b"stale")

        request = {
            "mechanical_only": True, "from_step": "02_parse",
            "idempotency_key": "u18-paper-full-mechanical",
        }
        resp, replay = await asyncio.gather(
            client.post("/api/jobs/jobs_paper_full/rebuild", json=request),
            client.post("/api/jobs/jobs_paper_full/rebuild", json=request),
        )

        assert resp.status_code == 200
        assert replay.status_code == 200
        body = resp.json()
        assert replay.json()["job_id"] == body["job_id"]
        assert body["from_step"] == "02_parse"
        assert body["processing_mode"] == "mechanical_only"
        new_id = body["job_id"]
        assert db.get_job(new_id).meta["flags"]["mechanical_only"] is True
        new_doc = json.loads((await storage.read_file(new_id, "job.json")).decode())
        assert new_doc["flags"]["mechanical_only"] is True
        assert db.get_job("jobs_paper_full").meta["flags"] == parent_flags
        assert await storage.read_file(new_id, ".01_download.done") is not None
        assert await storage.read_file(new_id, ".02_parse.done") is None
        assert await storage.read_file(new_id, ".03_structure.done") is None
        child_steps = {step.name: step for step in db.get_steps(new_id)}
        assert child_steps["01_download"].status == StepStatus.DONE
        assert child_steps["01_download"].input_hash == "parent:01_download"
        assert "02_parse" not in child_steps
        assert await storage.read_file(new_id, "input/source.pdf") == b"source"
        assert await storage.read_file(new_id, "intermediate/document.json") is None
        assert await storage.read_file("jobs_paper_full", ".02_parse.done") is not None
        assert len(db.lineage_versions(new_id)) == 2

        conflict = await client.post("/api/jobs/jobs_paper_full/rebuild", json={
            **request, "from_step": "03_structure",
        })
        assert conflict.status_code == 409

    @pytest.mark.asyncio
    async def test_rebuild_rejects_unknown_from_step_without_creating_snapshot(
        self, client, app,
    ):
        db = app.state.db
        db.create_job(Job(
            id="jobs_paper_invalid_step", content_type="document", pipeline="document",
            document_kind="research_paper", source="arxiv",
            lineage_key="jobs_paper_invalid_step", meta={"flags": {}}, status=JobStatus.DONE,
        ))
        before, _ = db.list_jobs(limit=100, current_only=False)

        resp = await client.post("/api/jobs/jobs_paper_invalid_step/rebuild", json={
            "mechanical_only": True, "from_step": "not_a_step",
        })

        after, _ = db.list_jobs(limit=100, current_only=False)
        assert resp.status_code == 422
        assert after == before

    @pytest.mark.asyncio
    async def test_partial_rebuild_rejects_missing_upstream_done_marker(
        self, client, app,
    ):
        db, storage = app.state.db, app.state.storage
        parent_id = "jobs_paper_missing_upstream_marker"
        db.create_job(Job(
            id=parent_id, content_type="document", pipeline="document",
            document_kind="research_paper", url="https://arxiv.org/abs/1810.04805",
            source="arxiv", lineage_key=parent_id, status=JobStatus.DONE,
        ))
        db.upsert_step(Step(
            job_id=parent_id, name="01_download", status=StepStatus.DONE,
            pool="io", input_hash="parent-download",
        ))
        await storage.write_file(parent_id, "job.json", b"{}")

        response = await client.post(f"/api/jobs/{parent_id}/rebuild", json={
            "mechanical_only": True,
            "from_step": "02_parse",
            "idempotency_key": "missing-upstream-marker",
        })

        assert response.status_code == 409
        assert "without completion proof" in response.text
        assert db.get_job(parent_id).is_current is True
        assert [job.id for job in db.lineage_versions(parent_id)] == [parent_id]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("failure_stage", ["clone", "db"])
    async def test_idempotent_rebuild_cleans_partial_target_before_db_commit(
        self, failure_stage, client, app, monkeypatch,
    ):
        db, storage = app.state.db, app.state.storage
        parent_id = f"jobs_rebuild_fail_{failure_stage}"
        key = f"failure-{failure_stage}"
        target_id = "jr_" + hashlib.sha256(
            f"{parent_id}\0{key}".encode(),
        ).hexdigest()[:24]
        db.create_job(Job(
            id=parent_id, content_type="document", pipeline="document",
            document_kind="research_paper", source="arxiv", meta={"flags": {}},
            status=JobStatus.DONE,
        ))
        await storage.write_file(parent_id, "job.json", b"{}")
        if failure_stage == "clone":
            original_clone = storage.clone

            async def fail_clone(source, target):
                await original_clone(source, target)
                await storage.write_file(target, "partial.tmp", b"partial")
                raise RuntimeError("clone failed")

            monkeypatch.setattr(storage, "clone", fail_clone)
        else:
            original_create = db.create_job

            def fail_create(job, parts=None):
                if job.id == target_id:
                    raise RuntimeError("db failed")
                return original_create(job, parts)

            monkeypatch.setattr(db, "create_job", fail_create)

        with pytest.raises(RuntimeError, match=f"{failure_stage} failed"):
            await client.post(f"/api/jobs/{parent_id}/rebuild", json={
                "mechanical_only": True, "idempotency_key": key,
            })

        assert db.get_job(target_id) is None
        assert await storage.read_file(target_id, "job.json") is None
        assert await storage.read_file(target_id, "partial.tmp") is None

    @pytest.mark.asyncio
    async def test_idempotent_rebuild_repairs_event_failure_without_count_duplication(
        self, client, app, mock_redis,
    ):
        db, storage = app.state.db, app.state.storage
        db.create_collection(Collection(id="c_rebuild", name="R", domain="general"))
        db.create_job(Job(
            id="jobs_rebuild_event", content_type="document", pipeline="document",
            document_kind="research_paper", source="arxiv", collection_id="c_rebuild",
            meta={"flags": {}}, status=JobStatus.DONE,
        ))
        db._reconcile_collection_count("c_rebuild")
        await storage.write_file("jobs_rebuild_event", "job.json", b"{}")
        original_append = mock_redis.append_lifecycle_event
        original_append.side_effect = RuntimeError("event failed")
        request = {"mechanical_only": False, "idempotency_key": "event-repair"}

        with pytest.raises(RuntimeError, match="event failed"):
            await client.post("/api/jobs/jobs_rebuild_event/rebuild", json=request)
        versions = db.lineage_versions("jobs_rebuild_event")
        assert len(versions) == 2
        target = next(item for item in versions if item.id != "jobs_rebuild_event")
        assert target.meta["rebuild_request"]["event_published"] is False
        assert db.get_collection("c_rebuild").job_count == 2

        original_append.side_effect = None
        mock_redis.list_worker_ids.return_value = []
        replay = await client.post("/api/jobs/jobs_rebuild_event/rebuild", json=request)

        assert replay.status_code == 200
        assert replay.json()["job_id"] == target.id
        assert len(db.lineage_versions("jobs_rebuild_event")) == 2
        assert db.get_collection("c_rebuild").job_count == 2
        assert db.get_job(target.id).meta["rebuild_request"]["event_published"] is True

    @pytest.mark.asyncio
    async def test_rebuild_stale_repairs_ready_event_before_pending_skip(
        self, client, app, mock_redis,
    ):
        db, storage, config = app.state.db, app.state.storage, app.state.config
        parent_id = "jobs_stale_event_repair"
        first = config.pipelines["document"]["steps"][0]["name"]
        db.create_job(Job(
            id=parent_id, content_type="document", pipeline="document",
            document_kind="research_paper", source="arxiv", status=JobStatus.DONE,
            meta={"flags": {"mechanical_only": True}},
        ))
        await storage.write_file(parent_id, "job.json", b"{}")
        await storage.write_file(parent_id, f".{first}.done", b'{"def_digest":"stale"}')
        mock_redis.append_lifecycle_event.side_effect = RuntimeError("stale event failed")

        with pytest.raises(RuntimeError, match="stale event failed"):
            await client.post("/api/jobs/rebuild-stale")
        child = next(item for item in db.lineage_versions(parent_id) if item.id != parent_id)
        assert child.status == JobStatus.PENDING
        assert child.meta["rebuild_request"]["event_published"] is False

        mock_redis.append_lifecycle_event.side_effect = None
        repaired = await client.post("/api/jobs/rebuild-stale")
        assert repaired.status_code == 200
        assert repaired.json()["items"][0]["job_id"] == child.id
        assert db.get_job(child.id).meta["rebuild_request"]["event_published"] is True

    @pytest.mark.asyncio
    async def test_rebuild_stale_full_target_requires_workers(
        self, client, app, mock_redis,
    ):
        db, storage, config = app.state.db, app.state.storage, app.state.config
        parent_id = "jobs_stale_full_no_workers"
        first = config.pipelines["document"]["steps"][0]["name"]
        db.create_job(Job(
            id=parent_id, content_type="document", pipeline="document",
            document_kind="research_paper", source="arxiv", status=JobStatus.DONE,
            meta={"flags": {"mechanical_only": False, "smart_note": True}},
        ))
        await storage.write_file(parent_id, f".{first}.done", b'{"def_digest":"stale"}')
        mock_redis.list_worker_ids.return_value = []

        response = await client.post("/api/jobs/rebuild-stale")
        assert response.status_code == 503
        assert len(db.lineage_versions(parent_id)) == 1

    @pytest.mark.asyncio
    async def test_rebuild_stale_only_expired(self, client, app):
        db, storage, config = app.state.db, app.state.storage, app.state.config
        first = config.pipelines["document"]["steps"][0]["name"]
        db.create_job(Job(
            id="jobs_paper_stale", content_type="document", pipeline="document",
            document_kind="research_paper", source="arxiv",
            lineage_key="jobs_paper_stale", status=JobStatus.DONE,
        ))
        await storage.write_file("jobs_paper_stale", "job.json", b"{}")
        await storage.write_file("jobs_paper_stale", f".{first}.done", b'{"def_digest":"sha256:STALE"}')
        db.create_job(Job(
            id="jobs_paper_fresh", content_type="document", pipeline="document",
            document_kind="research_paper", source="arxiv",
            lineage_key="jobs_paper_fresh",
        ))  # 无 .done → 不过期
        resp = await client.post("/api/jobs/rebuild-stale")
        assert resp.status_code == 200
        body = resp.json()
        parents = [it["parent_job_id"] for it in body["items"]]
        assert "jobs_paper_stale" in parents
        assert "jobs_paper_fresh" not in parents
        first_count = len(db.lineage_versions("jobs_paper_stale"))
        repeated = await client.post("/api/jobs/rebuild-stale")
        assert repeated.status_code == 200
        assert repeated.json()["rebuilt"] == 0
        assert len(db.lineage_versions("jobs_paper_stale")) == first_count
