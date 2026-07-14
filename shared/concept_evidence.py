"""把概念术语保守绑定到已验证的笔记溯源段。"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from .provenance import (
    canonical_json_bytes,
    validate_provenance_manifest,
    validate_source_manifest,
)


_CJK_RE = re.compile(r"[\u3400-\u9fff]")


def attach_concept_source_segments(
    key_terms: Any,
    *,
    job_id: str,
    pipeline: str,
    note_type: str | None,
    note_path: str,
    note_bytes: bytes,
    normalized_body: str,
    source_manifest_path: str,
    source_manifest_data: bytes | None,
    provenance_path: str | None,
    provenance_data: bytes | None,
) -> list[Any]:
    """覆盖模型自报 refs;任何身份或 hash 校验失败都返回空绑定。"""
    terms = _copy_terms_with_empty_evidence(key_terms)
    if (
        not note_type
        or not provenance_path
        or source_manifest_data is None
        or provenance_data is None
    ):
        return terms

    try:
        source_manifest = _load_canonical_json(
            source_manifest_data, field="source manifest",
        )
        provenance = _load_canonical_json(
            provenance_data, field="note provenance",
        )
        source_manifest = validate_source_manifest(source_manifest)
        if (
            source_manifest["job_id"] != job_id
            or source_manifest["pipeline"] != pipeline
        ):
            raise ValueError("source manifest identity mismatch")
        provenance = validate_provenance_manifest(
            provenance,
            source_manifest=source_manifest,
            note_bytes=note_bytes,
            normalized_body=normalized_body,
        )
        if (
            provenance["job_id"] != job_id
            or provenance["note_type"] != note_type
            or provenance["note_artifact"] != note_path
            or provenance["source_manifest"] != source_manifest_path
        ):
            raise ValueError("note provenance identity mismatch")
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        return terms

    mappings = provenance["segments"]
    for item in terms:
        if not isinstance(item, dict):
            continue
        candidates = _term_candidates(item)
        refs: list[str] = []
        for mapping in mappings:
            anchor = mapping["anchor"]
            if not any(_literal_term_in_anchor(candidate, anchor) for candidate in candidates):
                continue
            for segment_id in mapping["source_segment_ids"]:
                if segment_id not in refs:
                    refs.append(segment_id)
        item["evidence_source_segment_ids"] = refs
    return terms


def _load_canonical_json(data: bytes, *, field: str) -> Mapping[str, Any]:
    if type(data) is not bytes:
        raise TypeError(f"{field} bytes are invalid")
    value = json.loads(data.decode("utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an object")
    if canonical_json_bytes(value) != data:
        raise ValueError(f"{field} is not canonical JSON")
    return value


def _copy_terms_with_empty_evidence(key_terms: Any) -> list[Any]:
    if type(key_terms) is not list:
        return []
    result: list[Any] = []
    for item in key_terms:
        if isinstance(item, Mapping):
            copied = dict(item)
            copied["evidence_source_segment_ids"] = []
            result.append(copied)
        elif isinstance(item, str):
            result.append({
                "term": item,
                "evidence_source_segment_ids": [],
            })
    return result


def _term_candidates(item: Mapping[str, Any]) -> list[str]:
    candidates: list[str] = []
    for field in ("term", "zh_name"):
        value = item.get(field)
        if isinstance(value, str):
            value = value.strip()
            if value and value not in candidates:
                candidates.append(value)
    return candidates


def _literal_term_in_anchor(term: str, anchor: str) -> bool:
    """中文按逐字子串;Latin/数字术语要求两侧都不是 token 字符。"""
    if _CJK_RE.search(term):
        return term in anchor
    start = 0
    while True:
        index = anchor.find(term, start)
        if index < 0:
            return False
        before = anchor[index - 1] if index else ""
        end = index + len(term)
        after = anchor[end] if end < len(anchor) else ""
        if (
            (not before or not _is_token_char(before))
            and (not after or not _is_token_char(after))
        ):
            return True
        start = index + 1


def _is_token_char(value: str) -> bool:
    return value == "_" or value.isalnum()
