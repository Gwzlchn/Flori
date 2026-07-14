"""SubprocessStepRunner 零回归 + 工厂选型测试。

证明从 worker._run_step 搬入 SubprocessStepRunner 后行为字节级不变:
配置写入/清理、stdout/stderr 流式落盘、失败尾部返回、超时标记、进度转发。
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from worker.step_runner import (
    DockerStepRunner,
    StepContext,
    SubprocessStepRunner,
    _BoundedLogWriter,
    create_step_runner,
)


# helpers


def _ctx(work_dir: Path, module: str, step: str = "A", timeout_sec: int = 10,
         pool: str = "cpu") -> StepContext:
    return StepContext(
        job_id="j_test",
        step=step,
        work_dir=work_dir,
        exec_id="x",
        step_cfg={"step": {"name": step, "pool": pool, "timeout_sec": timeout_sec, "retries": 1}},
        module=module,
        timeout_sec=timeout_sec,
        pool=pool,
    )


def _write_stub(root: Path, pkg: str, name: str, body: str) -> str:
    """造一个临时 step 模块,返回可 -m 导入的模块路径。"""
    mod_dir = root / pkg
    mod_dir.mkdir(exist_ok=True)
    (mod_dir / "__init__.py").write_text("")
    (mod_dir / f"{name}.py").write_text(body)
    return f"{pkg}.{name}"


async def _noop_progress(event: str, payload: dict) -> None:
    pass


async def _noop_tick() -> None:
    pass


@pytest.fixture
def with_pythonpath(tmp_path):
    """让子进程能 import 临时 stub 模块。"""
    orig = os.environ.copy()
    os.environ["PYTHONPATH"] = str(tmp_path) + os.pathsep + os.environ.get("PYTHONPATH", "")
    yield tmp_path
    os.environ.clear()
    os.environ.update(orig)


# 成功路径


class TestSubprocessSuccess:
    @pytest.mark.asyncio
    async def test_success_writes_config_and_collects_output(self, with_pythonpath):
        root = with_pythonpath
        work_dir = root / "j_test"
        work_dir.mkdir()
        module = _write_stub(
            root, "_stub_ok", "noop",
            "import sys\n"
            "from pathlib import Path\n"
            "Path('.A.done').touch()\n"
            "print('step_output_ok')\n"
            "sys.exit(0)\n",
        )
        # stub 在 work_dir 内运行(StepBase 现状由 --job-dir 决定 cwd;这里直接断言日志即可)
        runner = SubprocessStepRunner()
        rc, stderr = await runner.run_step(_ctx(work_dir, module), _noop_progress, _noop_tick)

        assert (rc, stderr) == (0, "")
        log = (work_dir / "logs" / "A.log").read_text()
        assert "step_output_ok" in log
        # 配置文件应被清理
        assert not (work_dir / ".A.config.json").exists()

    @pytest.mark.asyncio
    async def test_streams_stdout_and_stderr_merged(self, with_pythonpath):
        root = with_pythonpath
        work_dir = root / "j_stream"
        work_dir.mkdir()
        module = _write_stub(
            root, "_stub_mixed", "mixed",
            "import sys\n"
            "print('out_line_1')\n"
            "print('err_line_1', file=sys.stderr)\n"
            "sys.stdout.flush(); sys.stderr.flush()\n"
            "sys.exit(0)\n",
        )
        runner = SubprocessStepRunner()
        rc, stderr_tail = await runner.run_step(_ctx(work_dir, module), _noop_progress, _noop_tick)

        assert rc == 0
        log = (work_dir / "logs" / "A.log").read_text()
        assert "out_line_1" in log
        assert "[stderr] err_line_1" in log
        # 返回尾部不带前缀
        assert "err_line_1" in stderr_tail
        assert "[stderr]" not in stderr_tail

    @pytest.mark.asyncio
    async def test_log_visible_before_completion(self, with_pythonpath):
        root = with_pythonpath
        work_dir = root / "j_live"
        work_dir.mkdir()
        module = _write_stub(
            root, "_stub_slow", "slow",
            "import sys, time\n"
            "print('early_marker', flush=True)\n"
            "time.sleep(1.5)\n"
            "print('late_marker', flush=True)\n"
            "sys.exit(0)\n",
        )
        log_path = work_dir / "logs" / "A.log"
        early_seen = asyncio.Event()

        async def watch():
            for _ in range(60):
                if log_path.is_file() and "early_marker" in log_path.read_text():
                    early_seen.set()
                    return
                await asyncio.sleep(0.1)

        watcher = asyncio.create_task(watch())
        runner = SubprocessStepRunner()
        rc, _ = await runner.run_step(_ctx(work_dir, module), _noop_progress, _noop_tick)
        watcher.cancel()

        assert rc == 0
        assert early_seen.is_set(), "log was not visible mid-run (not streaming)"
        full = log_path.read_text()
        assert "early_marker" in full and "late_marker" in full

    @pytest.mark.asyncio
    async def test_log_is_bounded_and_keeps_tail(self, with_pythonpath, monkeypatch):
        root = with_pythonpath
        work_dir = root / "j_bounded"
        work_dir.mkdir()
        monkeypatch.setenv("FLORI_STEP_LOG_MAX_BYTES", "2048")
        module = _write_stub(
            root, "_stub_loud", "loud",
            "print('old-marker-' + 'x' * 4096)\n"
            "print('tail-marker')\n",
        )

        runner = SubprocessStepRunner()
        rc, _ = await runner.run_step(_ctx(work_dir, module), _noop_progress, _noop_tick)

        log_path = work_dir / "logs" / "A.log"
        assert rc == 0
        assert log_path.stat().st_size <= 2048
        text = log_path.read_text()
        assert "older step log truncated" in text
        assert "tail-marker" in text


class TestBoundedLogWriter:
    def test_large_write_is_capped_and_keeps_newest_tail(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FLORI_STEP_LOG_MAX_BYTES", "1024")
        path = tmp_path / "step.log"
        writer = _BoundedLogWriter(path)
        writer.write(b"old-marker\n" + b"x" * 4096 + b"\nnewest-tail\n")
        writer.close()

        assert path.stat().st_size <= 1024
        text = path.read_text(errors="replace")
        assert "older step log truncated" in text
        assert "newest-tail" in text
        assert "old-marker" not in text

    def test_thousands_of_small_writes_compact_amortized(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FLORI_STEP_LOG_MAX_BYTES", "1024")
        path = tmp_path / "step.log"
        writer = _BoundedLogWriter(path)
        compact_count = 0
        original = writer._compact

        def counted_compact(incoming=b""):
            nonlocal compact_count
            compact_count += 1
            original(incoming)

        writer._compact = counted_compact
        for number in range(5000):
            writer.write(f"{number:04d}\n")
            assert path.stat().st_size <= 1024
        writer.close()

        assert compact_count < 100
        assert path.stat().st_size <= 1024
        assert "4999" in path.read_text(errors="replace")

    def test_oversized_chunk_never_hits_real_path_above_cap(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FLORI_STEP_LOG_MAX_BYTES", "1024")
        path = tmp_path / "step.log"
        writer = _BoundedLogWriter(path)
        inner = writer._file
        observed_sizes: list[int] = []

        class ObservedFile:
            def write(self, data):
                result = inner.write(data)
                inner.flush()
                observed_sizes.append(path.stat().st_size)
                assert observed_sizes[-1] <= 1024
                return result

            def __getattr__(self, name):
                return getattr(inner, name)

        writer._file = ObservedFile()
        writer.write(b"x" * 8192 + b"latest-tail")
        writer.close()

        assert path.stat().st_size <= 1024
        assert "latest-tail" in path.read_text(errors="replace")
        # 超大块走临时低水位文件原子替换,不得先写进真实路径.
        assert observed_sizes == []


# 失败路径


class TestSubprocessFailure:
    @pytest.mark.asyncio
    async def test_failure_returns_stderr_tail(self, with_pythonpath):
        root = with_pythonpath
        work_dir = root / "j_fail"
        work_dir.mkdir()
        module = _write_stub(
            root, "_stub_boom", "boom",
            "import sys\n"
            "from pathlib import Path\n"
            "Path('.A.error.json').write_text('{\"error_type\": \"processing\"}')\n"
            "print('boom_reason', file=sys.stderr, flush=True)\n"
            "sys.exit(3)\n",
        )
        # error.json 落在 cwd;子进程 cwd 不在 work_dir,故仅断言返回值与日志。
        runner = SubprocessStepRunner()
        rc, stderr_tail = await runner.run_step(_ctx(work_dir, module), _noop_progress, _noop_tick)

        assert rc == 3
        assert "boom_reason" in stderr_tail
        assert "[stderr] boom_reason" in (work_dir / "logs" / "A.log").read_text()


# 超时路径


class TestSubprocessTimeout:
    @pytest.mark.asyncio
    async def test_timeout_marks_log_and_raises(self, with_pythonpath):
        root = with_pythonpath
        work_dir = root / "j_to"
        work_dir.mkdir()
        module = _write_stub(
            root, "_stub_hang", "hang",
            "import time\n"
            "print('before_hang', flush=True)\n"
            "time.sleep(30)\n",
        )
        runner = SubprocessStepRunner()
        with pytest.raises(asyncio.TimeoutError):
            await runner.run_step(_ctx(work_dir, module, timeout_sec=1), _noop_progress, _noop_tick)

        log = (work_dir / "logs" / "A.log").read_text()
        assert "before_hang" in log
        assert "--- TIMEOUT after 1s ---" in log


# 进度转发


class TestSubprocessProgress:
    @pytest.mark.asyncio
    async def test_progress_forwarded_and_tick_called(self, with_pythonpath, monkeypatch):
        root = with_pythonpath
        work_dir = root / "j_prog"
        work_dir.mkdir()
        # 预写进度文件,monitor 读后转发。
        (work_dir / ".A.progress").write_text(
            json.dumps({"current": 3, "total": 10, "pct": 30, "message": "halfway"})
        )
        module = _write_stub(
            root, "_stub_prog", "prog",
            "import time\n"
            "time.sleep(0.6)\n",
        )

        progress_calls: list[tuple[str, dict]] = []
        tick_calls: list[int] = []

        async def on_progress(event: str, payload: dict) -> None:
            progress_calls.append((event, payload))

        async def on_tick() -> None:
            tick_calls.append(1)

        # 用可观测屏障释放 monitor 的第一拍;后续拍停在未置位 Event,直到 runner
        # 取消 monitor.不能用短 sleep 猜子进程与 monitor 的调度先后.
        real_sleep = asyncio.sleep
        monitor_waiting = asyncio.Event()
        release_monitor = asyncio.Event()
        park_later_cycles = asyncio.Event()
        monitor_cycles = 0

        async def gated_sleep(secs):
            nonlocal monitor_cycles
            if secs != 10:
                await real_sleep(secs)
                return
            monitor_cycles += 1
            if monitor_cycles == 1:
                monitor_waiting.set()
                await release_monitor.wait()
            else:
                await park_later_cycles.wait()

        monkeypatch.setattr("worker.step_runner.asyncio.sleep", gated_sleep)
        runner = SubprocessStepRunner()
        run_task = asyncio.create_task(
            runner.run_step(_ctx(work_dir, module), on_progress, on_tick)
        )
        try:
            await asyncio.wait_for(monitor_waiting.wait(), timeout=1)
            release_monitor.set()
            await run_task
        finally:
            if not run_task.done():
                run_task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await run_task

        assert tick_calls, "on_tick should be called each cycle"
        assert progress_calls, "on_progress should be called with progress data"
        event, payload = progress_calls[0]
        assert event == "step_progress"
        assert payload == {
            "step": "A", "current": 3, "total": 10, "pct": 30, "message": "halfway",
        }
        # heartbeat 应写回进度文件,不丢 current/total。
        written = json.loads((work_dir / ".A.progress").read_text())
        assert "worker_heartbeat_at" in written
        assert written["current"] == 3


# env 按需下放(密钥按需注入)


# stub:把自身可见的 os.environ 落到 work_dir/env_dump.json,供断言子进程实际继承了什么。
_ENV_DUMP_STUB = (
    "import json, os, sys\n"
    "from pathlib import Path\n"
    "Path(sys.argv[sys.argv.index('--job-dir') + 1], 'env_dump.json')"
    ".write_text(json.dumps(dict(os.environ)))\n"
    "sys.exit(0)\n"
)


class TestSubprocessEnvHardening:
    """DENYLIST:剥离控制面/AI 密钥但保留系统 env;ai 池才下放 AI 密钥。"""

    @pytest.mark.asyncio
    async def test_non_ai_pool_strips_secrets_keeps_system(self, with_pythonpath, monkeypatch):
        root = with_pythonpath
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-anthropic-secret")
        monkeypatch.setenv("MINIO_SECRET_KEY", "minio-secret")
        monkeypatch.setenv("MINIO_ACCESS_KEY", "minio-access")
        monkeypatch.setenv("REDIS_URL", "redis://localhost:6379")
        monkeypatch.setenv("GATEWAY_URL", "http://gateway:8000")
        monkeypatch.setenv("WORKER_TOKEN", "worker-secret")
        monkeypatch.setenv("HTTPS_PROXY", "http://proxy:7890")

        work_dir = root / "j_env_cpu"
        work_dir.mkdir()
        module = _write_stub(root, "_stub_env_cpu", "dump", _ENV_DUMP_STUB)
        runner = SubprocessStepRunner()
        rc, _ = await runner.run_step(
            _ctx(work_dir, module, pool="cpu"), _noop_progress, _noop_tick
        )
        assert rc == 0
        env = json.loads((work_dir / "env_dump.json").read_text())

        # 控制面密钥 + AI 密钥(非 ai 池)必须不可见。
        assert "ANTHROPIC_API_KEY" not in env
        assert "MINIO_SECRET_KEY" not in env
        assert "MINIO_ACCESS_KEY" not in env
        assert "REDIS_URL" not in env
        assert "GATEWAY_URL" not in env
        assert "WORKER_TOKEN" not in env
        # 系统变量(exec python/ffmpeg 必需)必须保留。
        assert "PATH" in env and env["PATH"]
        # 始终下放的运行期变量。
        assert env["STEP_EXEC_ID"] == "x"
        assert env["HTTPS_PROXY"] == "http://proxy:7890"

    @pytest.mark.asyncio
    async def test_ai_pool_sees_ai_keys_not_control_plane(self, with_pythonpath, monkeypatch):
        root = with_pythonpath
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-anthropic-secret")
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-secret")
        monkeypatch.setenv("MINIO_SECRET_KEY", "minio-secret")
        monkeypatch.setenv("HTTPS_PROXY", "http://proxy:7890")

        work_dir = root / "j_env_ai"
        work_dir.mkdir()
        module = _write_stub(root, "_stub_env_ai", "dump", _ENV_DUMP_STUB)
        runner = SubprocessStepRunner()
        rc, _ = await runner.run_step(
            _ctx(work_dir, module, pool="ai"), _noop_progress, _noop_tick
        )
        assert rc == 0
        env = json.loads((work_dir / "env_dump.json").read_text())

        # ai 池:AI 密钥按需下放。
        assert env["ANTHROPIC_API_KEY"] == "sk-anthropic-secret"
        assert env["DEEPSEEK_API_KEY"] == "sk-deepseek-secret"
        # 但控制面密钥仍不可见(步骤永不直连 MinIO)。
        assert "MINIO_SECRET_KEY" not in env
        assert env["STEP_EXEC_ID"] == "x"
        assert env["HTTPS_PROXY"] == "http://proxy:7890"


# 工厂选型


class TestFactory:
    def test_default_is_subprocess(self, monkeypatch):
        monkeypatch.delenv("STEP_RUNTIME", raising=False)
        assert isinstance(create_step_runner("w1"), SubprocessStepRunner)

    def test_explicit_subprocess(self, monkeypatch):
        monkeypatch.setenv("STEP_RUNTIME", "subprocess")
        assert isinstance(create_step_runner("w1"), SubprocessStepRunner)

    def test_docker_runtime(self, monkeypatch):
        monkeypatch.setenv("STEP_RUNTIME", "docker")

        class _FakeDockerModule:
            @staticmethod
            def from_env():
                return object()

        import sys

        monkeypatch.setitem(sys.modules, "docker", _FakeDockerModule)
        runner = create_step_runner("w1")
        assert isinstance(runner, DockerStepRunner)
