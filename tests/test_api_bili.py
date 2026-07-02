"""api/routes/bili.py 测试:B站扫码登录 + create_job 的 sessdata 注入。"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from api.main import create_app


@pytest.fixture
def mock_redis():
    r = AsyncMock()
    r.publish = AsyncMock()
    return r


@pytest.fixture
def app(db, mock_redis, test_config):
    return create_app(db=db, redis=mock_redis, config=test_config)


def _fake_client_returning(resp):
    """构造一个可当 async context manager 用的假 httpx.AsyncClient,get 永远返回 resp。"""
    client = MagicMock()
    client.get = AsyncMock(return_value=resp)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=ctx)
    return factory, client


def _resp(json_data, cookies=None):
    """假响应:暴露 .json() 与 .cookies(cookies 支持 .get(key))。"""
    r = MagicMock()
    r.json = MagicMock(return_value=json_data)
    cookies_obj = MagicMock()
    cookies_obj.get = MagicMock(side_effect=lambda k, d=None: (cookies or {}).get(k, d))
    r.cookies = cookies_obj
    return r


class TestLoginStart:
    @pytest.mark.asyncio
    async def test_start_returns_key_and_qr(self, client, monkeypatch):
        import httpx

        resp = _resp({
            "code": 0,
            "data": {"url": "https://passport.bilibili.com/h5/x", "qrcode_key": "KEY123"},
        })
        factory, _ = _fake_client_returning(resp)
        monkeypatch.setattr(httpx, "AsyncClient", factory)

        r = await client.post("/api/bili/login/start")
        assert r.status_code == 200
        body = r.json()
        assert body["qrcode_key"] == "KEY123"
        assert body["url"] == "https://passport.bilibili.com/h5/x"
        assert body["qr_png"].startswith("data:image/png;base64,")
        # 不止前缀:base64 解出来必须是能真正解码的 PNG(防拼了个坏串也"看着像")。
        import base64, io
        from PIL import Image
        png = base64.b64decode(body["qr_png"].split(",", 1)[1])
        assert Image.open(io.BytesIO(png)).format == "PNG"

    @pytest.mark.asyncio
    async def test_start_passport_error(self, client, monkeypatch):
        import httpx

        resp = _resp({"code": -412, "data": {}})
        factory, _ = _fake_client_returning(resp)
        monkeypatch.setattr(httpx, "AsyncClient", factory)

        r = await client.post("/api/bili/login/start")
        assert r.status_code == 502


class TestLoginPoll:
    @pytest.mark.asyncio
    async def test_poll_waiting(self, client, monkeypatch):
        import httpx

        resp = _resp({"code": 0, "data": {"code": 86101}})
        factory, _ = _fake_client_returning(resp)
        monkeypatch.setattr(httpx, "AsyncClient", factory)

        r = await client.get("/api/bili/login/poll", params={"qrcode_key": "K"})
        assert r.status_code == 200
        body = r.json()
        assert body["state"] == "waiting"
        assert body["logged_in"] is False
        assert body["uname"] is None

    @pytest.mark.asyncio
    async def test_poll_scanned(self, client, monkeypatch):
        import httpx

        resp = _resp({"code": 0, "data": {"code": 86090}})
        factory, _ = _fake_client_returning(resp)
        monkeypatch.setattr(httpx, "AsyncClient", factory)

        r = await client.get("/api/bili/login/poll", params={"qrcode_key": "K"})
        assert r.json()["state"] == "scanned"

    @pytest.mark.asyncio
    async def test_poll_expired(self, client, monkeypatch):
        import httpx

        resp = _resp({"code": 0, "data": {"code": 86038}})
        factory, _ = _fake_client_returning(resp)
        monkeypatch.setattr(httpx, "AsyncClient", factory)

        r = await client.get("/api/bili/login/poll", params={"qrcode_key": "K"})
        assert r.json()["state"] == "expired"

    @pytest.mark.asyncio
    async def test_poll_confirmed_stores_cookie(self, client, db, monkeypatch):
        import httpx
        import api.routes.bili as bili

        resp = _resp(
            {"code": 0, "data": {"code": 0}},
            cookies={"SESSDATA": "SD123", "bili_jct": "JCT", "DedeUserID": "42"},
        )
        factory, _ = _fake_client_returning(resp)
        monkeypatch.setattr(httpx, "AsyncClient", factory)
        # nav 取昵称走独立路径,直接 mock 掉避免真连。
        monkeypatch.setattr(bili, "_fetch_uname", AsyncMock(return_value="张三"))

        r = await client.get("/api/bili/login/poll", params={"qrcode_key": "K"})
        body = r.json()
        assert body["state"] == "confirmed"
        assert body["logged_in"] is True
        assert body["uname"] == "张三"

        # cookie 三件套 + uname 入库为 JSON。
        stored = json.loads(db.get_credential("bili_cookies"))
        assert stored["sessdata"] == "SD123"
        assert stored["bili_jct"] == "JCT"
        assert stored["dedeuserid"] == "42"
        assert stored["uname"] == "张三"


class TestStatusLogout:
    @pytest.mark.asyncio
    async def test_status_logged_out(self, client):
        r = await client.get("/api/bili/status")
        assert r.json() == {"logged_in": False, "uname": None}

    @pytest.mark.asyncio
    async def test_status_logged_in(self, client, db):
        db.set_credential(
            "bili_cookies", json.dumps({"sessdata": "SD", "uname": "李四"})
        )
        r = await client.get("/api/bili/status")
        assert r.json() == {"logged_in": True, "uname": "李四"}

    @pytest.mark.asyncio
    async def test_logout_clears(self, client, db):
        db.set_credential("bili_cookies", json.dumps({"sessdata": "SD"}))
        r = await client.post("/api/bili/logout")
        assert r.json() == {"ok": True}
        assert db.get_credential("bili_cookies") is None
        # 登出后 status 反映未登录。
        s = await client.get("/api/bili/status")
        assert s.json()["logged_in"] is False


class TestCreateJobSessdataInjection:
    @pytest.mark.asyncio
    async def test_bilibili_job_injects_sessdata(self, client, db, test_config):
        """已登录时,SESSDATA 写入本机侧载凭证文件(不进 job.json),供下载步本地读取。"""
        db.set_credential(
            "bili_cookies", json.dumps({"sessdata": "INJECTED_SD", "uname": "u"})
        )
        r = await client.post(
            "/api/jobs", json={"url": "https://www.bilibili.com/video/BV1xx411c7mD"}
        )
        assert r.status_code == 201
        job_id = r.json()["job_id"]
        job_doc = json.loads((test_config.jobs_dir / job_id / "job.json").read_text())
        assert job_doc["source"] == "bilibili"
        assert "sessdata" not in job_doc          # 通用 job.json 会下发远端,凭证不能进
        cred = test_config.jobs_dir / job_id / "input" / ".credentials.json"
        assert json.loads(cred.read_text())["sessdata"] == "INJECTED_SD"

    @pytest.mark.asyncio
    async def test_bilibili_job_no_cookie_no_sessdata(self, client, db, test_config):
        """未登录时不写凭证文件,保持匿名下载现状。"""
        r = await client.post(
            "/api/jobs", json={"url": "https://www.bilibili.com/video/BV1xx411c7mD"}
        )
        assert r.status_code == 201
        job_id = r.json()["job_id"]
        job_doc = json.loads((test_config.jobs_dir / job_id / "job.json").read_text())
        assert "sessdata" not in job_doc
        assert not (test_config.jobs_dir / job_id / "input" / ".credentials.json").exists()

    @pytest.mark.asyncio
    async def test_non_bilibili_job_no_sessdata(self, client, db, test_config):
        """非 B站源即便有 cookie 也不注入 sessdata。"""
        db.set_credential("bili_cookies", json.dumps({"sessdata": "SD"}))
        r = await client.post(
            "/api/jobs", json={"url": "https://www.youtube.com/watch?v=abc"}
        )
        assert r.status_code == 201
        job_id = r.json()["job_id"]
        job_doc = json.loads((test_config.jobs_dir / job_id / "job.json").read_text())
        assert "sessdata" not in job_doc
        assert not (test_config.jobs_dir / job_id / "input" / ".credentials.json").exists()
