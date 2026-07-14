"""Ask 本次检索来源清单与引用校验。"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any

from .evidence_contract import normalize_citation_text


ASK_SOURCE_SCHEMA_VERSION = 1
MAX_ASK_SOURCES = 20
MAX_ASK_SOURCE_BODY_CHARS = 4_000
MAX_ASK_QUESTION_CHARS = 4_000

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_CITATION_RE = re.compile(r"\[来源(?P<index>[1-9]\d*)\]")
_LOOSE_CITATION_RE = re.compile(r"\[来源[^\]\r\n]*\]")
_SOURCE_FIELDS = {
    "index", "job_id", "title", "domain", "content_type", "note_type",
    "chunk_id", "artifact_sha256", "body_sha256", "body", "section",
    "evidence", "source_fingerprint",
}
_MANIFEST_FIELDS = {
    "schema_version", "kind", "task_id", "question", "sources", "manifest_sha256",
}
_EVIDENCE_FIELDS = {
    "chunk_id", "note_type", "section", "chunk_index", "char_start", "char_end",
    "timestamp_sec", "page", "frame_path", "image_path", "snippet",
}


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalize_source_body(value: str) -> str:
    """统一 Unicode 与换行后保留正文结构,供 chunk 指纹和引用核验共用。"""
    text = unicodedata.normalize("NFC", value or "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in text.split("\n")).strip()


def normalized_body_sha256(value: str) -> str:
    """返回规范化 chunk body 的无前缀 SHA256。"""
    return _sha256_text(normalize_source_body(value))


def _required_text(value: Any, field: str, *, max_chars: int = 2_000) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{field} 必须是非空字符串")
    text = value.strip()
    if len(text) > max_chars:
        raise ValueError(f"{field} 超出长度上限")
    return text


def _optional_text(value: Any, field: str, *, max_chars: int = 2_000) -> str:
    if value is None:
        return ""
    if type(value) is not str:
        raise ValueError(f"{field} 必须是字符串")
    text = value.strip()
    if len(text) > max_chars:
        raise ValueError(f"{field} 超出长度上限")
    return text


def _sha256(value: Any, field: str) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{field} 必须是 64 位小写 SHA256")
    return value


def _safe_evidence(value: Any, chunk_id: str, note_type: str, section: str) -> dict[str, Any]:
    if value is None:
        source: dict[str, Any] = {}
    elif type(value) is dict:
        source = value
    else:
        raise ValueError("evidence 必须是对象")
    result: dict[str, Any] = {
        key: source[key]
        for key in sorted(_EVIDENCE_FIELDS)
        if key in source and source[key] is not None
    }
    result["chunk_id"] = chunk_id
    result["note_type"] = note_type
    result["section"] = section
    # 固定投影必须仍可安全进入 Redis/SQLite JSON。
    try:
        _canonical_json(result)
    except (TypeError, ValueError) as exc:
        raise ValueError("evidence 含非 JSON 安全值") from exc
    return result


def _source_fingerprint(source: dict[str, Any]) -> str:
    return _sha256_text(_canonical_json({
        "job_id": source["job_id"],
        "note_type": source["note_type"],
        "artifact_sha256": source["artifact_sha256"],
        "body_sha256": source["body_sha256"],
    }))


def _manifest_without_hash(manifest: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in manifest.items() if key != "manifest_sha256"}


def _manifest_hash(manifest: dict[str, Any]) -> str:
    return _sha256_text(_canonical_json(_manifest_without_hash(manifest)))


def build_source_manifest(task_id: str, question: str, passages: list[dict]) -> dict[str, Any]:
    """冻结 Ask 本次检索清单,绑定 task、note artifact 与 chunk body。"""
    task = _required_text(task_id, "task_id", max_chars=256)
    query = _required_text(question, "question", max_chars=MAX_ASK_QUESTION_CHARS)
    if type(passages) is not list or not passages:
        raise ValueError("passages 必须是非空列表")
    if len(passages) > MAX_ASK_SOURCES:
        raise ValueError(f"Ask 来源最多 {MAX_ASK_SOURCES} 条")

    sources = []
    identities: set[tuple[str, str, str, str]] = set()
    for index, passage in enumerate(passages, start=1):
        if type(passage) is not dict:
            raise ValueError("passage 必须是对象")
        job_id = _required_text(passage.get("job_id"), "job_id", max_chars=512)
        title = _required_text(passage.get("title"), "title", max_chars=2_000)
        domain = _optional_text(passage.get("domain"), "domain", max_chars=256)
        content_type = _optional_text(
            passage.get("content_type"), "content_type", max_chars=64,
        )
        note_type = _required_text(
            passage.get("note_type") or (passage.get("evidence") or {}).get("note_type"),
            "note_type", max_chars=128,
        )
        evidence_value = passage.get("evidence") or {}
        chunk_id = _required_text(
            passage.get("chunk_id") or evidence_value.get("chunk_id"),
            "chunk_id", max_chars=1_024,
        )
        section = _optional_text(
            passage.get("section") or evidence_value.get("section"),
            "section", max_chars=2_000,
        )
        body = normalize_source_body(
            _required_text(passage.get("body"), "body", max_chars=MAX_ASK_SOURCE_BODY_CHARS),
        )
        body_hash = normalized_body_sha256(body)
        supplied_body_hash = passage.get("body_sha256") or evidence_value.get("body_sha256")
        if supplied_body_hash not in {None, ""} and _sha256(
            supplied_body_hash, "body_sha256",
        ) != body_hash:
            raise ValueError("body_sha256 与规范化 body 不匹配")
        artifact_hash = _sha256(
            passage.get("artifact_sha256") or evidence_value.get("artifact_sha256"),
            "artifact_sha256",
        )
        identity = (job_id, note_type, artifact_hash, body_hash)
        if identity in identities:
            raise ValueError("Ask 来源身份重复")
        identities.add(identity)
        source = {
            "index": index,
            "job_id": job_id,
            "title": title,
            "domain": domain,
            "content_type": content_type,
            "note_type": note_type,
            "chunk_id": chunk_id,
            "artifact_sha256": artifact_hash,
            "body_sha256": body_hash,
            "body": body,
            "section": section,
            "evidence": _safe_evidence(evidence_value, chunk_id, note_type, section),
        }
        source["source_fingerprint"] = _source_fingerprint(source)
        sources.append(source)

    manifest: dict[str, Any] = {
        "schema_version": ASK_SOURCE_SCHEMA_VERSION,
        "kind": "ask_sources",
        "task_id": task,
        "question": query,
        "sources": sources,
    }
    manifest["manifest_sha256"] = _manifest_hash(manifest)
    return manifest


def _validate_manifest(task_id: str, manifest: Any) -> tuple[dict[int, dict[str, Any]], list[str]]:
    errors: list[str] = []
    if type(manifest) is not dict or set(manifest) != _MANIFEST_FIELDS:
        return {}, ["invalid_source_manifest"]
    if (
        manifest.get("schema_version") != ASK_SOURCE_SCHEMA_VERSION
        or manifest.get("kind") != "ask_sources"
    ):
        errors.append("invalid_source_manifest")
    if manifest.get("task_id") != task_id:
        errors.append("manifest_task_mismatch")
    sources = manifest.get("sources")
    if type(sources) is not list or not 1 <= len(sources) <= MAX_ASK_SOURCES:
        errors.append("invalid_source_manifest")
        sources = []
    try:
        if _sha256(manifest.get("manifest_sha256"), "manifest_sha256") != _manifest_hash(manifest):
            errors.append("invalid_source_manifest")
    except (TypeError, ValueError):
        errors.append("invalid_source_manifest")

    by_index: dict[int, dict[str, Any]] = {}
    for expected_index, source in enumerate(sources, start=1):
        try:
            if type(source) is not dict or set(source) != _SOURCE_FIELDS:
                raise ValueError("source fields")
            if source.get("index") != expected_index:
                raise ValueError("source index")
            _required_text(source.get("job_id"), "job_id", max_chars=512)
            _required_text(source.get("title"), "title", max_chars=2_000)
            _optional_text(source.get("domain"), "domain", max_chars=256)
            _optional_text(source.get("content_type"), "content_type", max_chars=64)
            _required_text(source.get("note_type"), "note_type", max_chars=128)
            _required_text(source.get("chunk_id"), "chunk_id", max_chars=1_024)
            _sha256(source.get("artifact_sha256"), "artifact_sha256")
            body = normalize_source_body(
                _required_text(source.get("body"), "body", max_chars=MAX_ASK_SOURCE_BODY_CHARS),
            )
            if body != source.get("body"):
                raise ValueError("body normalization")
            if _sha256(source.get("body_sha256"), "body_sha256") != normalized_body_sha256(body):
                raise ValueError("body hash")
            _optional_text(source.get("section"), "section", max_chars=2_000)
            expected_evidence = _safe_evidence(
                source.get("evidence"), source["chunk_id"], source["note_type"], source["section"],
            )
            if expected_evidence != source.get("evidence"):
                raise ValueError("evidence projection")
            if _sha256(source.get("source_fingerprint"), "source_fingerprint") != _source_fingerprint(source):
                raise ValueError("source fingerprint")
        except (KeyError, TypeError, ValueError):
            errors.append("invalid_source_manifest")
            continue
        by_index[expected_index] = source
    return by_index if not errors else {}, list(dict.fromkeys(errors))


def _claim_for_citation(answer: str, start: int) -> str:
    line_start = answer.rfind("\n", 0, start) + 1
    line_end = answer.find("\n", start)
    if line_end < 0:
        line_end = len(answer)
    line = _CITATION_RE.sub("", answer[line_start:line_end])
    return normalize_citation_text(line)


def _claim_lines(answer: str) -> list[tuple[str, bool]]:
    claims = []
    for raw in (answer or "").splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        normalized = normalize_citation_text(_CITATION_RE.sub("", stripped))
        if len(re.sub(r"[^A-Za-z\u4e00-\u9fff]", "", normalized)) < 2:
            continue
        claims.append((normalized, bool(_CITATION_RE.search(stripped))))
    return claims


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 0.0


def validate_ask_citations(task_id: str, answer: str, manifest: Any) -> dict[str, Any]:
    """按本次 source manifest 核验 [来源N],任何身份或逐字支撑缺口都不标 valid。"""
    by_index, manifest_errors = _validate_manifest(task_id, manifest)
    if manifest_errors:
        return {
            "status": "invalid", "checked": 0, "items": [],
            "errors": manifest_errors,
            "metrics": {
                "structural_precision": 0.0, "source_precision": 0.0,
                "claim_precision": 0.0, "coverage": 0.0,
            },
        }

    exact = list(_CITATION_RE.finditer(answer or ""))
    loose = list(_LOOSE_CITATION_RE.finditer(answer or ""))
    malformed = [match.group(0) for match in loose if _CITATION_RE.fullmatch(match.group(0)) is None]
    errors: list[str] = []
    if malformed:
        errors.append("malformed_citation")
    if not exact:
        errors.append("missing_citations")

    items = []
    structural_valid = 0
    source_valid = 0
    claim_valid = 0
    for match in exact:
        index = int(match.group("index"))
        source = by_index.get(index)
        item = {
            "index": index,
            "offset": match.start(),
            "source_fingerprint": source.get("source_fingerprint") if source else None,
            "claim": _claim_for_citation(answer or "", match.start()),
            "status": "valid",
            "errors": [],
        }
        structural_valid += 1
        if source is None:
            item["status"] = "invalid"
            item["errors"].append("unknown_source_index")
        else:
            source_valid += 1
            normalized_body = normalize_citation_text(source["body"])
            if not item["claim"] or item["claim"] not in normalized_body:
                item["status"] = "invalid"
                item["errors"].append("unsupported_claim")
            else:
                claim_valid += 1
        items.append(item)

    claims = _claim_lines(answer or "")
    cited_claims = sum(1 for _claim, cited in claims if cited)
    total_refs = len(exact) + len(malformed)
    metrics = {
        "structural_precision": _ratio(structural_valid, total_refs),
        "source_precision": _ratio(source_valid, len(exact)),
        "claim_precision": _ratio(claim_valid, len(exact)),
        "coverage": _ratio(cited_claims, len(claims)),
    }
    for item in items:
        errors.extend(item["errors"])
    errors = list(dict.fromkeys(errors))
    if errors:
        status = "invalid"
    elif metrics["coverage"] < 1.0:
        status = "unverified"
        errors.append("uncited_claims")
    else:
        status = "valid"
    return {
        "status": status,
        "checked": len(exact),
        "items": items,
        "errors": errors,
        "metrics": metrics,
    }


def validate_bound_ask_citations(
    task_id: str,
    answer: str,
    result_manifest: Any,
    original_manifest: Any,
) -> dict[str, Any]:
    """校验回答引用,并与任务认领时冻结的来源清单做精确绑定。"""
    result = validate_ask_citations(task_id, answer, result_manifest)
    binding_error = None
    if type(original_manifest) is not dict:
        binding_error = "source_manifest_unbound"
    elif type(result_manifest) is not dict:
        binding_error = "source_manifest_missing"
    elif result_manifest != original_manifest:
        binding_error = "source_manifest_mismatch"
    if binding_error is not None:
        result["status"] = "invalid"
        result["errors"] = list(dict.fromkeys([*result["errors"], binding_error]))
    return result
