"""作业上传流式写入的资源和原子性边界测试。"""

from __future__ import annotations

import tracemalloc
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.main import create_app
from api.routes import jobs as jobs_route


class _ChunkedUpload:
    filename = "large.mp4"

    def __init__(self, chunk: bytes, count: int, failure: Exception | None = None):
        self._chunk = chunk
        self._remaining = count
        self._failure = failure

    async def read(self, _size: int) -> bytes:
        if self._remaining:
            self._remaining -= 1
            return self._chunk
        if self._failure is not None:
            failure, self._failure = self._failure, None
            raise failure
        return b""


def _job_count(db) -> int:
    return db.list_jobs(limit=1)[0]


@pytest.fixture
def mock_redis():
    redis = AsyncMock()
    redis.list_worker_ids.return_value = ["w-ai"]
    return redis


@pytest.fixture
def app(db, mock_redis, test_config):
    return create_app(db=db, redis=mock_redis, config=test_config)


@pytest.mark.asyncio
async def test_upload_uses_stream_writer_and_accepts_exact_limit(
    client, app, db, mock_redis, monkeypatch,
):
    monkeypatch.setattr(jobs_route, "MAX_UPLOAD_SIZE", 6)
    storage = app.state.storage
    original_stream = storage.write_stream
    storage.write_stream = AsyncMock(wraps=original_stream)
    original_write = storage.write_file
    storage.write_file = AsyncMock(wraps=original_write)

    response = await client.post(
        "/api/jobs/upload",
        files={"file": ("boundary.mp4", b"abcdef", "video/mp4")},
    )

    assert response.status_code == 201
    job_id = response.json()["job_id"]
    storage.write_stream.assert_awaited_once()
    args, kwargs = storage.write_stream.await_args
    assert args[:2] == (job_id, "input/source.mp4")
    assert kwargs["max_bytes"] == 6
    assert isinstance(kwargs["staging_token"], str)
    assert [call.args[1] for call in storage.write_file.await_args_list] == [
        jobs_route.INITIALIZATION_MARKER_REL,
        "job.json",
        jobs_route.INITIALIZATION_MARKER_REL,
    ]
    assert await storage.read_file(job_id, "input/source.mp4") == b"abcdef"
    assert await storage.read_file(
        job_id, jobs_route.INITIALIZATION_MARKER_REL,
    ) is None
    assert _job_count(db) == 1
    mock_redis.append_lifecycle_event.assert_awaited_once()


@pytest.mark.asyncio
async def test_upload_limit_plus_one_leaves_no_job_object_or_event(
    client, app, db, mock_redis, monkeypatch,
):
    monkeypatch.setattr(jobs_route, "MAX_UPLOAD_SIZE", 6)

    response = await client.post(
        "/api/jobs/upload",
        files={"file": ("oversize.mp4", b"abcdefg", "video/mp4")},
    )

    assert response.status_code == 413
    assert _job_count(db) == 0
    assert list(app.state.config.jobs_dir.iterdir()) == []
    mock_redis.append_lifecycle_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_upload_disconnect_cleans_staging_db_and_event(
    app, db, mock_redis, monkeypatch,
):
    monkeypatch.setattr(jobs_route, "MAX_UPLOAD_SIZE", 8)
    upload = _ChunkedUpload(b"abcd", 1, ConnectionError("client disconnected"))

    with pytest.raises(ConnectionError, match="client disconnected"):
        await jobs_route.upload_job(
            file=upload,
            domain="general",
            style_tags="[]",
            collection_id=None,
            title=None,
            db=db,
            redis=mock_redis,
            storage=app.state.storage,
            config=app.state.config,
        )

    assert _job_count(db) == 0
    assert list(app.state.config.jobs_dir.iterdir()) == []
    mock_redis.append_lifecycle_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_upload_storage_failure_removes_published_source_before_db(
    client, app, db, mock_redis, monkeypatch,
):
    storage = app.state.storage
    original_stream = storage.write_stream

    async def publish_then_fail(job_id, rel_path, chunks, **kwargs):
        await original_stream(job_id, rel_path, chunks, **kwargs)
        raise OSError("job metadata write unavailable")

    monkeypatch.setattr(storage, "write_stream", publish_then_fail)

    with pytest.raises(OSError, match="job metadata write unavailable"):
        await client.post(
            "/api/jobs/upload",
            files={"file": ("failure.mp4", b"source", "video/mp4")},
        )
    assert _job_count(db) == 0
    assert list(app.state.config.jobs_dir.iterdir()) == []
    mock_redis.append_lifecycle_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_job_metadata_failure_removes_source_db_and_event(
    client, app, db, mock_redis, monkeypatch,
):
    storage = app.state.storage
    original_write = storage.write_file

    async def fail_job_json(job_id, rel_path, data):
        if rel_path == "job.json":
            raise OSError("job metadata unavailable")
        await original_write(job_id, rel_path, data)

    monkeypatch.setattr(storage, "write_file", fail_job_json)

    with pytest.raises(OSError, match="job metadata unavailable"):
        await client.post(
            "/api/jobs/upload",
            files={"file": ("failure.mp4", b"source", "video/mp4")},
        )
    assert _job_count(db) == 0
    assert list(app.state.config.jobs_dir.iterdir()) == []
    mock_redis.append_lifecycle_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_db_create_failure_removes_source_metadata_and_event(
    client, app, db, mock_redis, monkeypatch,
):
    def fail_create(_job):
        raise RuntimeError("database create failed")

    monkeypatch.setattr(db, "create_job", fail_create)

    with pytest.raises(RuntimeError, match="database create failed"):
        await client.post(
            "/api/jobs/upload",
            files={"file": ("failure.mp4", b"source", "video/mp4")},
        )
    assert _job_count(db) == 0
    assert list(app.state.config.jobs_dir.iterdir()) == []
    mock_redis.append_lifecycle_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_collection_count_failure_rolls_back_db_and_storage(
    app, db, mock_redis, monkeypatch,
):
    def fail_increment(*_args, **_kwargs):
        raise RuntimeError("collection count failed")

    monkeypatch.setattr(db, "increment_collection_count", fail_increment)

    with pytest.raises(RuntimeError, match="collection count failed"):
        await jobs_route.create_job_core(
            db,
            mock_redis,
            app.state.storage,
            url=None,
            content_type="video",
            collection_id="collection-missing",
            upload=(".mp4", b"source"),
            config=app.state.config,
        )

    assert _job_count(db) == 0
    assert list(app.state.config.jobs_dir.iterdir()) == []
    mock_redis.append_lifecycle_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_lifecycle_publish_failure_rolls_back_db_and_storage(
    app, db, mock_redis,
):
    mock_redis.append_lifecycle_event.side_effect = ConnectionError(
        "redis publish failed"
    )

    with pytest.raises(ConnectionError, match="redis publish failed"):
        await jobs_route.create_job_core(
            db,
            mock_redis,
            app.state.storage,
            url=None,
            content_type="video",
            upload=(".mp4", b"source"),
            config=app.state.config,
        )

    assert _job_count(db) == 0
    assert list(app.state.config.jobs_dir.iterdir()) == []
    mock_redis.append_lifecycle_event.assert_awaited_once()
    mock_redis.remove_job_tasks.assert_awaited_once()
    mock_redis.cleanup_job.assert_awaited_once()
    mock_redis.remove_active_job.assert_awaited_once()


@pytest.mark.asyncio
async def test_cleanup_failure_is_logged_without_masking_original(
    client, app, mock_redis, monkeypatch,
):
    storage = app.state.storage
    logger = MagicMock()

    async def fail_stream(*_args, **_kwargs):
        raise OSError("source write failed")

    async def fail_delete(_job_id):
        raise ConnectionError("cleanup unavailable")

    monkeypatch.setattr(storage, "write_stream", fail_stream)
    monkeypatch.setattr(storage, "delete", fail_delete)
    monkeypatch.setattr(jobs_route, "_LOG", logger)

    with pytest.raises(OSError, match="source write failed") as raised:
        await client.post(
            "/api/jobs/upload",
            files={"file": ("failure.mp4", b"source", "video/mp4")},
        )
    assert any("cleanup unavailable" in note for note in raised.value.__notes__)
    logger.error.assert_called_once_with(
        "job_initialization_cleanup_failed",
        job_id=logger.error.call_args.kwargs["job_id"],
        original_error="OSError",
        cleanup_error="ConnectionError",
        cleanup_detail="cleanup unavailable",
    )
    mock_redis.append_lifecycle_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_upload_route_memory_is_bounded_by_chunk(
    app, db, mock_redis, monkeypatch,
):
    chunk = b"x" * (1024 * 1024)
    upload = _ChunkedUpload(chunk, 32)
    monkeypatch.setattr(jobs_route, "MAX_UPLOAD_SIZE", 64 * 1024 * 1024)

    tracemalloc.start()
    tracemalloc.reset_peak()
    result = await jobs_route.upload_job(
        file=upload,
        domain="general",
        style_tags="[]",
        collection_id=None,
        title=None,
        db=db,
        redis=mock_redis,
        storage=app.state.storage,
        config=app.state.config,
    )
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert result["status"] == "pending"
    assert peak < 8 * 1024 * 1024
    assert _job_count(db) == 1
