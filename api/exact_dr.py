"""在线 exact DR 的停写、排空、快照与校验编排。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import secrets
import sqlite3
import stat
import sys
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

import structlog

from shared.exact_dr_maintenance import (
    ExactDrBarrierError,
    PHASE_DRAINING,
    PHASE_SNAPSHOTTING,
    acquire_barrier,
    advance_barrier,
    control_root,
    open_control_root,
    read_barrier,
    release_barrier,
    scheduler_quiesced,
)
from shared.step_manifest import canonical_json_bytes
from shared.content_result import ResultFileError, ensure_output_roots_disjoint
from shared.version import FLORI_VERSION


logger = structlog.get_logger(component="exact-dr")

OPERATION_FORMAT = "flori-exact-dr-operation/v1"
OUTPUT_ENV = "FLORI_EXACT_DR_DIR"
DEFAULT_OUTPUT_PATH = "/exact-dr"
CONFIRMATION = "创建完整灾备"
_OPERATION_RE = re.compile(r"exact-dr-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}")
_DEPLOYMENT_RE = re.compile(r"[A-Za-z0-9_.-]{1,128}")
_TERMINAL = frozenset({"success", "failed", "interrupted"})
_STATES = _TERMINAL | {"draining", "snapshotting", "verifying"}
_DRAIN_ALLOWED_PREFIXES = ("/api/runner/",)


class ExactDrError(RuntimeError):
    """exact DR 配置、控制状态或子进程结果不可信时拒绝继续。"""


@dataclass(frozen=True)
class SqliteWriteFence:
    connection: sqlite3.Connection
    source_fd: int
    device: int
    inode: int


def _uses_co_deployed_minio() -> bool:
    endpoint = os.environ.get("MINIO_URL", "").strip()
    if not endpoint:
        return False
    parsed = urlsplit(endpoint if "://" in endpoint else f"//{endpoint}")
    if parsed.hostname != "minio" or (parsed.port or 9000) != 9000:
        raise ExactDrError(
            "在线 exact DR 只支持 compose 内 minio:9000;外部 MinIO 缺少可信卷身份"
        )
    return True


class ExactDrMutationGate:
    """让已进入的写请求排空,并在维护阶段拒绝新的写请求。"""

    def __init__(self) -> None:
        self._condition = asyncio.Condition()
        self._phase: str | None = None
        self._operation_id: str | None = None
        self._active_mutations = 0

    @property
    def phase(self) -> str | None:
        return self._phase

    @property
    def active_mutations(self) -> int:
        return self._active_mutations

    @staticmethod
    def _is_maintenance_safe(method: str, path: str) -> bool:
        normalized = method.upper()
        if normalized == "OPTIONS":
            return True
        return normalized in {"GET", "HEAD"} and path == "/api/recovery"

    @staticmethod
    def _allowed_while_draining(path: str) -> bool:
        if path == "/api/runner/jobs/request":
            return False
        return path.startswith(_DRAIN_ALLOWED_PREFIXES)

    async def enter_request(self, method: str, path: str) -> bool:
        if self._is_maintenance_safe(method, path):
            return False
        async with self._condition:
            if self._phase == PHASE_SNAPSHOTTING:
                raise ExactDrError("exact DR 正在快照,写请求已暂停")
            if self._phase == PHASE_DRAINING and not self._allowed_while_draining(path):
                raise ExactDrError("exact DR 正在排空,新写请求已暂停")
            self._active_mutations += 1
            return True

    async def leave_request(self, entered: bool) -> None:
        if not entered:
            return
        async with self._condition:
            self._active_mutations -= 1
            self._condition.notify_all()

    async def begin_draining(
        self, data_dir: Path, *, operation_id: str, created_at: str,
    ) -> None:
        async with self._condition:
            if self._phase is not None:
                raise ExactDrError("已有 exact DR 操作正在运行")
            acquire_task = asyncio.create_task(asyncio.to_thread(
                acquire_barrier,
                data_dir,
                operation_id=operation_id,
                created_at=created_at,
            ))
            try:
                await asyncio.shield(acquire_task)
            except asyncio.CancelledError:
                acquired = False
                try:
                    await acquire_task
                    acquired = True
                except Exception:  # noqa: BLE001
                    pass
                if acquired:
                    cleanup_task = asyncio.create_task(asyncio.to_thread(
                        release_barrier,
                        data_dir,
                        operation_id=operation_id,
                    ))
                    with suppress(Exception):
                        await asyncio.shield(cleanup_task)
                raise
            except ExactDrBarrierError as exc:
                raise ExactDrError(str(exc)) from exc
            self._operation_id = operation_id
            self._phase = PHASE_DRAINING

    async def begin_snapshotting(
        self, data_dir: Path, *, operation_id: str, timeout_sec: float = 60,
    ) -> None:
        async with self._condition:
            if self._phase != PHASE_DRAINING or self._operation_id != operation_id:
                raise ExactDrError("exact DR 排空屏障所有权已改变")
            await asyncio.to_thread(
                advance_barrier,
                data_dir,
                operation_id=operation_id,
                phase=PHASE_SNAPSHOTTING,
                updated_at=utc_now(),
            )
            self._phase = PHASE_SNAPSHOTTING
            try:
                async with asyncio.timeout(timeout_sec):
                    while self._active_mutations:
                        await self._condition.wait()
            except TimeoutError as exc:
                raise ExactDrError("API 在途请求排空超时") from exc

    async def finish(self, data_dir: Path, *, operation_id: str) -> None:
        async with self._condition:
            if self._operation_id not in {None, operation_id}:
                raise ExactDrError("拒绝释放其他 exact DR 操作的停写屏障")
            await asyncio.to_thread(
                release_barrier,
                data_dir,
                operation_id=operation_id,
            )
            self._operation_id = None
            self._phase = None
            self._condition.notify_all()

    async def recover_stale(self, data_dir: Path) -> None:
        """API 重启后中断旧操作并释放可证明属于旧操作的屏障。"""
        operation = read_operation(data_dir)
        barrier = await asyncio.to_thread(read_barrier, data_dir)
        if (
            operation is not None
            and operation.get("status") == "success"
            and not _published_receipt_matches(operation)
        ):
            _cleanup_failed_outputs(operation)
            operation.update(
                status="interrupted",
                finished_at=utc_now(),
                error="API 在最终 receipt 发布前中断;本代未授权恢复",
            )
            write_operation(data_dir, operation)
        if barrier is None:
            if operation is not None and operation.get("status") != "success":
                _cleanup_failed_outputs(operation)
            if operation is not None and operation.get("status") not in _TERMINAL:
                operation.update(
                    status="interrupted",
                    finished_at=utc_now(),
                    error="API 重启导致 exact DR 中断;未发布的归档不得使用",
                )
                write_operation(data_dir, operation)
            return
        owner = str(barrier["operation_id"])
        if operation is None or operation.get("id") != owner:
            raise ExactDrError("发现无对应操作记录的 exact DR 屏障,拒绝自动释放")
        if operation.get("status") != "success":
            _cleanup_failed_outputs(operation)
        if operation.get("status") not in _TERMINAL:
            operation.update(
                status="interrupted",
                finished_at=utc_now(),
                error="API 重启导致 exact DR 中断;未发布的归档不得使用",
            )
            write_operation(data_dir, operation)
        await asyncio.to_thread(release_barrier, data_dir, operation_id=owner)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def output_path() -> Path:
    path = Path(os.environ.get(OUTPUT_ENV, DEFAULT_OUTPUT_PATH))
    if not path.is_absolute():
        raise ExactDrError(f"{OUTPUT_ENV} 必须是绝对路径")
    absolute = Path(os.path.abspath(path))
    if absolute.resolve(strict=False) != absolute:
        raise ExactDrError("exact DR 输出路径或祖先不得包含符号链接")
    return absolute


def validate_start_configuration(*, verify_physical_boundaries: bool = True) -> None:
    deployment_id = os.environ.get("FLORI_DEPLOYMENT_ID", "")
    if not _DEPLOYMENT_RE.fullmatch(deployment_id) or deployment_id == "unbound":
        raise ExactDrError("FLORI_DEPLOYMENT_ID 未配置或格式非法")
    root = output_path()
    if not root.is_dir() or root.is_symlink() or not os.access(root, os.W_OK):
        raise ExactDrError("exact DR 输出目录不存在、不可写或不安全")
    script = Path(os.environ.get("FLORI_EXACT_DR_SCRIPT", "/app/scripts/dr_snapshot.py"))
    if (
        not script.is_absolute()
        or Path(os.path.abspath(script)).resolve(strict=False) != Path(os.path.abspath(script))
        or not script.is_file()
        or script.is_symlink()
    ):
        raise ExactDrError("exact DR 脚本未安装或路径不安全")
    redis_root = Path("/dr-source/redis")
    if not redis_root.is_dir() or redis_root.is_symlink():
        raise ExactDrError("Redis 灾备源卷未只读挂载到 /dr-source/redis")
    if os.environ.get("MINIO_URL"):
        minio_root = Path("/dr-source/minio")
        if not minio_root.is_dir() or minio_root.is_symlink():
            raise ExactDrError("MinIO 灾备源卷未只读挂载到 /dr-source/minio")
    sources = [Path("/data"), redis_root, Path("/app/configs")]
    if _uses_co_deployed_minio():
        sources.append(Path("/dr-source/minio"))
    if verify_physical_boundaries:
        try:
            ensure_output_roots_disjoint((root,), tuple(sources))
        except ResultFileError as exc:
            raise ExactDrError(f"exact DR 输出目录与源资产重叠:{exc}") from exc
    owner_uid = os.environ.get("FLORI_DR_OWNER_UID")
    owner_gid = os.environ.get("FLORI_DR_OWNER_GID")
    if bool(owner_uid) != bool(owner_gid):
        raise ExactDrError("FLORI_DR_OWNER_UID 与 FLORI_DR_OWNER_GID 必须同时配置")
    if owner_uid and owner_gid:
        try:
            if int(owner_uid) < 0 or int(owner_gid) < 0:
                raise ValueError
        except ValueError as exc:
            raise ExactDrError("exact DR owner UID/GID 必须是非负整数") from exc


def _operation_path(data_dir: Path) -> Path:
    try:
        return control_root(data_dir) / "operation.json"
    except ExactDrBarrierError as exc:
        raise ExactDrError(str(exc)) from exc


def _validate_operation(operation: Mapping[str, Any]) -> dict[str, Any]:
    expected = {
        "format", "id", "status", "created_at", "started_at", "finished_at",
        "generation", "archive_name", "sidecar_name", "receipt_name",
        "archive_sha256", "size_bytes", "drain", "error",
    }
    if set(operation) != expected or operation.get("format") != OPERATION_FORMAT:
        raise ExactDrError("exact DR operation format is invalid")
    operation_id = operation.get("id")
    if not isinstance(operation_id, str) or not _OPERATION_RE.fullmatch(operation_id):
        raise ExactDrError("exact DR operation id is invalid")
    if operation.get("status") not in _STATES:
        raise ExactDrError("exact DR operation status is invalid")
    for key in ("created_at", "started_at", "finished_at"):
        if operation.get(key) is not None and not isinstance(operation.get(key), str):
            raise ExactDrError(f"exact DR operation {key} is invalid")
    generation = operation.get("generation")
    if (
        not isinstance(generation, str)
        or operation_id != f"exact-dr-{generation}"
        or not re.fullmatch(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}", generation)
    ):
        raise ExactDrError("exact DR operation generation is invalid")
    expected_names = {
        "archive_name": f"flori-backup-{generation}.tar.gz",
        "sidecar_name": f"flori-backup-{generation}.tar.gz.sha256",
        "receipt_name": f"flori-backup-{generation}.json",
    }
    if any(operation.get(key) != value for key, value in expected_names.items()):
        raise ExactDrError("exact DR operation output names are invalid")
    digest = operation.get("archive_sha256")
    if digest is not None and (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(ch not in "0123456789abcdef" for ch in digest)
    ):
        raise ExactDrError("exact DR operation digest is invalid")
    if operation.get("size_bytes") is not None and (
        type(operation.get("size_bytes")) is not int or operation["size_bytes"] < 0
    ):
        raise ExactDrError("exact DR operation size is invalid")
    drain = operation.get("drain")
    if (
        type(drain) is not dict
        or set(drain) != {"holders", "running_steps", "quiet_samples"}
        or any(type(drain[key]) is not int or drain[key] < 0 for key in drain)
    ):
        raise ExactDrError("exact DR operation drain state is invalid")
    if operation.get("error") is not None and not isinstance(operation.get("error"), str):
        raise ExactDrError("exact DR operation error is invalid")
    return dict(operation)


def write_operation(data_dir: Path, operation: Mapping[str, Any]) -> None:
    body = _validate_operation(operation)
    payload = canonical_json_bytes(body)
    name = "operation.json"
    temporary = f".{name}.{secrets.token_hex(8)}.tmp"
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        directory_fd = open_control_root(data_dir)
    except ExactDrBarrierError as exc:
        raise ExactDrError(str(exc)) from exc
    try:
        fd = os.open(temporary, flags, 0o600, dir_fd=directory_fd)
        try:
            offset = 0
            while offset < len(payload):
                written = os.write(fd, payload[offset:])
                if written <= 0:
                    raise ExactDrError("exact DR operation write did not make progress")
                offset += written
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        os.fsync(directory_fd)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=directory_fd)
        os.close(directory_fd)


def read_operation(data_dir: Path) -> dict[str, Any] | None:
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
    try:
        directory_fd = open_control_root(data_dir)
    except ExactDrBarrierError as exc:
        raise ExactDrError(str(exc)) from exc
    try:
        fd = os.open("operation.json", flags, dir_fd=directory_fd)
    except FileNotFoundError:
        os.close(directory_fd)
        return None
    except OSError as exc:
        os.close(directory_fd)
        raise ExactDrError(f"cannot open exact DR operation: {exc}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 64 * 1024:
            raise ExactDrError("exact DR operation file is not a bounded regular file")
        raw = os.read(fd, 64 * 1024 + 1)
    finally:
        os.close(fd)
        os.close(directory_fd)
    try:
        body = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ExactDrError("exact DR operation file is corrupt") from exc
    if type(body) is not dict or canonical_json_bytes(body) != raw:
        raise ExactDrError("exact DR operation file is not canonical")
    return _validate_operation(body)


def new_operation(data_dir: Path, *, persist: bool = True) -> dict[str, Any]:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    operation_id = f"exact-dr-{stamp}-{secrets.token_hex(4)}"
    generation = operation_id.removeprefix("exact-dr-")
    archive_name = f"flori-backup-{generation}.tar.gz"
    operation = {
        "format": OPERATION_FORMAT,
        "id": operation_id,
        "status": "draining",
        "created_at": utc_now(),
        "started_at": utc_now(),
        "finished_at": None,
        "generation": generation,
        "archive_name": archive_name,
        "sidecar_name": f"{archive_name}.sha256",
        "receipt_name": f"flori-backup-{generation}.json",
        "archive_sha256": None,
        "size_bytes": None,
        "drain": {"holders": 0, "running_steps": 0, "quiet_samples": 0},
        "error": None,
    }
    if persist:
        write_operation(data_dir, operation)
    return operation


def status_payload(data_dir: Path, *, active: bool) -> dict[str, Any]:
    operation = read_operation(data_dir)
    try:
        validate_start_configuration(verify_physical_boundaries=False)
        configured = True
    except ExactDrError:
        configured = False
    path = Path(os.environ.get(OUTPUT_ENV, DEFAULT_OUTPUT_PATH))
    state = str(operation["status"]) if operation else "idle"
    if operation and operation["status"] not in _TERMINAL and not active:
        state = "interrupted"
    return {
        "configured": configured,
        "output_path": str(path),
        "state": state,
        "operation": operation,
        "confirmation": CONFIRMATION,
        "drain_timeout_sec": _drain_timeout(),
    }


def _drain_timeout() -> int:
    try:
        value = int(os.environ.get("FLORI_EXACT_DR_DRAIN_TIMEOUT_SEC", "3600"))
    except ValueError as exc:
        raise ExactDrError("FLORI_EXACT_DR_DRAIN_TIMEOUT_SEC 必须是整数") from exc
    if not 10 <= value <= 86_400:
        raise ExactDrError("FLORI_EXACT_DR_DRAIN_TIMEOUT_SEC 必须在 10..86400")
    return value


def _bounded_error(value: object) -> str:
    return str(value).strip()[:1000] or "exact DR 子进程失败"


def _open_regular_nofollow(path: Path, *, writable: bool) -> int:
    absolute = Path(os.path.abspath(path))
    if not absolute.is_absolute() or absolute.name in {"", ".", ".."}:
        raise ExactDrError("SQLite 数据库路径非法")
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    directory_fd = os.open(absolute.anchor or "/", directory_flags)
    try:
        for component in absolute.parent.parts[1:]:
            next_fd = os.open(component, directory_flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        file_fd = os.open(
            absolute.name,
            (os.O_RDWR if writable else os.O_RDONLY) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory_fd,
        )
    finally:
        os.close(directory_fd)
    info = os.fstat(file_fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        os.close(file_fd)
        raise ExactDrError("SQLite 数据库必须是单链接普通文件")
    return file_fd


def _acquire_sqlite_write_fence(path: Path, *, timeout_sec: int) -> SqliteWriteFence:
    source_fd = _open_regular_nofollow(path, writable=True)
    source_info = os.fstat(source_fd)
    connection = sqlite3.connect(
        f"file:/proc/self/fd/{source_fd}?mode=rw",
        uri=True,
        timeout=timeout_sec,
        isolation_level=None,
        check_same_thread=False,
    )
    try:
        connection.execute(f"PRAGMA busy_timeout={timeout_sec * 1000}")
        connection.execute("BEGIN IMMEDIATE")
        after_lock = os.fstat(source_fd)
        if (after_lock.st_dev, after_lock.st_ino) != (
            source_info.st_dev,
            source_info.st_ino,
        ):
            raise ExactDrError("SQLite 数据库inode在加栅栏时改变")
    except BaseException:
        connection.close()
        os.close(source_fd)
        raise
    return SqliteWriteFence(
        connection=connection,
        source_fd=source_fd,
        device=source_info.st_dev,
        inode=source_info.st_ino,
    )


def _release_sqlite_write_fence(fence: SqliteWriteFence) -> None:
    try:
        fence.connection.rollback()
    finally:
        try:
            fence.connection.close()
        finally:
            os.close(fence.source_fd)


def _read_bounded_regular(path: Path, *, max_bytes: int) -> bytes:
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ExactDrError(f"cannot open exact DR output: {exc}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > max_bytes:
            raise ExactDrError("exact DR output is not a bounded regular file")
        payload = os.read(fd, max_bytes + 1)
        if len(payload) > max_bytes:
            raise ExactDrError("exact DR output grew beyond its size limit")
        return payload
    finally:
        os.close(fd)


def _read_bounded_regular_at(
    directory_fd: int, name: str, *, max_bytes: int,
) -> bytes:
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(name, flags, dir_fd=directory_fd)
    except OSError as exc:
        raise ExactDrError(f"cannot open exact DR output: {exc}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > max_bytes:
            raise ExactDrError("exact DR output is not a bounded regular file")
        payload = os.read(fd, max_bytes + 1)
        if len(payload) > max_bytes:
            raise ExactDrError("exact DR output grew beyond its size limit")
        return payload
    finally:
        os.close(fd)


def _read_result(path: Path) -> dict[str, Any]:
    try:
        result = json.loads(_read_bounded_regular(path, max_bytes=8 * 1024 * 1024))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ExactDrError("exact DR 结果文件不可读") from exc
    if type(result) is not dict:
        raise ExactDrError("exact DR 结果必须是对象")
    return result


def _read_pending_result(operation: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
    root_fd = os.open(
        output_path(),
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        pending_fd = os.open(
            ".pending-receipts",
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=root_fd,
        )
        try:
            raw = _read_bounded_regular_at(
                pending_fd,
                _pending_receipt_path(operation).name,
                max_bytes=8 * 1024 * 1024,
            )
        finally:
            os.close(pending_fd)
    finally:
        os.close(root_fd)
    try:
        result = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ExactDrError("exact DR 结果文件不可读") from exc
    if type(result) is not dict:
        raise ExactDrError("exact DR 结果必须是对象")
    return result, hashlib.sha256(raw).hexdigest()


def _published_receipt_matches(operation: Mapping[str, Any]) -> bool:
    try:
        result = _read_result(output_path() / str(operation["receipt_name"]))
    except (OSError, ExactDrError):
        return False
    return (
        result.get("status") == "success"
        and result.get("operation") == "backup"
        and result.get("generation") == operation.get("generation")
        and result.get("archive_sha256") == operation.get("archive_sha256")
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ExactDrError(f"cannot open exact DR archive: {exc}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ExactDrError("exact DR archive is not a regular single-link file")
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    finally:
        os.close(fd)
    return digest.hexdigest()


def _validate_create_result(
    result: Mapping[str, Any], operation: Mapping[str, Any], archive: Path,
) -> str:
    expected_generation = operation["generation"]
    expected_deployment = os.environ.get("FLORI_DEPLOYMENT_ID", "")
    manifest = result.get("manifest")
    deployment = manifest.get("deployment") if type(manifest) is dict else None
    assets = manifest.get("assets") if type(manifest) is dict else None
    if (
        result.get("status") != "success"
        or result.get("operation") != "backup"
        or result.get("generation") != expected_generation
        or result.get("archive") != str(archive)
        or type(manifest) is not dict
        or manifest.get("generation") != expected_generation
        or type(deployment) is not dict
        or deployment.get("id") != expected_deployment
        or type(assets) is not dict
    ):
        raise ExactDrError("exact DR create result identity mismatch")
    required_assets = {"data", "redis", "config"}
    if _uses_co_deployed_minio():
        required_assets.add("minio")
    if any(
        type(assets.get(name)) is not dict or assets[name].get("included") is not True
        for name in required_assets
    ):
        raise ExactDrError("exact DR create result缺少必需资产")
    expected_modes = {
        "data": "stable-filesystem-copy+sqlite-online-backup",
        "redis": "materialized-rdb-aof",
        "config": "stable-filesystem-copy",
        "minio": "stable-filesystem-copy",
    }
    if any(
        assets[name].get("capture_mode") != expected_modes[name]
        for name in required_assets
    ):
        raise ExactDrError("exact DR create result的资产捕获模式不可信")
    claimed_digest = result.get("archive_sha256")
    if not isinstance(claimed_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", claimed_digest):
        raise ExactDrError("exact DR create result digest is invalid")
    actual_digest = _sha256_file(archive)
    sidecar = archive.with_suffix(archive.suffix + ".sha256")
    expected_sidecar = f"{actual_digest}  {archive.name}\n"
    try:
        sidecar_body = _read_bounded_regular(sidecar, max_bytes=256).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ExactDrError("exact DR sidecar is not UTF-8") from exc
    if sidecar_body != expected_sidecar:
        raise ExactDrError("exact DR sidecar does not bind the current archive")
    if not secrets.compare_digest(claimed_digest, actual_digest):
        raise ExactDrError("exact DR receipt digest does not match the current archive")
    return actual_digest


def _validate_validation_result(
    validation: Mapping[str, Any], operation: Mapping[str, Any],
    create_result: Mapping[str, Any],
) -> None:
    checks = validation.get("checks")
    create_manifest = create_result.get("manifest")
    create_assets = (
        create_manifest.get("assets") if type(create_manifest) is dict else None
    )
    if (
        validation.get("status") != "success"
        or validation.get("operation") != "validate"
        or validation.get("generation") != operation["generation"]
        or validation.get("deployment_id") != os.environ.get("FLORI_DEPLOYMENT_ID", "")
        or type(checks) is not dict
        or set(checks.values()) != {"ok"}
        or type(validation.get("assets")) is not dict
        or validation.get("assets") != create_assets
    ):
        raise ExactDrError("exact DR validate result identity mismatch")


def _cleanup_failed_outputs(operation: Mapping[str, Any]) -> None:
    """失败代不留下看似完整的三件套或中断 partial。"""
    root = output_path()
    paths = [
        root / str(operation["archive_name"]),
        root / str(operation["sidecar_name"]),
        root / str(operation["receipt_name"]),
    ]
    for path in paths:
        with suppress(FileNotFoundError):
            path.unlink()
    root_fd = os.open(
        root,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        try:
            pending_fd = os.open(
                ".pending-receipts",
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=root_fd,
            )
        except FileNotFoundError:
            pending_fd = None
        if pending_fd is not None:
            try:
                with suppress(FileNotFoundError):
                    os.unlink(_pending_receipt_path(operation).name, dir_fd=pending_fd)
                os.fsync(pending_fd)
            finally:
                os.close(pending_fd)
    finally:
        os.close(root_fd)
    archive_name = str(operation["archive_name"])
    for partial in root.glob(f".{archive_name}.*.partial"):
        if partial.is_file() and not partial.is_symlink():
            with suppress(FileNotFoundError):
                partial.unlink()


def _pending_receipt_path(operation: Mapping[str, Any]) -> Path:
    return output_path() / ".pending-receipts" / f"{operation['id']}.json"


def _prepare_pending_receipt_path(operation: Mapping[str, Any]) -> Path:
    root = output_path()
    root_fd = os.open(
        root,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        try:
            os.mkdir(".pending-receipts", 0o700, dir_fd=root_fd)
        except FileExistsError:
            pass
        pending_fd = os.open(
            ".pending-receipts",
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=root_fd,
        )
        try:
            info = os.fstat(pending_fd)
            if not stat.S_ISDIR(info.st_mode):
                raise ExactDrError("exact DR pending receipt 目录不安全")
        finally:
            os.close(pending_fd)
        os.fsync(root_fd)
    finally:
        os.close(root_fd)
    return _pending_receipt_path(operation)


def _publish_receipt(
    operation: Mapping[str, Any], *, expected_sha256: str,
) -> None:
    """栅栏安全收口后才把已校验 receipt 原子发布为恢复授权标记。"""
    root = output_path()
    pending = _pending_receipt_path(operation)
    final = root / str(operation["receipt_name"])
    root_fd = os.open(
        root,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    pending_fd = os.open(
        ".pending-receipts",
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=root_fd,
    )
    temporary = f".{final.name}.{secrets.token_hex(8)}.publishing"
    try:
        payload = _read_bounded_regular_at(
            pending_fd, pending.name, max_bytes=8 * 1024 * 1024,
        )
        if not secrets.compare_digest(hashlib.sha256(payload).hexdigest(), expected_sha256):
            raise ExactDrError("exact DR pending receipt 在校验后被替换")
        source_info = os.stat(pending.name, dir_fd=pending_fd, follow_symlinks=False)
        output_fd = os.open(
            temporary,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=root_fd,
        )
        try:
            offset = 0
            while offset < len(payload):
                written = os.write(output_fd, payload[offset:])
                if written <= 0:
                    raise ExactDrError("exact DR receipt publish did not make progress")
                offset += written
            os.fchmod(output_fd, stat.S_IMODE(source_info.st_mode))
            with suppress(PermissionError):
                os.fchown(output_fd, source_info.st_uid, source_info.st_gid)
            os.fsync(output_fd)
        finally:
            os.close(output_fd)
        try:
            os.link(
                temporary,
                final.name,
                src_dir_fd=root_fd,
                dst_dir_fd=root_fd,
                follow_symlinks=False,
            )
        except FileExistsError as exc:
            raise ExactDrError("exact DR 最终 receipt 已存在,拒绝覆盖") from exc
        os.fsync(root_fd)
        os.unlink(pending.name, dir_fd=pending_fd)
        os.fsync(pending_fd)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=root_fd)
        os.close(pending_fd)
        os.close(root_fd)


async def _run_command(
    command: list[str], *, pass_fds: tuple[int, ...] = (),
) -> tuple[int, str, str]:
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        pass_fds=pass_fds,
    )
    try:
        stdout, stderr = await process.communicate()
    except asyncio.CancelledError:
        process.terminate()
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(process.wait(), timeout=5)
        if process.returncode is None:
            process.kill()
            await process.wait()
        raise
    return (
        int(process.returncode or 0),
        stdout.decode("utf-8", errors="replace")[-4000:],
        stderr.decode("utf-8", errors="replace")[-4000:],
    )


def _snapshot_command(
    operation: Mapping[str, Any], sqlite_fence: SqliteWriteFence,
) -> list[str]:
    deployment_id = os.environ.get("FLORI_DEPLOYMENT_ID", "")
    if not deployment_id:
        raise ExactDrError("未配置 FLORI_DEPLOYMENT_ID")
    output = output_path()
    if output.is_symlink() or not output.is_dir():
        raise ExactDrError("exact DR 输出目录不安全")
    script = Path(os.environ.get("FLORI_EXACT_DR_SCRIPT", "/app/scripts/dr_snapshot.py"))
    if not script.is_absolute() or not script.is_file():
        raise ExactDrError("exact DR 脚本未安装")
    archive = output / str(operation["archive_name"])
    receipt = _prepare_pending_receipt_path(operation)
    command = [
        sys.executable, str(script), "create",
        "--data", "/data",
        "--redis", "/dr-source/redis",
        "--redis-mode", "materialized-rdb-aof",
        "--config", "/app/configs",
        "--output", str(archive),
        "--generation", str(operation["generation"]),
        "--app-version", FLORI_VERSION,
        "--deployment-id", deployment_id,
        "--schema-manifest", "/app/shared/migrations/manifest.json",
        "--sqlite-source-fd", str(sqlite_fence.source_fd),
        "--sqlite-source-dev", str(sqlite_fence.device),
        "--sqlite-source-ino", str(sqlite_fence.inode),
        "--result-file", str(receipt),
        "--data-exclude", "exact-dr-control",
        "--data-exclude", "recovery-control",
    ]
    if _uses_co_deployed_minio():
        command.extend(("--minio", "/dr-source/minio"))
    owner_uid = os.environ.get("FLORI_DR_OWNER_UID")
    owner_gid = os.environ.get("FLORI_DR_OWNER_GID")
    if bool(owner_uid) != bool(owner_gid):
        raise ExactDrError("FLORI_DR_OWNER_UID 与 FLORI_DR_OWNER_GID 必须同时配置")
    if owner_uid and owner_gid:
        command.extend(("--owner-uid", owner_uid, "--owner-gid", owner_gid))
    return command


async def run_exact_dr(app, operation_id: str) -> None:
    data_dir = Path(app.state.config.data_dir)
    gate: ExactDrMutationGate = app.state.exact_dr_gate
    operation = read_operation(data_dir)
    if operation is None or operation["id"] != operation_id:
        raise ExactDrError("exact DR 操作记录已丢失")
    paused_background = False
    cleanup_failed = False
    redis_pause_may_be_active = False
    sqlite_fence: SqliteWriteFence | None = None
    prepared_digest: str | None = None
    prepared_size: int | None = None
    prepared_receipt_digest: str | None = None
    try:
        deadline = asyncio.get_running_loop().time() + _drain_timeout()
        quiet_samples = 0
        while True:
            holders = await app.state.redis.get_all_holders_strict()
            running_steps = await asyncio.to_thread(app.state.db.list_running_steps)
            if not holders and not running_steps:
                quiet_samples += 1
            else:
                quiet_samples = 0
            operation["drain"] = {
                "holders": len(holders),
                "running_steps": len(running_steps),
                "quiet_samples": quiet_samples,
            }
            write_operation(data_dir, operation)
            if quiet_samples >= 2:
                break
            if asyncio.get_running_loop().time() >= deadline:
                raise ExactDrError("Worker 排空超时;未创建灾备归档")
            await asyncio.sleep(1)

        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise ExactDrError("Worker 排空超时;未创建灾备归档")
        await gate.begin_snapshotting(
            data_dir,
            operation_id=operation_id,
            timeout_sec=remaining,
        )
        operation.update(status="snapshotting")
        write_operation(data_dir, operation)
        pause = getattr(app.state, "pause_exact_dr_background_writers", None)
        if callable(pause):
            await pause()
            paused_background = True
        wait_for_finalizers = getattr(
            getattr(app.state, "storage", None),
            "wait_for_finalizers",
            None,
        )
        if callable(wait_for_finalizers):
            try:
                await asyncio.wait_for(
                    wait_for_finalizers(),
                    timeout=_drain_timeout(),
                )
            except asyncio.TimeoutError as exc:
                raise ExactDrError("API 产物提交收尾排空超时") from exc

        ack_deadline = asyncio.get_running_loop().time() + 60
        while not await asyncio.to_thread(
            scheduler_quiesced, data_dir, operation_id=operation_id,
        ):
            if asyncio.get_running_loop().time() >= ack_deadline:
                raise ExactDrError("Scheduler 未在 60 秒内确认停写")
            await asyncio.sleep(0.5)

        sqlite_fence = await asyncio.to_thread(
            _acquire_sqlite_write_fence,
            Path(app.state.config.db_path),
            timeout_sec=60,
        )
        await app.state.redis.prepare_exact_dr_persistence(timeout_sec=60)
        redis_pause_may_be_active = True
        await app.state.redis.pause_writes_for_exact_dr()
        final_holders = await app.state.redis.get_all_holders_strict()
        final_running_steps = await asyncio.to_thread(app.state.db.list_running_steps)
        if final_holders or final_running_steps:
            raise ExactDrError(
                "最终停写切面仍有 Worker claim/运行步骤,拒绝发布混代灾备"
            )
        await asyncio.to_thread(validate_start_configuration)
        command = _snapshot_command(operation, sqlite_fence)
        returncode, stdout, stderr = await _run_command(
            command,
            pass_fds=(sqlite_fence.source_fd,),
        )
        receipt = _pending_receipt_path(operation)
        result, prepared_receipt_digest = _read_pending_result(operation)
        if returncode != 0:
            raise ExactDrError(_bounded_error(result.get("error") or stderr or stdout))
        archive = output_path() / str(operation["archive_name"])
        actual_digest = await asyncio.to_thread(
            _validate_create_result, result, operation, archive,
        )

        operation.update(status="verifying")
        write_operation(data_dir, operation)
        validate_command = [
            sys.executable,
            os.environ.get("FLORI_EXACT_DR_SCRIPT", "/app/scripts/dr_snapshot.py"),
            "validate",
            "--archive", str(archive),
            "--schema-manifest", "/app/shared/migrations/manifest.json",
        ]
        returncode, stdout, stderr = await _run_command(validate_command)
        if returncode != 0:
            raise ExactDrError(_bounded_error(stderr or stdout))
        try:
            validation = json.loads(stdout.strip().splitlines()[-1])
        except (IndexError, json.JSONDecodeError) as exc:
            raise ExactDrError("exact DR 校验没有返回可信结果") from exc
        _validate_validation_result(validation, operation, result)
        final_digest = await asyncio.to_thread(_sha256_file, archive)
        if not secrets.compare_digest(final_digest, actual_digest):
            raise ExactDrError("exact DR 归档在校验后被替换")
        prepared_digest = final_digest
        prepared_size = archive.stat().st_size
    except asyncio.CancelledError:
        try:
            _cleanup_failed_outputs(operation)
        except Exception as cleanup_exc:  # noqa: BLE001
            cleanup_failed = True
            cleanup_error = _bounded_error(cleanup_exc)
        else:
            cleanup_error = None
        operation.update(
            status="interrupted",
            finished_at=utc_now(),
            error=(
                f"API 关闭导致 exact DR 中断;残留清理失败:{cleanup_error}"
                if cleanup_error else
                "API 关闭导致 exact DR 中断;未校验成功的归档已删除"
            ),
        )
        with suppress(Exception):
            write_operation(data_dir, operation)
        raise
    except Exception as exc:  # noqa: BLE001
        try:
            _cleanup_failed_outputs(operation)
        except Exception as cleanup_exc:  # noqa: BLE001
            cleanup_failed = True
            cleanup_error = _bounded_error(cleanup_exc)
        else:
            cleanup_error = None
        operation.update(
            status="failed",
            finished_at=utc_now(),
            error=(
                f"{_bounded_error(exc)};残留清理失败:{cleanup_error}"
                if cleanup_error else _bounded_error(exc)
            ),
        )
        with suppress(Exception):
            write_operation(data_dir, operation)
        logger.exception(
            "exact_dr_failed",
            operation_id=operation_id,
            error_type=type(exc).__name__,
        )
    finally:
        fence_errors: list[str] = []
        if redis_pause_may_be_active:
            try:
                await app.state.redis.resume_writes_after_exact_dr()
            except Exception as exc:  # noqa: BLE001
                fence_errors.append(f"Redis恢复写入失败:{_bounded_error(exc)}")
        if sqlite_fence is not None:
            try:
                await asyncio.to_thread(_release_sqlite_write_fence, sqlite_fence)
            except Exception as exc:  # noqa: BLE001
                fence_errors.append(f"SQLite写屏障释放失败:{_bounded_error(exc)}")
        if fence_errors:
            cleanup_error = None
            try:
                _cleanup_failed_outputs(operation)
            except Exception as exc:  # noqa: BLE001
                cleanup_error = _bounded_error(exc)
            cleanup_failed = True
            operation.update(
                status="failed",
                finished_at=utc_now(),
                error=";".join(fence_errors) + (
                    f";残留清理失败:{cleanup_error}" if cleanup_error else
                    ";本代三件套已删除"
                ),
            )
            with suppress(Exception):
                write_operation(data_dir, operation)
        barrier_released = False
        if not cleanup_failed:
            try:
                await gate.finish(data_dir, operation_id=operation_id)
                barrier_released = True
            except (ExactDrError, ExactDrBarrierError) as exc:
                try:
                    _cleanup_failed_outputs(operation)
                except Exception as cleanup_exc:  # noqa: BLE001
                    cleanup_failed = True
                    cleanup_error = _bounded_error(cleanup_exc)
                else:
                    cleanup_error = None
                operation.update(
                    status="failed",
                    finished_at=utc_now(),
                    error=(
                        f"停写屏障释放失败;残留清理失败:{cleanup_error}"
                        if cleanup_error else
                        "停写屏障释放失败;本代三件套已删除,重启 API 后再操作"
                    ),
                )
                with suppress(Exception):
                    write_operation(data_dir, operation)
                logger.exception(
                    "exact_dr_barrier_release_failed",
                    operation_id=operation_id,
                    error_type=type(exc).__name__,
                    cleanup_failed=cleanup_failed,
                )
        if paused_background and barrier_released:
            if (
                prepared_digest is not None
                and prepared_receipt_digest is not None
                and operation.get("status") == "verifying"
            ):
                try:
                    operation.update(
                        status="success",
                        finished_at=utc_now(),
                        archive_sha256=prepared_digest,
                        size_bytes=prepared_size,
                        error=None,
                    )
                    write_operation(data_dir, operation)
                    await asyncio.to_thread(
                        _publish_receipt,
                        operation,
                        expected_sha256=prepared_receipt_digest,
                    )
                except Exception as exc:  # noqa: BLE001
                    try:
                        _cleanup_failed_outputs(operation)
                    except Exception as cleanup_exc:  # noqa: BLE001
                        cleanup_error = _bounded_error(cleanup_exc)
                    else:
                        cleanup_error = None
                    operation.update(
                        status="failed",
                        finished_at=utc_now(),
                        error=(
                            f"最终 receipt 发布失败:{_bounded_error(exc)};"
                            f"残留清理失败:{cleanup_error}"
                            if cleanup_error else
                            f"最终 receipt 发布失败:{_bounded_error(exc)};本代未授权恢复"
                        ),
                    )
                    with suppress(Exception):
                        write_operation(data_dir, operation)
                else:
                    logger.info(
                        "exact_dr_success",
                        operation_id=operation_id,
                        archive=operation["archive_name"],
                        size_bytes=prepared_size,
                    )
            resume = getattr(app.state, "resume_exact_dr_background_writers", None)
            if callable(resume):
                try:
                    await resume()
                except Exception as exc:  # noqa: BLE001
                    logger.exception(
                        "exact_dr_background_resume_failed",
                        operation_id=operation_id,
                        error_type=type(exc).__name__,
                    )
