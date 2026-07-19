"""API 后台便携备份子进程;避免大文件扫描阻塞请求事件循环。"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
from pathlib import Path

from api.recovery import read_operation, utc_now, validate_repository_boundary, write_operation
from shared.content_backup import BackupError, run_backup
from shared.content_policy import PolicyError
from shared.content_repository import ContentRepository, RepositoryError
from shared.content_result import ResultFileError, ensure_output_roots_disjoint
from shared.source_library import SourceReferenceError, source_roots_from_env
from shared.storage import create_storage
from shared.version import FLORI_VERSION


class WorkerInterrupted(RuntimeError):
    """父API关闭时让Python栈正常展开,释放仓库写锁。"""


def _interrupt(_signum, _frame) -> None:
    raise WorkerInterrupted("后台备份被API关闭信号中断")


def _open_or_create(path: Path) -> ContentRepository:
    if (path / "repository.json").is_file():
        return ContentRepository.open(path)
    return ContentRepository.create(path)


def _safe_error(exc: BaseException) -> str:
    if isinstance(exc, WorkerInterrupted):
        return str(exc)
    if isinstance(exc, (BackupError, RepositoryError, PolicyError, SourceReferenceError)):
        return str(exc)[:1000]
    return f"{type(exc).__name__}: 后台备份异常"


def _prepare_work_dir(data_dir: Path, repository: Path) -> Path:
    work = Path(os.environ.get("WORK_DIR", "/tmp/flori-work"))
    if not work.is_absolute():
        raise BackupError("WORK_DIR must be an absolute path")
    absolute = Path(os.path.abspath(work))
    resolved = work.resolve(strict=False)
    data_roots = [data_dir.resolve(strict=False)]
    try:
        data_roots.extend(
            path.resolve(strict=False) for path in source_roots_from_env().values()
        )
    except SourceReferenceError as exc:
        raise BackupError(f"source roots are invalid: {exc}") from exc
    protected = [*data_roots, repository.resolve(strict=False)]
    if absolute != resolved:
        raise BackupError("WORK_DIR or its ancestors must not contain symlinks")
    for root in protected:
        if resolved == root or resolved in root.parents or root in resolved.parents:
            raise BackupError("WORK_DIR must be isolated from data, repository, and source roots")
    try:
        ensure_output_roots_disjoint((repository, resolved), tuple(data_roots))
        ensure_output_roots_disjoint((resolved,), (repository,))
    except ResultFileError as exc:
        raise BackupError(f"backup output physical boundary is unsafe: {exc}") from exc
    work.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        # mkdir后再绑定实体,避免不存在的子目录在创建时改向。
        ensure_output_roots_disjoint((repository, resolved), tuple(data_roots))
        ensure_output_roots_disjoint((resolved,), (repository,))
    except ResultFileError as exc:
        raise BackupError(f"backup output physical boundary changed: {exc}") from exc
    os.chmod(work, 0o700)
    return work


async def run(operation_id: str) -> int:
    data_dir = Path(os.environ.get("DATA_DIR", "/data"))
    operation = read_operation(data_dir, operation_id)
    operation.update(status="running", started_at=utc_now(), error=None)
    write_operation(data_dir, operation)
    try:
        data_root = Path(os.environ.get("DATA_DIR", "/data"))
        repository_root = validate_repository_boundary(data_dir)
        work_dir = _prepare_work_dir(data_dir, repository_root)
        repository = _open_or_create(repository_root)
        result = await run_backup(
            db_path=data_root / "db" / "analyzer.db",
            storage=create_storage(data_root / "jobs"),
            repository=repository,
            run_id=operation_id,
            app_version=FLORI_VERSION,
            source_instance=os.environ.get("FLORI_DEPLOYMENT_ID") or None,
            ref="latest",
            full_rehash=bool(operation["full_rehash"]),
            user_config_dir=data_root / "prompts",
            vendor_media=bool(operation["vendor_media"]),
            work_dir=work_dir,
        )
    except BaseException as exc:
        operation.update(
            status="interrupted" if isinstance(exc, WorkerInterrupted) else "failed",
            finished_at=utc_now(),
            error=_safe_error(exc),
        )
        write_operation(data_dir, operation)
        return 1
    operation.update(
        status="success",
        finished_at=utc_now(),
        snapshot_digest=result.snapshot_digest,
        receipt_id=result.receipt_id,
        stats=result.stats,
        error=None,
    )
    write_operation(data_dir, operation)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="flori-recovery-worker")
    parser.add_argument("operation_id")
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, _interrupt)
    return asyncio.run(run(args.operation_id))


if __name__ == "__main__":
    sys.exit(main())
