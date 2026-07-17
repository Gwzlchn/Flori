"""把 PDF Figure/Table locator 确定性渲染为可展示的区域制品。"""

from __future__ import annotations

import os
import subprocess
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

from shared.document_contract import validate_document, validate_quality


PDF_RENDER_DPI = 144


def _safe_id(value: object) -> str:
    text = str(value or "")
    if not text or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-" for char in text):
        raise ValueError("visual id is invalid")
    return text


def _pdf_region(locator: object) -> tuple[int, list[float]] | None:
    if not isinstance(locator, Mapping) or not isinstance(locator.get("pdf"), Mapping):
        return None
    pdf = locator["pdf"]
    page = pdf.get("page")
    bboxes = pdf.get("bboxes")
    if type(page) is not int or not isinstance(bboxes, list) or not bboxes:
        return None
    bbox = bboxes[0]
    if not isinstance(bbox, list) or len(bbox) != 4:
        return None
    values = [float(value) for value in bbox]
    if values[2] <= values[0] or values[3] <= values[1]:
        return None
    return page, values


def _render_region(
    source: Path,
    destination: Path,
    *,
    page: int,
    bbox: list[float],
) -> None:
    scale = PDF_RENDER_DPI / 72
    x0, y0, x1, y1 = bbox
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.stem}.tmp")
    command = [
        "pdftocairo", "-png", "-singlefile", "-r", str(PDF_RENDER_DPI),
        "-f", str(page), "-l", str(page),
        "-x", str(max(0, round(x0 * scale))),
        "-y", str(max(0, round(y0 * scale))),
        "-W", str(max(1, round((x1 - x0) * scale))),
        "-H", str(max(1, round((y1 - y0) * scale))),
        str(source), str(temporary),
    ]
    try:
        subprocess.run(
            command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            timeout=120,
        )
        produced = temporary.with_suffix(".png")
        if not produced.is_file() or produced.stat().st_size <= 0:
            raise ValueError("PDF visual renderer produced no image")
        os.replace(produced, destination)
    finally:
        temporary.with_suffix(".png").unlink(missing_ok=True)


def materialize_pdf_visuals(
    job_dir: Path,
    document: Mapping[str, Any],
    quality: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """渲染所有可定位 PDF 图表；单个失败显式降级但不吞掉 registry 项。"""
    updated = deepcopy(dict(document))
    report = deepcopy(dict(quality))
    source = job_dir / "input" / "source.pdf"
    if not source.is_file():
        return validate_document(updated), validate_quality(report)

    failures: list[str] = []
    rendered = 0
    for figure in updated.get("figures", []):
        attempted = False
        for media in figure.get("media", []):
            if media.get("artifact"):
                continue
            region = _pdf_region(media.get("source_locator") or figure.get("source_locator"))
            if region is None:
                continue
            attempted = True
            media_id = _safe_id(media["media_id"])
            rel = f"assets/document/{_safe_id(figure['figure_id'])}-{media_id}.png"
            try:
                _render_region(source, job_dir / rel, page=region[0], bbox=region[1])
            except (OSError, subprocess.SubprocessError, ValueError):
                failures.append(f"figure_render_failed:{figure['figure_id']}:{media_id}")
                continue
            media["artifact"] = rel
            rendered += 1
        if attempted and any(not item.get("artifact") for item in figure.get("media", [])):
            figure["extraction"]["status"] = "degraded"
            figure["extraction"].setdefault("reasons", []).append("pdf_visual_render_incomplete")

    for table in updated.get("tables", []):
        representations = table.setdefault("representations", [])
        crop = next((item for item in representations if item.get("kind") == "source_crop"), None)
        if crop is None:
            region = _pdf_region(table.get("source_locator"))
            if region is None:
                continue
            crop = {
                "kind": "source_crop", "artifact": None,
                "source_locator": table.get("source_locator"),
            }
            representations.append(crop)
        if crop.get("artifact"):
            continue
        region = _pdf_region(crop.get("source_locator") or table.get("source_locator"))
        if region is None:
            continue
        rel = f"assets/document/{_safe_id(table['table_id'])}.png"
        try:
            _render_region(source, job_dir / rel, page=region[0], bbox=region[1])
        except (OSError, subprocess.SubprocessError, ValueError):
            failures.append(f"table_render_failed:{table['table_id']}")
            table["extraction"]["status"] = "degraded"
            table["extraction"].setdefault("reasons", []).append("pdf_table_crop_render_failed")
            continue
        crop["artifact"] = rel
        rendered += 1

    if failures:
        reasons = report.setdefault("reasons", [])
        if "pdf_visual_render_incomplete" not in reasons:
            reasons.append("pdf_visual_render_incomplete")
        if report.get("status") == "complete":
            report["status"] = "degraded"
    report.setdefault("metrics", {})["visual_assets_rendered"] = rendered
    report["metrics"]["visual_asset_failures"] = len(failures)
    return (
        validate_document(updated, expected_job_id=str(updated["job_id"])),
        validate_quality(report, expected_job_id=str(updated["job_id"])),
    )
