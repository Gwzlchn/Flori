"""从受哈希保护的 producer 产物复算来源支持文本。"""

from __future__ import annotations

import html
import json
import math
import re
from collections.abc import Mapping
from typing import Any

from shared.provenance import bounded_support_text


MAX_SUPPORT_ARTIFACT_BYTES = 8 * 1024 * 1024
_SRT_TIMESTAMP_RE = re.compile(
    r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})"
    r"\s*-->\s*"
    r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})"
)


def support_text_from_artifact(
    data: bytes,
    support_artifact: Mapping[str, Any],
    segment: Mapping[str, Any],
    source_artifact: Mapping[str, Any],
) -> str | None:
    """按 support selector 从真实产物取文本;坐标或内容不一致直接拒绝。"""
    if type(data) is not bytes or len(data) > MAX_SUPPORT_ARTIFACT_BYTES:
        raise ValueError("support artifact is missing or exceeds size limit")
    kind = support_artifact.get("kind")
    selector = support_artifact.get("selector")
    if not isinstance(selector, Mapping):
        raise ValueError("support selector is invalid")
    if kind == "html":
        return _html_support(data, selector, segment)
    if kind == "audio_segments":
        return _audio_support(data, selector, segment)
    if kind == "video_subtitle":
        return _subtitle_support(data, selector, segment)
    if kind == "video_ocr":
        return _ocr_support(data, selector, segment)
    if kind == "pdf_pages":
        return _pdf_support(data, selector, segment, source_artifact)
    raise ValueError("support artifact kind is unsupported")


def _decode_utf8(data: bytes, label: str) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{label} is not UTF-8") from exc


def _load_json(data: bytes, label: str) -> Any:
    try:
        return json.loads(_decode_utf8(data, label))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} is invalid JSON") from exc


def _html_support(
    data: bytes,
    selector: Mapping[str, Any],
    segment: Mapping[str, Any],
) -> str | None:
    source = _decode_utf8(data, "HTML support artifact")
    start, end = selector.get("start"), selector.get("end")
    locator = segment.get("locator")
    if (
        type(start) is not int
        or type(end) is not int
        or not isinstance(locator, Mapping)
        or locator.get("kind") != "text"
        or not 0 <= start < end <= len(source)
        or source[start:end] != locator.get("exact")
    ):
        raise ValueError("HTML support selector does not match source text")
    return bounded_support_text(html.unescape(str(locator["exact"])))


def _audio_support(
    data: bytes,
    selector: Mapping[str, Any],
    segment: Mapping[str, Any],
) -> str | None:
    items = _load_json(data, "audio segments support artifact")
    index = selector.get("index")
    if type(items) is not list or type(index) is not int or not 0 <= index < len(items):
        raise ValueError("audio support selector is outside segments")
    item = items[index]
    locator = segment.get("locator")
    if not isinstance(item, Mapping) or not isinstance(locator, Mapping):
        raise ValueError("audio support entry is invalid")
    if (
        _seconds_to_ms(item.get("start")) != locator.get("start_ms")
        or _seconds_to_ms(item.get("end")) != locator.get("end_ms")
    ):
        raise ValueError("audio support range does not match locator")
    return bounded_support_text(item.get("text"))


def _subtitle_support(
    data: bytes,
    selector: Mapping[str, Any],
    segment: Mapping[str, Any],
) -> str | None:
    entries = _parse_srt(_decode_utf8(data, "video subtitle support artifact"))
    index = selector.get("index")
    if type(index) is not int or not 0 <= index < len(entries):
        raise ValueError("subtitle support selector is outside entries")
    entry = entries[index]
    locator = segment.get("locator")
    if (
        not isinstance(locator, Mapping)
        or entry[0] != locator.get("start_ms")
        or entry[1] != locator.get("end_ms")
    ):
        raise ValueError("subtitle support range does not match locator")
    return bounded_support_text(entry[2])


def _ocr_support(
    data: bytes,
    selector: Mapping[str, Any],
    segment: Mapping[str, Any],
) -> str | None:
    entries = _load_json(data, "video OCR support artifact")
    entry_index, box_index = selector.get("entry_index"), selector.get("box_index")
    if (
        type(entries) is not list
        or type(entry_index) is not int
        or not 0 <= entry_index < len(entries)
    ):
        raise ValueError("OCR support entry selector is invalid")
    entry = entries[entry_index]
    boxes = entry.get("boxes") if isinstance(entry, Mapping) else None
    if type(boxes) is not list or type(box_index) is not int or not 0 <= box_index < len(boxes):
        raise ValueError("OCR support box selector is invalid")
    box = boxes[box_index]
    locator = segment.get("locator")
    filename = entry.get("filename") if isinstance(entry, Mapping) else None
    timestamp = entry.get("timestamp_sec") if isinstance(entry, Mapping) else None
    if (
        not isinstance(box, Mapping)
        or not isinstance(locator, Mapping)
        or type(filename) is not str
        or locator.get("asset_path") != f"assets/{filename}"
        or _seconds_to_ms(timestamp) != locator.get("start_ms")
        or locator.get("end_ms") != locator.get("start_ms") + 1
        or entry.get("asset_sha256") != locator.get("asset_sha256")
        or _normalize_bbox(box.get("box")) != locator.get("bbox")
    ):
        raise ValueError("OCR support entry does not match image locator")
    return bounded_support_text(box.get("text"))


def _pdf_support(
    data: bytes,
    selector: Mapping[str, Any],
    segment: Mapping[str, Any],
    source_artifact: Mapping[str, Any],
) -> str | None:
    value = _load_json(data, "PDF page support artifact")
    if not isinstance(value, Mapping) or set(value) != {
        "schema_version", "source_sha256", "pages",
    }:
        raise ValueError("PDF page support artifact fields are invalid")
    pages = value.get("pages")
    page = selector.get("page")
    locator = segment.get("locator")
    if (
        value.get("schema_version") != 1
        or value.get("source_sha256") != source_artifact.get("sha256")
        or type(pages) is not list
        or len(pages) != source_artifact.get("page_count")
        or type(page) is not int
        or page <= 0
        or page > len(pages)
        or not isinstance(locator, Mapping)
        or locator.get("page") != page
    ):
        raise ValueError("PDF page support identity does not match source")
    item = pages[page - 1]
    if not isinstance(item, Mapping) or set(item) != {"page", "support_text"}:
        raise ValueError("PDF page support entry is invalid")
    if item.get("page") != page:
        raise ValueError("PDF page support entry is out of order")
    return bounded_support_text(item.get("support_text"))


def _seconds_to_ms(value: Any) -> int:
    if type(value) not in {int, float} or not math.isfinite(value) or value < 0:
        raise ValueError("support time must be a finite non-negative number")
    return round(value * 1000)


def _parse_srt(text: str) -> list[tuple[int, int, str]]:
    entries: list[tuple[int, int, str]] = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = block.strip().splitlines()
        if len(lines) < 3:
            continue
        try:
            int(lines[0].strip())
        except ValueError:
            continue
        match = _SRT_TIMESTAMP_RE.search(lines[1])
        if match is None:
            continue
        start_ms = _timestamp_ms(match.groups()[:4])
        end_ms = _timestamp_ms(match.groups()[4:])
        body = "\n".join(lines[2:]).strip()
        if body:
            entries.append((start_ms, end_ms, body))
    return entries


def _timestamp_ms(parts: tuple[str, ...]) -> int:
    hours, minutes, seconds, milliseconds = (int(value) for value in parts)
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + milliseconds


def _normalize_bbox(value: Any) -> list[int | float] | None:
    if type(value) is list and len(value) == 4 and all(
        type(item) in {int, float} for item in value
    ):
        coordinates = value
    elif type(value) is list and len(value) >= 2 and all(
        type(point) is list
        and len(point) == 2
        and all(type(item) in {int, float} for item in point)
        for point in value
    ):
        coordinates = [
            min(point[0] for point in value),
            min(point[1] for point in value),
            max(point[0] for point in value),
            max(point[1] for point in value),
        ]
    else:
        return None
    if any(not math.isfinite(item) or item < 0 for item in coordinates):
        return None
    normalized = [
        int(item) if float(item).is_integer() else round(float(item), 6)
        for item in coordinates
    ]
    if normalized[2] <= normalized[0] or normalized[3] <= normalized[1]:
        return None
    return normalized
