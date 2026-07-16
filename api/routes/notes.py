"""笔记/截图/视频文件服务。经 StorageBackend 读,兼容本地盘与 MinIO。"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import mimetypes

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from shared.config import AppConfig
from shared.db import Database
from shared.notes_versions import latest_smart, parse_smart_version, review_path_for_note
from shared.evidence_contract import (
    MAX_EVIDENCE_BYTES,
    project_evidence,
    validate_manifest_with_reader,
)
from shared.review_contract import (
    MAX_REVIEW_SOURCE_BYTES,
    project_review,
    verify_persisted_review,
)
from shared.storage import (
    StorageBackend,
    read_file_bounded,
    read_verification_artifact_bounded,
)
from api.deps import get_config, get_db, get_storage, validate_path_segment, verify_token
from api.wire_schemas import (
    API_ERROR_RESPONSES,
    ArtifactsResponse,
    EvidenceProjectionResponse,
    NoteVersionsResponse,
    ReviewProjectionResponse,
)

router = APIRouter(
    prefix="/api/jobs", tags=["notes"], dependencies=[Depends(verify_token)],
    responses=API_ERROR_RESPONSES,
)

_MARKDOWN_RESPONSE = {
    200: {"content": {"text/markdown": {"schema": {"type": "string"}}}},
}
_BINARY_RESPONSE = {
    200: {"content": {"application/octet-stream": {
        "schema": {"type": "string", "format": "binary"},
    }}},
}
_RANGE_RESPONSE = {
    **_BINARY_RESPONSE,
    206: {"content": {"application/octet-stream": {
        "schema": {"type": "string", "format": "binary"},
    }}},
}

def _artifact_kind(path: str) -> str:
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    if ext in ("jpg", "jpeg", "png", "gif", "webp"):
        return "image"
    if ext in ("mp4", "webm", "mkv", "mov"):
        return "video"
    if ext in ("mp3", "m4a", "wav", "aac"):
        return "audio"
    if ext == "json":
        return "json"
    if ext in ("md", "srt", "txt", "html", "ass", "log"):
        return "text"
    return "other"


def _artifact_hidden(f: str) -> bool:
    # 仅出于安全/整洁强制隐藏:内部点文件 + job.json(含 SESSDATA)。
    # 展示哪些产物由 pipelines.yaml 各步的 outputs 决定(单一事实源),不在此写死。
    base = f.rsplit("/", 1)[-1]
    return base.startswith(".") or f == "job.json"


def _validate_job_id(job_id: str) -> None:
    validate_path_segment(job_id, "job_id")


async def _serve(
    storage: StorageBackend, job_id: str, rel_path: str, media_type: str, missing: str,
    cache: bool = False,
) -> Response:
    _validate_job_id(job_id)
    try:
        data = await storage.read_file(job_id, rel_path)
    except ValueError:
        # _safe_path 对路径穿越 / 空字节(null byte)抛 ValueError;映射为 400 而非裸 500。
        raise HTTPException(400, "invalid path")
    if data is None:
        raise HTTPException(404, missing)
    headers = {}
    if cache:
        # 帧图等产物不可变(文件名含时间戳),长缓存让翻页/重访秒开,省 1Mbps 公网带宽。
        headers["Cache-Control"] = "public, max-age=604800, immutable"
    return Response(content=data, media_type=media_type, headers=headers)


async def _verified_evidence_ids(
    storage: StorageBackend, job_id: str, manifest: dict,
) -> tuple[set[str], list[str]]:
    """重算 URL tier、机械稿锚点、match 与文件完整性;不信任 manifest 自报。"""
    async def reader(rel: str) -> bytes | None:
        return await _read_verification_artifact(storage, job_id, rel)

    verified, errors = await validate_manifest_with_reader(job_id, manifest, reader)
    return set(verified), errors


async def _verified_review(
    storage: StorageBackend, job_id: str, review: dict, pipeline: str | None,
) -> dict:
    async def reader(rel: str) -> bytes | None:
        return await _read_verification_artifact(storage, job_id, rel)

    return await verify_persisted_review(
        review, job_id=job_id, pipeline=pipeline, read_file=reader,
    )


async def _read_verification_artifact(
    storage: StorageBackend, job_id: str, rel: str,
) -> bytes | None:
    """评审/取证重验统一有界读;对象大小未知时也只消费 limit+1。"""
    return await read_verification_artifact_bounded(storage, job_id, rel)


@router.get(
    "/{job_id}/notes/smart", response_class=Response, responses=_MARKDOWN_RESPONSE,
)
async def get_smart_notes(job_id: str, file: str | None = None,
                          storage: StorageBackend = Depends(get_storage)):
    """默认取最新版本智能笔记;file= 指定某版本(output/versions/notes_smart_*.md)。"""
    _validate_job_id(job_id)
    if file:
        if ".." in file or "\x00" in file or not file.startswith("output/versions/notes_smart_") or not file.endswith(".md"):
            raise HTTPException(400, "invalid version file")
        rel = file
    else:
        rel = latest_smart(await storage.list_files(job_id))
        if not rel:
            raise HTTPException(404, "smart notes not ready")
    return await _serve(storage, job_id, rel,
                        "text/markdown; charset=utf-8", "smart notes not ready")


@router.get("/{job_id}/note-versions", response_model=NoteVersionsResponse)
async def list_note_versions(
    job_id: str,
    storage: StorageBackend = Depends(get_storage),
    db: Database = Depends(get_db),
):
    """列出智能笔记各版本(provider/model/生成时间)。review.json 记录评的是哪一版 + 总分。"""
    _validate_job_id(job_id)
    files = await storage.list_files(job_id)
    job = await asyncio.to_thread(db.get_job, job_id)
    pipeline = job.pipeline if job else None
    fileset = set(files)
    versions = []
    for f in files:
        v = parse_smart_version(f)
        if not v:
            continue
        # 与本版笔记 1:1 配对的评审文件 + 其总分。
        rpath = review_path_for_note(f)
        v["review_file"] = rpath if rpath in fileset else None
        v["overall"] = None
        v["review_state"] = None
        if v["review_file"]:
            rdata = await read_file_bounded(
                storage, job_id, v["review_file"], MAX_REVIEW_SOURCE_BYTES,
            )
            if rdata:
                try:
                    parsed = json.loads(rdata)
                    verified = await _verified_review(storage, job_id, parsed, pipeline)
                    projected = project_review(verified)
                    v["overall"] = projected.get("overall")
                    v["review_state"] = projected.get("reliability_state")
                except (ValueError, json.JSONDecodeError):
                    pass
        versions.append(v)
    versions.sort(key=lambda v: v["version"], reverse=True)   # 最新在前
    return {"versions": versions}


@router.get(
    "/{job_id}/notes/mechanical", response_class=Response, responses=_MARKDOWN_RESPONSE,
)
async def get_mechanical_notes(job_id: str, storage: StorageBackend = Depends(get_storage)):
    return await _serve(storage, job_id, "output/notes_mechanical.md",
                        "text/markdown; charset=utf-8", "mechanical notes not ready")


@router.get(
    "/{job_id}/notes/transcript", response_class=Response, responses=_MARKDOWN_RESPONSE,
)
async def get_transcript(job_id: str, storage: StorageBackend = Depends(get_storage)):
    """音频/视频逐字稿(output/transcript.md)。注:前端当前无入口调用(仅笔记类型标签映射「逐字稿」),
    保留供直接拉取/将来接入。"""
    return await _serve(storage, job_id, "output/transcript.md",
                        "text/markdown; charset=utf-8", "transcript not ready")


@router.get("/{job_id}/review", response_model=ReviewProjectionResponse)
async def get_review(job_id: str, file: str | None = None,
                     storage: StorageBackend = Depends(get_storage),
                     db: Database = Depends(get_db)):
    """默认取最新评审(review.json);file= 取与某版笔记配对的版本化评审。"""
    _validate_job_id(job_id)
    if file:
        if ".." in file or "\x00" in file or not file.startswith("output/versions/review_") or not file.endswith(".json"):
            raise HTTPException(400, "invalid review file")
        rel = file
    else:
        rel = "output/review.json"
    data = await read_file_bounded(
        storage, job_id, rel, MAX_REVIEW_SOURCE_BYTES,
    )
    if data is None:
        raise HTTPException(404, "review not ready")
    try:
        artifact = json.loads(data)
    except (ValueError, json.JSONDecodeError):
        raise HTTPException(422, "review artifact is invalid")
    if not isinstance(artifact, dict):
        raise HTTPException(422, "review artifact is invalid")
    job = await asyncio.to_thread(db.get_job, job_id)
    verified = await _verified_review(
        storage, job_id, artifact, job.pipeline if job else None,
    )
    projected = project_review(verified)
    return projected


@router.get("/{job_id}/evidence", response_model=EvidenceProjectionResponse)
async def get_evidence(job_id: str, storage: StorageBackend = Depends(get_storage)):
    """权威来源 API 投影:旧版/低置信/不合格来源保留诊断但不暴露可点击 URL。"""
    _validate_job_id(job_id)
    data = await read_file_bounded(
        storage, job_id, "output/evidence.json", MAX_EVIDENCE_BYTES,
    )
    if data is None:
        raise HTTPException(404, "evidence not ready")
    try:
        manifest = json.loads(data)
    except (ValueError, json.JSONDecodeError):
        raise HTTPException(422, "evidence artifact is invalid")
    if not isinstance(manifest, dict):
        raise HTTPException(422, "evidence artifact is invalid")
    verified, validation_errors = await _verified_evidence_ids(storage, job_id, manifest)
    projected = project_evidence(
        manifest, verified_ids=verified, validation_errors=validation_errors,
    )
    return projected


@router.get(
    "/{job_id}/assets/{filename}", response_class=Response, responses=_BINARY_RESPONSE,
)
async def get_asset(job_id: str, filename: str, storage: StorageBackend = Depends(get_storage)):
    if ".." in filename or "/" in filename:
        raise HTTPException(400, "invalid filename")
    media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    return await _serve(storage, job_id, f"assets/{filename}", media_type, "asset not found",
                        cache=True)


@router.get("/{job_id}/artifacts", response_model=ArtifactsResponse)
async def list_artifacts(
    job_id: str,
    storage: StorageBackend = Depends(get_storage),
    db: Database = Depends(get_db),
    config: AppConfig = Depends(get_config),
):
    """列某 job 产物,按步骤分组。分组与文件清单来自 pipelines.yaml 各步的 outputs(单一事实源);
    job.json / 内部点文件由 _artifact_hidden 强制隐藏。"""
    _validate_job_id(job_id)
    job = await asyncio.to_thread(db.get_job, job_id)
    if not job:
        raise HTTPException(404, "job not found")
    # 一次列举拿全部 relpath→size,据此透出每文件/每步/整 job 产物体积(无须逐文件 stat)。
    sizes = await storage.list_file_sizes(job_id)
    files = [f for f in await storage.list_files(job_id) if not _artifact_hidden(f)]
    steps = config.pipelines.get(job.pipeline, {}).get("steps", [])
    assigned: set[str] = set()
    by_step: dict[str, list[str]] = {}

    def _claim(step_name: str, f: str) -> None:
        by_step.setdefault(step_name, []).append(f)
        assigned.add(f)

    # 第一轮:精确路径(无通配)的 outputs 优先认领,避免被别的步的宽 glob 抢走——
    # 例如 02_whisper 的 input/subtitle.srt 若被 01_download 的 input/*.srt 抢走,字幕会错归「下载」。
    for s in steps:
        for p in (s.get("outputs") or []):
            if not any(c in p for c in "*?[") and p in files and p not in assigned:
                _claim(s["name"], p)
    # 第二轮:glob 匹配,按步顺序认领剩余文件。
    for s in steps:
        pats = s.get("outputs") or []
        for f in files:
            if f not in assigned and any(fnmatch.fnmatch(f, p) for p in pats):
                _claim(s["name"], f)

    groups = []
    total_bytes = 0
    for s in steps:
        matched = sorted(by_step.get(s["name"], []))
        if matched:
            step_bytes = sum(sizes.get(f, 0) for f in matched)
            total_bytes += step_bytes
            groups.append({"step": s["name"], "label": s.get("label") or s["name"],
                           "total_bytes": step_bytes,
                           "files": [{"path": f, "kind": _artifact_kind(f),
                                      "size": sizes.get(f, 0)} for f in matched]})
    # total_bytes:本 job 全部已分组产物体积合计,供前端汇总产物总体积。
    return {"groups": groups, "total_bytes": total_bytes}


@router.get(
    "/{job_id}/artifact", response_class=Response, responses=_BINARY_RESPONSE,
)
async def get_artifact(job_id: str, path: str, storage: StorageBackend = Depends(get_storage)):
    """取任意产物(仅放行真实存在且未隐藏的;按扩展名定 content-type;图片长缓存)。"""
    _validate_job_id(job_id)
    if ".." in path or path.startswith("/") or "\x00" in path:
        raise HTTPException(400, "invalid path")
    files = await storage.list_files(job_id)
    if path not in files or _artifact_hidden(path):
        raise HTTPException(404, "artifact not found")
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    ct = {
        "md": "text/markdown; charset=utf-8",
        "json": "application/json; charset=utf-8",
        "txt": "text/plain; charset=utf-8",
        "srt": "text/plain; charset=utf-8",
        "html": "text/plain; charset=utf-8",  # 不渲染原始 HTML
        "ass": "text/plain; charset=utf-8",
        "log": "text/plain; charset=utf-8",
    }.get(ext) or (mimetypes.guess_type(path)[0] or "application/octet-stream")
    return await _serve(storage, job_id, path, ct, "artifact not found",
                        cache=_artifact_kind(path) == "image")


_MEDIA_CHUNK = 2 * 1024 * 1024


def _media_range(value: str | None, size: int) -> tuple[int, int, int]:
    """解析单段 bytes Range;无 Range 时返回完整流式响应."""
    if not value:
        return 0, size, 200
    if not value.startswith("bytes=") or "," in value:
        raise HTTPException(
            416, "invalid Range header",
            headers={"Content-Range": f"bytes */{size}"},
        )
    left, sep, right = value[6:].partition("-")
    if not sep:
        raise HTTPException(
            416, "invalid Range header",
            headers={"Content-Range": f"bytes */{size}"},
        )
    try:
        if not left:
            suffix = int(right)
            if suffix <= 0:
                raise ValueError
            start = max(0, size - suffix)
            end = size - 1
        else:
            start = int(left)
            end = int(right) if right else size - 1
    except ValueError:
        raise HTTPException(
            416, "invalid Range header",
            headers={"Content-Range": f"bytes */{size}"},
        )
    if size <= 0 or start < 0 or start >= size or end < start:
        raise HTTPException(
            416, "invalid Range header",
            headers={"Content-Range": f"bytes */{size}"},
        )
    end = min(end, size - 1, start + _MEDIA_CHUNK - 1)
    return start, end - start + 1, 206


@router.get(
    "/{job_id}/media", response_class=Response, responses=_RANGE_RESPONSE,
)
async def get_media(job_id: str, path: str, request: Request,
                    storage: StorageBackend = Depends(get_storage)):
    """流式返回媒体或 PDF;单段 Range 封顶 2 MiB,无 Range 不截断文件."""
    _validate_job_id(job_id)
    if ".." in path or path.startswith("/") or "\x00" in path:
        raise HTTPException(400, "invalid path")
    if _artifact_hidden(path):
        raise HTTPException(404, "media not found")
    size = await storage.file_size(job_id, path)
    if size is None:
        raise HTTPException(404, "media not found")
    ct = mimetypes.guess_type(path)[0] or "application/octet-stream"

    start, length, status = _media_range(request.headers.get("range"), size)
    stream = await storage.open_stream(
        job_id, path, start=start, length=length, chunk_size=_MEDIA_CHUNK,
    )
    if stream is None:
        raise HTTPException(404, "media not found")
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(length),
    }
    if status == 206:
        headers["Content-Range"] = f"bytes {start}-{start + length - 1}/{size}"
    return StreamingResponse(
        stream, status_code=status, media_type=ct, headers=headers,
    )
