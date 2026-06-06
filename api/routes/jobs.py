"""任务管理路由。"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from fastapi.responses import PlainTextResponse

from shared.config import AppConfig
from shared.db import Database
from shared.models import Job, JobStatus, Step, StepStatus, generate_job_id
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
    StepResponse,
)

router = APIRouter(prefix="/api/jobs", tags=["jobs"], dependencies=[Depends(verify_token)])


def _validate_job_id(job_id: str) -> None:
    if ".." in job_id or "/" in job_id or "\x00" in job_id:
        raise HTTPException(400, "invalid job_id")


def _detect_content_type(url: str | None, filename: str | None = None) -> str:
    if filename:
        if filename.endswith(".pdf"):
            return "paper"
        if filename.endswith((".mp4", ".mkv", ".webm", ".flv")):
            return "video"
    if url:
        source = detect_source(url)
        if source == "arxiv":
            return "paper"
    return "video"


def _pipeline_for(content_type: str) -> str:
    return {"video": "video", "paper": "paper"}.get(content_type, "video")


def _now_iso() -> str:
    from datetime import datetime
    return datetime.now().isoformat()


@router.post("", status_code=201)
async def create_job(
    req: JobCreateRequest,
    db: Database = Depends(get_db),
    redis: RedisClient = Depends(get_redis),
    config: AppConfig = Depends(get_config),
):
    content_type = req.content_type or _detect_content_type(req.url)
    pipeline = _pipeline_for(content_type)
    source = detect_source(req.url) if req.url else "upload"

    job_id = generate_job_id()
    job_dir = config.jobs_dir / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "input").mkdir(exist_ok=True)

    (job_dir / "job.json").write_text(json.dumps({
        "id": job_id,
        "url": req.url,
        "source": source,
        "content_type": content_type,
        "domain": req.domain,
        "style_tags": req.style_tags,
        "created_at": _now_iso(),
    }, ensure_ascii=False, indent=2))

    job = Job(
        id=job_id,
        content_type=content_type,
        pipeline=pipeline,
        url=req.url,
        domain=req.domain,
        source=source,
        style_tags=req.style_tags,
        collection_id=req.collection_id,
    )
    await asyncio.to_thread(db.create_job, job)

    await redis.publish("job_command", {
        "action": "new_job",
        "job_id": job_id,
        "pipeline": pipeline,
    })

    return {"job_id": job_id, "content_type": content_type, "status": "pending", "created_at": job.created_at.isoformat()}


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
    job_id = generate_job_id()
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
    limit: int = 20,
    offset: int = 0,
    db: Database = Depends(get_db),
):
    total, jobs = await asyncio.to_thread(
        db.list_jobs, status=status, limit=limit, offset=offset,
    )
    return JobListResponse(
        total=total,
        items=[
            JobResponse(
                job_id=j.id, content_type=j.content_type, status=j.status.value,
                created_at=j.created_at.isoformat(), title=j.title,
                progress_pct=j.progress_pct, source=j.source, domain=j.domain,
            )
            for j in jobs
        ],
    )


@router.get("/{job_id}")
async def get_job(job_id: str, db: Database = Depends(get_db)):
    _validate_job_id(job_id)
    job = await asyncio.to_thread(db.get_job, job_id)
    if not job:
        raise HTTPException(404, "job not found")

    steps = await asyncio.to_thread(db.get_steps, job_id)
    return JobDetailResponse(
        job_id=job.id, content_type=job.content_type, status=job.status.value,
        created_at=job.created_at.isoformat(), title=job.title,
        progress_pct=job.progress_pct, source=job.source, domain=job.domain,
        meta=job.meta,
        steps=[
            StepResponse(
                name=s.name, status=s.status.value, duration_sec=s.duration_sec,
                meta=s.meta, error=s.error,
            )
            for s in steps
        ],
    )


@router.get("/{job_id}/steps/{step}/log")
async def get_step_log(
    job_id: str,
    step: str,
    storage: StorageBackend = Depends(get_storage),
):
    """返回某步骤的运行日志(尾部截断),供前端展开排错。经存储读,兼容本地/MinIO。"""
    _validate_job_id(job_id)
    if "/" in step or ".." in step or "\x00" in step:
        raise HTTPException(400, "invalid step")
    data = await storage.read_file(job_id, f"logs/{step}.log")
    if data is None:
        raise HTTPException(404, "log not found")
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
    await asyncio.to_thread(db.delete_job, job_id)
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
