"""Runner Gateway 经生产 Worker 执行的真 Redis 与流式制品闭环."""

from __future__ import annotations

import json
import os

import pytest
from httpx import ASGITransport, AsyncClient

from api.main import create_app
from shared.models import Job, Step, StepStatus
from shared.runner_ops import current_task_lease
from shared.storage import GatewayStorage
from scheduler.scheduler import Scheduler
from worker.gateway_transport import GatewayTransport
from worker.step_runner import SubprocessStepRunner
from worker.worker import Worker


pytestmark = pytest.mark.integration


def _install_fake_step(tmp_path, monkeypatch) -> None:
    """只替换外部步骤进程,Worker/Gateway/Storage 全走生产实现."""
    (tmp_path / "fake_gateway_step.py").write_text(
        """
import argparse
import json
import os
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-dir", required=True)
    parser.add_argument("--step-config", required=True)
    args = parser.parse_args()
    work_dir = Path(args.job_dir)
    job = json.loads((work_dir / "job.json").read_text())

    output_dir = work_dir / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    result = b"failed-artifact" if job["fake_fail"] else b"completed-artifact"
    (output_dir / "result.bin").write_bytes(result)

    logs_dir = work_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    usage = [{
        "exec_id": "usage-" + os.environ["STEP_EXEC_ID"],
        "provider": "fake",
        "model": "fake-step",
        "job_id": job["id"],
        "step": "01_download",
        "input_tokens": 10,
        "output_tokens": 5,
        "cost_usd": 0.01,
        "created_at": "2026-07-14T00:00:00+00:00",
    }]
    (logs_dir / ".01_download.usage.json").write_text(json.dumps(usage))
    print("fake gateway step ran", flush=True)
    if job["fake_fail"]:
        (work_dir / ".01_download.error.json").write_text(json.dumps({
            "error_type": "processing",
            "message": "controlled failure",
        }))
        print("controlled failure", file=sys.stderr, flush=True)
        return 3
    return 0


raise SystemExit(main())
""".lstrip(),
        encoding="utf-8",
    )
    pythonpath = os.environ.get("PYTHONPATH", "")
    monkeypatch.setenv(
        "PYTHONPATH", f"{tmp_path}{os.pathsep}{pythonpath}" if pythonpath else str(tmp_path),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("step_fails", [False, True], ids=["complete", "failed"])
async def test_gateway_production_worker_execute_closes_task_lifecycle(
    db, test_config, tmp_path, integration_redis, monkeypatch, step_fails,
):
    """真实 request_step -> Worker.execute 覆盖完成、失败、用量与租约资源释放."""
    redis = integration_redis
    bootstrap_gate = "registration-" + "secret"
    await redis.set_registration_token(bootstrap_gate)
    app = create_app(db=db, redis=redis, config=test_config)
    control_client = AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway")
    data_client = AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway")

    worker_id_file = tmp_path / "worker.id"
    monkeypatch.setenv("WORKER_ID_FILE", str(worker_id_file))
    monkeypatch.setenv("STEP_RUNTIME", "subprocess")
    _install_fake_step(tmp_path, monkeypatch)

    raw_step = next(
        item for item in test_config.pipelines["video"]["steps"]
        if item["name"] == "01_download"
    )
    raw_step.update(
        module="fake_gateway_step", pool="cpu", timeout_sec=10, retries=0,
    )

    transport = GatewayTransport(
        "http://gateway",
        registration_token=bootstrap_gate,
        id_file=str(worker_id_file),
        token_file=str(tmp_path / "worker.token"),
        inner=None,
    )
    transport._client = control_client
    storage = GatewayStorage(
        "http://gateway", token_getter=lambda: transport.worker_token,
        work_dir=tmp_path / "work",
    )
    storage._client_obj = data_client
    worker = Worker(
        transport, test_config, storage, worker_type="cpu", pools=["cpu"],
        tags={"vision"}, reject_tags=set(), concurrency=1,
    )
    assert isinstance(worker.runner, SubprocessStepRunner)

    claim = None
    job_id = "j-e2e-failed" if step_fails else "j-e2e-complete"
    resource = "gateway-e2e-account"
    try:
        await worker.register()
        db.create_job(Job(
            id=job_id, content_type="video", pipeline="video", domain="general",
        ))
        db.upsert_step(Step(
            job_id=job_id, name="01_download", status=StepStatus.READY, pool="cpu",
        ))
        await redis.init_job(
            job_id, "video", {"source": "upload", "domain": "general", "style_tags": "[]"},
        )
        await redis.set_step_status(job_id, "01_download", "ready")
        await redis.set_resource_limits({resource: 1})
        await redis.enqueue_step(
            "cpu", job_id, "01_download", [], priority=0, resources=[resource],
        )
        job_payload = json.dumps({"id": job_id, "fake_fail": step_fails}).encode()
        await app.state.storage.write_file(job_id, "job.json", job_payload)

        claim = await worker.transport.request_step(
            worker.worker_id, worker.pools, worker._pool_limits(),
            worker.tags, worker.reject_tags,
        )
        assert claim is not None
        assert claim["job_id"] == job_id and claim["step"] == "01_download"
        assert current_task_lease() is not None
        assert await redis.validate_task_lease(
            worker.worker_id, job_id, "01_download", claim["exec_id"],
            expected_pool="cpu",
        )
        assert await redis.get_pool_count("cpu") == 1
        assert await redis.get_resource_count(resource) == 1

        await worker.execute(claim)

        # Worker 只把终态写入权威 Stream；Scheduler 是唯一 DB 终态写入者。
        events = await redis.read_lifecycle_events(
            f"gateway-e2e-{job_id}", block_ms=1, reclaim_idle_ms=0,
        )
        assert len(events) == 1
        message_id, fields = events[0]
        terminal = json.loads(fields["payload"])
        terminal["_stream_id"] = message_id
        await Scheduler(redis, db, test_config, storage=app.state.storage)._dispatch(terminal)
        await redis.ack_lifecycle_event(message_id)

        expected_artifact = b"failed-artifact" if step_fails else b"completed-artifact"
        assert await app.state.storage.read_file(
            job_id, "output/result.bin",
        ) == expected_artifact
        assert db.get_usage_summary(job_id=job_id)["calls"] == 1
        step = db.get_steps(job_id)[0]
        expected_status = StepStatus.FAILED if step_fails else StepStatus.DONE
        assert step.status == expected_status
        if step_fails:
            assert "controlled failure" in (step.error or "")

        worker_row = db.get_worker(worker.worker_id)
        assert worker_row is not None
        assert worker_row.tasks_failed == int(step_fails)
        assert worker_row.tasks_completed == int(not step_fails)
        assert await redis.get_pool_count("cpu") == 0
        assert await redis.get_resource_count(resource) == 0
        assert await redis.get_step_resources(job_id, "01_download") == []
        assert await redis.validate_released_task_lease(
            worker.worker_id, job_id, "01_download", claim["exec_id"], "cpu",
        )
        assert not await redis.validate_task_lease(
            worker.worker_id, job_id, "01_download", claim["exec_id"],
        )
        assert current_task_lease() is None
        assert transport._running == {}
        assert not (tmp_path / "work" / job_id).exists()
    finally:
        if claim is not None and current_task_lease() is not None:
            await transport.release(claim)
        await storage.close()
        await transport.close()
