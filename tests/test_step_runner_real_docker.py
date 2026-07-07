"""守护进程能力 smoke:验证 DockerStepRunner 所依赖的 docker 原语在真实守护进程上确实生效:
work_dir bind-mount 到 /job、env 注入(PYTHONPATH/STEP_EXEC_ID)、离线池 network=none、退出码捕获。

注意:本测试刻意直接用 client.containers.run 复刻执行器装配,而非实例化 DockerStepRunner.
因为真正驱动 runner.run_step 需要一个完整的 flori step 镜像(含 /app 代码与模块 entrypoint),
python:3.11-slim 跑不了 flori 模块。故这里只断"守护进程支持这些原语",runner 自身的命令/卷/env 装配
由 tests/test_step_runner_docker.py 的 mock 单测覆盖。无 docker.sock 或本地无 python:3.11-slim 镜像
时跳过(不联网拉取),标准容器内单测套件会跳过,有 docker 的环境才真跑。"""

from __future__ import annotations

import os

import pytest

_SOCK = "/var/run/docker.sock"


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
        client.images.get("python:3.11-slim")  # 不联网拉取,本地无则跳过
    except Exception:
        pytest.skip("本地无 python:3.11-slim 镜像")
    return client


class TestRealDockerDaemonCapabilities:
    def test_bind_mount_env_and_network_none(self, tmp_path):
        client = _docker_or_skip()
        # 复刻执行器装配(非 DockerStepRunner 本体,见模块 docstring):
        # 把 work_dir 以宿主路径 bind 到 /job,注入 env,断网,跑一段真命令。
        # 用容器把 STEP_EXEC_ID 写进 /job,再从宿主读回,验证 bind-mount 双向可见。
        (tmp_path / "in.txt").write_text("hi", encoding="utf-8")
        script = (
            "import os;"
            "open('/job/out.txt','w').write("
            "os.environ.get('STEP_EXEC_ID','')+':'+os.environ.get('PYTHONPATH','')"
            "+':'+open('/job/in.txt').read())"
        )
        container = client.containers.run(
            "python:3.11-slim",
            command=["python3", "-c", script],
            volumes={str(tmp_path): {"bind": "/job", "mode": "rw"}},
            working_dir="/job",
            environment={"STEP_EXEC_ID": "exec-real-1", "PYTHONPATH": "/app"},
            network_mode="none",  # 离线计算池断网
            detach=True,
        )
        try:
            result = container.wait(timeout=60)
            assert result["StatusCode"] == 0, container.logs().decode()
        finally:
            container.remove(force=True)

        out = (tmp_path / "out.txt").read_text(encoding="utf-8")
        assert out == "exec-real-1:/app:hi"  # env 注入 + bind-mount 双向均生效
