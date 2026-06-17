"""任务管理路由。"""

from __future__ import annotations

import asyncio
import json
import secrets
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from fastapi.responses import PlainTextResponse

from shared.config import AppConfig
from shared.db import Database
from shared.models import Job, JobStatus, Step, StepStatus, derive_job_id
from shared.redis_client import RedisClient
from shared.source_detect import detect_source
from shared.storage import StorageBackend

from api.deps import get_config, get_db, get_redis, get_storage, verify_token
from api.schemas import (
    JobCreateRequest,
    JobDetailResponse,
    JobListResponse,
    JobResponse,
    RerunRequest,
    RerunSmartRequest,
    StepResponse,
)

router = APIRouter(prefix="/api/jobs", tags=["jobs"], dependencies=[Depends(verify_token)])

# 同模块第二个路由:/api/providers(不能挂在 /api/jobs 下,否则被 /{job_id} 截胡)。
providers_router = APIRouter(prefix="/api/providers", tags=["providers"],
                            dependencies=[Depends(verify_token)])


@providers_router.get("")
async def list_providers(config: AppConfig = Depends(get_config)):
    """列出 AI provider 及可用性(供前端"选 provider 重跑"挑选;未配 key 的标灰)。"""
    out = []
    for name, pc in (config.providers.get("providers") or {}).items():
        if name == "local":
            continue  # 本地 ollama 默认不展示
        out.append({
            "name": name,
            "type": pc.get("type", ""),
            "available": _provider_available(name, config.providers),
            "label": "订阅" if pc.get("type") == "cli" else "API",
        })
    return {"providers": out}


def _validate_job_id(job_id: str) -> None:
    if ".." in job_id or "/" in job_id or "\x00" in job_id:
        raise HTTPException(400, "invalid job_id")


def _detect_content_type(url: str | None, filename: str | None = None) -> str:
    if filename:
        name = filename.lower()
        if name.endswith(".pdf"):
            return "paper"
        if name.endswith((".mp4", ".mkv", ".webm", ".flv")):
            return "video"
        if name.endswith((".mp3", ".m4a", ".wav", ".aac")):
            return "audio"
        if name.endswith((".html", ".htm", ".txt")):
            return "article"
    if url:
        source = detect_source(url)
        if source == "arxiv":
            return "paper"
        if source == "http_article":
            return "article"
        if source == "podcast":
            return "audio"
    return "video"


def _pipeline_for(content_type: str) -> str:
    return {
        "video": "video",
        "paper": "paper",
        "article": "article",
        "audio": "audio",
    }.get(content_type, "video")


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _bili_sessdata(db: Database) -> str | None:
    """从凭证表取已登录 B站的 SESSDATA，未登录/解析失败返回 None。"""
    raw = db.get_credential("bili_cookies")
    if not raw:
        return None
    try:
        return json.loads(raw).get("sessdata") or None
    except (json.JSONDecodeError, ValueError):
        return None


async def create_job_core(
    db: Database, redis: RedisClient, storage: StorageBackend,
    url: str | None, content_type: str | None = None,
    domain: str = "general", style_tags: list[str] | None = None,
    collection_id: str | None = None, title: str | None = None,
) -> Job:
    """建 job 的核心流程(create_job 路由 + 订阅同步共用)。返回 Job。"""
    style_tags = style_tags or []
    ctype = content_type or _detect_content_type(url)
    pipeline = _pipeline_for(ctype)
    source = detect_source(url) if url else "upload"

    # 有意义的 id: jobs_{类别}_{inner}(bili=BV);撞已存在(同 BV 重投)加随机后缀。
    job_id = derive_job_id(url, ctype, source)
    if await asyncio.to_thread(db.get_job, job_id):
        job_id = f"{job_id}_{secrets.token_hex(3)}"
    job_doc = {
        "id": job_id, "url": url, "source": source, "content_type": ctype,
        "domain": domain, "style_tags": style_tags, "created_at": _now_iso(),
    }
    if source == "bilibili":
        sessdata = await asyncio.to_thread(_bili_sessdata, db)
        if sessdata:
            job_doc["sessdata"] = sessdata
    await storage.write_file(
        job_id, "job.json",
        json.dumps(job_doc, ensure_ascii=False, indent=2).encode("utf-8"),
    )
    job = Job(
        id=job_id, content_type=ctype, pipeline=pipeline, url=url, title=title,
        domain=domain, source=source, style_tags=style_tags, collection_id=collection_id,
    )
    await asyncio.to_thread(db.create_job, job)
    if collection_id:
        await asyncio.to_thread(db.increment_collection_count, collection_id, 1)
    await redis.publish("job_command", {
        "action": "new_job", "job_id": job_id, "pipeline": pipeline,
    })
    return job


@router.post("", status_code=201)
async def create_job(
    req: JobCreateRequest,
    db: Database = Depends(get_db),
    redis: RedisClient = Depends(get_redis),
    config: AppConfig = Depends(get_config),
    storage: StorageBackend = Depends(get_storage),
):
    # 校验 collection_id 存在,避免孤儿绑定 + job_count 漂移。
    if req.collection_id:
        if not await asyncio.to_thread(db.get_collection, req.collection_id):
            raise HTTPException(400, "collection_id not found")
    job = await create_job_core(
        db, redis, storage, req.url, req.content_type,
        req.domain, req.style_tags, req.collection_id,
    )
    return {"job_id": job.id, "content_type": job.content_type,
            "status": "pending", "created_at": job.created_at.isoformat()}


@router.post("/upload", status_code=201)
async def upload_job(
    file: UploadFile = File(...),
    domain: str = Form("general"),
    style_tags: str = Form("[]"),
    db: Database = Depends(get_db),
    redis: RedisClient = Depends(get_redis),
    config: AppConfig = Depends(get_config),
):
    content_type = _detect_content_type(None, file.filename)
    pipeline = _pipeline_for(content_type)
    try:
        tags = json.loads(style_tags)
    except json.JSONDecodeError:
        raise HTTPException(400, "invalid style_tags JSON")

    MAX_UPLOAD_SIZE = 2 * 1024 * 1024 * 1024  # 2GB
    job_id = derive_job_id(None, content_type, "upload")
    job_dir = config.jobs_dir / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    input_dir = job_dir / "input"
    input_dir.mkdir(exist_ok=True)

    ext = Path(file.filename).suffix if file.filename else ".mp4"
    dest = input_dir / f"source{ext}"
    total_size = 0
    with open(dest, "wb") as f:
        while chunk := await file.read(8192):
            total_size += len(chunk)
            if total_size > MAX_UPLOAD_SIZE:
                f.close()
                await asyncio.to_thread(shutil.rmtree, job_dir)
                raise HTTPException(413, f"file too large (max {MAX_UPLOAD_SIZE})")
            f.write(chunk)

    (job_dir / "job.json").write_text(json.dumps({
        "id": job_id,
        "url": None,
        "source": "upload",
        "content_type": content_type,
        "domain": domain,
        "style_tags": tags,
        "created_at": _now_iso(),
    }, ensure_ascii=False, indent=2))

    job = Job(
        id=job_id, content_type=content_type, pipeline=pipeline,
        domain=domain, source="upload", style_tags=tags,
    )
    await asyncio.to_thread(db.create_job, job)

    await redis.publish("job_command", {
        "action": "new_job", "job_id": job_id, "pipeline": pipeline,
    })

    return {"job_id": job_id, "content_type": content_type, "status": "pending", "created_at": job.created_at.isoformat()}


@router.get("")
async def list_jobs(
    status: str | None = None,
    collection_id: str | None = None,
    domain: str | None = None,
    uncategorized: bool = False,
    limit: int = 20,
    offset: int = 0,
    db: Database = Depends(get_db),
):
    total, jobs = await asyncio.to_thread(
        db.list_jobs, status=status, collection_id=collection_id,
        limit=limit, offset=offset, domain=domain, uncategorized=uncategorized,
    )
    return JobListResponse(
        total=total,
        items=[
            JobResponse(
                job_id=j.id, content_type=j.content_type, status=j.status.value,
                created_at=j.created_at.isoformat(), title=j.title,
                progress_pct=j.progress_pct, source=j.source, domain=j.domain,
                collection_id=j.collection_id,
            )
            for j in jobs
        ],
    )


@router.get("/{job_id}")
async def get_job(
    job_id: str,
    db: Database = Depends(get_db),
    config: AppConfig = Depends(get_config),
    storage: StorageBackend = Depends(get_storage),
):
    _validate_job_id(job_id)
    job = await asyncio.to_thread(db.get_job, job_id)
    if not job:
        raise HTTPException(404, "job not found")

    steps = await asyncio.to_thread(db.get_steps, job_id)
    # 步骤中文名取自 pipelines.yaml(单一事实源),按本 job 的 pipeline 查表。
    labels = {
        s["name"]: s.get("label")
        for s in config.pipelines.get(job.pipeline, {}).get("steps", [])
    }
    # 源发布时间(「上传于」)由 01_download 写入 metadata.json;读不到则 None。
    published_at = None
    try:
        raw = await storage.read_file(job_id, "input/metadata.json")
        if raw:
            published_at = json.loads(raw.decode("utf-8")).get("published_at")
    except Exception:
        pass
    return JobDetailResponse(
        job_id=job.id, content_type=job.content_type, status=job.status.value,
        created_at=job.created_at.isoformat(),
        updated_at=job.updated_at.isoformat() if job.updated_at else None,
        published_at=published_at,
        title=job.title, url=job.url,
        progress_pct=job.progress_pct, source=job.source, domain=job.domain,
        collection_id=job.collection_id,
        meta=job.meta,
        steps=[
            StepResponse(
                name=s.name, label=labels.get(s.name), status=s.status.value,
                started_at=s.started_at.isoformat() if s.started_at else None,
                finished_at=s.finished_at.isoformat() if s.finished_at else None,
                duration_sec=s.duration_sec, meta=s.meta, error=s.error,
            )
            for s in steps
        ],
    )


@router.patch("/{job_id}")
async def patch_job(
    job_id: str,
    body: dict,
    db: Database = Depends(get_db),
):
    """归类：把 job 归入/移出集合(collection_id)。collection.domain 须等于 job.domain(不变式2)。
    维护两侧 job_count。body: {"collection_id": "col_..." | null}。"""
    _validate_job_id(job_id)
    if "collection_id" not in body:
        raise HTTPException(400, "only collection_id is patchable")
    new_cid = body["collection_id"] or None
    job = await asyncio.to_thread(db.get_job, job_id)
    if not job:
        raise HTTPException(404, "job not found")
    old_cid = job.collection_id
    if new_cid == old_cid:
        return {"ok": True, "collection_id": new_cid}
    if new_cid:
        coll = await asyncio.to_thread(db.get_collection, new_cid)
        if not coll:
            raise HTTPException(400, "collection_id not found")
        if coll.domain != job.domain:
            raise HTTPException(400, f"领域不匹配：集合属于 {coll.domain}，内容属于 {job.domain}")
    await asyncio.to_thread(db.update_job, job_id, collection_id=new_cid)
    if old_cid:
        await asyncio.to_thread(db.increment_collection_count, old_cid, -1)
    if new_cid:
        await asyncio.to_thread(db.increment_collection_count, new_cid, 1)
    return {"ok": True, "collection_id": new_cid}


@router.get("/{job_id}/steps/{step}/log")
async def get_step_log(
    job_id: str,
    step: str,
    raw: int = 0,
    storage: StorageBackend = Depends(get_storage),
):
    """返回某步骤的运行日志,供前端展开排错。经存储读,兼容本地/MinIO。
    默认尾部截断 256KB;raw=1 返回完整日志(供下载)。"""
    _validate_job_id(job_id)
    if "/" in step or ".." in step or "\x00" in step:
        raise HTTPException(400, "invalid step")
    data = await storage.read_file(job_id, f"logs/{step}.log")
    if data is None:
        raise HTTPException(404, "log not found")
    if not raw:
        max_bytes = 256 * 1024
        if len(data) > max_bytes:
            data = b"...(truncated, last 256KB)...\n" + data[-max_bytes:]
    return PlainTextResponse(data.decode("utf-8", errors="replace"))


@router.delete("/{job_id}", status_code=204)
async def delete_job(
    job_id: str,
    db: Database = Depends(get_db),
    redis: RedisClient = Depends(get_redis),
    config: AppConfig = Depends(get_config),
):
    _validate_job_id(job_id)
    job = await asyncio.to_thread(db.get_job, job_id)
    if not job:
        raise HTTPException(404, "job not found")
    # 单事务删 job：jobs 行 + FTS 索引 + 集合计数 + glossary 悬空 source,避免中途崩溃留孤儿。
    await asyncio.to_thread(db.delete_job_cascade, job_id, job.collection_id)
    job_dir = config.jobs_dir / job_id
    if job_dir.exists():
        await asyncio.to_thread(shutil.rmtree, job_dir)
    await redis.publish("job_command", {"action": "delete", "job_id": job_id})


@router.post("/{job_id}/retry")
async def retry_job(
    job_id: str,
    db: Database = Depends(get_db),
    redis: RedisClient = Depends(get_redis),
):
    _validate_job_id(job_id)
    job = await asyncio.to_thread(db.get_job, job_id)
    if not job:
        raise HTTPException(404, "job not found")
    if job.status != JobStatus.FAILED:
        raise HTTPException(400, "job is not failed")
    await redis.publish("job_command", {"action": "retry", "job_id": job_id})
    return {"job_id": job_id, "status": "processing"}


@router.post("/{job_id}/rerun")
async def rerun_job(
    job_id: str,
    req: RerunRequest,
    db: Database = Depends(get_db),
    redis: RedisClient = Depends(get_redis),
):
    _validate_job_id(job_id)
    job = await asyncio.to_thread(db.get_job, job_id)
    if not job:
        raise HTTPException(404, "job not found")
    await redis.publish("job_command", {
        "action": "rerun", "job_id": job_id, "from_step": req.from_step,
    })
    return {"job_id": job_id, "status": "processing", "from_step": req.from_step}


def _provider_available(name: str, cfg: dict) -> bool:
    """provider 是否可用:CLI 类型(claude 订阅)始终可用;其余看运行时环境是否有 {NAME}_API_KEY。
    只认环境变量,不信 config 里的 api_key(可能是未解析的 ${VAR} 占位串)。"""
    import os
    pc = (cfg.get("providers") or {}).get(name, {})
    if pc.get("type") == "cli":
        return True
    return bool(os.environ.get(f"{name.upper()}_API_KEY"))


@router.post("/{job_id}/rerun-smart")
async def rerun_smart(
    job_id: str,
    req: RerunSmartRequest,
    db: Database = Depends(get_db),
    redis: RedisClient = Depends(get_redis),
    storage: StorageBackend = Depends(get_storage),
    config: AppConfig = Depends(get_config),
):
    """用指定 provider 重跑智能笔记 + 评审,生成新版本(旧版本保留)。"""
    _validate_job_id(job_id)
    job = await asyncio.to_thread(db.get_job, job_id)
    if not job:
        raise HTTPException(404, "job not found")
    if not _provider_available(req.provider, config.providers):
        raise HTTPException(400, f"provider '{req.provider}' 不可用(未配置 API key)")
    # 把 provider 覆盖写进 job.json(智能/评审步会读),worker rerun 时 pull 到新 job.json。
    raw = await storage.read_file(job_id, "job.json")
    doc = json.loads(raw) if raw else {}
    doc.setdefault("ai_overrides", {})
    doc["ai_overrides"]["10_smart"] = req.provider
    doc["ai_overrides"]["11_review"] = req.provider
    await storage.write_file(job_id, "job.json",
                             json.dumps(doc, ensure_ascii=False, indent=2).encode("utf-8"))
    await redis.publish("job_command", {
        "action": "rerun", "job_id": job_id, "from_step": "10_smart",
    })
    return {"job_id": job_id, "status": "processing", "provider": req.provider}


@router.post("/{job_id}/resubmit")
async def resubmit_job(
    job_id: str,
    db: Database = Depends(get_db),
    redis: RedisClient = Depends(get_redis),
):
    _validate_job_id(job_id)
    job = await asyncio.to_thread(db.get_job, job_id)
    if not job:
        raise HTTPException(404, "job not found")
    await redis.publish("job_command", {"action": "resubmit", "job_id": job_id})
    return {"job_id": job_id, "status": "processing"}
