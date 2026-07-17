"""订阅 = 集合属性:集合层的订阅创建 / 去重 / 同步 / 自动追更开关。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from shared.models import Collection, Job
from shared.subscriptions.base import SourceItem


class TestSubscriptionCollectionDB:
    def test_source_crud(self, db):
        c = Collection(id="col_bili_up_123", name="UP", domain="finance",
                       source_type="bilibili_up", source_id="123")
        db.create_collection(c)
        got = db.get_collection("col_bili_up_123")
        assert got.is_subscription and got.source_id == "123" and got.sync_enabled
        assert db.find_collection_by_source("bilibili_up", "123").id == "col_bili_up_123"
        assert len(db.list_subscription_collections()) == 1
        assert len(db.list_subscription_collections(enabled_only=True)) == 1
        db.update_collection("col_bili_up_123", sync_enabled=False)
        assert db.get_collection("col_bili_up_123").sync_enabled is False
        assert len(db.list_subscription_collections(enabled_only=True)) == 0
        db.mark_collection_synced("col_bili_up_123", datetime.now(timezone.utc))
        assert db.get_collection("col_bili_up_123").last_synced_at is not None

    def test_manual_collection_not_subscription(self, db):
        db.create_collection(Collection(id="col_abc", name="手动", domain="finance"))
        c = db.get_collection("col_abc")
        assert not c.is_subscription and c.source_type is None

    def test_ingested_bvids(self, db):
        db.create_job(Job(id="j1", content_type="video", pipeline="video",
                          url="https://www.bilibili.com/video/BV1hT7k6JEq7"))
        db.create_job(Job(id="j2", content_type="document", document_kind="article",
                          pipeline="document",
                          url="https://example.com/x"))
        assert db.ingested_bvids() == {"BV1hT7k6JEq7"}


class TestSubscriptionCollectionAPI:
    @pytest.mark.asyncio
    async def test_playlist_sync_reuses_lineage_preserves_order_and_is_idempotent(
        self, client, app, monkeypatch,
    ):
        from shared.ids import lineage_key

        urls = [
            "https://www.youtube.com/watch?v=aaaaaaaaaaa",
            "https://www.youtube.com/watch?v=bbbbbbbbbbb",
            "https://www.youtube.com/watch?v=ccccccccccc",
            "https://www.youtube.com/watch?v=ddddddddddd",
        ]
        playlist_items = [
            SourceItem("aaaaaaaaaaa", "第一课", urls[0], "video"),
            SourceItem("bbbbbbbbbbb", "第二课", urls[1], "video"),
            SourceItem("ccccccccccc", "第三课", urls[2], "video"),
            SourceItem("aaaaaaaaaaa", "重复的第一课", urls[0], "video"),
        ]
        app.state.db.create_job(Job(
            id="jobs_yt_bbbbbbbbbbb_legacy",
            content_type="video",
            pipeline="video",
            domain="deep-learning",
            url=urls[1],
            title="第二课旧任务",
            lineage_key=lineage_key(urls[1], "video", "youtube"),
        ))

        async def fake_enumerate(_source_type, _source_id, _ctx):
            return "CS336", list(playlist_items)

        monkeypatch.setattr("shared.subscriptions.enumerate_source", fake_enumerate)
        created = await client.post("/api/collections", json={
            "name": "CS336",
            "domain": "deep-learning",
            "source_type": "youtube_playlist",
            "source_id": "PLabc_123-xyz",
            "sync_now": False,
        })
        assert created.status_code == 201, created.text
        collection_id = created.json()["id"]

        first = await client.post(f"/api/collections/{collection_id}/sync")
        assert first.status_code == 200, first.text
        assert first.json()["total"] == 3
        assert first.json()["new"] == 3
        assert first.json()["reused"] == 1
        assert app.state.db.get_job("jobs_yt_bbbbbbbbbbb_legacy").collection_id == collection_id

        listed = (await client.get(
            f"/api/collections/{collection_id}/jobs?limit=20"
        )).json()
        assert listed["total"] == 3
        assert [item["title"] for item in listed["items"]] == [
            "第一课", "第二课旧任务", "第三课",
        ]

        again = await client.post(f"/api/collections/{collection_id}/sync")
        assert again.status_code == 200
        assert again.json()["new"] == 0
        assert again.json()["reused"] == 0
        assert (await client.get(
            f"/api/collections/{collection_id}/jobs?limit=20"
        )).json()["total"] == 3

        playlist_items[:] = [
            SourceItem("ccccccccccc", "第三课", urls[2], "video"),
            SourceItem("aaaaaaaaaaa", "第一课", urls[0], "video"),
            SourceItem("ddddddddddd", "第四课", urls[3], "video"),
        ]
        changed = await client.post(f"/api/collections/{collection_id}/sync")
        assert changed.status_code == 200, changed.text
        assert changed.json() == {
            "total": 3, "new": 1, "reused": 0, "skipped": 2, "failed": 0,
        }
        changed_jobs = (await client.get(
            f"/api/collections/{collection_id}/jobs?limit=20"
        )).json()
        assert changed_jobs["total"] == 4
        assert [item["title"] for item in changed_jobs["items"]] == [
            "第三课", "第一课", "第四课", "第二课旧任务",
        ]
        legacy = app.state.db.get_job("jobs_yt_bbbbbbbbbbb_legacy")
        assert legacy.meta["source_present"] is False

    @pytest.mark.asyncio
    async def test_create_syncs_and_dedups(self, client, app, monkeypatch):
        from shared.ids import lineage_key

        old_url = "https://www.bilibili.com/video/BV1old000000"
        app.state.db.create_job(Job(
            id="j0", content_type="video", pipeline="video", url=old_url,
            domain="finance",
            lineage_key=lineage_key(old_url, "video", "bilibili"),
        ))

        async def fake_enum(mid, cookies=None):
            return [
                {"bvid": "BV1old000000", "title": "已入库", "duration": "1:00"},
                {"bvid": "BV1new111111", "title": "新1", "duration": "2:00"},
                {"bvid": "BV1new222222", "title": "新2", "duration": "3:00"},
            ]
        monkeypatch.setattr("shared.bili_space.enumerate_up", fake_enum)
        async def fake_up_name(mid, cookies=None): return None   # 不打真网络(get_user_info)
        monkeypatch.setattr("shared.bili_space.up_name", fake_up_name)

        resp = await client.post("/api/collections", json={
            "name": "财经说", "domain": "finance",
            "source_type": "bilibili_up", "source_id": "247209804", "sync_now": True,
        })
        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert data["id"] == "col_bili_up_247209804"
        assert data["subscription"]["source_id"] == "247209804"
        assert data["subscription"]["enabled"] is True
        # 历史未分类 lineage 被复用归集,另两个新视频建 job。
        jobs = (await client.get(f"/api/collections/{data['id']}/jobs")).json()
        assert jobs["total"] == 3
        assert app.state.db.get_job("j0").collection_id == data["id"]

    @pytest.mark.asyncio
    async def test_subscription_requires_real_domain(self, client):
        resp = await client.post("/api/collections", json={
            "name": "x", "domain": "general",
            "source_type": "bilibili_up", "source_id": "111", "sync_now": False,
        })
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_duplicate_source_rejected(self, client, monkeypatch):
        async def fake_enum(mid, cookies=None):
            return []
        monkeypatch.setattr("shared.bili_space.enumerate_up", fake_enum)
        async def fake_up_name(mid, cookies=None): return None   # 不打真网络(get_user_info)
        monkeypatch.setattr("shared.bili_space.up_name", fake_up_name)
        body = {"name": "x", "domain": "finance", "source_type": "bilibili_up",
                "source_id": "999", "sync_now": False}
        assert (await client.post("/api/collections", json=body)).status_code == 201
        assert (await client.post("/api/collections", json=body)).status_code == 400

    @pytest.mark.asyncio
    async def test_sync_endpoint_and_toggle(self, client, monkeypatch):
        async def fake_enum(mid, cookies=None):
            return [{"bvid": "BV1aaaaaaaaa", "title": "x", "duration": "1:00"}]
        monkeypatch.setattr("shared.bili_space.enumerate_up", fake_enum)
        async def fake_up_name(mid, cookies=None): return None   # 不打真网络(get_user_info)
        monkeypatch.setattr("shared.bili_space.up_name", fake_up_name)
        cid = (await client.post("/api/collections", json={
            "name": "x", "domain": "finance", "source_type": "bilibili_up",
            "source_id": "555", "sync_now": False,
        })).json()["id"]
        # 立即同步
        r = await client.post(f"/api/collections/{cid}/sync")
        assert r.status_code == 200 and r.json()["new"] == 1
        # 关闭自动追更
        r2 = await client.put(f"/api/collections/{cid}", json={"sync_enabled": False})
        assert r2.status_code == 200 and r2.json()["subscription"]["enabled"] is False

    @pytest.mark.asyncio
    async def test_sync_on_manual_collection_rejected(self, client):
        cid = (await client.post("/api/collections", json={
            "name": "手动", "domain": "finance",
        })).json()["id"]
        assert (await client.post(f"/api/collections/{cid}/sync")).status_code == 400

    @pytest.mark.asyncio
    async def test_sync_isolates_failed_item(self, client, monkeypatch):
        """故障隔离:单条建 job 失败不阻断整轮;失败项不 mark_ingested,下轮重试。"""
        async def fake_enum(mid, cookies=None):
            return [
                {"bvid": "BV1good00001", "title": "好1", "duration": "1:00"},
                {"bvid": "BV1bad000000", "title": "坏", "duration": "2:00"},
                {"bvid": "BV1good00002", "title": "好2", "duration": "3:00"},
            ]
        monkeypatch.setattr("shared.bili_space.enumerate_up", fake_enum)
        async def fake_up_name(mid, cookies=None): return None
        monkeypatch.setattr("shared.bili_space.up_name", fake_up_name)

        import api.routes.jobs as jobs_mod
        real_create = jobs_mod.create_job_core
        async def flaky_create(db, redis, storage, *, url, **kw):
            if "BV1bad000000" in url:
                raise RuntimeError("boom")
            return await real_create(db, redis, storage, url=url, **kw)
        monkeypatch.setattr(jobs_mod, "create_job_core", flaky_create)

        cid = (await client.post("/api/collections", json={
            "name": "x", "domain": "finance", "source_type": "bilibili_up",
            "source_id": "777", "sync_now": False,
        })).json()["id"]

        r = await client.post(f"/api/collections/{cid}/sync")
        assert r.status_code == 200, r.text       # 单条失败没把整轮翻成 500
        assert r.json()["new"] == 2               # 两个好的入库,坏的跳过

        # 坏项未 mark_ingested:下轮(此次放行)应作为 new 重新建 job(重试可续)。
        monkeypatch.setattr(jobs_mod, "create_job_core", real_create)
        r2 = await client.post(f"/api/collections/{cid}/sync")
        assert r2.status_code == 200 and r2.json()["new"] == 1

    @pytest.mark.asyncio
    async def test_sync_success_records_status_ok(self, client, monkeypatch):
        """同步成功后 last_sync_status=ok、错误清空。"""
        async def fake_enum(mid, cookies=None):
            return [{"bvid": "BV1aaaaaaaaa", "title": "x", "duration": "1:00"}]
        monkeypatch.setattr("shared.bili_space.enumerate_up", fake_enum)
        async def fake_up_name(mid, cookies=None): return None
        monkeypatch.setattr("shared.bili_space.up_name", fake_up_name)
        cid = (await client.post("/api/collections", json={
            "name": "x", "domain": "finance", "source_type": "bilibili_up",
            "source_id": "5551", "sync_now": False,
        })).json()["id"]
        assert (await client.post(f"/api/collections/{cid}/sync")).status_code == 200
        sub = (await client.get(f"/api/collections/{cid}")).json()["subscription"]
        assert sub["last_sync_status"] == "ok"
        assert sub["last_sync_error"] is None

    @pytest.mark.asyncio
    async def test_sync_failure_records_status_error(self, client, monkeypatch):
        """同步异常 → 502 且 last_sync_status=error + 存错误摘要(不掩盖失败)。"""
        async def fake_up_name(mid, cookies=None): return None
        monkeypatch.setattr("shared.bili_space.up_name", fake_up_name)
        async def boom_enum(mid, cookies=None):
            raise RuntimeError("enumerate failed: net down")
        monkeypatch.setattr("shared.bili_space.enumerate_up", boom_enum)
        cid = (await client.post("/api/collections", json={
            "name": "x", "domain": "finance", "source_type": "bilibili_up",
            "source_id": "5552", "sync_now": False,   # 创建不触发枚举,故 boom 此刻不影响建集合
        })).json()["id"]
        assert (await client.post(f"/api/collections/{cid}/sync")).status_code == 502
        sub = (await client.get(f"/api/collections/{cid}")).json()["subscription"]
        assert sub["last_sync_status"] == "error"
        assert "enumerate failed" in sub["last_sync_error"]
