"""api/routes/jobs.py 测试。"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from shared.config import AppConfig, load_config
from shared.models import Collection, Job
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

    def test_unknown_file_has_no_default_pipeline(self):
        assert _detect_content_type(None, "payload.zip") is None

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

    def test_unknown_has_no_pipeline(self):
        assert _pipeline_for("mystery") is None


@pytest.fixture
def mock_redis():
    r = AsyncMock()
    r.publish = AsyncMock()
    return r


@pytest.fixture
def app(db, mock_redis, test_config):
    return create_app(db=db, redis=mock_redis, config=test_config)


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
    @pytest.mark.parametrize("url", ["ftp://example.com/file", "not-a-supported-id"])
    async def test_create_unknown_source_rejected_before_enqueue(self, client, mock_redis, url):
        resp = await client.post("/api/jobs", json={"url": url})
        assert resp.status_code == 422
        assert "unsupported_source" in resp.json()["message"]
        mock_redis.publish.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_explicit_source_content_type_mismatch_rejected(self, client, mock_redis):
        resp = await client.post("/api/jobs", json={
            "url": "https://youtu.be/dQw4w9WgXcQ", "content_type": "article",
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
            "video", "paper", "article", "audio",
        }
        assert "book_toc" in {
            item["type"] for item in catalog["subscription_sources"]
        }
        openapi = (await client.get("/openapi.json")).json()
        schemas = openapi["components"]["schemas"]
        assert set(schemas["ContentType"]["enum"]) == {
            "video", "paper", "article", "audio",
        }
        assert "book_toc" in schemas["SubscriptionSourceType"]["enum"]

    @pytest.mark.asyncio
    async def test_unsupported_upload_extension_rejected(self, client, mock_redis):
        resp = await client.post(
            "/api/jobs/upload",
            files={"file": ("payload.zip", b"not-media", "application/zip")},
        )
        assert resp.status_code == 422
        mock_redis.publish.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_direct_file_url_is_not_a_publicly_creatable_source(self, client, mock_redis):
        resp = await client.post(
            "/api/jobs",
            json={"url": "file:///data/private.txt", "content_type": "article"},
        )
        assert resp.status_code == 422
        mock_redis.publish.assert_not_awaited()

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

    @pytest.mark.asyncio
    async def test_delete_purges_artifacts(self, client, app):
        # 删 job 必须经 storage 清产物(本地删目录 / MinIO 删 {job_id}/ 前缀),否则对象存储留孤儿。
        create_resp = await client.post("/api/jobs", json={"url": "BV1xx411c7mD"})
        job_id = create_resp.json()["job_id"]
        storage = app.state.storage
        await storage.write_file(job_id, "output/notes.md", b"note")
        assert await storage.read_file(job_id, "output/notes.md") == b"note"
        resp = await client.delete(f"/api/jobs/{job_id}")
        assert resp.status_code == 204
        assert await storage.read_file(job_id, "output/notes.md") is None  # 产物已清

    @pytest.mark.asyncio
    async def test_delete_clears_ai_usage_and_calls_queue_cleanup(self, client, app, db, mock_redis):
        from shared.models import AIUsage
        create_resp = await client.post("/api/jobs", json={"url": "BV1xx411c7mD"})
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
        db.create_job(Job(id="j2", content_type="paper", pipeline="paper", domain="ml", source="arxiv"))
        db.create_job(Job(id="j3", content_type="video", pipeline="video", domain="finance", source="bilibili"))
        assert (await client.get("/api/jobs?domain=finance")).json()["total"] == 2
        assert (await client.get("/api/jobs?source=arxiv")).json()["total"] == 1
        assert (await client.get("/api/jobs?domain=finance&source=bilibili")).json()["total"] == 2

    @pytest.mark.asyncio
    async def test_facets(self, client, app):
        db = app.state.db
        db.create_job(Job(id="j1", content_type="video", pipeline="video", domain="finance", source="bilibili"))
        db.create_job(Job(id="j2", content_type="paper", pipeline="paper", domain="ml", source="arxiv"))
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
            json={"from_step": "11_smart"},
        )
        assert resp.status_code == 200
        assert resp.json()["from_step"] == "11_smart"
        # 真实副作用:job 以 rerun 命令被重新派发(而非只回显入参)。最后一次 publish 是 rerun。
        ch, payload = mock_redis.publish.call_args[0]
        assert ch == "job_command"
        assert payload["action"] == "rerun" and payload["job_id"] == job_id
        assert payload["from_step"] == "11_smart"

    @pytest.mark.asyncio
    async def test_resubmit(self, client, mock_redis):
        create_resp = await client.post("/api/jobs", json={"url": "BV1xx411c7mD"})
        job_id = create_resp.json()["job_id"]
        resp = await client.post(f"/api/jobs/{job_id}/resubmit")
        assert resp.status_code == 200
        # 真实副作用:job 以 resubmit 命令被重新派发。
        ch, payload = mock_redis.publish.call_args[0]
        assert ch == "job_command" and payload == {"action": "resubmit", "job_id": job_id}

    @pytest.mark.asyncio
    async def test_retry_all_failed(self, client, mock_redis, db):
        from shared.models import JobStatus
        ids = []
        for u in ("BV1xx411c7mD", "BV1yy422d8nE"):
            jid = (await client.post("/api/jobs", json={"url": u})).json()["job_id"]
            db.update_job(jid, status=JobStatus.FAILED)
            ids.append(jid)
        resp = await client.post("/api/jobs/retry-failed")
        assert resp.status_code == 200
        assert resp.json()["retried"] >= 2
        retried = {c[0][1]["job_id"] for c in mock_redis.publish.call_args_list
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
        retried = {c[0][1]["job_id"] for c in mock_redis.publish.call_args_list
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
        assert doc["ai_overrides"]["11_smart"] == "claude-cli"
        assert doc["ai_overrides"]["12_review"] == "claude-cli"


class TestRebuildP2c:
    """/rebuild 建新快照(fork:clone 产物+.done、父降级、新版 current);/rebuild-stale 只挑过期。"""

    @pytest.mark.asyncio
    async def test_rebuild_creates_snapshot(self, client, app):
        db, storage, config = app.state.db, app.state.storage, app.state.config
        first = config.pipelines["paper"]["steps"][0]["name"]
        db.create_job(Job(id="jobs_paper_p1", content_type="paper", pipeline="paper",
                          url="https://arxiv.org/abs/1810.04805", source="arxiv",
                          lineage_key="jobs_paper_p1"))
        await storage.write_file("jobs_paper_p1", "job.json", b'{"id":"jobs_paper_p1"}')
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

    @pytest.mark.asyncio
    async def test_rebuild_stale_only_expired(self, client, app):
        db, storage, config = app.state.db, app.state.storage, app.state.config
        first = config.pipelines["paper"]["steps"][0]["name"]
        db.create_job(Job(id="jobs_paper_stale", content_type="paper", pipeline="paper",
                          source="arxiv", lineage_key="jobs_paper_stale"))
        await storage.write_file("jobs_paper_stale", "job.json", b"{}")
        await storage.write_file("jobs_paper_stale", f".{first}.done", b'{"def_digest":"sha256:STALE"}')
        db.create_job(Job(id="jobs_paper_fresh", content_type="paper", pipeline="paper",
                          source="arxiv", lineage_key="jobs_paper_fresh"))  # 无 .done → 不过期
        resp = await client.post("/api/jobs/rebuild-stale")
        assert resp.status_code == 200
        body = resp.json()
        parents = [it["parent_job_id"] for it in body["items"]]
        assert "jobs_paper_stale" in parents
        assert "jobs_paper_fresh" not in parents
