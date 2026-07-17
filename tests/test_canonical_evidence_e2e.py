"""验证 Document HTML/PDF 到 Search、Ask、MCP 的 canonical evidence 闭环。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from api.mcp_server.server import build_server
from api.services.evidence import resolve_canonical_evidence
from scheduler.scheduler import _markdown_to_text
from shared.db import Database, _chunk_note_body
from shared.evidence_contract import build_canonical_evidence_records_with_reader
from shared.models import Job, JobStatus
from shared.storage import LocalStorage
from steps.document.adapters import parse_pdf_document, parse_scholarly_html
from steps.document.provenance import (
    extract_attestable_document_markers,
    persist_document_note_provenance,
    publish_document_source_manifest,
)


def _minimal_text_pdf(text: str) -> bytes:
    encoded = text.encode("ascii").replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")
    content = b"BT /F1 12 Tf 72 720 Td (" + encoded + b") Tj ET"
    objects = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        4: f"<< /Length {len(content)} >>\nstream\n".encode() + content + b"\nendstream",
        5: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }
    data = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = {}
    for number in range(1, 6):
        offsets[number] = len(data)
        data.extend(f"{number} 0 obj\n".encode() + objects[number] + b"\nendobj\n")
    xref = len(data)
    data.extend(b"xref\n0 6\n0000000000 65535 f \n")
    for number in range(1, 6):
        data.extend(f"{offsets[number]:010d} 00000 n \n".encode())
    data.extend(f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return bytes(data)


def _mcp_payload(result) -> list[dict]:
    structured = result[1] if isinstance(result, tuple) and len(result) == 2 else None
    if isinstance(structured, dict) and set(structured) == {"result"}:
        return structured["result"]
    if isinstance(structured, list):
        return structured
    blocks = result[0] if isinstance(result, tuple) else result
    return json.loads(blocks[0].text)


def _prepare_document(job_dir: Path, source_profile: str, claim: str) -> tuple[str, str]:
    for rel in ("input", "intermediate", "output/versions", "output/provenance"):
        (job_dir / rel).mkdir(parents=True, exist_ok=True)
    job = {
        "job_id": job_dir.name,
        "content_type": "document",
        "document_kind": "research_paper",
        "source_profile": source_profile,
    }
    if source_profile == "scholarly_html":
        source_rel = "input/source.html"
        (job_dir / source_rel).write_text(
            f"<article><h1>Evidence Paper</h1><p>{claim}</p></article>",
            encoding="utf-8",
        )
        document, quality = parse_scholarly_html(job_dir, job)
    else:
        source_rel = "input/source.pdf"
        (job_dir / source_rel).write_bytes(_minimal_text_pdf(claim))
        document, quality = parse_pdf_document(job_dir, job)
    assert quality["status"] != "rejected"
    (job_dir / "intermediate/document.json").write_text(json.dumps(document), encoding="utf-8")
    manifest = publish_document_source_manifest(job_dir, document)
    source_segment = next(
        item for item in manifest["segments"] if item.get("support_text") == claim
    )
    token = source_segment["segment_id"].removeprefix("seg_")
    note, candidates, semantic = extract_attestable_document_markers(
        f"# Evidence Paper - 笔记\n\n{claim} [[source:{token}]]",
        manifest,
        ai=SimpleNamespace(last_response=None),
    )
    assert semantic == [] and len(candidates) == 1
    note_rel = "output/versions/notes_smart_1.md"
    (job_dir / note_rel).write_text(note, encoding="utf-8")
    persisted = persist_document_note_provenance(
        job_dir, note_type="smart", note_artifact=note_rel, candidates=candidates,
    )
    assert persisted == {"status": "written", "segments": 1}
    return source_rel, note_rel


async def _index_document_note(
    db: Database, job_dir: Path, note_rel: str,
) -> str:
    note_data = (job_dir / note_rel).read_bytes()
    body = _markdown_to_text(note_data.decode())
    chunks = [{
        "chunk_id": f"{job_dir.name}:smart:{index}",
        "body": chunk["body"],
        "section": chunk["section"],
        "char_start": chunk["char_start"],
        "char_end": chunk["char_end"],
    } for index, chunk in enumerate(_chunk_note_body(body))]

    async def read_file(rel: str, max_bytes: int) -> bytes | None:
        path = job_dir / rel
        return path.read_bytes()[:max_bytes + 1] if path.is_file() else None

    async def sha256_file(rel: str) -> str | None:
        path = job_dir / rel
        return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None

    source_rel = "intermediate/source_segments.json"
    provenance_rel = "output/provenance/smart.json"
    records = await build_canonical_evidence_records_with_reader(
        job_id=job_dir.name,
        pipeline="document",
        note_type="smart",
        note_path=note_rel,
        note_data=note_data,
        normalized_body=body,
        chunks=chunks,
        source_manifest_data=(job_dir / source_rel).read_bytes(),
        source_manifest_path=source_rel,
        provenance_path=provenance_rel,
        provenance_data=(job_dir / provenance_rel).read_bytes(),
        read_file=read_file,
        sha256_file=sha256_file,
    )
    assert len(records) == 1
    db.index_job_notes(
        job_dir.name, "smart", "Evidence Paper", body,
        "document", "e2e", "", ["smart"], records,
    )
    return records[0]["evidence_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source_profile", "link_kind"),
    [("scholarly_html", "text"), ("digital_pdf", "pdf")],
)
async def test_document_evidence_reaches_all_consumers_and_tamper_fails_closed(
    source_profile: str,
    link_kind: str,
    client,
    db: Database,
    test_config,
) -> None:
    claim = (
        f"{source_profile.replace('_', ' ')} canonical evidence has a unique exact statement"
    )
    job_dir = test_config.jobs_dir / f"job-e2e-{source_profile}"
    source_rel, note_rel = _prepare_document(job_dir, source_profile, claim)
    db.create_job(Job(
        id=job_dir.name,
        content_type="document",
        document_kind="research_paper",
        pipeline="document",
        title="Evidence Paper",
        domain="e2e",
        status=JobStatus.DONE,
    ))
    evidence_id = await _index_document_note(db, job_dir, note_rel)

    search = (await client.get("/api/search", params={"q": claim})).json()
    ask = (await client.post("/api/ask", json={"question": claim})).json()
    mcp = build_server(db, LocalStorage(test_config.jobs_dir))
    mcp_items = _mcp_payload(await mcp.call_tool("search", {"query": claim}))
    projections = (
        search["items"][0]["canonical_evidence"],
        ask["sources"][0]["canonical_evidence"],
        mcp_items[0]["canonical_evidence"],
    )
    for projection in projections:
        assert [item["evidence_id"] for item in projection] == [evidence_id]
        assert projection[0]["status"] == "valid"
        assert projection[0]["locator"]["kind"] == link_kind
        assert projection[0]["link"]["kind"] == link_kind

    source = job_dir / source_rel
    source.write_bytes(source.read_bytes() + b" tampered")
    stale = await resolve_canonical_evidence(
        db, LocalStorage(test_config.jobs_dir), evidence_id,
    )
    assert stale is not None
    assert stale["status"] == "stale"
    assert stale["locator"] is None and stale["link"] is None
