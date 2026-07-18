"""从 Document blocks 生成现有 canonical source manifest。"""

from __future__ import annotations

import hashlib
import html as html_lib
import json
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from shared.note_text import markdown_to_index_text
from shared.provenance import (
    DIRECT_LOCATOR_POLICY,
    bounded_support_text,
    build_provenance_manifest,
    build_source_manifest,
    extract_attestable_markers,
    extract_exact_quote_markers,
    validate_source_manifest,
    write_provenance_manifest,
    write_source_manifest,
)
from steps.utils.provenance_attestation import producer_invocation_id


SOURCE_MANIFEST_PATH = "intermediate/source_segments.json"
PDF_SUPPORT_PATH = "intermediate/pdf_page_support.json"
OCR_EXACT_EVIDENCE_THRESHOLD = 0.8
DOCUMENT_INDEX_PATH = "intermediate/document_index.md"


def _html_support_range(source: str, exact: str) -> tuple[int, int, str] | None:
    """定位原始 HTML 中真实连续文本;内联标签拆开的块退到最长唯一文本节点。"""
    direct = (exact, html_lib.escape(exact, quote=False))
    for candidate in direct:
        if candidate and source.count(candidate) == 1:
            start = source.index(candidate)
            return start, start + len(candidate), candidate

    normalized_exact = re.sub(r"\s+", " ", html_lib.unescape(exact)).strip()
    candidates: list[tuple[int, int, str, int]] = []
    for match in re.finditer(r">([^<]+)<", source, flags=re.S):
        raw = match.group(1)
        left = len(raw) - len(raw.lstrip())
        right = len(raw.rstrip())
        raw = raw[left:right]
        if not raw or source.count(raw) != 1:
            continue
        visible = re.sub(r"\s+", " ", html_lib.unescape(raw)).strip()
        if len(visible) < 8 or visible not in normalized_exact:
            continue
        start = match.start(1) + left
        candidates.append((start, start + len(raw), raw, len(visible)))
    if not candidates:
        return None
    start, end, raw, _length = max(candidates, key=lambda item: (item[3], -item[0]))
    return start, end, raw


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _html_segment(
    block: Mapping[str, Any], source: str, source_sha: str,
) -> dict[str, Any] | None:
    locator = block.get("locator")
    html = locator.get("html") if isinstance(locator, Mapping) else None
    if not isinstance(html, Mapping):
        return None
    exact = str(html.get("exact") or block.get("text") or "").strip()
    if not exact:
        return None
    start = html.get("start")
    end = html.get("end")
    raw_exact = exact
    if (
        type(start) is not int
        or type(end) is not int
        or not 0 <= start < end <= len(source)
        or source[start:end] != exact
    ):
        support = _html_support_range(source, exact)
        if support is None:
            return None
        start, end, raw_exact = support
    canonical = {
        "kind": "text",
        "exact": raw_exact,
        "prefix": str(html.get("prefix") or source[max(0, start - 32):start]),
        "suffix": str(html.get("suffix") or source[end:end + 32]),
        "dom_path": str(html.get("dom_path") or ""),
    }
    return {
        "segment_id": str(block["block_id"]),
        "source_id": "html",
        "start": start,
        "end": end,
        "section": str(block.get("parent_id") or block["block_id"]),
        "locator": canonical,
        "support_text": bounded_support_text(html_lib.unescape(raw_exact)),
        "support_artifact": {
            "kind": "html",
            "path": "input/source.html",
            "sha256": source_sha,
            "selector": {"start": start, "end": end},
        },
    }


def _pdf_support(
    job_dir: Path, blocks: list[Mapping[str, Any]],
) -> tuple[Path, dict[int, str], dict[str, tuple[int, int, int]]]:
    page_text: dict[int, str] = {}
    block_ranges: dict[str, tuple[int, int, int]] = {}
    for block in blocks:
        locator = block.get("locator")
        pdf = locator.get("pdf") if isinstance(locator, Mapping) else None
        if not isinstance(pdf, Mapping) or type(pdf.get("page")) is not int:
            continue
        text = str(block.get("text") or "").strip()
        if text:
            page = int(pdf["page"])
            current = page_text.get(page, "")
            start = len(current) + (1 if current else 0)
            page_text[page] = f"{current}\n{text}" if current else text
            block_ranges[str(block["block_id"])] = (page, start, start + len(text))
    page_count = max(page_text, default=0)
    source_sha = _sha256(job_dir / "input" / "source.pdf")
    path = job_dir / PDF_SUPPORT_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source_sha256": source_sha,
                "pages": [
                    {"page": page, "support_text": page_text.get(page, "")}
                    for page in range(1, page_count + 1)
                ],
            },
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    return path, page_text, block_ranges


def _pdf_segment(
    block: Mapping[str, Any], support_sha: str, page_text: Mapping[int, str],
    block_ranges: Mapping[str, tuple[int, int, int]],
) -> dict[str, Any] | None:
    locator = block.get("locator")
    pdf = locator.get("pdf") if isinstance(locator, Mapping) else None
    if not isinstance(pdf, Mapping) or type(pdf.get("page")) is not int:
        return None
    page = int(pdf["page"])
    bboxes = pdf.get("bboxes") if isinstance(pdf.get("bboxes"), list) else []
    bbox = bboxes[0] if bboxes else None
    text = str(block.get("text") or "").strip()
    confidence = pdf.get("ocr_confidence", block.get("ocr_confidence"))
    reliable_text = not (
        type(confidence) in (int, float)
        and float(confidence) < OCR_EXACT_EVIDENCE_THRESHOLD
    )
    support = bounded_support_text(text) if reliable_text else None
    support_range = block_ranges.get(str(block["block_id"]))
    if support_range is not None and support_range[0] != page:
        raise ValueError("PDF support range belongs to another page")
    return {
        "segment_id": str(block["block_id"]),
        "source_id": "pdf",
        "start": None,
        "end": None,
        "section": str(block.get("parent_id") or block["block_id"]),
        "locator": {"kind": "pdf", "page": page, "bbox": bbox},
        "support_text": support,
        "support_artifact": ({
            "kind": "pdf_pages",
            "path": PDF_SUPPORT_PATH,
            "sha256": support_sha,
            "selector": {
                "page": page, "start": support_range[1], "end": support_range[2],
            },
        } if support is not None and page in page_text and support_range else None),
    }


def build_document_source_manifest(job_dir: Path, document: Mapping[str, Any]) -> dict:
    """只从已验证 blocks 投影 canonical manifest，不再 regex 扫描或截 64 段。"""
    blocks = [item for item in document["blocks"] if isinstance(item, Mapping)]
    artifacts: list[dict[str, Any]] = []
    segments: list[dict[str, Any]] = []
    represented_blocks: set[str] = set()
    html_path = job_dir / "input" / "source.html"
    pdf_path = job_dir / "input" / "source.pdf"
    if html_path.is_file():
        source = html_path.read_text(encoding="utf-8")
        source_sha = _sha256(html_path)
        artifacts.append({
            "source_id": "html", "path": "input/source.html", "sha256": source_sha,
            "revision": next(
                (item.get("revision") for item in document.get("sources", [])
                 if item.get("source_id") == "html"),
                None,
            ),
            "media_duration_ms": None, "page_count": None,
        })
        for block in blocks:
            item = _html_segment(block, source, source_sha)
            if item is None:
                continue
            segments.append(item)
            represented_blocks.add(str(block["block_id"]))
    if pdf_path.is_file():
        support_path, page_text, block_ranges = _pdf_support(job_dir, blocks)
        support_sha = _sha256(support_path)
        pages = max(page_text, default=int(document.get("metadata", {}).get("pages") or 0))
        artifacts.append({
            "source_id": "pdf", "path": "input/source.pdf", "sha256": _sha256(pdf_path),
            "revision": next(
                (item.get("revision") for item in document.get("sources", [])
                 if item.get("source_id") == "pdf"),
                None,
            ),
            "media_duration_ms": None, "page_count": pages or None,
        })
        for block in blocks:
            # 一个 Document block 只能映射成一个 canonical source segment。
            # 同时存在 HTML/PDF 时，HTML 用于精确文本定位，PDF crosswalk 保留在
            # document.json locator 中，避免同一 segment_id 在 manifest 内碰撞。
            if str(block["block_id"]) in represented_blocks:
                continue
            item = _pdf_segment(block, support_sha, page_text, block_ranges)
            if item is not None:
                segments.append(item)
    if not artifacts or not segments:
        raise ValueError("document has no canonical source segments")
    return build_source_manifest(
        job_id=job_dir.name,
        pipeline="document",
        source_artifacts=artifacts,
        segments=segments,
    )


def publish_document_source_manifest(job_dir: Path, document: Mapping[str, Any]) -> dict:
    manifest = build_document_source_manifest(job_dir, document)
    write_source_manifest(
        job_dir / SOURCE_MANIFEST_PATH, manifest, trusted_root=job_dir,
    )
    return manifest


def publish_document_index_projection(
    job_dir: Path,
    document: Mapping[str, Any],
) -> dict[str, Any]:
    """从 Document blocks 生成可重建的检索投影,并绑定原生来源 locator。"""
    source_manifest = load_document_source_manifest(job_dir)
    if source_manifest is None:
        raise ValueError("document index projection requires source manifest")
    known_segments = {
        str(item["segment_id"]) for item in source_manifest["segments"]
    }
    rendered: list[tuple[str, str, str]] = []
    for block in sorted(document["blocks"], key=lambda item: int(item["order"])):
        text = str(block.get("text") or "").strip()
        block_id = str(block["block_id"])
        if not text or block_id not in known_segments:
            continue
        kind = str(block.get("kind") or "paragraph")
        line = f"# {text}" if kind == "title" else (
            f"## {text}" if kind in {"heading", "appendix"} else text
        )
        rendered.append((line, block_id, str(block.get("parent_id") or block_id)))
    if not rendered:
        raise ValueError("document has no indexable source blocks")

    note_text = "\n\n".join(item[0] for item in rendered) + "\n"
    normalized = markdown_to_index_text(note_text)
    candidates = []
    for line, block_id, section in rendered:
        anchor = markdown_to_index_text(line).strip()
        if not anchor or normalized.count(anchor) != 1:
            continue
        candidates.append({
            "anchor": anchor,
            "prefix": "",
            "suffix": "",
            "section": section,
            "source_segment_ids": [block_id],
            "verification_policy": DIRECT_LOCATOR_POLICY,
        })
    target = job_dir / DOCUMENT_INDEX_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(note_text, encoding="utf-8")
    provenance = persist_document_note_provenance(
        job_dir,
        note_type="original",
        note_artifact=DOCUMENT_INDEX_PATH,
        candidates=candidates,
    )
    return {
        "path": DOCUMENT_INDEX_PATH,
        "blocks": len(rendered),
        "provenance": provenance,
    }


def load_document_source_manifest(job_dir: Path) -> dict[str, Any] | None:
    """只接受当前 job 的 Document manifest，防止证据串 job 或串 pipeline。"""
    path = job_dir / SOURCE_MANIFEST_PATH
    if not path.is_file():
        return None
    manifest = validate_source_manifest(json.loads(path.read_text(encoding="utf-8")))
    if manifest["job_id"] != job_dir.name or manifest["pipeline"] != "document":
        raise ValueError("source manifest belongs to another job or pipeline")
    return manifest


def extract_attestable_document_markers(
    marked_text: str,
    source_manifest: Mapping[str, Any],
    *,
    ai,
    force_semantic: bool = False,
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    """无独立 producer session 时只发布 exact quote；有 session 时分流语义候选。"""
    invocation_id = producer_invocation_id(ai)
    if invocation_id is None:
        cleaned, exact = extract_exact_quote_markers(
            marked_text, source_manifest, error_prefix="note",
        )
        return cleaned, exact, []
    return extract_attestable_markers(
        marked_text,
        source_manifest,
        error_prefix="note",
        producer_component=ai.step_name,
        producer_invocation_id=invocation_id,
        force_semantic=force_semantic,
    )


def persist_document_note_provenance(
    job_dir: Path,
    *,
    note_type: str,
    note_artifact: str,
    candidates: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """绑定最终字节、Document source manifest 与稳定锚点，歧义时发布空映射。"""
    target = job_dir / "output" / "provenance" / f"{note_type}.json"
    source_manifest = load_document_source_manifest(job_dir)
    if source_manifest is None:
        target.unlink(missing_ok=True)
        return {"status": "missing_source_manifest", "segments": 0}
    note_bytes = (job_dir / note_artifact).read_bytes()
    normalized_body = markdown_to_index_text(note_bytes.decode("utf-8"))
    mappings = [dict(item) for item in candidates]
    if any(normalized_body.count(item["anchor"]) != 1 for item in mappings):
        mappings = []
    manifest = build_provenance_manifest(
        job_id=job_dir.name,
        note_type=note_type,
        note_artifact=note_artifact,
        note_bytes=note_bytes,
        normalized_body=normalized_body,
        source_manifest_path=SOURCE_MANIFEST_PATH,
        source_manifest=source_manifest,
        segments=mappings,
    )
    write_provenance_manifest(
        target,
        manifest,
        trusted_root=job_dir,
        source_manifest=source_manifest,
        note_bytes=note_bytes,
        normalized_body=normalized_body,
    )
    return {
        "status": "written" if mappings else "written_empty",
        "segments": len(mappings),
    }
