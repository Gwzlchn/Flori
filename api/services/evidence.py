"""Canonical evidence 读时重验与安全深链投影。"""

from __future__ import annotations

from collections import OrderedDict
import hashlib
import json
import re
from typing import Any
from urllib.parse import quote, urlencode

from shared.db import Database
from shared.evidence_contract import (
    MAX_CANONICAL_SIDECAR_BYTES,
    canonical_evidence_content_identity,
    canonical_evidence_fingerprint,
    canonical_evidence_id,
    canonical_source_fingerprint,
    locate_provenance_anchor,
    validate_canonical_locator,
)
from shared.note_text import markdown_to_index_text
from shared.provenance import (
    DIRECT_LOCATOR_POLICY,
    canonical_json_bytes,
    validate_provenance_manifest,
    validate_source_manifest,
)
from shared.source_support import (
    MAX_SUPPORT_ARTIFACT_BYTES,
    support_text_from_artifact,
)
from shared.storage import (
    StorageBackend,
    StorageObjectVersion,
    read_file_bounded,
    sha256_file,
)


_EVIDENCE_ID_RE = re.compile(r"ce_[0-9a-f]{64}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_HASH_VALIDATION_MEMO_MAX_ENTRIES = 2048
_HashValidationKey = tuple[str, str, StorageObjectVersion, str]


class _HashValidationMemo:
    """进程内有界保存对象版本与期望 SHA 的重验结果。"""

    def __init__(self, max_entries: int = _HASH_VALIDATION_MEMO_MAX_ENTRIES) -> None:
        if type(max_entries) is not int or max_entries <= 0:
            raise ValueError("max_entries must be a positive integer")
        self.max_entries = max_entries
        self.entries: OrderedDict[_HashValidationKey, bool] = OrderedDict()

    def get(self, key: _HashValidationKey) -> tuple[bool, bool]:
        try:
            value = self.entries.pop(key)
        except KeyError:
            return False, False
        self.entries[key] = value
        return True, value

    def put(self, key: _HashValidationKey, value: bool) -> None:
        self.entries.pop(key, None)
        self.entries[key] = value
        while len(self.entries) > self.max_entries:
            self.entries.popitem(last=False)


_HASH_VALIDATION_MEMO = _HashValidationMemo()


async def _object_version(
    storage: StorageBackend,
    job_id: str,
    rel_path: str,
) -> StorageObjectVersion | None:
    getter = getattr(storage, "object_version", None)
    if not callable(getter):
        return None
    try:
        value = await getter(job_id, rel_path)
    except NotImplementedError:
        return None
    if value is not None and not isinstance(value, StorageObjectVersion):
        raise ValueError("storage object version metadata is invalid")
    return value


async def _hash_matches_expected(
    storage: StorageBackend,
    job_id: str,
    rel_path: str,
    expected_sha256: str,
) -> bool | None:
    """首次流式哈希并双检对象版本;无可信版本时不使用 memo。"""
    if type(expected_sha256) is not str or _SHA256_RE.fullmatch(expected_sha256) is None:
        raise ValueError("expected sha256 is invalid")
    before = await _object_version(storage, job_id, rel_path)
    key = (
        job_id, rel_path, before, expected_sha256
    ) if before is not None else None
    if key is not None:
        found, matched = _HASH_VALIDATION_MEMO.get(key)
        if found:
            return matched

    actual = await sha256_file(storage, job_id, rel_path)
    if actual is None:
        return None
    matched = actual == expected_sha256
    if key is not None:
        after = await _object_version(storage, job_id, rel_path)
        if after != before:
            raise ValueError("artifact changed while hashing")
        _HASH_VALIDATION_MEMO.put(key, matched)
    return matched


def canonical_evidence_ids(item: dict[str, Any]) -> list[str]:
    """从 note 或 chunk 投影提取稳定 ID；畸形存量值安全降级为空。"""
    raw = item.get("canonical_evidence_ids")
    if raw is None and isinstance(item.get("evidence"), dict):
        raw = item["evidence"].get("canonical_evidence_ids")
    if not isinstance(raw, list):
        return []
    result: list[str] = []
    for value in raw:
        if (
            type(value) is str
            and _EVIDENCE_ID_RE.fullmatch(value) is not None
            and value not in result
        ):
            result.append(value)
    return result


async def attach_canonical_evidence(
    db: Database,
    storage: StorageBackend,
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """一次请求批量重验所有 item，并按各自原 ID 顺序附加安全投影。"""
    id_lists = [canonical_evidence_ids(item) for item in items]
    ordered_ids = list(dict.fromkeys(
        evidence_id for ids in id_lists for evidence_id in ids
    ))
    resolved: dict[str, dict[str, Any]] = {}
    for start in range(0, len(ordered_ids), 100):
        batch = ordered_ids[start:start + 100]
        for projection in await resolve_canonical_evidence_batch(db, storage, batch):
            resolved[str(projection["evidence_id"])] = projection
    for item, evidence_ids in zip(items, id_lists):
        item["canonical_evidence"] = [
            resolved[evidence_id]
            for evidence_id in evidence_ids
            if evidence_id in resolved
        ]
        item.pop("canonical_evidence_ids", None)
    return items


class _ValidationCache:
    def __init__(self) -> None:
        self.payloads: dict[tuple[str, str, int], bytes | None] = {}
        self.hash_matches: dict[tuple[str, str, str], bool | None] = {}
        self.objects: dict[tuple[str, str], dict[str, Any] | None] = {}

    async def read(
        self,
        storage: StorageBackend,
        job_id: str,
        rel_path: str,
        max_bytes: int = MAX_CANONICAL_SIDECAR_BYTES,
    ) -> bytes | None:
        key = (job_id, rel_path, max_bytes)
        if key not in self.payloads:
            self.payloads[key] = await read_file_bounded(
                storage, job_id, rel_path, max_bytes
            )
        return self.payloads[key]

    async def matches_sha256(
        self,
        storage: StorageBackend,
        job_id: str,
        rel_path: str,
        expected_sha256: str,
    ) -> bool | None:
        key = (job_id, rel_path, expected_sha256)
        if key in self.hash_matches:
            return self.hash_matches[key]
        payload_key = (job_id, rel_path, MAX_CANONICAL_SIDECAR_BYTES)
        if payload_key in self.payloads:
            payload = self.payloads[payload_key]
            value = (
                hashlib.sha256(payload).hexdigest() == expected_sha256
                if payload is not None and len(payload) <= MAX_CANONICAL_SIDECAR_BYTES
                else None
            )
        else:
            value = await _hash_matches_expected(
                storage, job_id, rel_path, expected_sha256,
            )
        self.hash_matches[key] = value
        return value

    async def object(
        self,
        storage: StorageBackend,
        job_id: str,
        rel_path: str,
    ) -> dict[str, Any] | None:
        key = (job_id, rel_path)
        if key not in self.objects:
            self.objects[key] = _load_object(
                await self.read(storage, job_id, rel_path)
            )
        return self.objects[key]


def _missing_projection(evidence_id: str) -> dict[str, Any]:
    return {
        "evidence_id": evidence_id,
        "status": "missing",
        "reason": "evidence_not_found",
        "job_id": None,
        "note_type": None,
        "chunk_id": None,
        "section": None,
        "evidence_fingerprint": None,
        "source_fingerprint": None,
        "locator": None,
        "link": None,
        "validated_at": None,
    }


def _load_object(data: bytes | None) -> dict[str, Any] | None:
    if data is None or len(data) > MAX_CANONICAL_SIDECAR_BYTES:
        return None
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _safe_locator_projection(locator: dict[str, Any]) -> dict[str, Any]:
    kind = locator["kind"]
    if kind == "image":
        return {
            "kind": kind,
            "bbox": locator["bbox"],
            "start_ms": locator["start_ms"],
            "end_ms": locator["end_ms"],
            "page": locator["page"],
        }
    return dict(locator)


def _derived_link(row: dict[str, Any], locator: dict[str, Any]) -> dict[str, str]:
    kind = locator["kind"]
    job_path = quote(str(row["job_id"]), safe="")
    if kind == "media":
        href = (
            f"/api/jobs/{job_path}/media?"
            f"{urlencode({'path': row['source_path']})}"
            f"#t={locator['start_ms'] / 1000:g}"
        )
        label = "跳到音视频位置"
    elif kind == "pdf":
        href = (
            f"/api/jobs/{job_path}/media?"
            f"{urlencode({'path': row['source_path']})}#page={locator['page']}"
        )
        label = "跳到 PDF 页"
    elif kind == "image":
        href = (
            f"/api/jobs/{job_path}/artifact?"
            f"{urlencode({'path': locator['asset_path']})}"
        )
        label = "查看证据图像"
    else:
        href = (
            f"/api/jobs/{job_path}/artifact?"
            f"{urlencode({'path': row['source_path']})}"
            f"#:~:text={quote(locator['exact'], safe='')}"
        )
        label = "跳到原文证据"
    return {"kind": kind, "href": href, "label": label}


async def _revalidate_row(
    storage: StorageBackend,
    row: dict[str, Any],
    cache: _ValidationCache,
) -> tuple[str, str | None]:
    if row["database_status"] != "valid":
        return row["database_status"], row["database_reason"]
    job_id = str(row["job_id"])
    try:
        note_data = await cache.read(
            storage, job_id, str(row["note_path"]), MAX_CANONICAL_SIDECAR_BYTES,
        )
    except (OSError, ValueError):
        note_data = None
    if note_data is None:
        return "missing", "note_missing"
    if (
        len(note_data) > MAX_CANONICAL_SIDECAR_BYTES
        or hashlib.sha256(note_data).hexdigest() != row["note_sha256"]
    ):
        return "stale", "note_changed"
    try:
        normalized_body = markdown_to_index_text(note_data.decode("utf-8"))
    except UnicodeDecodeError:
        return "stale", "note_invalid"

    provenance_data = await cache.read(
        storage, job_id, str(row["provenance_path"])
    )
    if provenance_data is None:
        return "missing", "provenance_missing"
    if hashlib.sha256(provenance_data).hexdigest() != row["provenance_sha256"]:
        return "stale", "provenance_changed"
    provenance = await cache.object(storage, job_id, str(row["provenance_path"]))
    if provenance is None:
        return "stale", "provenance_invalid"
    source_manifest_path = provenance.get("source_manifest")
    source_manifest_sha256 = provenance.get("source_manifest_sha256")
    if (
        type(source_manifest_path) is not str
        or type(source_manifest_sha256) is not str
        or len(source_manifest_sha256) != 64
    ):
        return "stale", "provenance_invalid"
    source_manifest_data = await cache.read(
        storage, job_id, source_manifest_path
    )
    if source_manifest_data is None:
        return "missing", "source_manifest_missing"
    if hashlib.sha256(source_manifest_data).hexdigest() != source_manifest_sha256:
        return "stale", "source_manifest_changed"
    source_manifest = await cache.object(
        storage, job_id, source_manifest_path
    )
    if source_manifest is None:
        return "stale", "source_manifest_invalid"
    try:
        source_manifest = validate_source_manifest(source_manifest)
    except ValueError:
        return "stale", "source_manifest_invalid"
    if source_manifest_data != canonical_json_bytes(source_manifest):
        return "stale", "source_manifest_noncanonical"
    try:
        provenance = validate_provenance_manifest(
            provenance,
            source_manifest=source_manifest,
            note_bytes=note_data,
            normalized_body=normalized_body,
        )
    except ValueError:
        return "stale", "provenance_invalid"
    if provenance_data != canonical_json_bytes(provenance):
        return "stale", "provenance_noncanonical"
    if (
        provenance.get("job_id") != job_id
        or provenance.get("note_type") != row["note_type"]
        or provenance.get("note_artifact") != row["note_path"]
        or provenance.get("source_manifest") != source_manifest_path
    ):
        return "stale", "provenance_identity_changed"

    source_id = str(row["source_ref"])
    segment_id = str(row["source_segment_id"])
    artifacts = source_manifest.get("source_artifacts")
    segments = source_manifest.get("segments")
    if not isinstance(artifacts, list) or not isinstance(segments, list):
        return "stale", "source_manifest_invalid"
    artifact = next(
        (
            item for item in artifacts
            if isinstance(item, dict) and item.get("source_id") == source_id
        ),
        None,
    )
    segment = next(
        (
            item for item in segments
            if isinstance(item, dict) and item.get("segment_id") == segment_id
        ),
        None,
    )
    if artifact is None or segment is None or segment.get("source_id") != source_id:
        return "missing", "source_segment_missing"
    if (
        artifact.get("path") != row["source_path"]
        or artifact.get("sha256") != row["source_sha256"]
        or artifact.get("revision") != row["source_revision"]
        or segment.get("locator") != row["locator"]
    ):
        return "stale", "source_identity_changed"
    try:
        locator = validate_canonical_locator(
            segment.get("locator"), source_artifact=artifact
        )
    except ValueError:
        return "stale", "locator_invalid"

    source_data: bytes | None = None
    try:
        if locator["kind"] == "text":
            source_data = await cache.read(storage, job_id, str(artifact["path"]))
            source_matches = (
                hashlib.sha256(source_data).hexdigest() == row["source_sha256"]
                if source_data is not None else None
            )
        else:
            source_matches = await cache.matches_sha256(
                storage, job_id, str(artifact["path"]), str(row["source_sha256"]),
            )
    except (OSError, ValueError):
        source_matches = None
    if source_matches is None:
        return "missing", "source_missing"
    if not source_matches:
        return "stale", "source_changed"

    if locator["kind"] == "text":
        if source_data is None or len(source_data) > MAX_CANONICAL_SIDECAR_BYTES:
            return "stale", "text_source_unverifiable"
        try:
            source_text = source_data.decode("utf-8")
        except UnicodeDecodeError:
            return "stale", "text_source_invalid"
        start, end = segment.get("start"), segment.get("end")
        if (
            type(start) is not int or type(end) is not int
            or not 0 <= start < end <= len(source_text)
            or source_text[start:end] != locator["exact"]
            or (
                locator["prefix"]
                and not source_text[:start].endswith(locator["prefix"])
            )
            or (
                locator["suffix"]
                and not source_text[end:].startswith(locator["suffix"])
            )
        ):
            return "stale", "text_locator_changed"
    elif locator["kind"] == "image":
        try:
            image_matches = await cache.matches_sha256(
                storage, job_id, locator["asset_path"], locator["asset_sha256"],
            )
        except (OSError, ValueError):
            image_matches = None
        if image_matches is None:
            return "missing", "image_missing"
        if not image_matches:
            return "stale", "image_changed"

    support_artifact = segment.get("support_artifact")
    if source_manifest.get("schema_version") == 2 and support_artifact is not None:
        support_path = str(support_artifact["path"])
        support_sha256 = str(support_artifact["sha256"])
        try:
            support_matches = await cache.matches_sha256(
                storage, job_id, support_path, support_sha256,
            )
        except (OSError, ValueError):
            support_matches = None
        if support_matches is None:
            return "missing", "support_artifact_missing"
        if not support_matches:
            return "stale", "support_artifact_changed"
        try:
            support_data = await cache.read(
                storage, job_id, support_path, MAX_SUPPORT_ARTIFACT_BYTES,
            )
        except (OSError, ValueError):
            support_data = None
        if support_data is None:
            return "missing", "support_artifact_missing"
        try:
            expected_support = support_text_from_artifact(
                support_data, support_artifact, segment, artifact,
            )
        except ValueError:
            return "stale", "support_artifact_invalid"
        if expected_support != segment.get("support_text"):
            return "stale", "support_text_changed"

    source_identity = {
        "source_ref": row["source_ref"],
        "source_segment_id": row["source_segment_id"],
        "path": artifact["path"],
        "sha256": artifact["sha256"],
        "revision": artifact["revision"],
        "start": segment.get("start"),
        "end": segment.get("end"),
        "section": segment.get("section"),
        "locator": locator,
    }
    if source_manifest.get("schema_version") == 2:
        source_identity["support_text"] = segment.get("support_text")
        source_identity["support_artifact"] = segment.get("support_artifact")
    if canonical_source_fingerprint(source_identity) != row["source_fingerprint"]:
        return "stale", "source_fingerprint_changed"
    evidence_fingerprints: set[str] = set()
    for mapping in provenance.get("segments", []):
        refs = mapping.get("source_segment_ids")
        if not isinstance(refs, list) or segment_id not in refs:
            continue
        try:
            anchor_start, anchor_end = locate_provenance_anchor(
                normalized_body,
                anchor=str(mapping["anchor"]),
                prefix=str(mapping["prefix"]),
                suffix=str(mapping["suffix"]),
            )
        except (KeyError, ValueError):
            continue
        if not (
            int(row["chunk_char_start"]) < anchor_end
            and int(row["chunk_char_end"]) > anchor_start
        ):
            continue
        verification_policy = mapping.get(
            "verification_policy", DIRECT_LOCATOR_POLICY,
        )
        evidence_identity = canonical_evidence_content_identity(
            job_id=job_id,
            note_type=str(row["note_type"]),
            note_path=str(row["note_path"]),
            note_sha256=hashlib.sha256(note_data).hexdigest(),
            provenance_sha256=hashlib.sha256(provenance_data).hexdigest(),
            chunk_id=str(row["chunk_id"]),
            chunk_body_sha256=str(row["chunk_body_sha256"]),
            chunk_char_start=int(row["chunk_char_start"]),
            chunk_char_end=int(row["chunk_char_end"]),
            anchor_start=anchor_start,
            anchor_end=anchor_end,
            source_fingerprint=str(row["source_fingerprint"]),
            provenance_schema_version=int(provenance["schema_version"]),
            verification_policy=str(verification_policy),
        )
        evidence_fingerprints.add(
            canonical_evidence_fingerprint(evidence_identity)
        )
    if row["evidence_fingerprint"] not in evidence_fingerprints:
        return "stale", "evidence_fingerprint_changed"
    identity = {
        "schema_version": row["schema_version"],
        "job_id": row["job_id"],
        "note_type": row["note_type"],
        "chunk_id": row["chunk_id"],
        "source_ref": row["source_ref"],
        "source_segment_id": row["source_segment_id"],
        "evidence_fingerprint": row["evidence_fingerprint"],
    }
    try:
        expected_id = canonical_evidence_id(identity)
    except ValueError:
        return "stale", "evidence_identity_invalid"
    if expected_id != row["evidence_id"]:
        return "stale", "evidence_identity_changed"
    return "valid", None


def _projection(row: dict[str, Any], status: str, reason: str | None) -> dict[str, Any]:
    locator = row["locator"] if status == "valid" else None
    return {
        "evidence_id": row["evidence_id"],
        "status": status,
        "reason": reason,
        "job_id": row["job_id"],
        "note_type": row["note_type"],
        "chunk_id": row["chunk_id"],
        "section": row["section"],
        "evidence_fingerprint": row["evidence_fingerprint"],
        "source_fingerprint": row["source_fingerprint"],
        "locator": _safe_locator_projection(locator) if locator is not None else None,
        "link": _derived_link(row, locator) if locator is not None else None,
        "validated_at": row["validated_at"],
    }


async def resolve_canonical_evidence_batch(
    db: Database,
    storage: StorageBackend,
    evidence_ids: list[str],
) -> list[dict]:
    """批量重验并按请求顺序投影；未知 ID 保留 missing 占位。"""
    if (
        not isinstance(evidence_ids, list)
        or not 1 <= len(evidence_ids) <= 100
        or any(type(item) is not str or _EVIDENCE_ID_RE.fullmatch(item) is None for item in evidence_ids)
        or len(set(evidence_ids)) != len(evidence_ids)
    ):
        raise ValueError("evidence_ids must contain 1..100 unique canonical ids")
    rows = db.canonical_evidence_database_states(evidence_ids)
    states: dict[str, tuple[str, str | None]] = {}
    cache = _ValidationCache()
    for evidence_id, row in rows.items():
        try:
            states[evidence_id] = await _revalidate_row(storage, row, cache)
        except (OSError, ValueError, KeyError, TypeError):
            states[evidence_id] = ("stale", "validation_failed")

    # 文件重验期间可能发生 reindex/delete；提交状态前再次取 DB 事实。
    if rows:
        current = db.canonical_evidence_database_states(list(rows))
        for evidence_id, row in current.items():
            if row["database_status"] != "valid":
                states[evidence_id] = (
                    row["database_status"], row["database_reason"]
                )
        db.set_canonical_evidence_states([
            {"evidence_id": evidence_id, "status": state, "reason": reason}
            for evidence_id, (state, reason) in states.items()
        ])
        refreshed = db.canonical_evidence_database_states(list(rows))
        for evidence_id, row in refreshed.items():
            rows[evidence_id]["validated_at"] = row["validated_at"]
    return [
        _missing_projection(evidence_id)
        if evidence_id not in rows
        else _projection(rows[evidence_id], *states[evidence_id])
        for evidence_id in evidence_ids
    ]


async def resolve_canonical_evidence(
    db: Database,
    storage: StorageBackend,
    evidence_id: str,
) -> dict | None:
    """重验单条 canonical evidence；未知 ID 返回 None。"""
    items = await resolve_canonical_evidence_batch(db, storage, [evidence_id])
    return None if items[0]["reason"] == "evidence_not_found" else items[0]
