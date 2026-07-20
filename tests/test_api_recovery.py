"""系统设置备份与恢复交接API的安全边界。"""

from __future__ import annotations

import asyncio
import hashlib

import pytest

from api import exact_dr, recovery, recovery_worker
from shared.content_repository import (
    REPOSITORY_FORMAT,
    SNAPSHOT_FORMAT,
    SOURCE_MANIFEST_FORMAT,
    ContentRepository,
)
from shared.content_result import ResultFileError
from shared.step_manifest import canonical_digest
from shared.exact_dr_maintenance import acquire_barrier, read_barrier


def _ready_snapshot(*, portable_ready: bool = True) -> dict:
    reasons = [] if portable_ready else ["external_media_dependencies"]
    return {
        "format": SNAPSHOT_FORMAT,
        "repository_format": REPOSITORY_FORMAT,
        "source": {
            "app_version": "2.3.0",
            "db_user_version": 9,
            "manifest_format": SOURCE_MANIFEST_FORMAT,
        },
        "selector": {"partial": False, "job_ids": []},
        "records": {
            "jobs": [], "parts": [], "step_results": [],
            "failures": [], "business_ledgers": [],
        },
        "blob_refs": [],
        "relations_digest": canonical_digest({"edges": []}),
        "policy": {
            "successful_artifacts_only": True,
            "secrets_included": False,
            "secret_scan_exceptions": [],
            "runtime_state_included": False,
        },
        "completeness": {
            "terminal_steps": 0,
            "manifests_seen": 0,
            "manifests_missing": 0,
            "manifests_excluded": 0,
            "ai_config_complete": True,
            "user_config_complete": True,
            "secret_scan_complete": True,
            "media_self_contained": portable_ready,
            "external_media_roots": [] if portable_ready else ["library"],
            "portable_ready": portable_ready,
            "readiness_reasons": reasons,
        },
    }


def _seed_ready_repository(path, *, portable_ready: bool = True):
    repository = ContentRepository.create(path)
    snapshot = repository.put_snapshot(_ready_snapshot(portable_ready=portable_ready))
    repository.set_ref("latest", snapshot.digest)
    repository.write_receipt({
        "run_id": "backup-test",
        "observed_at": "2026-07-19T11:00:00Z",
        "outcome": "success",
        "snapshot_digest": snapshot.digest,
        "stats": {"jobs": 0, "parts": 0},
    })
    return repository, snapshot.digest


@pytest.mark.asyncio
async def test_recovery_status_empty_repository(client, test_config, tmp_path, monkeypatch):
    repository_path = tmp_path.parent / f"{tmp_path.name}-portable"
    repository_path.mkdir()
    monkeypatch.setenv(recovery.REPOSITORY_ENV, str(repository_path))

    response = await client.get("/api/recovery")

    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "empty"
    assert body["latest"] is None
    assert body["online_restore_supported"] is False
    assert body["exact_dr"]["state"] == "idle"
    assert (test_config.data_dir / "recovery-control" / "control.json").is_file()


@pytest.mark.asyncio
async def test_exact_dr_endpoint_fail_fast_blocks_new_writes_but_keeps_status(
    app, client, test_config, tmp_path, monkeypatch,
):
    repository_path = tmp_path.parent / f"{tmp_path.name}-portable-exact"
    repository_path.mkdir()
    output = tmp_path.parent / f"{tmp_path.name}-exact-output"
    monkeypatch.setenv(recovery.REPOSITORY_ENV, str(repository_path))
    monkeypatch.setenv(exact_dr.OUTPUT_ENV, str(output))
    monkeypatch.setenv("FLORI_DEPLOYMENT_ID", "flori-test")
    release = asyncio.Event()
    monkeypatch.setattr("api.routes.recovery.validate_exact_dr_start", lambda: None)

    async def fake_run(fake_app, operation_id):
        await release.wait()
        await fake_app.state.exact_dr_gate.finish(
            test_config.data_dir,
            operation_id=operation_id,
        )

    monkeypatch.setattr("api.routes.recovery.run_exact_dr", fake_run)
    bad = await client.post(
        "/api/recovery/exact-dr",
        json={"confirmation": "wrong"},
    )
    assert bad.status_code == 409

    started = await client.post(
        "/api/recovery/exact-dr",
        json={"confirmation": exact_dr.CONFIRMATION},
    )
    assert started.status_code == 202
    assert started.json()["operation"]["status"] == "draining"

    blocked = await client.post("/api/recovery/backups", json={})
    assert blocked.status_code == 503
    assert blocked.json()["error"] == "exact_dr_maintenance"
    blocked_get = await client.get("/api/bili/login/poll", params={"qrcode_key": "x"})
    assert blocked_get.status_code == 503
    status_response = await client.get("/api/recovery")
    assert status_response.status_code == 200
    assert status_response.json()["exact_dr"]["state"] == "draining"

    release.set()
    await app.state.exact_dr_task


@pytest.mark.asyncio
async def test_exact_dr_start_cancellation_releases_owner_barrier(
    app, client, test_config, monkeypatch,
):
    monkeypatch.setattr("api.routes.recovery.validate_exact_dr_start", lambda: None)

    async def acquire_then_cancel(data_dir, *, operation_id, created_at):
        acquire_barrier(
            data_dir,
            operation_id=operation_id,
            created_at=created_at,
        )
        raise asyncio.CancelledError

    monkeypatch.setattr(app.state.exact_dr_gate, "begin_draining", acquire_then_cancel)

    with pytest.raises(RuntimeError, match="No response returned"):
        await client.post(
            "/api/recovery/exact-dr",
            json={"confirmation": exact_dr.CONFIRMATION},
        )

    assert read_barrier(test_config.data_dir) is None
    assert exact_dr.read_operation(test_config.data_dir)["status"] == "interrupted"


@pytest.mark.asyncio
async def test_portable_backup_rechecks_exact_dr_after_entering_start_lock(
    app, client, test_config, monkeypatch,
):
    gate = app.state.exact_dr_gate
    await gate.begin_draining(
        test_config.data_dir,
        operation_id="exact-dr-portable-race",
        created_at="2026-07-20T00:00:00+00:00",
    )
    async def bypass_middleware(*_args):
        return False

    monkeypatch.setattr(gate, "enter_request", bypass_middleware)
    try:
        response = await client.post("/api/recovery/backups", json={})
        assert response.status_code == 409
        assert "exact DR" in response.json()["message"]
    finally:
        await gate.finish(
            test_config.data_dir,
            operation_id="exact-dr-portable-race",
        )


@pytest.mark.asyncio
async def test_recovery_status_rejects_repository_inside_data_tree(
    client, test_config, monkeypatch,
):
    unsafe = test_config.data_dir / "portable"
    unsafe.mkdir()
    monkeypatch.setenv(recovery.REPOSITORY_ENV, str(unsafe))

    response = await client.get("/api/recovery")

    assert response.status_code == 200
    assert response.json()["state"] == "error"
    assert "物理隔离" in response.json()["error"]


@pytest.mark.asyncio
async def test_recovery_status_rejects_repository_parent_symlink(
    client, tmp_path, monkeypatch,
):
    actual = tmp_path.parent / f"{tmp_path.name}-actual"
    actual.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(actual, target_is_directory=True)
    monkeypatch.setenv(recovery.REPOSITORY_ENV, str(alias / "portable"))

    response = await client.get("/api/recovery")

    assert response.status_code == 200
    assert response.json()["state"] == "error"
    assert "符号链接" in response.json()["error"]


@pytest.mark.asyncio
async def test_recovery_status_exposes_manifest_and_video_closure(
    client, tmp_path, monkeypatch,
):
    repository_path = tmp_path.parent / f"{tmp_path.name}-portable"
    _repo, digest = _seed_ready_repository(repository_path)
    monkeypatch.setenv(recovery.REPOSITORY_ENV, str(repository_path))

    body = (await client.get("/api/recovery")).json()

    assert body["state"] == "ready"
    assert body["latest"]["digest"] == digest
    assert body["latest"]["portable_ready"] is True
    assert body["latest"]["completeness"]["media_self_contained"] is True
    assert body["latest"]["stats"] == {"jobs": 0, "parts": 0}


@pytest.mark.asyncio
async def test_start_backup_returns_persistent_operation_without_running_inline(
    client, app, test_config, tmp_path, monkeypatch,
):
    repository_path = tmp_path.parent / f"{tmp_path.name}-portable"
    repository_path.mkdir()
    monkeypatch.setenv(recovery.REPOSITORY_ENV, str(repository_path))
    release = asyncio.Event()

    async def fake_worker(_app, _operation_id):
        await release.wait()

    monkeypatch.setattr("api.routes.recovery._run_backup_worker", fake_worker)

    response = await client.post(
        "/api/recovery/backups",
        json={"vendor_media": False, "full_rehash": True},
    )

    assert response.status_code == 202
    operation = response.json()["operation"]
    assert operation["status"] == "queued"
    assert operation["full_rehash"] is True
    assert recovery.read_operation(test_config.data_dir, operation["id"])["status"] == "queued"
    assert operation["id"] in app.state.recovery_tasks
    release.set()
    await app.state.recovery_tasks[operation["id"]]


@pytest.mark.asyncio
async def test_start_backup_endpoint_runs_isolated_worker_to_completion(
    client, app, db, test_config, tmp_path, monkeypatch,
):
    repository_path = tmp_path.parent / f"{tmp_path.name}-api-worker-repository"
    monkeypatch.setenv(recovery.REPOSITORY_ENV, str(repository_path))
    monkeypatch.delenv("MINIO_URL", raising=False)

    response = await client.post("/api/recovery/backups", json={})

    assert response.status_code == 202
    operation_id = response.json()["operation"]["id"]
    task = app.state.recovery_tasks[operation_id]
    await asyncio.wait_for(asyncio.shield(task), timeout=30)
    completed = recovery.read_operation(test_config.data_dir, operation_id)
    assert completed["status"] == "success", completed["error"]
    assert ContentRepository.open(repository_path).get_ref("latest") == completed["snapshot_digest"]


@pytest.mark.asyncio
async def test_start_backup_rejects_client_supplied_paths(client):
    response = await client.post(
        "/api/recovery/backups",
        json={"vendor_media": False, "repository_path": "/tmp/attacker"},
    )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_start_backup_rejects_physical_alias_before_operation(
    client, test_config, tmp_path, monkeypatch,
):
    repository_path = tmp_path.parent / f"{tmp_path.name}-physical-alias"
    monkeypatch.setenv(recovery.REPOSITORY_ENV, str(repository_path))

    def reject_alias(_outputs, _protected):
        raise ResultFileError("physically aliases protected data root")

    monkeypatch.setattr(recovery, "ensure_output_roots_disjoint", reject_alias)

    response = await client.post("/api/recovery/backups", json={})

    assert response.status_code == 503
    assert not repository_path.exists()
    operations = test_config.data_dir / "recovery-control" / "operations"
    assert not operations.exists() or list(operations.glob("*.json")) == []


@pytest.mark.asyncio
async def test_start_backup_rejects_concurrent_operation(client, app):
    blocker = asyncio.create_task(asyncio.sleep(30))
    app.state.recovery_tasks["backup-existing"] = blocker
    try:
        response = await client.post("/api/recovery/backups", json={})
        assert response.status_code == 409
        assert "正在运行" in response.json()["message"]
    finally:
        blocker.cancel()
        await asyncio.gather(blocker, return_exceptions=True)


@pytest.mark.asyncio
async def test_simultaneous_backup_requests_have_one_winner(
    client, app, tmp_path, monkeypatch,
):
    repository_path = tmp_path.parent / f"{tmp_path.name}-portable-simultaneous"
    repository_path.mkdir()
    monkeypatch.setenv(recovery.REPOSITORY_ENV, str(repository_path))
    release = asyncio.Event()

    async def fake_worker(_app, _operation_id):
        await release.wait()

    monkeypatch.setattr("api.routes.recovery._run_backup_worker", fake_worker)

    responses = await asyncio.gather(
        client.post("/api/recovery/backups", json={}),
        client.post("/api/recovery/backups", json={}),
    )

    assert sorted(response.status_code for response in responses) == [202, 409]
    release.set()
    await asyncio.gather(*list(app.state.recovery_tasks.values()))


@pytest.mark.asyncio
async def test_start_backup_never_breaks_existing_repository_lock(
    client, tmp_path, monkeypatch,
):
    repository_path = tmp_path.parent / f"{tmp_path.name}-portable"
    repository = ContentRepository.create(repository_path)
    monkeypatch.setenv(recovery.REPOSITORY_ENV, str(repository_path))

    with repository.write_lock("other-process"):
        response = await client.post("/api/recovery/backups", json={})

    assert response.status_code == 409
    assert "不要自动" not in response.json()["message"]
    assert "显式处理" in response.json()["message"]


def test_restore_handoff_is_idempotent_and_does_not_touch_live_db(
    db, test_config, tmp_path, monkeypatch,
):
    repository_path = tmp_path.parent / f"{tmp_path.name}-portable"
    _repository, digest = _seed_ready_repository(repository_path)
    monkeypatch.setenv(recovery.REPOSITORY_ENV, str(repository_path))
    monkeypatch.setenv("FLORI_DEPLOYMENT_ID", "flori-test")
    before = hashlib.sha256(test_config.db_path.read_bytes()).hexdigest()

    first, reused_first = recovery.build_restore_handoff(
        data_dir=test_config.data_dir,
        config=test_config,
        snapshot_digest=digest,
    )
    second, reused_second = recovery.build_restore_handoff(
        data_dir=test_config.data_dir,
        config=test_config,
        snapshot_digest=digest,
    )

    assert reused_first is False
    assert reused_second is True
    assert first == second
    assert first["snapshot_digest"] == digest
    assert first["target_generation"].startswith("gen-")
    assert first["target_generation"] in first["commands"]["restore"]
    assert "$(date" not in first["commands"]["restore"]
    assert "scripts/backup.sh" in first["commands"]["exact_dr"]
    assert "--into-live" in first["commands"]["restore"]
    assert hashlib.sha256(test_config.db_path.read_bytes()).hexdigest() == before


def test_restore_handoff_rejects_tampered_reused_commands(
    db, test_config, tmp_path, monkeypatch,
):
    repository_path = tmp_path.parent / f"{tmp_path.name}-portable-tamper"
    _repository, digest = _seed_ready_repository(repository_path)
    monkeypatch.setenv(recovery.REPOSITORY_ENV, str(repository_path))
    monkeypatch.setenv("FLORI_DEPLOYMENT_ID", "flori-test")
    handoff, _reused = recovery.build_restore_handoff(
        data_dir=test_config.data_dir,
        config=test_config,
        snapshot_digest=digest,
    )
    path = (
        test_config.data_dir / "recovery-control" / "handoffs"
        / f"{handoff['id']}.json"
    )
    tampered = dict(handoff)
    tampered["commands"] = {**handoff["commands"], "restore": "rm -rf /data"}
    recovery._write_json_atomic(path, tampered)

    with pytest.raises(recovery.RecoveryControlError, match="modified"):
        recovery.build_restore_handoff(
            data_dir=test_config.data_dir,
            config=test_config,
            snapshot_digest=digest,
        )


@pytest.mark.asyncio
async def test_restore_plan_endpoint_rejects_missing_deployment_id(
    client, tmp_path, monkeypatch,
):
    repository_path = tmp_path.parent / f"{tmp_path.name}-portable"
    _repository, digest = _seed_ready_repository(repository_path)
    monkeypatch.setenv(recovery.REPOSITORY_ENV, str(repository_path))
    monkeypatch.delenv("FLORI_DEPLOYMENT_ID", raising=False)

    response = await client.post(
        "/api/recovery/restore-plans",
        json={"snapshot_digest": digest},
    )

    assert response.status_code == 409
    assert "FLORI_DEPLOYMENT_ID" in response.json()["message"]


@pytest.mark.asyncio
async def test_restore_plan_rejects_incomplete_snapshot_before_handoff(
    client, tmp_path, monkeypatch,
):
    repository_path = tmp_path.parent / f"{tmp_path.name}-portable-incomplete"
    _repository, digest = _seed_ready_repository(repository_path, portable_ready=False)
    monkeypatch.setenv(recovery.REPOSITORY_ENV, str(repository_path))
    monkeypatch.setenv("FLORI_DEPLOYMENT_ID", "flori-test")

    response = await client.post(
        "/api/recovery/restore-plans",
        json={"snapshot_digest": digest},
    )

    assert response.status_code == 409
    assert "external_media_dependencies" in response.json()["message"]


def test_interrupted_operation_is_not_reported_as_running(test_config):
    operation = recovery.new_backup_operation(
        data_dir=test_config.data_dir,
        vendor_media=False,
        full_rehash=False,
    )

    listed = recovery.list_operations(test_config.data_dir, active_operation_ids=set())

    assert listed[0]["id"] == operation["id"]
    assert listed[0]["status"] == "interrupted"
    assert "API重启" in listed[0]["error"]


@pytest.mark.asyncio
async def test_recovery_worker_creates_real_ready_snapshot(
    db, test_config, tmp_path, monkeypatch,
):
    repository_path = tmp_path.parent / f"{tmp_path.name}-worker-repository"
    monkeypatch.setenv(recovery.REPOSITORY_ENV, str(repository_path))
    monkeypatch.setenv("DATA_DIR", str(test_config.data_dir))
    monkeypatch.delenv("MINIO_URL", raising=False)
    operation = recovery.new_backup_operation(
        data_dir=test_config.data_dir,
        vendor_media=False,
        full_rehash=False,
    )

    returncode = await recovery_worker.run(operation["id"])

    completed = recovery.read_operation(test_config.data_dir, operation["id"])
    assert returncode == 0, completed["error"]
    assert completed["status"] == "success"
    assert completed["snapshot_digest"].startswith("sha256:")
    status = recovery.repository_status(
        data_dir=test_config.data_dir,
        active_operation_ids=set(),
    )
    assert status["state"] == "ready"
    assert status["latest"]["digest"] == completed["snapshot_digest"]


@pytest.mark.asyncio
async def test_recovery_worker_rechecks_physical_alias_before_writes(
    test_config, tmp_path, monkeypatch,
):
    repository_path = tmp_path.parent / f"{tmp_path.name}-worker-physical-alias"
    work_path = tmp_path.parent / f"{tmp_path.name}-worker-work"
    monkeypatch.setenv(recovery.REPOSITORY_ENV, str(repository_path))
    monkeypatch.setenv("DATA_DIR", str(test_config.data_dir))
    monkeypatch.setenv("WORK_DIR", str(work_path))
    operation = recovery.new_backup_operation(
        data_dir=test_config.data_dir,
        vendor_media=False,
        full_rehash=False,
    )

    def reject_alias(_outputs, _protected):
        raise ResultFileError("physically aliases protected data root")

    monkeypatch.setattr(
        recovery_worker,
        "ensure_output_roots_disjoint",
        reject_alias,
    )

    returncode = await recovery_worker.run(operation["id"])

    assert returncode == 1
    completed = recovery.read_operation(test_config.data_dir, operation["id"])
    assert completed["status"] == "failed"
    assert "physical boundary" in completed["error"]
    assert not repository_path.exists()
    assert not work_path.exists()


def test_control_root_symlink_is_rejected(test_config, tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside-control"
    outside.mkdir()
    (test_config.data_dir / "recovery-control").symlink_to(
        outside, target_is_directory=True,
    )

    with pytest.raises(recovery.RecoveryControlError, match="escaped|symlink"):
        recovery.new_backup_operation(
            data_dir=test_config.data_dir,
            vendor_media=False,
            full_rehash=False,
        )


def test_control_operations_subdirectory_symlink_is_rejected(test_config, tmp_path):
    root = recovery.control_root(test_config.data_dir)
    outside = tmp_path.parent / f"{tmp_path.name}-outside-operations"
    outside.mkdir()
    (root / "operations").symlink_to(outside, target_is_directory=True)

    with pytest.raises(recovery.RecoveryControlError, match="subdirectory.*symlink"):
        recovery.new_backup_operation(
            data_dir=test_config.data_dir,
            vendor_media=False,
            full_rehash=False,
        )


def test_restore_plan_target_subdirectory_symlink_is_rejected(
    test_config, tmp_path, monkeypatch,
):
    repository_path = tmp_path.parent / f"{tmp_path.name}-portable-plan-symlink"
    _repository, digest = _seed_ready_repository(repository_path)
    monkeypatch.setenv(recovery.REPOSITORY_ENV, str(repository_path))
    monkeypatch.setenv("FLORI_DEPLOYMENT_ID", "flori-test")
    root = recovery.control_root(test_config.data_dir)
    outside = tmp_path.parent / f"{tmp_path.name}-outside-plan"
    outside.mkdir()
    (root / "plan-targets").symlink_to(outside, target_is_directory=True)

    with pytest.raises(recovery.RecoveryControlError, match="subdirectory.*symlink"):
        recovery.build_restore_handoff(
            data_dir=test_config.data_dir,
            config=test_config,
            snapshot_digest=digest,
        )


def test_restore_handoff_rehashes_snapshot_blobs(
    test_config, tmp_path, monkeypatch,
):
    repository_path = tmp_path.parent / f"{tmp_path.name}-portable-corrupt-blob"
    repository = ContentRepository.create(repository_path)
    blob = repository.put_blob_bytes(b"original")
    record = repository.put_record("user_config", {
        "path": "prompts/recovery-test.md",
        "kind": "prompts",
        "blob": blob.digest,
        "size_bytes": blob.size_bytes,
        "media_type": "text/markdown",
    })
    body = _ready_snapshot()
    body["blob_refs"] = [blob.digest]
    body["records"]["business_ledgers"] = [record.digest]
    snapshot = repository.put_snapshot(body)
    repository.set_ref("latest", snapshot.digest)
    repository.blob_path(blob.digest).write_bytes(b"tampered")
    monkeypatch.setenv(recovery.REPOSITORY_ENV, str(repository_path))
    monkeypatch.setenv("FLORI_DEPLOYMENT_ID", "flori-test")

    with pytest.raises(recovery.RecoveryControlError, match="blob chain verification"):
        recovery.build_restore_handoff(
            data_dir=test_config.data_dir,
            config=test_config,
            snapshot_digest=snapshot.digest,
        )


@pytest.mark.asyncio
async def test_api_shutdown_cancels_recovery_background_tasks(app, tmp_path, monkeypatch):
    monkeypatch.setenv("FLORI_MAINTENANCE_LOCK_DIR", str(tmp_path / "locks"))
    cancelled = asyncio.Event()

    async def background():
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    async with app.router.lifespan_context(app):
        task = asyncio.create_task(background())
        app.state.recovery_tasks["backup-shutdown"] = task
        await asyncio.sleep(0)

    assert task.cancelled()
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_api_restart_unpauses_redis_before_recovering_stale_exact_dr(
    app, test_config, tmp_path, monkeypatch,
):
    monkeypatch.setenv("FLORI_MAINTENANCE_LOCK_DIR", str(tmp_path / "locks"))
    output = tmp_path / "exact-output"
    output.mkdir()
    monkeypatch.setenv(exact_dr.OUTPUT_ENV, str(output))
    operation = exact_dr.new_operation(test_config.data_dir)
    acquire_barrier(
        test_config.data_dir,
        operation_id=operation["id"],
        created_at=operation["created_at"],
    )
    app.state.redis.resume_writes_after_exact_dr.reset_mock()

    async with app.router.lifespan_context(app):
        assert read_barrier(test_config.data_dir) is None

    app.state.redis.resume_writes_after_exact_dr.assert_awaited_once()
    assert exact_dr.read_operation(test_config.data_dir)["status"] == "interrupted"
