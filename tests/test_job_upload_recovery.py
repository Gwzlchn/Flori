"""进程终止后作业初始化 marker 与 staging 的恢复测试。"""

from __future__ import annotations

import json
import os
import asyncio
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.routes.jobs import (
    INITIALIZATION_MARKER_REL,
    reconcile_incomplete_job_uploads,
)
from shared.models import Job
from shared.storage import LocalStorage


def _marker(
    job_id: str,
    updated_at: datetime,
    *,
    token: str = "token-a",
    defer_submit: bool = False,
    event_published: bool = False,
) -> bytes:
    value = {
        "schema": "flori-job-initialization",
        "version": 1,
        "job_id": job_id,
        "source_rel": "input/source.mp4",
        "staging_token": token,
        "owner_id": "api-owner-a",
        "created_at": updated_at.isoformat(),
        "updated_at": updated_at.isoformat(),
        "defer_submit": defer_submit,
        "event_published": event_published,
    }
    return json.dumps(value).encode()


async def _seed(
    storage: LocalStorage,
    job_id: str,
    updated_at: datetime,
    *,
    marker: bytes | None = None,
) -> None:
    await storage.write_file(
        job_id,
        INITIALIZATION_MARKER_REL,
        marker if marker is not None else _marker(job_id, updated_at),
    )
    await storage.write_file(job_id, "input/source.mp4", b"source")
    staging = storage.jobs_dir / ".flori-staging" / job_id / "token-a"
    staging.parent.mkdir(parents=True, exist_ok=True)
    staging.write_bytes(b"partial")
    stamp = updated_at.timestamp()
    os.utime(staging, (stamp, stamp))


@pytest.mark.asyncio
async def test_active_marker_is_not_reclaimed(db, test_config):
    storage = LocalStorage(test_config.jobs_dir)
    redis = AsyncMock()
    now = datetime.now(timezone.utc)
    await _seed(storage, "jobs_video_active", now)

    result = await reconcile_incomplete_job_uploads(
        db, redis, storage, now=now, stale_after_sec=3600,
    )

    assert result["status"] == "ok"
    assert result["active"] == 1
    assert await storage.read_file("jobs_video_active", "input/source.mp4") == b"source"
    assert (storage.jobs_dir / ".flori-staging/jobs_video_active/token-a").exists()
    redis.append_lifecycle_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_stale_marker_without_db_deletes_job_and_global_staging(db, test_config):
    storage = LocalStorage(test_config.jobs_dir)
    redis = AsyncMock()
    now = datetime.now(timezone.utc)
    old = now - timedelta(hours=2)
    await _seed(storage, "jobs_video_orphan", old)

    result = await reconcile_incomplete_job_uploads(
        db, redis, storage, now=now, stale_after_sec=3600,
    )

    assert result["status"] == "ok"
    assert result["deleted_orphans"] == 1
    assert not (storage.jobs_dir / "jobs_video_orphan").exists()
    assert not (storage.jobs_dir / ".flori-staging/jobs_video_orphan").exists()
    redis.append_lifecycle_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_stale_marker_with_db_republishes_then_removes_marker(db, test_config):
    storage = LocalStorage(test_config.jobs_dir)
    redis = AsyncMock()
    now = datetime.now(timezone.utc)
    old = now - timedelta(hours=2)
    job_id = "jobs_video_recover"
    await _seed(storage, job_id, old)
    db.create_job(Job(id=job_id, content_type="video", pipeline="video", source="upload"))

    result = await reconcile_incomplete_job_uploads(
        db, redis, storage, now=now, stale_after_sec=3600,
    )

    assert result["status"] == "ok"
    assert result["recovered_db_jobs"] == 1
    redis.append_lifecycle_event.assert_awaited_once_with(
        "job_command",
        {"action": "new_job", "job_id": job_id, "pipeline": "video"},
    )
    assert await storage.read_file(job_id, INITIALIZATION_MARKER_REL) is None
    assert await storage.read_file(job_id, "input/source.mp4") == b"source"


@pytest.mark.asyncio
async def test_event_published_marker_does_not_publish_again(db, test_config):
    storage = LocalStorage(test_config.jobs_dir)
    redis = AsyncMock()
    now = datetime.now(timezone.utc)
    old = now - timedelta(hours=2)
    job_id = "jobs_video_event_done"
    await _seed(
        storage,
        job_id,
        old,
        marker=_marker(job_id, old, event_published=True),
    )
    db.create_job(Job(id=job_id, content_type="video", pipeline="video", source="upload"))

    result = await reconcile_incomplete_job_uploads(
        db, redis, storage, now=now, stale_after_sec=3600,
    )

    assert result["recovered_db_jobs"] == 1
    redis.append_lifecycle_event.assert_not_awaited()
    assert await storage.read_file(job_id, INITIALIZATION_MARKER_REL) is None


@pytest.mark.asyncio
async def test_corrupt_marker_fails_closed_and_preserves_job_and_staging(db, test_config):
    storage = LocalStorage(test_config.jobs_dir)
    redis = AsyncMock()
    now = datetime.now(timezone.utc)
    old = now - timedelta(hours=2)
    job_id = "jobs_video_corrupt"
    await _seed(storage, job_id, old, marker=b'{"schema":"wrong"}')

    result = await reconcile_incomplete_job_uploads(
        db, redis, storage, now=now, stale_after_sec=3600,
    )

    assert result["status"] == "partial"
    assert result["errors"][0]["job_id"] == job_id
    assert await storage.read_file(job_id, "input/source.mp4") == b"source"
    assert (storage.jobs_dir / ".flori-staging" / job_id / "token-a").exists()
    redis.append_lifecycle_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_recovery_loop_runs_immediately_and_cancels(monkeypatch):
    from api import main as api_main
    from api.routes import jobs as jobs_route

    called = asyncio.Event()

    async def reconcile(*_args, **_kwargs):
        called.set()
        return {"status": "ok"}

    monkeypatch.setattr(jobs_route, "reconcile_incomplete_job_uploads", reconcile)
    app = SimpleNamespace(state=SimpleNamespace(db=object(), redis=object(), storage=object()))

    task = asyncio.create_task(api_main._initialization_recovery_loop(app))
    await asyncio.wait_for(called.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_real_app_lifespan_drains_pending_remote_finalizer(
    db, test_config, monkeypatch,
):
    from api.main import create_app
    from shared.storage import RemoteStorage

    redis = AsyncMock()
    app = create_app(db=db, redis=redis, config=test_config)
    storage = RemoteStorage(
        "h:9000", "k", "s", "b", False, tmp_root=test_config.jobs_dir,
    )
    client = MagicMock()
    reader_started = threading.Event()
    release_minio = threading.Event()
    drain_started = asyncio.Event()

    def blocked_upload(_bucket, _key, data, **_kwargs):
        reader_started.set()
        try:
            while data.read(3):
                pass
        except OSError:
            release_minio.wait(2)
            raise

    client.put_object.side_effect = blocked_upload
    storage._client = lambda: client
    original_wait = storage.wait_for_finalizers

    async def observed_wait():
        drain_started.set()
        await original_wait()

    monkeypatch.setattr(storage, "wait_for_finalizers", observed_wait)
    app.state.storage = storage

    async def release_on_drain():
        await drain_started.wait()
        release_minio.set()

    release_task = asyncio.create_task(release_on_drain())
    async with app.router.lifespan_context(app):
        hold_source = asyncio.Event()

        async def source():
            yield b"partial"
            await hold_source.wait()

        upload = asyncio.create_task(storage.write_stream("j1", "out/a.bin", source()))
        assert await asyncio.to_thread(reader_started.wait, 1)
        upload.cancel()
        with pytest.raises(asyncio.CancelledError):
            await upload

    await release_task
    assert not storage._finalizer_tasks
    client.remove_object.assert_called_once()


@pytest.mark.asyncio
async def test_app_lifespan_finalizer_timeout_logs_recovery_fallback(
    db, test_config, monkeypatch,
):
    from api import main as api_main

    logger = MagicMock()
    cancelled = asyncio.Event()

    class StuckStorage:
        async def wait_for_finalizers(self):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    app = api_main.create_app(db=db, redis=AsyncMock(), config=test_config)
    app.state.storage = StuckStorage()
    monkeypatch.setattr(api_main, "_UPLOAD_FINALIZER_DRAIN_TIMEOUT_SEC", 0.01)
    monkeypatch.setattr("structlog.get_logger", lambda **_kwargs: logger)

    async with app.router.lifespan_context(app):
        pass

    assert cancelled.is_set()
    logger.error.assert_called_once_with(
        "upload_finalizer_drain_timeout",
        timeout_sec=0.01,
        recovery="initialization_marker_reconciler",
    )
