"""系统状态 + 健康检查 + 配置管理。"""

from __future__ import annotations

import asyncio
import os
import shutil
import sqlite3
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse

logger = structlog.get_logger(component="admin")

from shared.config import AppConfig
from shared.db import Database
from shared.redis_client import RedisClient
from shared.status import (
    DEFAULT_ONLINE_WINDOW_SEC,
    DEFAULT_STALE_WINDOW_SEC,
    HEALTH_DEGRADED,
    HEALTH_ERROR,
    HEALTH_OK,
    compute_component_status,
    summarize_readiness,
)
from shared.storage import RemoteStorage
from shared.sysload import read_process_rss_mb
from shared.version import FLORI_VERSION
from api.deps import get_config, get_db, get_redis, get_storage, verify_token
from api.routes.workers import merged_worker_responses
from api.wire_schemas import (
    API_ERROR_RESPONSES,
    FullStatusResponse,
    HealthLiveResponse,
    LinkTrafficHistoryResponse,
    PipelinesResponse,
    PoolLimitsResponse,
    PricingStatusResponse,
    ReadinessResponse,
    StatusUpdatedResponse,
    SystemEventsResponse,
    UsageAggregateResponse,
)

router = APIRouter(prefix="/api", tags=["admin"], responses=API_ERROR_RESPONSES)


def _health_item(
    status: str,
    *,
    required: bool,
    detail: str | None = None,
    recovery: str | None = None,
    **data,
) -> dict:
    return {
        "status": status,
        "required": required,
        "detail": detail,
        "recovery": recovery,
        **data,
    }


def _readiness_config(config: AppConfig) -> dict:
    cfg = (config.pools or {}).get("readiness") or {}
    pools = (config.pools or {}).get("pools") or {}
    optional_cfg = list(cfg.get("optional_pools") or ["gpu"])
    required = list(
        cfg.get("required_pools")
        or [name for name in pools if name not in optional_cfg]
    )
    optional = [name for name in optional_cfg if name not in required]

    def _threshold(name: str, default: float) -> float:
        try:
            value = float(cfg.get(name, default))
        except (TypeError, ValueError):
            return default
        return value if value > 0 else default

    return {
        "disk_min_free_gb": _threshold("disk_min_free_gb", 5),
        "disk_min_free_pct": _threshold("disk_min_free_pct", 5),
        "probe_ttl_sec": _threshold("probe_ttl_sec", 5),
        "probe_timeout_sec": _threshold("probe_timeout_sec", 3),
        "required_pools": required,
        "optional_pools": optional,
    }


async def _singleflight_probe(
    app,
    key: str,
    *,
    ttl_sec: float,
    timeout_sec: float,
    probe,
) -> dict:
    """短 TTL 单飞探针.过期不回陈旧绿灯,失败也短暂缓存为红灯."""
    cache = getattr(app.state, "readiness_probe_cache", None)
    if cache is None:
        cache = app.state.readiness_probe_cache = {}
    inflight = getattr(app.state, "readiness_probe_inflight", None)
    if inflight is None:
        inflight = app.state.readiness_probe_inflight = {}

    now = time.monotonic()
    cached = cache.get(key)
    if cached and cached[0] > now:
        return cached[1]

    task = inflight.get(key)
    if task is None or task.done():
        async def _run() -> dict:
            try:
                value = await asyncio.wait_for(probe(), timeout=timeout_sec)
                result = {"ok": True, "value": value}
            except asyncio.TimeoutError:
                result = {"ok": False, "error_type": "TimeoutError"}
            except Exception as e:  # noqa: BLE001
                result = {"ok": False, "error_type": type(e).__name__}
            cache[key] = (time.monotonic() + ttl_sec, result)
            return result

        task = asyncio.create_task(_run())
        inflight[key] = task
    try:
        return await asyncio.shield(task)
    finally:
        if task.done() and inflight.get(key) is task:
            inflight.pop(key, None)


def _probe_data_path(path: Path) -> None:
    """真实创建并 fsync 一个临时文件,验证挂载不是只读或假可写."""
    fd, name = tempfile.mkstemp(prefix=".flori-readiness-", dir=path)
    try:
        os.write(fd, b"ok")
        os.fsync(fd)
    finally:
        os.close(fd)
        Path(name).unlink(missing_ok=True)


def _probe_sqlite_write(path: Path) -> dict:
    """在独立连接执行真实 WAL 写事务并回滚,不留下表或业务数据."""
    table = f"__flori_readiness_{uuid.uuid4().hex}"
    connection = sqlite3.connect(
        f"file:{path}?mode=rw",
        uri=True,
        timeout=1,
        isolation_level=None,
    )
    try:
        connection.execute("PRAGMA busy_timeout = 1000")
        journal_mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        if journal_mode != "wal":
            raise RuntimeError(f"unexpected journal mode: {journal_mode}")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(f'CREATE TABLE "{table}" (value INTEGER NOT NULL)')
        connection.execute(f'INSERT INTO "{table}" VALUES (1)')
        connection.execute("ROLLBACK")
        leftover = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,),
        ).fetchone()
        if leftover is not None:
            raise RuntimeError("readiness probe table was not rolled back")
        return {"journal_mode": journal_mode}
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()


async def _probe_components(app, online_window: int, stale_window: int) -> list[dict]:
    redis = app.state.redis
    storage = getattr(app.state, "storage", None)
    readiness_cfg = _readiness_config(app.state.config)

    async def _safe(coro, name, kind):
        try:
            return await coro
        except Exception as e:  # noqa: BLE001
            logger.warning("component_probe_failed", component=name, error_type=type(e).__name__)
            return {
                "name": name, "kind": kind, "status": "unknown", "version": None,
                "last_heartbeat": None, "uptime_sec": None,
                "detail": f"探测异常: {type(e).__name__}", "extra": {},
            }

    return list(await asyncio.gather(
        _safe(_probe_api(app, online_window), "api", "api"),
        _safe(_probe_scheduler(redis, online_window, stale_window), "scheduler", "scheduler"),
        _safe(_probe_redis(redis), "redis", "redis"),
        _safe(
            _probe_minio(
                app,
                storage,
                readiness_cfg["probe_ttl_sec"],
                readiness_cfg["probe_timeout_sec"],
            ),
            "minio",
            "minio",
        ),
    ))


async def build_readiness(app, components: list[dict] | None = None) -> dict:
    """检查能否安全接收新任务,返回统一 readiness 模型.

    探针只暴露状态、容量和恢复建议,不返回连接串、路径外的宿主信息或凭证.
    """
    db = app.state.db
    config: AppConfig = app.state.config
    cfg = _readiness_config(config)
    online_window, stale_window = _windows(config)
    if components is None:
        components = await _probe_components(app, online_window, stale_window)
    by_kind = {item["kind"]: item for item in components}
    checks: dict[str, dict] = {}

    redis_comp = by_kind.get("redis") or {}
    redis_status = redis_comp.get("status")
    checks["redis"] = _health_item(
        (
            HEALTH_OK if redis_status == "up"
            else HEALTH_DEGRADED if redis_status == "degraded"
            else HEALTH_ERROR
        ),
        required=True,
        detail=redis_comp.get("detail"),
        recovery="检查 Redis 容器、网络和 REDIS_URL 后重试",
    )

    async def _db_write_probe():
        return await asyncio.to_thread(_probe_sqlite_write, Path(config.db_path))

    db_probe = await _singleflight_probe(
        app,
        "sqlite-write",
        ttl_sec=cfg["probe_ttl_sec"],
        timeout_sec=cfg["probe_timeout_sec"],
        probe=_db_write_probe,
    )
    if db_probe["ok"]:
        checks["db"] = _health_item(
            HEALTH_OK,
            required=True,
            journal_mode=db_probe["value"]["journal_mode"],
        )
    else:
        logger.warning("health_db_error", error_type=db_probe["error_type"])
        checks["db"] = _health_item(
            HEALTH_ERROR,
            required=True,
            detail=f"数据库写事务不可用: {db_probe['error_type']}",
            recovery="检查 SQLite 文件、WAL、锁和挂载权限",
        )

    data_path = Path(config.data_dir)
    try:
        disk = await asyncio.to_thread(shutil.disk_usage, data_path)
        free_gb = disk.free / (1024**3)
        free_pct = disk.free / disk.total * 100 if disk.total else 0.0
        below = free_gb < cfg["disk_min_free_gb"] or free_pct < cfg["disk_min_free_pct"]
        checks["disk"] = _health_item(
            HEALTH_ERROR if below else HEALTH_OK,
            required=True,
            detail=(
                f"数据盘空间不足: {free_gb:.1f}GB/{free_pct:.1f}% 可用"
                if below else None
            ),
            recovery="释放日志或产物空间,再确认数据盘挂载正常",
            free_gb=round(free_gb, 1),
            free_pct=round(free_pct, 1),
            min_free_gb=cfg["disk_min_free_gb"],
            min_free_pct=cfg["disk_min_free_pct"],
        )
    except (FileNotFoundError, OSError) as e:
        checks["disk"] = _health_item(
            HEALTH_ERROR, required=True, detail=f"数据盘不可用: {type(e).__name__}",
            recovery="恢复 DATA_DIR 挂载并确认目录存在",
            free_gb=-1,
            free_pct=-1,
            min_free_gb=cfg["disk_min_free_gb"],
            min_free_pct=cfg["disk_min_free_pct"],
        )

    try:
        await asyncio.to_thread(_probe_data_path, data_path)
        checks["data_writable"] = _health_item(HEALTH_OK, required=True)
    except (FileNotFoundError, PermissionError, OSError) as e:
        checks["data_writable"] = _health_item(
            HEALTH_ERROR, required=True, detail=f"数据盘不可写: {type(e).__name__}",
            recovery="把 DATA_DIR 恢复为可写挂载并检查宿主权限",
        )

    sched = by_kind.get("scheduler") or {}
    scheduler_status = sched.get("status")
    checks["scheduler"] = _health_item(
        (
            HEALTH_OK if scheduler_status == "up"
            else HEALTH_DEGRADED if scheduler_status == "degraded"
            else HEALTH_ERROR
        ),
        required=True,
        detail=sched.get("detail") or (
            None if scheduler_status == "up" else f"调度器状态为 {scheduler_status or 'unknown'}"
        ),
        recovery="启动或重启 scheduler,并确认心跳在在线窗口内",
    )

    minio = by_kind.get("minio") or {}
    storage_mode = (minio.get("extra") or {}).get("mode")
    minio_status = minio.get("status")
    storage_status = (
        HEALTH_OK if storage_mode == "local" or minio_status == "up"
        else HEALTH_DEGRADED if minio_status == "degraded"
        else HEALTH_ERROR
    )
    checks["storage"] = _health_item(
        storage_status,
        required=True,
        detail=None if storage_status == HEALTH_OK else (minio.get("detail") or "对象存储不可用"),
        recovery="检查对象存储服务、bucket 和中心存储配置",
        mode=storage_mode or "unknown",
    )

    workers_error: str | None = None
    try:
        workers = await asyncio.wait_for(
            merged_worker_responses(db, app.state.redis, config, include_traffic=False),
            timeout=cfg["probe_timeout_sec"],
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("health_workers_error", error_type=type(e).__name__)
        workers = []
        workers_error = type(e).__name__
    online_by_pool: dict[str, int] = {}
    paused_by_pool: dict[str, int] = {}
    for worker in workers:
        pools = set(worker.pools or ([worker.type] if worker.type else []))
        if str(worker.status).startswith("online"):
            for pool in pools:
                online_by_pool[pool] = online_by_pool.get(pool, 0) + 1
        elif worker.status == "paused":
            for pool in pools:
                paused_by_pool[pool] = paused_by_pool.get(pool, 0) + 1
    total_online = sum(1 for worker in workers if str(worker.status).startswith("online"))
    total_paused = sum(1 for worker in workers if worker.status == "paused")
    checks["workers"] = _health_item(
        HEALTH_ERROR if workers_error else HEALTH_OK,
        required=True,
        detail=(f"Worker 状态合并失败: {workers_error}" if workers_error else None),
        recovery="检查 SQLite、Redis 和 Worker 心跳后重试",
        total=len(workers),
        online=total_online,
        paused=total_paused,
    )
    for pool in cfg["required_pools"]:
        count = online_by_pool.get(pool, 0)
        paused = paused_by_pool.get(pool, 0)
        if workers_error:
            detail = f"必要资源池 {pool} 无法取得 Worker 状态"
        elif paused:
            detail = f"必要资源池 {pool} 的 Worker 全部暂停"
        else:
            detail = f"必要资源池 {pool} 没有在线 Worker"
        checks[f"pool:{pool}"] = _health_item(
            HEALTH_OK if count else HEALTH_ERROR,
            required=True,
            detail=None if count else detail,
            recovery=f"启动至少一个声明 --pools {pool} 的 Worker",
            online=count,
            paused=paused,
        )
    for pool in cfg["optional_pools"]:
        count = online_by_pool.get(pool, 0)
        paused = paused_by_pool.get(pool, 0)
        detail = (
            f"可选资源池 {pool} 无法取得 Worker 状态" if workers_error
            else f"可选资源池 {pool} 的 Worker 全部暂停" if paused
            else f"可选资源池 {pool} 当前离线"
        )
        checks[f"pool:{pool}"] = _health_item(
            HEALTH_OK if count else HEALTH_DEGRADED,
            required=False,
            detail=None if count else detail,
            recovery=f"需要该能力时启动声明 --pools {pool} 的 Worker",
            online=count,
            paused=paused,
        )

    return {"version": FLORI_VERSION, **summarize_readiness(checks)}


@router.get("/health/live", response_model=HealthLiveResponse)
async def health_live():
    """进程存活探针.依赖故障不改变 liveness,避免编排器重启健康 API 进程."""
    return {"status": "alive", "alive": True, "version": FLORI_VERSION}


@router.get(
    "/health/ready",
    response_model=ReadinessResponse,
    responses={503: {"model": ReadinessResponse, "description": "存在必需阻断项"}},
)
async def health_ready(request: Request):
    """安全接单探针.阻断项存在时返回 503,供反代和发布门使用."""
    state = await build_readiness(request.app)
    return state if state["ready"] else JSONResponse(status_code=503, content=state)


@router.get("/health", response_model=ReadinessResponse)
async def health(request: Request):
    """兼容健康端点.返回统一 readiness 模型,但保持 HTTP 200 供旧监控读取."""
    return await build_readiness(request.app)


@router.get("/metrics", response_class=PlainTextResponse)
async def metrics(request: Request, db: Database = Depends(get_db)):
    """Prometheus 文本指标(免鉴权,同 /health;只暴露计数/容量,无敏感信息)。
    个人工具不内置时序库,此端点供外部 Prometheus 抓取。"""
    readiness = await build_readiness(request.app)
    checks = readiness["checks"]
    redis_up = int(checks["redis"]["status"] == HEALTH_OK)
    db_up = int(checks["db"]["status"] == HEALTH_OK)
    disk_free = checks["disk"].get("free_gb", -1)
    try:
        workers = await merged_worker_responses(
            db, request.app.state.redis, request.app.state.config, include_traffic=False,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("metrics_workers_error", error_type=type(e).__name__)
        workers = []
    online = sum(1 for w in workers if w.status.startswith("online"))
    lines = [
        "# TYPE flori_up gauge", "flori_up 1",
        "# TYPE flori_ready gauge", f"flori_ready {int(readiness['ready'])}",
        "# TYPE flori_degraded gauge", f"flori_degraded {int(readiness['degraded'])}",
        "# TYPE flori_redis_up gauge", f"flori_redis_up {redis_up}",
        "# TYPE flori_db_up gauge", f"flori_db_up {db_up}",
        "# TYPE flori_disk_free_gb gauge", f"flori_disk_free_gb {disk_free}",
        "# TYPE flori_workers_total gauge", f"flori_workers_total {len(workers)}",
        "# TYPE flori_workers_online gauge", f"flori_workers_online {online}",
    ]
    try:
        by_status = await asyncio.to_thread(db.count_jobs_by_status)
        lines.append("# TYPE flori_jobs gauge")
        for st, n in sorted(by_status.items()):
            lines.append(f'flori_jobs{{status="{st}"}} {n}')
    except Exception:
        pass
    return "\n".join(lines) + "\n"


def _windows(config) -> tuple[int, int]:
    """组件/worker 判活窗口(单一事实源 pools.yaml::worker_status,缺省回退内置默认)。"""
    cfg = (config.pools.get("worker_status") or {}) if config else {}
    return (
        int(cfg.get("online_window_sec", DEFAULT_ONLINE_WINDOW_SEC)),
        int(cfg.get("stale_window_sec", DEFAULT_STALE_WINDOW_SEC)),
    )


async def build_live_status(db, redis, config) -> dict:
    """实时片段(workers/pools/jobs/disk):便宜、无组件探测。供 WS /api/ws/global 每 2s 推 +
    被 build_full_status 复用。disk 补 total_gb/used_pct(zero-cost,disk_usage 本就返回 total)。"""
    workers = await merged_worker_responses(db, redis, config, include_traffic=False)
    worker_summary = {}
    for w in workers:
        for pool in set(w.pools or ([w.type] if w.type else [])):
            if pool not in worker_summary:
                worker_summary[pool] = {"online": 0, "busy": 0, "paused": 0}
            if w.status.startswith("online"):
                worker_summary[pool]["online"] += 1
            if w.status == "online-busy":
                worker_summary[pool]["busy"] += 1
            if w.status == "paused":
                worker_summary[pool]["paused"] += 1

    pools_cfg = config.pools.get("pools", {})
    overrides = await redis.get_all_pool_limit_overrides()
    pools_info = {}
    for pool_name, pcfg in pools_cfg.items():
        count = await redis.get_pool_count(pool_name)
        queue = await redis.get_queue_info(pool_name)
        cap = overrides.get(pool_name)
        if cap is None:
            cap = pcfg.get("limit", 1024)
        pools_info[pool_name] = {
            "capacity": cap,  # 运行时覆盖优先,否则 pools.yaml 默认
            "used": count,
            "queue": queue["length"],
        }

    total, _ = await asyncio.to_thread(db.list_jobs, limit=0)
    stats = {}
    for s in ("done", "processing", "failed", "pending"):
        cnt, _ = await asyncio.to_thread(db.list_jobs, status=s, limit=0)
        stats[s] = cnt

    try:
        disk = shutil.disk_usage(str(config.data_dir))
        total_gb = round(disk.total / (1024**3), 1)
        used_gb = round(disk.used / (1024**3), 1)
        disk_info = {
            "used_gb": used_gb,
            "available_gb": round(disk.free / (1024**3), 1),
            "total_gb": total_gb,
            "used_pct": round(disk.used / disk.total * 100, 1) if disk.total else 0.0,
        }
    except (FileNotFoundError, OSError):
        disk_info = {"used_gb": -1, "available_gb": -1, "total_gb": -1, "used_pct": -1}

    return {
        "workers": worker_summary,
        "pools": pools_info,
        "jobs": {"total": total, **stats},
        "disk": disk_info,
    }


# 兼容别名:保留公开名 build_system_status 指向 live 子集;仓库内已无引用,留着防外部消费方断链。
build_system_status = build_live_status


async def _probe_api(app, online_window: int) -> dict:
    """API 组件:能返回响应即 up(恒 up;down 仅前端在 /api/status 请求失败时兜底)。
    uptime 据 app.state.started_at;extra 带进程 RSS。"""
    started_at = getattr(app.state, "started_at", None)
    now = datetime.now(timezone.utc)
    last_hb = now.isoformat()
    uptime = None
    if isinstance(started_at, datetime):
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        uptime = round((now - started_at).total_seconds())
    rss = read_process_rss_mb()
    extra: dict = {}
    if rss is not None:
        extra["rss_mb"] = rss
    return {
        "name": "api", "kind": "api", "status": "up", "version": FLORI_VERSION,
        "last_heartbeat": last_hb, "uptime_sec": uptime, "detail": None, "extra": extra,
    }


async def _probe_scheduler(redis, online_window: int, stale_window: int) -> dict:
    """Scheduler 组件:据 component:scheduler 心跳新鲜度算 up/degraded/down/unknown;
    loop_lag>5s 叠加 degraded。键从不存在=unknown(老版本/从未启动)。"""
    comp = {
        "name": "scheduler", "kind": "scheduler", "status": "unknown", "version": None,
        "last_heartbeat": None, "uptime_sec": None, "detail": None, "extra": {},
    }
    try:
        hb = await asyncio.wait_for(redis.get_component_heartbeat("scheduler"), timeout=2)
    except (asyncio.TimeoutError, Exception) as e:  # noqa: BLE001
        comp["detail"] = f"读心跳失败: {type(e).__name__}"
        return comp
    if not hb:
        comp["detail"] = "调度器从未上报心跳(未启动/老版本)"
        return comp
    ts = _parse_iso(hb.get("ts"))
    now = datetime.now(timezone.utc)
    status = compute_component_status(ts, now, online_window, stale_window)
    loop_lag = _to_float(hb.get("loop_lag_sec"))
    if status == "up" and loop_lag is not None and loop_lag > 5:
        status = "degraded"
        comp["detail"] = f"调度循环被拖慢 loop_lag={loop_lag}s"
    started = _parse_iso(hb.get("started_at"))
    uptime = round((now - started).total_seconds()) if started else None
    comp.update({
        "status": status,
        "version": hb.get("version") or None,
        "last_heartbeat": ts.isoformat() if ts else None,
        "uptime_sec": uptime,
        "extra": {
            "loop_lag_sec": loop_lag if loop_lag is not None else 0.0,
            "loop_interval_sec": _to_int(hb.get("loop_interval_sec"), 30),
            "pid": _to_int(hb.get("pid"), None),
        },
    })
    if status == "down" and not comp["detail"]:
        comp["detail"] = "调度器心跳已过期(进程可能已停止)"
    return comp


async def _probe_redis(redis) -> dict:
    """Redis 组件:ping 计时 + INFO. 超时(2s)为 down;其他异常为 unknown.
    ping_ms>200 或内存临界时为 degraded。"""
    comp = {
        "name": "redis", "kind": "redis", "status": "unknown", "version": None,
        "last_heartbeat": None, "uptime_sec": None, "detail": None, "extra": {},
    }
    try:
        info = await asyncio.wait_for(redis.server_info(), timeout=2)
    except asyncio.TimeoutError:
        comp.update(status="down", detail="redis 探活超时(2s)")
        return comp
    except Exception as e:  # noqa: BLE001
        comp.update(status="unknown", detail=f"redis 探活失败: {type(e).__name__}")
        return comp
    now = datetime.now(timezone.utc)
    ping_ms = info.get("ping_ms")
    used = info.get("used_memory_mb") or 0
    maxmem = info.get("maxmemory_mb") or 0
    status = "up"
    detail = None
    if ping_ms is not None and ping_ms > 200:
        status, detail = "degraded", f"ping 慢 {ping_ms}ms"
    if maxmem and used / maxmem > 0.9:
        status, detail = "degraded", f"内存临界 {used}/{maxmem}MB"
    comp.update({
        "status": status,
        "version": info.get("version"),
        "last_heartbeat": now.isoformat(),
        "uptime_sec": info.get("uptime_sec"),
        "detail": detail,
        "extra": {
            "used_memory_human": info.get("used_memory_human"),
            "used_memory_mb": used,
            "maxmemory_mb": maxmem,
            "connected_clients": info.get("connected_clients"),
            "ping_ms": ping_ms,
        },
    })
    return comp


async def _probe_minio(app, storage, ttl_sec: float, timeout_sec: float) -> dict:
    """MinIO 组件:远端用短 TTL 单飞 put/delete canary,本地盘不标红."""
    comp = {
        "name": "minio", "kind": "minio", "status": "unknown", "version": None,
        "last_heartbeat": None, "uptime_sec": None, "detail": None, "extra": {},
    }
    now = datetime.now(timezone.utc)
    if not isinstance(storage, RemoteStorage):
        h = await storage.health() if storage is not None else {"mode": "local", "detail": "本地盘"}
        comp.update(detail=h.get("detail"), extra={"mode": h.get("mode", "local")})
        return comp
    result = await _singleflight_probe(
        app,
        "minio-write-delete",
        ttl_sec=ttl_sec,
        timeout_sec=timeout_sec,
        probe=lambda: storage.readiness_probe(timeout_sec=timeout_sec),
    )
    if not result["ok"]:
        error_type = result["error_type"]
        detail = (
            f"对象存储写删探活超时({timeout_sec:g}s)"
            if error_type == "TimeoutError"
            else f"对象存储写删探活失败: {error_type}"
        )
        comp.update(status="down", detail=detail, extra={"mode": "remote"})
        return comp
    h = result["value"]
    comp.update({
        "status": h.get("status", "unknown"),
        "version": h.get("version"),
        "last_heartbeat": now.isoformat(),
        "detail": h.get("detail"),
        "extra": {
            "bucket": h.get("bucket"), "bucket_exists": h.get("bucket_exists"),
            "probe_ms": h.get("probe_ms"), "mode": h.get("mode", "remote"),
        },
    })
    return comp


async def build_full_status(app) -> dict:
    """全量(给 HTTP /api/status):live 子集 + version + 有序 components[api,scheduler,redis,minio]
    + throughput_1h。逐组件独立 try+超时:单项异常只影响该组件,绝不让整体 500。"""
    db = app.state.db
    redis = app.state.redis
    config = app.state.config
    online_window, stale_window = _windows(config)

    try:
        live = await build_live_status(db, redis, config)
    except Exception as e:  # noqa: BLE001
        # 状态页必须在依赖故障时仍能返回统一健康原因,不能让实时统计拖成 500.
        logger.warning("live_status_failed", error_type=type(e).__name__)
        live = {
            "workers": {},
            "pools": {},
            "jobs": {"total": -1, "done": -1, "processing": -1, "failed": -1, "pending": -1},
            "disk": {"used_gb": -1, "available_gb": -1, "total_gb": -1, "used_pct": -1},
        }
    components = await _probe_components(app, online_window, stale_window)
    readiness = await build_readiness(app, components)

    # MinIO 容量(对象数/总字节):读后台缓存,绝不在此同步扫. 有缓存才填,无则不填.
    cap = getattr(getattr(app.state, "minio_cap", None), "value", None)
    if cap:
        for c in components:
            if c.get("kind") == "minio":
                c.setdefault("extra", {})
                c["extra"]["objects"] = cap.get("objects")
                c["extra"]["size_bytes"] = cap.get("bytes")
                break

    # 近 1h 吞吐(便宜:GROUP BY done/failed,利用 idx_jobs_status)。失败不致命。
    throughput = {"done": 0, "failed": 0}
    try:
        since = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        throughput = await asyncio.to_thread(db.throughput_since, since)
    except Exception:
        logger.warning("throughput_failed")

    # 网关中转流量累计(产物代理:pull 为 NAS 到 worker,push 为 worker 到 NAS). 读 redis hash 总量,
    # get_traffic 内已吞异常;再包一层防 redis 连接级抛出影响整体(降级为 0)。
    traffic = {"pull_bytes": 0, "push_bytes": 0}
    try:
        pull = await redis.get_traffic("pull")
        push = await redis.get_traffic("push")
        traffic = {"pull_bytes": pull.get("total", 0), "push_bytes": push.get("total", 0)}
    except Exception:
        logger.warning("traffic_failed")

    # 链路流量快照:ECS 与 NAS 隧道 rx/tx + 每隧道 + up + 网关聚合 + 当前速率,由 tunnel_stats 上报器周期写。
    # 只放当前快照(轻);按节点时间趋势走单独端点 /api/link-traffic/history(富时间线,前端点节点时才用)。
    # 无上报器或无边缘时返回 None,前端「通联」区不渲染。
    link_traffic = None
    try:
        lt = await redis.get_link_traffic()
        if isinstance(lt, dict):  # 仅真实快照(dict)才透出;防 redis mock/异常对象流进响应
            link_traffic = lt
    except Exception:
        logger.warning("link_traffic_failed")

    return {
        "version": FLORI_VERSION,
        "components": list(components),
        "health": readiness,
        **live,
        "throughput_1h": throughput,
        "traffic": traffic,
        "link_traffic": link_traffic,
    }


def _parse_iso(value):
    """解析 ISO 时间串为 aware-UTC,naive 补 UTC;失败/空返回 None。"""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@router.get(
    "/status", dependencies=[Depends(verify_token)], response_model=FullStatusResponse,
)
async def system_status(request: Request):
    """全量系统状态(version + 组件健康 + workers/pools/jobs/disk + throughput_1h)。
    components.detail 不暴露密钥/连接串;逐组件探测失败只影响该组件,整体不 500。"""
    return await build_full_status(request.app)


@router.get(
    "/link-traffic/history", dependencies=[Depends(verify_token)],
    response_model=LinkTrafficHistoryResponse,
)
async def link_traffic_history(request: Request, limit: int = 120):
    """通联富时间线(tunnel_stats 上报器周期采的累计字节样本,最近在前):
    每样本含 总量 gw/tun + 每隧道 t{} + 每远程 worker w{}. 前端「通联」树点节点后切该节点序列算趋势.
    读失败或无上报器时返回空. limit 截断,默认 120 约 40min @20s。"""
    redis = request.app.state.redis
    try:
        samples = await redis.get_traffic_timeline(max(1, min(limit, 360)))
    except Exception:
        samples = []
    return {"samples": samples}


@router.get(
    "/usage", dependencies=[Depends(verify_token)], response_model=UsageAggregateResponse,
)
async def usage_aggregate(db: Database = Depends(get_db)):
    """全量 AI 用量聚合:累计 token/缓存/成本 + 平均缓存命中率 + 按 model 分(供系统状态展示)。"""
    return await asyncio.to_thread(db.get_usage_aggregate)


@router.get(
    "/pricing", dependencies=[Depends(verify_token)], response_model=PricingStatusResponse,
)
async def pricing_status(request: Request):
    """LiteLLM 价表状态:{ready, model_count, fetched_at(ISO|null), source_url}。"""
    return request.app.state.pricing.status()


@router.post(
    "/pricing/refresh", dependencies=[Depends(verify_token)],
    response_model=PricingStatusResponse,
)
async def pricing_refresh(request: Request, storage=Depends(get_storage)):
    """手动拉一次 LiteLLM 最新价表并存回 MinIO. 成功回新 status;拉取失败 502。"""
    pricing = request.app.state.pricing
    ok = await pricing.refresh(storage)
    if not ok:
        raise HTTPException(502, "拉取 LiteLLM 价表失败(网络/上游异常),已保留旧表")
    return pricing.status()


@router.get("/pricing/raw", dependencies=[Depends(verify_token)])
async def pricing_raw(request: Request):
    """原始 LiteLLM 价表(全量 dict,供前端新标签/弹窗查看)。空表返回 {}。"""
    return request.app.state.pricing.raw()


@router.get(
    "/events", dependencies=[Depends(verify_token)], response_model=SystemEventsResponse,
)
async def list_events(limit: int = 50, redis: RedisClient = Depends(get_redis)):
    """系统事件流(scheduler emit 的环形列表 events:system,最近在上,保留最近 200)。
    scheduler 在孤儿回收、卡步、无 worker、worker 清理、任务失败处 push_event;无事件或读失败返回空数组。"""
    import json as _json
    limit = max(1, min(limit, 200))
    try:
        raw = await redis.r.lrange("events:system", 0, limit - 1)
    except Exception:
        return {"events": []}
    events = []
    for item in raw or []:
        try:
            events.append(_json.loads(item))
        except (ValueError, TypeError):
            continue
    return {"events": events}


@router.get(
    "/config/styles", dependencies=[Depends(verify_token)], response_model=list[str],
)
async def get_styles_config(config: AppConfig = Depends(get_config)):
    """返回可用风格标签列表(从 prompts/styles/*.yaml 的文件名读取)。"""
    import yaml

    styles_dir = config.prompts_dir / "styles"
    if not styles_dir.exists():
        return []
    result = []
    for f in sorted(styles_dir.glob("*.yaml")):
        try:
            data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        except Exception:
            data = {}
        result.append(data.get("tag") or f.stem)
    return result


@router.get("/config/pools", dependencies=[Depends(verify_token)])
async def get_pools_config(config: AppConfig = Depends(get_config)):
    return config.pools


@router.put("/config/pools", dependencies=[Depends(verify_token)])
async def update_pools_config(
    new_config: dict,
    config: AppConfig = Depends(get_config),
    redis: RedisClient = Depends(get_redis),
):
    import yaml
    # 结构校验:必须含 pools 映射,每池含整数 limit,防畸形 PUT 损坏 pools.yaml 与在跑调度器池配置.
    if not isinstance(new_config, dict) or not isinstance(new_config.get("pools"), dict):
        raise HTTPException(400, "config must contain a 'pools' mapping")
    for name, pc in new_config["pools"].items():
        if not isinstance(pc, dict) or not isinstance(pc.get("limit"), int):
            raise HTTPException(400, f"pool '{name}' must have an integer 'limit'")
    path = config.config_dir / "pools.yaml"
    # 先落盘成功再改内存配置:写失败则回 500 且不污染在跑配置(无半改)。
    try:
        path.write_text(yaml.dump(new_config, allow_unicode=True))
    except OSError as e:
        raise HTTPException(500, f"failed to write pools.yaml: {e}")
    config.pools = new_config
    await redis.publish("config_reload", {"type": "pools"})
    return {"status": "updated"}


@router.get(
    "/config/pool-limits", dependencies=[Depends(verify_token)],
    response_model=PoolLimitsResponse,
)
async def get_pool_limits(
    config: AppConfig = Depends(get_config),
    redis: RedisClient = Depends(get_redis),
):
    """各池 {default(pools.yaml), override(redis 运行时覆盖,可为 null)}。前端据此渲染可调表单。"""
    overrides = await redis.get_all_pool_limit_overrides()
    pools = (config.pools or {}).get("pools", {}) or {}
    return {
        p: {"default": int((pc or {}).get("limit", 1024)), "override": overrides.get(p)}
        for p, pc in pools.items()
    }


@router.put(
    "/config/pool-limits", dependencies=[Depends(verify_token)],
    response_model=StatusUpdatedResponse,
)
async def update_pool_limits(
    body: dict,
    config: AppConfig = Depends(get_config),
    redis: RedisClient = Depends(get_redis),
):
    """运行时覆盖每池上限(写 redis,不动 pools.yaml);即时对所有 worker(含网关)生效。
    body: {pool: int}(设覆盖,0=暂停该池)或 {pool: null}(清除回落默认)。"""
    pools = (config.pools or {}).get("pools", {}) or {}
    if not isinstance(body, dict) or not body:
        raise HTTPException(400, "body must be a non-empty {pool: int|null} mapping")
    for pool, val in body.items():
        if pool not in pools:
            raise HTTPException(400, f"unknown pool '{pool}'")
        if val is None:
            await redis.clear_pool_limit_override(pool)
        elif isinstance(val, int) and not isinstance(val, bool) and val >= 0:
            await redis.set_pool_limit_override(pool, val)
        else:
            raise HTTPException(400, f"pool '{pool}' limit must be a non-negative integer or null")
    return {"status": "updated"}


@router.get(
    "/pipelines", dependencies=[Depends(verify_token)], response_model=PipelinesResponse,
)
async def list_pipelines(
    config: AppConfig = Depends(get_config), db: Database = Depends(get_db)
):
    """流水线只读视图:各 pipeline 的步骤 DAG(键+中文名+池 + is_ai/has_override)。
    模板/'.'前缀/default 不算 pipeline。is_ai=pool=='ai',标记可编辑 prompt 的 AI 节点;
    has_override=该 (pipeline,step) 已有 prompt 覆盖,前端据此画圆点角标。"""
    overridden = {
        (o["pipeline"], o["step"])
        for o in await asyncio.to_thread(db.list_prompt_overrides)
    }
    out = []
    for name, pc in (config.pipelines or {}).items():
        if name.startswith(".") or name == "default":
            continue
        steps = (pc or {}).get("steps")
        if not isinstance(steps, list):
            continue
        out.append({
            "name": name,
            "steps": [
                {"key": s.get("name"), "label": s.get("label"), "pool": s.get("pool"),
                 # 依赖(YAML needs 归一化为内部 depends_on,见 config._FIELD_ALIASES),供前端画 DAG
                 "needs": s.get("depends_on") or [],
                 "is_ai": s.get("pool") == "ai",
                 "has_override": (name, s.get("name")) in overridden}
                for s in steps
            ],
        })
    return {"pipelines": out}
