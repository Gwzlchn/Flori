"""设置页的便携备份状态、在线创建与离线恢复交接。"""

from __future__ import annotations

import asyncio
import os
import secrets
import sys
from contextlib import suppress

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status

from api.deps import get_config, verify_token
from api.exact_dr import (
    CONFIRMATION as EXACT_DR_CONFIRMATION,
    ExactDrError,
    new_operation as new_exact_dr_operation,
    run_exact_dr,
    status_payload as exact_dr_status_payload,
    validate_start_configuration as validate_exact_dr_start,
    write_operation as write_exact_dr_operation,
)
from api.recovery import (
    RecoveryControlError,
    build_restore_handoff,
    media_vendoring_available,
    new_backup_operation,
    read_operation,
    repository_status,
    utc_now,
    validate_repository_physical_boundary,
    write_operation,
)
from api.wire_schemas import (
    API_ERROR_RESPONSES,
    ExactDrStartRequest,
    ExactDrStartedResponse,
    RecoveryBackupRequest,
    RecoveryBackupStartedResponse,
    RecoveryRestorePlanRequest,
    RecoveryRestorePlanResponse,
    RecoveryStatusResponse,
)
from shared.content_repository import ContentRepository, RepositoryError


logger = structlog.get_logger(component="recovery-api")
router = APIRouter(
    prefix="/api/recovery",
    tags=["recovery"],
    dependencies=[Depends(verify_token)],
    responses=API_ERROR_RESPONSES,
)


def _active_tasks(request: Request) -> dict[str, asyncio.Task]:
    tasks = getattr(request.app.state, "recovery_tasks", None)
    if tasks is None:
        tasks = request.app.state.recovery_tasks = {}
    return tasks


def _backup_start_lock(request: Request) -> asyncio.Lock:
    lock = getattr(request.app.state, "recovery_start_lock", None)
    if lock is None:
        lock = request.app.state.recovery_start_lock = asyncio.Lock()
    return lock


async def _run_backup_worker(app, operation_id: str) -> None:
    process = None
    try:
        worker_env = os.environ.copy()
        worker_env["DATA_DIR"] = str(app.state.config.data_dir)
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "api.recovery_worker",
            operation_id,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env=worker_env,
        )
        returncode = await process.wait()
        if returncode == 0:
            return
        operation = read_operation(app.state.config.data_dir, operation_id)
        if operation.get("status") in {"queued", "running"}:
            operation.update(
                status="failed",
                finished_at=utc_now(),
                error=f"后台备份进程退出({returncode})",
            )
            write_operation(app.state.config.data_dir, operation)
    except asyncio.CancelledError:
        if process is not None and process.returncode is None:
            process.terminate()
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(process.wait(), timeout=5)
            if process.returncode is None:
                process.kill()
                await process.wait()
        with suppress(RecoveryControlError):
            operation = read_operation(app.state.config.data_dir, operation_id)
            if operation.get("status") in {"queued", "running"}:
                operation.update(
                    status="interrupted",
                    finished_at=utc_now(),
                    error="API关闭,后台备份已中断;请检查仓库写锁",
                )
                write_operation(app.state.config.data_dir, operation)
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "recovery_backup_worker_failed",
            operation_id=operation_id,
            error_type=type(exc).__name__,
        )
        with suppress(RecoveryControlError):
            operation = read_operation(app.state.config.data_dir, operation_id)
            if operation.get("status") in {"queued", "running"}:
                operation.update(
                    status="failed",
                    finished_at=utc_now(),
                    error=f"后台编排异常: {type(exc).__name__}",
                )
                write_operation(app.state.config.data_dir, operation)


@router.get("", response_model=RecoveryStatusResponse)
async def get_recovery_status(request: Request, config=Depends(get_config)):
    """读取便携仓库、有效快照、视频闭包与后台备份状态。"""
    try:
        tasks = _active_tasks(request)
        active = {operation_id for operation_id, task in tasks.items() if not task.done()}
        result = await asyncio.to_thread(
            repository_status,
            data_dir=config.data_dir,
            active_operation_ids=active,
        )
        exact_task = getattr(request.app.state, "exact_dr_task", None)
        result["exact_dr"] = await asyncio.to_thread(
            exact_dr_status_payload,
            config.data_dir,
            active=bool(exact_task and not exact_task.done()),
        )
        return result
    except (RecoveryControlError, ExactDrError) as exc:
        raise HTTPException(503, str(exc)) from exc


@router.post(
    "/exact-dr",
    response_model=ExactDrStartedResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_exact_dr(
    body: ExactDrStartRequest,
    request: Request,
    config=Depends(get_config),
):
    """拒绝新写入并排空 Worker 后创建、校验完整 exact DR 三件套。"""
    if not secrets.compare_digest(
        body.confirmation.encode("utf-8"),
        EXACT_DR_CONFIRMATION.encode("utf-8"),
    ):
        raise HTTPException(409, f"风险确认不匹配,请输入:{EXACT_DR_CONFIRMATION}")
    async with _backup_start_lock(request):
        tasks = _active_tasks(request)
        if any(not task.done() for task in tasks.values()):
            raise HTTPException(409, "便携备份正在运行,不能同时创建 exact DR")
        exact_task = getattr(request.app.state, "exact_dr_task", None)
        if exact_task is not None and not exact_task.done():
            raise HTTPException(409, "已有 exact DR 操作正在运行")
        operation = None
        try:
            await asyncio.to_thread(validate_exact_dr_start)
            operation = await asyncio.to_thread(
                new_exact_dr_operation,
                config.data_dir,
                persist=False,
            )
            await asyncio.to_thread(
                write_exact_dr_operation,
                config.data_dir,
                operation,
            )
            await request.app.state.exact_dr_gate.begin_draining(
                config.data_dir,
                operation_id=operation["id"],
                created_at=operation["created_at"],
            )
        except BaseException as exc:
            if operation is not None:
                cleanup_task = asyncio.create_task(
                    request.app.state.exact_dr_gate.finish(
                        config.data_dir,
                        operation_id=operation["id"],
                    )
                )
                with suppress(BaseException):
                    await asyncio.shield(cleanup_task)
                operation.update(
                    status=(
                        "interrupted"
                        if isinstance(exc, asyncio.CancelledError)
                        else "failed"
                    ),
                    finished_at=utc_now(),
                    error=(
                        "exact DR 启动被取消;未创建灾备归档"
                        if isinstance(exc, asyncio.CancelledError)
                        else f"exact DR 启动失败:{str(exc)[:800]}"
                    ),
                )
                with suppress(Exception):
                    await asyncio.to_thread(
                        write_exact_dr_operation,
                        config.data_dir,
                        operation,
                    )
            if isinstance(exc, asyncio.CancelledError):
                raise
            if isinstance(exc, (OSError, ExactDrError)):
                raise HTTPException(503, str(exc)) from exc
            raise
        task = asyncio.create_task(run_exact_dr(request.app, operation["id"]))
        request.app.state.exact_dr_task = task

        def _clear_exact_dr_task(done_task: asyncio.Task) -> None:
            if getattr(request.app.state, "exact_dr_task", None) is done_task:
                request.app.state.exact_dr_task = None

        task.add_done_callback(_clear_exact_dr_task)
        return {"operation": operation}


@router.post(
    "/backups",
    response_model=RecoveryBackupStartedResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_recovery_backup(
    body: RecoveryBackupRequest,
    request: Request,
    config=Depends(get_config),
):
    """启动隔离子进程创建增量便携备份;失败不会推进 latest。"""
    async with _backup_start_lock(request):
        exact_task = getattr(request.app.state, "exact_dr_task", None)
        if (
            request.app.state.exact_dr_gate.phase is not None
            or exact_task is not None and not exact_task.done()
        ):
            raise HTTPException(409, "exact DR 正在运行,不能同时创建便携备份")
        tasks = _active_tasks(request)
        if any(not task.done() for task in tasks.values()):
            raise HTTPException(409, "已有备份操作正在运行")
        if body.vendor_media and not media_vendoring_available():
            raise HTTPException(409, "NAS原视频目录未配置或当前不可读")
        try:
            path = await asyncio.to_thread(
                validate_repository_physical_boundary,
                config.data_dir,
            )
            if (path / "repository.json").is_file():
                holder = ContentRepository.open(path).write_lock_holder()
                if holder is not None:
                    raise HTTPException(
                        409,
                        f"便携仓库写锁仍由 {holder.get('owner') or 'unknown'} 持有;"
                        "确认原进程已退出后用运维脚本显式处理",
                    )
            operation = await asyncio.to_thread(
                new_backup_operation,
                data_dir=config.data_dir,
                vendor_media=body.vendor_media,
                full_rehash=body.full_rehash,
            )
        except HTTPException:
            raise
        except (OSError, RepositoryError, RecoveryControlError) as exc:
            raise HTTPException(503, f"无法启动备份: {type(exc).__name__}") from exc
        task = asyncio.create_task(_run_backup_worker(request.app, operation["id"]))
        tasks[operation["id"]] = task
        task.add_done_callback(lambda _task, key=operation["id"]: tasks.pop(key, None))
        return {"operation": operation}


@router.post(
    "/restore-plans",
    response_model=RecoveryRestorePlanResponse,
)
async def prepare_restore_plan(
    body: RecoveryRestorePlanRequest,
    config=Depends(get_config),
):
    """全链检查快照并生成离线恢复交接;本端点不写线上DB或产物。"""
    try:
        handoff, reused = await asyncio.to_thread(
            build_restore_handoff,
            data_dir=config.data_dir,
            config=config,
            snapshot_digest=body.snapshot_digest,
        )
    except (RecoveryControlError, RepositoryError, OSError) as exc:
        raise HTTPException(409, str(exc)) from exc
    return {**handoff, "reused": reused}
