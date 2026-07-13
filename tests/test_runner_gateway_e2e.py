"""Runner Gateway 控制面与流式制品面的真实进程内闭环."""

from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from api.main import create_app
from shared.models import AIUsage, Job, Step, StepStatus
from shared.storage import GatewayStorage
from tests.conftest import make_fakeredis
from worker.gateway_transport import GatewayTransport


@pytest.mark.asyncio
async def test_gateway_claim_artifact_usage_terminal_release_e2e(
    db, test_config, tmp_path,
):
    redis = make_fakeredis()
    await redis.set_registration_token("registration-secret")
    app = create_app(db=db, redis=redis, config=test_config)
    control_client = AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway")
    data_client = AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway")

    registration_gate = "registration-" + "secret"
    download_credential = "bili_" + "sess" + "data"
    transport = GatewayTransport(
        "http://gateway",
        registration_token=registration_gate,
        id_file=str(tmp_path / "worker.id"),
        token_file=str(tmp_path / "worker.token"),
        inner=None,
    )
    transport._client = control_client
    storage = GatewayStorage(
        "http://gateway", token_getter=lambda: transport.worker_token,
        work_dir=tmp_path / "work",
    )
    storage._client_obj = data_client

    try:
        worker_id = await transport.register(
            "cpu-e2e", "cpu", ["cpu"], {"vision"}, set(), "host-e2e",
            datetime.now(timezone.utc), concurrency=1, spec={"test": True},
        )
        db.create_job(Job(
            id="j-e2e", content_type="video", pipeline="video", domain="general",
        ))
        db.upsert_step(Step(
            job_id="j-e2e", name="01_download", status=StepStatus.READY, pool="cpu",
        ))
        await redis.init_job("j-e2e", "video", {"source": "bilibili"})
        await redis.set_step_status("j-e2e", "01_download", "ready")
        await redis.enqueue_step("cpu", "j-e2e", "01_download", [], priority=0)
        await app.state.storage.write_file("j-e2e", "job.json", b'{"id":"j-e2e"}')

        claim = await transport.request_step(
            worker_id, ["cpu"], {"cpu": 1}, {"vision"}, set(),
        )
        assert claim["job_id"] == "j-e2e" and claim["step"] == "01_download"
        assert await redis.validate_task_lease(
            worker_id, "j-e2e", "01_download", claim["exec_id"], expected_pool="cpu",
        )

        work_dir = await storage.pull("j-e2e", "01_download")
        assert (work_dir / "job.json").read_bytes() == b'{"id":"j-e2e"}'
        (work_dir / "output").mkdir()
        (work_dir / "output" / "result.bin").write_bytes(b"streamed-result")
        await storage.push("j-e2e", "01_download", work_dir)
        assert await app.state.storage.read_file(
            "j-e2e", "output/result.bin",
        ) == b"streamed-result"

        assert await transport.get_credential(download_credential) is None
        await transport.publish_step_event(
            "events:j-e2e", {"event": "step_log", "line": "running"},
        )
        await transport.record_ai_usage(AIUsage(
            exec_id="call-e2e", provider="anthropic", model="claude",
            job_id="j-e2e", step="01_download", worker_id=worker_id,
            input_tokens=10, output_tokens=5, cost_usd=0.01,
        ))
        await transport.report_done(claim, 1.0, 0.0)
        await transport.release(claim)

        assert db.get_steps("j-e2e")[0].status == StepStatus.DONE
        assert db.get_usage_summary(job_id="j-e2e")["calls"] == 1
        assert await redis.get_pool_count("cpu") == 0
        assert await redis.validate_released_task_lease(
            worker_id, "j-e2e", "01_download", claim["exec_id"], "cpu",
        )
    finally:
        await storage.close()
        await transport.close()
        await redis.close()
