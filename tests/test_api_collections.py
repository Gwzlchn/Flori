"""api/routes/collections.py 测试。"""

from __future__ import annotations

import pytest

from api.main import create_app


@pytest.fixture
def mock_redis():
    from tests.conftest import make_redis_mock

    r = make_redis_mock()
    r.get_all_step_statuses.return_value = {}
    return r


@pytest.fixture
def app(db, mock_redis, test_config):
    return create_app(db=db, redis=mock_redis, config=test_config)


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
        assert data["id"].startswith("col_")
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

    @pytest.mark.asyncio
    async def test_book_toc_is_creatable_and_exposed(self, client):
        resp = await client.post("/api/collections", json={
            "domain": "learning",
            "source_type": "book_toc",
            "source_id": "https://book.example.com/index.html",
            "sync_now": False,
        })
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["id"].startswith("col_book_")
        assert body["subscription"]["source_type"] == "book_toc"
        assert body["subscription"]["source_label"] == "book"

    @pytest.mark.asyncio
    async def test_partial_subscription_source_rejected(self, client):
        resp = await client.post("/api/collections", json={
            "name": "bad", "domain": "learning", "source_type": "rss",
        })
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_registered_source_without_adapter_rejected_before_write(
        self, client, app, monkeypatch,
    ):
        from shared.subscriptions import SOURCE_ADAPTERS

        monkeypatch.delitem(SOURCE_ADAPTERS, "book_toc")
        resp = await client.post("/api/collections", json={
            "domain": "learning",
            "source_type": "book_toc",
            "source_id": "https://book.example.com/index.html",
            "sync_now": False,
        })
        assert resp.status_code == 422
        assert app.state.db.list_collections() == []


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


class TestSubscriptionSyncStatus:
    """订阅集合响应携带 last_sync_status / last_sync_error。"""

    @pytest.mark.asyncio
    async def test_subscription_carries_sync_status(self, client, app):
        from shared.models import Collection
        app.state.db.create_collection(Collection(
            id="col_bili_up_1", name="UP", domain="finance",
            source_type="bilibili_up", source_id="123",
            last_sync_status="error", last_sync_error="boom: net down",
        ))
        sub = (await client.get("/api/collections/col_bili_up_1")).json()["subscription"]
        assert sub is not None
        assert sub["last_sync_status"] == "error"
        assert sub["last_sync_error"] == "boom: net down"

    @pytest.mark.asyncio
    async def test_never_synced_status_none(self, client, app):
        from shared.models import Collection
        app.state.db.create_collection(Collection(
            id="col_bili_up_2", name="UP2", domain="finance",
            source_type="bilibili_up", source_id="456",
        ))
        sub = (await client.get("/api/collections/col_bili_up_2")).json()["subscription"]
        assert sub["last_sync_status"] is None
        assert sub["last_sync_error"] is None

    @pytest.mark.asyncio
    async def test_manual_collection_no_subscription(self, client):
        cid = (await _create(client))["id"]
        body = (await client.get(f"/api/collections/{cid}")).json()
        assert body["subscription"] is None


class TestCollectionStatusCounts:
    """详情端点返回 status_counts(本集合各状态计数,缺省补 0);列表端点为 None。"""

    @pytest.mark.asyncio
    async def test_detail_includes_status_counts(self, client, app):
        from shared.models import Collection, Job, JobStatus
        db = app.state.db
        db.create_collection(Collection(id="c_sc", name="X", domain="general"))
        for jid, st in [("s1", JobStatus.DONE), ("s2", JobStatus.FAILED),
                        ("s3", JobStatus.FAILED)]:
            db.create_job(Job(id=jid, content_type="video", pipeline="video",
                              collection_id="c_sc", status=st))
        sc = (await client.get("/api/collections/c_sc")).json()["status_counts"]
        assert sc["done"] == 1
        assert sc["failed"] == 2
        assert sc["processing"] == 0   # 缺省补 0
        assert sc["pending"] == 0

    @pytest.mark.asyncio
    async def test_list_endpoint_omits_status_counts(self, client):
        await _create(client)
        items = (await client.get("/api/collections")).json()
        assert items[0]["status_counts"] is None   # 仅详情端点填,列表不查


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
        # None 字段不动:description/tags 保留。
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
        """删集合=解绑:job 保留但 collection_id 置 NULL。"""
        cid = (await _create(client))["id"]
        job_resp = await client.post(
            "/api/jobs", json={"url": "BV1xx411c7mD", "collection_id": cid},
        )
        job_id = job_resp.json()["job_id"]

        del_resp = await client.delete(f"/api/collections/{cid}")
        assert del_resp.status_code == 204

        # job 仍存在(未被级联删除)。
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
        # 3 个不同 url = 3 个不同 lineage:同 url 会归一组只显 current。
        for bv in ("BV1xx411c7mD", "BV2yy422d8nE", "BV3zz533e9oF"):
            await client.post(
                "/api/jobs", json={"url": bv, "collection_id": cid},
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
