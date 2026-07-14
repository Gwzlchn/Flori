"""验证 canonical evidence 摄入、失效和 resolver 安全投影。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.deps import get_db, get_storage, verify_token
from api.main import create_app
from api.services.evidence import (
    _HashValidationMemo,
    resolve_canonical_evidence,
    resolve_canonical_evidence_batch,
)
from scheduler.scheduler import _markdown_to_text
from shared.db import Database, _chunk_note_body
from shared.evidence_contract import (
    CanonicalEvidenceError,
    build_canonical_evidence_records_with_reader,
    canonical_evidence_content_identity,
    canonical_evidence_fingerprint,
    canonical_evidence_id,
    canonical_source_fingerprint,
)
from shared.provenance import canonical_json_bytes
from shared.storage import StorageObjectVersion


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class MemoryStorage:
    def __init__(self, files: dict[str, bytes]):
        self.files = files
        self.opens: dict[str, int] = {}

    async def read_file(self, _job_id: str, rel_path: str) -> bytes | None:
        return self.files.get(rel_path)

    async def file_size(self, _job_id: str, rel_path: str) -> int | None:
        data = self.files.get(rel_path)
        return len(data) if data is not None else None

    async def open_stream(
        self,
        _job_id: str,
        rel_path: str,
        *,
        start: int = 0,
        length: int | None = None,
        chunk_size: int = 256 * 1024,
    ):
        del chunk_size
        data = self.files.get(rel_path)
        if data is None:
            return None
        self.opens[rel_path] = self.opens.get(rel_path, 0) + 1
        end = None if length is None else start + length

        async def chunks():
            yield data[start:end]

        return chunks()


class VersionedMemoryStorage(MemoryStorage):
    _sequence = 0

    def __init__(self, files: dict[str, bytes]):
        super().__init__(files)
        type(self)._sequence += 1
        self.namespace = f"memory:{self._sequence}"
        self.versions = {path: 0 for path in files}

    async def object_version(
        self, _job_id: str, rel_path: str,
    ) -> StorageObjectVersion | None:
        data = self.files.get(rel_path)
        if data is None:
            return None
        return StorageObjectVersion(
            namespace=self.namespace,
            size=len(data),
            token=str(self.versions[rel_path]),
        )

    def replace(self, rel_path: str, data: bytes) -> None:
        self.files[rel_path] = data
        self.versions[rel_path] = self.versions.get(rel_path, 0) + 1

    def remove(self, rel_path: str) -> None:
        self.files.pop(rel_path, None)
        self.versions[rel_path] = self.versions.get(rel_path, 0) + 1


def _sidecars(
    note_data: bytes,
    *,
    source_data: bytes = b"alpha Claim evidence beta",
    two_segments: bool = False,
    locator_kind: str = "text",
) -> tuple[bytes, bytes, dict[str, bytes]]:
    support_files: dict[str, bytes] = {}
    if locator_kind == "media":
        source_id = "media:primary"
        source_path = "input/source.mp4"
        media_duration_ms = 1000
        page_count = None
        locator = {"kind": "media", "start_ms": 100, "end_ms": 300}
        start = None
        end = None
        subtitle = (
            b"1\n00:00:00,100 --> 00:00:00,300\nClaim evidence\n"
        )
        support_files["input/subtitle.srt"] = subtitle
        support_artifact = {
            "kind": "video_subtitle",
            "path": "input/subtitle.srt",
            "sha256": _sha(subtitle),
            "selector": {"index": 0},
        }
    elif locator_kind == "pdf":
        source_id = "pdf:primary"
        source_path = "input/source.pdf"
        media_duration_ms = None
        page_count = 5
        locator = {"kind": "pdf", "page": 2, "bbox": None}
        start = None
        end = None
        pages = canonical_json_bytes({
            "schema_version": 1,
            "source_sha256": _sha(source_data),
            "pages": [
                {"page": page, "support_text": (
                    "Claim evidence" if page == 2 else None
                )}
                for page in range(1, 6)
            ],
        })
        support_files["intermediate/pdf_page_support.json"] = pages
        support_artifact = {
            "kind": "pdf_pages",
            "path": "intermediate/pdf_page_support.json",
            "sha256": _sha(pages),
            "selector": {"page": 2},
        }
    else:
        source_id = "article:body"
        source_path = "output/original.md"
        media_duration_ms = None
        page_count = None
        locator = {
            "kind": "text",
            "exact": "Claim evidence",
            "prefix": "alpha ",
            "suffix": " beta",
            "dom_path": None,
        }
        start = 6
        end = 20
        support_artifact = {
            "kind": "html",
            "path": source_path,
            "sha256": _sha(source_data),
            "selector": {"start": start, "end": end},
        }
    source_manifest = {
        "schema_version": 2,
        "job_id": "job-evidence",
        "pipeline": "article",
        "source_artifacts": [{
            "source_id": source_id,
            "path": source_path,
            "sha256": _sha(source_data),
            "revision": "r1",
            "media_duration_ms": media_duration_ms,
            "page_count": page_count,
        }],
        "segments": [{
            "segment_id": "paragraph:1",
            "source_id": source_id,
            "start": start,
            "end": end,
            "section": "Intro",
            "locator": locator,
            "support_text": "Claim evidence",
            "support_artifact": support_artifact,
        }],
    }
    if two_segments:
        second = dict(source_manifest["segments"][0])
        second["segment_id"] = "paragraph:2"
        source_manifest["segments"].append(second)
    source_bytes = canonical_json_bytes(source_manifest)
    provenance = {
        "schema_version": 2,
        "job_id": "job-evidence",
        "note_type": "smart",
        "note_artifact": "output/notes.md",
        "note_sha256": _sha(note_data),
        "source_manifest": "intermediate/source_segments.json",
        "source_manifest_sha256": _sha(source_bytes),
        "segments": [{
            "anchor": "Claim evidence",
            "prefix": "",
            "suffix": ".",
            "section": "Intro",
            "source_segment_ids": [segment_id],
            "verification_policy": "exact_quote_v1",
        } for segment_id in (
            ["paragraph:1", "paragraph:2"] if two_segments else ["paragraph:1"]
        )],
    }
    provenance_bytes = canonical_json_bytes(provenance)
    return source_bytes, provenance_bytes, support_files


async def _records(
    note_data: bytes,
    storage: MemoryStorage,
    *,
    note_type: str = "smart",
) -> tuple[str, list[dict]]:
    body = _markdown_to_text(note_data.decode())
    chunks = [{
        "chunk_id": f"job-evidence:{note_type}:{index}",
        "body": chunk["body"],
        "section": chunk["section"],
        "char_start": chunk["char_start"],
        "char_end": chunk["char_end"],
    } for index, chunk in enumerate(_chunk_note_body(body))]

    async def read_file(rel: str, max_bytes: int) -> bytes | None:
        value = storage.files.get(rel)
        if value is None:
            return None
        return value[:max_bytes + 1]

    async def hash_file(rel: str) -> str | None:
        value = storage.files.get(rel)
        return _sha(value) if value is not None else None

    records = await build_canonical_evidence_records_with_reader(
        job_id="job-evidence",
        pipeline="article",
        note_type=note_type,
        note_path="output/notes.md",
        note_data=note_data,
        normalized_body=body,
        chunks=chunks,
        source_manifest_data=storage.files["intermediate/source_segments.json"],
        source_manifest_path="intermediate/source_segments.json",
        provenance_path=f"output/provenance/{note_type}.json",
        provenance_data=storage.files[f"output/provenance/{note_type}.json"],
        read_file=read_file,
        sha256_file=hash_file,
    )
    return body, records


def _database(path: Path) -> Database:
    db = Database(path)
    db.init_schema()
    db._conn.execute(
        """INSERT INTO jobs
           (id,content_type,pipeline,title,domain,status,is_current,created_at,updated_at)
           VALUES ('job-evidence','article','article','Evidence','general','done',1,'now','now')"""
    )
    db._conn.commit()
    return db


@pytest.mark.asyncio
async def test_v1_direct_sidecar_keeps_pre_v2_fingerprints(tmp_path: Path) -> None:
    note = b"# Intro\n\nClaim evidence."
    source_bytes, provenance_bytes, _support_files = _sidecars(note)
    source = json.loads(source_bytes)
    source["schema_version"] = 1
    for segment in source["segments"]:
        segment.pop("support_text")
        segment.pop("support_artifact")
    source_bytes = canonical_json_bytes(source)
    provenance = json.loads(provenance_bytes)
    provenance["schema_version"] = 1
    provenance["note_type"] = "original"
    provenance["source_manifest_sha256"] = _sha(source_bytes)
    for mapping in provenance["segments"]:
        mapping.pop("verification_policy")
    provenance_bytes = canonical_json_bytes(provenance)
    storage = MemoryStorage({
        "output/notes.md": note,
        "output/original.md": b"alpha Claim evidence beta",
        "intermediate/source_segments.json": source_bytes,
        "output/provenance/original.json": provenance_bytes,
    })

    body, records = await _records(note, storage, note_type="original")

    segment = source["segments"][0]
    artifact = source["source_artifacts"][0]
    source_identity = {
        "source_ref": artifact["source_id"],
        "source_segment_id": segment["segment_id"],
        "path": artifact["path"],
        "sha256": artifact["sha256"],
        "revision": artifact["revision"],
        "start": segment["start"],
        "end": segment["end"],
        "section": segment["section"],
        "locator": segment["locator"],
    }
    source_fingerprint = canonical_source_fingerprint(source_identity)
    chunk = _chunk_note_body(body)[0]
    anchor_start = body.index("Claim evidence")
    expected_evidence = canonical_evidence_fingerprint({
        "job_id": "job-evidence",
        "note_type": "original",
        "note_path": "output/notes.md",
        "note_sha256": _sha(note),
        "provenance_sha256": _sha(provenance_bytes),
        "chunk_id": "job-evidence:original:0",
        "chunk_body_sha256": _sha(chunk["body"].encode()),
        "chunk_char_start": chunk["char_start"],
        "chunk_char_end": chunk["char_end"],
        "anchor_start": anchor_start,
        "anchor_end": anchor_start + len("Claim evidence"),
        "source_fingerprint": source_fingerprint,
    })
    assert records[0]["source_fingerprint"] == source_fingerprint
    assert records[0]["evidence_fingerprint"] == expected_evidence
    db = _database(tmp_path / "v1-direct.db")
    try:
        db.index_job_notes(
            "job-evidence", "original", "Evidence", body,
            "article", "general", "", ["original"], records,
        )
        resolved = await resolve_canonical_evidence(
            db, storage, records[0]["evidence_id"],
        )
        assert resolved is not None and resolved["status"] == "valid"
    finally:
        db.close()


def test_hash_validation_memo_is_bounded_and_keys_expected_sha():
    memo = _HashValidationMemo(max_entries=2)
    version = StorageObjectVersion("memory:test", 10, "v1")
    first = ("job", "source.mp4", version, "a" * 64)
    second = ("job", "source.mp4", version, "b" * 64)
    third = ("job", "source.pdf", version, "c" * 64)

    memo.put(first, True)
    memo.put(second, False)
    assert memo.get(first) == (True, True)
    assert memo.get(second) == (True, False)

    memo.put(third, True)
    assert len(memo.entries) == 2
    assert memo.get(first) == (False, False)


@pytest.mark.asyncio
async def test_canonical_evidence_reindex_is_idempotent_and_old_id_becomes_stale(
    tmp_path: Path,
):
    note = b"# Intro\n\nClaim evidence."
    source, provenance, support_files = _sidecars(note)
    storage = MemoryStorage({
        **support_files,
        "output/notes.md": note,
        "output/original.md": b"alpha Claim evidence beta",
        "intermediate/source_segments.json": source,
        "output/provenance/smart.json": provenance,
    })
    body, records = await _records(note, storage)
    assert len(records) == 1
    assert records[0]["source_ref"] == "article:body"
    assert records[0]["source_segment_id"] == "paragraph:1"

    db = _database(tmp_path / "canonical.db")
    try:
        for _ in range(2):
            db.index_job_notes(
                "job-evidence", "smart", "Evidence", body,
                "article", "general", "", ["smart"], records,
            )
        old_id = records[0]["evidence_id"]
        assert db._conn.execute(
            "SELECT COUNT(*) FROM canonical_evidence"
        ).fetchone()[0] == 1
        chunk = db._conn.execute(
            "SELECT evidence_json FROM note_chunks WHERE chunk_id=?",
            (records[0]["chunk_id"],),
        ).fetchone()
        assert json.loads(chunk[0])["canonical_evidence_ids"] == [old_id]

        changed_note = b"# Intro\n\nClaim evidence. Added context."
        changed_source, changed_provenance, changed_support = _sidecars(changed_note)
        storage.files.update({
            **changed_support,
            "output/notes.md": changed_note,
            "intermediate/source_segments.json": changed_source,
            "output/provenance/smart.json": changed_provenance,
        })
        changed_body, changed_records = await _records(changed_note, storage)
        new_id = changed_records[0]["evidence_id"]
        assert new_id != old_id
        db.index_job_notes(
            "job-evidence", "smart", "Evidence", changed_body,
            "article", "general", "", ["smart"], changed_records,
        )
        states = db.canonical_evidence_database_states([old_id, new_id])
        assert states[old_id]["status"] == "stale"
        assert states[new_id]["database_status"] == "valid"
        assert db.canonical_evidence_ids_for_job("job-evidence") == [new_id]
    finally:
        db.close()


@pytest.mark.asyncio
async def test_resolver_rejects_database_policy_fingerprint_drift(tmp_path: Path) -> None:
    note = b"# Intro\n\nClaim evidence."
    source, provenance, support_files = _sidecars(note)
    storage = MemoryStorage({
        **support_files,
        "output/notes.md": note,
        "output/original.md": b"alpha Claim evidence beta",
        "intermediate/source_segments.json": source,
        "output/provenance/smart.json": provenance,
    })
    body, records = await _records(note, storage)
    record = records[0]
    anchor_start = body.index("Claim evidence")
    wrong_identity = canonical_evidence_content_identity(
        job_id=record["job_id"],
        note_type=record["note_type"],
        note_path=record["note_path"],
        note_sha256=record["note_sha256"],
        provenance_sha256=record["provenance_sha256"],
        chunk_id=record["chunk_id"],
        chunk_body_sha256=record["chunk_body_sha256"],
        chunk_char_start=record["chunk_char_start"],
        chunk_char_end=record["chunk_char_end"],
        anchor_start=anchor_start,
        anchor_end=anchor_start + len("Claim evidence"),
        source_fingerprint=record["source_fingerprint"],
        provenance_schema_version=2,
        verification_policy="direct_locator_v1",
    )
    wrong_fingerprint = canonical_evidence_fingerprint(wrong_identity)
    wrong_id = canonical_evidence_id({
        "schema_version": record["schema_version"],
        "job_id": record["job_id"],
        "note_type": record["note_type"],
        "chunk_id": record["chunk_id"],
        "source_ref": record["source_ref"],
        "source_segment_id": record["source_segment_id"],
        "evidence_fingerprint": wrong_fingerprint,
    })
    db = _database(tmp_path / "policy-drift.db")
    try:
        db.index_job_notes(
            "job-evidence", "smart", "Evidence", body,
            "article", "general", "", ["smart"], records,
        )
        db._conn.execute(
            """UPDATE canonical_evidence
               SET evidence_id=?, evidence_fingerprint=? WHERE evidence_id=?""",
            (wrong_id, wrong_fingerprint, record["evidence_id"]),
        )
        db._conn.commit()

        resolved = await resolve_canonical_evidence(db, storage, wrong_id)

        assert resolved is not None
        assert resolved["status"] == "stale"
        assert resolved["reason"] == "evidence_fingerprint_changed"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_empty_provenance_segments_indexes_note_without_evidence(tmp_path: Path):
    note = b"# Intro\n\nClaim without a reliable source mapping."
    source_data = b"media payload remains available"
    source, provenance, _support_files = _sidecars(
        note, source_data=source_data, locator_kind="media",
    )
    provenance_payload = json.loads(provenance)
    provenance_payload["segments"] = []
    provenance = canonical_json_bytes(provenance_payload)
    storage = MemoryStorage({
        "output/notes.md": note,
        "input/source.mp4": source_data,
        "intermediate/source_segments.json": source,
        "output/provenance/smart.json": provenance,
    })

    body, records = await _records(note, storage)
    assert records == []

    db = _database(tmp_path / "empty-provenance.db")
    try:
        db.index_job_notes(
            "job-evidence", "smart", "Evidence", body,
            "article", "general", "", ["smart"], records,
        )
        chunk = db._conn.execute(
            "SELECT body,evidence_json FROM note_chunks WHERE job_id=?",
            ("job-evidence",),
        ).fetchone()
        assert "Claim without a reliable source mapping." in chunk[0]
        assert json.loads(chunk[1])["canonical_evidence_ids"] == []
        assert db.canonical_evidence_ids_for_job("job-evidence") == []
    finally:
        db.close()


@pytest.mark.asyncio
async def test_empty_mapping_does_not_read_missing_unreferenced_support() -> None:
    note = b"# Intro\n\nClaim without evidence."
    source_data = b"media source"
    source, provenance, _support_files = _sidecars(
        note, source_data=source_data, locator_kind="media",
    )
    payload = json.loads(provenance)
    payload["segments"] = []
    provenance = canonical_json_bytes(payload)
    storage = MemoryStorage({
        "output/notes.md": note,
        "input/source.mp4": source_data,
        "intermediate/source_segments.json": source,
        "output/provenance/smart.json": provenance,
    })

    _body, records = await _records(note, storage)

    assert records == []
    assert "input/subtitle.srt" not in storage.opens


@pytest.mark.asyncio
async def test_builder_rejects_fabricated_html_support_text() -> None:
    note = b"# Intro\n\nClaim evidence."
    source, provenance, support_files = _sidecars(note)
    source_payload = json.loads(source)
    source_payload["segments"][0]["support_text"] = (
        "Claim evidence plus attacker supplied text"
    )
    source = canonical_json_bytes(source_payload)
    provenance_payload = json.loads(provenance)
    provenance_payload["source_manifest_sha256"] = _sha(source)
    provenance = canonical_json_bytes(provenance_payload)
    storage = MemoryStorage({
        **support_files,
        "output/notes.md": note,
        "output/original.md": b"alpha Claim evidence beta",
        "intermediate/source_segments.json": source,
        "output/provenance/smart.json": provenance,
    })

    with pytest.raises(CanonicalEvidenceError, match="support text does not match"):
        await _records(note, storage)


def test_note_consumers_bound_projection_but_job_endpoint_can_page_full_set(tmp_path: Path):
    db = _database(tmp_path / "bounded-projection.db")
    try:
        db.index_job_notes(
            "job-evidence", "smart", "Evidence", "# Intro\n\nBounded evidence.",
        )
        evidence_ids = [f"ce_{index:064x}" for index in range(25)]
        row = db._conn.execute(
            "SELECT chunk_id,evidence_json FROM note_chunks WHERE job_id=?",
            ("job-evidence",),
        ).fetchone()
        payload = json.loads(row["evidence_json"])
        payload["canonical_evidence_ids"] = evidence_ids
        encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        db._conn.execute(
            "UPDATE note_chunks SET evidence_json=? WHERE chunk_id=?",
            (encoded, row["chunk_id"]),
        )
        db._conn.commit()

        assert db.canonical_evidence_ids_for_notes([
            ("job-evidence", "smart"),
        ])[("job-evidence", "smart")] == evidence_ids[:20]
        assert db.canonical_evidence_ids_for_job("job-evidence", "smart") == evidence_ids
    finally:
        db.close()


@pytest.mark.asyncio
async def test_resolver_batches_shared_artifacts_once_and_uses_real_content_route(
    tmp_path: Path,
):
    note = b"# Intro\n\nClaim evidence."
    source, provenance, support_files = _sidecars(note, two_segments=True)
    storage = MemoryStorage({
        **support_files,
        "output/notes.md": note,
        "output/original.md": b"alpha Claim evidence beta",
        "intermediate/source_segments.json": source,
        "output/provenance/smart.json": provenance,
    })
    body, records = await _records(note, storage)
    db = _database(tmp_path / "resolver.db")
    try:
        db.index_job_notes(
            "job-evidence", "smart", "Evidence", body,
            "article", "general", "", ["smart"], records,
        )
        assert len(records) == 2
        evidence_ids = [record["evidence_id"] for record in records]
        unknown = "ce_" + "f" * 64
        items = await resolve_canonical_evidence_batch(
            db, storage, [*evidence_ids, unknown]
        )
        assert [item["evidence_id"] for item in items] == [*evidence_ids, unknown]
        assert [item["status"] for item in items[:2]] == ["valid", "valid"]
        assert items[0]["link"]["href"].startswith(
            "/api/jobs/job-evidence/artifact?path=output%2Foriginal.md"
        )
        assert "#:~:text=Claim%20evidence" in items[0]["link"]["href"]
        assert items[2]["reason"] == "evidence_not_found"
        assert all(count == 1 for count in storage.opens.values())

        storage.files["output/original.md"] = b"alpha changed beta"
        stale = await resolve_canonical_evidence(db, storage, evidence_ids[0])
        assert stale is not None
        assert stale["status"] == "stale"
        assert stale["link"] is None
        assert db.canonical_evidence_ids_for_job("job-evidence") == evidence_ids

        storage.files["output/original.md"] = b"alpha Claim evidence beta"
        recovered = await resolve_canonical_evidence(db, storage, evidence_ids[0])
        assert recovered is not None
        assert recovered["status"] == "valid"
        assert recovered["link"] is not None
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(("locator_kind", "source_path"), [
    ("media", "input/source.mp4"),
    ("pdf", "input/source.pdf"),
])
async def test_resolver_memo_rehashes_large_source_only_after_version_change(
    tmp_path: Path, locator_kind: str, source_path: str,
):
    note = b"# Intro\n\nClaim evidence."
    large_source = b"a" * (2 * 1024 * 1024)
    source, provenance, support_files = _sidecars(
        note, source_data=large_source, locator_kind=locator_kind,
    )
    storage = VersionedMemoryStorage({
        **support_files,
        "output/notes.md": note,
        source_path: large_source,
        "intermediate/source_segments.json": source,
        "output/provenance/smart.json": provenance,
    })
    body, records = await _records(note, storage)
    db = _database(tmp_path / "resolver-memo.db")
    try:
        db.index_job_notes(
            "job-evidence", "smart", "Evidence", body,
            "article", "general", "", ["smart"], records,
        )
        evidence_id = records[0]["evidence_id"]

        first = await resolve_canonical_evidence(db, storage, evidence_id)
        second = await resolve_canonical_evidence(db, storage, evidence_id)
        assert first is not None and first["status"] == "valid"
        assert second is not None and second["status"] == "valid"
        assert storage.opens[source_path] == 1

        storage.replace(source_path, b"b" * len(large_source))
        stale = await resolve_canonical_evidence(db, storage, evidence_id)
        assert stale is not None and stale["status"] == "stale"
        assert stale["reason"] == "source_changed"
        assert storage.opens[source_path] == 2

        storage.remove(source_path)
        missing = await resolve_canonical_evidence(db, storage, evidence_id)
        assert missing is not None and missing["status"] == "missing"
        assert missing["reason"] == "source_missing"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_malformed_or_cross_job_provenance_fails_closed():
    note = b"# Intro\n\nClaim evidence."
    source, provenance, support_files = _sidecars(note)
    manifest = json.loads(source)
    manifest["job_id"] = "other-job"
    source = json.dumps(manifest, separators=(",", ":")).encode()
    storage = MemoryStorage({
        **support_files,
        "output/original.md": b"alpha Claim evidence beta",
        "intermediate/source_segments.json": source,
        "output/provenance/smart.json": provenance,
    })
    with pytest.raises(CanonicalEvidenceError, match="identity"):
        await _records(note, storage)


def test_openapi_preserves_locator_union_and_request_rejects_extra_fields():
    app = create_app()
    app.dependency_overrides[verify_token] = lambda: None
    app.dependency_overrides[get_db] = lambda: object()
    app.dependency_overrides[get_storage] = lambda: object()
    schema = app.openapi()
    locator = schema["components"]["schemas"]["CanonicalEvidenceProjection"][
        "properties"
    ]["locator"]
    encoded = json.dumps(locator, sort_keys=True)
    assert "discriminator" in encoded
    assert all(
        name in encoded
        for name in (
            "CanonicalMediaLocator", "CanonicalPdfLocator",
            "CanonicalTextLocator", "CanonicalImageLocator",
        )
    )

    response = TestClient(app).post(
        "/api/evidence/resolve",
        json={"evidence_ids": ["ce_" + "a" * 64], "unexpected": True},
    )
    assert response.status_code == 422
