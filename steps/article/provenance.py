"""为 HTML/PDF 文本来源构建可复验的笔记溯源清单。"""

from __future__ import annotations

import hashlib
import html
import json
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from shared.note_text import markdown_to_index_text
from shared.provenance import (
    bounded_support_text,
    build_provenance_manifest,
    build_source_manifest,
    extract_attestable_markers,
    extract_exact_quote_markers,
    make_segment_id,
    validate_source_manifest,
    write_provenance_manifest,
    write_source_manifest,
)
from steps.utils.provenance_attestation import producer_invocation_id


SOURCE_MANIFEST_PATH = "intermediate/source_segments.json"
PDF_SUPPORT_PATH = "intermediate/pdf_page_support.json"
_HTML_TEXT_RE = re.compile(r">([^<>]+)<", re.S)
_MAX_HTML_SEGMENTS = 64
_MAX_REFERENCE_CHARS = 320


def build_html_source_manifest(
    job_dir: Path,
    *,
    pipeline: str,
    revision: str | None = None,
) -> dict[str, Any] | None:
    """从原始 HTML 的唯一文本节点生成 locator;不从派生 Markdown 猜坐标。"""
    source_path = job_dir / "input" / "source.html"
    if not source_path.is_file():
        return None
    source_bytes = source_path.read_bytes()
    try:
        source = source_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return None
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()

    without_noise = re.sub(
        r"<(?:script|style)\b[^>]*>.*?</(?:script|style)>",
        "",
        source,
        flags=re.I | re.S,
    )
    preferred = re.findall(
        r"<(?:article|main)\b[^>]*>(.*?)</(?:article|main)>",
        without_noise,
        flags=re.I | re.S,
    )
    regions = preferred or [without_noise]
    segments: list[dict[str, Any]] = []
    seen_exact: set[str] = set()
    for region in regions:
        for match in _HTML_TEXT_RE.finditer(region):
            raw = match.group(1)
            candidate = raw.strip()
            exact = candidate if len(candidate) <= 512 else candidate[:512]
            if len(exact) < 16 or exact in seen_exact or source.count(exact) != 1:
                continue
            start = source.index(exact)
            end = start + len(exact)
            locator = {
                "kind": "text",
                "exact": exact,
                "prefix": source[max(0, start - 32):start],
                "suffix": source[end:end + 32],
                "dom_path": None,
            }
            segment_id = make_segment_id(
                "html", start=start, end=end, section="html", locator=locator,
            )
            segments.append({
                "segment_id": segment_id,
                "source_id": "html",
                "start": start,
                "end": end,
                "section": "html",
                "locator": locator,
                "support_text": bounded_support_text(html.unescape(exact)),
                "support_artifact": {
                    "kind": "html",
                    "path": "input/source.html",
                    "sha256": source_sha256,
                    "selector": {"start": start, "end": end},
                },
            })
            seen_exact.add(exact)
            if len(segments) >= _MAX_HTML_SEGMENTS:
                break
        if len(segments) >= _MAX_HTML_SEGMENTS:
            break
    if not segments:
        return None
    return build_source_manifest(
        job_id=job_dir.name,
        pipeline=pipeline,
        source_artifacts=[{
            "source_id": "html",
            "path": "input/source.html",
            "sha256": source_sha256,
            "revision": revision,
            "media_duration_ms": None,
            "page_count": None,
        }],
        segments=segments,
    )


def build_pdf_source_manifest(
    job_dir: Path,
    *,
    pipeline: str,
    page_count: int,
    page_support_texts: Sequence[str | None],
) -> dict[str, Any] | None:
    """用实测页数和同序提取文本生成逐页 locator;不截断超限原文。"""
    source_path = job_dir / "input" / "source.pdf"
    if not source_path.is_file() or type(page_count) is not int or page_count <= 0:
        return None
    if (
        isinstance(page_support_texts, (str, bytes))
        or not isinstance(page_support_texts, Sequence)
        or len(page_support_texts) != page_count
    ):
        raise ValueError("PDF page support must align with measured page count")
    support_path = job_dir / PDF_SUPPORT_PATH
    support_sha256 = _sha256_file(support_path) if support_path.is_file() else None
    segments: list[dict[str, Any]] = []
    for page in range(1, page_count + 1):
        locator = {"kind": "pdf", "page": page, "bbox": None}
        segment_id = make_segment_id(
            "pdf", start=None, end=None, section=f"page-{page}", locator=locator,
        )
        support_text = bounded_support_text(page_support_texts[page - 1])
        segments.append({
            "segment_id": segment_id,
            "source_id": "pdf",
            "start": None,
            "end": None,
            "section": f"page-{page}",
            "locator": locator,
            "support_text": support_text if support_sha256 is not None else None,
            "support_artifact": ({
                "kind": "pdf_pages",
                "path": PDF_SUPPORT_PATH,
                "sha256": support_sha256,
                "selector": {"page": page},
            } if support_text is not None and support_sha256 is not None else None),
        })
    return build_source_manifest(
        job_id=job_dir.name,
        pipeline=pipeline,
        source_artifacts=[{
            "source_id": "pdf",
            "path": "input/source.pdf",
            "sha256": _sha256_file(source_path),
            "revision": None,
            "media_duration_ms": None,
            "page_count": page_count,
        }],
        segments=segments,
    )


def publish_source_manifest(
    job_dir: Path,
    manifest: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """原子发布清单;当前来源不可定位时移除旧清单以保持 fail-closed。"""
    target = job_dir / SOURCE_MANIFEST_PATH
    if manifest is None:
        target.unlink(missing_ok=True)
        return None
    write_source_manifest(target, manifest, trusted_root=job_dir)
    return dict(manifest)


def load_source_manifest(job_dir: Path, *, pipeline: str) -> dict[str, Any] | None:
    path = job_dir / SOURCE_MANIFEST_PATH
    if not path.is_file():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    manifest = validate_source_manifest(value)
    if manifest["job_id"] != job_dir.name or manifest["pipeline"] != pipeline:
        raise ValueError("source manifest belongs to another job or pipeline")
    return manifest


def source_reference_block(source_manifest: Mapping[str, Any] | None) -> str:
    """注入可验证 marker;模型只能复制已有 ID,marker 不属于最终笔记。"""
    if source_manifest is None:
        return ""
    lines = [
        "\n--- 可引用来源坐标 ---\n",
        "引用事实时在相关句末原样保留对应 [[source:ID]]。只能使用下列 ID,"
        "不得改写、编造或重复;这些内部标记会在笔记落盘前移除。\n",
    ]
    for segment in source_manifest["segments"][:_MAX_HTML_SEGMENTS]:
        locator = segment["locator"]
        if locator["kind"] == "text":
            excerpt = re.sub(r"\s+", " ", locator["exact"]).strip()
            excerpt = excerpt[:_MAX_REFERENCE_CHARS]
        elif locator["kind"] == "pdf":
            support = segment.get("support_text")
            if type(support) is not str or not support.strip():
                continue
            excerpt = re.sub(r"\s+", " ", support).strip()
            excerpt = excerpt[:_MAX_REFERENCE_CHARS]
        else:
            continue
        excerpt = excerpt.replace("[[source:", "[source:")
        lines.append(f"[[source:{_source_token(segment['segment_id'])}]] {excerpt}\n")
    return "".join(lines) if len(lines) > 2 else ""


def translation_reference_block(
    source_manifest: Mapping[str, Any] | None,
    *,
    source_text: str | None = None,
    page_range: tuple[int, int] | None = None,
) -> str:
    """给译文 producer 只注入当前 chunk/page 的 canonical marker。"""
    if source_manifest is None:
        return ""
    lines = [
        "\n--- 译文证据坐标 ---\n",
        "每个被翻译的来源段,在对应译文句末原样保留一个 [[source:ID]]。"
        "不得把 marker 单独成行,不得重复或编造;marker 落盘前会移除。\n",
    ]
    normalized_source = re.sub(r"\s+", " ", source_text or "").strip()
    added = 0
    for segment in source_manifest["segments"]:
        support = segment.get("support_text")
        if type(support) is not str or not support.strip():
            continue
        locator = segment["locator"]
        if page_range is not None:
            if locator["kind"] != "pdf" or not page_range[0] <= locator["page"] <= page_range[1]:
                continue
        elif source_text is not None:
            normalized_support = re.sub(r"\s+", " ", support).strip()
            if normalized_support not in normalized_source:
                continue
        excerpt = re.sub(r"\s+", " ", support).strip()[:_MAX_REFERENCE_CHARS]
        excerpt = excerpt.replace("[[source:", "[source:")
        lines.append(f"[[source:{_source_token(segment['segment_id'])}]] {excerpt}\n")
        added += 1
        if added >= _MAX_HTML_SEGMENTS:
            break
    return "".join(lines) if len(lines) > 2 else ""


def extract_note_markers(
    marked_text: str,
    source_manifest: Mapping[str, Any],
) -> tuple[str, list[dict[str, Any]]]:
    """校验并移除内部 marker;只发布来源 support_text 的逐字 claim。"""
    return extract_exact_quote_markers(
        marked_text, source_manifest, error_prefix="note",
    )


def extract_attestable_note_markers(
    marked_text: str,
    source_manifest: Mapping[str, Any],
    *,
    ai,
    force_semantic: bool = False,
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    """按真实 producer session 分流 exact 与下游 concepts 待证明候选。"""
    invocation_id = producer_invocation_id(ai)
    if invocation_id is None:
        cleaned, exact = extract_note_markers(marked_text, source_manifest)
        return cleaned, exact, []
    return extract_attestable_markers(
        marked_text,
        source_manifest,
        error_prefix="note",
        producer_component=ai.step_name,
        producer_invocation_id=invocation_id,
        force_semantic=force_semantic,
    )


def direct_text_provenance_candidates(
    source_manifest: Mapping[str, Any],
    note_text: str,
    *,
    section: str,
) -> list[dict[str, Any]]:
    """只发布在派生文本中仍唯一存在的原文节点,不做模糊或语义猜测。"""
    normalized_body = markdown_to_index_text(note_text)
    candidates: list[dict[str, Any]] = []
    for segment in source_manifest["segments"]:
        locator = segment["locator"]
        if locator["kind"] != "text":
            continue
        anchor = markdown_to_index_text(html.unescape(locator["exact"])).strip()
        if not anchor or normalized_body.count(anchor) != 1:
            continue
        candidates.append({
            "anchor": anchor,
            "prefix": "",
            "suffix": "",
            "section": section,
            "source_segment_ids": [segment["segment_id"]],
        })
    return candidates


def persist_note_provenance(
    job_dir: Path,
    *,
    pipeline: str,
    note_type: str,
    note_artifact: str,
    candidates: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """绑定最终字节与来源清单;锚点歧义时发布空映射或移除旧证据。"""
    target = job_dir / "output" / "provenance" / f"{note_type}.json"
    source_manifest = load_source_manifest(job_dir, pipeline=pipeline)
    if source_manifest is None:
        target.unlink(missing_ok=True)
        return {"status": "legacy_no_source_manifest", "segments": 0}
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


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_token(segment_id: str) -> str:
    return segment_id.removeprefix("seg_")
