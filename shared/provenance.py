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


PROVENANCE_SCHEMA_VERSION = 2
SUPPORTED_PROVENANCE_SCHEMA_VERSIONS = {1, PROVENANCE_SCHEMA_VERSION}
MAX_PROVENANCE_BYTES = 8 * 1024 * 1024
MAX_SOURCE_ARTIFACTS = 128
MAX_SOURCE_SEGMENTS = 20_000
MAX_NOTE_MAPPINGS = 20_000
MAX_SUPPORT_TEXT_BYTES = 4096

DIRECT_LOCATOR_POLICY = "direct_locator_v1"
EXACT_QUOTE_POLICY = "exact_quote_v1"

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
    "pdf_pages": {"page"},
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
        "schema_version": PROVENANCE_SCHEMA_VERSION,
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
    manifest = _json_copy({
        "schema_version": PROVENANCE_SCHEMA_VERSION,
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
    if note_type == "translated" and segments:
        raise ValueError("translated provenance requires cross-language attestation")
    if len(segments) > MAX_NOTE_MAPPINGS:
        raise ValueError("provenance segments exceeds limit")
    seen_items: set[str] = set()
    for index, segment in enumerate(segments):
        label = f"segments[{index}]"
        _require_mapping(segment, label)
        segment_keys = (
            _PROVENANCE_SEGMENT_KEYS_V2
            if schema_version >= 2
            else _PROVENANCE_SEGMENT_KEYS_V1
        )
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
            if policy not in {DIRECT_LOCATOR_POLICY, EXACT_QUOTE_POLICY}:
                raise ValueError(f"{label}.verification_policy is unsupported")
            if note_type == "smart" and policy != EXACT_QUOTE_POLICY:
                raise ValueError(f"{label} smart mapping requires exact_quote_v1")
            if policy == EXACT_QUOTE_POLICY:
                validate_exact_quote_mapping(segment, validated_source, field=label)
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
    if source_manifest.get("schema_version") != PROVENANCE_SCHEMA_VERSION:
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
        if (
            locator.get("kind") != "media"
            or not path.startswith("input/")
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
        if locator.get("kind") != "image" or path != "intermediate/ocr.json":
            raise ValueError(f"{label} does not match video OCR")
    else:
        page = _require_positive_int(selector.get("page"), f"{label}.selector.page")
        if (
            locator.get("kind") != "pdf"
            or page != locator.get("page")
            or path != "intermediate/pdf_page_support.json"
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
