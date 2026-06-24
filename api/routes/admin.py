"""系统状态 + 健康检查 + 配置管理。"""

from __future__ import annotations

import asyncio
import shutil
from datetime import datetime, timedelta, timezone

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse

logger = structlog.get_logger(component="admin")

from shared.config import AppConfig, load_yaml
from shared.db import Database
from shared.redis_client import RedisClient
from shared.status import (
    DEFAULT_ONLINE_WINDOW_SEC,
    DEFAULT_STALE_WINDOW_SEC,
    compute_component_status,
)
from shared.storage import RemoteStorage
from shared.sysload import read_process_rss_mb
from shared.version import FLORI_VERSION
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


# 历史别名:WS 旧 import build_system_status,保留指向 live 子集(契约收窄,对现有消费方无破坏)。
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
        comp["detail"] = f"读心跳失败: {str(e)[:120]}"
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
    """Redis 组件:ping 计时 + INFO。超时(2s)/异常 → unknown(采集失败 ≠ 红);ping_ms>200 或
    内存临界 → degraded。"""
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
        comp.update(status="unknown", detail=f"redis 探活失败: {str(e)[:120]}")
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


async def _probe_minio(storage) -> dict:
    """MinIO 组件:RemoteStorage 才探活(本地盘 mode=local→unknown 不标红)。超时 3s/异常 → down/unknown。"""
    comp = {
        "name": "minio", "kind": "minio", "status": "unknown", "version": None,
        "last_heartbeat": None, "uptime_sec": None, "detail": None, "extra": {},
    }
    now = datetime.now(timezone.utc)
    if not isinstance(storage, RemoteStorage):
        h = await storage.health() if storage is not None else {"mode": "local", "detail": "本地盘"}
        comp.update(detail=h.get("detail"), extra={"mode": h.get("mode", "local")})
        return comp
    try:
        h = await asyncio.wait_for(storage.health(), timeout=3)
    except asyncio.TimeoutError:
        comp.update(status="down", detail="对象存储探活超时(3s)", extra={"mode": "remote"})
        return comp
    except Exception as e:  # noqa: BLE001
        comp.update(status="down", detail=f"对象存储不可达: {str(e)[:120]}", extra={"mode": "remote"})
        return comp
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
    + throughput_1h。逐组件独立 try+超时:单项异常→该组件 unknown/down + detail,绝不让整体 500。"""
    db = app.state.db
    redis = app.state.redis
    config = app.state.config
    storage = getattr(app.state, "storage", None)
    online_window, stale_window = _windows(config)

    live = await build_live_status(db, redis, config)

    # 组件探测各自隔离:gather(return_exceptions)兜底,任一抛出退化为 unknown 占位(不影响其余)。
    async def _safe(coro, name, kind):
        try:
            return await coro
        except Exception as e:  # noqa: BLE001
            logger.warning("component_probe_failed", component=name, error=str(e)[:200])
            return {"name": name, "kind": kind, "status": "unknown", "version": None,
                    "last_heartbeat": None, "uptime_sec": None,
                    "detail": f"探测异常: {str(e)[:120]}", "extra": {}}

    components = await asyncio.gather(
        _safe(_probe_api(app, online_window), "api", "api"),
        _safe(_probe_scheduler(redis, online_window, stale_window), "scheduler", "scheduler"),
        _safe(_probe_redis(redis), "redis", "redis"),
        _safe(_probe_minio(storage), "minio", "minio"),
    )

    # MinIO 容量(对象数/总字节):读后台缓存,绝不在此同步扫(贵)。有缓存才填,无则不填(前端显 —)。
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

    # 网关中转流量累计(产物代理:pull=NAS→worker 出库,push=worker→NAS 入库)。读 redis hash 总量,
    # get_traffic 内已吞异常;再包一层防 redis 连接级抛出影响整体(降级为 0)。
    traffic = {"pull_bytes": 0, "push_bytes": 0}
    try:
        pull = await redis.get_traffic("pull")
        push = await redis.get_traffic("push")
        traffic = {"pull_bytes": pull.get("total", 0), "push_bytes": push.get("total", 0)}
    except Exception:
        logger.warning("traffic_failed")

    return {
        "version": FLORI_VERSION,
        "components": list(components),
        **live,
        "throughput_1h": throughput,
        "traffic": traffic,
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


@router.get("/status", dependencies=[Depends(verify_token)])
async def system_status(request: Request):
    """全量系统状态(version + 组件健康 + workers/pools/jobs/disk + throughput_1h)。
    components.detail 不暴露密钥/连接串;逐组件探测失败→该组件 unknown/down,整体不 500。"""
    return await build_full_status(request.app)


@router.get("/usage", dependencies=[Depends(verify_token)])
async def usage_aggregate(db: Database = Depends(get_db)):
    """全量 AI 用量聚合:累计 token/缓存/成本 + 平均缓存命中率 + 按 model 分(供系统状态展示)。"""
    return await asyncio.to_thread(db.get_usage_aggregate)


@router.get("/events", dependencies=[Depends(verify_token)])
async def list_events(limit: int = 50, redis: RedisClient = Depends(get_redis)):
    """系统事件流(scheduler emit 的环形列表 events:system,最近在上,保留最近 200)。
    scheduler 在 孤儿回收/卡步/无worker/worker清理/任务失败 处 push_event;无事件→空数组;读失败→空。"""
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
