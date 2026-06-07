"""DockerStepRunner 测试:用 mock client,不起真容器。

覆盖 command 同构、bind-mount、labels、GPU 门控、超时 kill、容器强删(必执行)、
孤儿清理、宿主路径前缀替换,以及 use_gpu 布尔门控的四种组合。
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

import pytest

from worker.step_runner import DockerStepRunner, StepContext


# ── 桩:伪 docker SDK ──


class _FakeContainer:
    def __init__(self, status_code=0, wait_delay=0.0, status="exited"):
        self._status_code = status_code
        self._wait_delay = wait_delay
        self.status = status
        self.killed = False
        self.removed = False
        self.remove_calls = 0

    def wait(self):
        import time
        if self._wait_delay:
            time.sleep(self._wait_delay)
        return {"StatusCode": self._status_code}

    def kill(self):
        self.killed = True

    def remove(self, force=False):
        self.removed = True
        self.remove_calls += 1

    def reload(self):
        pass

    def logs(self, stream=False, follow=False):
        return iter([b"line1\n", b"line2\n"])


class _FakeContainers:
    def __init__(self, container, listed=None):
        self._container = container
        self._listed = listed or []
        self.run_kwargs = None

    def run(self, **kwargs):
        self.run_kwargs = kwargs
        return self._container

    def list(self, all=False, filters=None):
        self.list_filters = filters
        return self._listed


class _FakeClient:
    def __init__(self, container=None, listed=None):
        self.containers = _FakeContainers(container, listed)


class _FakeDeviceRequest:
    def __init__(self, count=None, capabilities=None):
        self.count = count
        self.capabilities = capabilities

    def __eq__(self, other):
        return (
            isinstance(other, _FakeDeviceRequest)
            and self.count == other.count
            and self.capabilities == other.capabilities
        )


class _FakeAPIError(Exception):
    pass


@pytest.fixture
def fake_docker(monkeypatch):
    """注入伪 docker / docker.types / docker.errors,避免依赖真 SDK。"""
    docker_mod = types.ModuleType("docker")
    types_mod = types.ModuleType("docker.types")
    errors_mod = types.ModuleType("docker.errors")

    types_mod.DeviceRequest = _FakeDeviceRequest
    errors_mod.APIError = _FakeAPIError
    docker_mod.types = types_mod
    docker_mod.errors = errors_mod

    holder = {}

    def from_env():
        return holder["client"]

    docker_mod.from_env = from_env

    monkeypatch.setitem(sys.modules, "docker", docker_mod)
    monkeypatch.setitem(sys.modules, "docker.types", types_mod)
    monkeypatch.setitem(sys.modules, "docker.errors", errors_mod)
    return holder


def _ctx(work_dir: Path, *, use_gpu=False, image="mnemo/step-base", timeout_sec=10) -> StepContext:
    return StepContext(
        job_id="j1",
        step="A",
        work_dir=work_dir,
        exec_id="e1",
        step_cfg={"step": {"name": "A", "timeout_sec": timeout_sec}},
        module="steps.video.step_01_scene",
        image=image,
        timeout_sec=timeout_sec,
        pool="cpu",
        use_gpu=use_gpu,
    )


async def _noop_progress(event, payload):
    pass


async def _noop_tick():
    pass


# ── 成功路径:命令/挂载/labels ──


class TestDockerSuccess:
    @pytest.mark.asyncio
    async def test_command_volumes_labels(self, fake_docker, tmp_path):
        work_dir = tmp_path / "j1"
        work_dir.mkdir()
        container = _FakeContainer(status_code=0)
        fake_docker["client"] = _FakeClient(container)

        runner = DockerStepRunner("w1", host_work_root=str(tmp_path))
        rc, _ = await runner.run_step(_ctx(work_dir), _noop_progress, _noop_tick)

        assert rc == 0
        kw = runner._client.containers.run_kwargs
        assert kw["command"] == [
            "python3", "-m", "steps.video.step_01_scene",
            "--job-dir", "/job",
            "--step-config", "/job/.A.config.json",
        ]
        assert kw["working_dir"] == "/job"
        host_dir = str(tmp_path / "j1")
        assert kw["volumes"] == {host_dir: {"bind": "/job", "mode": "rw"}}
        assert kw["labels"] == {
            "mnemo.job": "j1", "mnemo.step": "A", "mnemo.worker": "w1",
        }
        assert kw["environment"] == {"STEP_EXEC_ID": "e1"}
        # 容器必被强删
        assert container.removed and container.remove_calls == 1

    @pytest.mark.asyncio
    async def test_use_gpu_true_adds_device_request(self, fake_docker, tmp_path):
        work_dir = tmp_path / "j1"
        work_dir.mkdir()
        container = _FakeContainer(status_code=0)
        fake_docker["client"] = _FakeClient(container)

        runner = DockerStepRunner("w1", host_work_root=str(tmp_path))
        await runner.run_step(_ctx(work_dir, use_gpu=True), _noop_progress, _noop_tick)

        dr = runner._client.containers.run_kwargs["device_requests"]
        assert dr == [_FakeDeviceRequest(count=-1, capabilities=[["gpu"]])]

    @pytest.mark.asyncio
    async def test_use_gpu_false_no_device_request(self, fake_docker, tmp_path):
        work_dir = tmp_path / "j1"
        work_dir.mkdir()
        container = _FakeContainer(status_code=0)
        fake_docker["client"] = _FakeClient(container)

        runner = DockerStepRunner("w1", host_work_root=str(tmp_path))
        await runner.run_step(_ctx(work_dir, use_gpu=False), _noop_progress, _noop_tick)

        assert runner._client.containers.run_kwargs["device_requests"] is None


# ── 失败/超时:remove 必执行 ──


class TestDockerCleanup:
    @pytest.mark.asyncio
    async def test_failure_still_removes(self, fake_docker, tmp_path):
        work_dir = tmp_path / "j1"
        work_dir.mkdir()
        container = _FakeContainer(status_code=1)
        fake_docker["client"] = _FakeClient(container)

        runner = DockerStepRunner("w1", host_work_root=str(tmp_path))
        rc, _ = await runner.run_step(_ctx(work_dir), _noop_progress, _noop_tick)

        assert rc == 1
        assert container.removed and container.remove_calls == 1

    @pytest.mark.asyncio
    async def test_timeout_kills_raises_and_removes(self, fake_docker, tmp_path):
        work_dir = tmp_path / "j1"
        work_dir.mkdir()
        container = _FakeContainer(status_code=0, wait_delay=2.0)
        fake_docker["client"] = _FakeClient(container)

        runner = DockerStepRunner("w1", host_work_root=str(tmp_path))
        with pytest.raises(asyncio.TimeoutError):
            await runner.run_step(_ctx(work_dir, timeout_sec=1), _noop_progress, _noop_tick)

        assert container.killed
        assert container.removed and container.remove_calls == 1
        # 超时标记应追加到日志
        log = (work_dir / "logs" / "A.log").read_text()
        assert "--- TIMEOUT after 1s ---" in log


# ── 孤儿清理 ──


class TestReapOrphans:
    def test_reap_removes_each_listed(self, fake_docker, tmp_path):
        c1 = _FakeContainer()
        c2 = _FakeContainer()
        fake_docker["client"] = _FakeClient(None, listed=[c1, c2])

        runner = DockerStepRunner("w1")
        runner.reap_orphans()

        assert c1.removed and c2.removed
        assert runner._client.containers.list_filters == {"label": "mnemo.worker=w1"}


# ── 宿主路径前缀替换 ──


class TestHostPath:
    def test_with_host_root(self, fake_docker, tmp_path):
        fake_docker["client"] = _FakeClient(None)
        runner = DockerStepRunner("w1", host_work_root="/host/work")
        assert runner._host_path(Path("/tmp/mnemo-work/j_abc")) == "/host/work/j_abc"

    def test_without_host_root(self, fake_docker, tmp_path):
        fake_docker["client"] = _FakeClient(None)
        runner = DockerStepRunner("w1", host_work_root=None)
        assert runner._host_path(Path("/tmp/mnemo-work/j_abc")) == "/tmp/mnemo-work/j_abc"


# ── use_gpu 布尔门控(worker.execute 内联表达式的四种组合) ──


def _use_gpu(tags: set[str], pool: str, raw_tags: list[str]) -> bool:
    """复刻 worker.execute 里的 use_gpu 计算,单测其真值表。"""
    return ("gpu" in tags) and (pool == "gpu" or "gpu" in set(raw_tags))


class TestUseGpuGating:
    def test_no_gpu_tag(self):
        assert _use_gpu(set(), "gpu", ["gpu"]) is False

    def test_gpu_tag_and_gpu_pool(self):
        assert _use_gpu({"gpu"}, "gpu", []) is True

    def test_gpu_tag_cpu_pool_no_raw_gpu(self):
        assert _use_gpu({"gpu"}, "cpu", []) is False

    def test_raw_tags_gpu_and_worker_gpu(self):
        assert _use_gpu({"gpu"}, "cpu", ["gpu"]) is True
