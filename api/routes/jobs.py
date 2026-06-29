"""任务管理路由。"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File, Form
from fastapi.responses import PlainTextResponse

from shared.audit import audit
from shared.config import AppConfig
from shared.db import Database
from shared.ids import lineage_key_of as _lineage_key_of
from shared.models import Job, JobStatus, Step, StepStatus, derive_job_id
from shared.redis_client import RedisClient
from shared.source_detect import detect_source
from shared.step_base import def_digest_for, pipeline_digest_for
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
        if source in ("arxiv", "pdf"):   # arxiv 摘要页 / 直链 PDF(OSDI/usenix 等)→ 论文
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
    smart_note: bool | None = None,
    item_id: str | None = None, actor: str = "api",
    config: AppConfig | None = None,
) -> Job:
    """建 job 的核心流程(create_job 路由 + upload + 订阅同步共用)。返回 Job。
    upload=(ext, data):上传路径,把源文件经 storage 写入 input/source{ext}(兼容本地/MinIO)。"""
    style_tags = style_tags or []
    ctype = content_type or _detect_content_type(url)
    pipeline = _pipeline_for(ctype)
    source = detect_source(url) if url else "upload"
    # 投递开关:smart_note None=按类型默认(article 轻链路默认关,其余默认开)。
    # 存进 flags 随 job 落库 → scheduler 读 redis flags 求值 rules 的 if_flag(条件跳步)。
    resolved_smart = smart_note if smart_note is not None else (ctype != "article")
    flags = {"smart_note": bool(resolved_smart)}

    # 有意义的 id: jobs_{类别}_{inner}(bili=BV);撞已存在(同 BV 重投/上传随机撞库)加随机后缀。
    job_id = derive_job_id(url, ctype, source)
    if await asyncio.to_thread(db.get_job, job_id):
        job_id = f"{job_id}_{secrets.token_hex(3)}"
    job_doc = {
        "id": job_id, "url": url, "source": source, "content_type": ctype,
        "domain": domain, "style_tags": style_tags, "created_at": _now_iso(),
        "flags": flags,
    }
    # 白盒 Phase2:job 创建时(api 有 DB)解析该 pipeline+domain 的 prompt 覆盖(domain 优先于 global),
    # 写进 job.json 随 job 下发;worker(pure,无 DB)的 step_base 读取注入覆盖作 system prompt。
    overrides = await asyncio.to_thread(db.resolve_prompt_overrides, pipeline, domain)
    if overrides:
        job_doc["prompt_overrides"] = overrides
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
    # item_id:订阅来源去重键,落 meta 供删除时按 (collection_id, item_id) 精准清 ingested_items(彻底删除)。
    job_meta: dict = {"flags": flags}
    if item_id:
        job_meta["source_item_id"] = item_id
    # pipeline_digest:当前 pipeline 各步定义指纹聚合(供"重建过期"批量快查);config 缺省(如订阅同步)则留空,
    # is_job_expired 会回退逐 .done 比对,不影响正确性。
    pdigest = _pipeline_digest(config, pipeline)
    job = Job(
        id=job_id, content_type=ctype, pipeline=pipeline, url=url, title=title,
        domain=domain, source=source, style_tags=style_tags, collection_id=collection_id,
        meta=job_meta, pipeline_digest=pdigest,
    )
    await asyncio.to_thread(db.create_job, job)
    if collection_id:
        await asyncio.to_thread(db.increment_collection_count, collection_id, 1)
    await redis.publish("job_command", {
        "action": "new_job", "job_id": job_id, "pipeline": pipeline,
    })
    audit("job", job_id, "create", actor=actor,
          detail={"content_type": ctype, "source": source, "collection_id": collection_id})
    return job


def _pipeline_digest(config: AppConfig | None, pipeline: str) -> str | None:
    if config is None:
        return None
    try:
        return pipeline_digest_for(config.pipelines[pipeline]["steps"])
    except Exception:
        return None


async def is_job_expired(storage: StorageBackend, config: AppConfig, job: Job) -> dict:
    """job 是否"过期"= 其某步 .done 存档的 def_digest 与【当前】pipeline 该步 def_digest 不同。
    逐步读 .done(权威,覆盖含 P2c 前无 pipeline_digest 的旧 job);老 .done 缺 def_digest 键→保守判过期。
    返回 {expired, first_changed_step}。"""
    try:
        steps = config.pipelines[job.pipeline]["steps"]
    except Exception:
        return {"expired": False, "first_changed_step": None}
    for s in steps:
        name = s.get("name")
        raw = await storage.read_file(job.id, f".{name}.done")
        if raw is None:
            continue  # 该步未跑(无 .done),不算过期
        try:
            stored = json.loads(raw).get("def_digest")
        except Exception:
            stored = None
        if stored is None or stored != def_digest_for(s.get("version"), s.get("ai")):
            return {"expired": True, "first_changed_step": name}
    return {"expired": False, "first_changed_step": None}


async def create_job_snapshot(
    db: Database, redis: RedisClient, storage: StorageBackend,
    config: AppConfig, parent_job_id: str, actor: str = "api",
) -> Job:
    """从父 job fork 一个新快照(同 lineage、新时间戳 id):clone 父产物+.done 播种 → submit_job,
    worker should_run 指纹自然只重跑分叉步及下游;旧快照保留供 A/B。不走 rerun(from_step)(它 unlink
    本地 .done 在 MinIO 是 no-op);用 submit_job + 被播种的中心 .done。"""
    parent = await asyncio.to_thread(db.get_job, parent_job_id)
    if not parent:
        raise HTTPException(404, "job not found")
    new_id = derive_job_id(parent.url, parent.content_type, parent.source)  # 带时间戳
    if new_id == parent.id or await asyncio.to_thread(db.get_job, new_id):
        new_id = f"{new_id}_{secrets.token_hex(3)}"   # 极小概率撞;绝不复用父 id
    lineage = parent.lineage_key or _lineage_key_of(parent.id)
    await storage.clone(parent.id, new_id)            # 播种父产物 + .done
    raw = await storage.read_file(parent.id, "job.json")
    doc = {}
    if raw:
        try:
            doc = json.loads(raw)
        except Exception:
            doc = {}
    doc.update({"id": new_id, "created_at": _now_iso()})
    # 白盒 Phase2:重建快照也重解析 prompt 覆盖(拾取最新编辑;父 job.json 里的旧覆盖会被替换)。
    overrides = await asyncio.to_thread(db.resolve_prompt_overrides, parent.pipeline, parent.domain)
    if overrides:
        doc["prompt_overrides"] = overrides
    else:
        doc.pop("prompt_overrides", None)
    await storage.write_file(
        new_id, "job.json", json.dumps(doc, ensure_ascii=False, indent=2).encode("utf-8"),
    )
    job = Job(
        id=new_id, content_type=parent.content_type, pipeline=parent.pipeline,
        url=parent.url, title=parent.title, domain=parent.domain, source=parent.source,
        style_tags=parent.style_tags, collection_id=parent.collection_id, meta=parent.meta,
        lineage_key=lineage, is_current=True, parent_job_id=parent.id,
        pipeline_digest=_pipeline_digest(config, parent.pipeline),
    )
    await asyncio.to_thread(db.create_job, job)        # P2b create_job 自动 demote 同 lineage 旧版
    if parent.collection_id:
        await asyncio.to_thread(db.increment_collection_count, parent.collection_id, 1)
    await redis.publish(
        "job_command", {"action": "new_job", "job_id": new_id, "pipeline": parent.pipeline},
    )
    audit("job", new_id, "create", actor=actor,
          detail={"rebuilt_from": parent.id, "lineage_key": lineage})
    return job


@router.post("", status_code=201)
async def create_job(
    req: JobCreateRequest,
    db: Database = Depends(get_db),
    redis: RedisClient = Depends(get_redis),
    storage: StorageBackend = Depends(get_storage),
    config: AppConfig = Depends(get_config),
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
        smart_note=req.smart_note, config=config,
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
    config: AppConfig = Depends(get_config),
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
        upload=(ext, bytes(buf)), config=config,
    )
    return {"job_id": job.id, "content_type": job.content_type,
            "status": "pending", "created_at": job.created_at.isoformat()}


@router.get("")
async def list_jobs(
    status: str | None = None,
    collection_id: str | None = None,
    domain: str | None = None,
    source: str | None = None,
    uncategorized: bool = False,   # true=只列无所属集合的内容(侧栏「未归类」)
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0, le=2_147_483_647),  # int32 max,远低于 SQLite int64 溢出点;挡住超大 offset → 500
    db: Database = Depends(get_db),
):
    total, jobs = await asyncio.to_thread(
        db.list_jobs, status=status, collection_id=collection_id,
        limit=limit, offset=offset, domain=domain, source=source,
        uncategorized=uncategorized,
    )
    # 默认按 lineage 归组只返 current;附每条同源快照总数(>1 表示有历史版本可跳转)。
    counts = await asyncio.to_thread(
        db.lineage_counts, [j.lineage_key for j in jobs if j.lineage_key]
    )
    return JobListResponse(
        total=total,
        items=[
            JobResponse(
                job_id=j.id, content_type=j.content_type, status=j.status.value,
                created_at=j.created_at.isoformat(), title=j.title,
                progress_pct=j.progress_pct, source=j.source, domain=j.domain,
                collection_id=j.collection_id,
                versions=counts.get(j.lineage_key, 1) if j.lineage_key else 1,
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
    validate_path_segment(job_id, "job_id")
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
            published_at = md.get("published_at")  # 兜底;DB 值(scheduler 已同步)优先,见下
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
                    # 视频基本信息(01_download 经 ffprobe 写入):编码/帧率/码率。
                    "video_codec": md.get("video_codec"),
                    "audio_codec": md.get("audio_codec"),
                    "fps": md.get("fps"),
                    "bitrate_kbps": md.get("bitrate_kbps"),
                    "video_bitrate_kbps": md.get("video_bitrate_kbps"),
                }.items() if v is not None
            }
    except Exception:
        pass
    # 文章/论文:从 02 解析(parsed.json)透元信息进「元信息」tab。article 字数/标签/封面;paper 页数。
    if job.content_type in ("article", "paper"):
        try:
            raw = await storage.read_file(job_id, "intermediate/parsed.json")
            if raw:
                p = json.loads(raw.decode("utf-8"))
                # 通用(article + paper):作者 / 摘要 / 正文语言。
                if p.get("authors"):
                    media["authors"] = p["authors"]
                if p.get("abstract"):
                    media["abstract"] = p["abstract"]
                if p.get("lang"):
                    media["lang"] = p["lang"]
                if job.content_type == "article":
                    if p.get("word_count") is not None:
                        media["word_count"] = p["word_count"]
                    if p.get("tags"):
                        media["tags"] = p["tags"]
                    if p.get("image"):
                        media["image"] = p["image"]
                    if p.get("sitename"):           # 来源:网站名(SemiAnalysis / 华尔街见闻 / 域名)
                        media["sitename"] = p["sitename"]
                else:                                # paper
                    if p.get("pages") is not None:
                        media["pages"] = p["pages"]
                    if p.get("venue"):              # 来源:会议/期刊 + 年份(OSDI 2023 / arXiv)
                        media["venue"] = p["venue"]
        except Exception:
            pass
    # 产物路径(元信息"产物路径"):NAS 宿主绝对路径。job 产物实际落在对象存储/本地盘,
    # 其在 NAS 上的根由 JOB_ARTIFACT_HOST_ROOT 指定(MinIO 部署=<NAS>/minio/<bucket>;
    # 本地盘部署=<NAS>/jobs)。未配置则回退容器内 $DATA_DIR/jobs。列可见产物(隐藏点文件/job.json)。
    artifacts: list[str] = []
    try:
        host_root = os.environ.get("JOB_ARTIFACT_HOST_ROOT") or f"{os.environ.get('DATA_DIR', '/data')}/jobs"
        root = f"{host_root.rstrip('/')}/{job_id}"
        artifacts = sorted(
            f"{root}/{f}" for f in await storage.list_files(job_id)
            if not (f.rsplit("/", 1)[-1].startswith(".") or f == "job.json")
        )
    except Exception:
        pass
    # 本任务 AI 步用的 prompt 覆盖版本号快照:从 job.json.prompt_overrides[step].version 读
    # (1.1.5 起注入为 {content, version};旧 job 为纯字符串无版本 → 跳过)。供前端比对「本任务 vX vs 当前 vY」。
    prompt_versions: dict = {}
    try:
        raw = await storage.read_file(job_id, "job.json")
        if raw:
            jd = json.loads(raw.decode("utf-8"))
            for stp, val in (jd.get("prompt_overrides") or {}).items():
                if isinstance(val, dict) and val.get("version") is not None:
                    prompt_versions[stp] = val["version"]
    except Exception:
        pass
    return JobDetailResponse(
        job_id=job.id, content_type=job.content_type, status=job.status.value,
        created_at=job.created_at.isoformat(),
        updated_at=job.updated_at.isoformat() if job.updated_at else None,
        published_at=(job.published_at.isoformat() if job.published_at else published_at),
        media=media, artifacts=artifacts,
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
                worker_id=s.worker_id,
            )
            for s in steps
        ],
        prompt_versions=prompt_versions,
    )


@router.get("/{job_id}/versions")
async def job_versions(
    job_id: str,
    db: Database = Depends(get_db),
):
    """同一 lineage(同源内容)的所有快照,按时间倒序(详情页历史版本跳转)。
    每条:{job_id, created_at, is_current, status, title, pipeline_digest}。"""
    validate_path_segment(job_id, "job_id")
    jobs = await asyncio.to_thread(db.lineage_versions, job_id)
    if not jobs:
        raise HTTPException(404, "job not found")
    return {
        "versions": [
            {
                "job_id": j.id, "created_at": j.created_at.isoformat(),
                "is_current": j.is_current, "status": j.status.value, "title": j.title,
                "pipeline_digest": j.pipeline_digest,
            }
            for j in jobs
        ]
    }


@router.get("/{job_id}/concepts")
async def job_concepts(
    job_id: str,
    db: Database = Depends(get_db),
):
    """该内容命中的概念(occurrences 含本 job),含本 job 命中的出现位置 job_occurrences。"""
    validate_path_segment(job_id, "job_id")
    job = await asyncio.to_thread(db.get_job, job_id)
    if not job:
        raise HTTPException(404, "job not found")
    return await asyncio.to_thread(db.glossary_for_job, job_id, job.domain)


@router.get("/{job_id}/usage")
async def job_usage(
    job_id: str,
    db: Database = Depends(get_db),
):
    """该 job 的逐次 AI 调用明细(按步展示 in/out/cache/命中率/cost/耗时/轮数/worker)。
    cost 对 claude-cli 订阅是「等价 API 成本」,前端按 provider==claude-cli 标「(等价)」。"""
    validate_path_segment(job_id, "job_id")
    return {"usage": await asyncio.to_thread(db.list_usage_by_job, job_id)}


@router.get("/{job_id}/steps/{step}/log")
async def get_step_log(
    job_id: str,
    step: str,
    raw: int = 0,
    storage: StorageBackend = Depends(get_storage),
):
    """返回某步骤的运行日志,供前端展开排错。经存储读,兼容本地/MinIO。
    默认尾部截断 256KB;raw=1 返回完整日志(供下载)。"""
    validate_path_segment(job_id, "job_id")
    validate_path_segment(step, "step")
    data = await storage.read_file(job_id, f"logs/{step}.log")
    if data is None:
        raise HTTPException(404, "log not found")
    if not raw:
        max_bytes = 256 * 1024
        if len(data) > max_bytes:
            data = b"...(truncated, last 256KB)...\n" + data[-max_bytes:]
    return PlainTextResponse(data.decode("utf-8", errors="replace"))


@router.get("/{job_id}/ai-logs")
async def job_ai_logs(
    job_id: str,
    step: str | None = None,
    storage: StorageBackend = Depends(get_storage),
):
    """该 job 各 AI 步的【完整 AI 审计日志】(prompt 白盒化)。
    读 output/ai_logs/{step}.jsonl —— 每次 LLM 调用一条(含路由/尝试链/prompt 渲染/输出/用量/raw),
    按 job_id 归成一条 trace;给 step 时只返回该步。经 storage 读,兼容本地/MinIO。"""
    validate_path_segment(job_id, "job_id")
    if step is not None:
        validate_path_segment(step, "step")
    try:
        files = await storage.list_files(job_id)
    except Exception:
        files = []
    targets = [f for f in files if f.startswith("output/ai_logs/") and f.endswith(".jsonl")]
    if step is not None:
        targets = [f for f in targets if f == f"output/ai_logs/{step}.jsonl"]
    steps: list[dict] = []
    for rel in sorted(targets):
        data = await storage.read_file(job_id, rel)
        if not data:
            continue
        calls = []
        for line in data.decode("utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                calls.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        steps.append({"step": rel.rsplit("/", 1)[-1][: -len(".jsonl")], "calls": calls})
    return {"job_id": job_id, "steps": steps}


@router.delete("/{job_id}", status_code=204)
async def delete_job(
    job_id: str,
    db: Database = Depends(get_db),
    redis: RedisClient = Depends(get_redis),
    storage: StorageBackend = Depends(get_storage),
):
    validate_path_segment(job_id, "job_id")
    job = await asyncio.to_thread(db.get_job, job_id)
    if not job:
        raise HTTPException(404, "job not found")
    await _delete_job_full(db, redis, storage, job, actor="api")


async def _delete_job_full(
    db: Database, redis: RedisClient, storage: StorageBackend, job: Job, actor: str = "api",
) -> None:
    """精准级联删一个 job —— 单 job 删除 与 集合 purge 共用,顺序保证【DB 行最后删 + 每步幂等】:
    任一步崩溃则 job 仍在 DB → 可原样重删补齐(不依赖周期 GC)。
    ① 清 redis 队列残留(queue:{pool} + queue:enqueued,补 G1)+ 7 个编排 hash + active 集合;
    ② publish 让 scheduler 取消在途延迟重试(进程内 asyncio,只能 scheduler 端做);
    ③ 删产物(LocalStorage 删目录 / RemoteStorage 删 {job_id}/ 前缀,审计 I-H1);
    ④ 最后删 DB(jobs 行 + FTS + ai_usage + 集合计数 + glossary 出现 + 订阅 ingested_items);
    ⑤ 审计。running job:不主动回收槽,worker 推回结果经 cas_step_status 见 steps hash 已删而 CAS 失败被丢弃。"""
    job_id = job.id
    item_id = (job.meta or {}).get("source_item_id")
    removed = await redis.remove_job_tasks(job_id)          # ① 队列 ZSET + queue:enqueued
    await redis.cleanup_job(job_id)                         #    7 个 job:{id}* 编排 hash
    await redis.remove_active_job(job_id)                   #    SREM active_jobs
    await redis.publish("job_command", {"action": "delete", "job_id": job_id})  # ② 取消在途重试
    await storage.delete(job_id)                            # ③ 产物
    await asyncio.to_thread(db.delete_job_cascade, job_id, job.collection_id, item_id)  # ④ DB 最后
    # 删的是 current → 把同 lineage 剩余最新一版提为 current(否则该内容在列表消失)。
    if job.is_current and job.lineage_key:
        await asyncio.to_thread(db.promote_lineage_current, job.lineage_key)
    audit("job", job_id, "delete", actor=actor, detail={                               # ⑤
        "queue_tasks_removed": removed, "collection_id": job.collection_id,
        "purged_ingested": bool(item_id),
    })


@router.post("/retry-failed")
async def retry_all_failed(
    collection_id: str | None = Query(None, description="仅重试该集合的失败 job;不传=全局所有失败"),
    db: Database = Depends(get_db),
    redis: RedisClient = Depends(get_redis),
):
    """批量重试 failed job(各自从首个失败步重跑,自动重置下游)。返回发起数。
    传 collection_id 则限定该集合(集合详情页"重试本集合失败");不传=全局所有失败。
    注:缺凭证类失败(如无 cookie 的 YouTube 下载)修好根因前会再失败。"""
    # 空串(?collection_id=)归一为 None:否则 list_jobs 的 `elif collection_id:` 对空串为假 →
    # 集合过滤落空 → 静默退化为「全局重试所有 failed」,与「限定该集合」语义相悖且误触批量重发。
    if collection_id is not None:
        collection_id = collection_id.strip() or None
    if collection_id is not None:
        validate_path_segment(collection_id, "collection_id")
    _, jobs = await asyncio.to_thread(
        db.list_jobs, status="failed", collection_id=collection_id, limit=100000
    )
    for j in jobs:
        await redis.publish("job_command", {"action": "retry", "job_id": j.id})
    return {"retried": len(jobs)}


@router.post("/{job_id}/retry")
async def retry_job(
    job_id: str,
    db: Database = Depends(get_db),
    redis: RedisClient = Depends(get_redis),
):
    validate_path_segment(job_id, "job_id")
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
    validate_path_segment(job_id, "job_id")
    job = await asyncio.to_thread(db.get_job, job_id)
    if not job:
        raise HTTPException(404, "job not found")
    await redis.publish("job_command", {
        "action": "rerun", "job_id": job_id, "from_step": req.from_step,
    })
    return {"job_id": job_id, "status": "processing", "from_step": req.from_step}


@router.post("/{job_id}/rebuild")
async def rebuild_job(
    job_id: str,
    db: Database = Depends(get_db),
    redis: RedisClient = Depends(get_redis),
    storage: StorageBackend = Depends(get_storage),
    config: AppConfig = Depends(get_config),
):
    """重建为【新快照】(fork 父 job:播种产物+.done,只重跑分叉步及下游;旧版保留 A/B)。
    返回新 job_id;新版自动成为该 lineage 的 current。"""
    validate_path_segment(job_id, "job_id")
    job = await create_job_snapshot(db, redis, storage, config, job_id)
    return {"job_id": job.id, "parent_job_id": job.parent_job_id,
            "lineage_key": job.lineage_key, "status": "pending"}


@router.post("/rebuild-stale")
async def rebuild_stale(
    db: Database = Depends(get_db),
    redis: RedisClient = Depends(get_redis),
    storage: StorageBackend = Depends(get_storage),
    config: AppConfig = Depends(get_config),
):
    """批量重建【所有过期 current job】(其某步定义指纹 def_digest 与当前 pipeline 不符)为新快照。
    仿 retry-failed:逐个判过期 → create_job_snapshot。返回重建清单。"""
    _, jobs = await asyncio.to_thread(db.list_jobs, None, None, 10000, 0, None, None, False, True)
    rebuilt = []
    for job in jobs:
        exp = await is_job_expired(storage, config, job)
        if exp["expired"]:
            new = await create_job_snapshot(db, redis, storage, config, job.id)
            rebuilt.append({"parent_job_id": job.id, "job_id": new.id,
                            "from_step": exp["first_changed_step"]})
    return {"rebuilt": len(rebuilt), "items": rebuilt}


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
    validate_path_segment(job_id, "job_id")
    job = await asyncio.to_thread(db.get_job, job_id)
    if not job:
        raise HTTPException(404, "job not found")
    if not _provider_available(req.provider, config.providers):
        raise HTTPException(400, f"provider '{req.provider}' 不可用(未配置 API key)")
    # 把 provider 覆盖写进 job.json(智能/评审步会读),worker rerun 时 pull 到新 job.json。
    raw = await storage.read_file(job_id, "job.json")
    doc = json.loads(raw) if raw else {}
    doc.setdefault("ai_overrides", {})
    doc["ai_overrides"]["11_smart"] = req.provider
    doc["ai_overrides"]["12_review"] = req.provider
    await storage.write_file(job_id, "job.json",
                             json.dumps(doc, ensure_ascii=False, indent=2).encode("utf-8"))
    await redis.publish("job_command", {
        "action": "rerun", "job_id": job_id, "from_step": "11_smart",
    })
    return {"job_id": job_id, "status": "processing", "provider": req.provider}


@router.post("/{job_id}/resubmit")
async def resubmit_job(
    job_id: str,
    db: Database = Depends(get_db),
    redis: RedisClient = Depends(get_redis),
):
    """从头重提交整个 job。注:前端当前无入口调用(前端用 retry/rerun),保留供后台/CLI 重提。"""
    validate_path_segment(job_id, "job_id")
    job = await asyncio.to_thread(db.get_job, job_id)
    if not job:
        raise HTTPException(404, "job not found")
    await redis.publish("job_command", {"action": "resubmit", "job_id": job_id})
    return {"job_id": job_id, "status": "processing"}
