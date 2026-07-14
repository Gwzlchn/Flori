"""验证四类 producer 到 Search、Ask、MCP 的 canonical evidence 闭环。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from PIL import Image

from api.mcp_server.server import build_server
from api.services.evidence import resolve_canonical_evidence
from scheduler.scheduler import _markdown_to_text
from shared.db import Database, _chunk_note_body
from shared.evidence_contract import build_canonical_evidence_records_with_reader
from shared.storage import LocalStorage
from steps.article.provenance import (
    build_html_source_manifest,
    direct_text_provenance_candidates,
    extract_note_markers,
    load_source_manifest,
    persist_note_provenance,
    publish_source_manifest,
)
from steps.audio.provenance import (
    build_audio_source_manifest,
    extract_smart_markers as extract_audio_smart_markers,
    persist_audio_note_provenance,
    smart_provenance_segments as audio_smart_provenance_segments,
    transcript_provenance_segments as audio_provenance_segments,
    write_audio_source_manifest,
)
from steps.utils.srt_parser import SrtEntry
from steps.paper.step_02_pdf_parse import PdfParseStep
from steps.paper.step_05_smart_paper import SmartPaperStep
from steps.video.provenance import (
    build_video_source_manifest,
    extract_smart_markers as extract_video_smart_markers,
    mechanical_ocr_provenance_segments,
    persist_video_note_provenance,
    smart_provenance_segments as video_smart_provenance_segments,
    transcript_provenance_segments as video_provenance_segments,
    write_video_source_manifest,
)
from tests.steps.conftest import make_step_config


CASES = (
    ("video", "视证", "media"),
    ("audio", "音证", "media"),
    ("paper", "paperproof", "pdf"),
    ("article", "文证", "text"),
)


def _minimal_text_pdf(*pages: str) -> bytes:
    """生成可由真实 Poppler 读取的确定性 Helvetica 小 PDF。"""
    if not pages:
        raise ValueError("PDF requires at least one page")
    font_number = 3 + len(pages) * 2
    page_numbers = [3 + index * 2 for index in range(len(pages))]
    objects: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: (
            f"<< /Type /Pages /Kids [{' '.join(f'{number} 0 R' for number in page_numbers)}] "
            f"/Count {len(pages)} >>"
        ).encode("ascii"),
        font_number: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }
    for index, page_text in enumerate(pages):
        try:
            encoded = page_text.encode("ascii")
        except UnicodeEncodeError as exc:
            raise ValueError("test PDF text must be ASCII") from exc
        encoded = encoded.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")
        page_number = page_numbers[index]
        content_number = page_number + 1
        content = b"BT /F1 12 Tf 72 720 Td (" + encoded + b") Tj ET" if encoded else b""
        objects[page_number] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 {font_number} 0 R >> >> "
            f"/Contents {content_number} 0 R >>"
        ).encode("ascii")
        objects[content_number] = (
            f"<< /Length {len(content)} >>\nstream\n".encode("ascii")
            + content
            + b"\nendstream"
        )

    data = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: dict[int, int] = {}
    for number in range(1, font_number + 1):
        offsets[number] = len(data)
        data.extend(f"{number} 0 obj\n".encode("ascii"))
        data.extend(objects[number])
        data.extend(b"\nendobj\n")
    xref_offset = len(data)
    data.extend(f"xref\n0 {font_number + 1}\n".encode("ascii"))
    data.extend(b"0000000000 65535 f \n")
    for number in range(1, font_number + 1):
        data.extend(f"{offsets[number]:010d} 00000 n \n".encode("ascii"))
    data.extend(
        f"trailer\n<< /Size {font_number + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF\n".encode("ascii")
    )
    return bytes(data)


def _mcp_payload(result) -> list[dict]:
    structured = result[1] if isinstance(result, tuple) and len(result) == 2 else None
    if isinstance(structured, dict) and set(structured) == {"result"}:
        return structured["result"]
    if isinstance(structured, list):
        return structured
    blocks = result[0] if isinstance(result, tuple) else result
    return json.loads(blocks[0].text)


def _prepare_pdf_smart(job_dir: Path, claim: str) -> str:
    """执行真实 PDF parse 和 SmartPaper producer,返回其版本化笔记路径。"""
    for rel in ("input", "output", "intermediate"):
        (job_dir / rel).mkdir(parents=True, exist_ok=True)
    (job_dir / "input/source.pdf").write_bytes(_minimal_text_pdf(claim))
    (job_dir / "input/metadata.json").write_text(
        json.dumps({"title": "A Trustworthy Evidence Paper"}), encoding="utf-8",
    )
    parse_step = PdfParseStep(
        "02_pdf_parse",
        job_dir,
        make_step_config(
            job_dir.parent,
            step_name="02_pdf_parse",
            pool="cpu",
            pipeline="paper",
        ),
    )
    parse_step.execute()
    parsed = json.loads(
        (job_dir / "intermediate/parsed.json").read_text(encoding="utf-8")
    )
    (job_dir / "intermediate/sections.json").write_text(
        json.dumps(parsed), encoding="utf-8",
    )
    manifest = load_source_manifest(job_dir, pipeline="paper")
    assert manifest is not None
    assert manifest["source_artifacts"][0]["sha256"] == hashlib.sha256(
        (job_dir / "input/source.pdf").read_bytes()
    ).hexdigest()
    assert manifest["segments"][0]["locator"] == {
        "kind": "pdf", "page": 1, "bbox": None,
    }
    assert manifest["segments"][0]["support_text"] == claim
    token = manifest["segments"][0]["segment_id"].removeprefix("seg_")
    smart_step = SmartPaperStep(
        "05_smart_paper",
        job_dir,
        make_step_config(
            job_dir.parent,
            step_name="05_smart_paper",
            pool="ai",
            pipeline="paper",
        ),
    )
    filler = "Additional context remains separate from the cited exact claim. " * 12
    smart_step.ai.call = lambda *_a, **_k: (
        f"# Smart paper\n\n{claim} [[source:{token}]]\n\n## Context\n\n{filler}"
    )
    result = smart_step.execute()
    assert result is not None
    assert result["source"] == "pdf-direct"
    assert result["provenance_status"] == "written"
    assert result["provenance_segments"] == 1
    return result["note_file"]


def _prepare_producer(
    job_dir: Path, pipeline: str, needle: str,
) -> tuple[str, str, str]:
    """用各 pipeline 的生产 helper 写真实 source/provenance sidecar。"""
    for rel in ("input", "output", "intermediate"):
        (job_dir / rel).mkdir(parents=True, exist_ok=True)
    note_path = "output/notes.md"

    if pipeline == "video":
        (job_dir / "input/source.mp4").write_bytes(b"deterministic-video")
        (job_dir / "input/metadata.json").write_text(
            '{"duration_sec": 8}', encoding="utf-8",
        )
        entry = SrtEntry(1, 2.0, 4.0, f"{needle} 视频来源")
        (job_dir / "input/subtitle.srt").write_text(
            f"1\n00:00:02,000 --> 00:00:04,000\n{entry.text}\n",
            encoding="utf-8",
        )
        manifest = build_video_source_manifest(job_dir, [entry])
        assert manifest is not None
        write_video_source_manifest(job_dir, manifest)
        note = f"# Transcript\n\n[00:02] {needle} 视频来源\n"
        (job_dir / note_path).write_text(note, encoding="utf-8")
        body = _markdown_to_text(note)
        mappings = video_provenance_segments(
            [f"[00:02] {needle} 视频来源"], manifest, body,
        )
        persist_video_note_provenance(
            job_dir, note_type="transcript", note_artifact=note_path,
            provenance_segments=mappings,
        )
        return "transcript", "input/source.mp4", note_path

    if pipeline == "audio":
        (job_dir / "input/source.mp3").write_bytes(b"deterministic-audio")
        (job_dir / "input/metadata.json").write_text(
            '{"duration_sec": 8}', encoding="utf-8",
        )
        transcript = [{"start": 1.0, "end": 3.0, "text": f"{needle} 音频来源"}]
        (job_dir / "intermediate/segments.json").write_text(
            json.dumps(transcript), encoding="utf-8",
        )
        manifest = build_audio_source_manifest(job_dir, transcript)
        assert manifest is not None
        write_audio_source_manifest(job_dir, manifest)
        note = f"# Transcript\n\n[00:01] {needle} 音频来源\n"
        (job_dir / note_path).write_text(note, encoding="utf-8")
        body = _markdown_to_text(note)
        mappings = audio_provenance_segments(transcript, manifest, body)
        persist_audio_note_provenance(
            job_dir, note_type="transcript", note_artifact=note_path,
            provenance_segments=mappings,
        )
        return "transcript", "input/source.mp3", note_path

    if pipeline == "paper":
        claim = f"{needle} is an exact statement extracted from the PDF page"
        return "smart", "input/source.pdf", _prepare_pdf_smart(job_dir, claim)

    source_text = f"{needle} article source paragraph with unique evidence"
    (job_dir / "input/source.html").write_text(
        f"<html><article><p>{source_text}</p></article></html>", encoding="utf-8",
    )
    manifest = build_html_source_manifest(job_dir, pipeline="article")
    assert manifest is not None
    publish_source_manifest(job_dir, manifest)
    note = f"# Article\n\n{source_text}\n"
    (job_dir / note_path).write_text(note, encoding="utf-8")
    persist_note_provenance(
        job_dir, pipeline="article", note_type="original", note_artifact=note_path,
        candidates=direct_text_provenance_candidates(
            manifest, note, section="article",
        ),
    )
    return "original", "input/source.html", note_path


async def _index_produced_note(
    db: Database,
    job_dir: Path,
    pipeline: str,
    note_type: str,
    *,
    note_path: str = "output/notes.md",
    require_records: bool = True,
) -> tuple[str, str | None]:
    source_path = "intermediate/source_segments.json"
    provenance_path = f"output/provenance/{note_type}.json"
    note_data = (job_dir / note_path).read_bytes()
    body = _markdown_to_text(note_data.decode("utf-8"))
    chunks = [{
        "chunk_id": f"{job_dir.name}:{note_type}:{index}",
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

    records = await build_canonical_evidence_records_with_reader(
        job_id=job_dir.name,
        pipeline=pipeline,
        note_type=note_type,
        note_path=note_path,
        note_data=note_data,
        normalized_body=body,
        chunks=chunks,
        source_manifest_data=(job_dir / source_path).read_bytes(),
        source_manifest_path=source_path,
        provenance_path=provenance_path,
        provenance_data=(job_dir / provenance_path).read_bytes(),
        read_file=read_file,
        sha256_file=sha256_file,
    )
    if require_records:
        assert records
    db.index_job_notes(
        job_dir.name, note_type, pipeline.title(), body,
        pipeline, "e2e", "", [note_type], records,
    )
    return body, records[0]["evidence_id"] if records else None


def _prepare_smart_producer(
    job_dir: Path, pipeline: str, claim: str,
) -> tuple[str, str]:
    """用真实 producer helper 写 exact-quote smart sidecar。"""
    for rel in ("input", "output", "intermediate"):
        (job_dir / rel).mkdir(parents=True, exist_ok=True)
    note_path = "output/notes.md"
    if pipeline == "paper":
        return "input/source.pdf", _prepare_pdf_smart(job_dir, claim)

    if pipeline == "article":
        (job_dir / "input/source.html").write_text(
            f"<html><article><p>{claim}</p></article></html>", encoding="utf-8",
        )
        manifest = build_html_source_manifest(job_dir, pipeline=pipeline)
        assert manifest is not None
        publish_source_manifest(job_dir, manifest)
        token = manifest["segments"][0]["segment_id"].removeprefix("seg_")
        note, mappings = extract_note_markers(
            f"# Smart\n\n{claim} [[source:{token}]]", manifest,
        )
        (job_dir / note_path).write_text(note, encoding="utf-8")
        persist_note_provenance(
            job_dir,
            pipeline=pipeline,
            note_type="smart",
            note_artifact=note_path,
            candidates=mappings,
        )
        return "input/source.html", note_path

    if pipeline == "audio":
        (job_dir / "input/source.mp3").write_bytes(b"smart-audio")
        (job_dir / "input/metadata.json").write_text('{"duration_sec": 8}')
        transcript = [{"start": 1.0, "end": 3.0, "text": claim}]
        (job_dir / "intermediate/segments.json").write_text(
            json.dumps(transcript), encoding="utf-8",
        )
        manifest = build_audio_source_manifest(job_dir, transcript)
        assert manifest is not None
        write_audio_source_manifest(job_dir, manifest)
        token = manifest["segments"][0]["segment_id"].removeprefix("seg_")
        note, candidates = extract_audio_smart_markers(
            f"# Smart\n\n{claim} [[source:{token}]]", manifest,
        )
        (job_dir / note_path).write_text(note, encoding="utf-8")
        mappings = audio_smart_provenance_segments(_markdown_to_text(note), candidates)
        persist_audio_note_provenance(
            job_dir,
            note_type="smart",
            note_artifact=note_path,
            provenance_segments=mappings,
        )
        return "input/source.mp3", note_path

    (job_dir / "input/source.mp4").write_bytes(b"smart-video")
    (job_dir / "input/metadata.json").write_text('{"duration_sec": 8}')
    entry = SrtEntry(1, 1.0, 3.0, claim)
    (job_dir / "input/subtitle.srt").write_text(
        f"1\n00:00:01,000 --> 00:00:03,000\n{claim}\n", encoding="utf-8",
    )
    manifest = build_video_source_manifest(job_dir, [entry])
    assert manifest is not None
    write_video_source_manifest(job_dir, manifest)
    token = manifest["segments"][0]["segment_id"].removeprefix("seg_")
    note, candidates = extract_video_smart_markers(
        f"# Smart\n\n{claim} [[source:{token}]]", manifest,
    )
    (job_dir / note_path).write_text(note, encoding="utf-8")
    mappings = video_smart_provenance_segments(_markdown_to_text(note), candidates)
    persist_video_note_provenance(
        job_dir,
        note_type="smart",
        note_artifact=note_path,
        provenance_segments=mappings,
    )
    return "input/source.mp4", note_path


def _insert_job(db: Database, job_id: str, pipeline: str) -> None:
    db._conn.execute(
        """INSERT INTO jobs
           (id,content_type,pipeline,title,domain,status,is_current,created_at,updated_at)
           VALUES (?,?,?,?,?,'done',1,'now','now')""",
        (job_id, pipeline, pipeline, pipeline.title(), "e2e"),
    )
    db._conn.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("pipeline,needle,link_kind", CASES)
async def test_four_producers_reach_search_ask_mcp_and_tamper_fails_closed(
    pipeline: str,
    needle: str,
    link_kind: str,
    client,
    db: Database,
    test_config,
):
    job_dir = test_config.jobs_dir / f"job-e2e-{pipeline}"
    note_type, source_rel, note_path = _prepare_producer(job_dir, pipeline, needle)
    _insert_job(db, job_dir.name, pipeline)
    _body, evidence_id = await _index_produced_note(
        db, job_dir, pipeline, note_type, note_path=note_path,
    )

    search = await client.get("/api/search", params={"q": needle})
    assert search.status_code == 200
    search_projection = search.json()["items"][0]["canonical_evidence"]

    ask = await client.post("/api/ask", json={"question": needle})
    assert ask.status_code == 202
    ask_projection = ask.json()["sources"][0]["canonical_evidence"]

    mcp = build_server(db, LocalStorage(test_config.jobs_dir))
    mcp_items = _mcp_payload(await mcp.call_tool("search", {"query": needle}))
    mcp_projection = mcp_items[0]["canonical_evidence"]

    for projection in (search_projection, ask_projection, mcp_projection):
        assert [item["evidence_id"] for item in projection] == [evidence_id]
        assert projection[0]["status"] == "valid"
        assert projection[0]["locator"]["kind"] == link_kind
        assert projection[0]["link"]["kind"] == link_kind
        assert "source_path" not in projection[0]

    source = job_dir / source_rel
    source.write_bytes(source.read_bytes() + b" tampered")
    stale = await resolve_canonical_evidence(
        db, LocalStorage(test_config.jobs_dir), evidence_id,
    )
    assert stale is not None
    assert stale["status"] == "stale"
    assert stale["link"] is None
    assert stale["locator"] is None


@pytest.mark.asyncio
async def test_legacy_note_projects_empty_evidence_in_all_consumers(
    client, db: Database, test_config,
):
    job_id = "job-e2e-legacy"
    _insert_job(db, job_id, "article")
    body = "# Legacy\n\n旧证 内容没有 provenance。"
    db.index_job_notes(
        job_id, "original", "Legacy", body, "article", "e2e", "", ["original"],
    )

    search = await client.get("/api/search", params={"q": "旧证"})
    assert search.json()["items"][0]["canonical_evidence"] == []
    ask = await client.post("/api/ask", json={"question": "旧证"})
    assert ask.json()["sources"][0]["canonical_evidence"] == []
    mcp = build_server(db, LocalStorage(test_config.jobs_dir))
    items = _mcp_payload(await mcp.call_tool("search", {"query": "旧证"}))
    assert items[0]["canonical_evidence"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("pipeline", "support_kind"), [
    ("article", "html"),
    ("paper", "pdf_pages"),
    ("audio", "audio_segments"),
    ("video", "video_subtitle"),
], ids=["html", "pdf-pages", "audio-segments", "video-srt"])
async def test_smart_support_tamper_or_delete_fails_closed_in_all_consumers(
    pipeline: str,
    support_kind: str,
    client,
    db: Database,
    test_config,
):
    claim = f"{pipeline} exact quote has enough unique source text"
    job_dir = test_config.jobs_dir / f"job-e2e-smart-{pipeline}"
    _source_rel, note_path = _prepare_smart_producer(job_dir, pipeline, claim)
    _insert_job(db, job_dir.name, pipeline)
    _body, evidence_id = await _index_produced_note(
        db, job_dir, pipeline, "smart", note_path=note_path,
    )

    search = await client.get("/api/search", params={"q": claim})
    ask = await client.post("/api/ask", json={"question": claim})
    mcp = build_server(db, LocalStorage(test_config.jobs_dir))
    mcp_items = _mcp_payload(await mcp.call_tool("search", {"query": claim}))
    projections = (
        search.json()["items"][0]["canonical_evidence"],
        ask.json()["sources"][0]["canonical_evidence"],
        mcp_items[0]["canonical_evidence"],
    )
    for projection in projections:
        assert [item["evidence_id"] for item in projection] == [evidence_id]
        assert projection[0]["status"] == "valid"

    manifest_path = job_dir / "intermediate/source_segments.json"
    original_manifest = manifest_path.read_bytes()
    tampered = json.loads(manifest_path.read_text(encoding="utf-8"))
    tampered["segments"][0]["support_text"] = "attacker supplied support"
    manifest_path.write_text(json.dumps(tampered), encoding="utf-8")
    stale = await resolve_canonical_evidence(
        db, LocalStorage(test_config.jobs_dir), evidence_id,
    )
    assert stale is not None and stale["status"] == "stale"
    assert stale["locator"] is None and stale["link"] is None
    manifest_path.write_bytes(original_manifest)
    recovered = await resolve_canonical_evidence(
        db, LocalStorage(test_config.jobs_dir), evidence_id,
    )
    assert recovered is not None and recovered["status"] == "valid"

    support_artifact = json.loads(original_manifest)["segments"][0][
        "support_artifact"
    ]
    assert support_artifact is not None
    assert support_artifact["kind"] == support_kind
    support_path = job_dir / support_artifact["path"]
    original_support = support_path.read_bytes()
    support_path.write_bytes(original_support + b"\ntampered support")
    stale_support = await resolve_canonical_evidence(
        db, LocalStorage(test_config.jobs_dir), evidence_id,
    )
    assert stale_support is not None and stale_support["status"] == "stale"
    assert stale_support["reason"] == (
        "source_changed" if support_kind == "html" else "support_artifact_changed"
    )
    assert stale_support["locator"] is None and stale_support["link"] is None

    support_path.unlink()
    missing_support = await resolve_canonical_evidence(
        db, LocalStorage(test_config.jobs_dir), evidence_id,
    )
    assert missing_support is not None and missing_support["status"] == "missing"
    assert missing_support["reason"] == (
        "source_missing" if support_kind == "html" else "support_artifact_missing"
    )
    assert missing_support["locator"] is None and missing_support["link"] is None

    support_path.parent.mkdir(parents=True, exist_ok=True)
    support_path.write_bytes(original_support)
    recovered = await resolve_canonical_evidence(
        db, LocalStorage(test_config.jobs_dir), evidence_id,
    )
    assert recovered is not None and recovered["status"] == "valid"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["paraphrase", "pdf-only"])
async def test_unverifiable_smart_claim_projects_empty_evidence_everywhere(
    mode: str,
    client,
    db: Database,
    test_config,
):
    pipeline = "article" if mode == "paraphrase" else "paper"
    job_dir = test_config.jobs_dir / f"job-e2e-smart-{mode}"
    for rel in ("input", "output", "intermediate"):
        (job_dir / rel).mkdir(parents=True, exist_ok=True)
    if mode == "paraphrase":
        source_claim = "source contains a precise statement for the exact quote gate"
        note_claim = "the source roughly says something similar"
        (job_dir / "input/source.html").write_text(
            f"<article><p>{source_claim}</p></article>", encoding="utf-8",
        )
        manifest = build_html_source_manifest(job_dir, pipeline=pipeline)
    else:
        note_claim = "Blank PDF pages cannot support an exact textual claim"
        (job_dir / "input/source.pdf").write_bytes(_minimal_text_pdf(""))
        (job_dir / "input/metadata.json").write_text(
            json.dumps({"title": "A Blank Evidence Paper"}), encoding="utf-8",
        )
        parse_step = PdfParseStep(
            "02_pdf_parse",
            job_dir,
            make_step_config(
                job_dir.parent,
                step_name="02_pdf_parse",
                pool="cpu",
                pipeline="paper",
            ),
        )
        parse_step.execute()
        manifest = load_source_manifest(job_dir, pipeline="paper")
    assert manifest is not None
    publish_source_manifest(job_dir, manifest)
    token = manifest["segments"][0]["segment_id"].removeprefix("seg_")
    note, mappings = extract_note_markers(
        f"# Smart\n\n{note_claim} [[source:{token}]]", manifest,
    )
    assert mappings == []
    (job_dir / "output/notes.md").write_text(note, encoding="utf-8")
    persisted = persist_note_provenance(
        job_dir,
        pipeline=pipeline,
        note_type="smart",
        note_artifact="output/notes.md",
        candidates=mappings,
    )
    assert persisted["status"] == "written_empty"
    _insert_job(db, job_dir.name, pipeline)
    _body, evidence_id = await _index_produced_note(
        db, job_dir, pipeline, "smart", require_records=False,
    )
    assert evidence_id is None

    search = await client.get("/api/search", params={"q": note_claim})
    ask = await client.post("/api/ask", json={"question": note_claim})
    mcp = build_server(db, LocalStorage(test_config.jobs_dir))
    mcp_items = _mcp_payload(await mcp.call_tool("search", {"query": note_claim}))
    assert search.json()["items"][0]["canonical_evidence"] == []
    assert ask.json()["sources"][0]["canonical_evidence"] == []
    assert mcp_items[0]["canonical_evidence"] == []


@pytest.mark.asyncio
async def test_video_ocr_support_and_image_tamper_or_delete_fail_closed(
    client, db: Database, test_config,
):
    job_dir = test_config.jobs_dir / "job-e2e-video-image"
    for rel in ("input", "output", "intermediate", "assets"):
        (job_dir / rel).mkdir(parents=True, exist_ok=True)
    (job_dir / "input/source.mp4").write_bytes(b"deterministic-video")
    (job_dir / "input/metadata.json").write_text('{"duration_sec": 8}')
    frame = job_dir / "assets/frame.png"
    Image.new("RGB", (16, 12), color="white").save(frame)
    frame_sha256 = hashlib.sha256(frame.read_bytes()).hexdigest()
    ocr = [{
        "filename": frame.name,
        "timestamp_sec": 2.5,
        "asset_sha256": frame_sha256,
        "width": 16,
        "height": 12,
        "text": "图像证据唯一文本",
        "boxes": [{
            "text": "图像证据唯一文本",
            "box": [[1, 2], [9, 2], [9, 8], [1, 8]],
        }],
    }]
    (job_dir / "intermediate/ocr.json").write_text(json.dumps(ocr))
    manifest = build_video_source_manifest(job_dir, [])
    assert manifest is not None
    write_video_source_manifest(job_dir, manifest)
    note = "# OCR\n\n![00:02](assets/frame.png)\n\n> OCR：图像证据唯一文本\n"
    (job_dir / "output/notes.md").write_text(note)
    mappings = mechanical_ocr_provenance_segments(
        ocr,
        manifest,
        _markdown_to_text(note),
        rendered_markdown=note,
    )
    persist_video_note_provenance(
        job_dir,
        note_type="mechanical",
        note_artifact="output/notes.md",
        provenance_segments=mappings,
    )
    _insert_job(db, job_dir.name, "video")
    _body, evidence_id = await _index_produced_note(
        db, job_dir, "video", "mechanical",
    )

    search = await client.get("/api/search", params={"q": "图像证据唯一文本"})
    ask = await client.post("/api/ask", json={"question": "图像证据唯一文本"})
    mcp = build_server(db, LocalStorage(test_config.jobs_dir))
    mcp_items = _mcp_payload(await mcp.call_tool(
        "search", {"query": "图像证据唯一文本"},
    ))
    projections = (
        search.json()["items"][0]["canonical_evidence"],
        ask.json()["sources"][0]["canonical_evidence"],
        mcp_items[0]["canonical_evidence"],
    )
    for projection in projections:
        assert [item["evidence_id"] for item in projection] == [evidence_id]
        assert projection[0]["status"] == "valid"
        assert projection[0]["locator"]["kind"] == "image"
        assert projection[0]["link"]["kind"] == "image"
        assert "asset_path" not in projection[0]["locator"]
        assert "asset_path" not in projection[0]
        assert "source_path" not in projection[0]

    valid = await resolve_canonical_evidence(
        db, LocalStorage(test_config.jobs_dir), evidence_id,
    )
    assert valid is not None and valid["status"] == "valid"
    assert valid["locator"] == {
        "kind": "image",
        "bbox": [1, 2, 9, 8],
        "start_ms": 2500,
        "end_ms": 2501,
        "page": None,
    }
    assert valid["link"] is not None and valid["link"]["kind"] == "image"

    ocr_path = job_dir / "intermediate/ocr.json"
    original_ocr = ocr_path.read_bytes()
    ocr_path.write_bytes(original_ocr + b"\ntampered support")
    stale_support = await resolve_canonical_evidence(
        db, LocalStorage(test_config.jobs_dir), evidence_id,
    )
    assert stale_support is not None and stale_support["status"] == "stale"
    assert stale_support["reason"] == "support_artifact_changed"
    assert stale_support["locator"] is None and stale_support["link"] is None

    ocr_path.unlink()
    missing_support = await resolve_canonical_evidence(
        db, LocalStorage(test_config.jobs_dir), evidence_id,
    )
    assert missing_support is not None and missing_support["status"] == "missing"
    assert missing_support["reason"] == "support_artifact_missing"
    assert missing_support["locator"] is None and missing_support["link"] is None

    ocr_path.write_bytes(original_ocr)
    recovered = await resolve_canonical_evidence(
        db, LocalStorage(test_config.jobs_dir), evidence_id,
    )
    assert recovered is not None and recovered["status"] == "valid"

    frame.write_bytes(b"tampered-frame")
    stale = await resolve_canonical_evidence(
        db, LocalStorage(test_config.jobs_dir), evidence_id,
    )
    assert stale is not None and stale["status"] == "stale"
    assert stale["reason"] == "image_changed"
    assert stale["locator"] is None and stale["link"] is None
