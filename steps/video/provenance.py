"""把视频字幕的真实时间段绑定到来源与笔记溯源清单。"""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from shared.note_text import markdown_to_index_text
from shared.provenance import (
    bounded_support_text,
    build_provenance_manifest,
    build_source_manifest,
    extract_exact_quote_markers,
    make_segment_id,
    validate_source_manifest,
    write_provenance_manifest,
    write_source_manifest,
)
from steps.utils.srt_parser import load_srt, pick_native_srt


SOURCE_MANIFEST_PATH = "intermediate/source_segments.json"
_TIMESTAMP_LINE_RE = re.compile(r"^\[(\d+):(\d{2})\]\s+(.+)$")


def build_video_source_manifest(job_dir: Path, entries: Sequence[Any]) -> dict[str, Any] | None:
    """用实际视频字节、字幕范围和 OCR 帧坐标构建来源清单。"""
    media_path = job_dir / "input" / "source.mp4"
    duration_ms = _measured_duration_ms(job_dir)
    if not media_path.is_file() or duration_ms is None:
        return None
    subtitle_path, _ = pick_native_srt(job_dir / "input")
    actual_entries = load_srt(subtitle_path) if subtitle_path is not None else []
    subtitle_sha256 = _sha256_file(subtitle_path) if subtitle_path is not None else None
    subtitle_rel = (
        subtitle_path.relative_to(job_dir).as_posix()
        if subtitle_path is not None else None
    )

    by_id: dict[str, dict[str, Any]] = {}
    for index, entry in enumerate(entries):
        start_ms = _seconds_to_ms(entry.start_sec, "subtitle start")
        end_ms = _seconds_to_ms(entry.end_sec, "subtitle end")
        locator = {"kind": "media", "start_ms": start_ms, "end_ms": end_ms}
        segment_id = make_segment_id(
            "video", start=None, end=None, section="subtitle", locator=locator,
        )
        actual = actual_entries[index] if index < len(actual_entries) else None
        support_text = None
        if (
            actual is not None
            and _seconds_to_ms(actual.start_sec, "support subtitle start") == start_ms
            and _seconds_to_ms(actual.end_sec, "support subtitle end") == end_ms
        ):
            support_text = bounded_support_text(actual.text)
        by_id[segment_id] = {
            "segment_id": segment_id,
            "source_id": "video",
            "start": None,
            "end": None,
            "section": "subtitle",
            "locator": locator,
            "support_text": support_text,
            "support_artifact": ({
                "kind": "video_subtitle",
                "path": subtitle_rel,
                "sha256": subtitle_sha256,
                "selector": {"index": index},
            } if (
                support_text is not None
                and subtitle_rel is not None
                and subtitle_sha256 is not None
            ) else None),
        }

    for segment in video_ocr_source_segments(job_dir, duration_ms=duration_ms):
        by_id[segment["segment_id"]] = segment
    if not by_id:
        return None

    return build_source_manifest(
        job_id=job_dir.name,
        pipeline="video",
        source_artifacts=[{
            "source_id": "video",
            "path": "input/source.mp4",
            "sha256": _sha256_file(media_path),
            "revision": None,
            "media_duration_ms": duration_ms,
            "page_count": None,
        }],
        segments=list(by_id.values()),
    )


def video_ocr_source_segments(
    job_dir: Path, *, duration_ms: int,
) -> list[dict[str, Any]]:
    """从真实 OCR 帧与框生成 image locator;不完整条目不进入来源清单。"""
    ocr_path = job_dir / "intermediate" / "ocr.json"
    if not ocr_path.is_file():
        return []
    entries = json.loads(ocr_path.read_text(encoding="utf-8"))
    if type(entries) is not list:
        raise ValueError("video OCR artifact must be a list")

    result: dict[str, dict[str, Any]] = {}
    ocr_sha256 = _sha256_file(ocr_path)
    asset_states: dict[str, tuple[str, int, int] | None] = {}
    for entry_index, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            continue
        asset = _ocr_asset(job_dir, entry.get("filename"))
        time_range = _ocr_time_range(entry.get("timestamp_sec"), duration_ms)
        boxes = entry.get("boxes")
        recorded_sha256 = entry.get("asset_sha256")
        recorded_size = _ocr_recorded_size(entry)
        if (
            asset is None
            or time_range is None
            or type(boxes) is not list
            or type(recorded_sha256) is not str
            or re.fullmatch(r"[0-9a-f]{64}", recorded_sha256) is None
            or recorded_size is None
        ):
            continue
        asset_path, local_path = asset
        if asset_path not in asset_states:
            try:
                width, height = _image_size(local_path)
                asset_states[asset_path] = (_sha256_file(local_path), width, height)
            except (OSError, ValueError):
                asset_states[asset_path] = None
        asset_state = asset_states[asset_path]
        if (
            asset_state is None
            or asset_state[0] != recorded_sha256
            or asset_state[1:] != recorded_size
        ):
            continue
        asset_sha256, width, height = asset_state
        for box_index, box_entry in enumerate(boxes):
            if not isinstance(box_entry, Mapping):
                continue
            text = box_entry.get("text")
            bbox = _normalize_ocr_bbox(
                box_entry.get("box"), width=width, height=height,
            )
            if type(text) is not str or not text.strip() or bbox is None:
                continue
            locator = {
                "kind": "image",
                "asset_path": asset_path,
                "asset_sha256": asset_sha256,
                "bbox": bbox,
                "start_ms": time_range[0],
                "end_ms": time_range[1],
                "page": None,
            }
            segment_id = make_segment_id(
                "video", start=None, end=None, section="ocr", locator=locator,
            )
            support_text = bounded_support_text(text)
            result[segment_id] = {
                "segment_id": segment_id,
                "source_id": "video",
                "start": None,
                "end": None,
                "section": "ocr",
                "locator": locator,
                "support_text": support_text,
                "support_artifact": ({
                    "kind": "video_ocr",
                    "path": "intermediate/ocr.json",
                    "sha256": ocr_sha256,
                    "selector": {
                        "entry_index": entry_index,
                        "box_index": box_index,
                    },
                } if support_text is not None else None),
            }
    return list(result.values())


def mechanical_ocr_provenance_segments(
    ocr_entries: Sequence[Mapping[str, Any]],
    source_manifest: Mapping[str, Any],
    normalized_body: str,
    *,
    rendered_markdown: str,
) -> list[dict[str, Any]]:
    """只映射最终机械稿中实际出现一次的 OCR 文本。"""
    refs: dict[tuple[str, str], str] = {}
    for segment in source_manifest["segments"]:
        locator = segment["locator"]
        if locator["kind"] == "image":
            refs[(locator["asset_path"], _canonical_bbox(locator["bbox"]))] = (
                segment["segment_id"]
            )

    candidates: dict[str, list[str]] = {}
    for entry in ocr_entries:
        if not isinstance(entry, Mapping):
            continue
        filename = entry.get("filename")
        if type(filename) is not str:
            continue
        asset_path = f"assets/{filename}"
        rendered_blocks = re.findall(
            rf"\]\({re.escape(asset_path)}\)\n\n> OCR：([^\n]+)",
            rendered_markdown,
        )
        if len(rendered_blocks) != 1:
            continue
        rendered_ocr = rendered_blocks[0]
        boxes = entry.get("boxes")
        if type(boxes) is not list:
            continue
        for box_entry in boxes:
            if not isinstance(box_entry, Mapping):
                continue
            text = box_entry.get("text")
            bbox = _normalize_ocr_bbox(box_entry.get("box"))
            if type(text) is not str or bbox is None:
                continue
            anchor = " ".join(text.split())
            ref = refs.get((asset_path, _canonical_bbox(bbox)))
            if (
                not anchor
                or anchor not in rendered_ocr
                or ref is None
                or normalized_body.count(anchor) != 1
            ):
                continue
            current = candidates.setdefault(anchor, [])
            if ref not in current:
                current.append(ref)
    return [{
        "anchor": anchor,
        "prefix": "",
        "suffix": "",
        "section": "ocr",
        "source_segment_ids": refs,
    } for anchor, refs in candidates.items()]


def write_video_source_manifest(job_dir: Path, manifest: Mapping[str, Any]) -> str:
    return write_source_manifest(
        job_dir / SOURCE_MANIFEST_PATH, manifest, trusted_root=job_dir,
    )


def load_video_source_manifest(job_dir: Path) -> dict[str, Any] | None:
    path = job_dir / SOURCE_MANIFEST_PATH
    if not path.is_file():
        return None
    value = validate_source_manifest(json.loads(path.read_text(encoding="utf-8")))
    if value["job_id"] != job_dir.name or value["pipeline"] != "video":
        raise ValueError("video source manifest belongs to another job or pipeline")
    return value


def ensure_video_source_manifest(job_dir: Path) -> dict[str, Any] | None:
    existing = load_video_source_manifest(job_dir)
    if existing is not None:
        return existing
    subtitle, _ = pick_native_srt(job_dir / "input")
    entries = load_srt(subtitle) if subtitle else []
    manifest = build_video_source_manifest(job_dir, entries)
    if manifest is not None:
        write_video_source_manifest(job_dir, manifest)
    return manifest


def transcript_provenance_segments(
    cue_lines: Sequence[str],
    source_manifest: Mapping[str, Any],
    normalized_body: str,
) -> list[dict[str, Any]]:
    refs_by_second = _refs_by_start_second(source_manifest)
    result: list[dict[str, Any]] = []
    for line in cue_lines:
        match = _TIMESTAMP_LINE_RE.fullmatch(line.strip())
        if match is None:
            continue
        second = int(match.group(1)) * 60 + int(match.group(2))
        refs = refs_by_second.get(second)
        if not refs:
            raise ValueError("video transcript contains a timestamp outside source subtitles")
        anchor = markdown_to_index_text(line.strip()).strip()
        if normalized_body.count(anchor) != 1:
            continue
        result.append({
            "anchor": anchor,
            "prefix": "",
            "suffix": "",
            "section": "transcript",
            "source_segment_ids": refs,
        })
    return result


def mechanical_provenance_segments(
    transcript_lines: Sequence[Mapping[str, Any]],
    source_manifest: Mapping[str, Any],
    normalized_body: str,
    *,
    beat_sec: int,
) -> list[dict[str, Any]]:
    """只为机械稿实际纳入的口播时间节生成映射。"""
    refs_by_second = _refs_by_start_second(source_manifest)
    refs_by_beat: dict[int, list[str]] = {}
    for item in transcript_lines:
        second = item.get("time_sec")
        if type(second) not in {int, float} or second < 0:
            continue
        refs = refs_by_second.get(int(second), [])
        beat = int(second // beat_sec)
        current = refs_by_beat.setdefault(beat, [])
        for ref in refs:
            if ref not in current:
                current.append(ref)

    result: list[dict[str, Any]] = []
    for beat, refs in sorted(refs_by_beat.items()):
        if not refs:
            continue
        total_seconds = beat * beat_sec
        anchor = f"## [{total_seconds // 60:02d}:{total_seconds % 60:02d}]"
        if normalized_body.count(anchor) != 1:
            continue
        result.append({
            "anchor": anchor,
            "prefix": "",
            "suffix": "",
            "section": f"beat-{beat}",
            "source_segment_ids": refs,
        })
    return result


def smart_reference_block(
    job_dir: Path,
    source_manifest: Mapping[str, Any] | None,
) -> str:
    if source_manifest is None:
        return ""
    subtitle, _ = pick_native_srt(job_dir / "input")
    if subtitle is None:
        return ""
    refs = _refs_by_range(source_manifest)
    lines = [
        "\n--- 带来源标记的字幕 ---\n",
        "引用字幕事实时,在相关句末原样附一个或多个 [[source:token]]。"
        "标记不得改写、编造或重复;它们落盘前会被移除。\n",
    ]
    for entry in load_srt(subtitle):
        key = (
            _seconds_to_ms(entry.start_sec, "subtitle start"),
            _seconds_to_ms(entry.end_sec, "subtitle end"),
        )
        segment_id = refs.get(key)
        if segment_id:
            lines.append(f"[[source:{_source_token(segment_id)}]] {entry.text.strip()}\n")
    return "".join(lines) if len(lines) > 2 else ""


def extract_smart_markers(
    marked_text: str,
    source_manifest: Mapping[str, Any],
) -> tuple[str, list[dict[str, Any]]]:
    """校验并移除中间 marker;只发布来源 support_text 的逐字 claim。"""
    return extract_exact_quote_markers(
        marked_text, source_manifest, error_prefix="video smart note",
    )


def smart_provenance_segments(
    normalized_body: str,
    candidates: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """只保留在最终净化笔记中仍唯一存在的 exact-quote claim。"""
    mappings = [dict(item) for item in candidates]
    if any(normalized_body.count(item["anchor"]) != 1 for item in mappings):
        return []
    return mappings


def persist_video_note_provenance(
    job_dir: Path,
    *,
    note_type: str,
    note_artifact: str,
    provenance_segments: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    target = job_dir / "output" / "provenance" / f"{note_type}.json"
    source_manifest = load_video_source_manifest(job_dir)
    if source_manifest is None:
        target.unlink(missing_ok=True)
        return {"status": "legacy_no_source_manifest", "segments": 0}

    note_path = job_dir / note_artifact
    note_bytes = note_path.read_bytes()
    normalized_body = markdown_to_index_text(note_bytes.decode("utf-8"))
    manifest = build_provenance_manifest(
        job_id=job_dir.name,
        note_type=note_type,
        note_artifact=note_artifact,
        note_bytes=note_bytes,
        normalized_body=normalized_body,
        source_manifest_path=SOURCE_MANIFEST_PATH,
        source_manifest=source_manifest,
        segments=provenance_segments,
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
        "status": "written" if provenance_segments else "written_empty",
        "segments": len(provenance_segments),
    }


def _refs_by_range(source_manifest: Mapping[str, Any]) -> dict[tuple[int, int], str]:
    refs: dict[tuple[int, int], str] = {}
    for item in source_manifest["segments"]:
        locator = item["locator"]
        if locator["kind"] != "media":
            continue
        key = (locator["start_ms"], locator["end_ms"])
        if key in refs:
            raise ValueError("video source manifest has duplicate media ranges")
        refs[key] = item["segment_id"]
    return refs


def _refs_by_start_second(source_manifest: Mapping[str, Any]) -> dict[int, list[str]]:
    refs: dict[int, list[str]] = {}
    for item in source_manifest["segments"]:
        locator = item["locator"]
        if locator["kind"] == "media":
            refs.setdefault(locator["start_ms"] // 1000, []).append(item["segment_id"])
    return refs


def _measured_duration_ms(job_dir: Path) -> int | None:
    path = job_dir / "input" / "metadata.json"
    if not path.is_file():
        return None
    value = json.loads(path.read_text(encoding="utf-8")).get("duration_sec")
    if type(value) not in {int, float} or value <= 0:
        raise ValueError("video metadata duration_sec must be measured and positive")
    return round(value * 1000)


def _seconds_to_ms(value: Any, field: str) -> int:
    if type(value) not in {int, float} or value < 0:
        raise ValueError(f"{field} must be a non-negative number")
    return round(value * 1000)


def _ocr_asset(job_dir: Path, filename: Any) -> tuple[str, Path] | None:
    if type(filename) is not str or not filename or "/" in filename or "\\" in filename:
        return None
    assets_dir = (job_dir / "assets").resolve()
    path = (assets_dir / filename).resolve()
    try:
        path.relative_to(assets_dir)
    except ValueError:
        return None
    if not path.is_file():
        return None
    return f"assets/{filename}", path


def _ocr_time_range(value: Any, duration_ms: int) -> tuple[int, int] | None:
    if type(value) not in {int, float} or not math.isfinite(value) or value < 0:
        return None
    start_ms = int(value * 1000)
    if start_ms >= duration_ms:
        return None
    return start_ms, start_ms + 1


def _ocr_recorded_size(entry: Mapping[str, Any]) -> tuple[int, int] | None:
    width, height = entry.get("width"), entry.get("height")
    if type(width) is not int or type(height) is not int or width <= 0 or height <= 0:
        return None
    return width, height


def _image_size(path: Path) -> tuple[int, int]:
    from PIL import Image

    with Image.open(path) as image:
        width, height = image.size
    if (
        type(width) is not int
        or type(height) is not int
        or width <= 0
        or height <= 0
    ):
        raise ValueError("OCR frame dimensions must be positive integers")
    return width, height


def _normalize_ocr_bbox(
    value: Any,
    *,
    width: int | None = None,
    height: int | None = None,
) -> list[int | float] | None:
    coordinates: list[Any]
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
    if any(
        type(item) not in {int, float} or not math.isfinite(item) or item < 0
        for item in coordinates
    ):
        return None
    normalized = [int(item) if float(item).is_integer() else round(float(item), 6)
                  for item in coordinates]
    if normalized[2] <= normalized[0] or normalized[3] <= normalized[1]:
        return None
    if width is not None and normalized[2] > width:
        return None
    if height is not None and normalized[3] > height:
        return None
    return normalized


def _canonical_bbox(value: Sequence[int | float]) -> str:
    return json.dumps(list(value), separators=(",", ":"), allow_nan=False)


def _source_token(segment_id: str) -> str:
    return segment_id.removeprefix("seg_")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
