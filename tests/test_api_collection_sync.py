"""订阅 = 集合属性：集合层的订阅创建 / 去重 / 同步 / 自动追更开关。
（订阅并入 collections 后，原 test_api_subscriptions.py 的用例迁移到这里。）"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from shared.config import load_config
from shared.db import Database
from shared.models import Collection, Job
from api.main import create_app


@pytest.fixture
def test_config(tmp_path, configs_dir):
    cfg = load_config(config_dir=configs_dir, data_dir=tmp_path)
    cfg.jobs_dir = tmp_path / "jobs"; cfg.jobs_dir.mkdir()
    cfg.prompts_dir = tmp_path / "prompts"; cfg.prompts_dir.mkdir()
    return cfg


@pytest.fixture
def db(test_config):
    d = Database(test_config.db_path); d.init_schema()
    yield d; d.close()


@pytest.fixture
def app(db, test_config):
    return create_app(db=db, redis=AsyncMock(), config=test_config)


@pytest.fixture
async def client(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


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
        db.create_job(Job(id="j2", content_type="article", pipeline="article",
                          url="https://example.com/x"))
        assert db.ingested_bvids() == {"BV1hT7k6JEq7"}


class TestSubscriptionCollectionAPI:
    @pytest.mark.asyncio
    async def test_create_syncs_and_dedups(self, client, app, monkeypatch):
        app.state.db.create_job(Job(id="j0", content_type="video", pipeline="video",
                                    url="https://www.bilibili.com/video/BV1old000000"))

        async def fake_enum(mid, cookies=None):
            return [
                {"bvid": "BV1old000000", "title": "已入库", "duration": "1:00"},
                {"bvid": "BV1new111111", "title": "新1", "duration": "2:00"},
                {"bvid": "BV1new222222", "title": "新2", "duration": "3:00"},
            ]
        monkeypatch.setattr("shared.bili_space.enumerate_up", fake_enum)

        resp = await client.post("/api/collections", json={
            "name": "财经说", "domain": "finance",
            "source_type": "bilibili_up", "source_id": "247209804", "sync_now": True,
        })
        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert data["id"] == "col_bili_up_247209804"
        assert data["subscription"]["source_id"] == "247209804"
        assert data["subscription"]["enabled"] is True
        # 新视频已建 job 并归入本集合（跳过已入库 1 个 → 2 个新）
        jobs = (await client.get(f"/api/collections/{data['id']}/jobs")).json()
        assert jobs["total"] == 2

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
        body = {"name": "x", "domain": "finance", "source_type": "bilibili_up",
                "source_id": "999", "sync_now": False}
        assert (await client.post("/api/collections", json=body)).status_code == 201
        assert (await client.post("/api/collections", json=body)).status_code == 400

    @pytest.mark.asyncio
    async def test_sync_endpoint_and_toggle(self, client, monkeypatch):
        async def fake_enum(mid, cookies=None):
            return [{"bvid": "BV1aaaaaaaaa", "title": "x", "duration": "1:00"}]
        monkeypatch.setattr("shared.bili_space.enumerate_up", fake_enum)
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
