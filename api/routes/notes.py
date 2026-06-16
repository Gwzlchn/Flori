"""笔记/截图/视频文件服务。经 StorageBackend 读，兼容本地盘与 MinIO。"""

from __future__ import annotations

import asyncio
import fnmatch
import mimetypes
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response

from shared.config import AppConfig
from shared.db import Database
from shared.storage import StorageBackend
from api.deps import get_config, get_db, get_storage, verify_token

router = APIRouter(prefix="/api/jobs", tags=["notes"], dependencies=[Depends(verify_token)])

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
    if ".." in job_id or "/" in job_id or "\x00" in job_id:
        raise HTTPException(400, "invalid job_id")


async def _serve(
    storage: StorageBackend, job_id: str, rel_path: str, media_type: str, missing: str,
    cache: bool = False,
) -> Response:
    _validate_job_id(job_id)
    data = await storage.read_file(job_id, rel_path)
    if data is None:
        raise HTTPException(404, missing)
    headers = {}
    if cache:
        # 帧图等产物不可变(文件名含时间戳),长缓存让翻页/重访秒开,省 1Mbps 公网带宽。
        headers["Cache-Control"] = "public, max-age=604800, immutable"
    return Response(content=data, media_type=media_type, headers=headers)


def _safe_provider(p: str) -> str:
    # provider 仅用于拼版本文件名,限字母数字与 -_,挡路径穿越。
    import re
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", p or ""):
        raise HTTPException(400, "invalid provider")
    return p


@router.get("/{job_id}/notes/smart")
async def get_smart_notes(job_id: str, provider: str | None = None,
                          storage: StorageBackend = Depends(get_storage)):
    # provider 指定时取该版本,否则取默认(最近一次)。
    rel = f"output/versions/smart__{_safe_provider(provider)}.md" if provider else "output/notes_smart.md"
    return await _serve(storage, job_id, rel,
                        "text/markdown; charset=utf-8", "smart notes not ready")


@router.get("/{job_id}/note-versions")
async def list_note_versions(job_id: str, storage: StorageBackend = Depends(get_storage)):
    """列出该 job 的智能笔记各 provider 版本 + 各自评分,供前端版本切换。"""
    _validate_job_id(job_id)
    import json as _json
    files = await storage.list_files(job_id)
    versions = []
    for f in files:
        if f.startswith("output/versions/smart__") and f.endswith(".md"):
            prov = f[len("output/versions/smart__"):-len(".md")]
            score = None
            rdata = await storage.read_file(job_id, f"output/versions/review__{prov}.json")
            if rdata:
                try:
                    score = _json.loads(rdata).get("overall")
                except (ValueError, _json.JSONDecodeError):
                    pass
            versions.append({"provider": prov, "overall": score})
    versions.sort(key=lambda v: v["provider"])
    return {"versions": versions}


@router.get("/{job_id}/notes/mechanical")
async def get_mechanical_notes(job_id: str, storage: StorageBackend = Depends(get_storage)):
    return await _serve(storage, job_id, "output/notes_mechanical.md",
                        "text/markdown; charset=utf-8", "mechanical notes not ready")


@router.get("/{job_id}/notes/transcript")
async def get_transcript(job_id: str, storage: StorageBackend = Depends(get_storage)):
    return await _serve(storage, job_id, "output/transcript.md",
                        "text/markdown; charset=utf-8", "transcript not ready")


@router.get("/{job_id}/review")
async def get_review(job_id: str, provider: str | None = None,
                     storage: StorageBackend = Depends(get_storage)):
    rel = f"output/versions/review__{_safe_provider(provider)}.json" if provider else "output/review.json"
    return await _serve(storage, job_id, rel, "application/json", "review not ready")


@router.get("/{job_id}/assets/{filename}")
async def get_asset(job_id: str, filename: str, storage: StorageBackend = Depends(get_storage)):
    if ".." in filename or "/" in filename:
        raise HTTPException(400, "invalid filename")
    media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    return await _serve(storage, job_id, f"assets/{filename}", media_type, "asset not found",
                        cache=True)


@router.get("/{job_id}/artifacts")
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
    files = [f for f in await storage.list_files(job_id) if not _artifact_hidden(f)]
    assigned: set[str] = set()
    groups = []
    for s in config.pipelines.get(job.pipeline, {}).get("steps", []):
        pats = s.get("outputs") or []
        matched = sorted(
            f for f in files
            if f not in assigned and any(fnmatch.fnmatch(f, p) for p in pats)
        )
        assigned.update(matched)
        if matched:
            groups.append({"step": s["name"], "label": s.get("label") or s["name"],
                           "files": [{"path": f, "kind": _artifact_kind(f)} for f in matched]})
    return {"groups": groups}


@router.get("/{job_id}/artifact")
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


_MEDIA_CHUNK = 2 * 1024 * 1024   # 单次最多回 2MB:浏览器按 range 续拉,内存不被整片视频撑爆。


@router.get("/{job_id}/media")
async def get_media(job_id: str, path: str, request: Request,
                    storage: StorageBackend = Depends(get_storage)):
    """视频/音频 range 流式(经 StorageBackend,兼容本地/MinIO)。<video>/<audio> 用它播放。
    每次最多回 _MEDIA_CHUNK,开放区间(bytes=N-)也只回一段,避免把整片视频读进内存。"""
    _validate_job_id(job_id)
    if ".." in path or path.startswith("/") or "\x00" in path:
        raise HTTPException(400, "invalid path")
    if _artifact_hidden(path):
        raise HTTPException(404, "media not found")
    size = await storage.file_size(job_id, path)
    if size is None:
        raise HTTPException(404, "media not found")
    ct = mimetypes.guess_type(path)[0] or "application/octet-stream"

    range_header = request.headers.get("range")
    if not range_header:
        # 无 range:只回首段 + Accept-Ranges,引导浏览器改用 range(不整片加载)。
        data = await storage.read_range(job_id, path, 0, min(_MEDIA_CHUNK, size))
        status = 206 if size > _MEDIA_CHUNK else 200
        headers = {"Accept-Ranges": "bytes", "Content-Length": str(len(data or b""))}
        if status == 206:
            headers["Content-Range"] = f"bytes 0-{len(data) - 1}/{size}"
        return Response(content=data or b"", status_code=status, media_type=ct, headers=headers)

    try:
        parts = range_header.replace("bytes=", "").split("-")
        start = int(parts[0]) if parts[0] else 0
        end = int(parts[1]) if len(parts) > 1 and parts[1] else size - 1
        end = min(end, size - 1, start + _MEDIA_CHUNK - 1)   # 封顶单段大小
        if start < 0 or start > end or start >= size:
            raise ValueError
        length = end - start + 1
    except (ValueError, IndexError):
        raise HTTPException(416, "invalid Range header")

    data = await storage.read_range(job_id, path, start, length)
    return Response(
        content=data or b"", status_code=206, media_type=ct,
        headers={
            "Content-Range": f"bytes {start}-{end}/{size}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(length),
        },
    )
