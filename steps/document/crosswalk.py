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


def _visual_key(item: Mapping[str, Any]) -> str:
    return _normalized(item.get("label") or item.get("caption"))


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
        pdf_visuals = [item for item in pdf_document[field] if _visual_key(item)]
        for visual in updated[field]:
            key = _visual_key(visual)
            label_matches = [item for item in pdf_visuals if _visual_key(item) == key]
            if len(label_matches) == 1:
                candidate = label_matches[0]
                confidence, status = 1.0, "matched"
            elif len(label_matches) > 1:
                candidate = None
                confidence, status = 0.0, "ambiguous"
            else:
                caption_match, confidence, status = _match(
                    visual.get("caption"),
                    [{**item, "text": item.get("caption")} for item in pdf_document[field]],
                )
                candidate = caption_match
            if status == "matched":
                visual_matched += 1
            elif status == "ambiguous":
                visual_ambiguous += 1
            locator = visual.get("source_locator")
            if isinstance(locator, dict):
                locator["crosswalk"] = {
                    "status": status,
                    "confidence": confidence,
                    "method": "visual_label_or_caption_unique",
                }
                pdf_locator = candidate.get("source_locator", {}).get("pdf") if candidate else None
                if status == "matched" and isinstance(pdf_locator, Mapping):
                    locator["pdf"] = deepcopy(dict(pdf_locator))
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
