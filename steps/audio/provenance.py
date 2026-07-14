"""把音频转写的真实时间段绑定到来源与笔记溯源清单。"""

from __future__ import annotations

import hashlib
import json
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


SOURCE_MANIFEST_PATH = "intermediate/source_segments.json"


def build_audio_source_manifest(
    job_dir: Path,
    transcript_segments: Sequence[Mapping[str, Any]],
) -> dict[str, Any] | None:
    """用下载阶段测得的媒体时长和真实转写时间段构建来源清单。"""
    if not transcript_segments:
        return None
    media_path = _audio_media_path(job_dir)
    duration_ms = _measured_duration_ms(job_dir)
    if media_path is None or duration_ms is None:
        return None
    support_path = job_dir / "intermediate" / "segments.json"
    support_segments: list[Any] = []
    support_sha256: str | None = None
    if support_path.is_file():
        try:
            loaded = json.loads(support_path.read_text(encoding="utf-8"))
            if type(loaded) is list:
                support_segments = loaded
                support_sha256 = _sha256_file(support_path)
        except (OSError, UnicodeError, json.JSONDecodeError):
            pass

    source_segments: list[dict[str, Any]] = []
    for index, item in enumerate(transcript_segments):
        start_ms = _seconds_to_ms(item.get("start"), "transcript start")
        end_ms = _seconds_to_ms(item.get("end"), "transcript end")
        locator = {"kind": "media", "start_ms": start_ms, "end_ms": end_ms}
        raw = {
            "source_id": "audio",
            "start": None,
            "end": None,
            "section": "transcript",
            "locator": locator,
        }
        actual = support_segments[index] if index < len(support_segments) else None
        support_text = None
        if isinstance(actual, Mapping):
            try:
                actual_range = (
                    _seconds_to_ms(actual.get("start"), "support start"),
                    _seconds_to_ms(actual.get("end"), "support end"),
                )
            except ValueError:
                actual_range = None
            if actual_range == (start_ms, end_ms):
                support_text = bounded_support_text(actual.get("text"))
        source_segments.append({
            "segment_id": make_segment_id(
                "audio", start=None, end=None, section="transcript", locator=locator,
            ),
            **raw,
            "support_text": support_text,
            "support_artifact": ({
                "kind": "audio_segments",
                "path": "intermediate/segments.json",
                "sha256": support_sha256,
                "selector": {"index": index},
            } if support_text is not None and support_sha256 is not None else None),
        })

    return build_source_manifest(
        job_id=job_dir.name,
        pipeline="audio",
        source_artifacts=[{
            "source_id": "audio",
            "path": media_path.relative_to(job_dir).as_posix(),
            "sha256": _sha256_file(media_path),
            "revision": None,
            "media_duration_ms": duration_ms,
            "page_count": None,
        }],
        segments=source_segments,
    )


def write_audio_source_manifest(job_dir: Path, manifest: Mapping[str, Any]) -> str:
    return write_source_manifest(
        job_dir / SOURCE_MANIFEST_PATH,
        manifest,
        trusted_root=job_dir,
    )


def load_audio_source_manifest(job_dir: Path) -> dict[str, Any] | None:
    path = job_dir / SOURCE_MANIFEST_PATH
    if not path.is_file():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    validated = validate_source_manifest(value)
    if validated["job_id"] != job_dir.name or validated["pipeline"] != "audio":
        raise ValueError("audio source manifest belongs to another job or pipeline")
    return validated


def transcript_provenance_segments(
    transcript_segments: Sequence[Mapping[str, Any]],
    source_manifest: Mapping[str, Any],
    normalized_body: str,
) -> list[dict[str, Any]]:
    """按同一转写时间范围绑定逐字稿行,不按段序号猜测。"""
    refs = _refs_by_range(source_manifest)
    result: list[dict[str, Any]] = []
    from steps.utils.srt_parser import format_timestamp

    for item in transcript_segments:
        start_ms = _seconds_to_ms(item.get("start"), "transcript start")
        end_ms = _seconds_to_ms(item.get("end"), "transcript end")
        segment_id = refs.get((start_ms, end_ms))
        text = item.get("text")
        if segment_id is None or type(text) is not str or not text.strip():
            continue
        anchor = f"{format_timestamp(start_ms / 1000)} {text}"
        if normalized_body.count(anchor) != 1:
            raise ValueError("audio transcript anchor is missing or ambiguous")
        result.append({
            "anchor": anchor,
            "prefix": "",
            "suffix": "",
            "section": "transcript",
            "source_segment_ids": [segment_id],
        })
    return result


def smart_reference_block(
    transcript: Mapping[str, Any],
    source_manifest: Mapping[str, Any] | None,
) -> str:
    """给 AI 注入只能原样复制的来源标记,标记直接绑定已校验时间段。"""
    if source_manifest is None:
        return ""
    refs = _refs_by_range(source_manifest)
    lines = [
        "\n--- 带来源标记的转写正文 ---\n",
        "每个事实只能引用下列转写段。引用时在相关句末原样保留对应 "
        "[[source:segment_id]] 标记;不得改写、编造或重复同一标记。\n",
    ]
    for item in transcript.get("segments") or []:
        if not isinstance(item, Mapping):
            continue
        try:
            key = (
                _seconds_to_ms(item.get("start"), "transcript start"),
                _seconds_to_ms(item.get("end"), "transcript end"),
            )
        except ValueError:
            continue
        segment_id = refs.get(key)
        text = item.get("text")
        if segment_id and type(text) is str and text.strip():
            lines.append(f"[[source:{_source_token(segment_id)}]] {text.strip()}\n")
    return "".join(lines) if len(lines) > 2 else ""


def extract_smart_markers(
    marked_text: str,
    source_manifest: Mapping[str, Any],
) -> tuple[str, list[dict[str, Any]]]:
    """校验并移除中间 marker;只发布来源 support_text 的逐字 claim。"""
    return extract_exact_quote_markers(
        marked_text, source_manifest, error_prefix="audio smart note",
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


def persist_audio_note_provenance(
    job_dir: Path,
    *,
    note_type: str,
    note_artifact: str,
    provenance_segments: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """绑定最终原始字节;无可靠映射时删除旧清单并显式返回零。"""
    target = job_dir / "output" / "provenance" / f"{note_type}.json"
    source_manifest = load_audio_source_manifest(job_dir)
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
            raise ValueError("audio source manifest has duplicate media ranges")
        refs[key] = item["segment_id"]
    return refs


def _audio_media_path(job_dir: Path) -> Path | None:
    for suffix in (".mp3", ".m4a", ".wav", ".aac", ".flac", ".mp4"):
        candidate = job_dir / "input" / f"source{suffix}"
        if candidate.is_file():
            return candidate
    return None


def _measured_duration_ms(job_dir: Path) -> int | None:
    path = job_dir / "input" / "metadata.json"
    if not path.is_file():
        return None
    value = json.loads(path.read_text(encoding="utf-8")).get("duration_sec")
    if type(value) not in {int, float} or value <= 0:
        raise ValueError("audio metadata duration_sec must be measured and positive")
    return round(value * 1000)


def _seconds_to_ms(value: Any, field: str) -> int:
    if type(value) not in {int, float} or value < 0:
        raise ValueError(f"{field} must be a non-negative number")
    return round(value * 1000)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_token(segment_id: str) -> str:
    """Markdown 归一化会移除下划线,marker 只携带稳定十六进制主体。"""
    return segment_id.removeprefix("seg_")
