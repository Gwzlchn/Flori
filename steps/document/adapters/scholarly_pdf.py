"""把数字或扫描 PDF 解析为带页码和 bbox 的统一 Document Model。"""

from __future__ import annotations

import re
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ._common import (
    base_document,
    make_id,
    pdf_locator,
    quality_report,
    source_context,
)


_FIGURE_CAPTION = re.compile(r"^(?:Figure|Fig\.?|图)\s*([A-Za-z]?\d+(?:[.:-]\d+)*)\b", re.I)
_TABLE_CAPTION = re.compile(r"^(?:Table|表)\s*([A-Za-z]?\d+(?:[.:-]\d+)*)\b", re.I)
_URL = re.compile(r"https?://[^\s<>()\[\]{}]+", re.I)
_DOI = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+\b", re.I)
_SCAN_CHARS_PER_PAGE = 40
_OCR_RELIABLE_THRESHOLD = 0.8


@dataclass(frozen=True)
class LayoutItem:
    text: str
    bbox: list[float]
    confidence: float | None = None


@dataclass
class PageLayout:
    number: int
    width: float
    height: float
    text_items: list[LayoutItem] = field(default_factory=list)
    image_bboxes: list[list[float]] = field(default_factory=list)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _float(value: str | None, default: float = 0.0) -> float:
    try:
        return round(float(value or default), 3)
    except ValueError:
        return default


class ScholarlyPdfAdapter:
    def __init__(self, job_dir: Path, job: dict[str, Any]) -> None:
        self.job_dir = job_dir
        self.job = job
        self.job_id, self.document_kind, self.path, self.fingerprint = source_context(
            job_dir, job, relative_path="input/source.pdf",
        )
        self.source_bytes = self.path.read_bytes()
        self.reasons: list[str] = []
        self.blocks: list[dict[str, Any]] = []
        self.figures: list[dict[str, Any]] = []
        self.tables: list[dict[str, Any]] = []
        self.assets: list[dict[str, Any]] = []
        self.references: list[dict[str, Any]] = []
        self._order = 0

    def parse(self) -> tuple[dict[str, Any], dict[str, Any]]:
        info = self._pdf_info()
        pages, layout_method = self._layout()
        page_count = int(info.get("Pages") or len(pages) or 0)
        text_chars = sum(len(item.text) for page in pages for item in page.text_items)
        forced_profile = self.job.get("source_profile")
        scanned = forced_profile == "scanned_pdf" or (
            page_count > 0 and text_chars / page_count < _SCAN_CHARS_PER_PAGE
        )
        profile = "scanned_pdf" if scanned else "digital_pdf"
        capabilities = ["pdf", "ocr", "page_bbox"] if scanned else [
            "pdf", "text_layer", "page_bbox",
        ]
        if scanned:
            self.reasons.append("scanned_pdf_source")
            if (
                text_chars == 0
                or page_count <= 0
                or (
                    forced_profile != "scanned_pdf"
                    and text_chars / max(page_count, 1) < _SCAN_CHARS_PER_PAGE
                )
            ):
                ocr_pages = self._ocr_layout()
                ocr_chars = sum(
                    len(item.text) for page in ocr_pages for item in page.text_items
                )
                if ocr_chars:
                    pages = ocr_pages
                    text_chars = ocr_chars
                    layout_method = "rapidocr_page_bbox"
                    self.reasons.append("scanned_pdf_ocr_applied")
                else:
                    self.reasons.append("scanned_pdf_ocr_failed")
        self._build_content(pages, allow_visuals=True)
        ocr_confidences = [
            item.confidence for page in pages for item in page.text_items
            if item.confidence is not None
        ]
        if ocr_confidences and min(ocr_confidences) < _OCR_RELIABLE_THRESHOLD:
            self.reasons.append("scanned_pdf_ocr_low_confidence")

        title = (info.get("Title") or "").strip()
        if not title and pages:
            title = next((item.text for item in pages[0].text_items if item.text), "")
            if title:
                self.reasons.append("pdf_title_inferred")
        authors = [
            {"name": name.strip(), "affiliations": [], "emails": [], "notes": []}
            for name in re.split(r"\s*(?:,|;|\band\b)\s*", info.get("Author", ""))
            if name.strip()
        ]
        rejected = scanned and not self.blocks
        if not pages or page_count <= 0:
            rejected = True
            self.reasons.append("pdf_layout_unavailable")
        if not title:
            self.reasons.append("pdf_title_missing")
        if self.path.read_bytes() != self.source_bytes:
            raise ValueError("PDF source changed while parsing")

        document = base_document(
            job_id=self.job_id,
            document_kind=self.document_kind,
            source_profile=profile,
            capabilities=capabilities,
            relative_path="input/source.pdf",
            source_path=self.path,
            source_fingerprint=self.fingerprint,
            source_url=self.job.get("url"),
            metadata={
                "title": title,
                "original_title": title,
                "authors": authors,
                "institutions": [],
                "abstract": "",
                "keywords": [],
                "publisher": "",
                "venue": "",
                "license": "",
                "identifiers": {},
                "pages": page_count,
                "pdf_info": info,
            },
            blocks=self.blocks,
            assets=self.assets,
            references=self.references,
            figures=self.figures,
            tables=self.tables,
        )
        report = quality_report(
            self.job_id,
            reasons=self.reasons,
            rejected=rejected,
            metrics={
                "page_count": page_count,
                "layout_page_count": len(pages),
                "layout_method": layout_method,
                "text_chars": text_chars,
                "chars_per_page": round(text_chars / max(page_count, 1), 2),
                "block_count": len(self.blocks),
                "figure_count": len(self.figures),
                "figure_panel_count": sum(len(item["panels"]) for item in self.figures),
                "table_count": len(self.tables),
                "table_cell_count": sum(len(item["cells"]) for item in self.tables),
                "reference_count": len(self.references),
                "scan_detected": scanned,
                "ocr_confidence_min": min(ocr_confidences, default=None),
                "ocr_exact_evidence_threshold": _OCR_RELIABLE_THRESHOLD,
            },
        )
        return document, report

    def _run(
        self,
        command: list[str],
        timeout: int = 120,
        *,
        cwd: str | None = None,
    ) -> str:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            cwd=cwd,
        )
        if result.returncode != 0:
            raise ValueError(f"PDF tool failed: {command[0]}")
        return result.stdout

    def _pdf_info(self) -> dict[str, str]:
        output = self._run(["pdfinfo", str(self.path)], timeout=60)
        info: dict[str, str] = {}
        for line in output.splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            info[key.strip()] = value.strip()
        return info

    def _layout(self) -> tuple[list[PageLayout], str]:
        try:
            with tempfile.TemporaryDirectory(prefix="flori-pdf-layout-") as temp_dir:
                xml = self._run([
                    "pdftohtml", "-xml", "-stdout", "-hidden", "-zoom", "1",
                    str(self.path),
                ], cwd=temp_dir)
            pages = self._parse_pdftohtml(xml)
            if pages:
                return pages, "pdftohtml_xml"
        except (ValueError, ET.ParseError, subprocess.TimeoutExpired):
            self.reasons.append("pdf_layout_primary_failed")
        try:
            xml = self._run(["pdftotext", "-bbox-layout", str(self.path), "-"])
            pages = self._parse_pdftotext(xml)
            if pages:
                self.reasons.append("pdf_layout_fallback")
                return pages, "pdftotext_bbox"
        except (ValueError, ET.ParseError, subprocess.TimeoutExpired):
            self.reasons.append("pdf_layout_fallback_failed")
        return [], "unavailable"

    def _ocr_layout(self) -> list[PageLayout]:
        """逐页渲染扫描 PDF 并把 OCR 多边形换算回 PDF page bbox。"""
        try:
            import fitz
            from steps.utils.ocr import create_ocr_engine

            engine = create_ocr_engine()
            pages: list[PageLayout] = []
            with fitz.open(self.path) as document, tempfile.TemporaryDirectory(
                prefix="flori-pdf-ocr-",
            ) as temporary:
                for index, source_page in enumerate(document):
                    pixmap = source_page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
                    image_path = Path(temporary) / f"page-{index + 1}.png"
                    pixmap.save(str(image_path))
                    result, _ = engine(str(image_path))
                    page = PageLayout(
                        number=index + 1,
                        width=round(float(source_page.rect.width), 3),
                        height=round(float(source_page.rect.height), 3),
                    )
                    for raw in result or []:
                        if not isinstance(raw, (list, tuple)) or len(raw) < 3:
                            continue
                        polygon, text, confidence = raw[0], str(raw[1]).strip(), raw[2]
                        if not text or not isinstance(polygon, (list, tuple)):
                            continue
                        points = [
                            point for point in polygon
                            if isinstance(point, (list, tuple)) and len(point) >= 2
                        ]
                        if not points:
                            continue
                        try:
                            score = round(float(confidence), 4)
                            xs = [float(point[0]) for point in points]
                            ys = [float(point[1]) for point in points]
                        except (TypeError, ValueError):
                            continue
                        bbox = [
                            round(min(xs) * page.width / pixmap.width, 3),
                            round(min(ys) * page.height / pixmap.height, 3),
                            round(max(xs) * page.width / pixmap.width, 3),
                            round(max(ys) * page.height / pixmap.height, 3),
                        ]
                        page.text_items.append(LayoutItem(text, bbox, score))
                    pages.append(page)
            return pages
        except Exception as exc:
            self.reasons.append(f"scanned_pdf_ocr_error:{type(exc).__name__}")
            return []

    @staticmethod
    def _parse_pdftohtml(xml_text: str) -> list[PageLayout]:
        root = ET.fromstring(xml_text)
        pages: list[PageLayout] = []
        for page_node in (node for node in root.iter() if _local_name(node.tag) == "page"):
            page = PageLayout(
                number=int(page_node.attrib.get("number") or len(pages) + 1),
                width=_float(page_node.attrib.get("width")),
                height=_float(page_node.attrib.get("height")),
            )
            for node in page_node:
                tag = _local_name(node.tag)
                left = _float(node.attrib.get("left"))
                top = _float(node.attrib.get("top"))
                width = _float(node.attrib.get("width"))
                height = _float(node.attrib.get("height"))
                bbox = [left, top, round(left + width, 3), round(top + height, 3)]
                if tag == "text":
                    text = " ".join("".join(node.itertext()).split())
                    if text:
                        page.text_items.append(LayoutItem(text, bbox))
                elif tag == "image" and width > 0 and height > 0:
                    page.image_bboxes.append(bbox)
            pages.append(page)
        return pages

    @staticmethod
    def _parse_pdftotext(xml_text: str) -> list[PageLayout]:
        root = ET.fromstring(xml_text)
        pages: list[PageLayout] = []
        for page_node in (node for node in root.iter() if _local_name(node.tag) == "page"):
            page = PageLayout(
                number=len(pages) + 1,
                width=_float(page_node.attrib.get("width")),
                height=_float(page_node.attrib.get("height")),
            )
            blocks = [node for node in page_node.iter() if _local_name(node.tag) == "block"]
            for block in blocks:
                words = [node for node in block.iter() if _local_name(node.tag) == "word"]
                text = " ".join("".join(word.itertext()).strip() for word in words).strip()
                if not text or not words:
                    continue
                bbox = [
                    min(_float(word.attrib.get("xMin")) for word in words),
                    min(_float(word.attrib.get("yMin")) for word in words),
                    max(_float(word.attrib.get("xMax")) for word in words),
                    max(_float(word.attrib.get("yMax")) for word in words),
                ]
                page.text_items.append(LayoutItem(text, bbox))
            pages.append(page)
        return pages

    def _build_content(self, pages: list[PageLayout], *, allow_visuals: bool) -> None:
        first_text = True
        for page in pages:
            consumed: set[int] = set()
            for item_index, item in enumerate(page.text_items):
                if item_index in consumed:
                    continue
                figure_match = _FIGURE_CAPTION.match(item.text)
                table_match = _TABLE_CAPTION.match(item.text)
                if allow_visuals and figure_match:
                    self._add_pdf_figure(page, item, figure_match.group(1))
                    continue
                if allow_visuals and table_match:
                    consumed.update(
                        self._add_pdf_table(page, item, table_match.group(1), item_index)
                    )
                    continue
                kind = "title" if first_text and page.number == 1 else "paragraph"
                first_text = False
                block_id = self._add_block(
                    kind,
                    item.text,
                    page.number,
                    [item.bbox],
                    **({"ocr_confidence": item.confidence} if item.confidence is not None else {}),
                )
                self._references_from_text(item.text, page.number, item.bbox, block_id, item_index)

    def _add_block(
        self,
        kind: str,
        text: str,
        page: int,
        bboxes: list[list[float]],
        *,
        parent_id: str | None = None,
        **extra: Any,
    ) -> str:
        block_id = make_id("blk", self.fingerprint, kind, page, self._order, text[:80])
        locator = pdf_locator(self.fingerprint, page, bboxes)
        confidence = extra.get("ocr_confidence")
        if type(confidence) in (int, float):
            locator["pdf"]["ocr_confidence"] = confidence
        self.blocks.append({
            "block_id": block_id,
            "parent_id": parent_id,
            "order": self._order,
            "kind": kind,
            "text": text,
            "locator": locator,
            **extra,
        })
        self._order += 1
        return block_id

    def _add_pdf_figure(self, page: PageLayout, caption: LayoutItem, label_id: str) -> None:
        label = f"Figure {label_id}"
        figure_id = make_id("fig", self.fingerprint, page.number, label, caption.bbox)
        candidate_panels = [
            bbox for bbox in page.image_bboxes
            if bbox[3] <= caption.bbox[1] + 3
            and caption.bbox[1] - bbox[1] <= max(page.height * 0.55, 1)
        ]
        if not candidate_panels:
            crop = [0.0, max(0.0, caption.bbox[1] - page.height * 0.4), page.width, caption.bbox[1]]
            candidate_panels = [crop]
            self.reasons.append("pdf_figure_crop_heuristic")
        block_id = self._add_block(
            "figure", caption.text, page.number, [caption.bbox], figure_id=figure_id,
        )
        self._add_block(
            "caption", caption.text, page.number, [caption.bbox], parent_id=block_id,
        )
        panels: list[dict[str, Any]] = []
        for index, bbox in enumerate(candidate_panels):
            asset_id = make_id("asset", self.fingerprint, figure_id, index, bbox)
            self.assets.append({
                "asset_id": asset_id,
                "kind": "pdf_region",
                "path": "input/source.pdf",
                "mime_type": "application/pdf",
                "sha256": self.fingerprint,
                "state": "virtual",
                "page": page.number,
                "bbox": bbox,
                "figure_id": figure_id,
            })
            panels.append({
                "panel_id": make_id("panel", self.fingerprint, figure_id, index),
                "label": chr(ord("a") + index),
                "asset_id": asset_id,
                "source_locator": pdf_locator(self.fingerprint, page.number, [bbox]),
            })
        self.figures.append({
            "figure_id": figure_id,
            "label": label,
            "caption": caption.text,
            "reading_order": self._order,
            "block_id": block_id,
            "panels": panels,
            "status": "complete" if page.image_bboxes else "degraded",
            "source_locator": pdf_locator(self.fingerprint, page.number, candidate_panels),
            "caption_locator": pdf_locator(self.fingerprint, page.number, [caption.bbox]),
        })

    def _add_pdf_table(
        self,
        page: PageLayout,
        caption: LayoutItem,
        label_id: str,
        caption_index: int,
    ) -> set[int]:
        label = f"Table {label_id}"
        table_id = make_id("tbl", self.fingerprint, page.number, label, caption.bbox)
        crop = [0.0, caption.bbox[3], page.width, min(page.height, caption.bbox[3] + page.height * 0.42)]
        block_id = self._add_block(
            "table", caption.text, page.number, [crop], table_id=table_id,
        )
        self._add_block(
            "caption", caption.text, page.number, [caption.bbox], parent_id=block_id,
        )
        cell_candidates = [
            (index, item) for index, item in enumerate(page.text_items)
            if index > caption_index
            and item.bbox[1] >= caption.bbox[3]
            and item.bbox[3] <= crop[3]
            and not _FIGURE_CAPTION.match(item.text)
            and not _TABLE_CAPTION.match(item.text)
        ]
        rows: list[list[tuple[int, LayoutItem]]] = []
        for index, item in sorted(
            cell_candidates, key=lambda value: (value[1].bbox[1], value[1].bbox[0]),
        ):
            center = (item.bbox[1] + item.bbox[3]) / 2
            previous_center = -1000.0
            if rows:
                previous = rows[-1][0][1]
                previous_center = (previous.bbox[1] + previous.bbox[3]) / 2
            if not rows or abs(center - previous_center) > 5.0:
                rows.append([])
            rows[-1].append((index, item))
        structured_rows = [
            sorted(row, key=lambda value: value[1].bbox[0])
            for row in rows if len(row) >= 2
        ]
        column_counts = [len(row) for row in structured_rows]
        reliable = (
            len(structured_rows) >= 2
            and min(column_counts, default=0) >= 2
            and max(column_counts, default=0) - min(column_counts, default=0) <= 1
        )
        cells: list[dict[str, Any]] = []
        consumed: set[int] = set()
        if reliable:
            for row_index, row in enumerate(structured_rows):
                for column_index, (source_index, item) in enumerate(row):
                    cell_block_id = self._add_block(
                        "table_cell", item.text, page.number, [item.bbox],
                        parent_id=block_id,
                    )
                    cells.append({
                        "cell_id": make_id(
                            "cell", self.fingerprint, table_id,
                            row_index, column_index, item.text,
                        ),
                        "block_id": cell_block_id,
                        "row": row_index,
                        "col": column_index,
                        "rowspan": 1,
                        "colspan": 1,
                        "role": "column_header" if row_index == 0 else "data",
                        "text": item.text,
                        "source_locator": pdf_locator(
                            self.fingerprint, page.number, [item.bbox],
                        ),
                    })
                    consumed.add(source_index)
        else:
            self.reasons.append("pdf_table_structure_unavailable")
        self.tables.append({
            "table_id": table_id,
            "label": label,
            "caption": caption.text,
            "reading_order": self._order,
            "block_id": block_id,
            "cells": cells,
            "status": "complete" if cells else "degraded",
            "source_crop": {"page": page.number, "bbox": crop},
            "source_locator": pdf_locator(self.fingerprint, page.number, [crop]),
            "caption_locator": pdf_locator(self.fingerprint, page.number, [caption.bbox]),
        })
        return consumed

    def _references_from_text(
        self,
        text: str,
        page: int,
        bbox: list[float],
        block_id: str,
        item_index: int,
    ) -> None:
        targets = [("external", match.group(0).rstrip(".,;")) for match in _URL.finditer(text)]
        targets.extend(("citation", "https://doi.org/" + match.group(0)) for match in _DOI.finditer(text))
        for ref_index, (kind, target) in enumerate(targets):
            self.references.append({
                "reference_id": make_id(
                    "ref", self.fingerprint, page, item_index, ref_index, target,
                ),
                "kind": kind,
                "target": target,
                "label": target,
                "source_block_id": block_id,
                "source_locator": pdf_locator(self.fingerprint, page, [bbox]),
            })


def parse_pdf_document(
    job_dir: Path,
    job: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """解析 input/source.pdf，不写 pipeline 产物；扫描件必须显式降级或拒绝。"""
    return ScholarlyPdfAdapter(job_dir, job).parse()
