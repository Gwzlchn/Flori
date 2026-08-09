"""保持步骤session leader存活,直到目标进程与同组后代全部退出。"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
from pathlib import Path

_POLL_INTERVAL_SEC = 0.05
_EMPTY_SCANS_REQUIRED = 2
_PR_SET_CHILD_SUBREAPER = 36


def _set_child_subreaper() -> None:
    """让目标步骤的孤儿后代重挂到supervisor,不泄漏给容器PID 1。"""
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(_PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))


def _reap_exited_children() -> None:
    """回收已重挂的退出后代;活进程仍由procfs成员门追踪。"""
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            return


def _group_has_other_processes(process_group_id: int) -> bool:
    """读Linux procfs确认当前进程组是否还有supervisor之外的成员。"""
    own_pid = os.getpid()
    try:
        entries = list(Path("/proc").iterdir())
    except OSError:
        return True
    for entry in entries:
        if not entry.name.isdigit() or int(entry.name) == own_pid:
            continue
        try:
            stat = (entry / "stat").read_bytes()
            tail = stat[stat.rfind(b")") + 2:].split()
            # zombie已无执行能力且无法被signal清理;它在被PID 1回收前
            # 仍占用原PGID,因此忽略它不会导致数字PGID被提前复用。
            if (
                len(tail) >= 3
                and tail[0] != b"Z"
                and int(tail[2]) == process_group_id
            ):
                return True
        except (FileNotFoundError, ProcessLookupError, ValueError):
            continue
        except OSError:
            # 看不清成员归属时保持sentinel存活,由runner超时后清组。
            return True
    return False


def main() -> int:
    command = sys.argv[1:]
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        raise SystemExit("subprocess supervisor requires a command")

    _set_child_subreaper()
    target = subprocess.Popen(command)
    returncode = target.wait()
    process_group_id = os.getpgrp()
    empty_scans = 0
    while empty_scans < _EMPTY_SCANS_REQUIRED:
        _reap_exited_children()
        if _group_has_other_processes(process_group_id):
            empty_scans = 0
        else:
            empty_scans += 1
        if empty_scans < _EMPTY_SCANS_REQUIRED:
            time.sleep(_POLL_INTERVAL_SEC)
    _reap_exited_children()
    return returncode


if __name__ == "__main__":
    raise SystemExit(main())
