#!/usr/bin/env python3
"""生成、校验并以可回滚切换恢复 Flori 灾备快照."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import importlib.util
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
FORMAT_VERSION = 2
CROSS_DEPLOYMENT_CONFIRMATION = "REPLACE_OTHER_FLORI_DEPLOYMENT"
SUPPORTED_FORMAT_VERSIONS = frozenset({1, FORMAT_VERSION})
MANIFEST_NAME = "manifest.json"
TRANSACTION_FILE = ".flori-dr-transaction.json"
STAGE_PREFIX = ".flori-dr-"
SCHEMA_MANIFEST_FORMAT = "flori-sqlite-migrations"
DEFAULT_SCHEMA_MANIFEST = Path(__file__).parents[1] / "shared" / "migrations" / "manifest.json"


class SnapshotError(RuntimeError):
    """表示快照不完整或恢复无法在不破坏现态的前提下继续."""


def _schema_manifest(path: Path) -> dict[str, Any]:
    """校验数据库迁移清单，避免灾备脚本凭手工上限放行 schema。"""
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SnapshotError(f"无法读取 schema manifest {path}: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("format") != SCHEMA_MANIFEST_FORMAT:
        raise SnapshotError("schema manifest format 不匹配")
    minimum = manifest.get("minimum_supported_version")
    current = manifest.get("current_version")
    ledger = manifest.get("ledger_version")
    if (
        type(minimum) is not int
        or type(current) is not int
        or type(ledger) is not int
        or minimum != 0
        or current < 1
        or not 1 <= ledger <= current
    ):
        raise SnapshotError("schema manifest 版本边界非法")
    migrations = manifest.get("migrations")
    if not isinstance(migrations, list) or len(migrations) != current:
        raise SnapshotError("schema manifest 必须覆盖 1..current_version")
    for expected, migration in enumerate(migrations, start=1):
        if (
            not isinstance(migration, dict)
            or type(migration.get("version")) is not int
            or migration["version"] != expected
            or not isinstance(migration.get("name"), str)
            or not migration["name"]
        ):
            raise SnapshotError("schema manifest 迁移版本或名称非法")
        checksum = migration.get("checksum")
        if (
            not isinstance(checksum, str)
            or len(checksum) != 64
            or any(ch not in "0123456789abcdef" for ch in checksum)
        ):
            raise SnapshotError("schema manifest 迁移 checksum 非法")
    return {
        "minimum_supported_version": minimum,
        "current_version": current,
        "ledger_version": ledger,
        "migrations": [
            {
                "version": migration["version"],
                "name": migration["name"],
                "checksum": migration["checksum"],
            }
            for migration in migrations
        ],
    }


def _migration_history_fingerprint(history: list[dict[str, Any]]) -> str:
    encoded = json.dumps(
        history, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _schema_support(
    schema_manifest_path: Path | None,
    *,
    allow_synthetic_zero: bool = False,
) -> dict[str, Any]:
    candidate = schema_manifest_path
    if candidate is None and DEFAULT_SCHEMA_MANIFEST.is_file():
        candidate = DEFAULT_SCHEMA_MANIFEST
    if candidate is not None:
        resolved = candidate.resolve()
        return {**_schema_manifest(resolved), "_manifest_path": resolved}
    if allow_synthetic_zero:
        # 仅供隔离 drill 自建 user_version=0 样本，不用于外部归档恢复。
        return {
            "minimum_supported_version": 0,
            "current_version": 0,
            "ledger_version": 1,
            "migrations": [],
        }
    raise SnapshotError("缺少本地 schema manifest，拒绝在无版本上限时处理快照")


def _effective_schema_range(
    schema_manifest_path: Path | None,
    *,
    minimum_override: int | None = None,
    maximum_override: int | None = None,
    allow_synthetic_zero: bool = False,
) -> dict[str, Any]:
    support = _schema_support(
        schema_manifest_path, allow_synthetic_zero=allow_synthetic_zero
    )
    minimum = support["minimum_supported_version"]
    maximum = support["current_version"]
    if minimum_override is not None:
        if type(minimum_override) is not int or minimum_override < minimum:
            raise SnapshotError("显式 schema 下限不得放宽本地 migration manifest")
        minimum = minimum_override
    if maximum_override is not None:
        if type(maximum_override) is not int or maximum_override > maximum:
            raise SnapshotError("显式 schema 上限不得放宽本地 migration manifest")
        maximum = maximum_override
    if minimum > maximum:
        raise SnapshotError("schema 兼容范围为空")
    return {**support, "minimum": minimum, "maximum": maximum}


def _load_migration_runtime(
    schema_support: dict[str, Any],
) -> tuple[Any, str]:
    """从 manifest 同目录加载生产 migration package，不复制结构规则。"""
    manifest_path = schema_support.get("_manifest_path")
    if not isinstance(manifest_path, Path):
        raise SnapshotError("缺少 migration package 路径，无法验证应用可启动性")
    package_dir = manifest_path.parent
    init_path = package_dir / "__init__.py"
    if not init_path.is_file():
        raise SnapshotError(f"缺少 migration package: {init_path}")
    package_name = f"_flori_dr_migrations_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(
        package_name,
        init_path,
        submodule_search_locations=[str(package_dir)],
    )
    if spec is None or spec.loader is None:
        raise SnapshotError("无法创建 migration package loader")
    module = importlib.util.module_from_spec(spec)
    sys.modules[package_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException as exc:
        _unload_migration_runtime(package_name)
        if not isinstance(exc, Exception):
            raise
        raise SnapshotError(f"无法加载 migration package: {exc}") from exc
    if not callable(getattr(module, "migration_steps", None)) or not callable(
        getattr(module, "run_migrations", None)
    ):
        _unload_migration_runtime(package_name)
        raise SnapshotError("migration package 缺少统一 registry/runner 入口")
    return module, package_name


def _unload_migration_runtime(package_name: str) -> None:
    loaded_names = [
        name
        for name in sys.modules
        if name == package_name or name.startswith(package_name + ".")
    ]
    for loaded in loaded_names:
        sys.modules.pop(loaded, None)


def _run_frozen_migration_chain(
    connection: sqlite3.Connection,
    schema_support: dict[str, Any],
) -> int:
    target = int(schema_support["current_version"])
    if target == 0:
        return int(connection.execute("PRAGMA user_version").fetchone()[0])
    runtime, package_name = _load_migration_runtime(schema_support)
    try:
        return int(
            runtime.run_migrations(
                connection,
                runtime.migration_steps(),
                manifest_path=schema_support["_manifest_path"],
                target_version=target,
            )
        )
    except Exception as exc:
        raise SnapshotError(f"SQLite 无法按冻结 migration chain 启动: {exc}") from exc
    finally:
        _unload_migration_runtime(package_name)


def _validate_application_schema(
    path: Path,
    schema_support: dict[str, Any],
) -> None:
    """只迁移临时副本，证明归档 DB 能按当前生产入口启动。"""
    with tempfile.TemporaryDirectory(prefix="flori-dr-schema-check-") as temporary:
        validation_copy = Path(temporary) / "analyzer.db"
        shutil.copy2(path, validation_copy)
        connection = sqlite3.connect(validation_copy)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            final = _run_frozen_migration_chain(connection, schema_support)
            if final != schema_support["current_version"]:
                raise SnapshotError(
                    f"migration chain 结束于 v{final}，预期 v{schema_support['current_version']}"
                )
        finally:
            connection.close()


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
    # Worker 模型缓存可重建且按上游约定包含同树相对 symlink。把它当持久资产会让
    # fail-closed 的归档路径门拒绝整代备份,也会无意义放大每代体积。
    if len(rel.parts) >= 3 and rel.parts[0] == "workers" and rel.parts[2] == ".cache":
        return True
    return rel.as_posix() in {
        "db/analyzer.db",
        "db/analyzer.db-wal",
        "db/analyzer.db-shm",
        "db/analyzer.db-journal",
    }


def _excluded_subtrees(values: tuple[str, ...], asset: str) -> set[PurePosixPath]:
    result: set[PurePosixPath] = set()
    for value in values:
        path = PurePosixPath(value)
        if path.is_absolute() or not path.parts or any(
            part in {"", ".", ".."} for part in path.parts
        ):
            raise SnapshotError(f"{asset}排除路径非法: {value}")
        result.add(path)
    return result


def _sqlite_schema_hash(connection: sqlite3.Connection) -> str:
    rows = connection.execute(
        "SELECT type, name, tbl_name, COALESCE(sql, '') FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
    ).fetchall()
    encoded = json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _sqlite_migration_history(
    connection: sqlite3.Connection,
) -> list[dict[str, Any]] | None:
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone()
    if exists is None:
        return None
    try:
        rows = connection.execute(
            "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
        ).fetchall()
    except sqlite3.DatabaseError as exc:
        raise SnapshotError(f"SQLite schema_migrations 无法读取: {exc}") from exc
    return [
        {"version": int(row[0]), "name": str(row[1]), "checksum": str(row[2])}
        for row in rows
    ]


def _validate_sqlite_migration_history(
    *,
    user_version: int,
    actual_history: list[dict[str, Any]] | None,
    schema_support: dict[str, Any],
) -> list[dict[str, Any]]:
    expected = schema_support["migrations"][:user_version]
    ledger_version = schema_support["ledger_version"]
    if user_version >= ledger_version:
        if actual_history != expected:
            raise SnapshotError(
                "SQLite schema_migrations 与本地 migration manifest 历史前缀不一致"
            )
        return expected
    if actual_history is not None:
        raise SnapshotError(
            f"SQLite user_version={user_version} 不应含 schema_migrations 记录"
        )
    return expected


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
        migration_history = _sqlite_migration_history(dst)
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
        "migration_history": migration_history,
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
    deployment_id: str = "unbound",
    redis_mode: str = "offline-volume",
    data_excludes: tuple[str, ...] = (),
    minio_excludes: tuple[str, ...] = (),
    schema_manifest_path: Path | None = None,
    _schema_support_override: dict[str, Any] | None = None,
    _boundary_checked: bool = False,
) -> dict[str, Any]:
    """生成完整快照,只有全部资产和校验通过才原子发布归档."""
    if not _boundary_checked:
        _assert_evidence_outside_roots(
            (output, output.with_suffix(output.suffix + ".sha256")),
            tuple(path for path in (data_root, redis_root, minio_root, config_root) if path),
        )
    started_monotonic = time.monotonic()
    started_at = _utc_now()
    generation = _safe_generation(generation)
    if (
        not deployment_id or deployment_id == "unbound" or len(deployment_id) > 128
        or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-" for ch in deployment_id)
    ):
        raise SnapshotError(
            "deployment_id 必须是稳定非unbound标识且只允许 1..128 位 [A-Za-z0-9_.-]"
        )
    excluded_subtrees = _excluded_subtrees(data_excludes, "数据")
    excluded_minio_subtrees = _excluded_subtrees(minio_excludes, "MinIO")
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

    def minio_excluded(rel: Path) -> bool:
        pure = PurePosixPath(rel.as_posix())
        return any(
            pure == excluded or excluded in pure.parents
            for excluded in excluded_minio_subtrees
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
        schema_support = _schema_support_override or _schema_support(schema_manifest_path)
        db_version = int(sqlite_meta["user_version"])
        if not (
            schema_support["minimum_supported_version"]
            <= db_version
            <= schema_support["current_version"]
        ):
            raise SnapshotError(
                f"SQLite user_version={db_version} 不在生产者迁移链范围 "
                f"{schema_support['minimum_supported_version']}.."
                f"{schema_support['current_version']}"
            )
        migration_history = _validate_sqlite_migration_history(
            user_version=db_version,
            actual_history=sqlite_meta["migration_history"],
            schema_support=schema_support,
        )
        _validate_application_schema(
            data_destination / "db" / "analyzer.db", schema_support
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
            _copy_stable_tree(minio_root, minio_destination, minio_excluded)
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
            "deployment": {"id": deployment_id},
            "compatibility": {
                "min_restore_format": FORMAT_VERSION,
                "sqlite_user_version": sqlite_meta["user_version"],
                "database_schema": {
                    "version": db_version,
                    "minimum_supported_version": schema_support[
                        "minimum_supported_version"
                    ],
                    "maximum_supported_version": schema_support["current_version"],
                    "migration_history": migration_history,
                    "migration_history_sha256": _migration_history_fingerprint(
                        migration_history
                    ),
                },
            },
            "sqlite": sqlite_meta,
            "assets": assets,
            "files": files,
        }
        manifest["assets"]["data"]["excluded_external_subtrees"] = sorted(
            path.as_posix() for path in excluded_subtrees
        )
        manifest["assets"]["data"]["excluded_runtime_subtrees"] = [
            "workers/*/.cache",
        ]
        if minio_root is not None:
            manifest["assets"]["minio"]["excluded_external_subtrees"] = sorted(
                path.as_posix() for path in excluded_minio_subtrees
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


def _validate_sqlite(
    path: Path,
    expected: dict[str, Any],
    minimum_user_version: int,
    maximum_user_version: int,
) -> tuple[int, list[dict[str, Any]] | None]:
    try:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
        schema_hash = _sqlite_schema_hash(connection)
        migration_history = _sqlite_migration_history(connection)
    except sqlite3.DatabaseError as exc:
        raise SnapshotError(f"SQLite 快照无法打开: {exc}") from exc
    finally:
        with contextlib.suppress(UnboundLocalError):
            connection.close()
    if not integrity or integrity[0] != "ok":
        raise SnapshotError(f"SQLite integrity_check 失败: {integrity}")
    if type(expected.get("user_version")) is not int:
        raise SnapshotError("manifest.sqlite.user_version 必须是整数")
    if type(expected.get("application_id")) is not int:
        raise SnapshotError("manifest.sqlite.application_id 必须是整数")
    if user_version != expected["user_version"]:
        raise SnapshotError("SQLite user_version 与 manifest 不一致")
    if application_id != expected["application_id"]:
        raise SnapshotError("SQLite application_id 与 manifest 不一致")
    if schema_hash != expected.get("schema_sha256"):
        raise SnapshotError("SQLite schema 指纹与 manifest 不一致")
    if "migration_history" in expected and migration_history != expected["migration_history"]:
        raise SnapshotError("SQLite schema_migrations 与 manifest.sqlite 不一致")
    if not minimum_user_version <= user_version <= maximum_user_version:
        raise SnapshotError(
            f"SQLite user_version={user_version} 不在当前恢复程序范围 "
            f"{minimum_user_version}..{maximum_user_version}"
        )
    return user_version, migration_history


def validate_extracted(
    root: Path,
    max_db_user_version: int | None = None,
    *,
    min_db_user_version: int | None = None,
    schema_manifest_path: Path | None = None,
    _schema_support_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    manifest_path = root / MANIFEST_NAME
    if not manifest_path.is_file():
        raise SnapshotError("快照缺少 manifest.json")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise SnapshotError(f"manifest.json 无法读取: {exc}") from exc
    if manifest.get("format") != FORMAT_NAME:
        raise SnapshotError("快照 format 不匹配")
    format_version = manifest.get("format_version")
    if type(format_version) is not int or format_version not in SUPPORTED_FORMAT_VERSIONS:
        raise SnapshotError(
            f"不支持的快照 format_version={format_version}"
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
        if any(
            type(metadata.get(field)) is not int
            for field in ("size", "mode", "uid", "gid")
        ):
            raise SnapshotError(f"manifest 文件整数元数据非法: {rel}")
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
    if (
        type(sqlite_meta.get("user_version")) is not int
        or type(sqlite_meta.get("application_id")) is not int
    ):
        raise SnapshotError("manifest.sqlite 版本字段必须是整数")
    declared_sqlite_history = sqlite_meta.get("migration_history")
    if declared_sqlite_history is not None:
        if not isinstance(declared_sqlite_history, list):
            raise SnapshotError("manifest.sqlite.migration_history 必须是数组")
        for expected_version, migration in enumerate(
            declared_sqlite_history, start=1
        ):
            if (
                not isinstance(migration, dict)
                or type(migration.get("version")) is not int
                or migration["version"] != expected_version
            ):
                raise SnapshotError("manifest.sqlite.migration_history 版本非法")
    sqlite_rel = sqlite_meta.get("path")
    if sqlite_rel not in actual:
        raise SnapshotError("manifest.sqlite.path 不在快照成员中")
    if _schema_support_override is None:
        schema_range = _effective_schema_range(
            schema_manifest_path,
            minimum_override=min_db_user_version,
            maximum_override=max_db_user_version,
        )
    else:
        schema_range = {
            **_schema_support_override,
            "minimum": _schema_support_override["minimum_supported_version"],
            "maximum": _schema_support_override["current_version"],
        }
    sqlite_user_version, sqlite_migration_history = _validate_sqlite(
        actual[sqlite_rel],
        sqlite_meta,
        schema_range["minimum"],
        schema_range["maximum"],
    )
    compatibility = manifest.get("compatibility")
    if not isinstance(compatibility, dict):
        raise SnapshotError("manifest.compatibility 缺失")
    min_restore_format = compatibility.get("min_restore_format")
    if (
        type(min_restore_format) is not int
        or min_restore_format < 1
        or min_restore_format > FORMAT_VERSION
    ):
        raise SnapshotError("manifest 要求的恢复 format 不受支持")
    if (
        type(compatibility.get("sqlite_user_version")) is not int
        or compatibility["sqlite_user_version"] != sqlite_user_version
    ):
        raise SnapshotError(
            "manifest compatibility.sqlite_user_version 必须是整数且与 SQLite 一致"
        )
    if format_version >= 2:
        database_schema = compatibility.get("database_schema")
        if not isinstance(database_schema, dict):
            raise SnapshotError("manifest 缺少 database_schema 兼容元数据")
        producer_minimum = database_schema.get("minimum_supported_version")
        producer_maximum = database_schema.get("maximum_supported_version")
        history = database_schema.get("migration_history")
        fingerprint = database_schema.get("migration_history_sha256")
        if (
            type(database_schema.get("version")) is not int
            or database_schema["version"] != sqlite_user_version
            or type(producer_minimum) is not int
            or type(producer_maximum) is not int
            or not 0 <= producer_minimum <= sqlite_user_version <= producer_maximum
            or not isinstance(history, list)
            or len(history) != sqlite_user_version
            or not isinstance(fingerprint, str)
            or len(fingerprint) != 64
            or any(ch not in "0123456789abcdef" for ch in fingerprint)
        ):
            raise SnapshotError("manifest database_schema 兼容元数据非法")
        for expected, migration in enumerate(history, start=1):
            if (
                not isinstance(migration, dict)
                or type(migration.get("version")) is not int
                or migration["version"] != expected
                or not isinstance(migration.get("name"), str)
                or not migration["name"]
                or not isinstance(migration.get("checksum"), str)
                or len(migration["checksum"]) != 64
                or any(
                    ch not in "0123456789abcdef" for ch in migration["checksum"]
                )
            ):
                raise SnapshotError("manifest migration_history 条目非法")
        if _migration_history_fingerprint(history) != fingerprint:
            raise SnapshotError("manifest migration_history 指纹不一致")
        local_history = schema_range["migrations"][:sqlite_user_version]
        if history != local_history:
            raise SnapshotError("SQLite 迁移历史与本地 migration manifest 分叉")
        if sqlite_user_version >= schema_range["ledger_version"]:
            if sqlite_migration_history != history:
                raise SnapshotError(
                    "SQLite schema_migrations 与归档 migration_history 不一致"
                )
        elif sqlite_migration_history is not None:
            raise SnapshotError(
                "SQLite 低版本库含不应存在的 schema_migrations 记录"
            )
    # format v1 无历史声明，但仍在临时副本上跑同一生产 migration chain。
    _validate_application_schema(actual[sqlite_rel], schema_range)
    assets = manifest.get("assets")
    if not isinstance(assets, dict):
        raise SnapshotError("manifest.assets 缺失")
    for required in ("data", "redis"):
        if (
            not isinstance(assets.get(required), dict)
            or assets[required].get("included") is not True
        ):
            raise SnapshotError(f"快照缺少必需资产: {required}")
    for name, metadata in assets.items():
        if (
            not isinstance(metadata, dict)
            or type(metadata.get("included")) is not bool
        ):
            raise SnapshotError(f"manifest.assets.{name} 元数据非法")
        if metadata["included"] and (
            type(metadata.get("file_count")) is not int
            or metadata["file_count"] < 0
            or type(metadata.get("total_bytes")) is not int
            or metadata["total_bytes"] < 0
        ):
            raise SnapshotError(f"manifest.assets.{name} 计数字段非法")
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


def validate_archive(
    archive_path: Path,
    max_db_user_version: int | None = None,
    *,
    min_db_user_version: int | None = None,
    schema_manifest_path: Path | None = None,
    _schema_support_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    _validate_archive_sidecar(archive_path)
    with tempfile.TemporaryDirectory(prefix="flori-dr-validate-") as temporary:
        root = Path(temporary)
        _extract_archive(archive_path, root)
        return validate_extracted(
            root,
            max_db_user_version,
            min_db_user_version=min_db_user_version,
            schema_manifest_path=schema_manifest_path,
            _schema_support_override=_schema_support_override,
        )


def _marker_path(target: Path) -> Path:
    return target / TRANSACTION_FILE


_MARKER_FIELDS = frozenset(
    {
        "format",
        "generation",
        "asset",
        "base",
        "status",
        "old_names",
        "new_names",
        "preserve_names",
        "moved_old",
        "moved_new",
    }
)
_MARKER_NAME_FIELDS = (
    "old_names",
    "new_names",
    "preserve_names",
    "moved_old",
    "moved_new",
)
_MARKER_STATUSES = frozenset(
    {"prepared", "switching", "committed", "accepted", "finalizing"}
)
_RESTORE_ASSETS = frozenset({"data", "redis", "minio", "config"})


def _is_stage_entry(name: str) -> bool:
    return name.startswith(STAGE_PREFIX) and name != TRANSACTION_FILE


def _stage_base_name(generation: object) -> str:
    if type(generation) is not str:
        raise SnapshotError("恢复事务 generation 必须是字符串")
    safe_generation = _safe_generation(generation)
    base = f"{STAGE_PREFIX}{safe_generation}"
    if base == TRANSACTION_FILE:
        raise SnapshotError("恢复事务 generation 与事务标记名称冲突")
    return base


def _safe_marker_name(value: object, field: str) -> str:
    if (
        type(value) is not str
        or not value
        or value in {".", ".."}
        or "\x00" in value
        or "/" in value
        or "\\" in value
        or Path(value).is_absolute()
        or Path(value).name != value
        or value == TRANSACTION_FILE
        or value.startswith(STAGE_PREFIX)
    ):
        raise SnapshotError(f"恢复事务 {field} 含非法顶层名称")
    return value


def _direct_child(parent: Path, name: str, field: str) -> Path:
    candidate = parent / name
    try:
        parent_resolved = parent.resolve(strict=True)
        candidate_resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise SnapshotError(f"恢复事务 {field} 路径无法安全解析: {exc}") from exc
    if candidate_resolved.parent != parent_resolved:
        raise SnapshotError(f"恢复事务 {field} 路径逃出受控根")
    return candidate


def _validate_marker(
    target: Path,
    marker: object,
    *,
    expected_asset: str | None = None,
) -> dict[str, Any]:
    if type(marker) is not dict or set(marker) != _MARKER_FIELDS:
        raise SnapshotError("恢复事务标记字段集合非法")
    if type(marker.get("format")) is not str or marker["format"] != FORMAT_NAME:
        raise SnapshotError("恢复事务标记 format 非法")
    expected_base = _stage_base_name(marker.get("generation"))
    base_name = marker.get("base")
    if type(base_name) is not str or base_name != expected_base:
        raise SnapshotError("恢复事务标记 base 与 generation 不一致")
    asset = marker.get("asset")
    if type(asset) is not str or asset not in _RESTORE_ASSETS:
        raise SnapshotError("恢复事务标记 asset 非法")
    if expected_asset is not None and asset != expected_asset:
        raise SnapshotError(
            f"恢复事务标记 asset 与目标不一致: {asset} != {expected_asset}"
        )
    status = marker.get("status")
    if type(status) is not str or status not in _MARKER_STATUSES:
        raise SnapshotError("恢复事务标记 status 非法")

    name_lists: dict[str, list[str]] = {}
    for field in _MARKER_NAME_FIELDS:
        raw = marker.get(field)
        if type(raw) is not list:
            raise SnapshotError(f"恢复事务 {field} 必须是列表")
        values = [_safe_marker_name(value, field) for value in raw]
        if values != sorted(set(values)):
            raise SnapshotError(f"恢复事务 {field} 必须唯一且有序")
        name_lists[field] = values
    if not set(name_lists["moved_old"]).issubset(name_lists["old_names"]):
        raise SnapshotError("恢复事务 moved_old 不是 old_names 子集")
    if not set(name_lists["moved_new"]).issubset(name_lists["new_names"]):
        raise SnapshotError("恢复事务 moved_new 不是 new_names 子集")
    if name_lists["moved_old"] != name_lists["old_names"][: len(name_lists["moved_old"])]:
        raise SnapshotError("恢复事务 moved_old 不是 old_names 已移动前缀")
    if name_lists["moved_new"] != name_lists["new_names"][: len(name_lists["moved_new"])]:
        raise SnapshotError("恢复事务 moved_new 不是 new_names 已移动前缀")
    preserved = set(name_lists["preserve_names"])
    if preserved & (
        set(name_lists["old_names"]) | set(name_lists["new_names"])
    ):
        raise SnapshotError("恢复事务 preserve_names 与切换名称重叠")
    if status == "prepared" and any(
        name_lists[field]
        for field in ("old_names", "new_names", "moved_old", "moved_new")
    ):
        raise SnapshotError("prepared 恢复事务不得含切换进度")
    if status == "switching" and name_lists["moved_new"] and (
        name_lists["moved_old"] != name_lists["old_names"]
    ):
        raise SnapshotError("恢复事务开始移动新资产前必须移完旧资产")
    if status in {"committed", "accepted", "finalizing"} and (
        name_lists["moved_old"] != name_lists["old_names"]
        or name_lists["moved_new"] != name_lists["new_names"]
    ):
        raise SnapshotError(f"{status} 恢复事务切换进度不完整")

    if not target.is_dir():
        raise SnapshotError(f"恢复目标不存在或不是目录: {target}")
    base = _direct_child(target, expected_base, "base")
    if status == "finalizing":
        if base.is_symlink() or (base.exists() and not base.is_dir()):
            raise SnapshotError("恢复事务 finalizing base 路径非法")
        return marker
    if base.is_symlink() or not base.is_dir():
        raise SnapshotError("恢复事务 base 必须是目标内真实目录")
    old_root = _direct_child(base, "old", "old")
    new_root = _direct_child(base, "new", "new")
    for field, root in (("old", old_root), ("new", new_root)):
        if root.is_symlink() or not root.is_dir():
            raise SnapshotError(f"恢复事务 {field} 必须是 base 内真实目录")
    for field, values in name_lists.items():
        for value in values:
            _direct_child(target, value, field)
            _direct_child(old_root, value, field)
            _direct_child(new_root, value, field)
    return marker


def _load_marker(
    target: Path,
    *,
    expected_asset: str | None = None,
) -> dict[str, Any] | None:
    path = _marker_path(target)
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink() or not path.is_file():
        raise SnapshotError(f"恢复事务标记必须是普通文件: {path}")
    try:
        marker = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SnapshotError(f"恢复事务标记损坏，需人工检查 {path}: {exc}") from exc
    return _validate_marker(target, marker, expected_asset=expected_asset)


def _persist_marker(
    target: Path,
    marker: dict[str, Any],
    *,
    expected_asset: str | None = None,
) -> None:
    _validate_marker(target, marker, expected_asset=expected_asset)
    _write_json_atomic(_marker_path(target), marker)


def _rollback_target(
    target: Path,
    *,
    expected_asset: str | None = None,
    require_marker: bool = False,
) -> None:
    marker = _load_marker(target, expected_asset=expected_asset)
    if marker is None:
        if require_marker:
            raise SnapshotError(f"回滚目标缺失活跃 marker: {expected_asset}")
        return
    if marker.get("status") in {"accepted", "finalizing"}:
        raise SnapshotError("全局提交决策已落盘，禁止回滚")
    base = target / marker["base"]
    new_root = base / "new"
    old_root = base / "old"
    old_names = marker["old_names"]
    new_names = marker["new_names"]
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


def _recover_target(target: Path, *, expected_asset: str | None = None) -> None:
    target.mkdir(parents=True, exist_ok=True)
    if _marker_path(target).exists() or _marker_path(target).is_symlink():
        marker = _load_marker(target, expected_asset=expected_asset)
        if marker is not None and marker.get("status") in {"accepted", "finalizing"}:
            _finalize_target(target, expected_asset=expected_asset)
        else:
            _rollback_target(target, expected_asset=expected_asset)
    orphan_stages = sorted(
        child.name for child in target.iterdir() if _is_stage_entry(child.name)
    )
    if orphan_stages:
        raise SnapshotError(
            f"恢复目标含无 marker 孤立 stage，已保留现场: {orphan_stages}"
        )


def _recover_target_set(targets: dict[str, Path] | list[Path]) -> None:
    """任一目标已 accepted 说明全部 commit 已完成,否则统一回滚中断事务."""
    entries = (
        list(targets.items())
        if isinstance(targets, dict)
        else [(None, target) for target in targets]
    )
    expected_assets = {target: asset for asset, target in entries}
    markers = {
        target: _load_marker(target, expected_asset=expected_assets[target])
        for _asset, target in entries
        if target.exists()
    }
    active = {target: marker for target, marker in markers.items() if marker is not None}
    for _asset, target in entries:
        if not target.exists():
            continue
        marker = markers.get(target)
        allowed = {marker["base"]} if marker is not None else set()
        unexpected = sorted(
            child.name
            for child in target.iterdir()
            if _is_stage_entry(child.name) and child.name not in allowed
        )
        if unexpected:
            raise SnapshotError(
                f"恢复目标含无 marker 孤立 stage，已保留现场: {unexpected}"
            )
    generations = {marker["generation"] for marker in active.values()}
    if len(generations) > 1:
        raise SnapshotError(f"恢复目标含混合 generation: {sorted(generations)}")
    assets = [marker["asset"] for marker in active.values()]
    if len(assets) != len(set(assets)):
        raise SnapshotError(f"恢复目标含重复 asset: {sorted(assets)}")
    if any(
        marker.get("status") in {"accepted", "finalizing"}
        for marker in active.values()
    ):
        invalid = {
            str(target): marker.get("status")
            for target, marker in active.items()
            if marker.get("status") not in {"committed", "accepted", "finalizing"}
        }
        if invalid:
            raise SnapshotError(f"已接受的恢复事务与未提交目标混合: {invalid}")
        for target, marker in active.items():
            if marker.get("status") == "committed":
                marker["status"] = "accepted"
                _persist_marker(
                    target,
                    marker,
                    expected_asset=expected_assets[target],
                )
        for target in active:
            _finalize_target(
                target,
                expected_asset=expected_assets[target],
            )
    else:
        for target in reversed(list(active)):
            _rollback_target(
                target,
                expected_asset=expected_assets[target],
            )
    for _asset, target in entries:
        _recover_target(target, expected_asset=expected_assets[target])


def _prepare_target(
    source: Path,
    target: Path,
    generation: str,
    asset: str,
    preserve_names: set[str] | None = None,
) -> dict[str, Any]:
    target.mkdir(parents=True, exist_ok=True)
    base_name = _stage_base_name(generation)
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
    try:
        _persist_marker(target, marker, expected_asset=asset)
    except BaseException:
        _marker_path(target).unlink(missing_ok=True)
        _remove(base)
        _fsync_dir(target)
        raise
    return marker


def _commit_target(
    target: Path,
    *,
    expected_asset: str | None = None,
    rollback_on_error: bool = True,
) -> None:
    marker = _load_marker(target, expected_asset=expected_asset)
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
        _persist_marker(target, marker, expected_asset=expected_asset)
        for name in current_names:
            os.replace(target / name, old_root / name)
            marker["moved_old"].append(name)
            _persist_marker(target, marker, expected_asset=expected_asset)
        for child in sorted(new_root.iterdir(), key=lambda item: item.name):
            name = child.name
            os.replace(child, target / name)
            marker["moved_new"].append(name)
            _persist_marker(target, marker, expected_asset=expected_asset)
        marker["status"] = "committed"
        _persist_marker(target, marker, expected_asset=expected_asset)
        _fsync_dir(target)
    except BaseException:
        if rollback_on_error:
            _rollback_target(target, expected_asset=expected_asset)
        raise


def _finalize_target(target: Path, *, expected_asset: str | None = None) -> None:
    marker = _load_marker(target, expected_asset=expected_asset)
    if marker is None or marker.get("status") not in {"accepted", "finalizing"}:
        raise SnapshotError(f"目标未处于 accepted 状态: {target}")
    if marker.get("status") == "accepted":
        marker["status"] = "finalizing"
        _persist_marker(target, marker, expected_asset=expected_asset)
    base = target / marker["base"]
    _remove(base)
    _fsync_dir(target)
    _marker_path(target).unlink(missing_ok=True)
    _fsync_dir(target)


def _accept_target(target: Path, *, expected_asset: str | None = None) -> None:
    marker = _load_marker(target, expected_asset=expected_asset)
    if marker is None or marker.get("status") != "committed":
        raise SnapshotError(f"目标未处于 committed 状态: {target}")
    marker["status"] = "accepted"
    _persist_marker(target, marker, expected_asset=expected_asset)


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


def _roll_forward_target_set(
    targets: dict[str, Path],
) -> list[tuple[str, BaseException]]:
    """全局提交决策后提升并收尾所有资产，不再进入回滚分支。"""
    markers = {
        name: _load_marker(target, expected_asset=name)
        for name, target in targets.items()
        if target.exists()
    }
    active = {name: marker for name, marker in markers.items() if marker is not None}
    missing = [name for name in targets if name not in active]
    if missing:
        raise SnapshotError(
            f"全局提交决策缺失活跃 marker: {missing}"
        )
    generations = {marker["generation"] for marker in active.values()}
    if len(generations) > 1:
        raise SnapshotError(
            f"全局提交决策含混合 generation: {sorted(generations)}"
        )
    invalid = {
        name: marker.get("status")
        for name, marker in active.items()
        if marker.get("status") not in {"committed", "accepted", "finalizing"}
    }
    if invalid:
        raise SnapshotError(f"全局提交决策含未提交目标: {invalid}")
    cleanup_errors: list[tuple[str, BaseException]] = []
    if active and not any(
        marker.get("status") in {"accepted", "finalizing"}
        for marker in active.values()
    ):
        first = next(name for name in targets if name in active)
        try:
            _accept_target(targets[first], expected_asset=first)
            active[first]["status"] = "accepted"
        except BaseException as error:
            cleanup_errors.append((f"accept:{first}", error))
    for name in targets:
        if active[name].get("status") != "committed":
            continue
        try:
            _accept_target(targets[name], expected_asset=name)
            active[name]["status"] = "accepted"
        except BaseException as error:
            cleanup_errors.append((f"accept:{name}", error))
    if cleanup_errors:
        return cleanup_errors
    for name in targets:
        try:
            _finalize_target(targets[name], expected_asset=name)
        except BaseException as error:
            cleanup_errors.append((f"finalize:{name}", error))
    return cleanup_errors


def _raise_restore_failure(
    primary: BaseException,
    cleanup_errors: list[tuple[str, BaseException]],
    summary: str,
) -> None:
    """清理尽力完成后保留控制流异常，普通错误则聚合为可恢复诊断。"""
    if not cleanup_errors:
        raise primary
    details = ", ".join(
        f"{name}({type(error).__name__})" for name, error in cleanup_errors
    )
    note = f"{summary}: {details}"
    primary.add_note(note)
    if not isinstance(primary, Exception):
        raise primary
    control_error = next(
        (error for _name, error in cleanup_errors if not isinstance(error, Exception)),
        None,
    )
    if control_error is not None:
        control_error.add_note(
            f"原恢复异常: {type(primary).__name__}; {note}"
        )
        raise control_error from primary
    raise SnapshotError(f"{summary}: {details}") from primary


def restore_snapshot(
    *,
    archive_path: Path,
    targets: dict[str, Path],
    max_db_user_version: int | None = None,
    min_db_user_version: int | None = None,
    schema_manifest_path: Path | None = None,
    fail_after_commits: int | None = None,
    _schema_support_override: dict[str, Any] | None = None,
    _boundary_checked: bool = False,
    expected_deployment_id: str | None = None,
    allow_cross_deployment: bool = False,
    cross_deployment_confirmation: str | None = None,
) -> dict[str, Any]:
    """校验后两阶段切换所有目标;accept 开始后异常只统一前滚。"""
    if not _boundary_checked:
        _assert_evidence_outside_roots(
            (archive_path, archive_path.with_suffix(archive_path.suffix + ".sha256")),
            tuple(targets.values()),
        )
    started_at = _utc_now()
    started_monotonic = time.monotonic()
    _validate_target_roots(targets)
    current_deployment = expected_deployment_id or os.environ.get("FLORI_DEPLOYMENT_ID")
    if (
        not current_deployment or current_deployment == "unbound"
        or len(current_deployment) > 128
        or any(
            ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-"
            for ch in current_deployment
        )
    ):
        raise SnapshotError("恢复必须提供稳定非unbound expected deployment ID")
    _validate_archive_sidecar(archive_path)
    with tempfile.TemporaryDirectory(prefix="flori-dr-restore-") as temporary:
        extracted = Path(temporary)
        _extract_archive(archive_path, extracted)
        manifest = validate_extracted(
            extracted,
            max_db_user_version,
            min_db_user_version=min_db_user_version,
            schema_manifest_path=schema_manifest_path,
            _schema_support_override=_schema_support_override,
        )
        archive_deployment = (manifest.get("deployment") or {}).get("id")
        deployment_matches = archive_deployment == current_deployment
        if not deployment_matches and not (
            allow_cross_deployment
            and cross_deployment_confirmation == CROSS_DEPLOYMENT_CONFIRMATION
        ):
            raise SnapshotError(
                "归档deployment ID与当前部署不一致;跨机克隆必须同时显式启用"
                " allow_cross_deployment 并提供高风险确认: "
                f"archive={archive_deployment or 'missing'}, current={current_deployment}"
            )
        for target in targets.values():
            target.mkdir(parents=True, exist_ok=True)
        preserves = _target_preserves(targets)
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
        _recover_target_set({name: targets[name] for name in selected})
        prepared: list[str] = []
        committed: list[str] = []
        accepted = False
        accept_phase_started = False
        commit_recovered_after_error = False
        recovered_error_type: str | None = None
        recovery_already_finalized = False
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
                _commit_target(
                    targets[name],
                    expected_asset=name,
                    rollback_on_error=False,
                )
                committed.append(name)
                if fail_after_commits is not None and len(committed) >= fail_after_commits:
                    raise SnapshotError("测试故障注入: 目标切换后中断")
            for name in selected:
                accept_phase_started = True
                _accept_target(targets[name], expected_asset=name)
            accepted = True
        except BaseException as restore_error:
            selected_targets = {name: targets[name] for name in selected}
            roll_forward_required = accept_phase_started
            decision_errors: list[tuple[str, BaseException]] = []
            if not roll_forward_required:
                current_markers: dict[str, dict[str, Any]] = {}
                for name in prepared:
                    try:
                        if not targets[name].is_dir():
                            raise SnapshotError(f"恢复目标缺失或不是目录: {name}")
                        marker = _load_marker(targets[name], expected_asset=name)
                        if marker is None:
                            raise SnapshotError(f"恢复目标缺失活跃 marker: {name}")
                        current_markers[name] = marker
                    except BaseException as decision_error:
                        decision_errors.append((f"commit-decision:{name}", decision_error))
                if decision_errors:
                    _raise_restore_failure(
                        restore_error,
                        decision_errors,
                        "恢复决策无法安全判定，现场已保留",
                    )
                roll_forward_required = any(
                    marker.get("status") == "accepted"
                    for marker in current_markers.values()
                )
            if roll_forward_required:
                try:
                    recovery_errors = _roll_forward_target_set(selected_targets)
                except BaseException as recovery_error:
                    decision_errors.append(("roll-forward", recovery_error))
                else:
                    decision_errors.extend(recovery_errors)
                if decision_errors:
                    _raise_restore_failure(
                        restore_error,
                        decision_errors,
                        "恢复全局提交待继续，marker 已保留",
                    )
                if not isinstance(restore_error, Exception):
                    restore_error.add_note(
                        "恢复全局提交已统一前滚完成"
                    )
                    raise restore_error
                accepted = True
                commit_recovered_after_error = True
                recovered_error_type = type(restore_error).__name__
                recovery_already_finalized = True
            else:
                rollback_errors: list[tuple[str, BaseException]] = []
                for name in reversed(prepared):
                    try:
                        _rollback_target(
                            targets[name],
                            expected_asset=name,
                            require_marker=True,
                        )
                    except BaseException as cleanup_error:
                        rollback_errors.append((name, cleanup_error))
                _raise_restore_failure(
                    restore_error,
                    rollback_errors,
                    "恢复失败且回滚未完成，marker 已保留",
                )
        cleanup_pending: list[str] = []
        if accepted and not recovery_already_finalized:
            for name in selected:
                try:
                    _finalize_target(targets[name], expected_asset=name)
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
        "commit_recovered_after_error": commit_recovered_after_error,
        "error_type": recovered_error_type,
        "preserved_target_entries": {
            name: sorted(values) for name, values in preserves.items() if values
        },
        "deployment": {
            "archive_id": archive_deployment,
            "current_id": current_deployment,
            "matched": deployment_matches,
            "cross_deployment_override": not deployment_matches,
        },
        "checks": {
            "archive_members": "ok",
            "checksums": "ok",
            "sqlite_integrity": "ok",
            "compatibility": "ok",
            "atomic_switch": "ok",
        },
    }


def run_empty_environment_drill(
    result_file: Path | None = None,
    schema_manifest_path: Path | None = None,
) -> dict[str, Any]:
    """在隔离临时根中演练完整恢复、损坏拒绝和跨目标回滚."""
    started = time.monotonic()
    checks: dict[str, str] = {}
    schema_support = _schema_support(
        schema_manifest_path, allow_synthetic_zero=True
    )
    with tempfile.TemporaryDirectory(prefix="flori-dr-drill-") as temporary:
        root = Path(temporary)
        source = {name: root / "source" / name for name in ("data", "redis", "minio", "config")}
        target = {name: root / "target" / name for name in source}
        for path in [*source.values(), *target.values()]:
            path.mkdir(parents=True)
        database = source["data"] / "db" / "analyzer.db"
        database.parent.mkdir(parents=True)
        connection = sqlite3.connect(database)
        connection.row_factory = sqlite3.Row
        if schema_support["current_version"]:
            connection.execute("PRAGMA foreign_keys=ON")
            _run_frozen_migration_chain(connection, schema_support)
            connection.execute(
                "INSERT INTO jobs "
                "(id, content_type, pipeline, document_kind, title, domain, created_at, updated_at) "
                "VALUES ('jobs_drill', 'document', 'document', 'unknown', '灾备演练', "
                "'general', '2026-01-01T00:00:00+00:00', "
                "'2026-01-01T00:00:00+00:00')"
            )
        else:
            connection.execute(
                "CREATE TABLE jobs(id TEXT PRIMARY KEY, title TEXT NOT NULL)"
            )
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
            deployment_id="isolated-drill",
            schema_manifest_path=schema_manifest_path,
            _schema_support_override=schema_support,
        )
        checks["backup_atomic_publish"] = "ok"

        sentinel = target["data"] / "sentinel.txt"
        sentinel.write_text("current", encoding="utf-8")
        corrupt = root / "backups" / "corrupt.tar.gz"
        raw = archive.read_bytes()
        corrupt.write_bytes(raw[: max(1, len(raw) // 2)])
        try:
            restore_snapshot(
                archive_path=corrupt,
                targets=target,
                schema_manifest_path=schema_manifest_path,
                _schema_support_override=schema_support,
                expected_deployment_id="isolated-drill",
            )
        except SnapshotError:
            pass
        else:
            raise SnapshotError("损坏快照未被拒绝")
        if sentinel.read_text(encoding="utf-8") != "current":
            raise SnapshotError("损坏快照校验失败后修改了现态")
        checks["corrupt_snapshot_fail_closed"] = "ok"

        restore_result = restore_snapshot(
            archive_path=archive,
            targets=target,
            schema_manifest_path=schema_manifest_path,
            _schema_support_override=schema_support,
            expected_deployment_id="isolated-drill",
        )
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
            restore_snapshot(
                archive_path=archive,
                targets=target,
                schema_manifest_path=schema_manifest_path,
                fail_after_commits=1,
                _schema_support_override=schema_support,
                expected_deployment_id="isolated-drill",
            )
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


def _directory_identity_index(roots: tuple[Path, ...]) -> frozenset[tuple[int, int]]:
    """一次遍历目标目录实体，供同一命令的全部 evidence 复用。"""
    identities: set[tuple[int, int]] = set()
    for root in sorted(set(roots), key=lambda item: len(item.parts)):
        if not root.is_dir():
            continue
        try:
            root_info = root.stat(follow_symlinks=False)
            root_identity = (root_info.st_dev, root_info.st_ino)
            if root_identity in identities:
                continue
            for current, directories, _files in os.walk(root, followlinks=False):
                base = Path(current)
                info = base.stat(follow_symlinks=False)
                identities.add((info.st_dev, info.st_ino))
                directories[:] = [
                    name for name in directories
                    if not (base / name).is_symlink()
                ]
        except OSError as exc:
            raise SnapshotError(f"无法建立恢复边界目录身份索引: {root}: {exc}") from exc
    return frozenset(identities)


def _evidence_aliases_tree(
    evidence: Path,
    root: Path,
    root_directory_identities: frozenset[tuple[int, int]],
) -> bool:
    evidence_abs = Path(os.path.abspath(evidence))
    root_abs = Path(os.path.abspath(root))
    if evidence_abs == root_abs or root_abs in evidence_abs.parents:
        return True
    if evidence_abs in root_abs.parents:
        return True
    parent = evidence_abs.parent
    if parent.exists():
        info = parent.stat(follow_symlinks=False)
        if (info.st_dev, info.st_ino) in root_directory_identities:
            return True
    return False


def _assert_evidence_outside_roots(
    evidence_paths: tuple[Path, ...], roots: tuple[Path, ...],
) -> None:
    root_directory_identities = _directory_identity_index(roots)
    for evidence in evidence_paths:
        for root in roots:
            if _evidence_aliases_tree(evidence, root, root_directory_identities):
                raise SnapshotError(
                    f"灾备证据路径不得与数据源或恢复目标重叠: {evidence} <> {root}"
                )


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
    create.add_argument("--deployment-id", required=True)
    create.add_argument(
        "--redis-mode",
        choices=("rdb", "offline-volume", "materialized-rdb-aof"),
        default="offline-volume",
    )
    create.add_argument("--data-exclude", action="append", default=[])
    create.add_argument("--minio-exclude", action="append", default=[])
    create.add_argument("--result-file")
    create.add_argument("--owner-uid", type=int)
    create.add_argument("--owner-gid", type=int)
    create.add_argument("--schema-manifest")

    validate = subparsers.add_parser("validate", help="只读校验快照")
    validate.add_argument("--archive", required=True)
    validate.add_argument("--max-db-user-version", type=int)
    validate.add_argument("--result-file")
    validate.add_argument("--owner-uid", type=int)
    validate.add_argument("--owner-gid", type=int)
    validate.add_argument("--schema-manifest")

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
    restore.add_argument("--schema-manifest")
    restore.add_argument("--expected-deployment-id", required=True)
    restore.add_argument("--allow-cross-deployment", action="store_true")
    restore.add_argument("--cross-deployment-confirmation")

    drill = subparsers.add_parser("drill", help="运行隔离空环境恢复演练")
    drill.add_argument("--result-file")
    drill.add_argument("--owner-uid", type=int)
    drill.add_argument("--owner-gid", type=int)
    drill.add_argument("--schema-manifest")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    result_file = _path_or_none(getattr(args, "result_file", None))
    requested_result_file = result_file
    try:
        if args.command == "create":
            sources = tuple(path for path in (
                Path(args.data), Path(args.redis), _path_or_none(args.minio),
                _path_or_none(args.config),
            ) if path is not None)
            try:
                _assert_evidence_outside_roots(
                    tuple(path for path in (
                        Path(args.output),
                        Path(args.output).with_suffix(Path(args.output).suffix + ".sha256"),
                        result_file,
                    ) if path is not None),
                    sources,
                )
            except SnapshotError:
                result_file = None
                raise
            result = create_snapshot(
                data_root=Path(args.data),
                redis_root=Path(args.redis),
                minio_root=_path_or_none(args.minio),
                config_root=_path_or_none(args.config),
                output=Path(args.output),
                generation=args.generation,
                app_version=args.app_version,
                deployment_id=args.deployment_id,
                redis_mode=args.redis_mode,
                data_excludes=tuple(args.data_exclude),
                minio_excludes=tuple(args.minio_exclude),
                schema_manifest_path=_path_or_none(args.schema_manifest),
                _boundary_checked=True,
            )
        elif args.command == "validate":
            if result_file is not None and (
                result_file == Path(args.archive)
                or result_file == Path(args.archive).with_suffix(Path(args.archive).suffix + ".sha256")
            ):
                result_file = None
                raise SnapshotError("校验result不得覆盖archive或sha256 sidecar")
            manifest = validate_archive(
                Path(args.archive),
                args.max_db_user_version,
                schema_manifest_path=_path_or_none(args.schema_manifest),
            )
            result = {
                "status": "success",
                "operation": "validate",
                "format": manifest["format"],
                "format_version": manifest["format_version"],
                "generation": manifest["generation"],
                "created_at": manifest["created_at"],
                "deployment_id": (
                    manifest.get("deployment") or {}
                ).get("id"),
                "assets": manifest["assets"],
                "checks": {"members": "ok", "checksums": "ok", "sqlite": "ok", "compatibility": "ok"},
            }
        elif args.command == "restore":
            targets = {"data": Path(args.data_target), "redis": Path(args.redis_target)}
            if args.minio_target:
                targets["minio"] = Path(args.minio_target)
            if args.config_target:
                targets["config"] = Path(args.config_target)
            try:
                _assert_evidence_outside_roots(
                    tuple(path for path in (
                        Path(args.archive),
                        Path(args.archive).with_suffix(Path(args.archive).suffix + ".sha256"),
                        result_file,
                    ) if path is not None),
                    tuple(targets.values()),
                )
            except SnapshotError:
                result_file = None
                raise
            result = restore_snapshot(
                archive_path=Path(args.archive),
                targets=targets,
                max_db_user_version=args.max_db_user_version,
                schema_manifest_path=_path_or_none(args.schema_manifest),
                _boundary_checked=True,
                expected_deployment_id=args.expected_deployment_id,
                allow_cross_deployment=args.allow_cross_deployment,
                cross_deployment_confirmation=args.cross_deployment_confirmation,
            )
        else:
            result = run_empty_environment_drill(
                result_file, _path_or_none(args.schema_manifest)
            )
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
