"""DockerStepRunner 测试:用 mock client,不起真容器。

覆盖 command 同构、bind-mount、labels、GPU 门控、超时 kill、容器强删(必执行)、
孤儿清理、宿主路径前缀替换、网络策略(出网池 vs 离线池)、密钥白名单注入,
以及 use_gpu 布尔门控的四种组合。
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

import pytest

from worker.step_runner import DockerStepRunner, StepContext


# 桩:伪 docker SDK


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


def _ctx(
    work_dir: Path, *, use_gpu=False, image="flori/step-base", timeout_sec=10, pool="cpu"
) -> StepContext:
    return StepContext(
        job_id="j1",
        step="A",
        work_dir=work_dir,
        exec_id="e1",
        step_cfg={"step": {"name": "A", "timeout_sec": timeout_sec}},
        module="steps.video.step_03_scene",
        image=image,
        timeout_sec=timeout_sec,
        pool=pool,
        use_gpu=use_gpu,
    )


async def _noop_progress(event, payload):
    pass


async def _noop_tick():
    pass


# 成功路径:命令/挂载/labels


class TestDockerSuccess:
    @pytest.mark.asyncio
    async def test_command_volumes_labels(self, fake_docker, tmp_path, monkeypatch):
        monkeypatch.delenv("HTTPS_PROXY", raising=False)
        work_dir = tmp_path / "j1"
        work_dir.mkdir()
        container = _FakeContainer(status_code=0)
        fake_docker["client"] = _FakeClient(container)

        runner = DockerStepRunner("w1", host_work_root=str(tmp_path))
        rc, _ = await runner.run_step(_ctx(work_dir), _noop_progress, _noop_tick)

        assert rc == 0
        kw = runner._client.containers.run_kwargs
        assert kw["command"] == [
            "python3", "-m", "steps.video.step_03_scene",
            "--job-dir", "/job",
            "--step-config", "/job/.A.config.json",
        ]
        assert kw["working_dir"] == "/job"
        host_dir = str(tmp_path / "j1")
        assert kw["volumes"] == {host_dir: {"bind": "/job", "mode": "rw"}}
        assert kw["labels"] == {
            "flori.job": "j1", "flori.step": "A", "flori.worker": "w1",
        }
        assert kw["environment"] == {"STEP_EXEC_ID": "e1", "PYTHONPATH": "/app"}
        # cpu 池离线
        assert kw["network_mode"] == "none"
        # --step-config 走文件,不进 Cmd 之外的 env,杜绝 docker inspect 泄漏
        assert "step_cfg" not in kw["environment"]
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


# 网络策略:出网池 vs 离线池


class TestNetworkPolicy:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("pool", ["io", "ai"])
    async def test_networked_pools_default_network(self, fake_docker, tmp_path, monkeypatch, pool):
        monkeypatch.delenv("HTTPS_PROXY", raising=False)
        work_dir = tmp_path / "j1"
        work_dir.mkdir()
        container = _FakeContainer(status_code=0)
        fake_docker["client"] = _FakeClient(container)

        runner = DockerStepRunner("w1", host_work_root=str(tmp_path))
        await runner.run_step(_ctx(work_dir, pool=pool), _noop_progress, _noop_tick)

        # io 下载/ai 走默认网络,network_mode 不设(None)
        assert runner._client.containers.run_kwargs["network_mode"] is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("pool", ["scene", "cpu", "gpu"])
    async def test_offline_pools_network_none(self, fake_docker, tmp_path, monkeypatch, pool):
        monkeypatch.delenv("HTTPS_PROXY", raising=False)
        work_dir = tmp_path / "j1"
        work_dir.mkdir()
        container = _FakeContainer(status_code=0)
        fake_docker["client"] = _FakeClient(container)

        runner = DockerStepRunner("w1", host_work_root=str(tmp_path))
        await runner.run_step(_ctx(work_dir, pool=pool), _noop_progress, _noop_tick)

        assert runner._client.containers.run_kwargs["network_mode"] == "none"


# 密钥白名单:STEP_EXEC_ID/HTTPS_PROXY 恒注入,AI key 仅 ai 池


class TestSecretsWhitelist:
    @pytest.mark.asyncio
    async def test_step_exec_id_and_proxy_always_present(
        self, fake_docker, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("HTTPS_PROXY", "http://proxy:7890")
        work_dir = tmp_path / "j1"
        work_dir.mkdir()
        container = _FakeContainer(status_code=0)
        fake_docker["client"] = _FakeClient(container)

        runner = DockerStepRunner("w1", host_work_root=str(tmp_path))
        await runner.run_step(_ctx(work_dir, pool="cpu"), _noop_progress, _noop_tick)

        env = runner._client.containers.run_kwargs["environment"]
        assert env["STEP_EXEC_ID"] == "e1"
        assert env["HTTPS_PROXY"] == "http://proxy:7890"
        # 代码在镜像 /app,容器 working_dir=/job,必须显式 PYTHONPATH 才能找到 steps.* 模块。
        assert env["PYTHONPATH"] == "/app"

    @pytest.mark.asyncio
    async def test_proxy_absent_when_unset(self, fake_docker, tmp_path, monkeypatch):
        monkeypatch.delenv("HTTPS_PROXY", raising=False)
        work_dir = tmp_path / "j1"
        work_dir.mkdir()
        container = _FakeContainer(status_code=0)
        fake_docker["client"] = _FakeClient(container)

        runner = DockerStepRunner("w1", host_work_root=str(tmp_path))
        await runner.run_step(_ctx(work_dir, pool="cpu"), _noop_progress, _noop_tick)

        env = runner._client.containers.run_kwargs["environment"]
        assert "HTTPS_PROXY" not in env

    @pytest.mark.asyncio
    async def test_ai_keys_injected_for_ai_pool(self, fake_docker, tmp_path, monkeypatch):
        monkeypatch.delenv("HTTPS_PROXY", raising=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-xxx")
        monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-yyy")
        # 未设置的 key 不应出现
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("OLLAMA_URL", raising=False)
        work_dir = tmp_path / "j1"
        work_dir.mkdir()
        container = _FakeContainer(status_code=0)
        fake_docker["client"] = _FakeClient(container)

        runner = DockerStepRunner("w1", host_work_root=str(tmp_path))
        await runner.run_step(_ctx(work_dir, pool="ai"), _noop_progress, _noop_tick)

        env = runner._client.containers.run_kwargs["environment"]
        assert env["ANTHROPIC_API_KEY"] == "sk-ant-xxx"
        assert env["DEEPSEEK_API_KEY"] == "ds-yyy"
        # env 里没有的 key 不注入
        assert "OPENAI_API_KEY" not in env
        assert "OLLAMA_URL" not in env

    @pytest.mark.asyncio
    @pytest.mark.parametrize("pool", ["io", "cpu", "scene", "gpu"])
    async def test_ai_keys_absent_for_non_ai_pools(
        self, fake_docker, tmp_path, monkeypatch, pool
    ):
        monkeypatch.delenv("HTTPS_PROXY", raising=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-xxx")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-zzz")
        monkeypatch.setenv("OLLAMA_URL", "http://ollama:11434")
        work_dir = tmp_path / "j1"
        work_dir.mkdir()
        container = _FakeContainer(status_code=0)
        fake_docker["client"] = _FakeClient(container)

        runner = DockerStepRunner("w1", host_work_root=str(tmp_path))
        await runner.run_step(_ctx(work_dir, pool=pool), _noop_progress, _noop_tick)

        env = runner._client.containers.run_kwargs["environment"]
        # 非 ai 池:绝不见任何 AI 密钥
        for key in ("ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY", "OLLAMA_URL"):
            assert key not in env
        assert env["STEP_EXEC_ID"] == "e1"


# 失败/超时:remove 必执行


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


# 进度监控:on_tick 先于 progress 发布,10s 周期


class TestProgressMonitor:
    @pytest.mark.asyncio
    async def test_tick_then_progress_publish(self, fake_docker, tmp_path, monkeypatch):
        """单跑 _progress_monitor:每周期先 on_tick 续约,再读 .progress 发 step_progress。"""
        fake_docker["client"] = _FakeClient(None)
        runner = DockerStepRunner("w1")

        work_dir = tmp_path / "j1"
        work_dir.mkdir()
        (work_dir / ".A.progress").write_text(
            '{"current": 3, "total": 10, "pct": 30, "message": "half"}'
        )

        calls: list = []

        async def _tick():
            calls.append(("tick",))

        async def _progress(event, payload):
            calls.append(("progress", event, payload))

        # 把 sleep(10) 改瞬时,跑一周期后让 proc_alive 转 False。
        alive = {"v": True}

        async def _fast_sleep(_):
            alive["v"] = False

        monkeypatch.setattr("worker.step_runner.asyncio.sleep", _fast_sleep)

        await runner._progress_monitor(
            _ctx(work_dir, pool="cpu"), _progress, _tick, lambda: alive["v"],
        )

        # on_tick 必在 progress 发布之前
        assert calls[0] == ("tick",)
        assert calls[1][0] == "progress"
        assert calls[1][1] == "step_progress"
        assert calls[1][2] == {
            "step": "A", "current": 3, "total": 10, "pct": 30, "message": "half",
        }
        # 续约心跳应写回 .progress
        import json as _json
        data = _json.loads((work_dir / ".A.progress").read_text())
        assert "worker_heartbeat_at" in data


# 孤儿清理


class TestReapOrphans:
    def test_reap_removes_each_listed(self, fake_docker, tmp_path):
        c1 = _FakeContainer()
        c2 = _FakeContainer()
        fake_docker["client"] = _FakeClient(None, listed=[c1, c2])

        runner = DockerStepRunner("w1")
        runner.reap_orphans()

        assert c1.removed and c2.removed
        assert runner._client.containers.list_filters == {"label": "flori.worker=w1"}


# 宿主路径前缀替换


class TestHostPath:
    def test_with_host_root(self, fake_docker, tmp_path):
        fake_docker["client"] = _FakeClient(None)
        runner = DockerStepRunner("w1", host_work_root="/host/work")
        assert runner._host_path(Path("/tmp/flori-work/j_abc")) == "/host/work/j_abc"

    def test_without_host_root_fails_fast(self, fake_docker, tmp_path):
        # HOST_WORK_DIR 缺失 → fail-fast,不静默挂错误目录
        fake_docker["client"] = _FakeClient(None)
        runner = DockerStepRunner("w1", host_work_root=None)
        with pytest.raises(ValueError):
            runner._host_path(Path("/tmp/flori-work/j_abc"))


class TestResolveImage:
    def test_logical_name_to_registry(self, fake_docker):
        fake_docker["client"] = _FakeClient(None)
        runner = DockerStepRunner("w1", registry="ghcr.io/gwzlchn")
        assert runner._resolve_image("flori/step-base") == "ghcr.io/gwzlchn/flori-step-base"
        assert runner._resolve_image("flori/step-heavy") == "ghcr.io/gwzlchn/flori-step-heavy"

    def test_no_registry_keeps_logical_name(self, fake_docker):
        # 未设 registry:本机自建 flori/step-base 直接命中,不改写。
        fake_docker["client"] = _FakeClient(None)
        runner = DockerStepRunner("w1", registry=None)
        assert runner._resolve_image("flori/step-base") == "flori/step-base"

    def test_full_image_name_untouched(self, fake_docker):
        # 已是带 host 的全名(非 flori/ 前缀)原样使用。
        fake_docker["client"] = _FakeClient(None)
        runner = DockerStepRunner("w1", registry="ghcr.io/gwzlchn")
        assert runner._resolve_image("docker.io/library/python:3.11") == "docker.io/library/python:3.11"

# use_gpu 门控真值表在 tests/test_worker.py::TestUseGpuGating——
# 那里直接驱动真实 worker.execute 捕获 StepContext.use_gpu,此处不复刻表达式只测副本。
