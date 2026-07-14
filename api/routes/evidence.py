"""Canonical evidence 单条与批量安全解析端点。"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query

from api.deps import get_db, get_storage, validate_path_segment, verify_token
from api.schemas import (
    CanonicalEvidenceJobResponse,
    CanonicalEvidenceProjection,
    CanonicalEvidenceResolveRequest,
    CanonicalEvidenceResolveResponse,
)
from api.services.evidence import (
    resolve_canonical_evidence,
    resolve_canonical_evidence_batch,
)
from api.wire_schemas import API_ERROR_RESPONSES
from shared.db import Database
from shared.storage import StorageBackend


router = APIRouter(
    prefix="/api/evidence",
    tags=["evidence"],
    dependencies=[Depends(verify_token)],
    responses=API_ERROR_RESPONSES,
)


@router.get(
    "/jobs/{job_id}",
    response_model=CanonicalEvidenceJobResponse,
)
async def resolve_job_evidence(
    job_id: str,
    note_type: str | None = Query(default=None, min_length=1, max_length=64),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=100),
    db: Database = Depends(get_db),
    storage: StorageBackend = Depends(get_storage),
):
    """返回当前笔记快照的安全定位；失效项也显式返回，供详情页解释。"""
    validate_path_segment(job_id, "job_id")
    if note_type is not None:
        validate_path_segment(note_type, "note_type")
    if await asyncio.to_thread(db.get_job, job_id) is None:
        raise HTTPException(404, "job not found")
    evidence_ids = await asyncio.to_thread(
        db.canonical_evidence_ids_for_job, job_id, note_type
    )
    if not evidence_ids:
        return {"total": 0, "items": []}
    page = evidence_ids[offset:offset + limit]
    return {
        "total": len(evidence_ids),
        "items": await resolve_canonical_evidence_batch(db, storage, page)
        if page else [],
    }


@router.get(
    "/{evidence_id}/resolve",
    response_model=CanonicalEvidenceProjection,
)
async def resolve_one(
    evidence_id: str,
    db: Database = Depends(get_db),
    storage: StorageBackend = Depends(get_storage),
):
    try:
        result = await resolve_canonical_evidence(db, storage, evidence_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if result is None:
        raise HTTPException(
            404,
            {
                "code": "canonical_evidence_not_found",
                "message": "canonical evidence not found",
            },
        )
    return result


@router.post(
    "/resolve",
    response_model=CanonicalEvidenceResolveResponse,
)
async def resolve_batch(
    request: CanonicalEvidenceResolveRequest,
    db: Database = Depends(get_db),
    storage: StorageBackend = Depends(get_storage),
):
    try:
        items = await resolve_canonical_evidence_batch(
            db, storage, request.evidence_ids
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"items": items}
