"""Job intake 在请求体和持久化之前执行安全门。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from api.main import create_app


def _video_request(url: str = "BV1xx411c7mD") -> dict:
    return {"content_type": "video", "parts": [{"url": url}]}


def _live_workers() -> dict[str, dict[str, str]]:
    heartbeat = datetime.now(timezone.utc).isoformat()
    return {
        "io": {
            "pools": "io", "tags": "net-cn,net-global", "reject_tags": "",
            "status": "idle", "admin_status": "active", "last_heartbeat": heartbeat,
        },
        "cpu": {
            "pools": "cpu", "tags": "", "reject_tags": "",
            "status": "idle", "admin_status": "active", "last_heartbeat": heartbeat,
        },
        "ai": {
            "pools": "ai", "tags": "claude-cli,vision,read", "reject_tags": "",
            "status": "idle", "admin_status": "active", "last_heartbeat": heartbeat,
        },
    }


def _redis_for_workers(workers: dict[str, dict[str, str]]) -> AsyncMock:
    redis = AsyncMock()
    redis.consume_rate_limit.return_value = (True, 1, 60)
    redis.list_worker_ids.return_value = list(workers)
    redis.get_worker_info.side_effect = workers.get
    redis.get_all_step_statuses.return_value = {}
    return redis


async def _asgi_post_without_body(
    app, path: str, query: str = "", headers: list[tuple[bytes, bytes]] | None = None,
) -> tuple[int, dict, bytes, int]:
    messages: list[dict] = []
    receive_calls = 0

    async def receive():
        nonlocal receive_calls
        receive_calls += 1
        raise AssertionError("guard must reject before reading request body")

    async def send(message):
        messages.append(message)

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "POST", "scheme": "http", "path": path,
        "raw_path": path.encode(), "query_string": query.encode(),
        "headers": [
            (b"content-type", b"multipart/form-data; boundary=flori"),
            *(headers or []),
        ],
        "client": ("10.1.2.3", 43210), "server": ("test", 80), "root_path": "",
    }
    await app(scope, receive, send)
    start = next(item for item in messages if item["type"] == "http.response.start")
    body = b"".join(
        item.get("body", b"") for item in messages if item["type"] == "http.response.body"
    )
    return start["status"], dict(start["headers"]), body, receive_calls


@pytest.mark.asyncio
async def test_removed_video_upload_rejects_without_reading_body(db, test_config):
    app = create_app(db=db, redis=_redis_for_workers({}), config=test_config)

    status, _headers, body, receive_calls = await _asgi_post_without_body(
        app, "/api/jobs/upload", "content_type=video",
    )

    assert status == 422
    assert json.loads(body)["error"] == "invalid_request"
    assert receive_calls == 0
    assert db.list_jobs(limit=1)[0] == 0
    assert list(test_config.jobs_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_upload_rate_limit_rejects_without_reading_body(db, test_config):
    redis = _redis_for_workers(_live_workers())
    redis.consume_rate_limit.return_value = (False, 31, 17)
    app = create_app(db=db, redis=redis, config=test_config)

    status, headers, body, receive_calls = await _asgi_post_without_body(
        app, "/api/jobs/upload", "content_type=video",
    )

    assert status == 429
    assert json.loads(body)["error"] == "rate_limited"
    assert headers[b"retry-after"] == b"17"
    assert receive_calls == 0


@pytest.mark.asyncio
async def test_upload_missing_content_type_rejects_without_reading_body(db, test_config):
    app = create_app(db=db, redis=_redis_for_workers(_live_workers()), config=test_config)

    status, _headers, body, receive_calls = await _asgi_post_without_body(
        app, "/api/jobs/upload",
    )

    assert status == 422
    assert json.loads(body)["error"] == "invalid_request"
    assert receive_calls == 0


@pytest.mark.asyncio
async def test_verified_token_principal_ignores_forwarded_for(
    db, test_config, monkeypatch,
):
    monkeypatch.setenv("API_TOKEN", "real-api-token")
    monkeypatch.delenv("API_ALLOW_NO_AUTH", raising=False)
    redis = _redis_for_workers(_live_workers())
    app = create_app(db=db, redis=redis, config=test_config)

    from httpx import ASGITransport, AsyncClient

    transport = ASGITransport(app=app)
    headers = {"Authorization": "Bearer real-api-token"}
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post(
            "/api/jobs", json=_video_request("BV1xx411c7mD"),
            headers={**headers, "X-Forwarded-For": "198.51.100.1"},
        )
        second = await client.post(
            "/api/jobs", json=_video_request("BV1xx411c7mE"),
            headers={**headers, "X-Forwarded-For": "203.0.113.9"},
        )

    assert first.status_code == second.status_code == 201
    principals = [call.args[1] for call in redis.consume_rate_limit.await_args_list]
    assert principals[0] == principals[1]
    assert "real-api-token" not in principals[0]


@pytest.mark.asyncio
async def test_invalid_token_does_not_create_rate_limit_key(
    db, test_config, monkeypatch,
):
    monkeypatch.setenv("API_TOKEN", "real-api-token")
    redis = _redis_for_workers(_live_workers())
    app = create_app(db=db, redis=redis, config=test_config)

    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/jobs", json=_video_request(),
            headers={"Authorization": "Bearer invalid"},
        )

    assert response.status_code == 401
    redis.consume_rate_limit.assert_not_awaited()


@pytest.mark.asyncio
async def test_rate_limiter_failure_is_unavailable_without_persistence(
    db, test_config,
):
    redis = _redis_for_workers(_live_workers())
    redis.consume_rate_limit.side_effect = ConnectionError("redis unavailable")
    app = create_app(db=db, redis=redis, config=test_config)

    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        response = await client.post("/api/jobs", json=_video_request())

    assert response.status_code == 503
    assert response.json()["error"] == "unavailable"
    assert db.list_jobs(limit=1)[0] == 0
    assert list(test_config.jobs_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_malformed_rate_limit_config_is_stable_unavailable(
    db, test_config, monkeypatch,
):
    monkeypatch.setenv("FLORI_JOBS_CREATE_RATE_LIMIT", "not-an-integer")
    redis = _redis_for_workers(_live_workers())
    app = create_app(db=db, redis=redis, config=test_config)

    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        response = await client.post("/api/jobs", json=_video_request())

    assert response.status_code == 503
    assert response.json()["error"] == "unavailable"
    redis.consume_rate_limit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    {"url": 123},
    {"url": []},
    {"content_type": []},
    {"url": "BV1xx411c7mD", "domain": {}},
    {"url": "BV1xx411c7mD", "style_tags": "formal"},
    {"url": "BV1xx411c7mD", "smart_note": {}},
])
async def test_invalid_create_shapes_remain_validation_422(
    db, test_config, payload,
):
    redis = _redis_for_workers(_live_workers())
    app = create_app(db=db, redis=redis, config=test_config)

    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        response = await client.post("/api/jobs", json=payload)

    assert response.status_code == 422
    assert response.json()["error"] == "invalid_request"


@pytest.mark.asyncio
async def test_video_admission_waits_when_worker_inventory_is_malformed(db, test_config):
    workers = _live_workers()
    workers["ai"]["reject_tags"] = None
    app = create_app(db=db, redis=_redis_for_workers(workers), config=test_config)

    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        response = await client.post("/api/jobs", json=_video_request())

    assert response.status_code == 201
    assert response.json()["status"] == "pending"


@pytest.mark.asyncio
async def test_malformed_worker_status_config_is_unavailable(db, test_config):
    test_config.pools["worker_status"]["online_window_sec"] = "invalid"
    app = create_app(
        db=db, redis=_redis_for_workers(_live_workers()), config=test_config,
    )

    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        response = await client.post("/api/jobs", json=_video_request())

    assert response.status_code == 503
    assert response.json()["error"] == "unavailable"


@pytest.mark.asyncio
async def test_malformed_pipeline_root_is_unavailable_without_persistence(
    db, test_config,
):
    test_config.pipelines = None
    app = create_app(
        db=db, redis=_redis_for_workers(_live_workers()), config=test_config,
    )

    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        response = await client.post("/api/jobs", json=_video_request())

    assert response.status_code == 503
    assert response.json()["error"] == "unavailable"
    assert db.list_jobs(limit=1)[0] == 0
    assert list(test_config.jobs_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_create_and_upload_share_one_operation_and_principal_bucket(
    db, test_config, monkeypatch,
):
    monkeypatch.setenv("API_TOKEN", "shared-bucket-token")
    monkeypatch.delenv("API_ALLOW_NO_AUTH", raising=False)
    redis = _redis_for_workers(_live_workers())
    redis.consume_rate_limit.side_effect = [(True, 1, 60), (False, 2, 29)]
    app = create_app(db=db, redis=redis, config=test_config)

    from httpx import ASGITransport, AsyncClient

    authorization = "Bearer shared-bucket-token"
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        created = await client.post(
            "/api/jobs", json=_video_request(),
            headers={"Authorization": authorization},
        )

    status, _headers, _body, receive_calls = await _asgi_post_without_body(
        app, "/api/jobs/upload", "content_type=video",
        headers=[(b"authorization", authorization.encode())],
    )

    assert created.status_code == 201
    assert status == 429
    assert receive_calls == 0
    calls = redis.consume_rate_limit.await_args_list
    assert calls[0].args[:2] == calls[1].args[:2]
    assert calls[0].args[0] == "jobs:create"


@pytest.mark.asyncio
@pytest.mark.parametrize("broken_pipeline", [
    {},
    {"steps": []},
    {"steps": [{}]},
    {"steps": [{"name": "broken"}]},
    {"steps": ["not-a-step"]},
])
async def test_malformed_or_empty_pipeline_fails_closed_before_persistence(
    db, test_config, broken_pipeline,
):
    test_config.pipelines["video"] = broken_pipeline
    app = create_app(
        db=db, redis=_redis_for_workers(_live_workers()), config=test_config,
    )

    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        response = await client.post("/api/jobs", json=_video_request())

    assert response.status_code == 503
    assert response.json()["error"] == "unavailable"
    assert db.list_jobs(limit=1)[0] == 0
    assert list(test_config.jobs_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_deep_json_is_stable_invalid_request_without_persistence(
    db, test_config,
):
    app = create_app(
        db=db, redis=_redis_for_workers(_live_workers()), config=test_config,
    )
    raw = b"[" * 1100 + b"0" + b"]" * 1100

    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/jobs", content=raw, headers={"Content-Type": "application/json"},
        )

    assert response.status_code == 422
    assert response.json()["error"] == "invalid_request"
    assert db.list_jobs(limit=1)[0] == 0


@pytest.mark.asyncio
async def test_removed_video_upload_rejects_before_deep_form_parsing(
    db, test_config,
):
    app = create_app(
        db=db, redis=_redis_for_workers(_live_workers()), config=test_config,
    )
    deep_style_tags = "[" * 1100 + '"tag"' + "]" * 1100

    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/jobs/upload?content_type=video",
            files={"file": ("deep.mp4", b"payload", "video/mp4")},
            data={"style_tags": deep_style_tags},
        )

    assert response.status_code == 422
    assert response.json()["error"] == "invalid_request"
    assert db.list_jobs(limit=1)[0] == 0
    assert list(test_config.jobs_dir.iterdir()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("workers", [{}, _live_workers()])
async def test_invalid_source_content_type_is_stable_422_before_worker_check(
    db, test_config, workers,
):
    redis = _redis_for_workers(workers)
    app = create_app(db=db, redis=redis, config=test_config)

    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/jobs",
            json={"url": "https://youtu.be/dQw4w9WgXcQ", "content_type": "article"},
        )

    assert response.status_code == 422
    assert response.json()["error"] == "invalid_request"
    redis.list_worker_ids.assert_not_awaited()
    assert db.list_jobs(limit=1)[0] == 0


def test_protected_post_routes_use_explicit_marker_not_endpoint_name():
    from api.routes.jobs import router as jobs_router

    protected = {
        route.path: (
            type(route).__name__,
            getattr(route.endpoint, "__flori_job_admission__", None),
        )
        for route in jobs_router.routes
        if "POST" in getattr(route, "methods", set())
        and route.path in {"/api/jobs", "/api/jobs/upload"}
    }
    assert protected == {
        "/api/jobs": ("JobAdmissionRoute", "create"),
        "/api/jobs/upload": ("JobAdmissionRoute", "upload"),
    }
