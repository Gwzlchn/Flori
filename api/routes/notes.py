"""笔记/截图/视频文件服务。经 StorageBackend 读，兼容本地盘与 MinIO。"""

from __future__ import annotations

import mimetypes
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse

from shared.config import AppConfig
from shared.storage import StorageBackend
from api.deps import get_config, get_storage, verify_token

router = APIRouter(prefix="/api/jobs", tags=["notes"], dependencies=[Depends(verify_token)])


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


@router.get("/{job_id}/notes/smart")
async def get_smart_notes(job_id: str, storage: StorageBackend = Depends(get_storage)):
    return await _serve(storage, job_id, "output/notes_smart.md",
                        "text/markdown; charset=utf-8", "smart notes not ready")


@router.get("/{job_id}/notes/mechanical")
async def get_mechanical_notes(job_id: str, storage: StorageBackend = Depends(get_storage)):
    return await _serve(storage, job_id, "output/notes_mechanical.md",
                        "text/markdown; charset=utf-8", "mechanical notes not ready")


@router.get("/{job_id}/notes/transcript")
async def get_transcript(job_id: str, storage: StorageBackend = Depends(get_storage)):
    return await _serve(storage, job_id, "output/transcript.md",
                        "text/markdown; charset=utf-8", "transcript not ready")


@router.get("/{job_id}/review")
async def get_review(job_id: str, storage: StorageBackend = Depends(get_storage)):
    return await _serve(storage, job_id, "output/review.json",
                        "application/json", "review not ready")


@router.get("/{job_id}/assets/{filename}")
async def get_asset(job_id: str, filename: str, storage: StorageBackend = Depends(get_storage)):
    if ".." in filename or "/" in filename:
        raise HTTPException(400, "invalid filename")
    media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    return await _serve(storage, job_id, f"assets/{filename}", media_type, "asset not found",
                        cache=True)


@router.get("/{job_id}/source")
async def get_source(job_id: str, request: Request, config: AppConfig = Depends(get_config)):
    # 视频回放仍走本地盘的 range 流式;分布式(MinIO)模式下的对象存储 range 流式为后续。
    _validate_job_id(job_id)
    video_path = config.jobs_dir / job_id / "input" / "source.mp4"
    if not video_path.exists():
        raise HTTPException(404, "source not found")

    file_size = video_path.stat().st_size
    range_header = request.headers.get("range")

    if not range_header:
        return FileResponse(video_path, media_type="video/mp4", headers={"Accept-Ranges": "bytes"})

    try:
        range_str = range_header.replace("bytes=", "")
        parts = range_str.split("-")
        start = int(parts[0]) if parts[0] else 0
        end = int(parts[1]) if len(parts) > 1 and parts[1] else file_size - 1
        end = min(end, file_size - 1)
        if start < 0 or start > end or start >= file_size:
            raise ValueError("invalid range")
        length = end - start + 1
    except (ValueError, IndexError):
        raise HTTPException(416, "invalid Range header")

    def _stream():
        with open(video_path, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(8192, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    return StreamingResponse(
        _stream(),
        status_code=206,
        media_type="video/mp4",
        headers={
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(length),
        },
    )
