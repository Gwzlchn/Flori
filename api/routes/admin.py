"""系统状态 + 健康检查 + 配置管理。"""

from __future__ import annotations

import asyncio
import shutil

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request

logger = structlog.get_logger(component="admin")

from shared.config import AppConfig, load_yaml
from shared.db import Database
from shared.redis_client import RedisClient
from api.deps import get_config, get_db, get_redis, verify_token

router = APIRouter(prefix="/api", tags=["admin"])


@router.get("/health")
async def health(request: Request, db: Database = Depends(get_db), redis: RedisClient = Depends(get_redis)):
    checks = {}
    try:
        await redis.ping()
        checks["redis"] = "ok"
    except Exception:
        logger.exception("health_redis_error")
        checks["redis"] = "error"

    try:
        await asyncio.to_thread(db.list_jobs, limit=1)
        checks["db"] = "ok"
    except Exception:
        logger.exception("health_db_error")
        checks["db"] = "error"

    config: AppConfig = request.app.state.config
    data_path = str(config.data_dir) if hasattr(config, "data_dir") else "/data"
    try:
        disk = shutil.disk_usage(data_path)
        checks["disk_free_gb"] = round(disk.free / (1024**3), 1)
    except (FileNotFoundError, OSError):
        checks["disk_free_gb"] = -1

    workers = await asyncio.to_thread(db.list_workers)
    checks["workers_online"] = sum(1 for w in workers if w.status in ("idle", "busy"))

    status = "healthy" if checks["redis"] == "ok" and checks["db"] == "ok" else "unhealthy"
    return {"status": status, "checks": checks}


@router.get("/status", dependencies=[Depends(verify_token)])
async def system_status(
    db: Database = Depends(get_db),
    redis: RedisClient = Depends(get_redis),
    config: AppConfig = Depends(get_config),
):
    workers = await asyncio.to_thread(db.list_workers)
    worker_summary = {}
    for w in workers:
        wtype = w.type
        if wtype not in worker_summary:
            worker_summary[wtype] = {"online": 0, "busy": 0}
        if w.status in ("idle", "busy"):
            worker_summary[wtype]["online"] += 1
        if w.status == "busy":
            worker_summary[wtype]["busy"] += 1

    pools_cfg = config.pools.get("pools", {})
    pools_info = {}
    for pool_name, pcfg in pools_cfg.items():
        count = await redis.get_pool_count(pool_name)
        queue = await redis.get_queue_info(pool_name)
        pools_info[pool_name] = {
            "capacity": pcfg.get("limit", 999),
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
        disk_info = {
            "used_gb": round(disk.used / (1024**3), 1),
            "available_gb": round(disk.free / (1024**3), 1),
        }
    except (FileNotFoundError, OSError):
        disk_info = {"used_gb": -1, "available_gb": -1}

    return {
        "workers": worker_summary,
        "pools": pools_info,
        "jobs": {"total": total, **stats},
        "disk": disk_info,
    }


@router.get("/config/styles", dependencies=[Depends(verify_token)])
async def get_styles_config(config: AppConfig = Depends(get_config)):
    """返回可用风格标签列表（从 prompts/styles/*.yaml 的文件名读取）。"""
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
    path = config.config_dir / "pools.yaml"
    path.write_text(yaml.dump(new_config, allow_unicode=True))
    config.pools = new_config
    await redis.publish("config_reload", {"type": "pools"})
    return {"status": "updated"}
