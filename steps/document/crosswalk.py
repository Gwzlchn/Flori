"""在独立 HTML/PDF 来源间建立唯一高置信 block 与 visual 对齐。"""

from __future__ import annotations

import re
import unicodedata
from copy import deepcopy
from typing import Any, Mapping

from shared.document_contract import validate_document, validate_quality
from steps.document.adapters.scholarly_pdf import parse_pdf_document


_SPACE = re.compile(r"\s+")


def _normalized(value: object) -> str:
    return _SPACE.sub(" ", unicodedata.normalize("NFKC", str(value or ""))).strip().casefold()


def _match(
    text: object,
    candidates: list[Mapping[str, Any]],
) -> tuple[Mapping[str, Any] | None, float, str]:
    needle = _normalized(text)
    if len(needle) < 8:
        return None, 0.0, "unmatched"
    exact = [item for item in candidates if _normalized(item.get("text")) == needle]
    if len(exact) == 1:
        return exact[0], 1.0, "matched"
    if len(exact) > 1:
        return None, 0.0, "ambiguous"
    contained = [
        item for item in candidates
        if len(_normalized(item.get("text"))) >= 8
        and (
            needle in _normalized(item.get("text"))
            or _normalized(item.get("text")) in needle
        )
    ]
    if len(contained) == 1:
        left = len(needle)
        right = len(_normalized(contained[0].get("text")))
        confidence = min(left, right) / max(left, right)
        if confidence >= 0.85:
            return contained[0], round(confidence, 6), "matched"
    return None, 0.0, "ambiguous" if contained else "unmatched"


def _merge_pdf_locator(
    target: dict[str, Any],
    candidate: Mapping[str, Any] | None,
    confidence: float,
    status: str,
) -> bool:
    locator = target.get("locator")
    if not isinstance(locator, dict):
        return False
    locator["crosswalk"] = {
        "status": status,
        "confidence": confidence,
        "method": "normalized_text_unique",
    }
    candidate_locator = candidate.get("locator") if isinstance(candidate, Mapping) else None
    pdf = candidate_locator.get("pdf") if isinstance(candidate_locator, Mapping) else None
    if status != "matched" or not isinstance(pdf, Mapping):
        return False
    locator["pdf"] = deepcopy(dict(pdf))
    return True


def _explicit_visual_label(item: Mapping[str, Any]) -> str:
    label = _normalized(item.get("label"))
    caption = _normalized(item.get("caption"))
    return label if label and label in caption else ""


def _visual_match(
    visual: Mapping[str, Any],
    html_visuals: list[Mapping[str, Any]],
    pdf_visuals: list[Mapping[str, Any]],
    claimed: set[int],
) -> tuple[Mapping[str, Any] | None, float, str, str]:
    """按 caption 优先且全局唯一匹配 visual;同一 PDF visual 只能消费一次."""
    caption = _normalized(visual.get("caption"))
    if len(caption) >= 8:
        exact = [
            (index, item) for index, item in enumerate(pdf_visuals)
            if _normalized(item.get("caption")) == caption
        ]
        if len(exact) == 1:
            index, candidate = exact[0]
            if index not in claimed:
                return candidate, 1.0, "matched", "visual_caption_exact_unique"
            return None, 0.0, "ambiguous", "visual_caption_exact_unique"
        if len(exact) > 1:
            return None, 0.0, "ambiguous", "visual_caption_exact_unique"

    label = _explicit_visual_label(visual)
    if label:
        html_label_count = sum(_explicit_visual_label(item) == label for item in html_visuals)
        pdf_label_matches = [
            (index, item) for index, item in enumerate(pdf_visuals)
            if _explicit_visual_label(item) == label
        ]
        if html_label_count == 1 and len(pdf_label_matches) == 1:
            index, candidate = pdf_label_matches[0]
            if index not in claimed:
                return candidate, 1.0, "matched", "visual_label_exact_unique"
            return None, 0.0, "ambiguous", "visual_label_exact_unique"
        if pdf_label_matches:
            return None, 0.0, "ambiguous", "visual_label_exact_unique"

    augmented = [
        {**item, "text": item.get("caption"), "_crosswalk_index": index}
        for index, item in enumerate(pdf_visuals)
    ]
    candidate, confidence, status = _match(visual.get("caption"), augmented)
    if status != "matched" or candidate is None:
        return None, confidence, status, "visual_caption_contained_unique"
    index = int(candidate["_crosswalk_index"])
    if index in claimed:
        return None, 0.0, "ambiguous", "visual_caption_contained_unique"
    return pdf_visuals[index], confidence, status, "visual_caption_contained_unique"


def attach_pdf_crosswalk(
    job_dir,
    document: Mapping[str, Any],
    quality: Mapping[str, Any],
    job: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """并行解析 PDF；只有唯一文本匹配才发布 page/bbox，否则显式降级。"""
    updated = deepcopy(dict(document))
    report = deepcopy(dict(quality))
    pdf_job = {
        key: value for key, value in job.items() if key != "source_fingerprint"
    }
    pdf_job["source_profile"] = "digital_pdf"
    pdf_document, pdf_quality = parse_pdf_document(job_dir, pdf_job)
    pdf_source = next(
        item for item in pdf_document["sources"] if item["source_id"] == "pdf"
    )
    updated["sources"] = [
        *[item for item in updated["sources"] if item["source_id"] != "pdf"],
        deepcopy(pdf_source),
    ]
    updated["capabilities"] = sorted(set(updated["capabilities"]) | {
        "pdf", "text_layer", "page_bbox",
    })

    pdf_blocks = [
        item for item in pdf_document["blocks"]
        if isinstance(item.get("locator", {}).get("pdf"), Mapping)
    ]
    matched = ambiguous = visual_matched = visual_ambiguous = 0
    for block in updated["blocks"]:
        candidate, confidence, status = _match(block.get("text"), pdf_blocks)
        if _merge_pdf_locator(block, candidate, confidence, status):
            matched += 1
        elif status == "ambiguous":
            ambiguous += 1

    for field in ("figures", "tables"):
        html_visuals = list(updated[field])
        pdf_visuals = list(pdf_document[field])
        claimed: set[int] = set()
        for visual in updated[field]:
            candidate, confidence, status, method = _visual_match(
                visual, html_visuals, pdf_visuals, claimed,
            )
            if status == "matched":
                visual_matched += 1
                claimed.add(next(
                    index for index, item in enumerate(pdf_visuals) if item is candidate
                ))
            elif status == "ambiguous":
                visual_ambiguous += 1
            locator = visual.get("source_locator")
            if isinstance(locator, dict):
                locator["crosswalk"] = {
                    "status": status,
                    "confidence": confidence,
                    "method": method,
                }
                pdf_locator = candidate.get("source_locator", {}).get("pdf") if candidate else None
                if status == "matched" and isinstance(pdf_locator, Mapping):
                    locator["pdf"] = deepcopy(dict(pdf_locator))
                    if field == "figures" and not visual.get("media"):
                        visual["media"] = deepcopy(candidate.get("media") or [])
                        referenced = {
                            str(media.get("asset_id"))
                            for media in visual["media"] if media.get("asset_id")
                        }
                        existing_assets = {
                            str(asset.get("asset_id")) for asset in updated.get("assets", [])
                        }
                        updated.setdefault("assets", []).extend(
                            deepcopy(asset) for asset in pdf_document.get("assets", [])
                            if str(asset.get("asset_id")) in referenced
                            and str(asset.get("asset_id")) not in existing_assets
                        )
                    block = next(
                        (item for item in updated["blocks"] if item["block_id"] == visual["block_id"]),
                        None,
                    )
                    if isinstance(block, dict):
                        block["locator"]["pdf"] = deepcopy(dict(pdf_locator))

    metrics = report.setdefault("metrics", {})
    metrics.update({
        "pdf_crosswalk_blocks": matched,
        "pdf_crosswalk_ambiguous": ambiguous,
        "pdf_crosswalk_visuals": visual_matched,
        "pdf_crosswalk_visual_ambiguous": visual_ambiguous,
        "pdf_source_quality": pdf_quality["status"],
    })
    metrics.update({
        f"pdf_{key}": value
        for key, value in pdf_quality.get("metrics", {}).items()
        if str(key).startswith("layout_detector_")
    })
    if pdf_quality["status"] == "rejected":
        report.setdefault("reasons", []).append("pdf_crosswalk_source_rejected")
    elif ambiguous or visual_ambiguous:
        report.setdefault("reasons", []).append("pdf_crosswalk_partial")
    elif matched == 0:
        report.setdefault("reasons", []).append("pdf_crosswalk_unmatched")
    report["reasons"] = list(dict.fromkeys(report.get("reasons", [])))
    if report["status"] == "complete" and report["reasons"]:
        report["status"] = "degraded"
    return (
        validate_document(updated, expected_job_id=str(updated["job_id"])),
        validate_quality(report, expected_job_id=str(updated["job_id"])),
    )
