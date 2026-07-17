"""构建并校验可复算的来源分段与笔记溯源清单。"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import unicodedata
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Sequence


SOURCE_MANIFEST_SCHEMA_VERSION = 2
PROVENANCE_SCHEMA_VERSION = 3
SUPPORTED_SOURCE_MANIFEST_SCHEMA_VERSIONS = {1, SOURCE_MANIFEST_SCHEMA_VERSION}
SUPPORTED_PROVENANCE_SCHEMA_VERSIONS = {1, 2, PROVENANCE_SCHEMA_VERSION}
MAX_PROVENANCE_BYTES = 8 * 1024 * 1024
MAX_SOURCE_ARTIFACTS = 128
MAX_SOURCE_SEGMENTS = 20_000
MAX_NOTE_MAPPINGS = 20_000
MAX_SUPPORT_TEXT_BYTES = 4096
MAX_SEMANTIC_CANDIDATES = 100
MAX_SEMANTIC_ATTESTATION_PROMPT_BYTES = 64 * 1024
MAX_SEMANTIC_AI_LOG_BYTES = 2 * 1024 * 1024
MAX_SEMANTIC_AI_LOG_RECORDS = 128

DIRECT_LOCATOR_POLICY = "direct_locator_v1"
EXACT_QUOTE_POLICY = "exact_quote_v1"
SEMANTIC_ATTESTATION_POLICY = "semantic_attestation_v1"
SEMANTIC_ATTESTATION_SCHEMA_VERSION = 1
SEMANTIC_ATTESTATION_MIN_CONFIDENCE_PPM = 950_000
SEMANTIC_BATCH_COMMIT_PATH = "output/provenance/semantic_batch.json"

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_SOURCE_MANIFEST_KEYS = {
    "schema_version", "job_id", "pipeline", "source_artifacts", "segments",
}
_ARTIFACT_KEYS = {
    "source_id", "path", "sha256", "revision", "media_duration_ms", "page_count",
}
_SOURCE_SEGMENT_KEYS_V1 = {
    "segment_id", "source_id", "start", "end", "section", "locator",
}
_SOURCE_SEGMENT_KEYS_V2 = _SOURCE_SEGMENT_KEYS_V1 | {
    "support_text", "support_artifact",
}
_SUPPORT_ARTIFACT_KEYS = {"kind", "path", "sha256", "selector"}
_SUPPORT_SELECTOR_KEYS = {
    "html": {"start", "end"},
    "audio_segments": {"index"},
    "video_subtitle": {"index"},
    "video_ocr": {"entry_index", "box_index"},
    "pdf_pages": {"page", "start", "end"},
}
_PROVENANCE_KEYS = {
    "schema_version", "job_id", "note_type", "note_artifact", "note_sha256",
    "source_manifest", "source_manifest_sha256", "segments",
}
_PROVENANCE_SEGMENT_KEYS_V1 = {
    "anchor", "prefix", "suffix", "section", "source_segment_ids",
}
_PROVENANCE_SEGMENT_KEYS_V2 = _PROVENANCE_SEGMENT_KEYS_V1 | {
    "verification_policy",
}
_PROVENANCE_SEGMENT_KEYS_V3_SEMANTIC = _PROVENANCE_SEGMENT_KEYS_V2 | {
    "attestation",
}
_SEMANTIC_ATTESTATION_KEYS = {
    "schema_version", "decision", "confidence_ppm", "transform_kind",
    "candidate_id", "job_id", "note_type", "note_sha256",
    "source_manifest_sha256", "batch_id",
    "claim_sha256", "source_segment_id", "source_support_sha256",
    "source_locator_sha256", "policy_id", "policy_version",
    "producer_component", "producer_invocation_id", "attestor_component",
    "attestor_invocation_id", "attestor", "ai_log",
    "reason_codes", "critical_facts",
}
_SEMANTIC_ATTESTOR_KEYS = {"kind", "provider", "model", "prompt_sha256"}
_SEMANTIC_AI_LOG_KEYS = {
    "path", "call_index", "record_sha256", "session_id", "provider", "model",
    "step", "job_id", "prompt_user_sha256", "response_content_sha256",
    "response_decision_sha256",
}
_SEMANTIC_CRITICAL_FACT_KEYS = {
    "claim_quantity_tokens", "source_quantity_tokens",
    "claim_negation_count", "source_negation_count",
    "claim_range_tokens", "source_range_tokens",
    "claim_polarity_tokens", "source_polarity_tokens",
    "claim_subject_tokens", "source_subject_tokens",
}
_SEMANTIC_TRANSFORM_KINDS = {
    "translated", "paraphrase", "synonym", "cross_language",
}
_SEMANTIC_REASON_CODES = {"semantic_equivalent", "critical_facts_match"}
_SEMANTIC_REJECTION_CODES = {
    "semantic_mismatch", "critical_facts_conflict", "low_confidence", "unverifiable",
}
_CANDIDATE_MANIFEST_KEYS = {
    "schema_version", "status", "job_id", "note_type", "note_artifact", "note_sha256",
    "source_manifest", "source_manifest_sha256", "candidates",
}
_CANDIDATE_KEYS = {
    "candidate_id", "anchor", "prefix", "suffix", "section",
    "source_segment_id", "transform_kind", "producer_component",
    "producer_invocation_id",
}
_SEMANTIC_BATCH_KEYS = {
    "schema_version", "job_id", "pipeline", "batch_id", "attestor_component",
    "candidate_manifests", "provenance_manifests", "ai_log",
}
_SEMANTIC_BATCH_ARTIFACT_KEYS = {"note_type", "path", "sha256"}
_QUANTITY_RE = re.compile(
    r"(?<![\w.])(?P<currency>[¥￥$€£]?)\s*(?P<sign>[+\-−]?)\s*"
    r"(?P<number>\d+(?:[.,]\d+)*)\s*"
    r"(?P<unit>years?|yrs?|年|days?|天|hours?|hrs?|小时|minutes?|mins?|分钟|"
    r"milliseconds?|msecs?|ms|毫秒|seconds?|secs?|s|秒|gb|gib|mb|mib|kb|kib|bytes?|"
    r"kg|公斤|千克|mg|毫克|g|克|km|公里|千米|cm|厘米|mm|毫米|m|米|"
    r"people|persons?|人|units?|台|%|％|usd|美元|cny|rmb|人民币|元|"
    r"℃|°c|kwh|千瓦时)?",
    re.IGNORECASE,
)
_UNKNOWN_ADJACENT_UNIT_RE = re.compile(r"[A-Za-zµμ\u3400-\u9fff]{1,12}")
_UNIT_ALIASES = {
    "％": "%", "公斤": "kg", "千克": "kg", "克": "g", "毫克": "mg",
    "公里": "km", "千米": "km", "米": "m", "厘米": "cm", "毫米": "mm",
    "hr": "h", "hour": "h", "hours": "h", "小时": "h",
    "minute": "min", "minutes": "min", "分钟": "min",
    "sec": "s", "second": "s", "seconds": "s", "秒": "s",
    "millisecond": "ms", "milliseconds": "ms", "msec": "ms", "msecs": "ms", "毫秒": "ms",
    "gb": "gb", "gib": "gb", "mb": "mb", "mib": "mb", "kb": "kb", "kib": "kb",
    "byte": "byte", "bytes": "byte",
    "day": "day", "days": "day", "天": "day", "year": "year", "years": "year",
    "yr": "year", "yrs": "year", "年": "year", "people": "person",
    "person": "person", "persons": "person", "人": "person", "unit": "device",
    "units": "device", "台": "device",
    "美元": "usd", "人民币": "cny", "rmb": "cny", "元": "cny",
    "°c": "℃", "千瓦时": "kwh",
}
_NEGATION_RE = re.compile(
    r"\b(?:no|not|never|without|neither|nor|cannot|can't|doesn't|isn't|aren't|"
    r"won't|didn't|none|lacks?|absent|missing|fails?|failed|failure)\b|"
    r"没有|没|缺少|缺失|不存在|失败|"
    r"不(?:会|是|能|得|超过|低于|少于|允许|支持|包含)?|无|未|非",
    re.IGNORECASE,
)
_RANGE_PATTERNS = (
    ("<=", re.compile(r"<=|≤|\b(?:at most|no more than|does not exceed|not exceed)\b|不超过|至多" , re.I)),
    (">=", re.compile(r">=|≥|\b(?:at least|no less than)\b|不低于|至少", re.I)),
    (">", re.compile(r"(?<![<])>(?!=)|\b(?:more than|greater than|exceeds?)\b|超过|大于", re.I)),
    ("<", re.compile(r"(?<![>])<(?!=)|\b(?:less than|below)\b|低于|少于|小于", re.I)),
    ("between", re.compile(r"\bbetween\b|\bfrom\b.+?\bto\b|介于|从.+?到", re.I)),
)
_POLARITY_PATTERNS = (
    ("positive", re.compile(r"\bpositive\b|正向|阳性", re.I)),
    ("negative", re.compile(r"\bnegative\b|负向|阴性", re.I)),
    ("increase", re.compile(r"\b(?:increase[ds]?|rise[sn]?|grew|growth)\b|上升|增加|增长", re.I)),
    ("decrease", re.compile(r"\b(?:decrease[ds]?|drop(?:ped|s)?|decline[ds]?)\b|下降|减少|衰退", re.I)),
    ("pass", re.compile(r"\bpass(?:ed|es)?\b|通过|成功", re.I)),
    ("fail", re.compile(r"\bfail(?:ed|s|ure)?\b|失败", re.I)),
    ("absent", re.compile(r"\b(?:absent|missing|lacks?)\b|没有|缺失|不存在", re.I)),
)
_SUBJECT_ID_RE = re.compile(
    r"(?:\b(?i:model|system|device|server|worker|version)\b|模型|系统|设备|服务器|工作器|版本)"
    r"\s*[-_:]?[ 	]*([A-Z0-9][A-Za-z0-9._-]{0,31})",
)
_LOCATOR_KEYS = {
    "media": {"kind", "start_ms", "end_ms"},
    "pdf": {"kind", "page", "bbox"},
    "text": {"kind", "exact", "prefix", "suffix", "dom_path"},
    "image": {
        "kind", "asset_path", "asset_sha256", "bbox", "start_ms", "end_ms", "page",
    },
}


def canonical_json(value: Any) -> str:
    """返回无歧义的稳定 JSON;非 JSON 值和 NaN 直接拒绝。"""
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("value is not canonical JSON") from exc


def canonical_json_bytes(value: Any) -> bytes:
    """返回原子 writer 的权威字节表示。"""
    return (canonical_json(value) + "\n").encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def make_segment_id(
    source_id: str,
    *,
    start: int | None,
    end: int | None,
    section: str | None,
    locator: Mapping[str, Any],
) -> str:
    """从稳定的来源坐标生成内容无关的分段 ID。"""
    _require_id(source_id, "source_id")
    _require_optional_range(start, end, "segment")
    _require_optional_text(section, "section", allow_empty=False)
    payload = {
        "source_id": source_id,
        "start": start,
        "end": end,
        "section": section,
        "locator": dict(locator),
    }
    return "seg_" + sha256_bytes(canonical_json(payload).encode("utf-8"))


def build_source_manifest(
    *,
    job_id: str,
    pipeline: str,
    source_artifacts: Sequence[Mapping[str, Any]],
    segments: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """构建严格 v2 来源清单;不会推断媒体时长或 PDF 页数。"""
    normalized_segments = [
        {
            **dict(segment),
            "support_text": segment.get("support_text"),
            "support_artifact": segment.get("support_artifact"),
        }
        for segment in segments
    ]
    manifest = _json_copy({
        "schema_version": SOURCE_MANIFEST_SCHEMA_VERSION,
        "job_id": job_id,
        "pipeline": pipeline,
        "source_artifacts": list(source_artifacts),
        "segments": normalized_segments,
    })
    validate_source_manifest(manifest)
    return manifest


def validate_source_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """严格校验来源、真实 extent、locator union 和引用完整性。"""
    _require_mapping(manifest, "source manifest")
    _require_exact_keys(manifest, _SOURCE_MANIFEST_KEYS, "source manifest")
    schema_version = _require_schema_version(
        manifest["schema_version"], "source manifest schema_version",
    )
    if schema_version not in SUPPORTED_SOURCE_MANIFEST_SCHEMA_VERSIONS:
        raise ValueError("unsupported source manifest schema_version")
    _require_id(manifest["job_id"], "job_id")
    _require_id(manifest["pipeline"], "pipeline")
    artifacts = _require_list(manifest["source_artifacts"], "source_artifacts", nonempty=True)
    segments = _require_list(manifest["segments"], "segments", nonempty=True)
    if len(artifacts) > MAX_SOURCE_ARTIFACTS:
        raise ValueError("source_artifacts exceeds limit")
    if len(segments) > MAX_SOURCE_SEGMENTS:
        raise ValueError("source segments exceeds limit")

    artifacts_by_id: dict[str, Mapping[str, Any]] = {}
    for index, artifact in enumerate(artifacts):
        label = f"source_artifacts[{index}]"
        _require_mapping(artifact, label)
        _require_exact_keys(artifact, _ARTIFACT_KEYS, label)
        source_id = _require_id(artifact["source_id"], f"{label}.source_id")
        if source_id in artifacts_by_id:
            raise ValueError(f"duplicate source_id: {source_id}")
        _require_relative_path(artifact["path"], f"{label}.path")
        _require_sha256(artifact["sha256"], f"{label}.sha256")
        _require_optional_text(artifact["revision"], f"{label}.revision", allow_empty=False)
        _require_optional_positive_int(
            artifact["media_duration_ms"], f"{label}.media_duration_ms",
        )
        _require_optional_positive_int(artifact["page_count"], f"{label}.page_count")
        artifacts_by_id[source_id] = artifact

    segment_ids: set[str] = set()
    for index, segment in enumerate(segments):
        label = f"segments[{index}]"
        _require_mapping(segment, label)
        segment_keys = (
            _SOURCE_SEGMENT_KEYS_V2
            if schema_version >= 2
            else _SOURCE_SEGMENT_KEYS_V1
        )
        _require_exact_keys(segment, segment_keys, label)
        segment_id = _require_id(segment["segment_id"], f"{label}.segment_id")
        if segment_id in segment_ids:
            raise ValueError(f"duplicate segment_id: {segment_id}")
        segment_ids.add(segment_id)
        source_id = _require_id(segment["source_id"], f"{label}.source_id")
        artifact = artifacts_by_id.get(source_id)
        if artifact is None:
            raise ValueError(f"unknown source_id: {source_id}")
        _require_optional_text(segment["section"], f"{label}.section", allow_empty=False)
        _validate_locator(segment["locator"], artifact, f"{label}.locator")
        kind = segment["locator"]["kind"]
        if kind == "text":
            _require_range(segment["start"], segment["end"], label)
        else:
            _require_optional_range(segment["start"], segment["end"], label)
        if schema_version >= 2:
            support_text = _require_support_text(
                segment["support_text"], f"{label}.support_text",
            )
            support_artifact = _validate_support_artifact(
                segment["support_artifact"], segment, artifact, label,
            )
            if (support_text is None) != (support_artifact is None):
                raise ValueError(
                    f"{label} support_text and support_artifact must be both null or non-null"
                )

    result = dict(manifest)
    if len(canonical_json_bytes(result)) > MAX_PROVENANCE_BYTES:
        raise ValueError("source manifest exceeds size limit")
    return result


def validate_locator(
    locator: Mapping[str, Any], source_artifact: Mapping[str, Any]
) -> dict[str, Any]:
    """按 provenance 的唯一 locator 规则校验读时投影。"""
    _require_mapping(locator, "locator")
    _require_mapping(source_artifact, "source_artifact")
    _validate_locator(locator, source_artifact, "locator")
    return _json_copy(locator)


def build_provenance_manifest(
    *,
    job_id: str,
    note_type: str,
    note_artifact: str,
    note_bytes: bytes,
    normalized_body: str,
    source_manifest_path: str,
    source_manifest: Mapping[str, Any],
    segments: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """绑定最终笔记字节、来源清单字节与唯一锚点。"""
    if type(note_bytes) is not bytes:
        raise ValueError("note_bytes must be bytes")
    if type(normalized_body) is not str:
        raise ValueError("normalized_body must be a string")
    validated_source = validate_source_manifest(source_manifest)
    normalized_segments = [{
        **dict(segment),
        "verification_policy": segment.get(
            "verification_policy", DIRECT_LOCATOR_POLICY,
        ),
    } for segment in segments]
    uses_semantic_attestation = any(
        segment.get("verification_policy") == SEMANTIC_ATTESTATION_POLICY
        or "attestation" in segment
        for segment in normalized_segments
    )
    manifest = _json_copy({
        "schema_version": (
            PROVENANCE_SCHEMA_VERSION if uses_semantic_attestation else 2
        ),
        "job_id": job_id,
        "note_type": note_type,
        "note_artifact": note_artifact,
        "note_sha256": sha256_bytes(note_bytes),
        "source_manifest": source_manifest_path,
        "source_manifest_sha256": sha256_bytes(canonical_json_bytes(validated_source)),
        "segments": normalized_segments,
    })
    validate_provenance_manifest(
        manifest,
        source_manifest=validated_source,
        note_bytes=note_bytes,
        normalized_body=normalized_body,
    )
    return manifest


def validate_provenance_manifest(
    manifest: Mapping[str, Any],
    *,
    source_manifest: Mapping[str, Any],
    note_bytes: bytes,
    normalized_body: str,
) -> dict[str, Any]:
    """以实际来源清单和最终笔记复算 hash、引用和锚点。"""
    _require_mapping(manifest, "provenance manifest")
    _require_exact_keys(manifest, _PROVENANCE_KEYS, "provenance manifest")
    schema_version = _require_schema_version(
        manifest["schema_version"], "provenance schema_version",
    )
    job_id = _require_id(manifest["job_id"], "job_id")
    note_type = _require_id(manifest["note_type"], "note_type")
    _require_relative_path(manifest["note_artifact"], "note_artifact")
    _require_sha256(manifest["note_sha256"], "note_sha256")
    _require_relative_path(manifest["source_manifest"], "source_manifest")
    _require_sha256(manifest["source_manifest_sha256"], "source_manifest_sha256")
    if type(note_bytes) is not bytes:
        raise ValueError("note_bytes must be bytes")
    if type(normalized_body) is not str:
        raise ValueError("normalized_body must be a string")

    validated_source = validate_source_manifest(source_manifest)
    if validated_source["job_id"] != job_id:
        raise ValueError("source manifest belongs to another job")
    expected_source_sha = sha256_bytes(canonical_json_bytes(validated_source))
    if manifest["source_manifest_sha256"] != expected_source_sha:
        raise ValueError("source_manifest_sha256 mismatch")
    expected_note_sha = sha256_bytes(note_bytes)
    if manifest["note_sha256"] != expected_note_sha:
        raise ValueError("note_sha256 mismatch")

    known_segments = {
        item["segment_id"]: item for item in validated_source["segments"]
    }
    # 空列表表示 producer 已审计该笔记,但没有可证明的来源映射。
    # 这与整个 provenance sidecar 缺失的 legacy 状态不同。
    segments = _require_list(manifest["segments"], "segments", nonempty=False)
    if schema_version == 1 and note_type == "smart" and segments:
        raise ValueError("legacy smart provenance mappings are not trusted")
    if len(segments) > MAX_NOTE_MAPPINGS:
        raise ValueError("provenance segments exceeds limit")
    seen_items: set[str] = set()
    for index, segment in enumerate(segments):
        label = f"segments[{index}]"
        _require_mapping(segment, label)
        if (
            schema_version >= 3
            and (
                segment.get("verification_policy") == SEMANTIC_ATTESTATION_POLICY
                or "attestation" in segment
            )
        ):
            segment_keys = _PROVENANCE_SEGMENT_KEYS_V3_SEMANTIC
        elif schema_version >= 2:
            segment_keys = _PROVENANCE_SEGMENT_KEYS_V2
        else:
            segment_keys = _PROVENANCE_SEGMENT_KEYS_V1
        _require_exact_keys(segment, segment_keys, label)
        anchor = _require_nonempty_text(segment["anchor"], f"{label}.anchor")
        prefix = _require_text(segment["prefix"], f"{label}.prefix")
        suffix = _require_text(segment["suffix"], f"{label}.suffix")
        _require_optional_text(segment["section"], f"{label}.section", allow_empty=False)
        refs = _require_list(
            segment["source_segment_ids"], f"{label}.source_segment_ids", nonempty=True,
        )
        seen_refs: set[str] = set()
        for ref_index, ref in enumerate(refs):
            ref = _require_id(ref, f"{label}.source_segment_ids[{ref_index}]")
            if ref in seen_refs:
                raise ValueError(f"duplicate source segment ref: {ref}")
            if ref not in known_segments:
                raise ValueError(f"unknown source segment ref: {ref}")
            seen_refs.add(ref)
        if schema_version >= 2:
            policy = segment["verification_policy"]
            if policy not in {
                DIRECT_LOCATOR_POLICY,
                EXACT_QUOTE_POLICY,
                SEMANTIC_ATTESTATION_POLICY,
            }:
                raise ValueError(f"{label}.verification_policy is unsupported")
            if note_type == "translated" and policy != SEMANTIC_ATTESTATION_POLICY:
                raise ValueError(
                    f"{label} translated provenance requires cross-language attestation"
                )
            if note_type == "smart" and policy not in {
                EXACT_QUOTE_POLICY, SEMANTIC_ATTESTATION_POLICY,
            }:
                raise ValueError(
                    f"{label} smart mapping requires exact_quote_v1 or semantic attestation"
                )
            if policy == EXACT_QUOTE_POLICY:
                validate_exact_quote_mapping(segment, validated_source, field=label)
            elif policy == SEMANTIC_ATTESTATION_POLICY:
                if schema_version < 3:
                    raise ValueError(f"{label} semantic attestation requires provenance v3")
                validate_semantic_attestation_mapping(
                    segment, validated_source, field=label,
                )
        _require_unique_anchor(normalized_body, anchor, prefix, suffix, label)
        fingerprint = canonical_json(segment)
        if fingerprint in seen_items:
            raise ValueError(f"duplicate provenance segment: {index}")
        seen_items.add(fingerprint)
    result = dict(manifest)
    if len(canonical_json_bytes(result)) > MAX_PROVENANCE_BYTES:
        raise ValueError("provenance manifest exceeds size limit")
    return result


def normalize_exact_quote_text(value: str) -> str:
    """只折叠表现差异,不做翻译、词形或标点等语义改写。"""
    value = _require_text(value, "exact quote text")
    value = unicodedata.normalize("NFC", value)
    return re.sub(r"\s+", " ", value).strip()


def bounded_support_text(value: Any) -> str | None:
    """只保留完整且有界的 producer 原文;超限时不发布部分真相。"""
    if type(value) is not str:
        return None
    value = value.strip()
    if not value or len(value.encode("utf-8")) > MAX_SUPPORT_TEXT_BYTES:
        return None
    return value


def validate_exact_quote_mapping(
    mapping: Mapping[str, Any],
    source_manifest: Mapping[str, Any],
    *,
    field: str = "provenance segment",
) -> None:
    """复算 claim 是否由同一来源的连续 support segment 逐字支撑。"""
    if source_manifest.get("schema_version") != SOURCE_MANIFEST_SCHEMA_VERSION:
        raise ValueError(f"{field} exact quote requires source manifest v2")
    anchor = normalize_exact_quote_text(mapping.get("anchor"))
    if not anchor or not any(char.isalpha() for char in anchor):
        raise ValueError(f"{field}.anchor is not an exact textual claim")
    refs = mapping.get("source_segment_ids")
    if type(refs) is not list or not refs:
        raise ValueError(f"{field}.source_segment_ids must not be empty")
    if len(refs) != 1:
        raise ValueError(f"{field} exact quote requires exactly one source segment")

    segments = source_manifest.get("segments")
    if type(segments) is not list:
        raise ValueError(f"{field} source manifest segments are invalid")
    by_id = {item.get("segment_id"): (index, item)
             for index, item in enumerate(segments) if isinstance(item, Mapping)}
    selected: list[tuple[int, Mapping[str, Any]]] = []
    for ref in refs:
        found = by_id.get(ref)
        if found is None:
            raise ValueError(f"{field} references an unknown support segment")
        selected.append(found)
    supports: list[str] = []
    for _, item in selected:
        support = item.get("support_text")
        if type(support) is not str:
            raise ValueError(f"{field} exact quote source has no support_text")
        normalized = normalize_exact_quote_text(support)
        if not normalized:
            raise ValueError(f"{field} exact quote source has empty support_text")
        supports.append(normalized)
    combined = supports[0]
    match_starts: list[int] = []
    cursor = 0
    while True:
        found = combined.find(anchor, cursor)
        if found < 0:
            break
        match_starts.append(found)
        cursor = found + 1
    if not match_starts:
        raise ValueError(f"{field}.anchor is not contained in support_text")


def build_semantic_attestation_mapping(
    *,
    anchor: str,
    prefix: str,
    suffix: str,
    section: str | None,
    source_segment_id: str,
    source_manifest: Mapping[str, Any],
    transform_kind: str,
    producer_component: str,
    producer_invocation_id: str,
    candidate_id: str,
    job_id: str,
    note_type: str,
    note_sha256: str,
    source_manifest_sha256: str,
    batch_id: str,
    attestor_component: str,
    attestor_invocation_id: str,
    attestor_provider: str,
    attestor_model: str,
    attestor_prompt: str,
    ai_log_binding: Mapping[str, Any],
    decision: str,
    confidence_ppm: int,
    reason_codes: Sequence[str],
) -> dict[str, Any]:
    """把独立调用的判断绑定到实际 claim 与 canonical source,不接收自报 hash。"""
    validated_source = validate_source_manifest(source_manifest)
    candidate_id = _require_id(candidate_id, "semantic candidate_id")
    if not candidate_id.startswith("cand_") or _SHA256_RE.fullmatch(
        candidate_id.removeprefix("cand_")
    ) is None:
        raise ValueError("semantic candidate_id is invalid")
    job_id = _require_id(job_id, "semantic job_id")
    note_type = _require_id(note_type, "semantic note_type")
    if note_type not in {"smart", "translated"}:
        raise ValueError("semantic note_type is unsupported")
    _require_sha256(note_sha256, "semantic note_sha256")
    _require_sha256(source_manifest_sha256, "semantic source_manifest_sha256")
    _require_sha256(batch_id, "semantic batch_id")
    if validated_source["job_id"] != job_id:
        raise ValueError("semantic attestation source belongs to another job")
    source_segment_id = _require_id(source_segment_id, "source_segment_id")
    segment = next(
        (
            item for item in validated_source["segments"]
            if item["segment_id"] == source_segment_id
        ),
        None,
    )
    if segment is None:
        raise ValueError("semantic attestation source segment is unknown")
    support = segment.get("support_text")
    if type(support) is not str or not support.strip():
        raise ValueError("semantic attestation source support is unavailable")
    anchor = _require_nonempty_text(anchor, "semantic attestation claim")
    _require_text(prefix, "semantic attestation prefix")
    _require_text(suffix, "semantic attestation suffix")
    _require_optional_text(section, "semantic attestation section", allow_empty=False)
    if transform_kind not in _SEMANTIC_TRANSFORM_KINDS:
        raise ValueError("semantic attestation transform kind is unsupported")
    producer_component = _require_id(producer_component, "producer component")
    attestor_component = _require_id(attestor_component, "attestor component")
    if producer_component == attestor_component:
        raise ValueError("semantic attestation requires an independent component")
    producer_invocation_id = _require_id(
        producer_invocation_id, "producer invocation id",
    )
    attestor_invocation_id = _require_id(
        attestor_invocation_id, "attestor invocation id",
    )
    if producer_invocation_id == attestor_invocation_id:
        raise ValueError("semantic attestation requires an independent invocation")
    if (
        type(attestor_provider) is not str
        or not attestor_provider.strip()
        or attestor_provider in {"unknown", "dry-run"}
        or type(attestor_model) is not str
        or not attestor_model.strip()
        or attestor_model in {"unknown", "dry-run"}
    ):
        raise ValueError("semantic attestor identity is invalid")
    attestor_prompt = _require_nonempty_text(
        attestor_prompt, "semantic attestor prompt",
    )
    if decision != "supported":
        raise ValueError("semantic attestation decision must be supported")
    if (
        type(confidence_ppm) is not int
        or not SEMANTIC_ATTESTATION_MIN_CONFIDENCE_PPM <= confidence_ppm <= 1_000_000
    ):
        raise ValueError("semantic attestation confidence is below policy")
    if type(reason_codes) is not list and not isinstance(reason_codes, tuple):
        raise ValueError("semantic attestation reason_codes must be a list")
    reasons = list(reason_codes)
    if (
        any(type(reason) is not str for reason in reasons)
        or len(reasons) != len(set(reasons))
        or set(reasons) != _SEMANTIC_REASON_CODES
    ):
        raise ValueError(
            "semantic attestation requires semantic_equivalent and critical_facts_match"
        )

    critical = _semantic_critical_facts(anchor, support)
    _require_semantic_critical_match(critical, field="semantic attestation")
    ai_log = _validate_semantic_ai_log_binding(ai_log_binding)

    mapping = {
        "anchor": anchor,
        "prefix": prefix,
        "suffix": suffix,
        "section": section,
        "source_segment_ids": [source_segment_id],
        "verification_policy": SEMANTIC_ATTESTATION_POLICY,
        "attestation": {
            "schema_version": SEMANTIC_ATTESTATION_SCHEMA_VERSION,
            "decision": decision,
            "confidence_ppm": confidence_ppm,
            "transform_kind": transform_kind,
            "candidate_id": candidate_id,
            "job_id": job_id,
            "note_type": note_type,
            "note_sha256": note_sha256,
            "source_manifest_sha256": source_manifest_sha256,
            "batch_id": batch_id,
            "claim_sha256": sha256_bytes(anchor.encode("utf-8")),
            "source_segment_id": source_segment_id,
            "source_support_sha256": sha256_bytes(support.encode("utf-8")),
            "source_locator_sha256": sha256_bytes(
                canonical_json(segment["locator"]).encode("utf-8")
            ),
            "policy_id": SEMANTIC_ATTESTATION_POLICY,
            "policy_version": 1,
            "producer_component": producer_component,
            "producer_invocation_id": producer_invocation_id,
            "attestor_component": attestor_component,
            "attestor_invocation_id": attestor_invocation_id,
            "attestor": {
                "kind": "ai_gateway_independent_call_v1",
                "provider": attestor_provider,
                "model": attestor_model,
                "prompt_sha256": sha256_bytes(attestor_prompt.encode("utf-8")),
            },
            "ai_log": ai_log,
            "reason_codes": reasons,
            "critical_facts": critical,
        },
    }
    validate_semantic_attestation_mapping(mapping, validated_source)
    return mapping


def validate_semantic_attestation_mapping(
    mapping: Mapping[str, Any],
    source_manifest: Mapping[str, Any],
    *,
    field: str = "provenance segment",
) -> None:
    """重算语义证明绑定;证明字段、策略或 canonical source 漂移即失效。"""
    if mapping.get("verification_policy") != SEMANTIC_ATTESTATION_POLICY:
        raise ValueError(f"{field}.verification_policy is unsupported")
    refs = mapping.get("source_segment_ids")
    if type(refs) is not list or len(refs) != 1:
        raise ValueError(f"{field} semantic attestation requires one source segment")
    attestation = mapping.get("attestation")
    _require_mapping(attestation, f"{field}.attestation")
    _require_exact_keys(
        attestation, _SEMANTIC_ATTESTATION_KEYS, f"{field}.attestation",
    )
    if attestation["schema_version"] != SEMANTIC_ATTESTATION_SCHEMA_VERSION:
        raise ValueError(f"{field} semantic attestation schema is unsupported")
    if attestation["decision"] != "supported":
        raise ValueError(f"{field} semantic attestation decision must be supported")
    confidence = attestation["confidence_ppm"]
    if (
        type(confidence) is not int
        or not SEMANTIC_ATTESTATION_MIN_CONFIDENCE_PPM <= confidence <= 1_000_000
    ):
        raise ValueError(f"{field} semantic attestation confidence is below policy")
    if attestation["transform_kind"] not in _SEMANTIC_TRANSFORM_KINDS:
        raise ValueError(f"{field} semantic attestation transform kind is unsupported")
    if attestation["policy_id"] != SEMANTIC_ATTESTATION_POLICY:
        raise ValueError(f"{field} semantic attestation policy is invalid")
    if attestation["policy_version"] != 1:
        raise ValueError(f"{field} semantic attestation policy version is invalid")
    candidate_id = _require_id(attestation["candidate_id"], f"{field}.candidate_id")
    if not candidate_id.startswith("cand_") or _SHA256_RE.fullmatch(
        candidate_id.removeprefix("cand_")
    ) is None:
        raise ValueError(f"{field} semantic candidate_id is invalid")
    _require_id(attestation["job_id"], f"{field}.job_id")
    note_type = _require_id(attestation["note_type"], f"{field}.note_type")
    if note_type not in {"smart", "translated"}:
        raise ValueError(f"{field} semantic note_type is unsupported")
    _require_sha256(attestation["note_sha256"], f"{field}.note_sha256")
    _require_sha256(
        attestation["source_manifest_sha256"], f"{field}.source_manifest_sha256",
    )
    _require_sha256(attestation["batch_id"], f"{field}.batch_id")
    producer_id = _require_id(
        attestation["producer_invocation_id"], f"{field}.producer_invocation_id",
    )
    producer_component = _require_id(
        attestation["producer_component"], f"{field}.producer_component",
    )
    attestor_component = _require_id(
        attestation["attestor_component"], f"{field}.attestor_component",
    )
    if producer_component == attestor_component:
        raise ValueError(f"{field} semantic attestation component is not independent")
    attestor_id = _require_id(
        attestation["attestor_invocation_id"], f"{field}.attestor_invocation_id",
    )
    if producer_id == attestor_id:
        raise ValueError(f"{field} semantic attestation is not independent")
    attestor = _require_mapping(attestation["attestor"], f"{field}.attestor")
    _require_exact_keys(attestor, _SEMANTIC_ATTESTOR_KEYS, f"{field}.attestor")
    if attestor["kind"] != "ai_gateway_independent_call_v1":
        raise ValueError(f"{field} semantic attestor kind is unsupported")
    for key in ("provider", "model"):
        value = attestor[key]
        if type(value) is not str or not value.strip() or value in {"unknown", "dry-run"}:
            raise ValueError(f"{field} semantic attestor identity is invalid")
    _require_sha256(attestor["prompt_sha256"], f"{field}.attestor.prompt_sha256")
    ai_log = _validate_semantic_ai_log_binding(
        attestation["ai_log"], field=f"{field}.ai_log",
    )
    if (
        ai_log["session_id"] != attestor_id
        or ai_log["provider"] != attestor["provider"]
        or ai_log["model"] != attestor["model"]
        or ai_log["step"] != attestor_component
        or ai_log["job_id"] != attestation["job_id"]
        or ai_log["prompt_user_sha256"] != attestor["prompt_sha256"]
    ):
        raise ValueError(f"{field} semantic ai_log identity changed")
    reasons = _require_list(
        attestation["reason_codes"], f"{field}.reason_codes", nonempty=True,
    )
    if len(reasons) != len(set(reasons)) or set(reasons) != _SEMANTIC_REASON_CODES:
        raise ValueError(f"{field} semantic attestation reason codes are invalid")

    segment_id = refs[0]
    if attestation["source_segment_id"] != segment_id:
        raise ValueError(f"{field} semantic attestation source binding changed")
    segment = next(
        (
            item for item in source_manifest.get("segments", [])
            if item.get("segment_id") == segment_id
        ),
        None,
    )
    if segment is None:
        raise ValueError(f"{field} semantic attestation source is missing")
    support = segment.get("support_text")
    if type(support) is not str or not support.strip():
        raise ValueError(f"{field} semantic attestation source support is unavailable")
    anchor = mapping.get("anchor")
    if type(anchor) is not str or not anchor.strip():
        raise ValueError(f"{field} semantic attestation claim is invalid")
    if attestation["claim_sha256"] != sha256_bytes(anchor.encode("utf-8")):
        raise ValueError(f"{field} semantic attestation claim binding changed")
    if attestation["source_support_sha256"] != sha256_bytes(support.encode("utf-8")):
        raise ValueError(f"{field} semantic attestation source support changed")
    expected_locator_sha = sha256_bytes(
        canonical_json(segment["locator"]).encode("utf-8")
    )
    if attestation["source_locator_sha256"] != expected_locator_sha:
        raise ValueError(f"{field} semantic attestation source locator changed")

    critical = _require_mapping(
        attestation["critical_facts"], f"{field}.critical_facts",
    )
    _require_exact_keys(
        critical, _SEMANTIC_CRITICAL_FACT_KEYS, f"{field}.critical_facts",
    )
    expected_critical = _semantic_critical_facts(anchor, support)
    _require_semantic_critical_match(expected_critical, field=field)
    if canonical_json(dict(critical)) != canonical_json(expected_critical):
        raise ValueError(f"{field} semantic attestation critical facts changed")


def _semantic_quantity_tokens(text: str) -> list[str]:
    result: list[str] = []
    for match in _QUANTITY_RE.finditer(text):
        sign = "-" if match.group("sign") in {"-", "−"} else match.group("sign")
        number = match.group("number").replace(",", "")
        currency = match.group("currency")
        unit = (match.group("unit") or "").lower()
        unit = _UNIT_ALIASES.get(unit, unit)
        if currency:
            unit = {"¥": "cny", "￥": "cny", "$": "usd", "€": "eur", "£": "gbp"}[currency]
        if not unit:
            adjacent = _UNKNOWN_ADJACENT_UNIT_RE.match(text, match.end())
            if adjacent is not None:
                unit = "unknown:" + adjacent.group(0).casefold()
        result.append(f"{sign}{number}{unit}")
    return result


def _semantic_negation_count(text: str) -> int:
    return sum(1 for _ in _NEGATION_RE.finditer(text))


def _semantic_pattern_tokens(
    text: str, patterns: Sequence[tuple[str, re.Pattern[str]]],
) -> list[str]:
    return sorted(token for token, pattern in patterns for _ in pattern.finditer(text))


def _semantic_subject_tokens(text: str) -> list[str]:
    return sorted(match.group(1).casefold() for match in _SUBJECT_ID_RE.finditer(text))


def _semantic_critical_facts(claim: str, support: str) -> dict[str, Any]:
    return {
        "claim_quantity_tokens": _semantic_quantity_tokens(claim),
        "source_quantity_tokens": _semantic_quantity_tokens(support),
        "claim_negation_count": _semantic_negation_count(claim),
        "source_negation_count": _semantic_negation_count(support),
        "claim_range_tokens": _semantic_pattern_tokens(claim, _RANGE_PATTERNS),
        "source_range_tokens": _semantic_pattern_tokens(support, _RANGE_PATTERNS),
        "claim_polarity_tokens": _semantic_pattern_tokens(claim, _POLARITY_PATTERNS),
        "source_polarity_tokens": _semantic_pattern_tokens(support, _POLARITY_PATTERNS),
        "claim_subject_tokens": _semantic_subject_tokens(claim),
        "source_subject_tokens": _semantic_subject_tokens(support),
    }


def _require_semantic_critical_match(critical: Mapping[str, Any], *, field: str) -> None:
    pairs = (
        ("quantity_tokens", "quantity or unit"),
        ("negation_count", "negation"),
        ("range_tokens", "range"),
        ("polarity_tokens", "polarity"),
        ("subject_tokens", "subject"),
    )
    for suffix, label in pairs:
        if critical[f"claim_{suffix}"] != critical[f"source_{suffix}"]:
            raise ValueError(f"{field} {label} conflict")


def _validate_semantic_ai_log_binding(
    value: Mapping[str, Any], *, field: str = "semantic ai_log",
) -> dict[str, Any]:
    binding = _require_mapping(value, field)
    _require_exact_keys(binding, _SEMANTIC_AI_LOG_KEYS, field)
    _require_relative_path(binding["path"], f"{field}.path")
    if not str(binding["path"]).startswith("output/ai_logs/"):
        raise ValueError(f"{field}.path is invalid")
    if type(binding["call_index"]) is not int or binding["call_index"] < 0:
        raise ValueError(f"{field}.call_index is invalid")
    for key in (
        "record_sha256", "prompt_user_sha256", "response_content_sha256",
        "response_decision_sha256",
    ):
        _require_sha256(binding[key], f"{field}.{key}")
    for key in ("session_id", "provider", "model"):
        value = binding[key]
        if type(value) is not str or not value.strip() or len(value) > 128:
            raise ValueError(f"{field}.{key} is invalid")
    for key in ("step", "job_id"):
        _require_id(binding[key], f"{field}.{key}")
    return dict(binding)


def build_provenance_candidate_manifest(
    *,
    job_id: str,
    note_type: str,
    note_artifact: str,
    note_bytes: bytes,
    normalized_body: str,
    source_manifest_path: str,
    source_manifest: Mapping[str, Any],
    candidates: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """固化 producer 候选但不接受 decision/attestor 字段。"""
    validated_source = validate_source_manifest(source_manifest)
    normalized: list[dict[str, Any]] = []
    for candidate in candidates:
        raw = dict(candidate)
        raw.pop("candidate_id", None)
        required = _CANDIDATE_KEYS - {"candidate_id"}
        _require_exact_keys(raw, required, "semantic candidate input")
        identity = {
            "job_id": job_id,
            "note_type": note_type,
            "note_sha256": sha256_bytes(note_bytes),
            **raw,
        }
        normalized.append({
            "candidate_id": "cand_" + sha256_bytes(
                canonical_json(identity).encode("utf-8")
            ),
            **raw,
        })
    manifest = _json_copy({
        "schema_version": 2,
        "status": "ready" if normalized else "empty",
        "job_id": job_id,
        "note_type": note_type,
        "note_artifact": note_artifact,
        "note_sha256": sha256_bytes(note_bytes),
        "source_manifest": source_manifest_path,
        "source_manifest_sha256": sha256_bytes(
            canonical_json_bytes(validated_source)
        ),
        "candidates": normalized,
    })
    validate_provenance_candidate_manifest(
        manifest,
        source_manifest=validated_source,
        note_bytes=note_bytes,
        normalized_body=normalized_body,
    )
    return manifest


def validate_provenance_candidate_manifest(
    manifest: Mapping[str, Any],
    *,
    source_manifest: Mapping[str, Any] | None,
    note_bytes: bytes,
    normalized_body: str,
) -> dict[str, Any]:
    """重算无信任候选的 note/source/producer 绑定并拒绝任何证明自报。"""
    _require_mapping(manifest, "semantic candidate manifest")
    _require_exact_keys(
        manifest, _CANDIDATE_MANIFEST_KEYS, "semantic candidate manifest",
    )
    if manifest["schema_version"] != 2:
        raise ValueError("semantic candidate schema is unsupported")
    if manifest["status"] not in {"ready", "empty", "no_source"}:
        raise ValueError("semantic candidate status is invalid")
    job_id = _require_id(manifest["job_id"], "semantic candidate job_id")
    note_type = _require_id(manifest["note_type"], "semantic candidate note_type")
    if note_type not in {"smart", "translated"}:
        raise ValueError("semantic candidate note_type is unsupported")
    note_artifact = _require_relative_path(
        manifest["note_artifact"], "semantic candidate note_artifact",
    )
    del note_artifact
    _require_sha256(manifest["note_sha256"], "semantic candidate note_sha256")
    _require_relative_path(
        manifest["source_manifest"], "semantic candidate source_manifest",
    )
    if manifest["status"] == "no_source":
        if manifest["source_manifest_sha256"] is not None:
            raise ValueError("no_source candidate must not bind a source hash")
    else:
        _require_sha256(
            manifest["source_manifest_sha256"],
            "semantic candidate source_manifest_sha256",
        )
    if type(note_bytes) is not bytes or type(normalized_body) is not str:
        raise ValueError("semantic candidate note input is invalid")
    if manifest["note_sha256"] != sha256_bytes(note_bytes):
        raise ValueError("semantic candidate note_sha256 mismatch")
    candidates = _require_list(
        manifest["candidates"], "semantic candidates", nonempty=False,
    )
    if manifest["status"] == "no_source":
        if source_manifest is not None or candidates:
            raise ValueError("no_source candidate tombstone is invalid")
        return dict(manifest)
    if source_manifest is None:
        raise ValueError("semantic candidate source manifest is missing")
    validated_source = validate_source_manifest(source_manifest)
    if validated_source["job_id"] != job_id:
        raise ValueError("semantic candidate source belongs to another job")
    if manifest["source_manifest_sha256"] != sha256_bytes(
        canonical_json_bytes(validated_source)
    ):
        raise ValueError("semantic candidate source_manifest_sha256 mismatch")
    source_segments = {
        item["segment_id"]: item for item in validated_source["segments"]
    }
    if len(candidates) > MAX_SEMANTIC_CANDIDATES:
        raise ValueError("semantic candidates exceed limit")
    if bool(candidates) != (manifest["status"] == "ready"):
        raise ValueError("semantic candidate status does not match candidates")
    seen_ids: set[str] = set()
    seen_pairs: set[tuple[str, str]] = set()
    for index, candidate in enumerate(candidates):
        label = f"semantic candidates[{index}]"
        _require_mapping(candidate, label)
        _require_exact_keys(candidate, _CANDIDATE_KEYS, label)
        candidate_id = candidate["candidate_id"]
        if (
            type(candidate_id) is not str
            or not candidate_id.startswith("cand_")
            or _SHA256_RE.fullmatch(candidate_id.removeprefix("cand_")) is None
        ):
            raise ValueError(f"{label}.candidate_id is invalid")
        if candidate_id in seen_ids:
            raise ValueError("semantic candidate id is duplicated")
        seen_ids.add(candidate_id)
        anchor = _require_nonempty_text(candidate["anchor"], f"{label}.anchor")
        prefix = _require_text(candidate["prefix"], f"{label}.prefix")
        suffix = _require_text(candidate["suffix"], f"{label}.suffix")
        _require_optional_text(candidate["section"], f"{label}.section", allow_empty=False)
        source_segment_id = _require_id(
            candidate["source_segment_id"], f"{label}.source_segment_id",
        )
        segment = source_segments.get(source_segment_id)
        if segment is None:
            raise ValueError(f"{label} semantic candidate source is missing")
        if type(segment.get("support_text")) is not str:
            raise ValueError(f"{label} semantic candidate source support is unavailable")
        if candidate["transform_kind"] not in _SEMANTIC_TRANSFORM_KINDS:
            raise ValueError(f"{label} semantic transform kind is unsupported")
        _require_id(candidate["producer_component"], f"{label}.producer_component")
        _require_id(
            candidate["producer_invocation_id"], f"{label}.producer_invocation_id",
        )
        _require_unique_anchor(normalized_body, anchor, prefix, suffix, label)
        pair = (anchor, source_segment_id)
        if pair in seen_pairs:
            raise ValueError("semantic candidate mapping is duplicated")
        seen_pairs.add(pair)
        expected_identity = {
            "job_id": job_id,
            "note_type": note_type,
            "note_sha256": manifest["note_sha256"],
            **{key: candidate[key] for key in _CANDIDATE_KEYS - {"candidate_id"}},
        }
        expected_id = "cand_" + sha256_bytes(
            canonical_json(expected_identity).encode("utf-8")
        )
        if candidate_id != expected_id:
            raise ValueError(f"{label} semantic candidate identity changed")
    result = dict(manifest)
    if len(canonical_json_bytes(result)) > MAX_PROVENANCE_BYTES:
        raise ValueError("semantic candidate manifest exceeds size limit")
    return result


def write_provenance_candidate_manifest(
    path: str | Path,
    manifest: Mapping[str, Any],
    *,
    trusted_root: str | Path,
    source_manifest: Mapping[str, Any],
    note_bytes: bytes,
    normalized_body: str,
) -> str:
    return write_json_atomic(
        path,
        manifest,
        trusted_root=trusted_root,
        validator=lambda value: validate_provenance_candidate_manifest(
            value,
            source_manifest=source_manifest,
            note_bytes=note_bytes,
            normalized_body=normalized_body,
        ),
    )


def semantic_attestation_batch_id(
    *,
    job_id: str,
    pipeline: str,
    attestor_component: str,
    candidate_manifests: Sequence[Mapping[str, Any]],
    ai_log: Mapping[str, Any] | None,
) -> str:
    """批次身份不依赖待写 final,避免 commit manifest 与 final 循环哈希。"""
    identity = {
        "job_id": _require_id(job_id, "semantic batch job_id"),
        "pipeline": _require_id(pipeline, "semantic batch pipeline"),
        "attestor_component": _require_id(
            attestor_component, "semantic batch attestor_component",
        ),
        "candidate_manifests": [
            {
                "note_type": _require_id(item["note_type"], "candidate note_type"),
                "path": _require_relative_path(item["path"], "candidate path"),
                "sha256": _require_sha256(item["sha256"], "candidate sha256"),
            }
            for item in sorted(candidate_manifests, key=lambda value: str(value["note_type"]))
        ],
        "ai_log": (
            None if ai_log is None else {
                key: value for key, value in _validate_semantic_ai_log_binding(ai_log).items()
                if key != "response_decision_sha256"
            }
        ),
    }
    return sha256_bytes(canonical_json_bytes(identity))


def build_semantic_batch_commit(
    *,
    job_id: str,
    pipeline: str,
    batch_id: str,
    attestor_component: str,
    candidate_manifests: Sequence[Mapping[str, Any]],
    provenance_manifests: Sequence[Mapping[str, Any]],
    ai_log: Mapping[str, Any] | None,
) -> dict[str, Any]:
    manifest = {
        "schema_version": 1,
        "job_id": job_id,
        "pipeline": pipeline,
        "batch_id": batch_id,
        "attestor_component": attestor_component,
        "candidate_manifests": list(candidate_manifests),
        "provenance_manifests": list(provenance_manifests),
        "ai_log": None if ai_log is None else dict(ai_log),
    }
    return validate_semantic_batch_commit(manifest)


def validate_semantic_batch_commit(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """验证事务提交清单形状;artifact 字节和 AI 日志由 canonical reader 重验。"""
    value = _require_mapping(manifest, "semantic batch commit")
    _require_exact_keys(value, _SEMANTIC_BATCH_KEYS, "semantic batch commit")
    if value["schema_version"] != 1:
        raise ValueError("semantic batch schema is unsupported")
    _require_id(value["job_id"], "semantic batch job_id")
    _require_id(value["pipeline"], "semantic batch pipeline")
    _require_sha256(value["batch_id"], "semantic batch_id")
    _require_id(value["attestor_component"], "semantic batch attestor_component")
    result = dict(value)
    for key in ("candidate_manifests", "provenance_manifests"):
        items = _require_list(value[key], f"semantic batch {key}", nonempty=True)
        seen: set[str] = set()
        normalized: list[dict[str, Any]] = []
        for item in items:
            entry = _require_mapping(item, f"semantic batch {key} item")
            _require_exact_keys(entry, _SEMANTIC_BATCH_ARTIFACT_KEYS, f"semantic batch {key} item")
            note_type = _require_id(entry["note_type"], f"semantic batch {key} note_type")
            if note_type not in {"smart", "translated"} or note_type in seen:
                raise ValueError(f"semantic batch {key} note_type is invalid")
            seen.add(note_type)
            normalized.append({
                "note_type": note_type,
                "path": _require_relative_path(entry["path"], f"semantic batch {key} path"),
                "sha256": _require_sha256(entry["sha256"], f"semantic batch {key} sha256"),
            })
        if [item["note_type"] for item in normalized] != sorted(seen):
            raise ValueError(f"semantic batch {key} order is not canonical")
        result[key] = normalized
    if {item["note_type"] for item in result["candidate_manifests"]} != {
        item["note_type"] for item in result["provenance_manifests"]
    }:
        raise ValueError("semantic batch artifact sets differ")
    if value["ai_log"] is None:
        result["ai_log"] = None
    else:
        result["ai_log"] = _validate_semantic_ai_log_binding(value["ai_log"])
    return _json_copy(result)


def extract_attestable_markers(
    marked_text: str,
    source_manifest: Mapping[str, Any],
    *,
    error_prefix: str,
    producer_component: str,
    producer_invocation_id: str,
    force_semantic: bool = False,
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    """移除 marker,把 exact 与待下游证明的 semantic candidate 分流。"""
    from shared.note_text import markdown_to_index_text

    validated_source = validate_source_manifest(source_manifest)
    _require_id(producer_component, "producer component")
    _require_id(producer_invocation_id, "producer invocation id")
    known = {
        str(item["segment_id"]).removeprefix("seg_"): str(item["segment_id"])
        for item in validated_source["segments"]
    }
    by_id = {
        str(item["segment_id"]): item for item in validated_source["segments"]
    }
    marker_re = re.compile(r"\[\[source:([^\]]+)\]\]")
    seen: set[str] = set()
    clean_lines: list[str] = []
    pending: list[tuple[str, list[str]]] = []
    for line in marked_text.splitlines():
        tokens = marker_re.findall(line)
        refs: list[str] = []
        for token in tokens:
            if token not in known:
                raise ValueError(f"{error_prefix} contains an unknown source marker")
            if token in seen:
                raise ValueError(f"{error_prefix} source marker is duplicated")
            seen.add(token)
            refs.append(known[token])
        clean_line = marker_re.sub("", line)
        if "[[source:" in clean_line:
            raise ValueError(f"{error_prefix} contains a malformed source marker")
        clean_line = re.sub(r"[ \t]{2,}", " ", clean_line).rstrip()
        clean_lines.append(clean_line)
        if refs:
            pending.append((markdown_to_index_text(clean_line).strip(), refs))

    cleaned = "\n".join(clean_lines)
    normalized_body = markdown_to_index_text(cleaned)
    exact: list[dict[str, Any]] = []
    semantic: list[dict[str, Any]] = []
    for anchor, refs in pending:
        if (
            not anchor
            or normalized_body.count(anchor) != 1
            or not any(char.isalpha() for char in anchor)
            or len(refs) != 1
        ):
            continue
        base = {
            "anchor": anchor,
            "prefix": "",
            "suffix": "",
            "section": "translated" if force_semantic else "smart",
            "source_segment_ids": refs,
            "verification_policy": EXACT_QUOTE_POLICY,
        }
        if not force_semantic:
            try:
                validate_exact_quote_mapping(base, validated_source)
            except ValueError:
                pass
            else:
                exact.append(base)
                continue
        support = by_id[refs[0]].get("support_text")
        if type(support) is not str or not support.strip():
            continue
        transform_kind = "translated" if force_semantic else (
            "cross_language"
            if _contains_cjk(anchor) != _contains_cjk(support)
            else "paraphrase"
        )
        semantic.append({
            "anchor": anchor,
            "prefix": "",
            "suffix": "",
            "section": "translated" if force_semantic else "smart",
            "source_segment_id": refs[0],
            "transform_kind": transform_kind,
            "producer_component": producer_component,
            "producer_invocation_id": producer_invocation_id,
        })
    return cleaned, exact, semantic


def _contains_cjk(value: str) -> bool:
    return any("\u3400" <= char <= "\u9fff" for char in value)


def build_semantic_attestation_prompt(
    candidate_manifest: Mapping[str, Any] | Sequence[Mapping[str, Any]],
    source_manifest: Mapping[str, Any],
    *,
    protocol: str,
) -> str:
    """把本批实际消费的 smart/translated 候选合成一次有界核验。

    protocol 为核验协议指令文本(tracked 模板 semantic_attestation,prompt_locked 步不吃覆盖);
    响应 schema 与 materialize_semantic_attestations 成对,改协议须同步解析器并 bump 步 version。
    """
    if not protocol.strip():
        raise ValueError("semantic attestation protocol is empty")
    manifests = (
        [candidate_manifest]
        if isinstance(candidate_manifest, Mapping)
        else list(candidate_manifest)
    )
    source_segments = {
        item["segment_id"]: item for item in source_manifest["segments"]
    }
    items = []
    for manifest in sorted(manifests, key=lambda item: str(item["note_type"])):
        for candidate in manifest["candidates"]:
            segment = source_segments[candidate["source_segment_id"]]
            items.append({
                "candidate_id": candidate["candidate_id"],
                "note_type": manifest["note_type"],
                "transform_kind": candidate["transform_kind"],
                "claim": candidate["anchor"],
                "canonical_source": segment["support_text"],
                "locator": segment["locator"],
            })
    request = canonical_json({"schema_version": 2, "items": items})
    prompt = f"{protocol.rstrip()}\n\nINPUT={request}"
    if len(prompt.encode("utf-8")) > MAX_SEMANTIC_ATTESTATION_PROMPT_BYTES:
        raise ValueError("semantic attestation prompt exceeds UTF-8 byte budget")
    return prompt


def materialize_semantic_attestations(
    candidate_manifest: Mapping[str, Any],
    source_manifest: Mapping[str, Any],
    *,
    response_text: str,
    attestor_component: str,
    attestor_invocation_id: str,
    attestor_provider: str,
    attestor_model: str,
    attestor_prompt: str,
    ai_log_binding: Mapping[str, Any],
    batch_id: str,
    response_candidate_ids: Sequence[str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """严格解析独立响应;低置信和冲突只进拒绝诊断,不生成 mapping。"""
    try:
        response = json.loads(response_text)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ValueError("semantic attestor response is not strict JSON") from exc
    if not isinstance(response, Mapping) or set(response) != {"schema_version", "decisions"}:
        raise ValueError("semantic attestor response fields are invalid")
    if response["schema_version"] not in {1, 2} or type(response["decisions"]) is not list:
        raise ValueError("semantic attestor response schema is invalid")
    candidates = candidate_manifest["candidates"]
    decisions = response["decisions"]
    expected_ids = list(response_candidate_ids or [item["candidate_id"] for item in candidates])
    if (
        len(decisions) != len(expected_ids)
        or [item.get("candidate_id") if isinstance(item, Mapping) else None for item in decisions]
        != expected_ids
    ):
        raise ValueError("semantic attestor response is incomplete")
    decision_by_id = {item["candidate_id"]: item for item in decisions}
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for candidate in candidates:
        decision = decision_by_id[candidate["candidate_id"]]
        if (
            not isinstance(decision, Mapping)
            or set(decision) != {
                "candidate_id", "decision", "confidence_ppm", "reason_codes",
            }
            or decision.get("candidate_id") != candidate["candidate_id"]
        ):
            raise ValueError("semantic attestor decision identity is invalid")
        outcome = decision.get("decision")
        confidence = decision.get("confidence_ppm")
        reasons = decision.get("reason_codes")
        if (
            outcome not in {"supported", "rejected"}
            or type(confidence) is not int
            or not 0 <= confidence <= 1_000_000
            or type(reasons) is not list
            or not reasons
            or any(type(reason) is not str for reason in reasons)
            or len(reasons) != len(set(reasons))
        ):
            raise ValueError("semantic attestor decision values are invalid")
        if outcome == "supported":
            if (
                confidence < SEMANTIC_ATTESTATION_MIN_CONFIDENCE_PPM
                or set(reasons) != _SEMANTIC_REASON_CODES
            ):
                rejected.append({
                    "candidate_id": candidate["candidate_id"],
                    "reason": "attestor_policy_rejected",
                })
                continue
            try:
                mapping = build_semantic_attestation_mapping(
                    anchor=candidate["anchor"],
                    prefix=candidate["prefix"],
                    suffix=candidate["suffix"],
                    section=candidate["section"],
                    source_segment_id=candidate["source_segment_id"],
                    source_manifest=source_manifest,
                    transform_kind=candidate["transform_kind"],
                    producer_component=candidate["producer_component"],
                    producer_invocation_id=candidate["producer_invocation_id"],
                    candidate_id=candidate["candidate_id"],
                    job_id=candidate_manifest["job_id"],
                    note_type=candidate_manifest["note_type"],
                    note_sha256=candidate_manifest["note_sha256"],
                    source_manifest_sha256=candidate_manifest["source_manifest_sha256"],
                    batch_id=batch_id,
                    attestor_component=attestor_component,
                    attestor_invocation_id=attestor_invocation_id,
                    attestor_provider=attestor_provider,
                    attestor_model=attestor_model,
                    attestor_prompt=attestor_prompt,
                    ai_log_binding={
                        **dict(ai_log_binding),
                        "response_decision_sha256": sha256_bytes(
                            canonical_json_bytes(decision)
                        ),
                    },
                    decision=outcome,
                    confidence_ppm=confidence,
                    reason_codes=reasons,
                )
            except ValueError as exc:
                rejected.append({
                    "candidate_id": candidate["candidate_id"],
                    "reason": str(exc),
                })
                continue
            accepted.append(mapping)
        else:
            if not set(reasons).issubset(_SEMANTIC_REJECTION_CODES):
                raise ValueError("semantic attestor rejection reason is invalid")
            rejected.append({
                "candidate_id": candidate["candidate_id"],
                "reason": ",".join(reasons),
            })
    return accepted, rejected


def extract_exact_quote_markers(
    marked_text: str,
    source_manifest: Mapping[str, Any],
    *,
    error_prefix: str,
) -> tuple[str, list[dict[str, Any]]]:
    """移除模型 marker,只把可复算且在笔记中唯一的整行 claim 变成候选。"""
    from shared.note_text import markdown_to_index_text

    validated_source = validate_source_manifest(source_manifest)
    known = {
        str(item["segment_id"]).removeprefix("seg_"): str(item["segment_id"])
        for item in validated_source["segments"]
    }
    marker_re = re.compile(r"\[\[source:([^\]]+)\]\]")
    seen: set[str] = set()
    clean_lines: list[str] = []
    pending: list[tuple[str, list[str]]] = []
    for line in marked_text.splitlines():
        tokens = marker_re.findall(line)
        refs: list[str] = []
        for token in tokens:
            if token not in known:
                raise ValueError(f"{error_prefix} contains an unknown source marker")
            if token in seen:
                raise ValueError(f"{error_prefix} source marker is duplicated")
            seen.add(token)
            refs.append(known[token])
        clean_line = marker_re.sub("", line)
        if "[[source:" in clean_line:
            raise ValueError(f"{error_prefix} contains a malformed source marker")
        clean_line = re.sub(r"[ \t]{2,}", " ", clean_line).rstrip()
        clean_lines.append(clean_line)
        if refs:
            anchor = markdown_to_index_text(clean_line).strip()
            pending.append((anchor, refs))

    cleaned = "\n".join(clean_lines)
    normalized_body = markdown_to_index_text(cleaned)
    candidates: list[dict[str, Any]] = []
    for anchor, refs in pending:
        candidate = {
            "anchor": anchor,
            "prefix": "",
            "suffix": "",
            "section": "smart",
            "source_segment_ids": refs,
            "verification_policy": EXACT_QUOTE_POLICY,
        }
        if not anchor or normalized_body.count(anchor) != 1:
            continue
        try:
            validate_exact_quote_mapping(candidate, validated_source)
        except ValueError:
            continue
        candidates.append(candidate)
    return cleaned, candidates


def write_json_atomic(
    path: str | Path,
    value: Any,
    *,
    trusted_root: str | Path | None = None,
    validator: Callable[[Mapping[str, Any]], Any] | None = None,
) -> str:
    """以稳定字节原子替换 JSON;可选 root 防止目录逃逸。"""
    if validator is not None:
        _require_mapping(value, "JSON document")
        validator(value)
    data = canonical_json_bytes(value)
    target = Path(path)
    if trusted_root is not None:
        root = Path(trusted_root).resolve()
        resolved = target.resolve(strict=False)
        if resolved != root and root not in resolved.parents:
            raise ValueError("target path escapes trusted root")
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_name, target)
        _fsync_directory(target.parent)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise
    return sha256_bytes(data)


def write_source_manifest(path: str | Path, manifest: Mapping[str, Any], *, trusted_root: str | Path) -> str:
    return write_json_atomic(
        path, manifest, trusted_root=trusted_root, validator=validate_source_manifest,
    )


def write_provenance_manifest(
    path: str | Path,
    manifest: Mapping[str, Any],
    *,
    trusted_root: str | Path,
    source_manifest: Mapping[str, Any],
    note_bytes: bytes,
    normalized_body: str,
) -> str:
    return write_json_atomic(
        path,
        manifest,
        trusted_root=trusted_root,
        validator=lambda value: validate_provenance_manifest(
            value,
            source_manifest=source_manifest,
            note_bytes=note_bytes,
            normalized_body=normalized_body,
        ),
    )


def _json_copy(value: Any) -> Any:
    return json.loads(canonical_json(value))


def _require_mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an object")
    if any(type(key) is not str for key in value):
        raise ValueError(f"{field} keys must be strings")
    return value


def _require_exact_keys(value: Mapping[str, Any], expected: set[str], field: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ValueError(f"{field} keys mismatch; missing={missing}, extra={extra}")


def _require_list(value: Any, field: str, *, nonempty: bool) -> list[Any]:
    if type(value) is not list:
        raise ValueError(f"{field} must be a list")
    if nonempty and not value:
        raise ValueError(f"{field} must not be empty")
    return value


def _require_id(value: Any, field: str) -> str:
    if type(value) is not str or _ID_RE.fullmatch(value) is None:
        raise ValueError(f"{field} is invalid")
    return value


def _require_schema_version(value: Any, field: str) -> int:
    if type(value) is not int or value not in SUPPORTED_PROVENANCE_SCHEMA_VERSIONS:
        raise ValueError(f"unsupported {field}")
    return value


def _require_sha256(value: Any, field: str) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{field} must be 64 lowercase hex characters")
    return value


def _require_relative_path(value: Any, field: str) -> str:
    if type(value) is not str or not value or "\\" in value:
        raise ValueError(f"{field} must be a relative POSIX path")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"{field} contains control characters")
    path = PurePosixPath(value)
    if path.is_absolute() or value != str(path) or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"{field} escapes its root or is not canonical")
    if ":" in path.parts[0]:
        raise ValueError(f"{field} must not contain a drive prefix")
    return value


def _require_text(value: Any, field: str) -> str:
    if type(value) is not str:
        raise ValueError(f"{field} must be a string")
    return value


def _require_nonempty_text(value: Any, field: str) -> str:
    value = _require_text(value, field)
    if not value or not value.strip():
        raise ValueError(f"{field} must not be empty")
    return value


def _require_optional_text(value: Any, field: str, *, allow_empty: bool) -> str | None:
    if value is None:
        return None
    value = _require_text(value, field)
    if not allow_empty and not value.strip():
        raise ValueError(f"{field} must not be empty")
    return value


def _require_support_text(value: Any, field: str) -> str | None:
    if value is None:
        return None
    value = _require_nonempty_text(value, field)
    if len(value.encode("utf-8")) > MAX_SUPPORT_TEXT_BYTES:
        raise ValueError(f"{field} exceeds {MAX_SUPPORT_TEXT_BYTES} bytes")
    return value


def _validate_support_artifact(
    value: Any,
    segment: Mapping[str, Any],
    source_artifact: Mapping[str, Any],
    field: str,
) -> Mapping[str, Any] | None:
    """校验 support 的实际文件身份和定位选择器,不接受自报文本。"""
    if value is None:
        return None
    label = f"{field}.support_artifact"
    _require_mapping(value, label)
    _require_exact_keys(value, _SUPPORT_ARTIFACT_KEYS, label)
    kind = value.get("kind")
    if kind not in _SUPPORT_SELECTOR_KEYS:
        raise ValueError(f"{label}.kind is unsupported")
    path = _require_relative_path(value.get("path"), f"{label}.path")
    sha256 = _require_sha256(value.get("sha256"), f"{label}.sha256")
    selector = value.get("selector")
    _require_mapping(selector, f"{label}.selector")
    _require_exact_keys(
        selector, _SUPPORT_SELECTOR_KEYS[str(kind)], f"{label}.selector",
    )
    locator = segment.get("locator")
    if not isinstance(locator, Mapping):
        raise ValueError(f"{label} requires a valid segment locator")

    if kind == "html":
        start = _require_nonnegative_int(selector.get("start"), f"{label}.selector.start")
        end = _require_positive_int(selector.get("end"), f"{label}.selector.end")
        if (
            locator.get("kind") != "text"
            or start != segment.get("start")
            or end != segment.get("end")
            or path != source_artifact.get("path")
            or sha256 != source_artifact.get("sha256")
        ):
            raise ValueError(f"{label} does not match the HTML locator")
    elif kind == "audio_segments":
        _require_nonnegative_int(selector.get("index"), f"{label}.selector.index")
        if locator.get("kind") != "media" or path != "intermediate/segments.json":
            raise ValueError(f"{label} does not match an audio transcript segment")
    elif kind == "video_subtitle":
        _require_nonnegative_int(selector.get("index"), f"{label}.selector.index")
        part_id = locator.get("part_id")
        expected_prefixes = {"input/"}
        if part_id:
            expected_prefixes.add(f"parts/{part_id}/input/")
        if (
            locator.get("kind") != "media"
            or not any(path.startswith(prefix) for prefix in expected_prefixes)
            or not path.endswith(".srt")
        ):
            raise ValueError(f"{label} does not match a video subtitle")
    elif kind == "video_ocr":
        _require_nonnegative_int(
            selector.get("entry_index"), f"{label}.selector.entry_index",
        )
        _require_nonnegative_int(
            selector.get("box_index"), f"{label}.selector.box_index",
        )
        asset_path = str(locator.get("asset_path") or "")
        if asset_path.startswith("parts/"):
            part_prefix = asset_path.split("/assets/", 1)[0]
            expected_paths = {
                "intermediate/ocr.json",
                f"{part_prefix}/intermediate/ocr.json",
            }
        else:
            expected_paths = {"intermediate/ocr.json"}
        if locator.get("kind") != "image" or path not in expected_paths:
            raise ValueError(f"{label} does not match video OCR")
    else:
        page = _require_positive_int(selector.get("page"), f"{label}.selector.page")
        start = _require_nonnegative_int(
            selector.get("start"), f"{label}.selector.start",
        )
        end = _require_nonnegative_int(selector.get("end"), f"{label}.selector.end")
        if (
            locator.get("kind") != "pdf"
            or page != locator.get("page")
            or path != "intermediate/pdf_page_support.json"
            or start >= end
        ):
            raise ValueError(f"{label} does not match the PDF page")
    return value


def _require_positive_int(value: Any, field: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _require_nonnegative_int(value: Any, field: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return value


def _require_optional_positive_int(value: Any, field: str) -> int | None:
    if value is None:
        return None
    return _require_positive_int(value, field)


def _require_range(start: Any, end: Any, field: str) -> tuple[int, int]:
    if type(start) is not int or type(end) is not int or start < 0 or end <= start:
        raise ValueError(f"{field} range must satisfy 0 <= start < end")
    return start, end


def _require_optional_range(
    start: Any, end: Any, field: str,
) -> tuple[int, int] | tuple[None, None]:
    if start is None and end is None:
        return None, None
    if (start is None) != (end is None):
        raise ValueError(f"{field} range must both be null or both be set")
    return _require_range(start, end, field)


def _require_bbox(value: Any, field: str) -> list[int | float]:
    if type(value) is not list or len(value) != 4:
        raise ValueError(f"{field} must contain four coordinates")
    coordinates: list[int | float] = []
    for coordinate in value:
        if type(coordinate) not in {int, float} or coordinate < 0:
            raise ValueError(f"{field} coordinates must be finite non-negative numbers")
        if type(coordinate) is float and not math.isfinite(coordinate):
            raise ValueError(f"{field} coordinates must be finite non-negative numbers")
        coordinates.append(coordinate)
    if coordinates[2] <= coordinates[0] or coordinates[3] <= coordinates[1]:
        raise ValueError(f"{field} must satisfy x1 > x0 and y1 > y0")
    return coordinates


def _validate_locator(locator: Any, artifact: Mapping[str, Any], field: str) -> None:
    _require_mapping(locator, field)
    kind = locator.get("kind")
    if kind not in _LOCATOR_KEYS:
        raise ValueError(f"{field}.kind is unsupported")
    if kind == "media" and "part_id" in locator:
        allowed = {*_LOCATOR_KEYS[kind], "part_id"}
        timeline = {"timeline_start_ms", "timeline_end_ms"}
        if frozenset(locator) not in {frozenset(allowed), frozenset(allowed | timeline)}:
            raise ValueError(f"{field} has unexpected keys")
        _require_id(locator["part_id"], f"{field}.part_id")
        if timeline <= set(locator):
            _require_range(
                locator["timeline_start_ms"], locator["timeline_end_ms"],
                f"{field}.timeline",
            )
    else:
        _require_exact_keys(locator, _LOCATOR_KEYS[kind], field)
    if kind == "media":
        start, end = _require_range(locator["start_ms"], locator["end_ms"], field)
        duration = artifact["media_duration_ms"]
        if duration is None:
            raise ValueError(f"{field} requires a measured media_duration_ms")
        if end > duration:
            raise ValueError(f"{field} exceeds media_duration_ms")
    elif kind == "pdf":
        page = _require_positive_int(locator["page"], f"{field}.page")
        page_count = artifact["page_count"]
        if page_count is None:
            raise ValueError(f"{field} requires a measured page_count")
        if page > page_count:
            raise ValueError(f"{field}.page exceeds page_count")
        if locator["bbox"] is not None:
            _require_bbox(locator["bbox"], f"{field}.bbox")
    elif kind == "text":
        _require_nonempty_text(locator["exact"], f"{field}.exact")
        _require_optional_text(locator["prefix"], f"{field}.prefix", allow_empty=True)
        _require_optional_text(locator["suffix"], f"{field}.suffix", allow_empty=True)
        _require_optional_text(locator["dom_path"], f"{field}.dom_path", allow_empty=False)
    else:
        _require_relative_path(locator["asset_path"], f"{field}.asset_path")
        _require_sha256(locator["asset_sha256"], f"{field}.asset_sha256")
        _require_bbox(locator["bbox"], f"{field}.bbox")
        start_ms = locator["start_ms"]
        end_ms = locator["end_ms"]
        if (start_ms is None) != (end_ms is None):
            raise ValueError(f"{field} media coordinates must both be null or both be set")
        if start_ms is not None:
            _, end = _require_range(start_ms, end_ms, field)
            duration = artifact["media_duration_ms"]
            if duration is None:
                raise ValueError(f"{field} requires a measured media_duration_ms")
            if end > duration:
                raise ValueError(f"{field} exceeds media_duration_ms")
        page = locator["page"]
        if page is not None:
            page = _require_positive_int(page, f"{field}.page")
            page_count = artifact["page_count"]
            if page_count is None:
                raise ValueError(f"{field} requires a measured page_count")
            if page > page_count:
                raise ValueError(f"{field}.page exceeds page_count")


def _require_unique_anchor(text: str, anchor: str, prefix: str, suffix: str, field: str) -> None:
    matches: list[int] = []
    offset = 0
    while True:
        index = text.find(anchor, offset)
        if index < 0:
            break
        before = text[:index]
        after = text[index + len(anchor):]
        if before.endswith(prefix) and after.startswith(suffix):
            matches.append(index)
        offset = index + 1
    if not matches:
        raise ValueError(f"{field}.anchor does not match normalized_text")
    if len(matches) != 1:
        raise ValueError(f"{field}.anchor is ambiguous in normalized_text")


def _fsync_directory(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
