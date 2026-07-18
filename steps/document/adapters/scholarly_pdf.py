"""把数字或扫描 PDF 解析为带页码和 bbox 的统一 Document Model。"""

from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from PIL import Image

from shared.titles import is_suspicious_title

from ._common import (
    base_document,
    make_id,
    pdf_locator,
    quality_report,
    source_context,
)
from ..layout_detector import (
    DocumentLayoutDetector,
    LayoutDetection,
    LayoutDetectorError,
)


_FIGURE_CAPTION = re.compile(
    r"^(?:Figure|Fig\.?|图)\s*([A-Za-z]?\d+(?:[.:-]\d+)*)\b"
    r"(?=\s*(?:[.:|\u2013\u2014-]|$))",
    re.I,
)
_TABLE_CAPTION = re.compile(
    r"^(?:Table|表)\s*([A-Za-z]?\d+(?:[.:-]\d+)*)\b"
    r"(?=\s*(?:[.:|\u2013\u2014-]|$))",
    re.I,
)
_SECTION_HEADING = re.compile(
    r"^(?:(?:\d+(?:\.\d+)*\.?)|(?:[A-Z]\.(?:\d+(?:\.\d+)*\.?)))\s+\S",
)
_URL = re.compile(r"https?://[^\s<>()\[\]{}]+", re.I)
_DOI = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+\b", re.I)
_SCAN_CHARS_PER_PAGE = 40
_OCR_RELIABLE_THRESHOLD = 0.8
_COVER_LABELS = {
    "title", "authors", "author", "publication date", "date", "permalink",
    "copyright information", "abstract", "introduction", "document version",
    "accepted author manuscript", "published in", "doi", "license",
    "citation for published version (apa)",
}
_AUTHOR_NOISE = {
    "abstract", "berkeley", "copyright", "department", "engineering",
    "institute", "laboratory", "national", "publication", "research",
    "report", "school", "sciences", "university", "working paper",
}
_MONTH_DATE_FORMATS = (
    "%B %d, %Y", "%b %d, %Y", "%B %Y", "%b %Y",
)


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


def _valid_bboxes(bboxes: list[list[float]]) -> list[list[float]]:
    """保留有实际面积的 PDF 区域；退化字形仍由 page locator 表达。"""
    return [
        bbox for bbox in bboxes
        if len(bbox) == 4 and bbox[2] > bbox[0] and bbox[3] > bbox[1]
    ]


def _bbox_union(bboxes: list[list[float]]) -> list[float]:
    valid = _valid_bboxes(bboxes)
    if not valid:
        return []
    return [
        min(bbox[0] for bbox in valid),
        min(bbox[1] for bbox in valid),
        max(bbox[2] for bbox in valid),
        max(bbox[3] for bbox in valid),
    ]


def _compact_text(value: object) -> str:
    return " ".join(str(value or "").split()).strip()


def _caption_text(items: list[LayoutItem]) -> str:
    """合并PDF caption碎片,并修复跨行断词与数学标点空格."""
    text = ""
    for item in items:
        value = _compact_text(item.text)
        if not value:
            continue
        if text.endswith(("-", "\u2010")) and value[0].islower():
            text = text[:-1] + value
        else:
            text = f"{text} {value}".strip()
    text = re.sub(r"(?<=\d)\s*\.\s*(?=\d)", ".", text)
    text = re.sub(r"\(\s+", "(", text)
    text = re.sub(r"\s+\)", ")", text)
    text = re.sub(r"\s+([,;:])", r"\1", text)
    return text


def _normalized_heading(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", _compact_text(value).casefold())


def _normalize_date(value: object) -> str:
    text = _compact_text(value)
    if not text:
        return ""
    iso = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", text)
    if iso:
        return iso.group(1)
    iso_month = re.fullmatch(r"(\d{4})-(0[1-9]|1[0-2])", text)
    if iso_month:
        return iso_month.group(0)
    if re.fullmatch(r"(?:19|20)\d{2}", text):
        return text
    month_date = re.search(
        r"\b(?:January|February|March|April|May|June|July|August|"
        r"September|October|November|December)\s+\d{1,2},\s+\d{4}\b",
        text,
        re.I,
    )
    if month_date:
        try:
            return datetime.strptime(month_date.group(0), "%B %d, %Y").strftime(
                "%Y-%m-%d",
            )
        except ValueError:
            return ""
    for pattern in _MONTH_DATE_FORMATS:
        try:
            parsed = datetime.strptime(text, pattern)
        except ValueError:
            continue
        return parsed.strftime("%Y-%m-%d" if "%d" in pattern else "%Y-%m")
    return ""


def _author_records(values: list[str]) -> list[dict[str, Any]]:
    return [
        {"name": value, "affiliations": [], "emails": [], "notes": []}
        for value in dict.fromkeys(_compact_text(item) for item in values)
        if value
    ]


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
        self._claimed_images: dict[int, set[tuple[float, float, float, float]]] = {}
        self._claimed_detections: dict[int, set[tuple[str, int]]] = {}
        self._table_rules: dict[int, list[tuple[float, float, float]]] = {}
        self._page_rasters: dict[int, Image.Image | None] = {}
        self._layout_detector = DocumentLayoutDetector.from_env()
        self._layout_detections: dict[int, list[LayoutDetection]] = {}
        self._layout_detector_disabled = False
        self._layout_detector_pages = 0
        self._layout_detector_figure_matches = 0
        self._layout_detector_table_matches = 0
        self._layout_detector_failures = 0

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

        metadata = self._paper_metadata(info, pages)
        title = metadata["title"]
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
            metadata={**metadata, "pages": page_count, "pdf_info": info},
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
                "layout_detector_enabled": self._layout_detector is not None,
                "layout_detector_model": (
                    self._layout_detector.model_identity
                    if self._layout_detector is not None else None
                ),
                "layout_detector_pages": self._layout_detector_pages,
                "layout_detector_figure_matches": self._layout_detector_figure_matches,
                "layout_detector_table_matches": self._layout_detector_table_matches,
                "layout_detector_failures": self._layout_detector_failures,
            },
        )
        return document, report

    def _paper_metadata(
        self,
        info: dict[str, str],
        pages: list[PageLayout],
    ) -> dict[str, Any]:
        sidecar = self._sidecar_metadata()
        title = _compact_text(sidecar.get("title"))
        inferred_title = False
        if not title:
            title = self._labeled_cover_value(pages, "title")
            inferred_title = bool(title)
        embedded_title = _compact_text(info.get("Title"))
        embedded_token_title = (
            embedded_title.isalpha()
            and embedded_title.casefold() not in {"draft", "untitled"}
        )
        if (
            not title and embedded_title
            and (not is_suspicious_title(embedded_title) or embedded_token_title)
        ):
            title = embedded_title
        if not title:
            title = self._cover_title(pages)
            inferred_title = bool(title)
        if inferred_title:
            self.reasons.append("pdf_title_inferred")

        authors = self._sidecar_authors(sidecar)
        if not authors:
            authors = [
                item for item in self._split_author_text(info.get("Author", ""))
                if self._looks_like_person(item)
            ]
        if not authors:
            authors = self._cover_authors(pages, title)

        published_at = _normalize_date(
            sidecar.get("published_at") or sidecar.get("date")
        )
        if not published_at:
            published_at = _normalize_date(
                self._labeled_cover_value(pages, "publication date")
                or self._cover_date(pages)
            )
        identifiers = self._identifiers(sidecar, pages)
        return {
            "title": title,
            "original_title": title,
            "authors": _author_records(authors),
            "institutions": [],
            "abstract": _compact_text(sidecar.get("abstract")),
            "keywords": sidecar.get("keywords")
            if isinstance(sidecar.get("keywords"), list) else [],
            "published_at": published_at,
            "publisher": _compact_text(sidecar.get("sitename") or sidecar.get("publisher")),
            "venue": _compact_text(sidecar.get("venue")),
            "license": _compact_text(sidecar.get("license")),
            "lang": _compact_text(sidecar.get("lang") or sidecar.get("language")) or None,
            "identifiers": identifiers,
        }

    def _sidecar_metadata(self) -> dict[str, Any]:
        path = self.job_dir / "input" / "metadata.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _sidecar_authors(sidecar: dict[str, Any]) -> list[str]:
        raw = sidecar.get("authors")
        if isinstance(raw, list):
            return [
                _compact_text(item.get("name") if isinstance(item, dict) else item)
                for item in raw
                if _compact_text(item.get("name") if isinstance(item, dict) else item)
            ]
        return ScholarlyPdfAdapter._split_author_text(sidecar.get("author", ""))

    @staticmethod
    def _split_author_text(value: object) -> list[str]:
        text = _compact_text(value)
        if not text:
            return []
        return [
            item for item in (
                _compact_text(part)
                for part in re.split(r"\s*(?:;|\band\b)\s*", text)
            ) if item
        ]

    @staticmethod
    def _labeled_cover_value(
        pages: list[PageLayout],
        label: str,
    ) -> str:
        for page in pages[:2]:
            for index, item in enumerate(page.text_items[:80]):
                if _normalized_heading(item.text) != _normalized_heading(label):
                    continue
                for candidate in page.text_items[index + 1:index + 4]:
                    value = _compact_text(candidate.text)
                    if not value or value.casefold() in _COVER_LABELS:
                        continue
                    return value
        return ""

    @staticmethod
    def _cover_title(pages: list[PageLayout]) -> str:
        if not pages:
            return ""
        items = pages[0].text_items[:40]
        for index, item in enumerate(items):
            title = _compact_text(item.text)
            if (
                not title or title.casefold() in _COVER_LABELS
                or is_suspicious_title(title)
            ):
                continue
            parts = [title]
            previous = item
            for candidate in items[index + 1:index + 3]:
                value = _compact_text(candidate.text)
                height = max(previous.bbox[3] - previous.bbox[1], 1)
                overlap = min(previous.bbox[2], candidate.bbox[2]) - max(
                    previous.bbox[0], candidate.bbox[0],
                )
                gap = candidate.bbox[1] - previous.bbox[3]
                if (
                    not value or value.casefold() in _COVER_LABELS
                    or gap < -2 or gap > max(5, height * 0.45) or overlap <= 0
                ):
                    break
                parts.append(value)
                previous = candidate
            return " ".join(parts)
        return ""

    @staticmethod
    def _title_span(page: PageLayout, title: str) -> tuple[int, int] | None:
        target = _normalized_heading(title)
        if not target:
            return None
        items = page.text_items[:80]
        for start, item in enumerate(items):
            combined = _normalized_heading(item.text)
            if not combined or not target.startswith(combined):
                continue
            end = start
            while combined != target and end + 1 < len(items):
                candidate = combined + _normalized_heading(items[end + 1].text)
                if not target.startswith(candidate):
                    break
                combined = candidate
                end += 1
            if combined == target:
                return start, end
        return None

    @staticmethod
    def _looks_like_person(value: str) -> bool:
        text = _compact_text(value)
        lowered = text.casefold()
        if (
            not text or len(text) > 80 or re.search(r"\d|https?://|@", text)
            or lowered in _COVER_LABELS or lowered == "et al."
            or lowered.strip("() ") == "extended version"
            or re.search(r"\b(?:inc|corp|corporation|ltd|llc|company)\b", lowered)
            or any(noise in lowered for noise in _AUTHOR_NOISE)
        ):
            return False
        words = re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ][A-Za-zÀ-ÖØ-öø-ÿ.'´-]*", text)
        return 2 <= len(words) <= 8 and any(word[0].isupper() for word in words)

    @staticmethod
    def _normalize_author_name(value: str) -> str:
        text = _compact_text(value)
        for plain, accented in (
            ("a", "á"), ("e", "é"), ("i", "í"), ("o", "ó"), ("u", "ú"),
        ):
            text = re.sub(
                rf"([A-Za-z])´{plain}",
                lambda match: match.group(1) + accented,
                text,
                flags=re.I,
            )
        text = text.strip(" ,;*†‡§¶")
        if text.count(",") == 1:
            family, given = (_compact_text(part) for part in text.split(",", 1))
            if (
                family and given
                and given.casefold().rstrip(".") not in {"jr", "sr", "ii", "iii", "iv"}
                and len(family.split()) <= 3 and len(given.split()) <= 4
            ):
                text = f"{given} {family}"
        return text

    @classmethod
    def _authors_after(
        cls,
        page: PageLayout,
        start: int,
    ) -> list[str]:
        authors: list[tuple[str, LayoutItem]] = []
        for item in page.text_items[start:start + 24]:
            value = _compact_text(item.text)
            if not value:
                continue
            if authors:
                previous_item = authors[-1][1]
                line_height = max(
                    item.bbox[3] - item.bbox[1],
                    previous_item.bbox[3] - previous_item.bbox[1],
                    1,
                )
                if item.bbox[1] - previous_item.bbox[3] > max(24, line_height * 2):
                    break
            lowered = value.casefold()
            if (
                bool(_normalize_date(value))
                or re.search(r"\b(?:19|20)\d{2}\b", value)
                or lowered.rstrip(":").strip() in _COVER_LABELS
            ):
                break
            if len(value) <= 2 and not value.isalpha():
                continue
            if lowered.startswith("and "):
                candidate = cls._normalize_author_name(value[4:])
                if cls._looks_like_person(candidate):
                    authors.append((candidate, item))
                continue
            if authors and value[0].islower():
                previous, previous_item = authors[-1]
                same_row = abs(item.bbox[1] - previous_item.bbox[1]) <= max(
                    item.bbox[3] - item.bbox[1], previous_item.bbox[3] - previous_item.bbox[1],
                )
                if same_row and item.bbox[0] - previous_item.bbox[2] <= 10:
                    separator = "" if item.bbox[0] <= previous_item.bbox[2] + 2 else " "
                    authors[-1] = (
                        cls._normalize_author_name(previous + separator + value), item,
                    )
                    continue
            normalized = cls._normalize_author_name(re.sub(
                r"(?<=[A-Za-zÀ-ÖØ-öø-ÿ])\d+\b", "", value,
            ))
            split_authors = cls._split_author_text(normalized)
            if len(split_authors) == 1 and normalized.count(",") >= 2:
                split_authors = [
                    _compact_text(part)
                    for part in re.split(r"\s*,\s*|\s+and\s+", normalized)
                    if _compact_text(part)
                ]
            if len(split_authors) > 1:
                for part in split_authors:
                    if cls._looks_like_person(part):
                        authors.append((cls._normalize_author_name(part), item))
            elif cls._looks_like_person(normalized):
                authors.append((normalized, item))
        return list(dict.fromkeys(name for name, _item in authors))

    @staticmethod
    def _author_list_is_abbreviated(page: PageLayout, start: int) -> bool:
        for item in page.text_items[start:start + 24]:
            value = _compact_text(item.text).casefold().rstrip(".")
            if value in {"et al", "and others"}:
                return True
            if re.search(r"\b(?:19|20)\d{2}\b", value):
                break
        return False

    @classmethod
    def _cover_authors(cls, pages: list[PageLayout], title: str) -> list[str]:
        if not title:
            return []
        primary: tuple[int, int, int] | None = None
        matches: list[tuple[int, int, int]] = []
        for page_index, page in enumerate(pages[:3]):
            span = cls._title_span(page, title)
            if span:
                match = (page_index, span[0], span[1])
                matches.append(match)
                primary = primary or match
        if primary:
            authors = cls._authors_after(pages[primary[0]], primary[2] + 1)
            if (
                len(authors) >= 2
                and not cls._author_list_is_abbreviated(
                    pages[primary[0]], primary[2] + 1,
                )
            ):
                return authors
        for page_index, _start, end in reversed(matches[1:]):
            authors = cls._authors_after(pages[page_index], end + 1)
            if len(authors) >= 2:
                return authors
        target = _normalized_heading(title)
        for page in pages[:3]:
            for index, item in enumerate(page.text_items[:100]):
                if _normalized_heading(item.text) != target:
                    continue
                if primary and page is pages[primary[0]] and index == primary[1]:
                    continue
                authors = cls._authors_after(page, index + 1)
                if len(authors) >= 2:
                    return authors
        return []

    @staticmethod
    def _cover_date(pages: list[PageLayout]) -> str:
        for page in pages[:3]:
            for item in page.text_items[:100]:
                value = _compact_text(item.text)
                if re.search(
                    r"\b(?:January|February|March|April|May|June|July|August|"
                    r"September|October|November|December)\s+\d{1,2},\s+\d{4}\b",
                    value,
                    re.I,
                ):
                    return value
                publication_year = re.search(
                    r"\b(?:published|proceedings|conference|journal|volume|vol\.)\b"
                    r".*\b((?:19|20)\d{2})\b",
                    value,
                    re.I,
                )
                if publication_year:
                    return publication_year.group(1)
                short_publication_year = re.search(
                    r"\b(?:proceedings|conference|journal|symposium|usenix|osdi|sosp|nsdi)\b"
                    r".*?[\u2019'](\d{2})\b",
                    value,
                    re.I,
                )
                if short_publication_year:
                    year = int(short_publication_year.group(1))
                    return str(2000 + year if year < 30 else 1900 + year)
        return ""

    def _identifiers(
        self,
        sidecar: dict[str, Any],
        pages: list[PageLayout],
    ) -> dict[str, str]:
        raw_identifiers = sidecar.get("identifiers")
        raw_identifiers = raw_identifiers if isinstance(raw_identifiers, dict) else {}
        identifiers = {
            str(key): _compact_text(value)
            for key, value in raw_identifiers.items() if _compact_text(value)
        }
        for key in ("doi", "arxiv_id"):
            if _compact_text(sidecar.get(key)):
                identifiers[key] = _compact_text(sidecar[key])
        urls = [
            _compact_text(sidecar.get("source_url")),
            _compact_text(sidecar.get("final_url")),
            _compact_text(self.job.get("url")),
        ]
        for url in filter(None, urls):
            parsed = urlparse(url)
            if match := re.search(r"/papers/(w\d+)\b", parsed.path, re.I):
                identifiers.setdefault("nber_working_paper", match.group(1).lower())
            ssrn_id = parse_qs(parsed.query).get("abstract_id", [""])[0]
            if ssrn_id.isdigit():
                identifiers.setdefault("ssrn_id", ssrn_id)
            if match := re.search(r"/(qt[0-9a-z]+)(?:/|\.pdf|$)", parsed.path, re.I):
                identifiers.setdefault("escholarship_id", match.group(1).lower())
            if match := re.search(r"/(?:abs|pdf)/(\d{4}\.\d{4,5})", parsed.path):
                identifiers.setdefault("arxiv_id", match.group(1))
        for page in pages[:2]:
            for item in page.text_items[:100]:
                text = _compact_text(item.text)
                if match := re.search(r"\b(UCB/EECS-\d{4}-\d+)\b", text, re.I):
                    identifiers.setdefault("report_number", match.group(1).upper())
                if match := _DOI.search(text):
                    identifiers.setdefault("doi", match.group(0).rstrip(".,;"))
        return identifiers

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
                layout_pdf = Path(temp_dir) / "source.pdf"
                shutil.copyfile(self.path, layout_pdf)
                xml = self._run([
                    "pdftohtml", "-xml", "-stdout", "-hidden", "-zoom", "1",
                    str(layout_pdf),
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
        figure_winners = self._visual_caption_winners(pages, _FIGURE_CAPTION)
        table_winners = self._visual_caption_winners(pages, _TABLE_CAPTION)
        first_text = True
        for page in pages:
            consumed: set[int] = set()
            for item_index, item in enumerate(page.text_items):
                if item_index in consumed:
                    continue
                figure_match = _FIGURE_CAPTION.match(item.text)
                table_match = _TABLE_CAPTION.match(item.text)
                if allow_visuals and figure_match and (page.number, item_index) in figure_winners:
                    caption_items = self._figure_caption_items(page, item_index)
                    consumed.update(index for index, _item in caption_items[1:])
                    self._add_pdf_figure(
                        page, [value for _index, value in caption_items],
                        figure_match.group(1),
                    )
                    continue
                if allow_visuals and table_match and (page.number, item_index) in table_winners:
                    caption_items = self._figure_caption_items(page, item_index)
                    consumed.update(index for index, _item in caption_items[1:])
                    consumed.update(
                        self._add_pdf_table(
                            page,
                            [value for _index, value in caption_items],
                            table_match.group(1),
                            {index for index, _value in caption_items},
                        )
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

    def _visual_caption_winners(
        self,
        pages: list[PageLayout],
        pattern: re.Pattern[str],
    ) -> set[tuple[int, int]]:
        """同一编号只保留信息最完整的caption,裸编号或正文引用不能重复占位."""
        candidates: dict[str, list[tuple[int, int, int, int, int, int]]] = {}
        for page in pages:
            for index, item in enumerate(page.text_items):
                match = pattern.match(item.text)
                if match is None or not self._caption_starts_row(page, index):
                    continue
                caption = _caption_text([
                    value for _source, value in self._figure_caption_items(page, index)
                ])
                suffix = caption[match.end():].strip(" .:|\u2013\u2014-")
                score = len(_compact_text(suffix))
                first_suffix = item.text[match.end():].strip(" .:|\u2013\u2014-")
                split_caption = int(not first_suffix and bool(suffix))
                explicit_delimiter = int(
                    item.text[match.end():].lstrip().startswith((":", "|", "\u2013", "\u2014"))
                )
                full_word = int(item.text.casefold().startswith("figure"))
                candidates.setdefault(match.group(1).casefold(), []).append(
                    (
                        explicit_delimiter, split_caption, full_word, score,
                        page.number, index,
                    ),
                )
        return {
            (page, index)
            for values in candidates.values()
            for _delimiter, _split, _word, _score, page, index in [max(
                values,
                key=lambda value: (
                    value[0], value[1], value[2], value[3], -value[4], -value[5],
                ),
            )]
        }

    @staticmethod
    def _caption_starts_row(page: PageLayout, caption_index: int) -> bool:
        """排除正文行中被PDF拆成独立text item的Figure/Table引用."""
        caption = page.text_items[caption_index]
        caption_height = max(caption.bbox[3] - caption.bbox[1], 1.0)
        if caption.bbox[0] >= page.width * 0.5:
            column_left, column_right = page.width * 0.5 + 10, page.width
        elif caption.bbox[2] <= page.width * 0.5:
            column_left, column_right = 0.0, page.width * 0.5 - 10
        else:
            column_left, column_right = 0.0, page.width
        for index, item in enumerate(page.text_items):
            if index == caption_index or item.bbox[0] >= caption.bbox[0] - 1:
                continue
            item_center = (item.bbox[0] + item.bbox[2]) / 2
            if not column_left <= item_center <= column_right:
                continue
            item_height = max(item.bbox[3] - item.bbox[1], 1.0)
            overlap = min(item.bbox[3], caption.bbox[3]) - max(
                item.bbox[1], caption.bbox[1],
            )
            if (
                overlap >= min(caption_height, item_height) * 0.5
                and -2 <= caption.bbox[0] - item.bbox[2] <= 16
            ):
                return False
        return True

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
        locator = pdf_locator(self.fingerprint, page, _valid_bboxes(bboxes))
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

    @staticmethod
    def _figure_caption_items(
        page: PageLayout,
        caption_index: int,
    ) -> list[tuple[int, LayoutItem]]:
        result = [(caption_index, page.text_items[caption_index])]
        for index in range(caption_index + 1, min(len(page.text_items), caption_index + 96)):
            previous = result[-1][1]
            candidate = page.text_items[index]
            vertical_overlap = min(previous.bbox[3], candidate.bbox[3]) - max(
                previous.bbox[1], candidate.bbox[1],
            )
            same_line = (
                vertical_overlap >= min(
                    previous.bbox[3] - previous.bbox[1],
                    candidate.bbox[3] - candidate.bbox[1],
                ) * 0.5
                and -2 <= candidate.bbox[0] - previous.bbox[2] <= 12
            )
            if same_line:
                result.append((index, candidate))
                continue
            if _FIGURE_CAPTION.match(candidate.text) or _TABLE_CAPTION.match(candidate.text):
                break
            previous_line = [previous]
            for _source, line_item in reversed(result[:-1]):
                line_overlap = min(previous.bbox[3], line_item.bbox[3]) - max(
                    previous.bbox[1], line_item.bbox[1],
                )
                if line_overlap < min(
                    previous.bbox[3] - previous.bbox[1],
                    line_item.bbox[3] - line_item.bbox[1],
                ) * 0.5:
                    break
                previous_line.append(line_item)
            line_bbox = _bbox_union([value.bbox for value in previous_line])
            previous_height = max(line_bbox[3] - line_bbox[1], 1)
            vertical_gap = candidate.bbox[1] - line_bbox[3]
            horizontal_overlap = min(line_bbox[2], candidate.bbox[2]) - max(
                line_bbox[0], candidate.bbox[0],
            )
            if (
                previous.text.rstrip().endswith((".", "!", "?", ";"))
                and vertical_gap > max(4.0, previous_height * 0.3)
            ):
                break
            if (
                vertical_gap < -2 or vertical_gap > max(12, previous_height * 0.6)
                or horizontal_overlap <= 0
            ):
                break
            result.append((index, candidate))
        return result

    @staticmethod
    def _figure_column(page: PageLayout, caption: LayoutItem) -> tuple[float, float]:
        margin = page.width * 0.08
        if page.width > page.height * 1.05:
            return margin, page.width - margin
        if caption.bbox[2] <= page.width * 0.5:
            return margin, page.width * 0.5 - 10
        if caption.bbox[0] >= page.width * 0.5:
            return page.width * 0.5 + 10, page.width - margin
        return margin, page.width - margin

    @staticmethod
    def _table_column(page: PageLayout, caption: LayoutItem) -> tuple[float, float]:
        margin = page.width * 0.04
        if caption.bbox[2] <= page.width * 0.5:
            return margin, page.width * 0.5 - 4
        if caption.bbox[0] >= page.width * 0.5:
            return page.width * 0.5 + 4, page.width - margin
        return margin, page.width - margin

    @staticmethod
    def _bbox_overlaps_column(
        bbox: list[float],
        left: float,
        right: float,
    ) -> bool:
        width = bbox[2] - bbox[0]
        if width <= 0:
            return left <= bbox[0] <= right
        overlap = min(bbox[2], right) - max(bbox[0], left)
        return overlap >= min(width * 0.35, max((right - left) * 0.08, 1))

    def _page_layout_detections(self, page: PageLayout) -> list[LayoutDetection]:
        detector = self._layout_detector
        if detector is None or self._layout_detector_disabled:
            return []
        if page.number in self._layout_detections:
            return self._layout_detections[page.number]
        try:
            detections = detector.detect_pdf_page(
                self.path,
                page=page.number,
                page_width=page.width,
                page_height=page.height,
            )
        except LayoutDetectorError:
            self._layout_detector_disabled = True
            self._layout_detector_failures += 1
            if "pdf_layout_detector_failed" not in self.reasons:
                self.reasons.append("pdf_layout_detector_failed")
            return []
        self._layout_detector_pages += 1
        self._layout_detections[page.number] = detections
        return detections

    @staticmethod
    def _intersection_area(first: list[float], second: list[float]) -> float:
        width = min(first[2], second[2]) - max(first[0], second[0])
        height = min(first[3], second[3]) - max(first[1], second[1])
        return max(0.0, width) * max(0.0, height)

    def _detected_visual_region(
        self,
        page: PageLayout,
        caption: LayoutItem,
        kind: str,
    ) -> list[float]:
        """用caption给模型候选分配语义;低关联框不得覆盖确定性回退。"""
        if kind not in {"figure", "table"}:
            raise ValueError("layout visual kind is invalid")
        detections = self._page_layout_detections(page)
        if not detections:
            return []
        if kind == "figure":
            column_left, column_right = self._figure_column(page, caption)
            preferred_direction = "above"
        else:
            column_left, column_right = self._table_column(page, caption)
            preferred_direction = "below"
        column_width = max(column_right - column_left, 1.0)
        caption_bbox = caption.bbox
        caption_height = max(caption_bbox[3] - caption_bbox[1], 1.0)
        caption_center = (caption_bbox[0] + caption_bbox[2]) / 2
        claimed = self._claimed_detections.setdefault(page.number, set())
        ranked: list[tuple[float, float, int, str, list[float]]] = []
        for index, detection in enumerate(detections):
            if detection.kind != kind or (kind, index) in claimed:
                continue
            bbox = list(detection.bbox)
            width = bbox[2] - bbox[0]
            height = bbox[3] - bbox[1]
            horizontal_overlap = min(bbox[2], column_right) - max(
                bbox[0], column_left,
            )
            if horizontal_overlap < min(width, column_width) * 0.25:
                continue
            overlap_area = self._intersection_area(bbox, caption_bbox)
            if overlap_area > min(width * height, (
                caption_bbox[2] - caption_bbox[0]
            ) * caption_height) * 0.2:
                continue
            if bbox[3] <= caption_bbox[1] + 3:
                direction = "above"
                distance = max(0.0, caption_bbox[1] - bbox[3])
            elif bbox[1] >= caption_bbox[3] - 3:
                direction = "below"
                distance = max(0.0, bbox[1] - caption_bbox[3])
            else:
                continue
            if distance > page.height * 0.25:
                continue
            center = (bbox[0] + bbox[2]) / 2
            direction_penalty = 0.04 if direction != preferred_direction else 0.0
            score = (
                distance / max(page.height, 1.0)
                + abs(center - caption_center) / max(page.width, 1.0) * 0.04
                + direction_penalty
                - detection.confidence * 0.02
            )
            ranked.append((score, -detection.confidence, index, direction, bbox))
        if not ranked:
            return []
        _score, _confidence, index, direction, bbox = min(ranked)
        claimed.add((kind, index))
        padding = 2.0
        region = [
            max(0.0, bbox[0] - padding),
            max(0.0, bbox[1] - padding),
            min(page.width, bbox[2] + padding),
            min(page.height, bbox[3] + padding),
        ]
        if direction == "above":
            region[3] = min(region[3], caption_bbox[1] - 1)
        else:
            region[1] = max(region[1], caption_bbox[3] + 1)
        return region if _valid_bboxes([region]) else []

    def _items_in_region(
        self,
        page: PageLayout,
        crop: list[float],
        excluded: set[int],
    ) -> list[tuple[int, LayoutItem]]:
        return [
            (index, item) for index, item in enumerate(page.text_items)
            if index not in excluded
            and self._intersection_area(item.bbox, crop) > 0
        ]

    @classmethod
    def _figure_edge_has_prose(
        cls,
        page: PageLayout,
        edge: float,
        direction: str,
        left: float,
        right: float,
    ) -> bool:
        """正文恰好落在启发式边界时禁止把触边墨迹当成未裁完整的图."""
        column_width = right - left
        for item in page.text_items:
            text = _compact_text(item.text)
            words = re.findall(r"[A-Za-z]{2,}", text)
            if (
                len(text) < 10
                or len(words) < 2
                or not cls._bbox_overlaps_column(item.bbox, left, right)
            ):
                continue
            item_edge = item.bbox[3] if direction == "above" else item.bbox[1]
            if abs(item_edge - edge) > 5:
                continue
            if (
                len(text) >= 25
                and len(words) >= 5
                and item.bbox[2] - item.bbox[0] >= column_width * 0.55
            ):
                return True
            for neighbor in page.text_items:
                neighbor_text = _compact_text(neighbor.text)
                neighbor_words = re.findall(r"[A-Za-z]{2,}", neighbor_text)
                if (
                    len(neighbor_text) < 25
                    or len(neighbor_words) < 5
                    or neighbor.bbox[2] - neighbor.bbox[0] < column_width * 0.55
                    or not cls._bbox_overlaps_column(neighbor.bbox, left, right)
                ):
                    continue
                if direction == "above":
                    gap = item.bbox[1] - neighbor.bbox[3]
                else:
                    gap = neighbor.bbox[1] - item.bbox[3]
                if 0 <= gap <= 6:
                    return True
        return False

    def _figure_image_cluster(
        self,
        page: PageLayout,
        caption: LayoutItem,
    ) -> list[list[float]]:
        left, right = self._figure_column(page, caption)
        claimed = self._claimed_images.setdefault(page.number, set())
        available = [
            bbox for bbox in page.image_bboxes
            if tuple(bbox) not in claimed
            and self._bbox_overlaps_column(bbox, left, right)
        ]
        if not available:
            return []

        sides: list[tuple[float, str, list[list[float]]]] = []
        above = [bbox for bbox in available if bbox[3] <= caption.bbox[1] + 3]
        below = [bbox for bbox in available if bbox[1] >= caption.bbox[3] - 3]
        if above:
            distance = min(max(0.0, caption.bbox[1] - bbox[3]) for bbox in above)
            sides.append((distance, "above", above))
        if below:
            distance = min(max(0.0, bbox[1] - caption.bbox[3]) for bbox in below)
            sides.append((distance, "below", below))
        if not sides:
            return []
        distance, direction, candidates = min(sides, key=lambda item: item[0])
        if distance > page.height * 0.2:
            return []

        heights = sorted(max(bbox[3] - bbox[1], 1) for bbox in candidates)
        median_height = heights[len(heights) // 2]
        row_tolerance = max(6.0, min(page.height * 0.04, median_height * 0.08))
        if direction == "above":
            nearest_edge = max(bbox[3] for bbox in candidates)
            cluster = [
                bbox for bbox in candidates
                if nearest_edge - bbox[3] <= row_tolerance
            ]
        else:
            nearest_edge = min(bbox[1] for bbox in candidates)
            cluster = [
                bbox for bbox in candidates
                if bbox[1] - nearest_edge <= row_tolerance
            ]

        gap_limit = max(12.0, min(page.height * 0.06, median_height * 0.15))
        remaining = [bbox for bbox in candidates if bbox not in cluster]
        while remaining:
            union = _bbox_union(cluster)
            adjacent: list[list[float]] = []
            for bbox in remaining:
                if direction == "above":
                    gap = union[1] - bbox[3]
                else:
                    gap = bbox[1] - union[3]
                if -row_tolerance <= gap <= gap_limit:
                    adjacent.append(bbox)
            if not adjacent:
                break
            cluster.extend(adjacent)
            remaining = [bbox for bbox in remaining if bbox not in adjacent]

        overview = _bbox_union(cluster)
        overview_width = overview[2] - overview[0]
        overview_height = overview[3] - overview[1]
        column_width = right - left
        component_area = sum(
            (bbox[2] - bbox[0]) * (bbox[3] - bbox[1]) for bbox in cluster
        )
        overview_area = max(overview_width * overview_height, 1.0)
        if (
            overview_width < column_width * 0.12
            or (
                overview_width < column_width * 0.2
                and overview_height < page.height * 0.08
            )
            or (len(cluster) > 1 and component_area / overview_area < 0.18)
        ):
            return []
        claimed.update(tuple(bbox) for bbox in cluster)
        return sorted(cluster, key=lambda bbox: (bbox[1], bbox[0], bbox[3], bbox[2]))

    @classmethod
    def _heuristic_figure_crop(
        cls,
        page: PageLayout,
        caption: LayoutItem,
    ) -> list[float]:
        """无可靠raster bbox时用两侧版面证据推导vector figure区域."""
        left, right = cls._figure_column(page, caption)
        column_width = right - left
        if page.width > page.height * 1.05:
            height_ratio = 0.75
        else:
            height_ratio = 0.18 if column_width < page.width * 0.7 else 0.32
        window = page.height * height_ratio

        def is_boundary(item: LayoutItem) -> bool:
            return bool(
                re.match(r"^(?:Notes?|Source)\s*[:.]", item.text.strip(), re.I)
                or _FIGURE_CAPTION.match(item.text)
                or _TABLE_CAPTION.match(item.text)
            )

        visual_boundaries: list[list[float]] = []
        for index, item in enumerate(page.text_items):
            if _FIGURE_CAPTION.match(item.text):
                visual_boundaries.append(_bbox_union([
                    value.bbox
                    for _source, value in cls._figure_caption_items(page, index)
                ]))
            elif _TABLE_CAPTION.match(item.text):
                visual_boundaries.append(item.bbox)
            elif re.match(r"^(?:Notes?|Source)\s*[:.]", item.text.strip(), re.I):
                visual_boundaries.append(item.bbox)

        below_boundaries = [
            bbox[1] for bbox in visual_boundaries
            if bbox[1] > caption.bbox[3] + 3
            and cls._bbox_overlaps_column(bbox, left, right)
        ]
        below_end = min(
            below_boundaries,
            default=min(page.height, caption.bbox[3] + window),
        )
        above_boundaries = [
            bbox[3] for bbox in visual_boundaries
            if bbox[3] < caption.bbox[1] - 3
            and cls._bbox_overlaps_column(bbox, left, right)
        ]
        above_start = max(
            max(0.0, caption.bbox[1] - window),
            max(above_boundaries, default=0.0),
        )

        def side_items(start: float, end: float) -> list[LayoutItem]:
            return [
                item for item in page.text_items
                if item.bbox[1] >= start and item.bbox[3] <= end
                and cls._bbox_overlaps_column(item.bbox, left, right)
                and not is_boundary(item)
            ]

        above_items = side_items(above_start, caption.bbox[1] - 1)
        below_items = side_items(caption.bbox[3] + 1, below_end)
        above_images = [
            bbox for bbox in page.image_bboxes
            if bbox[1] >= above_start and bbox[3] <= caption.bbox[1] + 2
            and cls._bbox_overlaps_column(bbox, left, right)
        ]
        below_images = [
            bbox for bbox in page.image_bboxes
            if bbox[1] >= caption.bbox[3] - 2 and bbox[3] <= below_end
            and cls._bbox_overlaps_column(bbox, left, right)
        ]

        def evidence(items: list[LayoutItem], images: list[list[float]]) -> tuple[int, int]:
            prose = sum(
                item.bbox[2] - item.bbox[0] >= column_width * 0.55
                for item in items
            )
            labels = sum(
                item.bbox[2] - item.bbox[0] < column_width * 0.55
                and not re.fullmatch(r"\d+", item.text.strip())
                for item in items
            )
            return labels * 2 + len(images) * 4 - prose, prose

        above_score, above_prose = evidence(above_items, above_images)
        below_score, below_prose = evidence(below_items, below_images)
        prefer_above = caption.bbox[1] >= page.height * 0.15
        preferred_items = above_items if prefer_above else below_items
        preferred_images = above_images if prefer_above else below_images
        if preferred_items or preferred_images:
            direction = "above" if prefer_above else "below"
        else:
            opposite_images = below_images if prefer_above else above_images
            opposite_score = below_score if prefer_above else above_score
            opposite_prose = below_prose if prefer_above else above_prose
            if opposite_images or (opposite_score >= 4 and opposite_prose == 0):
                direction = "below" if prefer_above else "above"
            else:
                direction = "above" if prefer_above else "below"

        crop_left, crop_right = left, right
        selected_items = above_items if direction == "above" else below_items
        selected_images = above_images if direction == "above" else below_images
        if (
            column_width < page.width * 0.7
            and caption.bbox[0] >= page.width * 0.5
            and caption.bbox[2] - caption.bbox[0] < column_width * 0.9
        ):
            non_prose_boxes = [
                item.bbox for item in selected_items
                if item.bbox[2] - item.bbox[0] < column_width * 0.55
                and caption.bbox[0]
                <= (item.bbox[0] + item.bbox[2]) / 2
                <= caption.bbox[2]
            ] + selected_images
            if non_prose_boxes:
                horizontal = _bbox_union(non_prose_boxes)
                max_padding = max(12.0, column_width * 0.08)
                left_padding = min(
                    max_padding, max(12.0, abs(caption.bbox[0] - horizontal[0])),
                )
                right_padding = min(
                    max_padding, max(12.0, abs(caption.bbox[2] - horizontal[2])),
                )
                crop_left = max(left, horizontal[0] - left_padding)
                crop_right = min(right, horizontal[2] + right_padding)

        selected_rows = cls._layout_rows(list(enumerate(selected_items)))
        row_records: list[tuple[list[float], bool, bool]] = []
        for row in selected_rows:
            row_bboxes = _valid_bboxes([item.bbox for _index, item in row])
            if not row_bboxes:
                continue
            row_bbox = _bbox_union(row_bboxes)
            row_text = _compact_text(" ".join(item.text for _index, item in row))
            words = re.findall(r"[A-Za-z]{2,}", row_text)
            word_count = len(words)
            unique_ratio = len({word.casefold() for word in words}) / max(word_count, 1)
            panel_markers = len(re.findall(r"\([a-z]\)", row_text, re.I))
            looks_like_prose = bool(
                (
                    len(row_text) >= 25
                    and word_count >= 5
                    and unique_ratio >= 0.6
                    and panel_markers < 2
                    and row_bbox[2] - row_bbox[0] >= column_width * 0.3
                )
                or (
                    row_records and row_records[-1][1]
                    and row_bbox[1] - row_records[-1][0][3] <= 4
                    and len(row_text) >= 10
                    and word_count >= 2
                )
            )
            row_records.append((row_bbox, looks_like_prose, cls._row_is_section(row)))
        if direction == "above":
            leading_cut: float | None = None
            previous_bottom: float | None = None
            cut_after_section = False
            for row_bbox, looks_like_prose, looks_like_section in row_records:
                broad_prose = bool(
                    looks_like_prose
                    and row_bbox[2] - row_bbox[0] >= column_width * 0.65
                )
                if leading_cut is None:
                    if not broad_prose:
                        break
                    leading_cut = row_bbox[3]
                    previous_bottom = row_bbox[3]
                    cut_after_section = False
                    continue
                gap = row_bbox[1] - (previous_bottom or row_bbox[1])
                if broad_prose and gap <= 16:
                    leading_cut = row_bbox[3]
                    previous_bottom = row_bbox[3]
                    cut_after_section = False
                    continue
                if looks_like_section and gap <= 32:
                    leading_cut = row_bbox[3]
                    previous_bottom = row_bbox[3]
                    cut_after_section = True
                    continue
                break
            if leading_cut is not None:
                above_start = max(
                    above_start, leading_cut + (2.0 if cut_after_section else 0.0),
                )
        prose_clusters: list[list[list[float]]] = []
        cluster: list[list[float]] = []
        for row_bbox, looks_like_prose, _looks_like_section in row_records:
            if looks_like_prose:
                cluster.append(row_bbox)
            elif cluster:
                prose_clusters.append(cluster)
                cluster = []
        if cluster:
            prose_clusters.append(cluster)
        if column_width < page.width * 0.7:
            candidates = []
        elif direction == "above":
            candidates = [
                values for values in prose_clusters
                if len(values) >= 2
                and any(
                    bbox[1] >= values[-1][3] - 2
                    for bbox, is_prose, _section in row_records if not is_prose
                )
            ]
            if candidates:
                above_start = max(above_start, max(values[-1][3] for values in candidates))
        else:
            candidates = [
                values for values in prose_clusters
                if len(values) >= 2
                and any(
                    bbox[3] <= values[0][1] + 2
                    for bbox, is_prose, _section in row_records if not is_prose
                )
            ]
            if candidates:
                below_end = min(below_end, min(values[0][1] for values in candidates))

        if direction == "below":
            return [crop_left, caption.bbox[3], crop_right, below_end]
        caption_gap = 4.0 if page.width > page.height * 1.05 else 0.0
        return [crop_left, above_start, crop_right, caption.bbox[1] - caption_gap]

    def _add_pdf_figure(
        self,
        page: PageLayout,
        caption_items: list[LayoutItem],
        label_id: str,
    ) -> None:
        caption = LayoutItem(
            _caption_text(caption_items),
            [
                min(item.bbox[0] for item in caption_items),
                min(item.bbox[1] for item in caption_items),
                max(item.bbox[2] for item in caption_items),
                max(item.bbox[3] for item in caption_items),
            ],
            min(
                (item.confidence for item in caption_items if item.confidence is not None),
                default=None,
            ),
        )
        label = f"Figure {label_id}"
        figure_id = make_id("fig", self.fingerprint, page.number, label, caption.bbox)
        components = self._figure_image_cluster(page, caption)
        detected_region = self._detected_visual_region(page, caption, "figure")
        overview = detected_region or _bbox_union(components)
        if detected_region:
            self._layout_detector_figure_matches += 1
        if not overview:
            overview = self._heuristic_figure_crop(page, caption)
            direction = "above" if overview[3] <= caption.bbox[1] + 2 else "below"
            raster_available = self._page_visual_raster(page) is not None
            column_left, column_right = self._figure_column(page, caption)
            if raster_available:
                edge = overview[1] if direction == "above" else overview[3]
                if self._figure_edge_has_prose(
                    page, edge, direction, column_left, column_right,
                ):
                    if direction == "above":
                        overview = [
                            overview[0], min(overview[1] + 2, overview[3]),
                            overview[2], overview[3],
                        ]
                    else:
                        overview = [
                            overview[0], overview[1], overview[2],
                            max(overview[1], overview[3] - 2),
                        ]
            refined = self._refine_heuristic_figure_crop(page, overview, direction)
            visual_boundaries: list[list[float]] = []
            for index, item in enumerate(page.text_items):
                if _FIGURE_CAPTION.match(item.text):
                    visual_boundaries.append(_bbox_union([
                        value.bbox for _source, value in self._figure_caption_items(page, index)
                    ]))
                elif _TABLE_CAPTION.match(item.text):
                    visual_boundaries.append(item.bbox)
                elif re.match(r"^(?:Notes?|Source)\s*[:.]", item.text.strip(), re.I):
                    visual_boundaries.append(item.bbox)
            height = overview[3] - overview[1]
            candidate = overview
            if raster_available:
                for _attempt in range(3):
                    edge = candidate[1] if direction == "above" else candidate[3]
                    if self._figure_edge_has_prose(
                        page, edge, direction, column_left, column_right,
                    ):
                        break
                    if direction == "above":
                        if refined[1] > candidate[1] + 1 or candidate[1] <= 0:
                            break
                        previous_edges = [
                            bbox[3] for bbox in visual_boundaries
                            if bbox[3] < caption.bbox[1] - 3
                            and self._bbox_overlaps_column(
                                bbox, column_left, column_right,
                            )
                        ]
                        expanded_edge = max(
                            max(previous_edges, default=0.0),
                            candidate[1] - height * 0.5,
                        )
                        if expanded_edge >= candidate[1] - 1:
                            break
                        candidate = [
                            candidate[0], expanded_edge, candidate[2], candidate[3],
                        ]
                    else:
                        if refined[3] < candidate[3] - 1 or candidate[3] >= page.height:
                            break
                        next_edges = [
                            bbox[1] for bbox in visual_boundaries
                            if bbox[1] > caption.bbox[3] + 3
                            and self._bbox_overlaps_column(
                                bbox, column_left, column_right,
                            )
                        ]
                        expanded_edge = min(
                            min(next_edges, default=page.height),
                            candidate[3] + height * 0.5,
                        )
                        if expanded_edge <= candidate[3] + 1:
                            break
                        candidate = [
                            candidate[0], candidate[1], candidate[2], expanded_edge,
                        ]
                    refined = self._refine_heuristic_figure_crop(
                        page, candidate, direction, anchor=overview,
                    )
            overview = refined
            self.reasons.append("pdf_figure_crop_heuristic")
        block_id = self._add_block(
            "figure", caption.text, page.number, [caption.bbox], figure_id=figure_id,
        )
        self._add_block(
            "caption", caption.text, page.number, [caption.bbox], parent_id=block_id,
        )
        asset_id = make_id("asset", self.fingerprint, figure_id, "overview", overview)
        self.assets.append({
            "asset_id": asset_id,
            "kind": "pdf_region",
            "path": "input/source.pdf",
            "mime_type": "application/pdf",
            "sha256": self.fingerprint,
            "state": "virtual",
            "page": page.number,
            "bbox": overview,
            "figure_id": figure_id,
        })
        panels = [{
            "panel_id": make_id("panel", self.fingerprint, figure_id, "overview"),
            "label": "overview",
            "asset_id": asset_id,
            "source_locator": pdf_locator(self.fingerprint, page.number, [overview]),
        }]
        self.figures.append({
            "figure_id": figure_id,
            "label": label,
            "caption": caption.text,
            "reading_order": self._order,
            "block_id": block_id,
            "panels": panels,
            "status": "complete" if components or detected_region else "degraded",
            "source_locator": pdf_locator(self.fingerprint, page.number, [overview]),
            "caption_locator": pdf_locator(self.fingerprint, page.number, [caption.bbox]),
        })

    def _page_visual_raster(self, page: PageLayout) -> Image.Image | None:
        """按需渲染72 DPI灰度页;Figure边界与Table横线检测共享同一份缓存."""
        if page.number in self._page_rasters:
            return self._page_rasters[page.number]
        raster: Image.Image | None = None
        try:
            with tempfile.TemporaryDirectory(prefix="flori-pdf-visual-") as temp_dir:
                prefix = Path(temp_dir) / "page"
                subprocess.run(
                    [
                        "pdftoppm", "-f", str(page.number), "-l", str(page.number),
                        "-r", "72", "-gray", "-png", "-singlefile",
                        str(self.path), str(prefix),
                    ],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=60,
                )
                with Image.open(prefix.with_suffix(".png")) as source:
                    raster = source.convert("L").copy()
        except (OSError, subprocess.SubprocessError, TimeoutError):
            raster = None
        self._page_rasters[page.number] = raster
        return raster

    def _refine_heuristic_figure_crop(
        self,
        page: PageLayout,
        crop: list[float],
        direction: str,
        *,
        anchor: list[float] | None = None,
    ) -> list[float]:
        """把候选区收缩到真实墨迹外接框;失败时保留版面候选而不伪造边界."""
        raster = self._page_visual_raster(page)
        if raster is None or not _valid_bboxes([crop]):
            return crop
        scale_x = raster.width / max(page.width, 1.0)
        scale_y = raster.height / max(page.height, 1.0)
        left = max(0, math.floor(crop[0] * scale_x))
        top = max(0, math.floor(crop[1] * scale_y))
        right = min(raster.width, math.ceil(crop[2] * scale_x))
        bottom = min(raster.height, math.ceil(crop[3] * scale_y))
        if right <= left or bottom <= top:
            return crop
        region = raster.crop((left, top, right, bottom))
        ink = region.point(lambda value: 255 if value < 235 else 0, mode="1")
        pixels = ink.load()
        row_threshold = max(2, int(ink.width * 0.003))
        active_rows = [
            y for y in range(ink.height)
            if sum(bool(pixels[x, y]) for x in range(ink.width)) >= row_threshold
        ]
        if active_rows:
            groups: list[list[int]] = [[active_rows[0]]]
            gap_limit = max(8, min(14, int(ink.height * 0.06)))
            for y in active_rows[1:]:
                if y - groups[-1][-1] <= gap_limit:
                    groups[-1].append(y)
                else:
                    groups.append([y])
            selected_groups = []
            for group_index, values in enumerate(groups):
                group_top = values[0]
                group_bottom = values[-1] + 1
                group_bbox = ink.crop(
                    (0, group_top, ink.width, group_bottom),
                ).getbbox()
                if (
                    group_bbox is not None
                    and group_bottom - group_top <= 3
                    and group_bbox[2] - group_bbox[0] >= ink.width * 0.7
                ):
                    continue
                next_gap = (
                    groups[group_index + 1][0] - values[-1]
                    if group_index + 1 < len(groups) else 0
                )
                previous_gap = (
                    values[0] - groups[group_index - 1][-1]
                    if group_index > 0 else 0
                )
                absolute_top = crop[1] + group_top / max(scale_y, 1e-9)
                group_height = (group_bottom - group_top) / max(scale_y, 1e-9)
                if (
                    absolute_top < page.height * 0.08
                    and group_height <= 12
                    and next_gap > 15
                ):
                    continue
                if (
                    direction == "below"
                    and absolute_top > page.height * 0.86
                    and group_height <= 14
                    and previous_gap > 18
                    and group_bbox is not None
                    and group_bbox[2] - group_bbox[0] < ink.width * 0.15
                ):
                    continue
                selected_groups.append(values)
            if anchor is not None:
                anchor_top = (anchor[1] - crop[1]) * scale_y
                anchor_bottom = (anchor[3] - crop[1]) * scale_y
                anchored = [
                    values for values in selected_groups
                    if values[-1] + 1 >= anchor_top and values[0] <= anchor_bottom
                ]
                if anchored:
                    selected_groups = anchored
            band_boxes: list[list[float]] = []
            for selected in selected_groups:
                band_top = max(0, selected[0] - 1)
                band_bottom = min(ink.height, selected[-1] + 2)
                band_bounds = ink.crop((0, band_top, ink.width, band_bottom)).getbbox()
                if band_bounds is not None:
                    band_boxes.append([
                        band_bounds[0], band_bounds[1] + band_top,
                        band_bounds[2], band_bounds[3] + band_top,
                    ])
            bounds = tuple(_bbox_union(band_boxes)) if band_boxes else None
        else:
            bounds = None
        if bounds is None:
            return crop
        x0, y0, x1, y1 = bounds
        ink_width = (x1 - x0) / max(scale_x, 1e-9)
        ink_height = (y1 - y0) / max(scale_y, 1e-9)
        if (
            ink_width < (crop[2] - crop[0]) * 0.12
            or ink_height < (crop[3] - crop[1]) * 0.04
        ):
            return crop
        padding = 4.0
        refined = [
            max(crop[0], (left + x0) / scale_x - padding),
            max(crop[1], (top + y0) / scale_y - padding),
            min(crop[2], (left + x1) / scale_x + padding),
            min(crop[3], (top + y1) / scale_y + padding),
        ]
        return refined if _valid_bboxes([refined]) else crop

    @staticmethod
    def _layout_rows(
        values: list[tuple[int, LayoutItem]],
    ) -> list[list[tuple[int, LayoutItem]]]:
        rows: list[list[tuple[int, LayoutItem]]] = []
        for index, item in sorted(
            values, key=lambda value: (
                (value[1].bbox[1] + value[1].bbox[3]) / 2,
                value[1].bbox[0],
            ),
        ):
            center = (item.bbox[1] + item.bbox[3]) / 2
            if rows:
                centers = [
                    (value.bbox[1] + value.bbox[3]) / 2 for _source, value in rows[-1]
                ]
                tolerance = max(
                    5.0,
                    max(value.bbox[3] - value.bbox[1] for _source, value in rows[-1]) * 0.55,
                )
                if abs(center - sum(centers) / len(centers)) <= tolerance:
                    rows[-1].append((index, item))
                    continue
            rows.append([(index, item)])
        return [sorted(row, key=lambda value: value[1].bbox[0]) for row in rows]

    @staticmethod
    def _row_is_section(row: list[tuple[int, LayoutItem]]) -> bool:
        normal = [
            item for _index, item in row
            if item.bbox[2] - item.bbox[0] > 1
        ]
        if not normal:
            return False
        text = " ".join(item.text.strip() for item in normal).strip()
        return bool(
            _SECTION_HEADING.match(text)
            or re.match(r"^(?:[A-Z]\.)?\d+(?:\.\d+)*$", text)
        )

    @staticmethod
    def _row_is_tabular(
        row: list[tuple[int, LayoutItem]],
        column_width: float,
    ) -> bool:
        cells = [
            item for _index, item in row
            if item.bbox[2] - item.bbox[0] > 1
        ]
        if len(cells) < 2:
            return False
        if len(cells) >= 3:
            span = max(item.bbox[2] for item in cells) - min(item.bbox[0] for item in cells)
            broad = sum(
                item.bbox[2] - item.bbox[0] > column_width * 0.3
                for item in cells
            )
            return span >= column_width * 0.55 and broad <= 1
        widths = [item.bbox[2] - item.bbox[0] for item in cells]
        return min(widths) <= column_width * 0.4 and max(widths) <= column_width * 0.8

    def _table_region(
        self,
        page: PageLayout,
        caption: LayoutItem,
        caption_indexes: set[int],
    ) -> tuple[list[float], list[tuple[int, LayoutItem]]]:
        left, right = self._table_column(page, caption)
        column_width = right - left
        sides: list[tuple[float, int, list[tuple[int, LayoutItem]]]] = []
        for direction in (1, -1):
            values: list[tuple[int, LayoutItem]] = []
            for index, item in enumerate(page.text_items):
                if index in caption_indexes:
                    continue
                if not self._bbox_overlaps_column(item.bbox, left, right):
                    continue
                if direction == 1:
                    distance = item.bbox[1] - caption.bbox[3]
                else:
                    distance = caption.bbox[1] - item.bbox[3]
                if -2 <= distance <= page.height * 0.45:
                    values.append((index, item))
            rows = self._layout_rows(values)
            if direction == -1:
                rows.reverse()
            selected: list[list[tuple[int, LayoutItem]]] = []
            pending: list[list[tuple[int, LayoutItem]]] = []
            tabular_rows = 0
            dense_rows = 0
            previous_edge = caption.bbox[3] if direction == 1 else caption.bbox[1]
            for row in rows:
                top = min(item.bbox[1] for _index, item in row)
                bottom = max(item.bbox[3] for _index, item in row)
                gap = top - previous_edge if direction == 1 else previous_edge - bottom
                heights = [item.bbox[3] - item.bbox[1] for _index, item in row]
                continuity_limit = max(7.0, min(16.0, max(heights, default=10.0) * 0.9))
                if (
                    (selected and gap > continuity_limit)
                    or (not selected and gap > 18)
                    or self._row_is_section(row)
                    or any(
                    _FIGURE_CAPTION.match(item.text) or _TABLE_CAPTION.match(item.text)
                    for _index, item in row
                    )
                ):
                    break
                tabular = self._row_is_tabular(row, column_width)
                positive_cells = [
                    item for _index, item in row
                    if item.bbox[2] - item.bbox[0] > 1
                ]
                if dense_rows >= 2 and not tabular:
                    pending.append(row)
                    if len(pending) >= 2:
                        break
                    previous_edge = bottom if direction == 1 else top
                    continue
                if tabular and pending:
                    selected.extend(pending)
                    pending = []
                selected.append(row)
                if tabular:
                    tabular_rows += 1
                if len(positive_cells) >= 3:
                    dense_rows += 1
                previous_edge = bottom if direction == 1 else top
            if tabular_rows < 2 or len(selected) < 2:
                continue
            flat = [value for row in selected for value in row]
            if not flat:
                continue
            nearest = min(
                max(0.0, item.bbox[1] - caption.bbox[3]) if direction == 1
                else max(0.0, caption.bbox[1] - item.bbox[3])
                for _index, item in flat
            )
            sides.append((nearest, tabular_rows + dense_rows * 2, flat))
        if not sides:
            return [], []
        _distance, _rows, selected = min(
            sides, key=lambda value: (-value[1], value[0]),
        )
        boxes = [item.bbox for _index, item in selected]
        if not _valid_bboxes(boxes):
            return [], []
        union = [
            min(bbox[0] for bbox in boxes),
            min(bbox[1] for bbox in boxes),
            max(bbox[2] for bbox in boxes),
            max(bbox[3] for bbox in boxes),
        ]
        direction = 1 if union[1] >= caption.bbox[3] - 2 else -1
        horizontal_padding = 4.0
        vertical_padding = 1.0
        crop = [
            max(0.0, union[0] - horizontal_padding),
            max(caption.bbox[3], union[1] - vertical_padding) if direction == 1
            else max(0.0, union[1] - vertical_padding),
            min(page.width, union[2] + horizontal_padding),
            min(page.height, union[3] + vertical_padding) if direction == 1
            else min(caption.bbox[1], union[3] + vertical_padding),
        ]
        return crop, selected

    def _page_table_rules(self, page: PageLayout) -> list[tuple[float, float, float]]:
        """检测页面中的长横线;仅供文本布局无法确定表格边界时兜底."""
        cached = self._table_rules.get(page.number)
        if cached is not None:
            return cached
        rules: list[tuple[float, float, float]] = []
        image = self._page_visual_raster(page)
        if image is not None:
            width, height = image.size
            pixels = image.load()
            minimum_run = max(40, round(width * 0.12))
            raw: list[tuple[int, int, int]] = []
            for y in range(height):
                row_runs: list[tuple[int, int]] = []
                start: int | None = None
                for x in range(width + 1):
                    dark = x < width and pixels[x, y] < 170
                    if dark and start is None:
                        start = x
                    elif not dark and start is not None:
                        if x - start >= 8:
                            row_runs.append((start, x))
                        start = None
                merged: list[tuple[int, int, int]] = []
                for run_left, run_right in row_runs:
                    if merged and run_left - merged[-1][1] <= 12:
                        left_edge, _right_edge, dark_width = merged[-1]
                        merged[-1] = (
                            left_edge, run_right,
                            dark_width + run_right - run_left,
                        )
                    else:
                        merged.append(
                            (run_left, run_right, run_right - run_left),
                        )
                raw.extend(
                    (y, run_left, run_right)
                    for run_left, run_right, dark_width in merged
                    if run_right - run_left >= minimum_run
                    and dark_width / (run_right - run_left) >= 0.65
                )
            grouped: list[list[tuple[int, int, int]]] = []
            for line in raw:
                if (
                    grouped
                    and line[0] - grouped[-1][-1][0] <= 2
                    and abs(line[1] - grouped[-1][-1][1]) <= 4
                    and abs(line[2] - grouped[-1][-1][2]) <= 4
                ):
                    grouped[-1].append(line)
                else:
                    grouped.append([line])
            scale_x = page.width / max(width, 1)
            scale_y = page.height / max(height, 1)
            rules = [
                (
                    sum(line[0] for line in group) / len(group) * scale_y,
                    min(line[1] for line in group) * scale_x,
                    max(line[2] for line in group) * scale_x,
                )
                for group in grouped
            ]
        self._table_rules[page.number] = rules
        return rules

    def _rule_table_region(
        self,
        page: PageLayout,
        caption: LayoutItem,
        caption_indexes: set[int],
    ) -> tuple[list[float], list[tuple[int, LayoutItem]]]:
        """用标题附近成对横线确定表格边界,避免把相邻正文纳入裁图."""
        left, right = self._table_column(page, caption)
        column_width = right - left
        rules = [
            rule for rule in self._page_table_rules(page)
            if min(rule[2], right) - max(rule[1], left) >= column_width * 0.2
        ]

        def path_is_clear(start: float, end: float) -> bool:
            return not any(
                index not in caption_indexes
                and start < (item.bbox[1] + item.bbox[3]) / 2 < end
                and self._bbox_overlaps_column(item.bbox, left, right)
                and (
                    _FIGURE_CAPTION.match(item.text)
                    or _TABLE_CAPTION.match(item.text)
                    or self._row_is_section([(index, item)])
                )
                for index, item in enumerate(page.text_items)
            )

        candidates: list[tuple[float, float, float, float, float]] = []
        for top_index, top in enumerate(rules):
            for bottom_index, bottom in enumerate(rules[top_index + 1:], top_index + 1):
                height = bottom[0] - top[0]
                if height < 8 or height > page.height * 0.55:
                    continue
                overlap = min(top[2], bottom[2]) - max(top[1], bottom[1])
                shorter = min(top[2] - top[1], bottom[2] - bottom[1])
                endpoint_limit = max(30.0, column_width * 0.12)
                if (
                    shorter <= 0
                    or overlap < shorter * 0.65
                    or abs(top[1] - bottom[1]) > endpoint_limit
                    or abs(top[2] - bottom[2]) > endpoint_limit
                ):
                    continue
                if any(
                    index not in caption_indexes
                    and top[0] < (item.bbox[1] + item.bbox[3]) / 2 < bottom[0]
                    and self._bbox_overlaps_column(item.bbox, left, right)
                    and (
                        _FIGURE_CAPTION.match(item.text)
                        or _TABLE_CAPTION.match(item.text)
                    )
                    for index, item in enumerate(page.text_items)
                ):
                    continue
                after_gap = top[0] - caption.bbox[3]
                before_gap = caption.bbox[1] - bottom[0]
                if 0 <= after_gap and path_is_clear(caption.bbox[3], top[0]):
                    distance = after_gap
                    direction = 1.0
                elif 0 <= before_gap and path_is_clear(bottom[0], caption.bbox[1]):
                    distance = before_gap
                    direction = -1.0
                else:
                    continue
                candidates.append(
                    (distance, -height, direction, top_index, bottom_index),
                )
        if not candidates:
            return [], []
        _distance, _negative_height, direction, top_index, bottom_index = min(candidates)
        top = rules[int(top_index)]
        bottom = rules[int(bottom_index)]
        crop = [
            max(left, min(top[1], bottom[1]) - 2),
            max(0.0, top[0] - 1),
            min(right, max(top[2], bottom[2]) + 2),
            min(page.height, bottom[0] + 1),
        ]
        if direction == 1:
            notes = [
                item.bbox[1] for index, item in enumerate(page.text_items)
                if index not in caption_indexes
                and item.bbox[1] >= crop[3]
                and item.bbox[1] - crop[3] <= page.height * 0.25
                and self._bbox_overlaps_column(item.bbox, left, right)
                and re.match(r"^(?:Notes?|Source)\s*[:.]", item.text.strip(), re.I)
            ]
            if notes:
                crop[3] = min(notes)
        region_items = [
            (index, item) for index, item in enumerate(page.text_items)
            if index not in caption_indexes
            and min(item.bbox[2], crop[2]) - max(item.bbox[0], crop[0]) > 0
            and min(item.bbox[3], crop[3]) - max(item.bbox[1], crop[1]) > 0
        ]
        return crop, region_items

    @staticmethod
    def _rule_region_completes_text_crop(
        text_crop: list[float],
        rule_crop: list[float],
        caption: LayoutItem,
    ) -> bool:
        """横线边界完整包含文本裁图时采用更完整区域;限制扩张避免吞入正文."""
        if not text_crop or not rule_crop:
            return False
        tolerance = 3.0
        contains = (
            rule_crop[0] <= text_crop[0] + tolerance
            and rule_crop[1] <= text_crop[1] + tolerance
            and rule_crop[2] >= text_crop[2] - tolerance
            and rule_crop[3] >= text_crop[3] - tolerance
        )
        text_before = text_crop[3] <= caption.bbox[1] + tolerance
        rule_before = rule_crop[3] <= caption.bbox[1] + tolerance
        text_after = text_crop[1] >= caption.bbox[3] - tolerance
        rule_after = rule_crop[1] >= caption.bbox[3] - tolerance
        if not contains or not (
            (text_before and rule_before) or (text_after and rule_after)
        ):
            return False
        text_area = max(1.0, (text_crop[2] - text_crop[0]) * (text_crop[3] - text_crop[1]))
        rule_area = max(1.0, (rule_crop[2] - rule_crop[0]) * (rule_crop[3] - rule_crop[1]))
        if rule_area > text_area * 4.0:
            return False
        height_gain = (rule_crop[3] - rule_crop[1]) - (text_crop[3] - text_crop[1])
        width_gain = (rule_crop[2] - rule_crop[0]) - (text_crop[2] - text_crop[0])
        return height_gain >= max(6.0, (text_crop[3] - text_crop[1]) * 0.15) or (
            width_gain >= max(12.0, (text_crop[2] - text_crop[0]) * 0.1)
        )

    def _add_pdf_table(
        self,
        page: PageLayout,
        caption_items: list[LayoutItem],
        label_id: str,
        caption_indexes: set[int],
    ) -> set[int]:
        caption = LayoutItem(
            _caption_text(caption_items),
            [
                min(item.bbox[0] for item in caption_items),
                min(item.bbox[1] for item in caption_items),
                max(item.bbox[2] for item in caption_items),
                max(item.bbox[3] for item in caption_items),
            ],
            min(
                (item.confidence for item in caption_items if item.confidence is not None),
                default=None,
            ),
        )
        label = f"Table {label_id}"
        table_id = make_id("tbl", self.fingerprint, page.number, label, caption.bbox)
        detected_region = self._detected_visual_region(page, caption, "table")
        if detected_region:
            crop = detected_region
            region_items = self._items_in_region(page, crop, caption_indexes)
            self._layout_detector_table_matches += 1
        else:
            crop, region_items = self._table_region(page, caption, caption_indexes)
        if crop and not detected_region:
            rule_crop, rule_items = self._rule_table_region(
                page, caption, caption_indexes,
            )
            if self._rule_region_completes_text_crop(crop, rule_crop, caption):
                crop, region_items = rule_crop, rule_items
        if not crop:
            crop, region_items = self._rule_table_region(
                page, caption, caption_indexes,
            )
            if crop:
                self.reasons.append("pdf_table_crop_rule_fallback")
        if not crop:
            self.reasons.append("pdf_table_crop_ambiguous")
        block_id = self._add_block(
            "table", caption.text, page.number, [crop] if crop else [caption.bbox],
            table_id=table_id,
        )
        self._add_block(
            "caption", caption.text, page.number, [caption.bbox], parent_id=block_id,
        )
        cell_candidates = [
            (index, item) for index, item in region_items
            if not _FIGURE_CAPTION.match(item.text)
            and not _TABLE_CAPTION.match(item.text)
        ]
        rows = self._layout_rows(cell_candidates)
        structured_rows = [
            sorted(row, key=lambda value: value[1].bbox[0])
            for row in rows if len(row) >= 2
        ]
        column_counts = [len(row) for row in structured_rows]
        aligned = False
        if structured_rows and len(set(column_counts)) == 1:
            tolerance = max(8.0, (crop[2] - crop[0]) * 0.04) if crop else 8.0
            aligned = all(
                max(row[column][1].bbox[0] for row in structured_rows)
                - min(row[column][1].bbox[0] for row in structured_rows)
                <= tolerance
                for column in range(column_counts[0])
            )
        reliable = (
            len(structured_rows) >= 2
            and min(column_counts, default=0) >= 2
            and max(column_counts, default=0) - min(column_counts, default=0) <= 1
            and aligned
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
            "source_crop": {"page": page.number, "bbox": crop} if crop else None,
            "source_locator": pdf_locator(
                self.fingerprint, page.number, [crop] if crop else [],
            ),
            "caption_locator": pdf_locator(self.fingerprint, page.number, [caption.bbox]),
        })
        return consumed | {index for index, _item in region_items}

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
