"""exact DR 停写屏障的安全边界。"""

import json
import asyncio
import hashlib
import os
import sqlite3
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock
from pathlib import Path

import pytest

from shared.content_import_guard import LiveTargetError, verify_dr_receipt
from shared.exact_dr_maintenance import (
    ExactDrBarrierError,
    PHASE_DRAINING,
    PHASE_SNAPSHOTTING,
    acquire_barrier,
    advance_barrier,
    barrier_path,
    read_barrier,
    release_barrier,
    scheduler_quiesced,
    write_scheduler_quiesced,
)
from api import exact_dr


def test_barrier_is_exclusive_monotonic_and_owner_bound(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    first = acquire_barrier(
        data, operation_id="exact-dr-one", created_at="2026-07-20T00:00:00+00:00",
    )

    assert first["phase"] == PHASE_DRAINING
    with pytest.raises(ExactDrBarrierError, match="already held"):
        acquire_barrier(
            data, operation_id="exact-dr-two", created_at="2026-07-20T00:00:01+00:00",
        )

    advanced = advance_barrier(
        data,
        operation_id="exact-dr-one",
        phase=PHASE_SNAPSHOTTING,
        updated_at="2026-07-20T00:01:00+00:00",
    )
    assert advanced["phase"] == PHASE_SNAPSHOTTING
    with pytest.raises(ExactDrBarrierError, match="cannot move backwards"):
        advance_barrier(
            data,
            operation_id="exact-dr-one",
            phase=PHASE_DRAINING,
            updated_at="2026-07-20T00:02:00+00:00",
        )
    with pytest.raises(ExactDrBarrierError, match="another"):
        release_barrier(data, operation_id="exact-dr-two")

    release_barrier(data, operation_id="exact-dr-one")
    assert read_barrier(data) is None


def test_corrupt_or_symlink_barrier_fails_closed(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    acquire_barrier(
        data, operation_id="exact-dr-owner", created_at="2026-07-20T00:00:00+00:00",
    )
    path = barrier_path(data)
    path.write_text(json.dumps({"format": "wrong"}), encoding="utf-8")
    with pytest.raises(ExactDrBarrierError, match="unsupported"):
        read_barrier(data)

    path.unlink()
    target = tmp_path / "outside.json"
    target.write_text("{}", encoding="utf-8")
    path.symlink_to(target)
    with pytest.raises(ExactDrBarrierError, match="cannot open|regular file"):
        read_barrier(data)


def test_control_root_rename_swap_cannot_redirect_barrier_read(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    acquire_barrier(
        data,
        operation_id="exact-dr-original",
        created_at="2026-07-20T00:00:00+00:00",
    )
    original = data / "exact-dr-control"
    moved = data / "exact-dr-control-moved"
    original.rename(moved)
    outside = tmp_path / "outside-control"
    outside.mkdir()
    original.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ExactDrBarrierError, match="safely open"):
        read_barrier(data)

def test_scheduler_ack_is_bound_to_current_snapshotting_operation(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    acquire_barrier(
        data, operation_id="exact-dr-owner", created_at="2026-07-20T00:00:00+00:00",
    )
    with pytest.raises(ExactDrBarrierError, match="snapshotting"):
        write_scheduler_quiesced(
            data, operation_id="exact-dr-owner", at="2026-07-20T00:01:00+00:00",
        )
    advance_barrier(
        data,
        operation_id="exact-dr-owner",
        phase=PHASE_SNAPSHOTTING,
        updated_at="2026-07-20T00:01:00+00:00",
    )
    write_scheduler_quiesced(
        data, operation_id="exact-dr-owner", at="2026-07-20T00:01:01+00:00",
    )
    assert scheduler_quiesced(data, operation_id="exact-dr-owner") is True
    with pytest.raises(ExactDrBarrierError, match="identity"):
        scheduler_quiesced(data, operation_id="exact-dr-other")
    release_barrier(data, operation_id="exact-dr-owner")
    assert scheduler_quiesced(data, operation_id="exact-dr-owner") is False


@pytest.mark.asyncio
async def test_mutation_gate_rejects_new_writes_and_waits_for_entered_write(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    gate = exact_dr.ExactDrMutationGate()
    entered = await gate.enter_request("POST", "/api/jobs")
    await gate.begin_draining(
        data,
        operation_id="exact-dr-gate",
        created_at="2026-07-20T00:00:00+00:00",
    )
    with pytest.raises(exact_dr.ExactDrError, match="排空"):
        await gate.enter_request("POST", "/api/jobs")
    with pytest.raises(exact_dr.ExactDrError, match="排空"):
        await gate.enter_request("GET", "/api/bili/login/poll")
    assert await gate.enter_request("GET", "/api/recovery") is False
    runner_entered = await gate.enter_request(
        "POST", "/api/runner/jobs/job/steps/step/complete",
    )
    transition = asyncio.create_task(
        gate.begin_snapshotting(data, operation_id="exact-dr-gate")
    )
    await asyncio.sleep(0)
    assert transition.done() is False
    await gate.leave_request(entered)
    await gate.leave_request(runner_entered)
    await transition
    with pytest.raises(exact_dr.ExactDrError, match="快照"):
        await gate.enter_request("POST", "/api/runner/heartbeat")
    with pytest.raises(exact_dr.ExactDrError, match="快照"):
        await gate.enter_request("GET", "/api/health/ready")
    assert await gate.enter_request("GET", "/api/recovery") is False
    await gate.finish(data, operation_id="exact-dr-gate")


@pytest.mark.asyncio
async def test_mutation_gate_times_out_hung_request(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    gate = exact_dr.ExactDrMutationGate()
    entered = await gate.enter_request("POST", "/api/jobs")
    await gate.begin_draining(
        data,
        operation_id="exact-dr-timeout",
        created_at="2026-07-20T00:00:00+00:00",
    )

    with pytest.raises(exact_dr.ExactDrError, match="在途请求排空超时"):
        await gate.begin_snapshotting(
            data,
            operation_id="exact-dr-timeout",
            timeout_sec=0.01,
        )

    await gate.leave_request(entered)
    await gate.finish(data, operation_id="exact-dr-timeout")


@pytest.mark.asyncio
async def test_begin_draining_joins_cancelled_acquire_before_owner_cleanup(
    tmp_path, monkeypatch,
):
    data = tmp_path / "data"
    data.mkdir()
    gate = exact_dr.ExactDrMutationGate()
    acquired = threading.Event()
    finish_acquire = threading.Event()
    real_acquire = exact_dr.acquire_barrier

    def delayed_acquire(*args, **kwargs):
        result = real_acquire(*args, **kwargs)
        acquired.set()
        finish_acquire.wait(timeout=5)
        return result

    monkeypatch.setattr(exact_dr, "acquire_barrier", delayed_acquire)
    task = asyncio.create_task(gate.begin_draining(
        data,
        operation_id="exact-dr-cancelled-acquire",
        created_at="2026-07-20T00:00:00+00:00",
    ))
    await asyncio.to_thread(acquired.wait, 5)
    task.cancel()
    finish_acquire.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert read_barrier(data) is None
    assert gate.phase is None


@pytest.mark.asyncio
async def test_stale_unknown_barrier_and_symlink_output_fail_closed(
    tmp_path, monkeypatch,
):
    data = tmp_path / "data"
    data.mkdir()
    acquire_barrier(
        data,
        operation_id="exact-dr-unknown",
        created_at="2026-07-20T00:00:00+00:00",
    )
    gate = exact_dr.ExactDrMutationGate()
    with pytest.raises(exact_dr.ExactDrError, match="无对应操作记录"):
        await gate.recover_stale(data)
    assert read_barrier(data)["operation_id"] == "exact-dr-unknown"

    real_output = tmp_path / "real-output"
    real_output.mkdir()
    output_alias = tmp_path / "output-alias"
    output_alias.symlink_to(real_output, target_is_directory=True)
    script = tmp_path / "dr_snapshot.py"
    script.write_text("# test placeholder\n", encoding="utf-8")
    monkeypatch.setenv(exact_dr.OUTPUT_ENV, str(output_alias))
    monkeypatch.setenv("FLORI_DEPLOYMENT_ID", "flori-test")
    monkeypatch.setenv("FLORI_EXACT_DR_SCRIPT", str(script))
    with pytest.raises(exact_dr.ExactDrError, match="符号链接"):
        exact_dr.validate_start_configuration()


@pytest.mark.asyncio
async def test_stale_owned_operation_removes_unverified_outputs(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    output = tmp_path / "exact-output"
    output.mkdir()
    monkeypatch.setenv(exact_dr.OUTPUT_ENV, str(output))
    operation = exact_dr.new_operation(data)
    acquire_barrier(
        data,
        operation_id=operation["id"],
        created_at=operation["created_at"],
    )
    for key in ("archive_name", "sidecar_name", "receipt_name"):
        (output / operation[key]).write_text("unverified", encoding="utf-8")

    gate = exact_dr.ExactDrMutationGate()
    await gate.recover_stale(data)

    recovered = exact_dr.read_operation(data)
    assert recovered["status"] == "interrupted"
    assert read_barrier(data) is None
    for key in ("archive_name", "sidecar_name", "receipt_name"):
        assert not (output / operation[key]).exists()


@pytest.mark.asyncio
async def test_restart_marks_operation_intent_without_barrier_interrupted(
    tmp_path, monkeypatch,
):
    data = tmp_path / "data"
    data.mkdir()
    output = tmp_path / "exact-output"
    output.mkdir()
    monkeypatch.setenv(exact_dr.OUTPUT_ENV, str(output))
    operation = exact_dr.new_operation(data)

    await exact_dr.ExactDrMutationGate().recover_stale(data)

    recovered = exact_dr.read_operation(data)
    assert recovered["id"] == operation["id"]
    assert recovered["status"] == "interrupted"
    assert read_barrier(data) is None


def test_operation_control_rejects_ancestor_symlink_fifo_and_hardlink(tmp_path):
    outside = tmp_path / "outside"
    data_real = outside / "data"
    data_real.mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(outside, target_is_directory=True)
    with pytest.raises(exact_dr.ExactDrError, match="safely open"):
        exact_dr.read_operation(alias / "data")
    assert not (data_real / "exact-dr-control").exists()

    data = tmp_path / "safe-data"
    data.mkdir()
    operation_path = exact_dr._operation_path(data)
    os.mkfifo(operation_path)
    with pytest.raises(exact_dr.ExactDrError, match="regular file"):
        exact_dr.read_operation(data)
    operation_path.unlink()

    outside_file = tmp_path / "outside-operation.json"
    outside_file.write_text("{}", encoding="utf-8")
    os.link(outside_file, operation_path)
    with pytest.raises(exact_dr.ExactDrError, match="regular file"):
        exact_dr.read_operation(data)


def test_receipt_publish_never_overwrites_or_accepts_swapped_pending(
    tmp_path, monkeypatch,
):
    output = tmp_path / "exact-output"
    output.mkdir()
    monkeypatch.setenv(exact_dr.OUTPUT_ENV, str(output))
    operation = exact_dr.new_operation(tmp_path / "data", persist=False)
    pending = exact_dr._prepare_pending_receipt_path(operation)
    original = b'{"status":"success"}\n'
    pending.write_bytes(original)
    final = output / operation["receipt_name"]
    final.write_bytes(b"existing")

    with pytest.raises(exact_dr.ExactDrError, match="已存在"):
        exact_dr._publish_receipt(
            operation,
            expected_sha256=hashlib.sha256(original).hexdigest(),
        )
    assert final.read_bytes() == b"existing"

    final.unlink()
    pending.write_bytes(b"swapped")
    with pytest.raises(exact_dr.ExactDrError, match="被替换"):
        exact_dr._publish_receipt(
            operation,
            expected_sha256=hashlib.sha256(original).hexdigest(),
        )
    assert not final.exists()
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "release_failure",
    [None, "release", "cleanup", "success-record", "pause-cancel", "late-holder"],
)
async def test_exact_dr_orchestrator_publishes_only_after_validate_and_release(
    tmp_path, monkeypatch, release_failure,
):
    data = tmp_path / "data"
    data.mkdir()
    connection = sqlite3.connect(data / "analyzer.db")
    connection.execute("CREATE TABLE fence_probe(value TEXT)")
    connection.commit()
    connection.close()
    output = tmp_path / "exact-output"
    output.mkdir()
    script = tmp_path / "dr_snapshot.py"
    script.write_text("# test placeholder\n", encoding="utf-8")
    monkeypatch.setenv(exact_dr.OUTPUT_ENV, str(output))
    monkeypatch.setenv("FLORI_DEPLOYMENT_ID", "flori-test")
    monkeypatch.setenv("FLORI_EXACT_DR_SCRIPT", str(script))
    monkeypatch.delenv("MINIO_URL", raising=False)
    monkeypatch.setattr(exact_dr, "validate_start_configuration", lambda: None)

    operation = exact_dr.new_operation(data)
    gate = exact_dr.ExactDrMutationGate()
    await gate.begin_draining(
        data,
        operation_id=operation["id"],
        created_at=operation["created_at"],
    )
    if release_failure in {"release", "cleanup"}:
        gate.finish = AsyncMock(side_effect=exact_dr.ExactDrError("release failed"))
    if release_failure == "cleanup":
        def fail_cleanup(_operation):
            raise OSError("permission denied")

        monkeypatch.setattr(exact_dr, "_cleanup_failed_outputs", fail_cleanup)
    redis = AsyncMock()
    redis.get_component_heartbeat.return_value = {"last_heartbeat": "now"}
    redis.get_all_holders_strict.return_value = set()
    if release_failure == "late-holder":
        redis.get_all_holders_strict.side_effect = [set(), set(), {"exec-late"}]
    if release_failure == "pause-cancel":
        redis.pause_writes_for_exact_dr.side_effect = asyncio.CancelledError
    db = SimpleNamespace(list_running_steps=lambda: [])
    storage = SimpleNamespace(wait_for_finalizers=AsyncMock())
    pause_background = AsyncMock()
    resume_background = AsyncMock()
    app = SimpleNamespace(state=SimpleNamespace(
        config=SimpleNamespace(data_dir=data, db_path=data / "analyzer.db"),
        exact_dr_gate=gate,
        redis=redis,
        db=db,
        storage=storage,
        pause_exact_dr_background_writers=pause_background,
        resume_exact_dr_background_writers=resume_background,
    ))

    async def scheduler_ack():
        while read_barrier(data)["phase"] != PHASE_SNAPSHOTTING:
            await asyncio.sleep(0)
        write_scheduler_quiesced(
            data,
            operation_id=operation["id"],
            at="2026-07-20T00:01:00+00:00",
        )

    async def fake_command(command, *, pass_fds=()):
        if "create" in command:
            assert command[command.index("--redis-mode") + 1] == "materialized-rdb-aof"
            sqlite_fd = int(command[command.index("--sqlite-source-fd") + 1])
            assert pass_fds == (sqlite_fd,)
            info = os.fstat(sqlite_fd)
            assert int(command[command.index("--sqlite-source-dev") + 1]) == info.st_dev
            assert int(command[command.index("--sqlite-source-ino") + 1]) == info.st_ino
            archive = Path(command[command.index("--output") + 1])
            receipt = Path(command[command.index("--result-file") + 1])
            archive.parent.mkdir(parents=True, exist_ok=True)
            archive.write_bytes(b"archive")
            digest = hashlib.sha256(b"archive").hexdigest()
            archive.with_suffix(archive.suffix + ".sha256").write_text(
                f"{digest}  {archive.name}\n", encoding="utf-8",
            )
            receipt.write_text(json.dumps({
                "status": "success",
                "operation": "backup",
                "generation": operation["generation"],
                "archive": str(archive),
                "archive_sha256": digest,
                "manifest": {
                    "format": "flori-disaster-recovery",
                    "format_version": 2,
                    "generation": operation["generation"],
                    "created_at": exact_dr.utc_now(),
                    "deployment": {"id": "flori-test"},
                    "assets": {
                        "data": {
                            "included": True,
                            "capture_mode": "stable-filesystem-copy+sqlite-online-backup",
                        },
                        "redis": {
                            "included": True,
                            "capture_mode": "materialized-rdb-aof",
                        },
                        "config": {
                            "included": True,
                            "capture_mode": "stable-filesystem-copy",
                        },
                    },
                },
            }), encoding="utf-8")
            return 0, "{}", ""
        assert pass_fds == ()
        return 0, json.dumps({
            "status": "success",
            "operation": "validate",
            "generation": operation["generation"],
            "deployment_id": "flori-test",
            "checks": {
                "members": "ok",
                "checksums": "ok",
                "sqlite": "ok",
                "compatibility": "ok",
            },
            "assets": {
                "data": {
                    "included": True,
                    "capture_mode": "stable-filesystem-copy+sqlite-online-backup",
                },
                "redis": {
                    "included": True,
                    "capture_mode": "materialized-rdb-aof",
                },
                "config": {
                    "included": True,
                    "capture_mode": "stable-filesystem-copy",
                },
            },
        }), ""

    monkeypatch.setattr(exact_dr, "_run_command", fake_command)
    if release_failure == "success-record":
        real_write_operation = exact_dr.write_operation

        def fail_success_record(data_dir, body):
            if body.get("status") == "success":
                raise OSError("operation storage read-only")
            return real_write_operation(data_dir, body)

        monkeypatch.setattr(exact_dr, "write_operation", fail_success_record)
    ack_task = asyncio.create_task(scheduler_ack())
    if release_failure == "pause-cancel":
        with pytest.raises(asyncio.CancelledError):
            await exact_dr.run_exact_dr(app, operation["id"])
    else:
        await exact_dr.run_exact_dr(app, operation["id"])
    await ack_task

    completed = exact_dr.read_operation(data)
    if release_failure in {"release", "cleanup"}:
        assert completed["status"] == "failed"
        if release_failure == "cleanup":
            assert "残留清理失败" in completed["error"]
            assert (output / operation["archive_name"]).exists()
            assert exact_dr._pending_receipt_path(operation).exists()
            assert not (output / operation["receipt_name"]).exists()
            with pytest.raises(LiveTargetError, match="同目录"):
                verify_dr_receipt(exact_dr._pending_receipt_path(operation))
        else:
            assert "三件套已删除" in completed["error"]
            assert not (output / operation["archive_name"]).exists()
            assert not (output / operation["sidecar_name"]).exists()
            assert not (output / operation["receipt_name"]).exists()
        assert gate.phase == PHASE_SNAPSHOTTING
        resume_background.assert_not_awaited()
    elif release_failure == "success-record":
        assert completed["status"] == "failed"
        assert "最终 receipt 发布失败" in completed["error"]
        assert gate.phase is None
        assert not (output / operation["receipt_name"]).exists()
        assert not exact_dr._pending_receipt_path(operation).exists()
        resume_background.assert_awaited_once()
    elif release_failure == "pause-cancel":
        assert completed["status"] == "interrupted"
        assert gate.phase is None
        assert not (output / operation["receipt_name"]).exists()
        resume_background.assert_awaited_once()
    elif release_failure == "late-holder":
        assert completed["status"] == "failed"
        assert "最终停写切面" in completed["error"]
        assert gate.phase is None
        assert not (output / operation["receipt_name"]).exists()
        resume_background.assert_awaited_once()
    else:
        assert completed["status"] == "success"
        assert completed["size_bytes"] == 7
        assert not exact_dr._pending_receipt_path(operation).exists()
        assert (output / operation["receipt_name"]).is_file()
        assert gate.phase is None
        resume_background.assert_awaited_once()
    pause_background.assert_awaited_once()
    redis.prepare_exact_dr_persistence.assert_awaited_once_with(timeout_sec=60)
    redis.pause_writes_for_exact_dr.assert_awaited_once()
    redis.resume_writes_after_exact_dr.assert_awaited_once()
    storage.wait_for_finalizers.assert_awaited_once()


def test_exact_dr_rejects_external_minio_endpoint(monkeypatch):
    monkeypatch.setenv("MINIO_URL", "https://objects.example.test:9000")

    with pytest.raises(exact_dr.ExactDrError, match="外部 MinIO"):
        exact_dr._uses_co_deployed_minio()
