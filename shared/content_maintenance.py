"""按真实数据库与产物 namespace 协调在线服务和便携导入。"""

from __future__ import annotations

import fcntl
import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


LOCK_DIR_ENV = "FLORI_MAINTENANCE_LOCK_DIR"
DEFAULT_LOCK_DIR = "/data/maintenance-locks"


class MaintenanceLockError(RuntimeError):
    """目标 namespace 仍被服务使用或锁目录不可信时拒绝。"""


def _canonical_path(path: str | Path, *, label: str) -> Path:
    absolute = Path(os.path.abspath(os.fspath(path)))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise MaintenanceLockError(f"cannot inspect {label} {current}: {exc}") from exc
        if stat.S_ISLNK(info.st_mode):
            raise MaintenanceLockError(f"{label} contains a symbolic link: {current}")
    resolved = absolute.resolve(strict=False)
    if resolved != absolute:
        raise MaintenanceLockError(
            f"{label} changes after canonical resolution: {absolute} -> {resolved}"
        )
    return resolved


def path_resources(kind: str, path: str | Path) -> tuple[str, ...]:
    canonical = _canonical_path(path, label=f"{kind} path")
    # kind 只用于诊断，不能参与物理互斥身份。否则同一目录被分别称作
    # artifact/config/source 时会拿到三把互不相干的锁，在线写面可被角色改名绕过。
    resources = [
        f"{kind}:path:{canonical}",
        f"physical:path:{canonical}",
    ]
    try:
        info = canonical.stat(follow_symlinks=False)
    except FileNotFoundError:
        return tuple(resources)
    except OSError as exc:
        raise MaintenanceLockError(f"cannot stat {kind} path {canonical}: {exc}") from exc
    resources.extend([
        f"{kind}:inode:{info.st_dev}:{info.st_ino}",
        f"physical:inode:{info.st_dev}:{info.st_ino}",
    ])
    return tuple(resources)


def object_resource(bucket: str) -> str:
    if not bucket or any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789.-" for ch in bucket):
        raise MaintenanceLockError(f"invalid object-store bucket namespace: {bucket!r}")
    return f"object-store:bucket:{bucket}"


def service_resources(
    *, db_path: str | Path, jobs_dir: str | Path, object_bucket: str | None = None,
    config_root: str | Path | None = None,
    source_roots: Iterable[str | Path] = (),
) -> tuple[str, ...]:
    resources = list(path_resources("database", db_path))
    if os.environ.get("MINIO_URL"):
        resources.append(object_resource(object_bucket or os.environ.get("MINIO_BUCKET") or "flori"))
    else:
        resources.extend(path_resources("artifact-root", jobs_dir))
    if config_root is not None:
        resources.extend(path_resources("config-root", config_root))
    for source_root in source_roots:
        resources.extend(path_resources("source-root", source_root))
    return tuple(sorted(set(resources)))


def live_import_resources(
    *,
    targets: Iterable[str],
    live_db_path: str | Path,
    live_jobs_dir: str | Path,
    production_bucket: str,
    live_config_root: str | Path | None = None,
) -> tuple[str, ...]:
    resources: list[str] = []
    target_set = set(targets)
    if "database" in target_set:
        resources.extend(path_resources("database", live_db_path))
    if "artifact-root" in target_set:
        # 子目录导入也锁住服务实际持有的线上根,不能只锁子目录 inode。
        resources.extend(path_resources("artifact-root", live_jobs_dir))
    if "object-store" in target_set:
        resources.append(object_resource(production_bucket))
    if "config-root" in target_set:
        if live_config_root is None:
            raise MaintenanceLockError("live config target lacks its canonical root")
        resources.extend(path_resources("config-root", live_config_root))
    return tuple(sorted(set(resources)))


def import_target_resources(*, db_path: str | Path, storage) -> tuple[str, ...]:
    """任意导入都互斥同一 DB 或产物 namespace,不只保护 live 目标。"""
    resources = list(path_resources("database", db_path))
    bucket = getattr(storage, "bucket", None)
    jobs_dir = getattr(storage, "jobs_dir", None)
    if isinstance(bucket, str) and bucket:
        resources.append(object_resource(bucket))
    elif jobs_dir is not None:
        resources.extend(path_resources("artifact-root", jobs_dir))
    else:
        raise MaintenanceLockError(
            "import storage exposes neither bucket nor jobs_dir; refusing an uncoordinated write"
        )
    return tuple(sorted(set(resources)))


@dataclass
class MaintenanceLease:
    """持有一组 flock;close 可重复调用。"""

    resources: tuple[str, ...]
    _descriptors: list[int]
    _directory_fd: int | None
    exclusive: bool

    def close(self) -> None:
        for descriptor in reversed(self._descriptors):
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)
        self._descriptors.clear()
        if self._directory_fd is not None:
            os.close(self._directory_fd)
            self._directory_fd = None

    def __enter__(self) -> MaintenanceLease:
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


def acquire_maintenance_lease(
    resources: Iterable[str], *, exclusive: bool, owner: str,
) -> MaintenanceLease:
    identities = tuple(sorted(set(resources)))
    if not identities:
        return MaintenanceLease((), [], None, exclusive)
    lock_root = _canonical_path(
        os.environ.get(LOCK_DIR_ENV) or DEFAULT_LOCK_DIR,
        label="maintenance lock directory",
    )
    lock_root.mkdir(parents=True, exist_ok=True)
    lock_root = _canonical_path(lock_root, label="maintenance lock directory")
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    directory_fd = os.open(lock_root, directory_flags)
    descriptors: list[int] = []
    operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    try:
        for resource in identities:
            name = hashlib.sha256(resource.encode("utf-8")).hexdigest() + ".lock"
            flags = (
                os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0)
            )
            descriptor = os.open(name, flags, 0o600, dir_fd=directory_fd)
            try:
                fcntl.flock(descriptor, operation | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                os.close(descriptor)
                mode = "exclusive import" if exclusive else "service"
                raise MaintenanceLockError(
                    f"{mode} maintenance lease blocked for {resource};"
                    f"stop the process holding this namespace before {owner} continues"
                ) from exc
            descriptors.append(descriptor)
        return MaintenanceLease(identities, descriptors, directory_fd, exclusive)
    except BaseException:
        for descriptor in reversed(descriptors):
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)
        os.close(directory_fd)
        raise


def acquire_service_lease(
    *, db_path: str | Path, jobs_dir: str | Path, owner: str,
    object_bucket: str | None = None,
    config_root: str | Path | None = None,
    source_roots: Iterable[str | Path] = (),
) -> MaintenanceLease:
    return acquire_maintenance_lease(
        service_resources(
            db_path=db_path, jobs_dir=jobs_dir, object_bucket=object_bucket,
            config_root=config_root, source_roots=source_roots,
        ),
        exclusive=False,
        owner=owner,
    )


def acquire_live_import_lease(authorization: dict) -> MaintenanceLease:
    resources = authorization.get("maintenance_resources")
    if not isinstance(resources, list) or not all(isinstance(item, str) for item in resources):
        raise MaintenanceLockError("live import authorization lacks maintenance resources")
    return acquire_maintenance_lease(resources, exclusive=True, owner="content-import")


def acquire_import_target_lease(
    *, db_path: str | Path, storage, extra_resources: Iterable[str] = (),
) -> MaintenanceLease:
    resources = set(import_target_resources(db_path=db_path, storage=storage))
    resources.update(extra_resources)
    return acquire_maintenance_lease(
        resources,
        exclusive=True,
        owner="content-import",
    )
