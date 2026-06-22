"""任务管理路由。"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File, Form
from fastapi.responses import PlainTextResponse

from shared.config import AppConfig
from shared.db import Database
from shared.models import Job, JobStatus, Step, StepStatus, derive_job_id
from shared.redis_client import RedisClient
from shared.source_detect import detect_source
from shared.storage import CREDENTIAL_REL, StorageBackend

from api.deps import get_config, get_db, get_redis, get_storage, validate_path_segment, verify_token
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
    validate_path_segment(job_id, "job_id")


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
    upload: tuple[str, bytes] | None = None,
) -> Job:
    """建 job 的核心流程(create_job 路由 + upload + 订阅同步共用)。返回 Job。
    upload=(ext, data):上传路径,把源文件经 storage 写入 input/source{ext}(兼容本地/MinIO)。"""
    style_tags = style_tags or []
    ctype = content_type or _detect_content_type(url)
    pipeline = _pipeline_for(ctype)
    source = detect_source(url) if url else "upload"

    # 有意义的 id: jobs_{类别}_{inner}(bili=BV);撞已存在(同 BV 重投/上传随机撞库)加随机后缀。
    job_id = derive_job_id(url, ctype, source)
    if await asyncio.to_thread(db.get_job, job_id):
        job_id = f"{job_id}_{secrets.token_hex(3)}"
    job_doc = {
        "id": job_id, "url": url, "source": source, "content_type": ctype,
        "domain": domain, "style_tags": style_tags, "created_at": _now_iso(),
    }
    await storage.write_file(
        job_id, "job.json",
        json.dumps(job_doc, ensure_ascii=False, indent=2).encode("utf-8"),
    )
    # 上传源文件经 storage 落库(本地/MinIO 一致),远端 worker 才能 pull 到 input/source.*
    # (此前 upload_job 直写 API 容器本地盘,MinIO 部署下 worker 拉不到源文件)。
    if upload is not None:
        ext, data = upload
        await storage.write_file(job_id, f"input/source{ext}", data)
    # SESSDATA 不进 job.json(那是会下发到远端 worker 的通用文档);写入本机侧载凭证文件,
    # 由 storage/runner 保证绝不入中心存储、绝不下发远端(见 shared/storage.is_credential_file)。
    if source == "bilibili":
        sessdata = await asyncio.to_thread(_bili_sessdata, db)
        if sessdata:
            await storage.write_file(
                job_id, CREDENTIAL_REL,
                json.dumps({"sessdata": sessdata}, ensure_ascii=False).encode("utf-8"),
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
    storage: StorageBackend = Depends(get_storage),
):
    # 注:url 接受 http(s) 链接或裸 BV 号(detect_source 解析),故不强校验 http(s) 前缀;
    # 契约的 invalid_url 语义改由 docs/03-contracts.md 对齐(见 C12 处置)。
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
    collection_id: str | None = Form(None),
    title: str | None = Form(None),
    db: Database = Depends(get_db),
    redis: RedisClient = Depends(get_redis),
    storage: StorageBackend = Depends(get_storage),
):
    content_type = _detect_content_type(None, file.filename)
    try:
        tags = json.loads(style_tags)
    except json.JSONDecodeError:
        raise HTTPException(400, "invalid style_tags JSON")
    # 与 URL 投递路径一致:校验 collection_id 存在,支持归集合 + title。
    if collection_id:
        if not await asyncio.to_thread(db.get_collection, collection_id):
            raise HTTPException(400, "collection_id not found")

    MAX_UPLOAD_SIZE = 2 * 1024 * 1024 * 1024  # 2GB
    # 流式累加并超限早退;做完大小校验后统一经 create_job_core → storage 落库(兼容 MinIO)。
    # NOTE:storage.write_file 入参为 bytes,大文件整体驻留内存(上界 2GB);流式 put 为后续优化。
    buf = bytearray()
    while chunk := await file.read(1024 * 1024):
        buf.extend(chunk)
        if len(buf) > MAX_UPLOAD_SIZE:
            raise HTTPException(413, f"file too large (max {MAX_UPLOAD_SIZE})")

    ext = Path(file.filename).suffix if file.filename else ".mp4"
    job = await create_job_core(
        db, redis, storage, url=None, content_type=content_type,
        domain=domain, style_tags=tags, collection_id=collection_id, title=title,
        upload=(ext, bytes(buf)),
    )
    return {"job_id": job.id, "content_type": job.content_type,
            "status": "pending", "created_at": job.created_at.isoformat()}


@router.get("")
async def list_jobs(
    status: str | None = None,
    collection_id: str | None = None,
    domain: str | None = None,
    source: str | None = None,
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0, le=2_147_483_647),  # int32 max,远低于 SQLite int64 溢出点;挡住超大 offset → 500
    db: Database = Depends(get_db),
):
    total, jobs = await asyncio.to_thread(
        db.list_jobs, status=status, collection_id=collection_id,
        limit=limit, offset=offset, domain=domain, source=source,
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


@router.get("/facets")
async def job_facets(db: Database = Depends(get_db)):
    """全量 jobs 按 source / domain / status 的计数,供前端过滤 chip。
    注:须在 /{job_id} 之前注册,否则被路径参数捕获为 job_id='facets'。"""
    return await asyncio.to_thread(db.job_facets)


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
    # collection_id → 集合名(供元信息显示);无归属或集合已删则 None。
    collection_name = None
    if job.collection_id:
        coll = await asyncio.to_thread(db.get_collection, job.collection_id)
        collection_name = coll.name if coll else None
    # 步骤中文名取自 pipelines.yaml(单一事实源),按本 job 的 pipeline 查表。
    labels = {
        s["name"]: s.get("label")
        for s in config.pipelines.get(job.pipeline, {}).get("steps", [])
    }
    # 源媒体元信息(发布时间/分辨率/时长/大小/字幕)由 01_download 写入 metadata.json;读不到则空。
    published_at = None
    media: dict = {}
    try:
        raw = await storage.read_file(job_id, "input/metadata.json")
        if raw:
            md = json.loads(raw.decode("utf-8"))
            published_at = md.get("published_at")
            # 仅透出展示相关字段(元信息标签页用);分辨率优先 resolution,无则由 width×height 拼。
            res = md.get("resolution")
            if not res and md.get("width") and md.get("height"):
                res = f"{md['width']}x{md['height']}"
            media = {
                k: v for k, v in {
                    "resolution": res,
                    "width": md.get("width"), "height": md.get("height"),
                    "duration_sec": md.get("duration_sec"),
                    "file_size_bytes": md.get("file_size_bytes"),
                    "file_size_mb": md.get("file_size_mb"),
                    "has_subtitle": md.get("has_subtitle"),
                    "has_danmaku": md.get("has_danmaku"),
                }.items() if v is not None
            }
    except Exception:
        pass
    # 文章:字数(视频是分辨率)——取自 02_parse 写入 parsed.json 的 word_count。
    if job.content_type == "article":
        try:
            raw = await storage.read_file(job_id, "intermediate/parsed.json")
            if raw:
                wc = json.loads(raw.decode("utf-8")).get("word_count")
                if wc is not None:
                    media["word_count"] = wc
        except Exception:
            pass
    # 产物路径(元信息"产物路径"):绝对路径(job 数据根 = $DATA_DIR/jobs/<job_id>);
    # 列可见产物文件(隐藏内部点文件/job.json)。
    artifacts: list[str] = []
    try:
        root = f"{os.environ.get('DATA_DIR', '/data')}/jobs/{job_id}"
        artifacts = sorted(
            f"{root}/{f}" for f in await storage.list_files(job_id)
            if not (f.rsplit("/", 1)[-1].startswith(".") or f == "job.json")
        )
    except Exception:
        pass
    return JobDetailResponse(
        job_id=job.id, content_type=job.content_type, status=job.status.value,
        created_at=job.created_at.isoformat(),
        updated_at=job.updated_at.isoformat() if job.updated_at else None,
        published_at=published_at, media=media, artifacts=artifacts,
        title=job.title, url=job.url,
        progress_pct=job.progress_pct, source=job.source, domain=job.domain,
        collection_id=job.collection_id, collection_name=collection_name,
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


@router.get("/{job_id}/concepts")
async def job_concepts(
    job_id: str,
    db: Database = Depends(get_db),
):
    """该内容命中的概念(occurrences 含本 job),含本 job 命中的出现位置 job_occurrences。"""
    _validate_job_id(job_id)
    job = await asyncio.to_thread(db.get_job, job_id)
    if not job:
        raise HTTPException(404, "job not found")
    return await asyncio.to_thread(db.glossary_for_job, job_id, job.domain)


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
    validate_path_segment(step, "step")
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
    """从头重提交整个 job。注:前端当前无入口调用(前端用 retry/rerun),保留供后台/CLI 重提。"""
    _validate_job_id(job_id)
    job = await asyncio.to_thread(db.get_job, job_id)
    if not job:
        raise HTTPException(404, "job not found")
    await redis.publish("job_command", {"action": "resubmit", "job_id": job_id})
    return {"job_id": job_id, "status": "processing"}
