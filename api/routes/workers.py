"""Worker 管理路由。"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query

from shared.config import AppConfig
from shared.db import Database
from shared.redis_client import RedisClient
from shared.status import (
    DEFAULT_ONLINE_WINDOW_SEC,
    DEFAULT_STALE_WINDOW_SEC,
    OFFLINE,
    PAUSED,
    STALE,
    compute_worker_status,
)
from api.deps import get_config, get_db, get_redis, verify_token
from api.schemas import WorkerConfigRequest, WorkerResponse, WorkerUpdateRequest

router = APIRouter(prefix="/api/workers", tags=["workers"], dependencies=[Depends(verify_token)])


def _windows(config: AppConfig) -> tuple[int, int]:
    """从 pools.yaml 读在线/失联窗口,缺省回退到内置默认(全栈共用一套阈值)。"""
    cfg = config.pools.get("worker_status", {}) if config else {}
    return (
        int(cfg.get("online_window_sec", DEFAULT_ONLINE_WINDOW_SEC)),
        int(cfg.get("stale_window_sec", DEFAULT_STALE_WINDOW_SEC)),
    )


def _iso_utc(value: datetime | str | None) -> str | None:
    """序列化时间戳为带 Z 后缀的 UTC ISO 串,让前端无歧义按 UTC 解析。
    入参可能是 DB 来的 aware datetime,或 Redis 里存的原始 ISO 串(可能 naive)。"""
    if value is None:
        return None
    if isinstance(value, str):
        if not value:
            return None
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _to_response(w) -> WorkerResponse:
    return WorkerResponse(
        id=w.id, type=w.type, pools=w.pools,
        tags=sorted(w.tags), reject_tags=sorted(w.reject_tags),
        hostname=w.hostname, gpu_name=w.gpu_name, gpu_memory_mb=w.gpu_memory_mb,
        concurrency=w.concurrency, remote_addr=w.remote_addr,
        status=w.status,
        current_job=w.current_job, current_step=w.current_step,
        tasks_completed=w.tasks_completed, tasks_failed=w.tasks_failed,
        total_duration_sec=w.total_duration_sec,
        first_seen=_iso_utc(w.first_seen),
        started_at=_iso_utc(w.started_at),
        last_heartbeat=_iso_utc(w.last_heartbeat),
        admin_note=w.admin_note,
        desired_config=w.desired_config,
        cfg_rev=w.cfg_rev,
    )


def _int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_iso(value: str | None) -> datetime | None:
    """解析 Redis 里存的 ISO 时间串为 aware-UTC,naive 补 UTC。"""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _spec(info: dict) -> dict:
    """从 Redis info 解析 worker 自报的 spec(版本/机器配置)JSON;失败返回 {}。"""
    try:
        return json.loads(info.get("spec") or "{}") or {}
    except (ValueError, TypeError):
        return {}


def _load(info: dict) -> dict:
    """从 Redis info 解析 worker 自报的 live 负载(cpu%/mem%/loadavg)JSON;失败返回 {}。"""
    try:
        return json.loads(info.get("load") or "{}") or {}
    except (ValueError, TypeError):
        return {}


@router.get("")
async def list_workers(
    db: Database = Depends(get_db),
    redis: RedisClient = Depends(get_redis),
    config: AppConfig = Depends(get_config),
):
    online_window, stale_window = _windows(config)
    workers = await asyncio.to_thread(db.list_workers, online_window, stale_window)
    by_id: dict[str, WorkerResponse] = {w.id: _to_response(w) for w in workers}
    # 网关中转流量(产物代理 pull/push 字节,按 worker 归因);读两个 hash 一次,按 id 查填。
    pull_by = (await redis.get_traffic("pull")).get("by_worker", {})
    push_by = (await redis.get_traffic("push")).get("by_worker", {})

    def _traffic(wid: str) -> dict:
        return {"pull": pull_by.get(wid, 0), "push": push_by.get(wid, 0)}

    for wid, resp in by_id.items():
        resp.traffic = _traffic(wid)
    # 合并 Redis 里注册的远程 worker:本地 SQLite 没有它们(状态写在 Redis),
    # 没这一步分布式 worker 在 /api/workers 里是隐身的。Redis key 带 TTL,
    # 失活的远程 worker 会自动消失。状态同样按 last_heartbeat 走后端权威判定,
    # 不信任 Redis 里 worker 自报的 status 字段。
    now = datetime.now(timezone.utc)
    for wid in await redis.list_worker_ids():
        info = await redis.get_worker_info(wid)
        if not info:
            continue
        status = compute_worker_status(
            last_heartbeat=_parse_iso(info.get("last_heartbeat")),
            current_job=info.get("current_job") or None,
            admin_status=info.get("admin_status"),
            now=now,
            online_window_sec=online_window,
            stale_window_sec=stale_window,
        )
        # Redis 是实时 liveness 源(TTL,每心跳刷新);db.last_heartbeat 在 worker 空闲时可能不刷新而
        # 过期,据此判定会误标离线。故 db 已有该 worker 时,用 Redis 覆盖状态/心跳/当前任务(累计统计
        # 仍用 db);Redis 里没有的(已失活)保留 db 判定。
        existing = by_id.get(wid)
        if existing is not None:
            existing.status = status
            existing.last_heartbeat = _iso_utc(info.get("last_heartbeat"))
            existing.current_job = info.get("current_job") or None
            existing.current_step = info.get("current_step") or None
            existing.spec = _spec(info)
            existing.load = _load(info)
            existing.applied_cfg_rev = _int(info.get("cfg_applied_rev"))
            continue
        by_id[wid] = WorkerResponse(
            id=wid,
            type=info.get("type", ""),
            pools=[p for p in info.get("pools", "").split(",") if p],
            tags=[t for t in info.get("tags", "").split(",") if t],
            reject_tags=[t for t in info.get("reject_tags", "").split(",") if t],
            hostname=info.get("hostname"),
            gpu_name=info.get("gpu_name") or None,
            gpu_memory_mb=_int(info.get("gpu_memory_mb")) or None,
            concurrency=_int(info.get("concurrency")) or 1,
            remote_addr=info.get("remote_addr") or None,
            spec=_spec(info),
            load=_load(info),
            traffic=_traffic(wid),
            status=status,
            current_job=info.get("current_job") or None,
            current_step=info.get("current_step") or None,
            tasks_completed=_int(info.get("tasks_completed")),
            tasks_failed=_int(info.get("tasks_failed")),
            total_duration_sec=_float(info.get("total_duration_sec")),
            first_seen=_iso_utc(info.get("started_at") or info.get("last_heartbeat")) or "",
            started_at=_iso_utc(info.get("started_at")),
            last_heartbeat=_iso_utc(info.get("last_heartbeat")),
            admin_note=None,
        )
    return list(by_id.values())


@router.post("/registration-token")
async def mint_registration_token(redis: RedisClient = Depends(get_redis)):
    """铸/重置接入 token,重铸即作废旧的。默认 24h 过期,泄漏自动失效;
    长期接入用 env WORKER_REGISTRATION_TOKEN。TTL 可经 REGISTRATION_TOKEN_TTL_SEC 调。"""
    token = "flw-" + secrets.token_urlsafe(18)
    ttl = int(os.environ.get("REGISTRATION_TOKEN_TTL_SEC", "86400"))
    await redis.set_registration_token(token, ttl_sec=ttl)
    return {"token": token, "expires_in_sec": ttl}


@router.get("/registration-token")
async def registration_token_status(redis: RedisClient = Depends(get_redis)):
    """接入 token 状态(不回明文):是否已铸 + 剩余有效秒。注:env WORKER_REGISTRATION_TOKEN
    配的长期 token 不经 redis,不在此反映。须置于 GET /{worker_id} 之前,否则被路径参数路由遮蔽。"""
    tok = await redis.get_registration_token()
    ttl = await redis.get_registration_token_ttl() if tok else -2
    return {"exists": bool(tok), "expires_in_sec": (ttl if ttl and ttl > 0 else None)}


@router.get("/{worker_id}")
async def get_worker(
    worker_id: str,
    db: Database = Depends(get_db),
    redis: RedisClient = Depends(get_redis),
    config: AppConfig = Depends(get_config),
):
    online_window, stale_window = _windows(config)
    w = await asyncio.to_thread(db.get_worker, worker_id, online_window, stale_window)
    if not w:
        raise HTTPException(404, "worker not found")
    resp = _to_response(w)
    # 网关中转流量(产物代理 pull/push 字节,按 worker 归因);redis-only。
    resp.traffic = {
        "pull": (await redis.get_traffic("pull")).get("by_worker", {}).get(worker_id, 0),
        "push": (await redis.get_traffic("push")).get("by_worker", {}).get(worker_id, 0),
    }
    # 同 list_workers:Redis 是实时 liveness 源(TTL,每心跳刷新),db.last_heartbeat 在 worker 空闲时
    # 可能不刷新而过期。故用 Redis 覆盖状态/心跳/当前任务;Redis 无该 worker 时保留 db 判定,即已失活。
    info = await redis.get_worker_info(worker_id)
    if info:
        resp.status = compute_worker_status(
            last_heartbeat=_parse_iso(info.get("last_heartbeat")),
            current_job=info.get("current_job") or None,
            admin_status=info.get("admin_status"),
            now=datetime.now(timezone.utc),
            online_window_sec=online_window,
            stale_window_sec=stale_window,
        )
        resp.last_heartbeat = _iso_utc(info.get("last_heartbeat"))
        resp.current_job = info.get("current_job") or None
        resp.current_step = info.get("current_step") or None
        if info.get("remote_addr"):
            resp.remote_addr = info.get("remote_addr")
        resp.spec = _spec(info)
        resp.load = _load(info)
        resp.applied_cfg_rev = _int(info.get("cfg_applied_rev"))
    return resp


@router.get("/{worker_id}/tasks")
async def list_worker_tasks(
    worker_id: str,
    limit: int = Query(50, ge=1, le=200),
    db: Database = Depends(get_db),
):
    """该 worker 的 task 执行历史(task = 某作业的某步骤的一次执行;每条对应一个 step 记录)。
    enrich 作业标题/类型,使卡片显作业标题而非裸 job_id(与队列页同款 task 数据)。"""
    steps = await asyncio.to_thread(db.list_worker_tasks, worker_id, limit)
    briefs = await asyncio.to_thread(db.jobs_brief, [s.job_id for s in steps])
    return [
        {
            "job_id": s.job_id,
            "title": (briefs.get(s.job_id) or {}).get("title"),
            "content_type": (briefs.get(s.job_id) or {}).get("content_type"),
            "domain": (briefs.get(s.job_id) or {}).get("domain"),
            "step": s.name,
            "status": s.status.value if hasattr(s.status, "value") else s.status,
            "started_at": _iso_utc(s.started_at),
            "finished_at": _iso_utc(s.finished_at),
            "duration_sec": s.duration_sec,
            "error": s.error,
        }
        for s in steps
    ]


@router.put("/{worker_id}/config")
async def set_worker_config(
    worker_id: str,
    req: WorkerConfigRequest,
    db: Database = Depends(get_db),
    redis: RedisClient = Depends(get_redis),
):
    """中心下发 worker 运营参数(当前仅并发),worker 下一心跳(≤10s)热应用,不重启容器。
    能力(pools/tags)刻意不收:那是机器客观属性——池由启动参数表达、net-zone 由探针判定,
    人在页面改一台够不着 B 站机器的池并不能让它下载成功,语义即错(用户 review 定论)。
    cfg_rev 单调递增,worker 按 rev 幂等应用。"""
    cfg: dict = {}
    if req.concurrency is not None:
        cfg["concurrency"] = req.concurrency
    if not cfg:
        raise HTTPException(400, "至少提供一个配置项(当前支持: concurrency)")
    rev = await asyncio.to_thread(db.set_worker_desired_config, worker_id, cfg)
    if rev < 0:
        raise HTTPException(404, "worker not found")
    try:
        await redis.push_event(
            "worker_config_updated", worker_id=worker_id, cfg_rev=rev,
            **{k: v for k, v in cfg.items()},
        )
    except Exception:
        pass
    return {"cfg_rev": rev, "desired_config": cfg}


@router.put("/{worker_id}")
async def update_worker(
    worker_id: str,
    req: WorkerUpdateRequest,
    db: Database = Depends(get_db),
    redis: RedisClient = Depends(get_redis),
):
    w = await asyncio.to_thread(db.get_worker, worker_id)
    if not w:
        raise HTTPException(404, "worker not found")
    # status 入参解释为暂停/恢复指令 → 写独立的 admin_status 叠加位(不碰运行时 status)。
    # 这样 busy worker 暂停后跑完当前步不会被 idle 覆盖,gateway 心跳自报 idle 也不覆盖。
    admin_status: str | None = None
    if req.status is not None:
        if req.status == PAUSED:
            admin_status = PAUSED
        elif req.status in ("active", "resume", "idle", "online-idle", ""):
            admin_status = ""
        else:
            raise HTTPException(400, f"无效 status '{req.status}'(仅支持 'paused' / 'active')")
        w.admin_status = admin_status
    if req.admin_note is not None:
        w.admin_note = req.admin_note
    if req.tags is not None:
        w.tags = set(req.tags)
    if req.reject_tags is not None:
        w.reject_tags = set(req.reject_tags)
    await asyncio.to_thread(db.upsert_worker, w)
    # 暂停真生效:worker 认领读 Redis 的 admin_status,只写 SQLite 不顶用,必须同步进 Redis。
    # tags 同理透传给在跑的 worker 认领逻辑。
    if admin_status is not None:
        await redis.set_worker_field(worker_id, "admin_status", admin_status)
    if req.tags is not None:
        await redis.set_worker_field(worker_id, "tags", ",".join(sorted(req.tags)))
    if req.reject_tags is not None:
        await redis.set_worker_field(
            worker_id, "reject_tags", ",".join(sorted(req.reject_tags))
        )
    return {"id": worker_id, "status": "updated"}


@router.delete("/{worker_id}", status_code=204)
async def delete_worker(
    worker_id: str,
    force: bool = Query(False),
    db: Database = Depends(get_db),
    redis: RedisClient = Depends(get_redis),
    config: AppConfig = Depends(get_config),
):
    online_window, stale_window = _windows(config)
    w = await asyncio.to_thread(db.get_worker, worker_id, online_window, stale_window)
    redis_alive = await redis.worker_exists(worker_id)
    if not w and not redis_alive:
        raise HTTPException(404, "worker not found")

    # 仅离线/失联可删,活着的需 force;否则下次扫描又冒出来(远程 worker 尤甚)。
    # 状态统一按心跳衍生:DB 有行用 DB 行,否则用 Redis hash 现算。
    if w is not None:
        status = w.status
    else:
        info = await redis.get_worker_info(worker_id) or {}
        status = compute_worker_status(
            last_heartbeat=_parse_iso(info.get("last_heartbeat")),
            current_job=info.get("current_job") or None,
            admin_status=info.get("admin_status"),
            online_window_sec=online_window,
            stale_window_sec=stale_window,
        )
    if status not in (OFFLINE, STALE) and not force:
        raise HTTPException(409, "worker is online; pass force=true to remove")

    if w is not None:
        await asyncio.to_thread(db.delete_worker, worker_id)
    # 删 worker 连带吊销其 per-worker token:被删的 worker 心跳/认领立即 401,杜绝复活。
    await asyncio.to_thread(db.revoke_worker_token, worker_id)
    # 远程 worker 只在 Redis 里活着,必须连 Redis key 一起清,否则会复活。
    await redis.delete_worker(worker_id)
