"""DockerStepRunner 的真实守护进程验收.

测试容器经 docker.sock 驱动宿主守护进程,并用宿主可见临时目录验证 DooD bind、
动态容器 local 日志驱动、离线网络、退出码和完整日志尾部.无守护进程或本地基础
镜像时跳过,绝不联网拉取.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import uuid
from pathlib import Path

import pytest

from worker.step_runner import DockerStepRunner, StepContext


_SOCK = "/var/run/docker.sock"
_CONTAINER_ROOT = Path("/real-docker-host")
_HOST_ROOT = Path(os.environ.get("FLORI_TEST_HOST_TMP", "/tmp/flori-test-real-docker"))


def _docker_or_skip():
    if not os.path.exists(_SOCK):
        pytest.skip("无 /var/run/docker.sock")
    try:
        import docker
    except ImportError:
        pytest.skip("无 docker SDK")
    try:
        client = docker.from_env()
        client.ping()
    except Exception:
        pytest.skip("docker 守护进程不可达")
    try:
        client.images.get("python:3.11-slim")
    except Exception:
        pytest.skip("本地无 python:3.11-slim 镜像")
    return client


async def _noop_progress(_event, _payload):
    pass


async def _noop_tick():
    pass


@pytest.mark.asyncio
async def test_real_runner_uses_bounded_local_driver_and_preserves_tail():
    client = _docker_or_skip()
    suffix = uuid.uuid4().hex
    container_root = _CONTAINER_ROOT / suffix
    host_root = _HOST_ROOT / suffix
    work_dir = container_root / "job-real"
    work_dir.mkdir(parents=True)
    (work_dir / "real_step.py").write_text(
        "import time\n"
        "print('real-runner-start', flush=True)\n"
        "time.sleep(1.5)\n"
        "print('real-runner-tail', flush=True)\n",
        encoding="utf-8",
    )
    ctx = StepContext(
        job_id=f"real-{suffix}",
        step="real",
        work_dir=work_dir,
        exec_id=f"exec-{suffix}",
        step_cfg={"step": {"name": "real", "pool": "cpu"}},
        module="real_step",
        image="python:3.11-slim",
        timeout_sec=20,
        pool="cpu",
    )
    runner = DockerStepRunner(f"worker-{suffix}", host_work_root=str(host_root))
    task = asyncio.create_task(runner.run_step(ctx, _noop_progress, _noop_tick))
    dynamic = None
    try:
        for _ in range(100):
            matches = client.containers.list(
                all=True, filters={"label": f"flori.job={ctx.job_id}"},
            )
            if matches:
                dynamic = matches[0]
                dynamic.reload()
                break
            await asyncio.sleep(0.05)
        assert dynamic is not None, "DockerStepRunner 未创建带 flori.job label 的容器"

        host_config = dynamic.attrs["HostConfig"]
        log_config = host_config["LogConfig"]
        assert log_config["Type"] == "local"
        assert log_config["Config"] == {
            "max-size": "10m", "max-file": "3", "compress": "true",
        }
        assert host_config["NetworkMode"] == "none"
        mounts = dynamic.attrs.get("Mounts") or []
        assert any(
            mount.get("Source") == str(host_root / "job-real")
            and mount.get("Destination") == "/job"
            for mount in mounts
        )

        returncode, tail = await task
        assert returncode == 0
        assert "real-runner-tail" in tail
        text = (work_dir / "logs" / "real.log").read_text(encoding="utf-8")
        assert "real-runner-start" in text and "real-runner-tail" in text
    finally:
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        for container in client.containers.list(
            all=True, filters={"label": f"flori.job={ctx.job_id}"},
        ):
            container.remove(force=True)
        shutil.rmtree(container_root, ignore_errors=True)
