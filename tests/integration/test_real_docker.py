"""在真实 Docker daemon 上验证 DockerStepRunner 的完整容器边界."""

from __future__ import annotations

import asyncio
import os
import shutil
import threading
import uuid
from pathlib import Path

import docker
import pytest

from worker.step_runner import DockerStepRunner, StepContext


pytestmark = pytest.mark.integration


async def _noop_progress(_event: str, _payload: dict) -> None:
    pass


async def _noop_tick() -> None:
    pass


@pytest.mark.asyncio
async def test_real_runner_uses_bounded_local_driver_and_preserves_tail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """驱动真实动态容器,验证 DooD、隔离、日志尾部和清理闭环."""
    socket = Path("/var/run/docker.sock")
    assert socket.is_socket(), "integration 栈必须挂载 Docker socket"

    image = os.environ["DOCKER_TEST_IMAGE"]
    host_tmp = Path(os.environ["INTEGRATION_HOST_TMP"])
    suffix = uuid.uuid4().hex
    runner_root = host_tmp / f"runner-{suffix}"
    work_dir = runner_root / "job-real"
    work_dir.mkdir(parents=True)
    (work_dir / "in.txt").write_text("bind-visible", encoding="utf-8")
    (work_dir / "real_step.py").write_text(
        "import os, time\n"
        "from pathlib import Path\n"
        "job = Path('/job')\n"
        "value = job.joinpath('in.txt').read_text()\n"
        "job.joinpath('out.txt').write_text(value + ':' + os.environ['STEP_EXEC_ID'])\n"
        "print('real-runner-start:' + value, flush=True)\n"
        "time.sleep(1.5)\n"
        "print('real-runner-tail', flush=True)\n",
        encoding="utf-8",
    )

    client = docker.from_env()
    runner: DockerStepRunner | None = None
    task: asyncio.Task[tuple[int, str]] | None = None
    job_id = f"real-{suffix}"
    try:
        assert client.ping() is True
        client.images.get(image)
        ctx = StepContext(
            job_id=job_id,
            step="real",
            work_dir=work_dir,
            exec_id=f"exec-{suffix}",
            step_cfg={
                "step": {
                    "name": "real",
                    "pool": "cpu",
                    "timeout_sec": 20,
                    "retries": 1,
                }
            },
            module="real_step",
            image=image,
            timeout_sec=20,
            pool="cpu",
        )
        # integration compose 把 INTEGRATION_HOST_TMP 以同路径挂进测试容器,
        # 因而这里既是容器内工作根,也是 Docker daemon 可见的宿主根.
        runner = DockerStepRunner(f"worker-{suffix}", host_work_root=str(runner_root))
        created = threading.Event()
        holder: dict[str, object] = {}
        container_collection = runner._client.containers
        collection_type = type(container_collection)
        real_run = collection_type.run

        def run_and_signal(collection, *args, **kwargs):
            container = real_run(collection, *args, **kwargs)
            holder["container"] = container
            created.set()
            return container

        # DockerClient.containers 每次访问都会新建 collection,故在该 collection
        # 类型上包真实 run；monkeypatch 会在用例结束时自动还原。
        monkeypatch.setattr(collection_type, "run", run_and_signal)
        task = asyncio.create_task(runner.run_step(ctx, _noop_progress, _noop_tick))
        assert await asyncio.to_thread(created.wait, 5), (
            "DockerStepRunner 未在 5 秒内创建动态容器"
        )
        dynamic = holder["container"]
        dynamic.reload()

        host_config = dynamic.attrs["HostConfig"]
        assert host_config["LogConfig"] == {
            "Type": "local",
            "Config": {
                "max-size": "10m",
                "max-file": "3",
                "compress": "true",
            },
        }
        assert host_config["NetworkMode"] == "none"
        assert any(
            mount.get("Source") == str(work_dir)
            and mount.get("Destination") == "/job"
            and mount.get("RW") is True
            for mount in dynamic.attrs.get("Mounts") or []
        )

        returncode, tail = await task
        assert returncode == 0
        assert "real-runner-tail" in tail
        assert (work_dir / "out.txt").read_text(encoding="utf-8") == (
            f"bind-visible:exec-{suffix}"
        )
        log_text = (work_dir / "logs" / "real.log").read_text(encoding="utf-8")
        assert "real-runner-start:bind-visible" in log_text
        assert "real-runner-tail" in log_text
        assert not (work_dir / ".real.config.json").exists()
        assert client.containers.list(
            all=True,
            filters={"label": f"flori.job={job_id}"},
        ) == []
    finally:
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        for container in client.containers.list(
            all=True,
            filters={"label": f"flori.job={job_id}"},
        ):
            container.remove(force=True)
        if runner is not None:
            runner._client.close()
        client.close()
        shutil.rmtree(runner_root, ignore_errors=True)
