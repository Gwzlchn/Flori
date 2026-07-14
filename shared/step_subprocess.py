"""步骤外部命令执行组件。"""

from __future__ import annotations

import subprocess
import sys


class SubprocessFailed(subprocess.CalledProcessError):
    """保留 CalledProcessError 类型并在文本中附带 stderr 尾部。"""

    def __str__(self) -> str:
        base = super().__str__()
        tail = self.stderr or ""
        if isinstance(tail, (bytes, bytearray)):
            tail = tail.decode(errors="replace")
        tail = tail.strip()[-1500:]
        return f"{base}\nstderr(tail):\n{tail}" if tail else base


class SubprocessExecutor:
    """用既有参数执行命令并原样转发捕获的 stdout 和 stderr。"""

    def run(
        self, cmd: list[str], timeout: int = 600, **kwargs,
    ) -> subprocess.CompletedProcess:
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout, check=True, **kwargs,
            )
        except subprocess.CalledProcessError as exc:
            if exc.stdout:
                print(exc.stdout, flush=True)
            if exc.stderr:
                print(exc.stderr, file=sys.stderr, flush=True)
            raise SubprocessFailed(
                exc.returncode, exc.cmd, output=exc.output, stderr=exc.stderr,
            ) from exc
        if result.stdout:
            print(result.stdout, flush=True)
        if result.stderr:
            print(result.stderr, file=sys.stderr, flush=True)
        return result
