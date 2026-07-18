"""任务管理路由。"""

from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import json
import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import AsyncIterable, AsyncIterator

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File, Form
from fastapi.responses import PlainTextResponse

from shared.audit import audit
from shared.config import AppConfig
from shared.ai_routing import (
    READ_TOOL_TAG,
    pipeline_ai_roles,
    provider_is_configured,
    provider_required_tags,
    step_required_capability_tags,
    worker_satisfies_requirements,
)
from shared.db import Database
from shared.ids import lineage_key_of as _lineage_key_of
from shared.models import Job, JobPart, JobStatus, Step, StepStatus, derive_job_id
from shared.redis_client import RedisClient
from shared.source_detect import detect_source
from shared.source_library import (
    SourceLibrary,
    SourceReferenceError,
    build_source_ref,
    normalize_source_digest,
    parse_source_ref,
)
from shared.source_registry import (
    SourceRegistryError,
    content_type_for_filename,
    default_content_type,
    pipeline_for_content_type,
    resolve_job_route,
)
from shared.step_base import def_digest_for, pipeline_digest_for
from shared.step_scope import execution_step_key, part_id_from_scope, part_scope, stable_part_id
from shared.storage import (
    ArtifactTooLarge,
    CREDENTIAL_REL,
    INITIALIZATION_MARKER_REL,
    StorageBackend,
    read_file_bounded,
)

from api.deps import get_config, get_db, get_redis, get_storage, validate_path_segment, verify_token
from api.business_admission import (
    JobAdmissionRoute,
    ensure_job_workers,
    job_admission_guard,
)
from api.schemas import (
    ContentType,
    DocumentKind,
    JobCollectionUpdateRequest,
    JobCollectionUpdateResponse,
    JobCreateRequest,
    JobDetailResponse,
    JobListResponse,
    JobResponse,
    GlossaryTermResponse,
    RerunRequest,
    RerunSmartRequest,
    RebuildRequest,
    StepResponse,
)
from api.wire_schemas import (
    API_ERROR_RESPONSES,
    AiLogsResponse,
    JobConceptResponse,
    JobCreatedResponse,
    JobFacetsResponse,
    JobRebuildResponse,
    JobRerunResponse,
    JobRerunSmartResponse,
    JobStatusResponse,
    JobUsageResponse,
    JobsRebuiltResponse,
    JobsRetriedResponse,
    LineageVersionsResponse,
)

router = APIRouter(
    prefix="/api/jobs", tags=["jobs"], dependencies=[Depends(verify_token)],
    responses=API_ERROR_RESPONSES, route_class=JobAdmissionRoute,
)

MAX_UPLOAD_SIZE = 2 * 1024 * 1024 * 1024
UPLOAD_CHUNK_SIZE = 1024 * 1024
INITIALIZATION_MARKER_SCHEMA = "flori-job-initialization"
INITIALIZATION_MARKER_VERSION = 1
INITIALIZATION_STALE_AFTER_SEC = 24 * 60 * 60
INITIALIZATION_HEARTBEAT_SEC = 30
_LOG = structlog.get_logger()

# 同模块第二个路由:/api/providers(不能挂在 /api/jobs 下,否则被 /{job_id} 截胡)。
providers_router = APIRouter(prefix="/api/providers", tags=["providers"],
                            dependencies=[Depends(verify_token)])


@providers_router.get("")
async def list_providers(
    config: AppConfig = Depends(get_config),
    redis: RedisClient = Depends(get_redis),
):
    """列出已配置且有在线匹配 worker 的 AI provider。"""
    workers = await _provider_workers(redis)
    out = []
    for name, pc in (config.providers.get("providers") or {}).items():
        if name == "local":
            continue  # 本地 ollama 默认不展示
        out.append({
            "name": name,
            "type": pc.get("type", ""),
            "available": _provider_available(
                name, config.providers, workers, [("ai", [])],
            ),
            "label": "CLI" if pc.get("type") in {"cli", "codex_cli"} else "API",
        })
    return {"providers": out}


def _detect_content_type(url: str | None, filename: str | None = None) -> str | None:
    if filename:
        return content_type_for_filename(filename)
    if url:
        return default_content_type(detect_source(url))
    return None


def _pipeline_for(content_type: str) -> str | None:
    return pipeline_for_content_type(content_type)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _initialization_marker(
    job_id: str,
    source_rel: str,
    staging_token: str,
    *,
    defer_submit: bool,
) -> dict:
    now = _now_iso()
    return {
        "schema": INITIALIZATION_MARKER_SCHEMA,
        "version": INITIALIZATION_MARKER_VERSION,
        "job_id": job_id,
        "source_rel": source_rel,
        "staging_token": staging_token,
        "owner_id": secrets.token_hex(12),
        "created_at": now,
        "updated_at": now,
        "defer_submit": defer_submit,
        "event_published": False,
    }


def _marker_bytes(marker: dict) -> bytes:
    return json.dumps(
        marker, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def _parse_initialization_marker(
    raw: bytes, expected_job_id: str, now: datetime,
) -> tuple[dict, datetime]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("marker is not valid UTF-8 JSON") from exc
    fields = {
        "schema", "version", "job_id", "source_rel", "staging_token",
        "owner_id", "created_at", "updated_at", "defer_submit",
        "event_published",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("marker fields do not match schema")
    if value["schema"] != INITIALIZATION_MARKER_SCHEMA:
        raise ValueError("marker schema is unsupported")
    if type(value["version"]) is not int or value["version"] != INITIALIZATION_MARKER_VERSION:
        raise ValueError("marker version is unsupported")
    if value["job_id"] != expected_job_id or not re.fullmatch(
        r"[A-Za-z0-9_-]{1,200}", value["job_id"],
    ):
        raise ValueError("marker job_id is invalid")
    if not isinstance(value["source_rel"], str) or not re.fullmatch(
        r"input/source[^/\\\x00]{0,32}", value["source_rel"],
    ):
        raise ValueError("marker source_rel is invalid")
    if not isinstance(value["staging_token"], str) or not re.fullmatch(
        r"[A-Za-z0-9_-]{1,128}", value["staging_token"],
    ):
        raise ValueError("marker staging_token is invalid")
    if not isinstance(value["owner_id"], str) or not re.fullmatch(
        r"[A-Za-z0-9_-]{1,128}", value["owner_id"],
    ):
        raise ValueError("marker owner_id is invalid")
    if type(value["defer_submit"]) is not bool or type(value["event_published"]) is not bool:
        raise ValueError("marker flags are invalid")
    try:
        created = datetime.fromisoformat(value["created_at"])
        updated = datetime.fromisoformat(value["updated_at"])
    except (TypeError, ValueError) as exc:
        raise ValueError("marker timestamps are invalid") from exc
    if created.tzinfo is None or updated.tzinfo is None:
        raise ValueError("marker timestamps must be timezone-aware")
    created = created.astimezone(timezone.utc)
    updated = updated.astimezone(timezone.utc)
    if updated < created or updated > now + timedelta(minutes=5):
        raise ValueError("marker timestamp ordering is invalid")
    return value, updated


async def _write_initialization_marker(
    storage: StorageBackend, job_id: str, marker: dict,
) -> None:
    await storage.write_file(job_id, INITIALIZATION_MARKER_REL, _marker_bytes(marker))


async def _remove_initialization_marker(
    storage: StorageBackend, job_id: str,
) -> None:
    try:
        await storage.delete_file(job_id, INITIALIZATION_MARKER_REL)
    except Exception as exc:
        _LOG.error(
            "job_initialization_marker_cleanup_failed",
            job_id=job_id,
            error=type(exc).__name__,
            detail=str(exc),
        )


async def reconcile_incomplete_job_uploads(
    db: Database,
    redis: RedisClient,
    storage: StorageBackend,
    *,
    now: datetime | None = None,
    stale_after_sec: int = INITIALIZATION_STALE_AFTER_SEC,
) -> dict:
    """恢复 API 进程中断的上传初始化;损坏 marker 一律保留现场。"""
    if type(stale_after_sec) is not int or stale_after_sec <= 0:
        raise ValueError("stale_after_sec must be a positive integer")
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    current = current.astimezone(timezone.utc)
    cutoff = current.timestamp() - stale_after_sec
    report: dict = {
        "status": "ok",
        "active": 0,
        "deleted_orphans": 0,
        "recovered_db_jobs": 0,
        "staging_removed": 0,
        "errors": [],
    }
    active_tokens: set[tuple[str, str]] = set()
    protected_job_ids: set[str] = set()
    try:
        job_ids = await storage.list_initialization_markers()
    except Exception as exc:
        report["status"] = "partial"
        report["errors"].append({
            "job_id": None,
            "stage": "list_markers",
            "error": f"{type(exc).__name__}: {exc}",
        })
        _LOG.error("job_initialization_recovery_partial", report=report)
        return report

    for job_id in job_ids:
        try:
            raw = await storage.read_file(job_id, INITIALIZATION_MARKER_REL)
            if raw is None:
                continue
            marker, updated = _parse_initialization_marker(raw, job_id, current)
        except Exception as exc:
            protected_job_ids.add(job_id)
            report["errors"].append({
                "job_id": job_id,
                "stage": "parse_marker",
                "error": f"{type(exc).__name__}: {exc}",
            })
            continue

        token = marker["staging_token"]
        if updated.timestamp() >= cutoff:
            active_tokens.add((job_id, token))
            report["active"] += 1
            continue

        try:
            job = await asyncio.to_thread(db.get_job, job_id)
            if job is None:
                await storage.delete(job_id)
                report["deleted_orphans"] += 1
                continue
            if not marker["defer_submit"] and not marker["event_published"]:
                await redis.append_lifecycle_event("job_command", {
                    "action": "new_job",
                    "job_id": job_id,
                    "pipeline": job.pipeline,
                })
                marker["event_published"] = True
                marker["updated_at"] = _now_iso()
                await _write_initialization_marker(storage, job_id, marker)
            await storage.delete_file(job_id, INITIALIZATION_MARKER_REL)
            report["recovered_db_jobs"] += 1
        except Exception as exc:
            protected_job_ids.add(job_id)
            report["errors"].append({
                "job_id": job_id,
                "stage": "recover_job",
                "error": f"{type(exc).__name__}: {exc}",
            })

    try:
        report["staging_removed"] = await storage.cleanup_stale_staging(
            active_tokens=active_tokens,
            protected_job_ids=protected_job_ids,
            stale_before_epoch=cutoff,
        )
    except Exception as exc:
        report["errors"].append({
            "job_id": None,
            "stage": "cleanup_staging",
            "error": f"{type(exc).__name__}: {exc}",
        })
    if report["errors"]:
        report["status"] = "partial"
        _LOG.error("job_initialization_recovery_partial", report=report)
    else:
        _LOG.info("job_initialization_recovery_complete", report=report)
    return report


def _bili_sessdata(db: Database) -> str | None:
    """从凭证表取已登录 B站的 SESSDATA,未登录/解析失败返回 None。"""
    raw = db.get_credential("bili_cookies")
    if not raw:
        return None
    try:
        return json.loads(raw).get("sessdata") or None
    except (json.JSONDecodeError, ValueError):
        return None


async def _cleanup_failed_job(
    storage: StorageBackend, job_id: str, original: BaseException,
) -> None:
    """初始创建失败时清产物;清理故障只加诊断,不覆盖原始失败。"""
    try:
        if isinstance(original, asyncio.CancelledError):
            await storage.delete(job_id, defer_if_busy=True)
        else:
            await storage.delete(job_id)
    except Exception as cleanup_error:
        original.add_note(
            "job artifact cleanup failed: "
            f"{type(cleanup_error).__name__}: {cleanup_error}"
        )
        _LOG.error(
            "job_initialization_cleanup_failed",
            job_id=job_id,
            original_error=type(original).__name__,
            cleanup_error=type(cleanup_error).__name__,
            cleanup_detail=str(cleanup_error),
        )


async def _rollback_created_job(
    db: Database,
    redis: RedisClient,
    storage: StorageBackend,
    job: Job,
    original: BaseException,
    *,
    collection_incremented: bool,
) -> None:
    """投递确认前失败时回滚 DB 与产物;补偿失败不覆盖原始错误。"""
    db_rolled_back = False
    try:
        await asyncio.to_thread(
            db.delete_job_cascade,
            job.id,
            job.collection_id if collection_incremented else None,
            (job.meta or {}).get("source_item_id"),
        )
        db_rolled_back = True
    except Exception as cleanup_error:
        original.add_note(
            "job database rollback failed: "
            f"{type(cleanup_error).__name__}: {cleanup_error}"
        )
        _LOG.error(
            "job_initialization_db_rollback_failed",
            job_id=job.id,
            original_error=type(original).__name__,
            cleanup_error=type(cleanup_error).__name__,
            cleanup_detail=str(cleanup_error),
        )
    if db_rolled_back:
        for method_name in (
            "remove_job_tasks", "cleanup_job", "remove_active_job",
        ):
            try:
                await getattr(redis, method_name)(job.id)
            except Exception as cleanup_error:
                original.add_note(
                    f"job redis rollback {method_name} failed: "
                    f"{type(cleanup_error).__name__}: {cleanup_error}"
                )
                _LOG.error(
                    "job_initialization_redis_rollback_failed",
                    job_id=job.id,
                    operation=method_name,
                    original_error=type(original).__name__,
                    cleanup_error=type(cleanup_error).__name__,
                    cleanup_detail=str(cleanup_error),
                )
        await _cleanup_failed_job(storage, job.id, original)


async def create_job_core(
    db: Database, redis: RedisClient, storage: StorageBackend,
    url: str | None, content_type: str | None = None,
    domain: str = "general", style_tags: list[str] | None = None,
    collection_id: str | None = None, title: str | None = None,
    upload: tuple[str, bytes | AsyncIterable[bytes]] | None = None,
    smart_note: bool | None = None,
    mechanical_only: bool = False,
    document_kind: str | None = None,
    item_id: str | None = None, actor: str = "api",
    source_position: int | None = None,
    config: AppConfig | None = None,
    defer_submit: bool = False,
    parts: list[dict] | None = None,
) -> Job:
    """建 job 的核心流程(create_job 路由 + upload + 订阅同步共用)。返回 Job。
    upload=(ext, data/stream):源文件先原子发布,其余初始文件失败时整项清理。"""
    style_tags = style_tags or []
    if parts and upload is not None:
        raise HTTPException(422, "video parts do not support upload")
    normalized_part_inputs: list[dict] | None = None
    if parts:
        normalized_part_inputs = []
        library: SourceLibrary | None = None
        for item in parts:
            source_doc = item.get("source")
            if source_doc is None:
                normalized_part_inputs.append({
                    "title": item.get("title"),
                    "url": item.get("url"),
                    "source_type": detect_source(item.get("url") or ""),
                })
                continue
            try:
                if not isinstance(source_doc, dict):
                    raise SourceReferenceError("invalid source object")
                source_ref = build_source_ref(
                    source_doc.get("root_id"), source_doc.get("relative_path"),
                )
                source_digest = normalize_source_digest(source_doc.get("sha256"))
                size_bytes = source_doc.get("size_bytes")
                library = library or SourceLibrary.from_env()
                await asyncio.to_thread(
                    library.verify, source_ref, source_digest, size_bytes,
                )
            except SourceReferenceError as exc:
                raise HTTPException(422, str(exc)) from exc
            normalized_part_inputs.append({
                "title": item.get("title"),
                "url": None,
                "source_type": "nas_source",
                "source_ref": source_ref,
                "source_digest": source_digest,
                "size_bytes": size_bytes,
            })
        parts = normalized_part_inputs
    route_url = parts[0].get("url") if parts else url
    source = (
        "upload" if upload is not None
        else parts[0]["source_type"] if parts else detect_source(route_url or "")
    )
    requested_type = getattr(content_type, "value", content_type)
    requested_kind = getattr(document_kind, "value", document_kind)
    try:
        route = resolve_job_route(
            source, requested_type or _detect_content_type(route_url),
            document_kind=requested_kind,
            allow_internal=(actor == "subscription"),
        )
    except SourceRegistryError as exc:
        raise HTTPException(422, f"unsupported_source: {exc}") from exc
    ctype = route.content_type
    resolved_kind = route.document_kind or ""
    pipeline = route.pipeline
    if not pipeline or (config is not None and pipeline not in config.pipelines):
        raise HTTPException(422, f"source_pipeline_unavailable: {ctype}")
    # 投递开关:smart_note None=按类型默认(article 轻链路默认关,其余默认开)。
    # 存进 flags 随 job 落库 → scheduler 读 redis flags 求值 rules 的 if_flag(条件跳步)。
    resolved_smart = smart_note if smart_note is not None else resolved_kind != "article"
    flags = {
        "smart_note": bool(resolved_smart),
        "mechanical_only": bool(mechanical_only),
    }
    if ctype == "video" and not parts and actor == "subscription" and url:
        # 订阅枚举仍以单个SourceItem交付;进入领域层后立即归一为单Part,
        # 不给公开API保留旧的顶层url视频语法。
        parts = [{
            "url": url,
            "title": None,
            "source_type": detect_source(url),
        }]

    # 有意义的 id: jobs_{类别}_{inner}(bili=BV);撞已存在(同 BV 重投/上传随机撞库)加随机后缀。
    manifest_digest = None
    creation_fingerprint = None
    if ctype == "video":
        if not parts:
            raise HTTPException(422, "video jobs require parts[]")
        normalized_parts = []
        for index, item in enumerate(parts, start=1):
            manifest_item = {
                "part_index": index,
                "title": item.get("title"),
            }
            if item.get("source_ref"):
                manifest_item.update({
                    "source_ref": item["source_ref"],
                    "source_digest": item["source_digest"],
                    "size_bytes": item["size_bytes"],
                })
            else:
                manifest_item["url"] = item["url"]
            normalized_parts.append(manifest_item)
        manifest_bytes = json.dumps(
            normalized_parts, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        manifest_digest = f"sha256:{hashlib.sha256(manifest_bytes).hexdigest()}"
        creation_payload = {
            "manifest": normalized_parts,
            "title": title,
            "domain": domain,
            "style_tags": sorted(set(style_tags)),
            "collection_id": collection_id,
            "smart_note": bool(resolved_smart),
            "mechanical_only": bool(mechanical_only),
        }
        creation_bytes = json.dumps(
            creation_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        creation_fingerprint = f"sha256:{hashlib.sha256(creation_bytes).hexdigest()}"
        job_id = f"jobs_video_{creation_fingerprint.split(':', 1)[1][:20]}"
        existing = await asyncio.to_thread(db.get_job, job_id)
        if (
            existing is not None
            and existing.source_digest == manifest_digest
            and (existing.meta or {}).get("creation_fingerprint") == creation_fingerprint
        ):
            return existing
        if existing is not None:
            raise HTTPException(409, "video creation fingerprint conflicts with existing job")
        url = None
    else:
        if parts:
            raise HTTPException(422, "parts[] is only valid for video jobs")
        job_id = derive_job_id(url, ctype, source)
        if await asyncio.to_thread(db.get_job, job_id):
            job_id = f"{job_id}_{secrets.token_hex(3)}"
    job_doc = {
        "id": job_id, "url": url, "source": source, "content_type": ctype,
        "document_kind": resolved_kind or None,
        "source_profile": route.source_profile,
        "domain": domain, "style_tags": style_tags, "created_at": _now_iso(),
        "flags": flags,
    }
    db_parts: list[JobPart] | None = None
    if parts:
        db_parts = []
        job_doc["parts"] = []
        job_doc["creation_fingerprint"] = creation_fingerprint
        for index, item in enumerate(parts, start=1):
            part_id = stable_part_id(job_id, index)
            part_source = item["source_type"]
            try:
                part_route = resolve_job_route(
                    part_source,
                    "video",
                    allow_internal=(actor == "subscription"),
                )
            except SourceRegistryError as exc:
                raise HTTPException(
                    422,
                    f"unsupported video part P{index:02d}: {exc}",
                ) from exc
            part_doc = {
                "job_id": job_id,
                "part_id": part_id,
                "part_index": index,
                "title": item.get("title"),
                "url": item.get("url"),
                "source": part_route.source,
                "content_type": "video",
                "domain": domain,
                "style_tags": style_tags,
                "flags": flags,
            }
            if item.get("source_ref"):
                part_doc.update({
                    "source_ref": item["source_ref"],
                    "source_digest": item["source_digest"],
                    "size_bytes": item["size_bytes"],
                })
            job_doc["parts"].append(part_doc)
            db_parts.append(JobPart(
                id=part_id,
                job_id=job_id,
                part_index=index,
                title=item.get("title"),
                source_url=item.get("url"),
                source_ref=item.get("source_ref"),
                source_digest=item.get("source_digest"),
                size_bytes=item.get("size_bytes"),
                meta={"source": part_route.source},
            ))
    # prompt 白盒:job 创建时由 api 解析该 pipeline+domain 的 prompt 覆盖(domain 优先于 global),
    # 写进 job.json 随 job 下发;worker 是 pure 进程无 DB,其 step_base 读取注入覆盖作 system prompt。
    overrides = await asyncio.to_thread(
        db.resolve_prompt_overrides, pipeline, domain, resolved_kind or None,
    )
    if overrides:
        job_doc["prompt_overrides"] = overrides
        for part_doc in job_doc.get("parts", []):
            part_doc["prompt_overrides"] = overrides
    initialization_marker: dict | None = None
    try:
        # 源文件先走不可见暂存并原子发布。job.json 随后失败时删除整个 job 前缀,
        # DB 和生命周期事件均尚未创建,不会留下可调度的半成品。
        if upload is not None:
            ext, data = upload
            rel_path = f"input/source{ext}"
            staging_token = secrets.token_hex(16)
            initialization_marker = _initialization_marker(
                job_id,
                rel_path,
                staging_token,
                defer_submit=defer_submit,
            )
            await _write_initialization_marker(storage, job_id, initialization_marker)

            async def _source_chunks() -> AsyncIterator[bytes]:
                last_heartbeat = time.monotonic()
                if isinstance(data, bytes):
                    source: AsyncIterable[bytes] = _single_chunk(data)
                else:
                    source = data
                async for chunk in source:
                    current = time.monotonic()
                    if current - last_heartbeat >= INITIALIZATION_HEARTBEAT_SEC:
                        initialization_marker["updated_at"] = _now_iso()
                        await _write_initialization_marker(
                            storage, job_id, initialization_marker,
                        )
                        last_heartbeat = current
                    yield chunk

            await storage.write_stream(
                job_id,
                rel_path,
                _source_chunks(),
                max_bytes=MAX_UPLOAD_SIZE,
                staging_token=staging_token,
            )
        await storage.write_file(
            job_id, "job.json",
            json.dumps(job_doc, ensure_ascii=False, indent=2).encode("utf-8"),
        )
        for part_doc in job_doc.get("parts", []):
            await storage.write_file(
                job_id,
                f"parts/{part_doc['part_id']}/job.json",
                json.dumps(part_doc, ensure_ascii=False, indent=2).encode("utf-8"),
            )
        # 站点会话凭证不进 job.json(那是会下发到远端 worker 的通用文档);写入本机侧载凭证文件,
        # 由 storage/runner 保证绝不入中心存储、绝不下发远端(见 shared/storage.is_credential_file)。
        if source == "bilibili":
            sessdata = await asyncio.to_thread(_bili_sessdata, db)
            if sessdata:
                await storage.write_file(
                    job_id, CREDENTIAL_REL,
                    json.dumps({"sessdata": sessdata}, ensure_ascii=False).encode("utf-8"),
                )
    except (Exception, asyncio.CancelledError) as exc:
        await _cleanup_failed_job(storage, job_id, exc)
        raise
    # item_id:订阅来源去重键,落 meta 供删除时按 (collection_id, item_id) 精准清 ingested_items(彻底删除)。
    job_meta: dict = {"flags": flags}
    if creation_fingerprint is not None:
        job_meta["creation_fingerprint"] = creation_fingerprint
    if item_id:
        job_meta["source_item_id"] = item_id
        job_meta["source_present"] = True
    if source_position is not None:
        job_meta["source_position"] = source_position
    # pipeline_digest:当前 pipeline 各步定义指纹聚合(供"重建过期"批量快查);config 缺省(如订阅同步)则留空,
    # is_job_expired 会回退逐 .done 比对,不影响正确性。
    pdigest = _pipeline_digest(config, pipeline)
    job = Job(
        id=job_id, content_type=ctype, pipeline=pipeline,
        document_kind=resolved_kind, url=url, title=title,
        domain=domain, source=source, style_tags=style_tags, collection_id=collection_id,
        meta=job_meta, pipeline_digest=pdigest, source_digest=manifest_digest,
    )
    try:
        await asyncio.to_thread(db.create_job, job, db_parts)
    except (Exception, asyncio.CancelledError) as exc:
        await _cleanup_failed_job(storage, job_id, exc)
        raise
    collection_incremented = False
    try:
        if collection_id:
            await asyncio.to_thread(db.increment_collection_count, collection_id, 1)
            collection_incremented = True
        if not defer_submit:
            await redis.append_lifecycle_event("job_command", {
                "action": "new_job", "job_id": job_id, "pipeline": pipeline,
            })
            if initialization_marker is not None:
                initialization_marker["event_published"] = True
                initialization_marker["updated_at"] = _now_iso()
                try:
                    await _write_initialization_marker(
                        storage, job_id, initialization_marker,
                    )
                except Exception as exc:
                    _LOG.error(
                        "job_initialization_marker_update_failed",
                        job_id=job_id,
                        error=type(exc).__name__,
                        detail=str(exc),
                    )
    except (Exception, asyncio.CancelledError) as exc:
        await _rollback_created_job(
            db,
            redis,
            storage,
            job,
            exc,
            collection_incremented=collection_incremented,
        )
        raise
    if initialization_marker is not None:
        await _remove_initialization_marker(storage, job_id)
    # defer_submit(book 章序):job 落库但不触发调度,由 scheduler 的 book 投递器在
    # 前章终态时按 created_at 顺序 submit(shared/book_chain.next_chapter_job)。
    audit("job", job_id, "create", actor=actor,
          detail={"content_type": ctype, "document_kind": resolved_kind or None,
                  "source": source, "collection_id": collection_id})
    return job


async def _single_chunk(data: bytes) -> AsyncIterator[bytes]:
    yield data


def _pipeline_digest(config: AppConfig | None, pipeline: str) -> str | None:
    if config is None:
        return None
    try:
        return pipeline_digest_for(config.pipelines[pipeline]["steps"])
    except Exception:
        return None


async def is_job_expired(storage: StorageBackend, config: AppConfig, job: Job) -> dict:
    """job 是否"过期"= 其某步 .done 存档的 def_digest 与当前 pipeline 该步 def_digest 不同。
    Part 步逐 Part 读隔离目录下的 .done;老 .done 缺 def_digest 键 → 保守判过期。
    返回 {expired, first_changed_step}。"""
    try:
        steps = config.pipelines[job.pipeline]["steps"]
    except Exception:
        return {"expired": False, "first_changed_step": None}
    part_ids: list[str] = []
    if job.content_type == "video":
        raw_job = await storage.read_file(job.id, "job.json")
        try:
            job_doc = json.loads(raw_job) if raw_job is not None else {}
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
            job_doc = {}
        if isinstance(job_doc, dict):
            part_ids = [
                str(item["part_id"])
                for item in job_doc.get("parts", [])
                if isinstance(item, dict) and item.get("part_id")
            ]
    for s in steps:
        name = s.get("name")
        markers = (
            [f"parts/{part_id}/.{name}.done" for part_id in part_ids]
            if s.get("scope", "job") == "part" else [f".{name}.done"]
        )
        for marker in markers:
            raw = await storage.read_file(job.id, marker)
            if raw is None:
                continue  # 该步未跑(无 .done),不算过期
            try:
                stored = json.loads(raw).get("def_digest")
            except Exception:
                stored = None
            if stored is None or stored != def_digest_for(
                s.get("version"), s.get("ai"),
            ):
                return {"expired": True, "first_changed_step": name}
    return {"expired": False, "first_changed_step": None}


async def _create_job_snapshot_once(
    db: Database, redis: RedisClient, storage: StorageBackend,
    config: AppConfig, parent_job_id: str, actor: str = "api",
    *, mechanical_only: bool | None = None, smart_note: bool | None = None,
    from_step: str | None = None,
    reset_roots: list[str] | None = None,
    new_id_override: str | None = None,
    rebuild_request: dict | None = None,
    reserved: bool = False,
) -> Job:
    """从父 job fork 一个新快照(同 lineage、新时间戳 id):clone 父产物+.done 播种 → submit_job,
    worker should_run 指纹自然只重跑分叉步及下游;旧快照保留供 A/B。
    不走 rerun(from_step):它 unlink 本地 .done,对 MinIO 是 no-op;改用 submit_job + 被播种的中心 .done。"""
    parent = await asyncio.to_thread(db.get_job, parent_job_id)
    if not parent:
        raise HTTPException(404, "job not found")
    parent_parts = await asyncio.to_thread(db.get_parts, parent_job_id)
    try:
        route = resolve_job_route(
            parent.source or detect_source(parent.url or ""),
            parent.content_type,
            document_kind=parent.document_kind or None,
            allow_internal=True,
        )
    except SourceRegistryError as exc:
        raise HTTPException(409, f"parent job route is no longer supported: {exc}") from exc
    pipeline_steps = config.pipelines.get(route.pipeline, {}).get("steps", [])
    by_name = {
        step.get("name"): step for step in pipeline_steps
        if isinstance(step, dict) and isinstance(step.get("name"), str)
    }
    requested_roots = ([from_step] if from_step is not None else []) + list(reset_roots or [])
    if any(root not in by_name for root in requested_roots):
        raise HTTPException(422, "from_step is not part of the job pipeline")

    reset_steps: set[str] = set(requested_roots)
    if reset_steps:
        changed = True
        while changed:
            changed = False
            for name, step in by_name.items():
                if name in reset_steps:
                    continue
                dependencies = [
                    *step.get("depends_on", []),
                    *step.get("fan_in", []),
                ]
                if any(dep in reset_steps for dep in dependencies):
                    reset_steps.add(name)
                    changed = True
    source_url = parent_parts[0].source_url if parent_parts else parent.url
    new_id = new_id_override or derive_job_id(
        source_url, route.content_type, route.source,
    )
    if new_id_override is None and (
        new_id == parent.id or await asyncio.to_thread(db.get_job, new_id)
    ):
        new_id = f"{new_id}_{secrets.token_hex(3)}"   # 极小概率撞;绝不复用父 id
    lineage = parent.lineage_key or _lineage_key_of(parent.id)
    target_parts = [
        JobPart(
            id=part.id,
            job_id=new_id,
            part_index=part.part_index,
            title=part.title,
            source_url=part.source_url,
            source_ref=part.source_ref,
            source_digest=part.source_digest,
            size_bytes=part.size_bytes,
            duration_ms=part.duration_ms,
            meta=dict(part.meta),
        )
        for part in parent_parts
    ]
    flags = dict((parent.meta or {}).get("flags") or {})
    if mechanical_only is not None:
        flags["mechanical_only"] = mechanical_only
    if smart_note is not None:
        flags["smart_note"] = smart_note
    preserved_steps: list[Step] = []
    try:
        if requested_roots:
            parent_steps = {
                (step.scope_key, step.name): step
                for step in await asyncio.to_thread(db.get_steps, parent.id)
            }
            expected_nodes: list[tuple[str, str, dict]] = []
            for name, cfg in by_name.items():
                if name in reset_steps:
                    continue
                if cfg.get("scope", "job") == "part":
                    expected_nodes.extend(
                        (part_scope(part.id), name, cfg) for part in target_parts
                    )
                else:
                    expected_nodes.append(("job", name, cfg))
            for scope_key, name, cfg in expected_nodes:
                source_step = parent_steps.get((scope_key, name))
                if source_step is None or source_step.status not in {
                    StepStatus.DONE, StepStatus.SKIPPED,
                }:
                    raise HTTPException(
                        409,
                        f"cannot preserve non-terminal upstream step: "
                        f"{scope_key}/{name}",
                    )
                part_id = part_id_from_scope(scope_key)
                marker = (
                    f"parts/{part_id}/.{name}.done"
                    if part_id is not None else f".{name}.done"
                )
                if (
                    source_step.status == StepStatus.DONE
                    and await storage.read_file(parent.id, marker) is None
                ):
                    raise HTTPException(
                        409,
                        f"cannot preserve upstream step without done marker: "
                        f"{scope_key}/{name}",
                    )
                preserved_steps.append(Step(
                    job_id=new_id,
                    name=name,
                    scope_key=scope_key,
                    status=source_step.status,
                    pool=cfg["pool"],
                    input_hash=source_step.input_hash,
                    worker_id=source_step.worker_id,
                    started_at=source_step.started_at,
                    finished_at=source_step.finished_at,
                    duration_sec=source_step.duration_sec,
                    meta=dict(source_step.meta),
                    error=source_step.error,
                    retries=source_step.retries,
                ))
            if preserved_steps and not reserved:
                raise HTTPException(
                    409,
                    "partial snapshot rebuild requires an idempotency reservation",
                )
        await storage.clone(parent.id, new_id)            # 播种父产物 + .done
        raw = await storage.read_file(parent.id, "job.json")
        doc = {}
        if raw:
            try:
                doc = json.loads(raw)
            except Exception:
                doc = {}
        if not isinstance(doc, dict):
            doc = {}
        doc.pop("pipeline", None)
        doc.update({
            "id": new_id,
            "url": None if parent_parts else parent.url,
            "source": route.source,
            "content_type": route.content_type,
            "document_kind": route.document_kind,
            "source_profile": route.source_profile,
            "domain": parent.domain,
            "style_tags": parent.style_tags,
            "created_at": _now_iso(),
            "flags": flags,
        })
        if parent_parts:
            part_docs = []
            existing_part_docs = {
                str(item.get("part_id")): dict(item)
                for item in doc.get("parts", [])
                if isinstance(item, dict) and item.get("part_id")
            }
            for part in target_parts:
                part_doc = existing_part_docs.get(part.id, {})
                part_doc.update({
                    "job_id": new_id,
                    "part_id": part.id,
                    "part_index": part.part_index,
                    "title": part.title,
                    "url": part.source_url,
                    "source": str((part.meta or {}).get("source") or ""),
                    "source_ref": part.source_ref,
                    "source_digest": part.source_digest,
                    "size_bytes": part.size_bytes,
                    "content_type": "video",
                    "domain": parent.domain,
                    "style_tags": parent.style_tags,
                    "flags": flags,
                })
                part_docs.append(part_doc)
            doc["parts"] = part_docs
        if rebuild_request is not None:
            doc["rebuild_request"] = rebuild_request
        # prompt 白盒:重建快照也重解析 prompt 覆盖,拾取最新编辑;父 job.json 里的旧覆盖会被替换。
        overrides = await asyncio.to_thread(
            db.resolve_prompt_overrides,
            route.pipeline,
            parent.domain,
            route.document_kind,
        )
        if overrides:
            doc["prompt_overrides"] = overrides
        else:
            doc.pop("prompt_overrides", None)
        await storage.write_file(
            new_id, "job.json",
            json.dumps(doc, ensure_ascii=False, indent=2).encode("utf-8"),
        )
        for part_doc in doc.get("parts", []):
            await storage.write_file(
                new_id,
                f"parts/{part_doc['part_id']}/job.json",
                json.dumps(part_doc, ensure_ascii=False, indent=2).encode("utf-8"),
            )
        cloned_files = await storage.list_files(new_id)
        reset_targets: list[tuple[str, str]] = []
        for step_name in reset_steps:
            cfg = by_name[step_name]
            if cfg.get("scope", "job") == "part":
                reset_targets.extend(
                    (f"parts/{part.id}/", step_name) for part in target_parts
                )
            else:
                reset_targets.append(("", step_name))
        for prefix, step_name in reset_targets:
            await storage.delete_file(new_id, f"{prefix}.{step_name}.done")
            output_patterns = {
                pattern for pattern in by_name[step_name].get("outputs", [])
                if isinstance(pattern, str)
            }
            for rel_path in cloned_files:
                if prefix and not rel_path.startswith(prefix):
                    continue
                candidate = rel_path[len(prefix):] if prefix else rel_path
                if any(fnmatch.fnmatch(candidate, pattern) for pattern in output_patterns):
                    await storage.delete_file(new_id, rel_path)
        # reservation 行先承载上游终态,再与 target current 状态一并发布。否则 new_job
        # 初始化会把全部步骤置 waiting,使 from_step 之前的下载/转写再次执行。
        for step in preserved_steps:
            await asyncio.to_thread(db.upsert_step, step)
    except (Exception, asyncio.CancelledError):
        await storage.delete(new_id)
        if reserved:
            await asyncio.to_thread(db.delete_job_cascade, new_id, None, None)
        raise
    meta = dict(parent.meta or {})
    meta["flags"] = flags
    if rebuild_request is not None:
        meta["rebuild_request"] = rebuild_request
    job = Job(
        id=new_id, content_type=route.content_type, pipeline=route.pipeline,
        document_kind=route.document_kind or "",
        url=None if parent_parts else parent.url,
        title=parent.title, domain=parent.domain, source=route.source,
        style_tags=parent.style_tags, collection_id=parent.collection_id, meta=meta,
        lineage_key=lineage, is_current=True, parent_job_id=parent.id,
        pipeline_digest=_pipeline_digest(config, route.pipeline),
        source_digest=parent.source_digest,
    )
    if reserved:
        ready_meta = dict(meta)
        ready_record = dict(ready_meta.get("rebuild_request") or {})
        ready_record["phase"] = "ready"
        ready_record["heartbeat_at"] = _now_iso()
        ready_meta["rebuild_request"] = ready_record
        owner_token = ready_record.get("owner_token")
        if not isinstance(owner_token, str) or not await asyncio.to_thread(
            db._update_rebuild_reservation, new_id, owner_token, ready_meta,
            status=JobStatus.PENDING, is_current=True,
            collection_id=parent.collection_id,
        ):
            raise RuntimeError("rebuild reservation ownership was lost before promotion")
        job.meta = ready_meta
        job.status = JobStatus.PENDING
        job.is_current = True
    else:
        try:
            await asyncio.to_thread(
                db.create_job, job, target_parts or None,
            )    # create_job 自动 demote 同 lineage 旧版
        except (Exception, asyncio.CancelledError):
            # to_thread 取消时 SQL 可能已提交;有 DB 行就保留完整 target,由幂等重放续作。
            if not await asyncio.to_thread(db.get_job, new_id):
                await storage.delete(new_id)
            raise
    if parent.collection_id:
        await asyncio.to_thread(db._reconcile_collection_count, parent.collection_id)
    await redis.append_lifecycle_event(
        "job_command", {"action": "new_job", "job_id": new_id, "pipeline": route.pipeline},
    )
    if rebuild_request is not None:
        await _mark_rebuild_event_published(db, storage, job)
    audit("job", new_id, "create", actor=actor,
          detail={"rebuilt_from": parent.id, "lineage_key": lineage})
    return job


def _assert_rebuild_request_matches(job: Job, fingerprint: str) -> Job:
    record = (job.meta or {}).get("rebuild_request")
    if isinstance(record, dict) and record.get("fingerprint") == fingerprint:
        return job
    raise HTTPException(409, "idempotency_key was already used with different rebuild parameters")


async def _mark_rebuild_event_published(
    db: Database, storage: StorageBackend, job: Job,
) -> None:
    fresh = await asyncio.to_thread(db.get_job, job.id)
    if not fresh:
        raise RuntimeError("rebuild snapshot disappeared before event checkpoint")
    meta = dict(fresh.meta or {})
    record = dict(meta.get("rebuild_request") or {})
    record["event_published"] = True
    meta["rebuild_request"] = record
    raw = await storage.read_file(job.id, "job.json")
    try:
        doc = json.loads(raw) if raw is not None else {}
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError) as exc:
        raise RuntimeError("rebuild snapshot job.json is invalid") from exc
    if not isinstance(doc, dict):
        raise RuntimeError("rebuild snapshot job.json must be an object")
    doc["rebuild_request"] = record
    await storage.write_file(
        job.id, "job.json",
        json.dumps(doc, ensure_ascii=False, indent=2).encode("utf-8"),
    )
    await asyncio.to_thread(db.update_job, job.id, meta=meta)
    job.meta = meta


async def _replay_existing_snapshot(
    db: Database, redis: RedisClient, storage: StorageBackend,
    job: Job, fingerprint: str,
) -> Job:
    matched = _assert_rebuild_request_matches(job, fingerprint)
    if matched.collection_id:
        await asyncio.to_thread(db._reconcile_collection_count, matched.collection_id)
    record = (matched.meta or {}).get("rebuild_request") or {}
    if record.get("event_published") is not True:
        await redis.append_lifecycle_event(
            "job_command", {
                "action": "new_job", "job_id": matched.id, "pipeline": matched.pipeline,
            },
        )
        await _mark_rebuild_event_published(db, storage, matched)
    return matched


async def create_job_snapshot(
    db: Database, redis: RedisClient, storage: StorageBackend,
    config: AppConfig, parent_job_id: str, actor: str = "api",
    *, mechanical_only: bool | None = None, smart_note: bool | None = None,
    from_step: str | None = None,
    reset_roots: list[str] | None = None,
    idempotency_key: str | None = None,
) -> Job:
    """创建重建快照;幂等请求先持久化非 current reservation,克隆完成后才原子发布。"""
    if idempotency_key is None:
        return await _create_job_snapshot_once(
            db, redis, storage, config, parent_job_id, actor,
            mechanical_only=mechanical_only, smart_note=smart_note, from_step=from_step,
            reset_roots=reset_roots,
        )

    parent = await asyncio.to_thread(db.get_job, parent_job_id)
    if not parent:
        raise HTTPException(404, "job not found")
    parent_parts = await asyncio.to_thread(db.get_parts, parent_job_id)
    try:
        route = resolve_job_route(
            parent.source or detect_source(parent.url or ""),
            parent.content_type,
            document_kind=parent.document_kind or None,
            allow_internal=True,
        )
    except SourceRegistryError as exc:
        raise HTTPException(409, f"parent job route is no longer supported: {exc}") from exc
    step_names = {
        step.get("name")
        for step in config.pipelines.get(route.pipeline, {}).get("steps", [])
        if isinstance(step, dict)
    }
    requested_roots = ([from_step] if from_step is not None else []) + list(reset_roots or [])
    if any(root not in step_names for root in requested_roots):
        raise HTTPException(422, "from_step is not part of the job pipeline")

    payload = {
        "parent_job_id": parent_job_id,
        "mechanical_only": mechanical_only,
        "smart_note": smart_note,
        "from_step": from_step,
        "reset_roots": sorted(reset_roots or []),
    }
    fingerprint = "sha256:" + hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    target_id = "jr_" + hashlib.sha256(
        f"{parent_job_id}\0{idempotency_key}".encode("utf-8")
    ).hexdigest()[:24]
    request_record: dict = {
        "idempotency_key": idempotency_key,
        "fingerprint": fingerprint,
        "event_published": False,
        **payload,
    }
    action = f"rebuild:{hashlib.sha256(idempotency_key.encode()).hexdigest()[:16]}"
    clone_owner: str | None = None
    for _ in range(12000):
        existing = await asyncio.to_thread(db.get_job, target_id)
        if existing:
            matched = _assert_rebuild_request_matches(existing, fingerprint)
            record = (matched.meta or {}).get("rebuild_request") or {}
            phase = record.get("phase")
            if phase == "ready":
                token = secrets.token_hex(16)
                if await redis.acquire_job_control_lock(parent_job_id, action, token, ttl_sec=30):
                    try:
                        fresh = await asyncio.to_thread(db.get_job, target_id)
                        if fresh:
                            return await _replay_existing_snapshot(
                                db, redis, storage, fresh, fingerprint,
                            )
                    finally:
                        await redis.release_job_control_lock(parent_job_id, action, token)
                await asyncio.sleep(0.05)
                continue
            heartbeat_raw = record.get("heartbeat_at")
            try:
                heartbeat = datetime.fromisoformat(heartbeat_raw)
            except (TypeError, ValueError):
                heartbeat = None
            fresh_heartbeat = heartbeat is not None and (
                datetime.now(timezone.utc) - heartbeat
            ) < timedelta(seconds=45)
            if phase == "cloning" and fresh_heartbeat:
                await asyncio.sleep(0.05)
                continue
            raise HTTPException(
                409,
                "stale rebuild reservation requires controlled cleanup before retry",
            )

        token = secrets.token_hex(16)
        if not await redis.acquire_job_control_lock(parent_job_id, action, token, ttl_sec=30):
            await asyncio.sleep(0.05)
            continue
        try:
            existing = await asyncio.to_thread(db.get_job, target_id)
            if existing:
                _assert_rebuild_request_matches(existing, fingerprint)
                raise HTTPException(
                    409,
                    "rebuild reservation appeared while acquiring ownership; retry",
                )
            clone_owner = secrets.token_hex(16)
            claimed_record = {
                **request_record,
                "phase": "cloning",
                "owner_token": clone_owner,
                "heartbeat_at": _now_iso(),
            }
            flags = dict((parent.meta or {}).get("flags") or {})
            if mechanical_only is not None:
                flags["mechanical_only"] = mechanical_only
            if smart_note is not None:
                flags["smart_note"] = smart_note
            reservation_meta = dict(parent.meta or {})
            reservation_meta["flags"] = flags
            reservation_meta["rebuild_request"] = claimed_record
            reservation_parts = [
                JobPart(
                    id=part.id,
                    job_id=target_id,
                    part_index=part.part_index,
                    title=part.title,
                    source_url=part.source_url,
                    source_ref=part.source_ref,
                    source_digest=part.source_digest,
                    size_bytes=part.size_bytes,
                    duration_ms=part.duration_ms,
                    meta=dict(part.meta),
                )
                for part in parent_parts
            ]
            reservation = Job(
                id=target_id, content_type=route.content_type, pipeline=route.pipeline,
                document_kind=route.document_kind or "",
                url=None if parent_parts else parent.url,
                title=parent.title, domain=parent.domain, source=route.source,
                style_tags=parent.style_tags, collection_id=None,
                meta=reservation_meta, lineage_key=parent.lineage_key or _lineage_key_of(parent.id),
                is_current=False, parent_job_id=parent.id,
                pipeline_digest=_pipeline_digest(config, route.pipeline),
                source_digest=parent.source_digest,
                status=JobStatus.PROCESSING,
            )
            await asyncio.to_thread(
                db.create_job, reservation, reservation_parts or None,
            )
        finally:
            await redis.release_job_control_lock(parent_job_id, action, token)
        break
    if clone_owner is None:
        raise HTTPException(409, "rebuild with this idempotency_key is already in progress")

    stop_heartbeat = asyncio.Event()

    async def _heartbeat() -> None:
        while not stop_heartbeat.is_set():
            try:
                await asyncio.wait_for(stop_heartbeat.wait(), timeout=10)
                return
            except asyncio.TimeoutError:
                if not await asyncio.to_thread(
                    db._heartbeat_rebuild_reservation,
                    target_id, clone_owner, _now_iso(),
                ):
                    return

    heartbeat_task = asyncio.create_task(_heartbeat())
    try:
        existing = await asyncio.to_thread(db.get_job, target_id)
        if not existing:
            raise RuntimeError("rebuild reservation disappeared before clone")
        claimed_record = dict((existing.meta or {}).get("rebuild_request") or {})
        return await _create_job_snapshot_once(
            db, redis, storage, config, parent_job_id, actor,
            mechanical_only=mechanical_only, smart_note=smart_note, from_step=from_step,
            reset_roots=reset_roots,
            new_id_override=target_id, rebuild_request=claimed_record,
            reserved=True,
        )
    finally:
        stop_heartbeat.set()
        await asyncio.gather(heartbeat_task, return_exceptions=True)


@router.post("", status_code=201, response_model=JobCreatedResponse)
@job_admission_guard("create")
async def create_job(
    req: JobCreateRequest,
    db: Database = Depends(get_db),
    redis: RedisClient = Depends(get_redis),
    storage: StorageBackend = Depends(get_storage),
    config: AppConfig = Depends(get_config),
):
    # 注:url 接受 http(s) 链接或裸 BV 号(detect_source 解析),故不强校验 http(s) 前缀;
    # invalid_url 语义以 docs/03-contracts.md 为准。
    first_part = req.parts[0] if req.parts else None
    first_url = first_part.url if first_part else req.url
    source = "nas_source" if first_part and first_part.source is not None else detect_source(first_url or "")
    try:
        route = resolve_job_route(
            source,
            getattr(req.content_type, "value", req.content_type),
            document_kind=getattr(req.document_kind, "value", req.document_kind),
        )
    except SourceRegistryError as exc:
        raise HTTPException(422, f"unsupported_source: {exc}") from exc
    resolved_domain = req.domain
    if req.collection_id:
        collection = await asyncio.to_thread(db.get_collection, req.collection_id)
        if not collection:
            raise HTTPException(400, "collection_id not found")
        if req.domain not in ("general", collection.domain):
            raise HTTPException(409, "job and collection domain mismatch")
        resolved_domain = collection.domain
    await ensure_job_workers(
        redis=redis, config=config, content_type=route.content_type,
        source=source, url=first_url, domain=resolved_domain,
        style_tags=req.style_tags, smart_note=req.smart_note,
        mechanical_only=req.mechanical_only,
        document_kind=route.document_kind,
        allow_waiting=route.content_type == "video",
    )
    job = await create_job_core(
        db, redis, storage, req.url, req.content_type,
        resolved_domain, req.style_tags, req.collection_id,
        smart_note=req.smart_note, document_kind=route.document_kind, config=config,
        mechanical_only=req.mechanical_only,
        title=req.title,
        parts=[part.model_dump() for part in req.parts] if req.parts else None,
    )
    created_parts = await asyncio.to_thread(db.get_parts, job.id)
    return {"job_id": job.id, "content_type": job.content_type,
            "document_kind": job.document_kind or None, "pipeline": job.pipeline,
            "status": job.status.value, "created_at": job.created_at.isoformat(),
            "parts": [
                {"part_id": part.id, "part_index": part.part_index, "title": part.title}
                for part in created_parts
            ]}


@router.post("/upload", status_code=201, response_model=JobCreatedResponse)
@job_admission_guard("upload")
async def upload_job(
    content_type: ContentType = Query(...),
    document_kind: DocumentKind | None = Query(None),
    mechanical_only: bool = Query(False),
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
    if getattr(content_type, "value", content_type) == "video":
        raise HTTPException(422, "video upload is replaced by parts[]")
    filename_type = _detect_content_type(None, file.filename)
    if filename_type is None:
        raise HTTPException(422, "unsupported_upload_type")
    if filename_type != content_type:
        raise HTTPException(422, "upload_content_type_mismatch")
    try:
        route = resolve_job_route(
            "upload", getattr(content_type, "value", content_type),
            document_kind=getattr(document_kind, "value", document_kind),
        )
    except SourceRegistryError as exc:
        raise HTTPException(422, f"unsupported_source: {exc}") from exc
    try:
        tags = json.loads(style_tags)
    except (json.JSONDecodeError, RecursionError):
        raise HTTPException(400, "invalid style_tags JSON")
    if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
        raise HTTPException(400, "style_tags must be a string array")
    resolved_domain = domain
    if collection_id:
        collection = await asyncio.to_thread(db.get_collection, collection_id)
        if not collection:
            raise HTTPException(400, "collection_id not found")
        if domain not in ("general", collection.domain):
            raise HTTPException(409, "job and collection domain mismatch")
        resolved_domain = collection.domain
    await ensure_job_workers(
        redis=redis, config=config, content_type=route.content_type,
        source="upload", url=None, domain=resolved_domain, style_tags=tags,
        document_kind=route.document_kind,
        mechanical_only=mechanical_only,
    )

    async def _chunks() -> AsyncIterator[bytes]:
        while chunk := await file.read(UPLOAD_CHUNK_SIZE):
            yield chunk

    ext = Path(file.filename).suffix if file.filename else ".mp4"
    try:
        job = await create_job_core(
            db, redis, storage, url=None, content_type=content_type,
            domain=resolved_domain, style_tags=tags, collection_id=collection_id, title=title,
            upload=(ext, _chunks()), document_kind=route.document_kind, config=config,
            mechanical_only=mechanical_only,
        )
    except ArtifactTooLarge as exc:
        raise HTTPException(
            413, f"file too large (max {MAX_UPLOAD_SIZE})",
        ) from exc
    return {"job_id": job.id, "content_type": job.content_type,
            "document_kind": job.document_kind or None, "pipeline": job.pipeline,
            "status": "pending", "created_at": job.created_at.isoformat()}


@router.put("/{job_id}/collection", response_model=JobCollectionUpdateResponse)
async def update_job_collection(
    job_id: str,
    req: JobCollectionUpdateRequest,
    db: Database = Depends(get_db),
):
    """把已有 job 归入、移动到或移出集合,不复制 lineage 也不重跑流水线。"""
    validate_path_segment(job_id, "job_id")
    if req.collection_id is not None:
        validate_path_segment(req.collection_id, "collection_id")
    job = await asyncio.to_thread(db.get_job, job_id)
    if not job:
        raise HTTPException(404, "job not found")
    if not job.is_current:
        raise HTTPException(409, "only current job can change collection")
    if req.collection_id is not None:
        collection = await asyncio.to_thread(db.get_collection, req.collection_id)
        if not collection:
            raise HTTPException(404, "collection not found")
        if collection.domain != job.domain:
            raise HTTPException(409, "job and collection domain mismatch")
    try:
        previous, current, changed = await asyncio.to_thread(
            db.move_job_to_collection, job_id, req.collection_id,
        )
    except KeyError as exc:
        raise HTTPException(404, str(exc).strip("'")) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    audit(
        "job", job_id, "collection_update", actor="api",
        detail={"previous_collection_id": previous, "collection_id": current,
                "changed": changed},
    )
    return JobCollectionUpdateResponse(
        job_id=job_id,
        previous_collection_id=previous,
        collection_id=current,
        changed=changed,
    )


@router.get("", response_model=JobListResponse)
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
                job_id=j.id, content_type=j.content_type,
                document_kind=j.document_kind or None, pipeline=j.pipeline,
                status=j.status.value,
                created_at=j.created_at.isoformat(), title=j.title,
                progress_pct=j.progress_pct, source=j.source, domain=j.domain,
                collection_id=j.collection_id,
                versions=counts.get(j.lineage_key, 1) if j.lineage_key else 1,
                processing_mode=(
                    "mechanical_only"
                    if (j.meta.get("flags") or {}).get("mechanical_only") else "full"
                ),
                completion_scope=(
                    "mechanical"
                    if (j.meta.get("flags") or {}).get("mechanical_only") else "full"
                ),
            )
            for j in jobs
        ],
    )


@router.get("/facets", response_model=JobFacetsResponse)
async def job_facets(db: Database = Depends(get_db)):
    """全量 jobs 按 source / domain / status 的计数,供前端过滤 chip。
    注:须在 /{job_id} 之前注册,否则被路径参数捕获为 job_id='facets'。"""
    return await asyncio.to_thread(db.job_facets)


@router.get("/{job_id}", response_model=JobDetailResponse)
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
    parts = await asyncio.to_thread(db.get_parts, job_id)
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
    part_responses: list[dict] = []
    if parts:
        try:
            source_library = SourceLibrary.from_env()
        except SourceReferenceError:
            source_library = SourceLibrary({})
        by_part: dict[str, list[Step]] = {part.id: [] for part in parts}
        for step in steps:
            part_id = part_id_from_scope(step.scope_key)
            if part_id in by_part:
                by_part[part_id].append(step)
        total_duration = 0.0
        for part in parts:
            part_media: dict = {}
            try:
                raw = await storage.read_file(
                    job_id, f"parts/{part.id}/input/metadata.json",
                )
                if raw:
                    md = json.loads(raw.decode("utf-8"))
                    part_media = {
                        key: md[key]
                        for key in (
                            "duration_sec", "file_size_bytes", "resolution",
                            "width", "height", "has_subtitle", "has_danmaku",
                        )
                        if md.get(key) is not None
                    }
                    total_duration += float(part_media.get("duration_sec") or 0)
            except Exception:
                pass
            current_steps = by_part.get(part.id, [])
            statuses = [step.status.value for step in current_steps]
            if any(status == "failed" for status in statuses):
                part_status = "failed"
            elif any(status == "running" for status in statuses):
                part_status = "running"
            elif statuses and all(status in {"done", "skipped"} for status in statuses):
                part_status = "done"
            else:
                part_status = "pending"
            completed = sum(status in {"done", "skipped"} for status in statuses)
            progress = round(100 * completed / len(statuses)) if statuses else 0
            source_response = None
            if part.source_ref and part.source_digest and part.size_bytes:
                try:
                    reference = parse_source_ref(part.source_ref)
                    source_response = {
                        "root_id": reference.root_id,
                        "relative_path": reference.relative_path,
                        "sha256": part.source_digest.removeprefix("sha256:"),
                        "size_bytes": part.size_bytes,
                        "status": source_library.status(
                            part.source_ref, part.size_bytes,
                        ),
                    }
                except SourceReferenceError:
                    source_response = {
                        "root_id": "invalid",
                        "relative_path": "",
                        "sha256": part.source_digest.removeprefix("sha256:"),
                        "size_bytes": part.size_bytes,
                        "status": "invalid",
                    }
            part_responses.append({
                "part_id": part.id,
                "part_index": part.part_index,
                "title": part.title,
                "url": part.source_url,
                "source": source_response,
                "status": part_status,
                "progress_pct": progress,
                "media": part_media,
                "steps": [
                    StepResponse(
                        name=step.name,
                        label=labels.get(step.name),
                        status=step.status.value,
                        started_at=step.started_at.isoformat() if step.started_at else None,
                        finished_at=step.finished_at.isoformat() if step.finished_at else None,
                        duration_sec=step.duration_sec,
                        meta=step.meta,
                        error=step.error,
                        worker_id=step.worker_id,
                    )
                    for step in current_steps
                ],
            })
        if total_duration:
            media["duration_sec"] = total_duration
    # Document metadata 以 document.json 为真相源；旧 parsed.json 不再参与新文档链。
    source_profile = None
    if job.content_type == "document":
        try:
            raw = await storage.read_file(job_id, "intermediate/document.json")
            if raw:
                p = json.loads(raw.decode("utf-8"))
                source_profile = p.get("source_profile")
                metadata = p.get("metadata") if isinstance(p.get("metadata"), dict) else {}
                for key in (
                    "authors", "affiliations", "abstract", "lang", "word_count",
                    "tags", "image", "sitename", "pages", "venue", "source_license",
                    "rights_notices", "version", "published_at",
                ):
                    if metadata.get(key) is not None:
                        media[key] = metadata[key]
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
    # 本任务 AI 步用的 prompt 覆盖版本号快照:从 job.json.prompt_overrides[step].version 读,
    # 注入形态为 {content, version};旧 job 的覆盖是纯字符串无版本,跳过。供前端比对本任务与当前版本。
    prompt_versions: dict = {}
    prompt_snapshot: dict = {}
    try:
        raw = await storage.read_file(job_id, "job.json")
        if raw:
            jd = json.loads(raw.decode("utf-8"))
            stored_prompts = jd.get("prompt_overrides") or {}
            prompt_snapshot = stored_prompts if isinstance(stored_prompts, dict) else {}
            for stp, val in prompt_snapshot.items():
                if isinstance(val, dict) and val.get("version") is not None:
                    prompt_versions[stp] = str(val["version"])
    except Exception:
        pass
    try:
        update_state = await is_job_expired(storage, config, job)
    except Exception:
        update_state = {"expired": False, "first_changed_step": None}
    # Prompt 覆盖不属于 pipeline def_digest,需独立比较任务快照与当前激活内容.
    try:
        active_prompts = await asyncio.to_thread(
            db.resolve_prompt_overrides, job.pipeline, job.domain,
        )
        changed_prompts = {
            step_name
            for step_name in set(prompt_snapshot) | set(active_prompts)
            if prompt_snapshot.get(step_name) != active_prompts.get(step_name)
        }
        if changed_prompts:
            pipeline_order = [
                step["name"]
                for step in config.pipelines.get(job.pipeline, {}).get("steps", [])
            ]
            changed_steps = set(changed_prompts)
            if update_state["first_changed_step"]:
                changed_steps.add(update_state["first_changed_step"])
            first_changed = next(
                (step_name for step_name in pipeline_order if step_name in changed_steps),
                sorted(changed_steps)[0],
            )
            update_state = {"expired": True, "first_changed_step": first_changed}
    except Exception:
        pass
    return JobDetailResponse(
        job_id=job.id, content_type=job.content_type,
        document_kind=job.document_kind or None, pipeline=job.pipeline,
        status=job.status.value,
        created_at=job.created_at.isoformat(),
        updated_at=job.updated_at.isoformat() if job.updated_at else None,
        published_at=(job.published_at.isoformat() if job.published_at else published_at),
        media=media, artifacts=artifacts,
        title=job.title, url=job.url,
        progress_pct=job.progress_pct, source=job.source, domain=job.domain,
        collection_id=job.collection_id, collection_name=collection_name,
        meta=job.meta, source_profile=source_profile,
        processing_mode=(
            "mechanical_only"
            if (job.meta.get("flags") or {}).get("mechanical_only") else "full"
        ),
        completion_scope=(
            "mechanical"
            if (job.meta.get("flags") or {}).get("mechanical_only") else "full"
        ),
        update_available=bool(update_state["expired"]),
        update_from_step=update_state["first_changed_step"],
        steps=[
            StepResponse(
                name=s.name, label=labels.get(s.name), status=s.status.value,
                started_at=s.started_at.isoformat() if s.started_at else None,
                finished_at=s.finished_at.isoformat() if s.finished_at else None,
                duration_sec=s.duration_sec, meta=s.meta, error=s.error,
                worker_id=s.worker_id,
            )
            for s in steps if s.scope_key == "job"
        ],
        parts=part_responses,
        prompt_versions=prompt_versions,
    )


@router.get("/{job_id}/versions", response_model=LineageVersionsResponse)
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


@router.get("/{job_id}/concepts", response_model=list[JobConceptResponse])
async def job_concepts(
    job_id: str,
    db: Database = Depends(get_db),
):
    """该内容命中的概念(occurrences 含本 job),含本 job 命中的出现位置 job_occurrences。"""
    validate_path_segment(job_id, "job_id")
    job = await asyncio.to_thread(db.get_job, job_id)
    if not job:
        raise HTTPException(404, "job not found")
    rows = await asyncio.to_thread(db.glossary_for_job, job_id, job.domain)
    return [
        JobConceptResponse(
            **GlossaryTermResponse.from_row(row).model_dump(),
            job_occurrences=row.get("job_occurrences") or [],
        )
        for row in rows
    ]


@router.get("/{job_id}/usage", response_model=JobUsageResponse)
async def job_usage(
    job_id: str,
    db: Database = Depends(get_db),
):
    """该 job 的逐次 AI 调用明细(按步展示 in/out/cache/命中率/cost/耗时/轮数/worker)。
    cost 对 claude-cli CLI 是等价 API 成本,前端按 provider==claude-cli 标「(等价)」。"""
    validate_path_segment(job_id, "job_id")
    return {"usage": await asyncio.to_thread(db.list_usage_by_job, job_id)}


async def _read_step_log(
    storage: StorageBackend, job_id: str, rel_path: str, raw: int,
) -> PlainTextResponse:
    """读取并按公开日志契约截断单个scope的步骤日志。"""
    data = await storage.read_file(job_id, rel_path)
    if data is None:
        raise HTTPException(404, "log not found")
    if not raw:
        max_bytes = 256 * 1024
        if len(data) > max_bytes:
            data = b"...(truncated, last 256KB)...\n" + data[-max_bytes:]
    return PlainTextResponse(data.decode("utf-8", errors="replace"))


@router.get("/{job_id}/steps/{step}/log", response_class=PlainTextResponse)
async def get_step_log(
    job_id: str, step: str, raw: int = 0,
    storage: StorageBackend = Depends(get_storage),
):
    """返回Job级步骤日志;Part步骤必须使用显式Part日志路径。"""
    validate_path_segment(job_id, "job_id")
    validate_path_segment(step, "step")
    return await _read_step_log(storage, job_id, f"logs/{step}.log", raw)


@router.get(
    "/{job_id}/parts/{part_id}/steps/{step}/log",
    response_class=PlainTextResponse,
)
async def get_part_step_log(
    job_id: str, part_id: str, step: str, raw: int = 0,
    storage: StorageBackend = Depends(get_storage),
    db: Database = Depends(get_db),
):
    """返回指定Part的步骤日志,拒绝跨Job伪造Part ID。"""
    validate_path_segment(job_id, "job_id")
    validate_path_segment(part_id, "part_id")
    validate_path_segment(step, "step")
    parts = await asyncio.to_thread(db.get_parts, job_id)
    if part_id not in {part.id for part in parts}:
        raise HTTPException(404, "job part not found")
    return await _read_step_log(
        storage, job_id, f"parts/{part_id}/logs/{step}.log", raw,
    )


@router.get("/{job_id}/ai-logs", response_model=AiLogsResponse)
async def job_ai_logs(
    job_id: str,
    step: str | None = None,
    part_id: str | None = None,
    storage: StorageBackend = Depends(get_storage),
    db: Database = Depends(get_db),
):
    """返回Job或显式Part scope的AI审计日志,不做跨scope隐式聚合。"""
    validate_path_segment(job_id, "job_id")
    if step is not None:
        validate_path_segment(step, "step")
    if part_id is not None:
        validate_path_segment(part_id, "part_id")
        parts = await asyncio.to_thread(db.get_parts, job_id)
        if part_id not in {part.id for part in parts}:
            raise HTTPException(404, "job part not found")
    scope_key = part_scope(part_id) if part_id is not None else "job"
    prefix = f"parts/{part_id}/" if part_id is not None else ""
    log_prefix = f"{prefix}output/ai_logs/"
    try:
        files = await storage.list_files(job_id)
    except Exception:
        files = []
    targets = [f for f in files if f.startswith(log_prefix) and f.endswith(".jsonl")]
    if step is not None:
        targets = [f for f in targets if f == f"{log_prefix}{step}.jsonl"]
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
        steps.append({
            "scope_key": scope_key,
            "part_id": part_id,
            "step": rel.rsplit("/", 1)[-1][: -len(".jsonl")],
            "calls": calls,
        })
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
    """精准级联删一个 job —— 单 job 删除与集合 purge 共用,顺序保证 DB 行最后删 + 每步幂等:
    任一步崩溃则 job 仍在 DB → 可原样重删补齐(不依赖周期 GC)。
    1. 清 redis 队列残留(queue:{pool} + queue:enqueued)+ 7 个编排 hash + active 集合;
    2. publish 让 scheduler 取消在途延迟重试(进程内 asyncio,只能 scheduler 端做);
    3. 删产物(LocalStorage 删目录 / RemoteStorage 删 {job_id}/ 前缀);
    4. 最后删 DB(jobs 行 + FTS + ai_usage + 集合计数 + glossary 出现 + 订阅 ingested_items);
    5. 审计。running job:读其 running 步的 holder(=exec_id)→ release_holders 立即归还所占池槽/资源槽;
       worker 推回结果经 cas_step_status 见 steps hash 已删而 CAS 失败被丢弃,其迟到 release_step 再 SREM 同
       holder 也幂等无害。"""
    job_id = job.id
    item_id = (job.meta or {}).get("source_item_id")
    # 删 running job 立即归还其 running 步占的槽。必须在 cleanup_job 删 steps hash 之前读 exec_id。
    stale_holders: set[str] = set()
    try:
        for st, status in (await redis.get_all_step_statuses(job_id)).items():
            if status == "running":
                ex = await redis.get_step_exec_id(job_id, st)
                if ex:
                    stale_holders.add(ex)
    except Exception:
        pass
    removed = await redis.remove_job_tasks(job_id)          # 1. 队列 ZSET + queue:enqueued
    await redis.cleanup_job(job_id)                         #    7 个 job:{id}* 编排 hash
    await redis.remove_active_job(job_id)                   #    SREM active_jobs
    await redis.release_holders(stale_holders)              #    归还 running 步的池槽/资源槽(幂等)
    await redis.append_lifecycle_event("job_command", {"action": "delete", "job_id": job_id})  # 2. 取消在途重试
    await storage.delete(job_id)                            # 3. 产物
    await asyncio.to_thread(db.delete_job_cascade, job_id, job.collection_id, item_id)  # 4. DB 最后
    # 删的是 current → 把同 lineage 剩余最新一版提为 current(否则该内容在列表消失)。
    if job.is_current and job.lineage_key:
        await asyncio.to_thread(db.promote_lineage_current, job.lineage_key)
    audit("job", job_id, "delete", actor=actor, detail={                               # 5. 审计
        "queue_tasks_removed": removed, "collection_id": job.collection_id,
        "purged_ingested": bool(item_id),
    })


@router.post("/retry-failed", response_model=JobsRetriedResponse)
async def retry_all_failed(
    collection_id: str | None = Query(None, description="仅重试该集合的失败 job;不传=全局所有失败"),
    db: Database = Depends(get_db),
    redis: RedisClient = Depends(get_redis),
):
    """批量重试 failed job(各自从首个失败步重跑,自动重置下游)。返回发起数。
    传 collection_id 则限定该集合(集合详情页"重试本集合失败");不传=全局所有失败。
    注:缺凭证类失败(如无 cookie 的 YouTube 下载)修好根因前会再失败。"""
    # 空串(?collection_id=)归一为 None:否则 list_jobs 的 `elif collection_id:` 对空串为假,
    # 集合过滤落空,静默退化成全局重试所有 failed,与限定该集合的语义相悖且误触批量重发。
    if collection_id is not None:
        collection_id = collection_id.strip() or None
    if collection_id is not None:
        validate_path_segment(collection_id, "collection_id")
    _, jobs = await asyncio.to_thread(
        db.list_jobs, status="failed", collection_id=collection_id, limit=100000
    )
    for j in jobs:
        await redis.append_lifecycle_event("job_command", {"action": "retry", "job_id": j.id})
    return {"retried": len(jobs)}


@router.post("/{job_id}/retry", response_model=JobStatusResponse)
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
    await redis.append_lifecycle_event("job_command", {"action": "retry", "job_id": job_id})
    return {"job_id": job_id, "status": "processing"}


@router.post("/{job_id}/rerun", response_model=JobRerunResponse)
async def rerun_job(
    job_id: str,
    req: RerunRequest,
    db: Database = Depends(get_db),
    redis: RedisClient = Depends(get_redis),
    config: AppConfig = Depends(get_config),
):
    validate_path_segment(job_id, "job_id")
    job = await asyncio.to_thread(db.get_job, job_id)
    if not job:
        raise HTTPException(404, "job not found")
    pipeline = config.pipelines.get(job.pipeline)
    steps = pipeline.get("steps") if isinstance(pipeline, dict) else None
    allowed_steps = {
        str(step.get("name")) for step in steps or []
        if (
            isinstance(step, dict)
            and step.get("name")
            and step.get("scope", "job") == "job"
        )
    }
    if req.from_step not in allowed_steps:
        raise HTTPException(422, "job rerun only accepts job-scoped steps")
    await redis.append_lifecycle_event("job_command", {
        "action": "rerun", "job_id": job_id, "from_step": req.from_step,
    })
    return {"job_id": job_id, "status": "processing", "from_step": req.from_step}


@router.post(
    "/{job_id}/parts/{part_id}/rerun",
    response_model=JobRerunResponse,
)
async def rerun_job_part(
    job_id: str,
    part_id: str,
    req: RerunRequest,
    db: Database = Depends(get_db),
    redis: RedisClient = Depends(get_redis),
    config: AppConfig = Depends(get_config),
):
    """只重跑目标Part的map步骤,并失效其Job级下游。"""
    validate_path_segment(job_id, "job_id")
    validate_path_segment(part_id, "part_id")
    job = await asyncio.to_thread(db.get_job, job_id)
    if not job:
        raise HTTPException(404, "job not found")
    parts = await asyncio.to_thread(db.get_parts, job_id)
    if part_id not in {part.id for part in parts}:
        raise HTTPException(404, "part not found")
    pipeline = config.pipelines.get(job.pipeline)
    steps = pipeline.get("steps") if isinstance(pipeline, dict) else None
    allowed_steps = {
        str(step.get("name")) for step in steps or []
        if (
            isinstance(step, dict)
            and step.get("name")
            and step.get("scope", "job") == "part"
        )
    }
    if req.from_step not in allowed_steps:
        raise HTTPException(422, "part rerun only accepts part-scoped steps")
    execution_step = execution_step_key(part_scope(part_id), req.from_step)
    await redis.append_lifecycle_event("job_command", {
        "action": "rerun",
        "job_id": job_id,
        "part_id": part_id,
        "from_step": execution_step,
    })
    return {
        "job_id": job_id,
        "status": "processing",
        "from_step": req.from_step,
        "part_id": part_id,
    }


@router.post("/{job_id}/rebuild", response_model=JobRebuildResponse)
async def rebuild_job(
    job_id: str,
    req: RebuildRequest | None = None,
    db: Database = Depends(get_db),
    redis: RedisClient = Depends(get_redis),
    storage: StorageBackend = Depends(get_storage),
    config: AppConfig = Depends(get_config),
):
    """重建为新快照(fork 父 job:播种产物+.done,只重跑分叉步及下游;旧版保留 A/B)。
    返回新 job_id;新版自动成为该 lineage 的 current。"""
    validate_path_segment(job_id, "job_id")
    request = req or RebuildRequest()
    parent = await asyncio.to_thread(db.get_job, job_id)
    if not parent:
        raise HTTPException(404, "job not found")
    operation_key = request.idempotency_key
    if operation_key is None:
        prompt_overrides = await asyncio.to_thread(
            db.resolve_prompt_overrides,
            parent.pipeline,
            parent.domain,
            parent.document_kind or None,
        )
        operation_key = "rebuild-default:" + hashlib.sha256(json.dumps({
            "mechanical_only": request.mechanical_only,
            "from_step": request.from_step,
            "pipeline_digest": _pipeline_digest(config, parent.pipeline),
            "prompt_overrides": prompt_overrides or {},
        }, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    target_id = "jr_" + hashlib.sha256(
        f"{job_id}\0{operation_key}".encode("utf-8")
    ).hexdigest()[:24]
    existing = await asyncio.to_thread(db.get_job, target_id)
    if existing is None:
        if parent.status in {
            JobStatus.PENDING, JobStatus.DOWNLOADING, JobStatus.PROCESSING,
        }:
            raise HTTPException(409, "cannot rebuild an active parent job")
        inherited_mechanical = (
            (parent.meta.get("flags") or {}).get("mechanical_only") is True
        )
        effective_mechanical = (
            request.mechanical_only
            if request.mechanical_only is not None
            else inherited_mechanical
        )
        if not effective_mechanical:
            await ensure_job_workers(
                redis=redis, config=config, content_type=parent.content_type,
                source=parent.source or detect_source(parent.url or ""), url=parent.url,
                domain=parent.domain, style_tags=parent.style_tags,
                smart_note=(parent.meta.get("flags") or {}).get("smart_note"),
                mechanical_only=False, document_kind=parent.document_kind or None,
            )
    job = await create_job_snapshot(
        db, redis, storage, config, job_id,
        mechanical_only=request.mechanical_only,
        from_step=request.from_step,
        idempotency_key=operation_key,
    )
    mechanical = (job.meta.get("flags") or {}).get("mechanical_only") is True
    return {"job_id": job.id, "parent_job_id": job.parent_job_id,
            "lineage_key": job.lineage_key, "status": "pending",
            "from_step": request.from_step,
            "processing_mode": "mechanical_only" if mechanical else "full"}


@router.post("/rebuild-stale", response_model=JobsRebuiltResponse)
async def rebuild_stale(
    db: Database = Depends(get_db),
    redis: RedisClient = Depends(get_redis),
    storage: StorageBackend = Depends(get_storage),
    config: AppConfig = Depends(get_config),
):
    """批量重建所有过期 current job(其某步定义指纹 def_digest 与当前 pipeline 不符)为新快照。
    仿 retry-failed:逐个判过期 → create_job_snapshot。返回重建清单。"""
    _, jobs = await asyncio.to_thread(db.list_jobs, None, None, 10000, 0, None, None, False, True)
    rebuilt = []
    for job in jobs:
        record = (job.meta or {}).get("rebuild_request") or {}
        if (
            isinstance(record, dict)
            and record.get("phase") == "ready"
            and record.get("event_published") is not True
            and str(record.get("idempotency_key", "")).startswith("rebuild-stale:")
            and isinstance(record.get("parent_job_id"), str)
        ):
            repaired = await create_job_snapshot(
                db, redis, storage, config, record["parent_job_id"],
                mechanical_only=record.get("mechanical_only"),
                smart_note=record.get("smart_note"),
                from_step=record.get("from_step"),
                reset_roots=record.get("reset_roots") or [],
                idempotency_key=record["idempotency_key"],
            )
            rebuilt.append({
                "parent_job_id": record["parent_job_id"],
                "job_id": repaired.id,
                "from_step": record.get("from_step"),
            })
            continue
        if job.status in {JobStatus.PENDING, JobStatus.PROCESSING, JobStatus.DOWNLOADING}:
            continue
        exp = await is_job_expired(storage, config, job)
        if exp["expired"]:
            operation = {
                "lineage_key": job.lineage_key or _lineage_key_of(job.id),
                "pipeline_digest": _pipeline_digest(config, job.pipeline),
                "first_changed_step": exp["first_changed_step"],
            }
            durable_key = "rebuild-stale:" + hashlib.sha256(json.dumps(
                operation, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            ).encode("utf-8")).hexdigest()
            mechanical = (job.meta.get("flags") or {}).get("mechanical_only") is True
            if not mechanical:
                await ensure_job_workers(
                    redis=redis, config=config, content_type=job.content_type,
                    source=job.source or detect_source(job.url or ""), url=job.url,
                    domain=job.domain, style_tags=job.style_tags,
                    smart_note=(job.meta.get("flags") or {}).get("smart_note"),
                    mechanical_only=False, document_kind=job.document_kind or None,
                )
            new = await create_job_snapshot(
                db, redis, storage, config, job.id,
                from_step=exp["first_changed_step"],
                idempotency_key=durable_key,
            )
            rebuilt.append({"parent_job_id": job.id, "job_id": new.id,
                            "from_step": exp["first_changed_step"]})
    return {"rebuilt": len(rebuilt), "items": rebuilt}


async def _provider_workers(redis: RedisClient) -> list[dict]:
    """取一次在线能力快照;mock/注册表异常都按无可用 worker 处理。"""
    try:
        worker_ids = await redis.list_worker_ids()
    except Exception:
        return []
    if not isinstance(worker_ids, (list, tuple, set)):
        return []
    workers = []
    for worker_id in worker_ids:
        try:
            info = await redis.get_worker_info(worker_id)
        except Exception:
            continue
        if isinstance(info, dict):
            workers.append(info)
    return workers


def _provider_available(
    name: str,
    cfg: dict,
    workers: list[dict],
    step_requirements: list[tuple[str, list[str]]],
) -> bool:
    """已配置 provider 必须对每个目标步骤都有真实在线 worker 能力。"""
    if not provider_is_configured(name, cfg):
        return False
    for pool, static_tags in step_requirements:
        try:
            provider_tags = provider_required_tags(
                name, cfg,
                required_tags=[tag for tag in static_tags if tag == READ_TOOL_TAG],
            )
        except ValueError:
            return False
        if not any(
            worker_satisfies_requirements(
                worker, pool, set(static_tags) | set(provider_tags),
            )
            for worker in workers
        ):
            return False
    return True


async def _rerun_step_requirements(
    config: AppConfig, pipeline: str, step_names: tuple[str, str],
    storage: StorageBackend, job_id: str,
) -> list[tuple[str, list[str]]] | None:
    """目标步骤的 pool、静态标签和本次产物条件能力来自 pipeline 定义。"""
    try:
        steps = config.pipelines[pipeline]["steps"]
    except (KeyError, TypeError):
        return None
    by_name = {
        step.get("name"): step for step in steps
        if isinstance(step, dict) and isinstance(step.get("name"), str)
    }
    result = []
    for name in step_names:
        step = by_name.get(name)
        if not step or not isinstance(step.get("pool"), str):
            return None
        tags = step.get("tags") or []
        if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
            return None
        async def has_nonempty_artifact(rel: str) -> bool:
            return bool(await read_file_bounded(storage, job_id, rel, 0))

        try:
            capability_tags = await step_required_capability_tags(
                step, has_nonempty_artifact,
            )
        except (OSError, ValueError, TypeError):
            return None
        result.append((step["pool"], sorted(set(tags) | set(capability_tags))))
    return result


@router.post("/{job_id}/rerun-smart", response_model=JobRerunSmartResponse)
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
    try:
        smart_step, review_step = pipeline_ai_roles(job.pipeline)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    requirements = await _rerun_step_requirements(
        config, job.pipeline, (smart_step, review_step), storage, job_id,
    )
    workers = await _provider_workers(redis)
    if requirements is None or not _provider_available(
        req.provider, config.providers, workers, requirements,
    ):
        raise HTTPException(400, f"provider '{req.provider}' 无匹配在线 worker")
    # 把 provider 覆盖写进 job.json(智能/评审步会读),worker rerun 时 pull 到新 job.json。
    raw = await storage.read_file(job_id, "job.json")
    try:
        doc = {} if raw is None else json.loads(raw)
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        raise HTTPException(409, "job.json 格式非法") from exc
    if not isinstance(doc, dict):
        raise HTTPException(409, "job.json 顶层必须是对象")
    overrides = doc.get("ai_overrides")
    if overrides is None and "ai_overrides" not in doc:
        overrides = {}
        doc["ai_overrides"] = overrides
    if not isinstance(overrides, dict):
        raise HTTPException(409, "job.json ai_overrides 必须是对象")
    doc["ai_overrides"][smart_step] = req.provider
    doc["ai_overrides"][review_step] = req.provider
    await storage.write_file(job_id, "job.json",
                             json.dumps(doc, ensure_ascii=False, indent=2).encode("utf-8"))
    await redis.append_lifecycle_event("job_command", {
        "action": "rerun", "job_id": job_id, "from_step": smart_step,
    })
    return {"job_id": job_id, "status": "processing", "provider": req.provider,
            "from_step": smart_step, "review_step": review_step}


@router.post("/{job_id}/resubmit", response_model=JobStatusResponse)
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
    await redis.append_lifecycle_event("job_command", {"action": "resubmit", "job_id": job_id})
    return {"job_id": job_id, "status": "processing"}


@router.post("/{job_id}/continue-ai", response_model=JobStatusResponse)
async def continue_job_ai(
    job_id: str,
    db: Database = Depends(get_db),
    redis: RedisClient = Depends(get_redis),
    storage: StorageBackend = Depends(get_storage),
    config: AppConfig = Depends(get_config),
):
    """机械版完成后 fork 完整处理快照;父快照不可变,新快照重算 AI 根及其全部下游。"""
    validate_path_segment(job_id, "job_id")
    job = await asyncio.to_thread(db.get_job, job_id)
    if not job:
        raise HTTPException(404, "job not found")
    flags = dict((job.meta or {}).get("flags") or {})
    if flags.get("mechanical_only") is not True:
        raise HTTPException(409, "job is not in mechanical_only mode")
    steps = config.pipelines.get(job.pipeline, {}).get("steps", [])
    by_name = {
        step.get("name"): step for step in steps
        if isinstance(step, dict) and isinstance(step.get("name"), str)
    }

    def _has_ai_ancestor(name: str, seen: set[str] | None = None) -> bool:
        visited = set(seen or ())
        if name in visited:
            return False
        visited.add(name)
        for dep in by_name[name].get("depends_on", by_name[name].get("needs", [])):
            cfg = by_name.get(dep)
            if cfg is None:
                continue
            if cfg.get("pool") == "ai" or _has_ai_ancestor(dep, visited):
                return True
        return False

    ai_roots = sorted(
        name for name, cfg in by_name.items()
        if cfg.get("pool") == "ai" and not _has_ai_ancestor(name)
    )
    if not ai_roots:
        raise HTTPException(409, "pipeline has no AI steps to continue")
    if not job.is_current:
        versions = await asyncio.to_thread(db.lineage_versions, job_id)
        for version in versions:
            record = (version.meta or {}).get("rebuild_request") or {}
            if (
                version.parent_job_id == job_id
                and record.get("idempotency_key") == "continue-ai:v1"
                and record.get("phase") == "ready"
            ):
                repaired = await create_job_snapshot(
                    db, redis, storage, config, job_id,
                    mechanical_only=False, smart_note=True, reset_roots=ai_roots,
                    idempotency_key="continue-ai:v1",
                )
                return {"job_id": repaired.id, "status": repaired.status.value}
    if not job.is_current or job.status != JobStatus.DONE:
        raise HTTPException(409, "continue-ai requires a completed current mechanical snapshot")
    step_rows = await asyncio.to_thread(db.get_steps, job_id)
    if any(step.status not in {StepStatus.DONE, StepStatus.SKIPPED} for step in step_rows):
        raise HTTPException(409, "mechanical steps are not all terminal")
    await ensure_job_workers(
        redis=redis, config=config, content_type=job.content_type,
        source=job.source or detect_source(job.url or ""), url=job.url,
        domain=job.domain, style_tags=job.style_tags, smart_note=True,
        mechanical_only=False, document_kind=job.document_kind or None,
    )
    new = await create_job_snapshot(
        db, redis, storage, config, job_id,
        mechanical_only=False, smart_note=True, reset_roots=ai_roots,
        idempotency_key="continue-ai:v1",
    )
    return {"job_id": new.id, "status": "pending"}
