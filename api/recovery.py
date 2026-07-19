"""备份状态、后台操作记录与离线恢复交接的安全编排。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from shared.content_import import MODE_EMPTY, ImportError_, build_plan
from shared.content_result import ResultFileError, ensure_output_roots_disjoint
from shared.content_repository import ContentRepository, RepositoryError
from shared.source_library import SourceReferenceError, source_roots_from_env
from shared.step_manifest import canonical_json_bytes
from shared.version import FLORI_VERSION


REPOSITORY_ENV = "FLORI_CONTENT_REPOSITORY"
DEFAULT_REPOSITORY_PATH = "/content-repo"
CONTROL_FORMAT = "flori-recovery-control/v1"
OPERATION_FORMAT = "flori-recovery-operation/v1"
HANDOFF_FORMAT = "flori-restore-handoff/v1"
_OPERATION_ID_RE = re.compile(r"^backup-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")
_HANDOFF_ID_RE = re.compile(r"^restore-[0-9a-f]{24}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_DEPLOYMENT_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_TERMINAL_OPERATION_STATES = frozenset({"success", "failed", "interrupted"})
_OPERATION_STATES = _TERMINAL_OPERATION_STATES | {"queued", "running"}
_OPERATION_KEYS = frozenset({
    "format", "id", "kind", "status", "created_at", "started_at", "finished_at",
    "vendor_media", "full_rehash", "snapshot_digest", "receipt_id", "stats", "error",
})


class RecoveryControlError(RuntimeError):
    """恢复控制面的配置或持久化状态不安全。"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def repository_path() -> Path:
    value = os.environ.get(REPOSITORY_ENV, DEFAULT_REPOSITORY_PATH)
    path = Path(value)
    if not path.is_absolute():
        raise RecoveryControlError(f"{REPOSITORY_ENV} must be an absolute path")
    return path


def validate_repository_boundary(data_dir: Path) -> Path:
    """快速检查词法隔离与symlink;物理bind检查只在操作前执行。"""
    path = repository_path()
    absolute = Path(os.path.abspath(path))
    resolved = path.resolve(strict=False)
    data = Path(data_dir).resolve(strict=False)
    if absolute != resolved:
        raise RecoveryControlError("便携仓库路径或祖先不得包含符号链接")
    if resolved == data or resolved in data.parents or data in resolved.parents:
        raise RecoveryControlError("便携仓库必须与DATA_DIR物理隔离")
    return path


def validate_repository_physical_boundary(data_dir: Path) -> Path:
    """仓库不得物理别名到数据树或受控来源树。"""
    path = validate_repository_boundary(data_dir)
    data = Path(data_dir)
    try:
        source_roots = tuple(source_roots_from_env().values())
        ensure_output_roots_disjoint(
            (path,),
            (data, data / "jobs", data / "prompts", *source_roots),
        )
    except (ResultFileError, SourceReferenceError) as exc:
        raise RecoveryControlError(f"便携仓库物理边界不安全: {exc}") from exc
    return path


def control_root(data_dir: Path) -> Path:
    data = Path(data_dir)
    root = data / "recovery-control"
    data_resolved = data.resolve(strict=False)
    root_resolved = root.resolve(strict=False)
    if root_resolved.parent != data_resolved:
        raise RecoveryControlError("recovery control root escaped DATA_DIR")
    if root.exists() and root.is_symlink():
        raise RecoveryControlError("recovery control root must not be a symlink")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    marker = root / "control.json"
    if not marker.exists():
        _write_json_atomic(marker, {"format": CONTROL_FORMAT})
    else:
        body = _read_json(marker, max_bytes=4096)
        if body != {"format": CONTROL_FORMAT}:
            raise RecoveryControlError("unsupported recovery control format")
    return root


def control_subdir(data_dir: Path, name: str) -> Path:
    """返回受控元数据子目录;拒绝目录替换为symlink或其他实体。"""
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", name):
        raise RecoveryControlError("invalid recovery control subdirectory")
    root = control_root(data_dir)
    directory = root / name
    if directory.is_symlink():
        raise RecoveryControlError(f"recovery control subdirectory is a symlink: {name}")
    if directory.exists() and not directory.is_dir():
        raise RecoveryControlError(f"recovery control subdirectory is not a directory: {name}")
    directory.mkdir(mode=0o700, exist_ok=True)
    if directory.is_symlink() or directory.resolve(strict=True).parent != root.resolve(strict=True):
        raise RecoveryControlError(f"recovery control subdirectory escaped root: {name}")
    os.chmod(directory, 0o700)
    return directory


def _write_json_atomic(path: Path, body: Mapping[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.is_symlink():
        raise RecoveryControlError(f"unsafe recovery control path: {path}")
    payload = canonical_json_bytes(dict(body))
    temporary_name = f".{path.name}.{secrets.token_hex(8)}.tmp"
    directory_flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        directory_flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        directory_flags |= os.O_NOFOLLOW
    directory_fd = os.open(path.parent, directory_flags)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(temporary_name, flags, 0o600, dir_fd=directory_fd)
        try:
            offset = 0
            while offset < len(payload):
                offset += os.write(fd, payload[offset:])
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(
            temporary_name,
            path.name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        os.fsync(directory_fd)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(temporary_name, dir_fd=directory_fd)
        os.close(directory_fd)


def _read_json(path: Path, *, max_bytes: int = 1024 * 1024) -> dict[str, Any]:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except (FileNotFoundError, OSError) as exc:
        raise RecoveryControlError(f"unsafe recovery control file: {path}") from exc
    try:
        raw = bytearray()
        while len(raw) <= max_bytes:
            chunk = os.read(fd, min(64 * 1024, max_bytes + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
    finally:
        os.close(fd)
    if len(raw) > max_bytes:
        raise RecoveryControlError(f"recovery control file is too large: {path.name}")
    try:
        body = json.loads(bytes(raw))
    except (json.JSONDecodeError, RecursionError) as exc:
        raise RecoveryControlError(f"invalid recovery control JSON: {path.name}") from exc
    if type(body) is not dict:
        raise RecoveryControlError(f"recovery control file must be an object: {path.name}")
    try:
        canonical = canonical_json_bytes(body)
    except (TypeError, ValueError) as exc:
        raise RecoveryControlError(f"recovery control JSON is not canonical: {path.name}") from exc
    if canonical != bytes(raw):
        raise RecoveryControlError(f"recovery control JSON is not canonical: {path.name}")
    return body


def _validate_operation(operation: Mapping[str, Any]) -> None:
    if set(operation) != _OPERATION_KEYS:
        raise RecoveryControlError("invalid recovery operation keys")
    operation_id = operation.get("id")
    if type(operation_id) is not str or not _OPERATION_ID_RE.fullmatch(operation_id):
        raise RecoveryControlError("invalid recovery operation id")
    if operation.get("format") != OPERATION_FORMAT or operation.get("kind") != "backup":
        raise RecoveryControlError("invalid recovery operation format")
    if operation.get("status") not in _OPERATION_STATES:
        raise RecoveryControlError("invalid recovery operation status")
    for key in ("created_at", "started_at", "finished_at"):
        if operation.get(key) is not None and not isinstance(operation.get(key), str):
            raise RecoveryControlError(f"invalid recovery operation {key}")
    for key in ("vendor_media", "full_rehash"):
        if type(operation.get(key)) is not bool:
            raise RecoveryControlError(f"invalid recovery operation {key}")
    snapshot_digest = operation.get("snapshot_digest")
    if snapshot_digest is not None and (
        not isinstance(snapshot_digest, str) or not _DIGEST_RE.fullmatch(snapshot_digest)
    ):
        raise RecoveryControlError("invalid recovery operation snapshot digest")
    if operation.get("receipt_id") is not None and not isinstance(operation.get("receipt_id"), str):
        raise RecoveryControlError("invalid recovery operation receipt id")
    if type(operation.get("stats")) is not dict:
        raise RecoveryControlError("invalid recovery operation stats")
    if operation.get("error") is not None and not isinstance(operation.get("error"), str):
        raise RecoveryControlError("invalid recovery operation error")


def new_backup_operation(*, data_dir: Path, vendor_media: bool, full_rehash: bool) -> dict:
    operation_id = (
        datetime.now(timezone.utc).strftime("backup-%Y%m%dT%H%M%SZ-")
        + secrets.token_hex(4)
    )
    operation = {
        "format": OPERATION_FORMAT,
        "id": operation_id,
        "kind": "backup",
        "status": "queued",
        "created_at": utc_now(),
        "started_at": None,
        "finished_at": None,
        "vendor_media": vendor_media,
        "full_rehash": full_rehash,
        "snapshot_digest": None,
        "receipt_id": None,
        "stats": {},
        "error": None,
    }
    write_operation(data_dir, operation)
    return operation


def operation_path(data_dir: Path, operation_id: str) -> Path:
    if not _OPERATION_ID_RE.fullmatch(operation_id):
        raise RecoveryControlError("invalid recovery operation id")
    return control_subdir(data_dir, "operations") / f"{operation_id}.json"


def write_operation(data_dir: Path, operation: Mapping[str, Any]) -> None:
    _validate_operation(operation)
    operation_id = operation["id"]
    assert isinstance(operation_id, str)
    _write_json_atomic(operation_path(data_dir, operation_id), operation)


def read_operation(data_dir: Path, operation_id: str) -> dict[str, Any]:
    body = _read_json(operation_path(data_dir, operation_id))
    if body.get("format") != OPERATION_FORMAT or body.get("id") != operation_id:
        raise RecoveryControlError("recovery operation identity mismatch")
    _validate_operation(body)
    return body


def list_operations(
    data_dir: Path, *, active_operation_ids: set[str] | None = None, limit: int = 10,
) -> list[dict[str, Any]]:
    directory = control_subdir(data_dir, "operations")
    active = active_operation_ids or set()
    result: list[dict[str, Any]] = []
    entries = sorted(directory.iterdir(), key=lambda item: item.name, reverse=True)
    for entry in entries:
        if len(result) >= limit or entry.is_symlink() or not entry.name.endswith(".json"):
            continue
        operation_id = entry.name[:-5]
        if not _OPERATION_ID_RE.fullmatch(operation_id):
            continue
        body = read_operation(data_dir, operation_id)
        status = body.get("status")
        if status not in _TERMINAL_OPERATION_STATES and operation_id not in active:
            body = dict(body)
            body["status"] = "interrupted"
            body["finished_at"] = body.get("finished_at") or body.get("started_at")
            body["error"] = body.get("error") or "API重启或后台进程中断;请检查仓库写锁"
        result.append(body)
    return result


def media_vendoring_available() -> bool:
    try:
        roots = source_roots_from_env()
    except SourceReferenceError:
        return False
    return bool(roots) and all(path.is_dir() and not path.is_symlink() for path in roots.values())


def _snapshot_view(
    repository: ContentRepository,
    digest: str,
    *,
    refs: list[str],
    receipt: Mapping[str, Any] | None,
) -> dict[str, Any]:
    body = repository.get_snapshot(digest, verify_closure=False)
    completeness = dict(body.get("completeness") or {})
    return {
        "digest": digest,
        "refs": refs,
        "created_at": receipt.get("observed_at") if receipt else None,
        "source_app_version": str((body.get("source") or {}).get("app_version") or ""),
        "partial": bool((body.get("selector") or {}).get("partial")),
        "portable_ready": completeness.get("portable_ready") is True,
        "readiness_reasons": list(completeness.get("readiness_reasons") or []),
        "completeness": completeness,
        "stats": dict(receipt.get("stats") or {}) if receipt else {},
    }


def repository_status(*, data_dir: Path, active_operation_ids: set[str]) -> dict[str, Any]:
    path = repository_path()
    base = {
        "state": "empty",
        "repository_path": str(path),
        "host_repository_env": "FLORI_CONTENT_REPOSITORY_DIR",
        "write_lock": None,
        "latest": None,
        "snapshots": [],
        "media_vendoring_available": media_vendoring_available(),
        "deployment_id_configured": bool(valid_deployment_id()),
        "online_restore_supported": False,
        "operations": list_operations(
            data_dir, active_operation_ids=active_operation_ids,
        ),
        "error": None,
    }
    try:
        validate_repository_boundary(data_dir)
    except RecoveryControlError as exc:
        return {**base, "state": "error", "error": str(exc)}
    if not path.exists():
        return base
    if path.is_symlink() or not path.is_dir():
        return {**base, "state": "error", "error": "便携仓库路径不安全"}
    if not (path / "repository.json").is_file():
        if any(path.iterdir()):
            return {**base, "state": "error", "error": "目录非空但不是便携仓库"}
        return base
    try:
        repository = ContentRepository.open(path)
        holder = repository.write_lock_holder()
        refs = repository.list_refs()
        latest_digest = refs.get("latest")
        latest_receipt: dict[str, Any] | None = None
        if latest_digest is not None:
            # 设置页轮询只需要latest统计;从尾部命中即停,不能每2秒重读多年receipt。
            for receipt_id in reversed(repository.list_receipts()):
                receipt = repository.read_receipt(receipt_id)
                if (
                    receipt.get("outcome") == "success"
                    and receipt.get("snapshot_digest") == latest_digest
                ):
                    latest_receipt = receipt
                    break
        refs_by_digest: dict[str, list[str]] = {}
        for name, digest in refs.items():
            refs_by_digest.setdefault(digest, []).append(name)
        snapshots = [
            _snapshot_view(
                repository,
                digest,
                refs=sorted(names),
                receipt=latest_receipt if digest == latest_digest else None,
            )
            for digest, names in sorted(refs_by_digest.items(), key=lambda item: item[0])
        ]
        latest = next((item for item in snapshots if item["digest"] == latest_digest), None)
        state = "locked" if holder else (
            "ready" if latest and latest["portable_ready"] else "incomplete"
            if latest else "empty"
        )
        return {
            **base,
            "state": state,
            "write_lock": (
                {
                    "owner": holder.get("owner"),
                    "acquired_at": holder.get("acquired_at"),
                }
                if holder else None
            ),
            "latest": latest,
            "snapshots": snapshots,
        }
    except (OSError, RepositoryError, RecoveryControlError) as exc:
        return {
            **base,
            "state": "error",
            "error": f"便携仓库不可读: {type(exc).__name__}",
        }


def valid_deployment_id() -> str | None:
    deployment_id = os.environ.get("FLORI_DEPLOYMENT_ID", "")
    if deployment_id == "unbound" or not _DEPLOYMENT_ID_RE.fullmatch(deployment_id):
        return None
    return deployment_id


def validate_snapshot_digest(value: str) -> str:
    if not _DIGEST_RE.fullmatch(value):
        raise RecoveryControlError("invalid snapshot digest")
    return value


def _valid_generated_at(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return False
    return moment.tzinfo is not None and moment.utcoffset() is not None


def build_restore_handoff(*, data_dir: Path, config, snapshot_digest: str) -> tuple[dict, bool]:
    """生成幂等离线交接单;只读仓库,绝不触碰线上数据库。"""
    digest = validate_snapshot_digest(snapshot_digest)
    deployment_id = valid_deployment_id()
    if deployment_id is None:
        raise RecoveryControlError("请先配置稳定的 FLORI_DEPLOYMENT_ID")
    repository = ContentRepository.open(validate_repository_physical_boundary(data_dir))
    plan_root = control_subdir(data_dir, "plan-targets")
    try:
        plan, _body, _records = build_plan(
            repository=repository,
            snapshot=digest,
            target_db_path=plan_root / f"{digest[7:23]}.db",
            allow_partial=False,
            verify_blobs=True,
            config=config,
            mode=MODE_EMPTY,
        )
    except ImportError_ as exc:
        raise RecoveryControlError(f"恢复预检失败: {exc}") from exc
    if not plan.portable_ready:
        reasons = ", ".join(plan.readiness_reasons) or "unknown"
        raise RecoveryControlError(f"快照不具备清库恢复闭包: {reasons}")
    if plan.conflicts:
        raise RecoveryControlError("恢复计划存在冲突: " + "; ".join(plan.conflicts))
    core = {
        "format": HANDOFF_FORMAT,
        "snapshot_digest": digest,
        "plan_digest": plan.plan_digest,
        "deployment_id": deployment_id,
        "app_version": FLORI_VERSION,
        "target_mode": MODE_EMPTY,
    }
    handoff_id = "restore-" + hashlib.sha256(canonical_json_bytes(core)).hexdigest()[:24]
    if not _HANDOFF_ID_RE.fullmatch(handoff_id):
        raise AssertionError("generated invalid restore handoff id")
    target_generation = f"gen-{handoff_id[8:]}"
    source_placeholders = " ".join(
        f"--source-root {root_id}=/path/to/{root_id}"
        for root_id in plan.required_source_roots
    )
    source_suffix = f" {source_placeholders}" if source_placeholders else ""
    snapshot_arg = f"--snapshot {digest}"
    repo_arg = '"${FLORI_CONTENT_REPOSITORY_DIR:?设置便携仓库宿主路径}"'
    handoff_body = {
        **core,
        "id": handoff_id,
        "target_generation": target_generation,
        "counts": dict(plan.counts),
        "bytes_to_write": plan.bytes_to_write,
        "required_source_roots": list(plan.required_source_roots),
        "commands": {
            "verify": (
                f"scripts/content-import.sh --repo {repo_arg} --db /data/db/analyzer.db "
                f"{snapshot_arg} --verify-only"
            ),
            "exact_dr": (
                f"FLORI_DEPLOYMENT_ID={deployment_id} "
                'scripts/backup.sh "${FLORI_DR_BACKUP_DIR:?设置exact DR目录}" '
                '--result-file "${FLORI_DR_BACKUP_DIR}/latest-dr.json"'
            ),
            "plan": (
                f"scripts/content-import.sh --repo {repo_arg} --db /data/db/analyzer.db "
                f"{snapshot_arg} --config-root /data/import-staging/prompts --plan"
                f"{source_suffix}"
            ),
            "restore": (
                f"FLORI_DEPLOYMENT_ID={deployment_id} FLORI_REMOTE_WORKERS_QUIESCED=1 "
                f"FLORI_DR_RECEIPT=\"$FLORI_DR_BACKUP_DIR/latest-dr.json\" "
                f"scripts/content-import.sh --repo {repo_arg} --db /data/db/analyzer.db "
                f"{snapshot_arg} --into-live --config-root /data/prompts "
                f"--target-generation {target_generation}{source_suffix}"
            ),
        },
    }
    handoff_path = control_subdir(data_dir, "handoffs") / f"{handoff_id}.json"
    if handoff_path.exists() or handoff_path.is_symlink():
        existing = _read_json(handoff_path)
        expected_keys = set(handoff_body) | {"generated_at"}
        if (
            set(existing) == expected_keys
            and all(existing.get(key) == value for key, value in handoff_body.items())
            and _valid_generated_at(existing.get("generated_at"))
        ):
            return existing, True
        raise RecoveryControlError("restore handoff file was modified or collided")
    handoff = {**handoff_body, "generated_at": utc_now()}
    _write_json_atomic(handoff_path, handoff)
    return handoff, False
