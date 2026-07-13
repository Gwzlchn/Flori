#!/usr/bin/env python3
"""生成、校验并以可回滚切换恢复 Flori 灾备快照."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import sys
import tarfile
import tempfile
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Callable


FORMAT_NAME = "flori-disaster-recovery"
FORMAT_VERSION = 1
MANIFEST_NAME = "manifest.json"
TRANSACTION_FILE = ".flori-dr-transaction.json"
STAGE_PREFIX = ".flori-dr-"


class SnapshotError(RuntimeError):
    """表示快照不完整或恢复无法在不破坏现态的前提下继续."""


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_json_atomic(path: Path, payload: dict[str, Any], mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(temporary, mode)
    os.replace(temporary, path)
    _fsync_dir(path.parent)


def _remove(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.exists():
        shutil.rmtree(path)


def _safe_generation(value: str) -> str:
    if not value or len(value) > 100:
        raise SnapshotError("generation 长度非法")
    if any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_." for ch in value):
        raise SnapshotError("generation 只允许字母、数字、点、下划线和短横线")
    if value in {".", ".."}:
        raise SnapshotError("generation 非法")
    return value


def _is_internal(rel: Path) -> bool:
    return bool(rel.parts and rel.parts[0].startswith(STAGE_PREFIX))


def _scan_tree(
    root: Path,
    excluded: Callable[[Path], bool] | None = None,
) -> dict[str, tuple[int, int, int, int, int]]:
    """记录正则文件的大小、mtime 和 mode,并拒绝会逃出资产根的链接."""
    if not root.is_dir():
        raise SnapshotError(f"资产根不存在或不是目录: {root}")
    result: dict[str, tuple[int, int, int, int, int]] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if _is_internal(rel) or (excluded and excluded(rel)):
            continue
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise SnapshotError(f"资产中不允许符号链接: {rel.as_posix()}")
        if stat.S_ISDIR(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode):
            raise SnapshotError(f"资产中不允许特殊文件: {rel.as_posix()}")
        result[rel.as_posix()] = (
            info.st_size,
            info.st_mtime_ns,
            stat.S_IMODE(info.st_mode),
            info.st_uid,
            info.st_gid,
        )
    return result


def _copy_stable_tree(
    source: Path,
    destination: Path,
    excluded: Callable[[Path], bool] | None = None,
) -> None:
    """拷贝前后比较源目录,捕捉演练或备份窗口内的并发写."""
    before = _scan_tree(source, excluded)
    destination.mkdir(parents=True, exist_ok=True)
    for path in sorted(source.rglob("*")):
        rel = path.relative_to(source)
        if _is_internal(rel) or (excluded and excluded(rel)):
            continue
        target = destination / rel
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise SnapshotError(f"资产中不允许符号链接: {rel.as_posix()}")
        if stat.S_ISDIR(info.st_mode):
            target.mkdir(parents=True, exist_ok=True)
            os.chmod(target, stat.S_IMODE(info.st_mode))
            with contextlib.suppress(PermissionError):
                os.chown(target, info.st_uid, info.st_gid)
        elif stat.S_ISREG(info.st_mode):
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            with contextlib.suppress(PermissionError):
                os.chown(target, info.st_uid, info.st_gid)
        else:
            raise SnapshotError(f"资产中不允许特殊文件: {rel.as_posix()}")
    after = _scan_tree(source, excluded)
    if before != after:
        raise SnapshotError("快照窗口内源资产发生变化，拒绝发布混代备份")


def _base_data_excluded(rel: Path) -> bool:
    return rel.as_posix() in {
        "db/analyzer.db",
        "db/analyzer.db-wal",
        "db/analyzer.db-shm",
        "db/analyzer.db-journal",
    }


def _sqlite_schema_hash(connection: sqlite3.Connection) -> str:
    rows = connection.execute(
        "SELECT type, name, tbl_name, COALESCE(sql, '') FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
    ).fetchall()
    encoded = json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _snapshot_sqlite(source: Path, destination: Path) -> dict[str, Any]:
    if not source.is_file():
        raise SnapshotError(f"SQLite 主库不存在: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    uri = f"file:{source.resolve().as_posix()}?mode=ro"
    src = sqlite3.connect(uri, uri=True, timeout=30)
    dst = sqlite3.connect(destination)
    try:
        src.backup(dst, pages=256, sleep=0.01)
        dst.commit()
        integrity = dst.execute("PRAGMA integrity_check").fetchone()
        if not integrity or integrity[0] != "ok":
            raise SnapshotError(f"SQLite 快照 integrity_check 失败: {integrity}")
        user_version = int(dst.execute("PRAGMA user_version").fetchone()[0])
        application_id = int(dst.execute("PRAGMA application_id").fetchone()[0])
        schema_hash = _sqlite_schema_hash(dst)
    finally:
        dst.close()
        src.close()
    source_info = source.stat()
    os.chmod(destination, stat.S_IMODE(source_info.st_mode))
    with contextlib.suppress(PermissionError):
        os.chown(destination, source_info.st_uid, source_info.st_gid)
    return {
        "path": "assets/data/db/analyzer.db",
        "integrity_check": "ok",
        "user_version": user_version,
        "application_id": application_id,
        "schema_sha256": schema_hash,
    }


def _asset_inventory(root: Path, archive_prefix: str) -> tuple[dict[str, dict[str, Any]], int, int]:
    files: dict[str, dict[str, Any]] = {}
    total_bytes = 0
    for rel, metadata in _scan_tree(root).items():
        path = root / rel
        size = metadata[0]
        total_bytes += size
        files[f"{archive_prefix}/{rel}"] = {
            "size": size,
            "sha256": _sha256(path),
            "mode": metadata[2],
            "uid": metadata[3],
            "gid": metadata[4],
        }
    return files, len(files), total_bytes


def _validate_redis_payload(root: Path) -> str:
    dump = root / "dump.rdb"
    if dump.is_file():
        with dump.open("rb") as stream:
            header = stream.read(9)
        if header[:5] != b"REDIS":
            raise SnapshotError("Redis dump.rdb 头部非法")
        return header.decode("ascii", errors="replace")
    aof_files = [
        path
        for path in root.rglob("*")
        if path.is_file() and ("appendonly" in path.name or "appendonly" in path.as_posix())
    ]
    if not aof_files or not any(path.stat().st_size > 0 for path in aof_files):
        raise SnapshotError("Redis 资产中无可恢复的 RDB 或 AOF")
    return "aof"


def _tar_stage(stage: Path, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise SnapshotError(f"输出快照已存在，拒绝覆盖: {output}")
    temporary = output.with_name(f".{output.name}.{uuid.uuid4().hex}.partial")
    try:
        with tarfile.open(temporary, "w:gz", format=tarfile.PAX_FORMAT) as archive:
            for child in sorted(stage.iterdir(), key=lambda item: item.name):
                archive.add(child, arcname=child.name, recursive=True)
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, output)
        _fsync_dir(output.parent)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def create_snapshot(
    *,
    data_root: Path,
    redis_root: Path,
    output: Path,
    generation: str,
    minio_root: Path | None = None,
    config_root: Path | None = None,
    app_version: str = "unknown",
    redis_mode: str = "offline-volume",
    data_excludes: tuple[str, ...] = (),
) -> dict[str, Any]:
    """生成完整快照,只有全部资产和校验通过才原子发布归档."""
    started_monotonic = time.monotonic()
    started_at = _utc_now()
    generation = _safe_generation(generation)
    excluded_subtrees: set[PurePosixPath] = set()
    for value in data_excludes:
        path = PurePosixPath(value)
        if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
            raise SnapshotError(f"数据排除路径非法: {value}")
        excluded_subtrees.add(path)
    if minio_root is not None and minio_root.exists():
        for child in data_root.iterdir():
            with contextlib.suppress(OSError):
                if child.is_dir() and os.path.samefile(child, minio_root):
                    excluded_subtrees.add(PurePosixPath(child.name))

    def data_excluded(rel: Path) -> bool:
        pure = PurePosixPath(rel.as_posix())
        return _base_data_excluded(rel) or any(
            pure == excluded or excluded in pure.parents for excluded in excluded_subtrees
        )

    with tempfile.TemporaryDirectory(prefix="flori-dr-create-") as temporary:
        stage = Path(temporary)
        assets_root = stage / "assets"
        data_destination = assets_root / "data"
        _copy_stable_tree(data_root, data_destination, data_excluded)
        sqlite_meta = _snapshot_sqlite(
            data_root / "db" / "analyzer.db",
            data_destination / "db" / "analyzer.db",
        )

        redis_destination = assets_root / "redis"
        if redis_mode == "rdb":
            redis_dump = redis_root / "dump.rdb"
            if not redis_dump.is_file():
                raise SnapshotError("Redis SAVE 后未找到 dump.rdb")
            redis_destination.mkdir(parents=True, exist_ok=True)
            shutil.copy2(redis_dump, redis_destination / "dump.rdb")
        elif redis_mode in {"offline-volume", "materialized-rdb-aof"}:
            _copy_stable_tree(redis_root, redis_destination)
        else:
            raise SnapshotError(f"不支持的 Redis 快照模式: {redis_mode}")
        redis_format = _validate_redis_payload(redis_destination)

        included_roots: dict[str, Path] = {
            "data": data_destination,
            "redis": redis_destination,
        }
        capture_modes = {
            "data": "stable-filesystem-copy+sqlite-online-backup",
            "redis": redis_mode,
        }
        schema_versions: dict[str, str | int] = {
            "data": sqlite_meta["user_version"],
            "redis": redis_format,
            "minio": "filesystem-v1",
            "config": "filesystem-v1",
        }
        if minio_root is not None:
            minio_destination = assets_root / "minio"
            _copy_stable_tree(minio_root, minio_destination)
            included_roots["minio"] = minio_destination
            capture_modes["minio"] = "stable-filesystem-copy"
        if config_root is not None:
            config_destination = assets_root / "config"
            _copy_stable_tree(config_root, config_destination)
            included_roots["config"] = config_destination
            capture_modes["config"] = "stable-filesystem-copy"

        files: dict[str, dict[str, Any]] = {}
        assets: dict[str, dict[str, Any]] = {}
        for name in ("data", "redis", "minio", "config"):
            root = included_roots.get(name)
            if root is None:
                assets[name] = {
                    "included": False,
                    "reason": "not-configured",
                    "path": f"assets/{name}",
                    "generation": generation,
                    "schema_version": schema_versions[name],
                }
                continue
            one_files, count, total = _asset_inventory(root, f"assets/{name}")
            files.update(one_files)
            assets[name] = {
                "included": True,
                "path": f"assets/{name}",
                "capture_mode": capture_modes[name],
                "generation": generation,
                "schema_version": schema_versions[name],
                "file_count": count,
                "total_bytes": total,
            }

        completed_at = _utc_now()
        manifest = {
            "format": FORMAT_NAME,
            "format_version": FORMAT_VERSION,
            "generation": generation,
            "created_at": completed_at,
            "capture": {
                "started_at": started_at,
                "completed_at": completed_at,
                "rpo_window_seconds": round(time.monotonic() - started_monotonic, 6),
            },
            "producer": {"app_version": app_version},
            "compatibility": {
                "min_restore_format": FORMAT_VERSION,
                "sqlite_user_version": sqlite_meta["user_version"],
            },
            "sqlite": sqlite_meta,
            "assets": assets,
            "files": files,
        }
        manifest["assets"]["data"]["excluded_external_subtrees"] = sorted(
            path.as_posix() for path in excluded_subtrees
        )
        _write_json_atomic(stage / MANIFEST_NAME, manifest)
        _tar_stage(stage, output)
    digest = _sha256(output)
    sidecar = output.with_suffix(output.suffix + ".sha256")
    sidecar_tmp = sidecar.with_name(f".{sidecar.name}.{uuid.uuid4().hex}.tmp")
    sidecar_tmp.write_text(f"{digest}  {output.name}\n", encoding="utf-8")
    os.chmod(sidecar_tmp, 0o600)
    os.replace(sidecar_tmp, sidecar)
    _fsync_dir(output.parent)
    return {
        "status": "success",
        "operation": "backup",
        "generation": generation,
        "archive": str(output),
        "archive_sha256": digest,
        "manifest": manifest,
        "elapsed_seconds": round(time.monotonic() - started_monotonic, 6),
    }


def _safe_archive_path(name: str) -> Path:
    pure = PurePosixPath(name)
    if pure.is_absolute() or not pure.parts or any(part in {"", ".", ".."} for part in pure.parts):
        raise SnapshotError(f"归档成员路径非法: {name}")
    return Path(*pure.parts)


def _extract_archive(archive_path: Path, destination: Path) -> None:
    seen: set[str] = set()
    try:
        archive = tarfile.open(archive_path, "r:gz")
    except (tarfile.TarError, OSError) as exc:
        raise SnapshotError(f"无法读取快照归档: {exc}") from exc
    with archive:
        try:
            members = archive.getmembers()
        except (tarfile.TarError, EOFError, OSError) as exc:
            raise SnapshotError(f"快照归档截断或损坏: {exc}") from exc
        for member in members:
            rel = _safe_archive_path(member.name.rstrip("/"))
            canonical = rel.as_posix()
            if canonical in seen:
                raise SnapshotError(f"归档含重复成员: {canonical}")
            seen.add(canonical)
            target = destination / rel
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                os.chmod(target, member.mode & 0o777)
                with contextlib.suppress(PermissionError):
                    os.chown(target, member.uid, member.gid)
                continue
            if not member.isfile():
                raise SnapshotError(f"归档不允许链接或特殊文件: {canonical}")
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise SnapshotError(f"无法读取归档成员: {canonical}")
            with source, target.open("wb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
            os.chmod(target, member.mode & 0o777)
            with contextlib.suppress(PermissionError):
                os.chown(target, member.uid, member.gid)


def _validate_sqlite(path: Path, expected: dict[str, Any], max_user_version: int | None) -> None:
    try:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
        schema_hash = _sqlite_schema_hash(connection)
    except sqlite3.DatabaseError as exc:
        raise SnapshotError(f"SQLite 快照无法打开: {exc}") from exc
    finally:
        with contextlib.suppress(UnboundLocalError):
            connection.close()
    if not integrity or integrity[0] != "ok":
        raise SnapshotError(f"SQLite integrity_check 失败: {integrity}")
    if user_version != int(expected.get("user_version", -1)):
        raise SnapshotError("SQLite user_version 与 manifest 不一致")
    if application_id != int(expected.get("application_id", -1)):
        raise SnapshotError("SQLite application_id 与 manifest 不一致")
    if schema_hash != expected.get("schema_sha256"):
        raise SnapshotError("SQLite schema 指纹与 manifest 不一致")
    if max_user_version is not None and user_version > max_user_version:
        raise SnapshotError(
            f"SQLite user_version={user_version} 超出当前恢复程序上限 {max_user_version}"
        )


def validate_extracted(root: Path, max_db_user_version: int | None = None) -> dict[str, Any]:
    manifest_path = root / MANIFEST_NAME
    if not manifest_path.is_file():
        raise SnapshotError("快照缺少 manifest.json")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise SnapshotError(f"manifest.json 无法读取: {exc}") from exc
    if manifest.get("format") != FORMAT_NAME:
        raise SnapshotError("快照 format 不匹配")
    if manifest.get("format_version") != FORMAT_VERSION:
        raise SnapshotError(
            f"不支持的快照 format_version={manifest.get('format_version')}"
        )
    _safe_generation(str(manifest.get("generation", "")))
    declared = manifest.get("files")
    if not isinstance(declared, dict):
        raise SnapshotError("manifest.files 必须是对象")
    actual: dict[str, Path] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise SnapshotError("解包后不允许符号链接")
        if path.is_file() and path != manifest_path:
            actual[path.relative_to(root).as_posix()] = path
    if set(actual) != set(declared):
        missing = sorted(set(declared) - set(actual))
        extra = sorted(set(actual) - set(declared))
        raise SnapshotError(f"快照成员与 manifest 不一致: missing={missing}, extra={extra}")
    for rel, path in actual.items():
        metadata = declared[rel]
        if not isinstance(metadata, dict):
            raise SnapshotError(f"manifest 文件元数据非法: {rel}")
        if path.stat().st_size != metadata.get("size"):
            raise SnapshotError(f"文件大小校验失败: {rel}")
        if _sha256(path) != metadata.get("sha256"):
            raise SnapshotError(f"文件 sha256 校验失败: {rel}")
        info = path.stat()
        if stat.S_IMODE(info.st_mode) != metadata.get("mode"):
            raise SnapshotError(f"文件 mode 校验失败: {rel}")
        if info.st_uid != metadata.get("uid") or info.st_gid != metadata.get("gid"):
            raise SnapshotError(f"文件 owner 校验失败: {rel}")
    sqlite_meta = manifest.get("sqlite")
    if not isinstance(sqlite_meta, dict):
        raise SnapshotError("manifest.sqlite 缺失")
    sqlite_rel = sqlite_meta.get("path")
    if sqlite_rel not in actual:
        raise SnapshotError("manifest.sqlite.path 不在快照成员中")
    _validate_sqlite(actual[sqlite_rel], sqlite_meta, max_db_user_version)
    assets = manifest.get("assets")
    if not isinstance(assets, dict):
        raise SnapshotError("manifest.assets 缺失")
    for required in ("data", "redis"):
        if not isinstance(assets.get(required), dict) or not assets[required].get("included"):
            raise SnapshotError(f"快照缺少必需资产: {required}")
    return manifest


def _validate_archive_sidecar(archive_path: Path) -> None:
    sidecar = archive_path.with_suffix(archive_path.suffix + ".sha256")
    if not sidecar.is_file():
        raise SnapshotError(f"快照缺少外部 sha256 文件: {sidecar.name}")
    parts = sidecar.read_text(encoding="utf-8").strip().split()
    if len(parts) != 2 or len(parts[0]) != 64 or parts[1] != archive_path.name:
        raise SnapshotError("快照 sha256 文件格式非法")
    if not all(ch in "0123456789abcdefABCDEF" for ch in parts[0]):
        raise SnapshotError("快照 sha256 值非法")
    if _sha256(archive_path) != parts[0].lower():
        raise SnapshotError("快照外部 sha256 校验失败")


def validate_archive(archive_path: Path, max_db_user_version: int | None = None) -> dict[str, Any]:
    _validate_archive_sidecar(archive_path)
    with tempfile.TemporaryDirectory(prefix="flori-dr-validate-") as temporary:
        root = Path(temporary)
        _extract_archive(archive_path, root)
        return validate_extracted(root, max_db_user_version)


def _marker_path(target: Path) -> Path:
    return target / TRANSACTION_FILE


def _load_marker(target: Path) -> dict[str, Any] | None:
    path = _marker_path(target)
    if not path.exists():
        return None
    try:
        marker = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SnapshotError(f"恢复事务标记损坏，需人工检查 {path}: {exc}") from exc
    if not isinstance(marker, dict):
        raise SnapshotError(f"恢复事务标记非法: {path}")
    return marker


def _persist_marker(target: Path, marker: dict[str, Any]) -> None:
    _write_json_atomic(_marker_path(target), marker)


def _rollback_target(target: Path) -> None:
    marker = _load_marker(target)
    if marker is None:
        return
    base = target / str(marker.get("base", ""))
    new_root = base / "new"
    old_root = base / "old"
    old_names = marker.get("old_names", marker.get("moved_old", []))
    new_names = marker.get("new_names", marker.get("moved_new", []))
    for name in reversed(new_names):
        current = target / name
        old_was_moved = (old_root / name).exists() or (old_root / name).is_symlink()
        was_new_only = name not in old_names
        if (old_was_moved or was_new_only) and (current.exists() or current.is_symlink()):
            new_root.mkdir(parents=True, exist_ok=True)
            _remove(new_root / name)
            os.replace(current, new_root / name)
    for name in reversed(old_names):
        old = old_root / name
        if old.exists() or old.is_symlink():
            _remove(target / name)
            os.replace(old, target / name)
    _fsync_dir(target)
    _marker_path(target).unlink(missing_ok=True)
    _remove(base)
    _fsync_dir(target)


def _recover_target(target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    if _marker_path(target).exists():
        _rollback_target(target)
    for child in target.iterdir():
        if child.name.startswith(STAGE_PREFIX):
            _remove(child)


def _recover_target_set(targets: list[Path]) -> None:
    """任一目标已 accepted 说明全部 commit 已完成,否则统一回滚中断事务."""
    markers = {target: _load_marker(target) for target in targets if target.exists()}
    active = {target: marker for target, marker in markers.items() if marker is not None}
    if any(marker.get("status") == "accepted" for marker in active.values()):
        invalid = {
            str(target): marker.get("status")
            for target, marker in active.items()
            if marker.get("status") not in {"committed", "accepted"}
        }
        if invalid:
            raise SnapshotError(f"已接受的恢复事务与未提交目标混合: {invalid}")
        for target, marker in active.items():
            if marker.get("status") == "committed":
                marker["status"] = "accepted"
                _persist_marker(target, marker)
        for target in active:
            _finalize_target(target)
    else:
        for target in reversed(list(active)):
            _rollback_target(target)
    for target in targets:
        _recover_target(target)


def _prepare_target(
    source: Path,
    target: Path,
    generation: str,
    asset: str,
    preserve_names: set[str] | None = None,
) -> dict[str, Any]:
    target.mkdir(parents=True, exist_ok=True)
    base_name = f"{STAGE_PREFIX}{generation}"
    base = target / base_name
    new_root = base / "new"
    old_root = base / "old"
    _remove(base)
    new_root.mkdir(parents=True)
    old_root.mkdir(parents=True)
    try:
        _copy_stable_tree(source, new_root)
        preserve_names = preserve_names or set()
        overlap = preserve_names & {child.name for child in new_root.iterdir()}
        if overlap:
            raise SnapshotError(f"快照资产与外部挂载保留路径冲突: {sorted(overlap)}")
    except BaseException:
        _remove(base)
        raise
    marker = {
        "format": FORMAT_NAME,
        "generation": generation,
        "asset": asset,
        "base": base_name,
        "status": "prepared",
        "old_names": [],
        "new_names": [],
        "preserve_names": sorted(preserve_names),
        "moved_old": [],
        "moved_new": [],
    }
    _persist_marker(target, marker)
    return marker


def _commit_target(target: Path) -> None:
    marker = _load_marker(target)
    if marker is None or marker.get("status") != "prepared":
        raise SnapshotError(f"目标未处于 prepared 状态: {target}")
    base = target / marker["base"]
    new_root = base / "new"
    old_root = base / "old"
    try:
        current_names = sorted(
            child.name
            for child in target.iterdir()
            if not child.name.startswith(STAGE_PREFIX)
            and child.name not in set(marker.get("preserve_names", []))
        )
        new_names = sorted(child.name for child in new_root.iterdir())
        marker["old_names"] = current_names
        marker["new_names"] = new_names
        marker["status"] = "switching"
        _persist_marker(target, marker)
        for name in current_names:
            os.replace(target / name, old_root / name)
            marker["moved_old"].append(name)
            _persist_marker(target, marker)
        for child in sorted(new_root.iterdir(), key=lambda item: item.name):
            name = child.name
            os.replace(child, target / name)
            marker["moved_new"].append(name)
            _persist_marker(target, marker)
        marker["status"] = "committed"
        _persist_marker(target, marker)
        _fsync_dir(target)
    except BaseException:
        _rollback_target(target)
        raise


def _finalize_target(target: Path) -> None:
    marker = _load_marker(target)
    if marker is None or marker.get("status") != "accepted":
        raise SnapshotError(f"目标未处于 accepted 状态: {target}")
    base = target / marker["base"]
    _marker_path(target).unlink(missing_ok=True)
    _remove(base)
    _fsync_dir(target)


def _accept_target(target: Path) -> None:
    marker = _load_marker(target)
    if marker is None or marker.get("status") != "committed":
        raise SnapshotError(f"目标未处于 committed 状态: {target}")
    marker["status"] = "accepted"
    _persist_marker(target, marker)


def _validate_target_roots(targets: dict[str, Path]) -> None:
    resolved = {name: path.resolve() for name, path in targets.items()}
    values = list(resolved.items())
    for index, (left_name, left) in enumerate(values):
        for right_name, right in values[index + 1 :]:
            if left == right:
                raise SnapshotError(
                    f"恢复目标必须相互独立: {left_name}={left}, {right_name}={right}"
                )


def _target_preserves(targets: dict[str, Path]) -> dict[str, set[str]]:
    preserves = {name: set() for name in targets}
    resolved = {name: path.resolve() for name, path in targets.items()}
    for parent_name, parent in resolved.items():
        for child_name, child in resolved.items():
            if parent_name == child_name:
                continue
            if parent in child.parents:
                preserves[parent_name].add(child.relative_to(parent).parts[0])
    for parent_name, parent in targets.items():
        if not parent.exists():
            continue
        for entry in parent.iterdir():
            if not entry.is_dir() or entry.name.startswith(STAGE_PREFIX):
                continue
            for child_name, child in targets.items():
                if parent_name == child_name or not child.exists():
                    continue
                with contextlib.suppress(OSError):
                    if os.path.samefile(entry, child):
                        preserves[parent_name].add(entry.name)
    return preserves


def restore_snapshot(
    *,
    archive_path: Path,
    targets: dict[str, Path],
    max_db_user_version: int | None = None,
    fail_after_commits: int | None = None,
) -> dict[str, Any]:
    """校验后两阶段切换所有目标,后续目标失败会回滚已切换目标."""
    started_at = _utc_now()
    started_monotonic = time.monotonic()
    _validate_target_roots(targets)
    for target in targets.values():
        target.mkdir(parents=True, exist_ok=True)
    preserves = _target_preserves(targets)
    _validate_archive_sidecar(archive_path)
    with tempfile.TemporaryDirectory(prefix="flori-dr-restore-") as temporary:
        extracted = Path(temporary)
        _extract_archive(archive_path, extracted)
        manifest = validate_extracted(extracted, max_db_user_version)
        generation = manifest["generation"]
        included = {
            name
            for name, metadata in manifest["assets"].items()
            if isinstance(metadata, dict) and metadata.get("included")
        }
        missing_targets = sorted((included - {"config"}) - set(targets))
        if missing_targets:
            raise SnapshotError(f"快照资产缺少恢复目标: {missing_targets}")
        selected = [name for name in ("data", "redis", "minio", "config") if name in included and name in targets]
        _recover_target_set([targets[name] for name in selected])
        prepared: list[str] = []
        committed: list[str] = []
        accepted = False
        try:
            for name in selected:
                _prepare_target(
                    extracted / "assets" / name,
                    targets[name],
                    generation,
                    name,
                    preserves[name],
                )
                prepared.append(name)
            for name in selected:
                _commit_target(targets[name])
                committed.append(name)
                if fail_after_commits is not None and len(committed) >= fail_after_commits:
                    raise SnapshotError("测试故障注入: 目标切换后中断")
            for name in selected:
                _accept_target(targets[name])
            accepted = True
        except BaseException:
            for name in reversed(committed):
                with contextlib.suppress(Exception):
                    _rollback_target(targets[name])
            for name in reversed(prepared):
                with contextlib.suppress(Exception):
                    _rollback_target(targets[name])
            raise
        cleanup_pending: list[str] = []
        if accepted:
            for name in selected:
                try:
                    _finalize_target(targets[name])
                except Exception:
                    cleanup_pending.append(name)
    return {
        "status": "success",
        "operation": "restore",
        "generation": generation,
        "started_at": started_at,
        "completed_at": _utc_now(),
        "rto_seconds": round(time.monotonic() - started_monotonic, 6),
        "restored_assets": selected,
        "skipped_assets": sorted(included - set(selected)),
        "cleanup_pending": cleanup_pending,
        "preserved_target_entries": {
            name: sorted(values) for name, values in preserves.items() if values
        },
        "checks": {
            "archive_members": "ok",
            "checksums": "ok",
            "sqlite_integrity": "ok",
            "compatibility": "ok",
            "atomic_switch": "ok",
        },
    }


def run_empty_environment_drill(result_file: Path | None = None) -> dict[str, Any]:
    """在隔离临时根中演练完整恢复、损坏拒绝和跨目标回滚."""
    started = time.monotonic()
    checks: dict[str, str] = {}
    with tempfile.TemporaryDirectory(prefix="flori-dr-drill-") as temporary:
        root = Path(temporary)
        source = {name: root / "source" / name for name in ("data", "redis", "minio", "config")}
        target = {name: root / "target" / name for name in source}
        for path in [*source.values(), *target.values()]:
            path.mkdir(parents=True)
        database = source["data"] / "db" / "analyzer.db"
        database.parent.mkdir(parents=True)
        connection = sqlite3.connect(database)
        connection.execute("CREATE TABLE jobs(id TEXT PRIMARY KEY, title TEXT NOT NULL)")
        connection.execute("INSERT INTO jobs VALUES('jobs_drill', '灾备演练')")
        connection.commit()
        connection.close()
        (source["data"] / "jobs" / "jobs_drill").mkdir(parents=True)
        (source["data"] / "jobs" / "jobs_drill" / "note.md").write_text("演练笔记\n", encoding="utf-8")
        (source["data"] / "prompts" / "profiles").mkdir(parents=True)
        (source["data"] / "prompts" / "profiles" / "general.yaml").write_text("role: drill\n", encoding="utf-8")
        (source["redis"] / "dump.rdb").write_bytes(b"REDIS-DRILL")
        (source["minio"] / "flori" / "jobs_drill").mkdir(parents=True)
        (source["minio"] / "flori" / "jobs_drill" / "artifact.bin").write_bytes(b"OBJECT-DRILL")
        (source["config"] / "pipelines.yaml").write_text("pipelines: {}\n", encoding="utf-8")

        archive = root / "backups" / "flori-backup-drill.tar.gz"
        backup_result = create_snapshot(
            data_root=source["data"],
            redis_root=source["redis"],
            minio_root=source["minio"],
            config_root=source["config"],
            output=archive,
            generation="drill",
            redis_mode="offline-volume",
            app_version="drill",
        )
        checks["backup_atomic_publish"] = "ok"

        sentinel = target["data"] / "sentinel.txt"
        sentinel.write_text("current", encoding="utf-8")
        corrupt = root / "backups" / "corrupt.tar.gz"
        raw = archive.read_bytes()
        corrupt.write_bytes(raw[: max(1, len(raw) // 2)])
        try:
            restore_snapshot(archive_path=corrupt, targets=target)
        except SnapshotError:
            pass
        else:
            raise SnapshotError("损坏快照未被拒绝")
        if sentinel.read_text(encoding="utf-8") != "current":
            raise SnapshotError("损坏快照校验失败后修改了现态")
        checks["corrupt_snapshot_fail_closed"] = "ok"

        restore_result = restore_snapshot(archive_path=archive, targets=target)
        connection = sqlite3.connect(target["data"] / "db" / "analyzer.db")
        row = connection.execute("SELECT title FROM jobs WHERE id='jobs_drill'").fetchone()
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        connection.close()
        if row != ("灾备演练",) or integrity != "ok":
            raise SnapshotError("空环境恢复后关键业务查询失败")
        if not (target["data"] / "jobs" / "jobs_drill" / "note.md").is_file():
            raise SnapshotError("空环境恢复缺少 job 产物")
        if not (target["data"] / "prompts" / "profiles" / "general.yaml").is_file():
            raise SnapshotError("空环境恢复缺少 prompt profile")
        if not (target["redis"] / "dump.rdb").is_file():
            raise SnapshotError("空环境恢复缺少 Redis 快照")
        if not (target["minio"] / "flori" / "jobs_drill" / "artifact.bin").is_file():
            raise SnapshotError("空环境恢复缺少 MinIO 对象")
        if not (target["config"] / "pipelines.yaml").is_file():
            raise SnapshotError("空环境恢复缺少必要配置")
        checks["empty_environment_restore"] = "ok"

        before_failure = {
            name: sorted((path.relative_to(target[name]).as_posix(), _sha256(path))
                         for path in target[name].rglob("*") if path.is_file())
            for name in target
        }
        try:
            restore_snapshot(archive_path=archive, targets=target, fail_after_commits=1)
        except SnapshotError:
            pass
        else:
            raise SnapshotError("故障注入未中断恢复")
        after_failure = {
            name: sorted((path.relative_to(target[name]).as_posix(), _sha256(path))
                         for path in target[name].rglob("*") if path.is_file())
            for name in target
        }
        if before_failure != after_failure:
            raise SnapshotError("恢复中断后未回滚到原现态")
        checks["cross_asset_rollback"] = "ok"

        result = {
            "status": "success",
            "operation": "empty-environment-drill",
            "generation": backup_result["generation"],
            "rpo_seconds": backup_result["manifest"]["capture"]["rpo_window_seconds"],
            "rto_seconds": restore_result["rto_seconds"],
            "elapsed_seconds": round(time.monotonic() - started, 6),
            "checks": checks,
        }
    if result_file is not None:
        _write_json_atomic(result_file, result)
    return result


def _write_result(path: Path | None, result: dict[str, Any]) -> None:
    if path is not None:
        _write_json_atomic(path, result)


def _path_or_none(value: str | None) -> Path | None:
    return Path(value) if value else None


def _chown_if_requested(path: Path | None, uid: int | None, gid: int | None) -> None:
    if path is not None and path.exists() and uid is not None and gid is not None:
        os.chown(path, uid, gid)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create", help="生成并原子发布快照")
    create.add_argument("--data", required=True)
    create.add_argument("--redis", required=True)
    create.add_argument("--minio")
    create.add_argument("--config")
    create.add_argument("--output", required=True)
    create.add_argument("--generation", required=True)
    create.add_argument("--app-version", default="unknown")
    create.add_argument(
        "--redis-mode",
        choices=("rdb", "offline-volume", "materialized-rdb-aof"),
        default="offline-volume",
    )
    create.add_argument("--data-exclude", action="append", default=[])
    create.add_argument("--result-file")
    create.add_argument("--owner-uid", type=int)
    create.add_argument("--owner-gid", type=int)

    validate = subparsers.add_parser("validate", help="只读校验快照")
    validate.add_argument("--archive", required=True)
    validate.add_argument("--max-db-user-version", type=int)
    validate.add_argument("--result-file")
    validate.add_argument("--owner-uid", type=int)
    validate.add_argument("--owner-gid", type=int)

    restore = subparsers.add_parser("restore", help="校验后两阶段恢复")
    restore.add_argument("--archive", required=True)
    restore.add_argument("--data-target", required=True)
    restore.add_argument("--redis-target", required=True)
    restore.add_argument("--minio-target")
    restore.add_argument("--config-target")
    restore.add_argument("--max-db-user-version", type=int)
    restore.add_argument("--result-file")
    restore.add_argument("--owner-uid", type=int)
    restore.add_argument("--owner-gid", type=int)

    drill = subparsers.add_parser("drill", help="运行隔离空环境恢复演练")
    drill.add_argument("--result-file")
    drill.add_argument("--owner-uid", type=int)
    drill.add_argument("--owner-gid", type=int)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    result_file = _path_or_none(getattr(args, "result_file", None))
    requested_result_file = result_file
    try:
        if args.command == "create":
            result = create_snapshot(
                data_root=Path(args.data),
                redis_root=Path(args.redis),
                minio_root=_path_or_none(args.minio),
                config_root=_path_or_none(args.config),
                output=Path(args.output),
                generation=args.generation,
                app_version=args.app_version,
                redis_mode=args.redis_mode,
                data_excludes=tuple(args.data_exclude),
            )
        elif args.command == "validate":
            manifest = validate_archive(Path(args.archive), args.max_db_user_version)
            result = {
                "status": "success",
                "operation": "validate",
                "generation": manifest["generation"],
                "assets": manifest["assets"],
                "checks": {"members": "ok", "checksums": "ok", "sqlite": "ok", "compatibility": "ok"},
            }
        elif args.command == "restore":
            targets = {"data": Path(args.data_target), "redis": Path(args.redis_target)}
            if args.minio_target:
                targets["minio"] = Path(args.minio_target)
            if args.config_target:
                targets["config"] = Path(args.config_target)
            result = restore_snapshot(
                archive_path=Path(args.archive),
                targets=targets,
                max_db_user_version=args.max_db_user_version,
            )
        else:
            result = run_empty_environment_drill(result_file)
            result_file = None
        _write_result(result_file, result)
        if args.command == "create":
            archive = Path(args.output)
            _chown_if_requested(archive, args.owner_uid, args.owner_gid)
            _chown_if_requested(archive.with_suffix(archive.suffix + ".sha256"), args.owner_uid, args.owner_gid)
        _chown_if_requested(requested_result_file, args.owner_uid, args.owner_gid)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (SnapshotError, OSError, sqlite3.Error, tarfile.TarError) as exc:
        failure = {
            "status": "failed",
            "operation": args.command,
            "error_type": type(exc).__name__,
            "error": str(exc),
            "completed_at": _utc_now(),
        }
        with contextlib.suppress(Exception):
            _write_result(result_file, failure)
            _chown_if_requested(requested_result_file, args.owner_uid, args.owner_gid)
        print(json.dumps(failure, ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
