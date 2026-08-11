"""把概念术语保守绑定到已验证的笔记溯源段。"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .provenance import (
    canonical_json_bytes,
    validate_provenance_manifest,
    validate_source_manifest,
)


MAX_CONCEPT_EVIDENCE_ANCHORS = 128
MAX_CONCEPT_EVIDENCE_ANCHORS_BYTES = 32 * 1024
MAX_CONCEPT_EVIDENCE_SOURCE_IDS = 500
MAX_CONCEPT_KEY_TERMS = 64
MAX_CONCEPT_TERM_BYTES = 256
MAX_CONCEPT_RELATED = 64
_CJK_RE = re.compile(r"[\u3400-\u9fff]")


@dataclass(frozen=True)
class ConceptEvidenceAnchor:
    """一次完整校验后冻结的 anchor 与来源段绑定。"""

    anchor: str
    source_segment_ids: tuple[str, ...]


@dataclass(frozen=True)
class ConceptEvidenceSnapshot:
    """prompt 与确定性绑定共用的有界 provenance 快照。"""

    anchors: tuple[ConceptEvidenceAnchor, ...]
    provenance_nonempty: bool
    truncated: bool

    def prompt_anchors(self) -> tuple[str, ...]:
        return tuple(item.anchor for item in self.anchors)


def validate_concept_evidence_snapshot(
    *,
    job_id: str,
    pipeline: str,
    note_type: str,
    note_path: str,
    note_bytes: bytes,
    normalized_body: str,
    source_manifest_path: str,
    source_manifest_data: bytes,
    provenance_data: bytes,
) -> ConceptEvidenceSnapshot:
    """完整重验 sidecars 后冻结有界 anchor;身份或预算异常直接拒绝。"""
    source_manifest = validate_source_manifest(
        _load_canonical_json(source_manifest_data, field="source manifest"),
    )
    if (
        source_manifest["job_id"] != job_id
        or source_manifest["pipeline"] != pipeline
    ):
        raise ValueError("source manifest identity mismatch")
    provenance = validate_provenance_manifest(
        _load_canonical_json(provenance_data, field="note provenance"),
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

    mappings = provenance["segments"]
    selected: dict[str, list[str]] = {}
    truncated = False
    for mapping in mappings:
        anchor = mapping["anchor"]
        if anchor in selected:
            refs = selected[anchor]
            for segment_id in mapping["source_segment_ids"]:
                if segment_id not in refs:
                    refs.append(segment_id)
            continue
        if len(selected) >= MAX_CONCEPT_EVIDENCE_ANCHORS:
            truncated = True
            continue
        candidate_anchors = [*selected, anchor]
        encoded = canonical_json_bytes({"anchors": candidate_anchors})
        if len(encoded) > MAX_CONCEPT_EVIDENCE_ANCHORS_BYTES:
            truncated = True
            continue
        selected[anchor] = list(mapping["source_segment_ids"])

    if mappings and not selected:
        raise ValueError("concept evidence anchors exceed prompt budget")
    unique_source_ids = {
        segment_id for refs in selected.values() for segment_id in refs
    }
    if len(unique_source_ids) > MAX_CONCEPT_EVIDENCE_SOURCE_IDS:
        raise ValueError("concept evidence source refs exceed binding budget")
    return ConceptEvidenceSnapshot(
        anchors=tuple(
            ConceptEvidenceAnchor(anchor=anchor, source_segment_ids=tuple(refs))
            for anchor, refs in selected.items()
        ),
        provenance_nonempty=bool(mappings),
        truncated=truncated or len(selected) < len({item["anchor"] for item in mappings}),
    )


def validate_source_concept_evidence_snapshot(
    *,
    job_id: str,
    pipeline: str,
    source_manifest_data: bytes,
) -> ConceptEvidenceSnapshot:
    """从完整来源清单冻结概念证据锚点,不信任模型自报的来源 refs。"""
    source_manifest = validate_source_manifest(
        _load_canonical_json(source_manifest_data, field="source manifest"),
    )
    if (
        source_manifest["job_id"] != job_id
        or source_manifest["pipeline"] != pipeline
    ):
        raise ValueError("source manifest identity mismatch")

    selected: dict[str, list[str]] = {}
    supported = 0
    truncated = False
    for segment in source_manifest["segments"]:
        support = segment.get("support_text")
        if not isinstance(support, str) or not support.strip():
            continue
        supported += 1
        anchor = support.strip()
        segment_id = segment["segment_id"]
        if anchor in selected:
            if segment_id not in selected[anchor]:
                selected[anchor].append(segment_id)
            continue
        if len(selected) >= MAX_CONCEPT_EVIDENCE_ANCHORS:
            truncated = True
            continue
        candidate_anchors = [*selected, anchor]
        if len(canonical_json_bytes({"anchors": candidate_anchors})) > (
            MAX_CONCEPT_EVIDENCE_ANCHORS_BYTES
        ):
            truncated = True
            continue
        selected[anchor] = [segment_id]

    if supported and not selected:
        raise ValueError("source concept evidence anchors exceed prompt budget")
    if not supported:
        raise ValueError("source manifest has no concept evidence anchors")
    unique_source_ids = {
        segment_id for refs in selected.values() for segment_id in refs
    }
    if len(unique_source_ids) > MAX_CONCEPT_EVIDENCE_SOURCE_IDS:
        raise ValueError("concept evidence source refs exceed binding budget")
    return ConceptEvidenceSnapshot(
        anchors=tuple(
            ConceptEvidenceAnchor(anchor=anchor, source_segment_ids=tuple(refs))
            for anchor, refs in selected.items()
        ),
        provenance_nonempty=True,
        truncated=truncated or len(selected) < supported,
    )


def attach_concept_source_segments(
    key_terms: Any,
    *,
    snapshot: ConceptEvidenceSnapshot,
) -> list[Any]:
    """覆盖模型自报 refs，只按同次 prompt 使用的冻结 anchors 重新绑定。"""
    terms = _copy_terms_with_empty_evidence(key_terms)
    for item in terms:
        if not isinstance(item, dict):
            continue
        candidates = _term_candidates(item)
        refs: list[str] = []
        for mapping in snapshot.anchors:
            if not any(
                _literal_term_in_anchor(candidate, mapping.anchor)
                for candidate in candidates
            ):
                continue
            for segment_id in mapping.source_segment_ids:
                if segment_id not in refs:
                    refs.append(segment_id)
        item["evidence_source_segment_ids"] = refs
    return terms


def all_concept_terms_have_evidence(key_terms: Any) -> bool:
    """判断每个概念是否都有至少一个服务端重建的来源段绑定。"""
    if type(key_terms) is not list or not key_terms:
        return False
    return all(
        isinstance(item, Mapping)
        and type(item.get("evidence_source_segment_ids")) is list
        and bool(item["evidence_source_segment_ids"])
        for item in key_terms
    )


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
