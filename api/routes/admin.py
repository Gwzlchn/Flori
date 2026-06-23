"""系统状态 + 健康检查 + 配置管理。"""

from __future__ import annotations

import asyncio
import shutil

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse

logger = structlog.get_logger(component="admin")

from shared.config import AppConfig, load_yaml
from shared.db import Database
from shared.redis_client import RedisClient
from api.deps import get_config, get_db, get_redis, verify_token

router = APIRouter(prefix="/api", tags=["admin"])


@router.get("/health")
async def health(request: Request, db: Database = Depends(get_db), redis: RedisClient = Depends(get_redis)):
    # 刻意免鉴权(同 /metrics):供存活探针/编排健康检查;仅暴露 up/disk/worker 计数,无敏感信息。
    # 与 WS/REST 的 verify_token 是有意区分——若需收紧,在反代/网络层限制本路由可达性。
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
    checks["workers_online"] = sum(1 for w in workers if w.status.startswith("online"))

    status = "healthy" if checks["redis"] == "ok" and checks["db"] == "ok" else "unhealthy"
    return {"status": status, "checks": checks}


@router.get("/metrics", response_class=PlainTextResponse)
async def metrics(request: Request, db: Database = Depends(get_db), redis: RedisClient = Depends(get_redis)):
    """Prometheus 文本指标(免鉴权,同 /health;只暴露计数/容量,无敏感信息)。
    个人工具不内置时序库,此端点供外部 Prometheus 抓取——补齐审计 #26『无 /metrics 端点』。"""
    redis_up = 1
    try:
        await redis.ping()
    except Exception:
        redis_up = 0
    db_up = 1
    try:
        await asyncio.to_thread(db.list_jobs, limit=1)
    except Exception:
        db_up = 0

    config: AppConfig = request.app.state.config
    data_path = str(config.data_dir) if hasattr(config, "data_dir") else "/data"
    try:
        disk_free = round(shutil.disk_usage(data_path).free / (1024**3), 2)
    except (FileNotFoundError, OSError):
        disk_free = -1

    workers = await asyncio.to_thread(db.list_workers)
    online = sum(1 for w in workers if w.status.startswith("online"))
    lines = [
        "# TYPE flori_up gauge", "flori_up 1",
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


async def build_system_status(db, redis, config) -> dict:
    """聚合系统状态(workers/pools/jobs/disk)。供 GET /api/status 与 WS /api/ws/global 共用,
    避免两处各自拼装导致漂移(WS 此前只回 jobs 计数,与契约「格式同 GET /api/status」不符)。"""
    workers = await asyncio.to_thread(db.list_workers)
    worker_summary = {}
    for w in workers:
        wtype = w.type
        if wtype not in worker_summary:
            worker_summary[wtype] = {"online": 0, "busy": 0}
        if w.status.startswith("online") or w.status == "paused":
            worker_summary[wtype]["online"] += 1
        if w.status == "online-busy":
            worker_summary[wtype]["busy"] += 1

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


@router.get("/status", dependencies=[Depends(verify_token)])
async def system_status(
    db: Database = Depends(get_db),
    redis: RedisClient = Depends(get_redis),
    config: AppConfig = Depends(get_config),
):
    return await build_system_status(db, redis, config)


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
    # 结构校验:必须含 pools 映射,每池含整数 limit——挡畸形 PUT 损坏 pools.yaml + 在跑调度器池配置。
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


@router.get("/config/pool-limits", dependencies=[Depends(verify_token)])
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


@router.put("/config/pool-limits", dependencies=[Depends(verify_token)])
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


@router.get("/pipelines", dependencies=[Depends(verify_token)])
async def list_pipelines(config: AppConfig = Depends(get_config)):
    """流水线只读视图:各 pipeline 的步骤 DAG(键+中文名+池)。模板/'.'前缀/default 不算 pipeline。"""
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
                {"key": s.get("name"), "label": s.get("label"), "pool": s.get("pool")}
                for s in steps
            ],
        })
    return {"pipelines": out}
