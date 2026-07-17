"""为文档 adapter 提供稳定身份和质量报告构造。"""

from __future__ import annotations

import hashlib
import mimetypes
from pathlib import Path
from typing import Any

from shared.document_contract import (
    DOCUMENT_SCHEMA_VERSION,
    QUALITY_SCHEMA_VERSION,
    canonicalize_document,
    stable_id,
    validate_document,
    validate_quality,
)


def sha256_fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def source_context(
    job_dir: Path,
    job: dict[str, Any],
    *,
    relative_path: str,
) -> tuple[str, str, Path, str]:
    """解析 adapter 的不可变来源身份；job 内显式 hash 不一致时拒绝。"""
    path = job_dir / relative_path
    if not path.is_file():
        raise FileNotFoundError(relative_path)
    fingerprint = sha256_fingerprint(path)
    expected = job.get("source_fingerprint")
    if expected is not None and expected != fingerprint:
        raise ValueError("document source fingerprint mismatch")
    job_id = str(job.get("job_id") or job_dir.name).strip()
    if not job_id:
        raise ValueError("document job_id is missing")
    document_kind = str(job.get("document_kind") or "unknown").strip()
    return job_id, document_kind, path, fingerprint


def base_document(
    *,
    job_id: str,
    document_kind: str,
    source_profile: str,
    capabilities: list[str],
    relative_path: str,
    source_path: Path,
    source_fingerprint: str,
    source_url: str | None,
    metadata: dict[str, Any],
    blocks: list[dict[str, Any]],
    assets: list[dict[str, Any]],
    references: list[dict[str, Any]],
    figures: list[dict[str, Any]],
    tables: list[dict[str, Any]],
    classification_method: str = "source",
    classification_confidence: float = 1.0,
) -> dict[str, Any]:
    mime_type = mimetypes.guess_type(source_path.name)[0] or "application/octet-stream"
    document = {
        "schema_version": DOCUMENT_SCHEMA_VERSION,
        "job_id": job_id,
        "content_type": "document",
        "document_kind": document_kind,
        "classification": {
            "method": classification_method,
            "confidence": classification_confidence,
        },
        "source_profile": source_profile,
        "capabilities": capabilities,
        "primary_source_id": "html" if mime_type == "text/html" else "pdf",
        "sources": [{
            "source_id": "html" if mime_type == "text/html" else "pdf",
            "source_profile": source_profile,
            "capabilities": capabilities,
            "path": relative_path,
            "url": source_url,
            "mime_type": mime_type,
            "size_bytes": source_path.stat().st_size,
            "fingerprint": source_fingerprint,
            "immutable": True,
        }],
        "metadata": metadata,
        "blocks": blocks,
        "assets": assets,
        "references": references,
        "figures": figures,
        "tables": tables,
    }
    return validate_document(
        canonicalize_document(document), expected_job_id=job_id,
    )


def quality_report(
    job_id: str,
    *,
    reasons: list[str],
    metrics: dict[str, Any],
    rejected: bool = False,
) -> dict[str, Any]:
    unique_reasons = list(dict.fromkeys(reason for reason in reasons if reason))
    status = "rejected" if rejected else ("degraded" if unique_reasons else "complete")
    return validate_quality({
        "schema_version": QUALITY_SCHEMA_VERSION,
        "job_id": job_id,
        "status": status,
        "reasons": unique_reasons,
        "metrics": metrics,
    }, expected_job_id=job_id)


def make_id(prefix: str, fingerprint: str, *parts: object) -> str:
    return stable_id(prefix, fingerprint, *(str(part) for part in parts))


def html_locator(
    fingerprint: str,
    dom_path: str,
    *,
    exact: str | None = None,
) -> dict[str, Any]:
    return {
        "html": {
            "source_id": "html",
            "source_fingerprint": fingerprint,
            "dom_path": dom_path,
            "exact": exact,
        },
    }


def pdf_locator(
    fingerprint: str,
    page: int,
    bboxes: list[list[float]],
) -> dict[str, Any]:
    return {
        "pdf": {
            "source_id": "pdf",
            "source_fingerprint": fingerprint,
            "page": page,
            "bboxes": bboxes,
        },
    }
