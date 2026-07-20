"""exact DR 的跨进程停写屏障。"""

from __future__ import annotations

import json
import os
import re
import secrets
import stat
from contextlib import suppress
from pathlib import Path
from typing import Any, Mapping

from shared.step_manifest import canonical_json_bytes


BARRIER_FORMAT = "flori-exact-dr-barrier/v1"
CONTROL_DIR_NAME = "exact-dr-control"
BARRIER_NAME = "barrier.json"
SCHEDULER_ACK_NAME = "scheduler-quiesced.json"
PHASE_DRAINING = "draining"
PHASE_SNAPSHOTTING = "snapshotting"
_PHASES = frozenset({PHASE_DRAINING, PHASE_SNAPSHOTTING})
_OPERATION_ID_RE = re.compile(r"exact-dr-[A-Za-z0-9_.-]{1,96}")


class ExactDrBarrierError(RuntimeError):
    """停写屏障损坏、被替换或由其他操作持有时拒绝。"""


def _write_all(fd: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(fd, payload[offset:])
        if written <= 0:
            raise ExactDrBarrierError("exact DR control write did not make progress")
        offset += written


def _data_path(data_dir: str | Path) -> Path:
    data = Path(os.path.abspath(os.fspath(data_dir)))
    if not data.is_absolute():
        raise ExactDrBarrierError("DATA_DIR 必须是绝对路径")
    return data


def _open_directory_chain(path: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path.anchor or "/", flags)
    try:
        for component in path.parts[1:]:
            next_fd = os.open(component, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def open_control_root(data_dir: str | Path) -> int:
    """返回稳定 control-root dirfd;调用方负责 close。"""
    data = _data_path(data_dir)
    try:
        data_fd = _open_directory_chain(data)
    except OSError as exc:
        raise ExactDrBarrierError(f"cannot safely open DATA_DIR: {exc}") from exc
    try:
        try:
            os.mkdir(CONTROL_DIR_NAME, 0o700, dir_fd=data_fd)
        except FileExistsError:
            pass
        flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        root_fd = os.open(CONTROL_DIR_NAME, flags, dir_fd=data_fd)
        named = os.stat(CONTROL_DIR_NAME, dir_fd=data_fd, follow_symlinks=False)
        opened = os.fstat(root_fd)
        if (
            not stat.S_ISDIR(named.st_mode)
            or (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            os.close(root_fd)
            raise ExactDrBarrierError("exact DR control root identity changed")
        os.fchmod(root_fd, 0o700)
        return root_fd
    except OSError as exc:
        raise ExactDrBarrierError(f"cannot safely open exact DR control root: {exc}") from exc
    finally:
        os.close(data_fd)


def _control_root(data_dir: str | Path) -> Path:
    data = _data_path(data_dir)
    root_fd = open_control_root(data)
    os.close(root_fd)
    return data / CONTROL_DIR_NAME


def barrier_path(data_dir: str | Path) -> Path:
    return _control_root(data_dir) / BARRIER_NAME


def control_root(data_dir: str | Path) -> Path:
    """返回通过 DATA_DIR 祖先与目录身份校验的控制根。"""
    return _control_root(data_dir)


def _validate_barrier(body: Mapping[str, Any]) -> dict[str, Any]:
    expected = {"format", "operation_id", "phase", "created_at", "updated_at"}
    if set(body) != expected or body.get("format") != BARRIER_FORMAT:
        raise ExactDrBarrierError("exact DR barrier has an unsupported structure")
    operation_id = body.get("operation_id")
    if not isinstance(operation_id, str) or not _OPERATION_ID_RE.fullmatch(operation_id):
        raise ExactDrBarrierError("exact DR barrier has an invalid operation id")
    if body.get("phase") not in _PHASES:
        raise ExactDrBarrierError("exact DR barrier has an invalid phase")
    for key in ("created_at", "updated_at"):
        if not isinstance(body.get(key), str) or not body[key]:
            raise ExactDrBarrierError(f"exact DR barrier has an invalid {key}")
    encoded = canonical_json_bytes(dict(body))
    if len(encoded) > 4096:
        raise ExactDrBarrierError("exact DR barrier is too large")
    return dict(body)


def _read_barrier_at(directory_fd: int) -> dict[str, Any] | None:
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(BARRIER_NAME, flags, dir_fd=directory_fd)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ExactDrBarrierError(f"cannot open exact DR barrier: {exc}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 4096:
            raise ExactDrBarrierError("exact DR barrier is not a bounded regular file")
        raw = os.read(fd, 4097)
    finally:
        os.close(fd)
    try:
        body = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ExactDrBarrierError("exact DR barrier is corrupt") from exc
    if type(body) is not dict:
        raise ExactDrBarrierError("exact DR barrier must be an object")
    validated = _validate_barrier(body)
    if canonical_json_bytes(validated) != raw:
        raise ExactDrBarrierError("exact DR barrier is not canonical")
    return validated


def read_barrier(data_dir: str | Path) -> dict[str, Any] | None:
    directory_fd = open_control_root(data_dir)
    try:
        return _read_barrier_at(directory_fd)
    finally:
        os.close(directory_fd)


def acquire_barrier(
    data_dir: str | Path, *, operation_id: str, created_at: str,
) -> dict[str, Any]:
    body = _validate_barrier({
        "format": BARRIER_FORMAT,
        "operation_id": operation_id,
        "phase": PHASE_DRAINING,
        "created_at": created_at,
        "updated_at": created_at,
    })
    directory_fd = open_control_root(data_dir)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        try:
            fd = os.open(BARRIER_NAME, flags, 0o600, dir_fd=directory_fd)
        except FileExistsError as exc:
            current = _read_barrier_at(directory_fd)
            owner = current.get("operation_id") if current else "unknown"
            raise ExactDrBarrierError(f"exact DR barrier is already held by {owner}") from exc
        try:
            payload = canonical_json_bytes(body)
            _write_all(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return body


def advance_barrier(
    data_dir: str | Path, *, operation_id: str, phase: str, updated_at: str,
) -> dict[str, Any]:
    if phase not in _PHASES:
        raise ExactDrBarrierError(f"unsupported exact DR phase: {phase}")
    temporary = f".{BARRIER_NAME}.{secrets.token_hex(8)}.tmp"
    directory_fd = open_control_root(data_dir)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        current = _read_barrier_at(directory_fd)
        if current is None or current.get("operation_id") != operation_id:
            raise ExactDrBarrierError("exact DR barrier ownership changed")
        if current["phase"] == PHASE_SNAPSHOTTING and phase != PHASE_SNAPSHOTTING:
            raise ExactDrBarrierError("exact DR barrier phase cannot move backwards")
        updated = _validate_barrier({**current, "phase": phase, "updated_at": updated_at})
        fd = os.open(temporary, flags, 0o600, dir_fd=directory_fd)
        try:
            _write_all(fd, canonical_json_bytes(updated))
            os.fsync(fd)
        finally:
            os.close(fd)
        observed = _read_barrier_at(directory_fd)
        if observed is None or observed.get("operation_id") != operation_id:
            raise ExactDrBarrierError("exact DR barrier ownership changed before publish")
        os.replace(temporary, BARRIER_NAME, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        os.fsync(directory_fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        os.close(directory_fd)
    return updated


def release_barrier(data_dir: str | Path, *, operation_id: str) -> None:
    directory_fd = open_control_root(data_dir)
    try:
        current = _read_barrier_at(directory_fd)
        if current is None:
            return
        if current.get("operation_id") != operation_id:
            raise ExactDrBarrierError("refusing to release another exact DR operation's barrier")
        os.unlink(BARRIER_NAME, dir_fd=directory_fd)
        with suppress(FileNotFoundError):
            os.unlink(SCHEDULER_ACK_NAME, dir_fd=directory_fd)
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def barrier_phase(data_dir: str | Path) -> str | None:
    barrier = read_barrier(data_dir)
    return str(barrier["phase"]) if barrier is not None else None


def write_scheduler_quiesced(data_dir: str | Path, *, operation_id: str, at: str) -> None:
    """Scheduler 关闭 DB/Redis 后发布确认,API 只接受当前屏障 owner。"""
    payload = canonical_json_bytes({
        "format": BARRIER_FORMAT,
        "operation_id": operation_id,
        "service": "scheduler",
        "quiesced_at": at,
    })
    temporary = f".{SCHEDULER_ACK_NAME}.{secrets.token_hex(8)}.tmp"
    directory_fd = open_control_root(data_dir)
    try:
        current = _read_barrier_at(directory_fd)
        if (
            current is None
            or current.get("operation_id") != operation_id
            or current.get("phase") != PHASE_SNAPSHOTTING
        ):
            raise ExactDrBarrierError("scheduler ack does not match the snapshotting barrier")
        fd = os.open(
            temporary,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_fd,
        )
        try:
            _write_all(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(
            temporary,
            SCHEDULER_ACK_NAME,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        os.fsync(directory_fd)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=directory_fd)
        os.close(directory_fd)


def scheduler_quiesced(data_dir: str | Path, *, operation_id: str) -> bool:
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
    directory_fd = open_control_root(data_dir)
    try:
        fd = os.open(SCHEDULER_ACK_NAME, flags, dir_fd=directory_fd)
    except FileNotFoundError:
        os.close(directory_fd)
        return False
    except OSError as exc:
        os.close(directory_fd)
        raise ExactDrBarrierError(f"cannot open scheduler ack: {exc}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 4096:
            raise ExactDrBarrierError("scheduler ack is not a bounded regular file")
        raw = os.read(fd, 4097)
    finally:
        os.close(fd)
        os.close(directory_fd)
    try:
        body = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ExactDrBarrierError("scheduler ack is corrupt") from exc
    expected = {
        "format": BARRIER_FORMAT,
        "operation_id": operation_id,
        "service": "scheduler",
    }
    if type(body) is not dict or any(body.get(key) != value for key, value in expected.items()):
        raise ExactDrBarrierError("scheduler ack identity mismatch")
    if not isinstance(body.get("quiesced_at"), str) or not body["quiesced_at"]:
        raise ExactDrBarrierError("scheduler ack timestamp is invalid")
    if canonical_json_bytes(body) != raw:
        raise ExactDrBarrierError("scheduler ack is not canonical")
    return True
