"""Step 09: 按不可变 Part 顺序汇总视频 map 产物。"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from shared.note_text import markdown_to_index_text
from shared.step_base import StepBase, file_hash
from steps.video.provenance import (
    ensure_video_source_manifest,
    load_video_source_manifest,
    persist_video_note_provenance,
    write_video_source_manifest,
)


_CUE_RE = re.compile(r"^\[(\d+):(\d{2})\]\s*(.*)$")


class MergePartsStep(StepBase):
    def _parts(self) -> list[dict]:
        manifest = json.loads((self.job_dir / "job.json").read_text(encoding="utf-8"))
        parts = manifest.get("parts")
        if not isinstance(parts, list) or not parts:
            raise ValueError("video job manifest must contain ordered parts")
        ordered = sorted(parts, key=lambda item: item["part_index"])
        indexes = [item["part_index"] for item in ordered]
        if indexes != list(range(1, len(ordered) + 1)):
            raise ValueError("video job parts must be contiguous")
        return ordered

    def validate_inputs(self) -> list[str]:
        if not (self.job_dir / "job.json").is_file():
            return ["job.json"]
        return []

    def input_hashes(self) -> dict[str, str]:
        hashes = {"manifest": file_hash(self.job_dir / "job.json")}
        for part in self._parts():
            root = self.job_dir / "parts" / part["part_id"]
            for rel in (
                "output/transcript.md",
                "intermediate/source_segments.json",
                "intermediate/dedup.json",
                "intermediate/ocr.json",
                "intermediate/danmaku.json",
                "input/metadata.json",
            ):
                path = root / rel
                if path.is_file():
                    hashes[f"{part['part_id']}:{rel}"] = file_hash(path)
        return hashes

    def execute(self) -> dict | None:
        transcript_sections: list[str] = []
        transcript_mappings: list[tuple[str, list[str]]] = []
        source_artifacts: list[dict] = []
        source_segments: list[dict] = []
        merged_dedup: list[dict] = []
        merged_ocr: list[dict] = []
        merged_danmaku: list[dict] = []
        assets_dir = self.job_dir / "assets"
        assets_dir.mkdir(parents=True, exist_ok=True)
        offset_ms = 0
        global_index = 0

        parts = self._parts()
        root_manifest = json.loads(
            (self.job_dir / "job.json").read_text(encoding="utf-8")
        )
        job_id = root_manifest.get("id") or root_manifest.get("job_id") or self.job_dir.name
        if type(job_id) is not str or not job_id:
            raise ValueError("video job identity is missing")
        for position, part in enumerate(parts, start=1):
            part_id = part["part_id"]
            part_index = part["part_index"]
            root = self.job_dir / "parts" / part_id
            title = (part.get("title") or f"P{part_index:02d}").strip()
            transcript_path = root / "output" / "transcript.md"
            transcript = (
                transcript_path.read_text(encoding="utf-8")
                if transcript_path.is_file() else ""
            )
            transformed_lines: list[str] = []
            local_cues: list[tuple[int, str]] = []
            for line in transcript.splitlines():
                match = _CUE_RE.fullmatch(line.strip())
                if match is None:
                    continue
                local_sec = int(match.group(1)) * 60 + int(match.group(2))
                transformed = f"{self._timestamp(offset_ms // 1000 + local_sec)} {match.group(3)}".rstrip()
                transformed_lines.append(transformed)
                local_cues.append((local_sec, transformed))
            if transformed_lines:
                transcript_sections.append(
                    f"## P{part_index:02d} {title}\n\n" + "\n\n".join(transformed_lines)
                )

            source_manifest = load_video_source_manifest(root)
            if source_manifest is None:
                source_manifest = ensure_video_source_manifest(
                    root, job_id=job_id, part_id=part_id,
                )
            if source_manifest is None:
                raise ValueError(f"part source manifest missing: {part_id}")
            refs_by_second: dict[int, list[str]] = {}
            for artifact in source_manifest["source_artifacts"]:
                item = dict(artifact)
                item["path"] = f"parts/{part_id}/{artifact['path']}"
                source_artifacts.append(item)
            for segment in source_manifest["segments"]:
                item = json.loads(json.dumps(segment, ensure_ascii=False))
                locator = item["locator"]
                if locator["kind"] == "media":
                    locator["part_id"] = part_id
                    locator["timeline_start_ms"] = offset_ms + locator["start_ms"]
                    locator["timeline_end_ms"] = offset_ms + locator["end_ms"]
                    refs_by_second.setdefault(locator["start_ms"] // 1000, []).append(
                        item["segment_id"]
                    )
                elif locator["kind"] == "image":
                    locator["asset_path"] = f"parts/{part_id}/{locator['asset_path']}"
                support = item.get("support_artifact")
                if support is not None:
                    support["path"] = f"parts/{part_id}/{support['path']}"
                source_segments.append(item)
            for local_sec, line in local_cues:
                refs = refs_by_second.get(local_sec)
                if refs:
                    transcript_mappings.append((line, refs))

            dedup = self._load_list(root / "intermediate" / "dedup.json")
            ocr_by_index = {
                item.get("index"): item
                for item in self._load_list(root / "intermediate" / "ocr.json")
            }
            for frame in dedup:
                global_index += 1
                old_index = frame.get("index")
                old_name = self._asset_name(frame.get("filename"))
                new_name = f"P{part_index:02d}_{old_name}"
                source_asset = root / "assets" / old_name
                if source_asset.is_file():
                    shutil.copy2(source_asset, assets_dir / new_name)
                local_time = float(frame.get("timestamp_sec") or 0)
                merged_dedup.append({
                    **frame,
                    "index": global_index,
                    "filename": new_name,
                    "timestamp_sec": offset_ms / 1000 + local_time,
                    "part_id": part_id,
                    "part_index": part_index,
                    "part_filename": old_name,
                    "part_timestamp_sec": local_time,
                })
                ocr = ocr_by_index.get(old_index)
                if ocr is not None:
                    merged_ocr.append({
                        **ocr,
                        "index": global_index,
                        "filename": new_name,
                        "timestamp_sec": offset_ms / 1000 + local_time,
                        "part_id": part_id,
                        "part_index": part_index,
                        "part_filename": old_name,
                        "part_timestamp_sec": local_time,
                    })
            for item in self._load_list(root / "intermediate" / "danmaku.json"):
                local_time = float(item.get("time_sec") or 0)
                merged_danmaku.append({
                    **item,
                    "time_sec": offset_ms / 1000 + local_time,
                    "part_id": part_id,
                    "part_index": part_index,
                    "part_time_sec": local_time,
                })

            offset_ms += self._duration_ms(root, source_manifest)
            self.progress.report(position, len(parts), f"merging P{part_index:02d}")

        transcript = (
            "\n\n".join(transcript_sections).strip() + "\n"
            if transcript_sections else "# 逐字稿\n\n（无口播稿）\n"
        )
        self.artifacts.write("output/transcript.md", transcript)
        self.artifacts.write("intermediate/dedup.json", merged_dedup)
        self.artifacts.write("intermediate/ocr.json", merged_ocr)
        self.artifacts.write("intermediate/danmaku.json", merged_danmaku)
        manifest = {
            "schema_version": source_manifest["schema_version"],
            "job_id": source_manifest["job_id"],
            "pipeline": "video",
            "source_artifacts": source_artifacts,
            "segments": source_segments,
        }
        write_video_source_manifest(self.job_dir, manifest)
        normalized = markdown_to_index_text(transcript)
        mappings = [
            {
                "anchor": markdown_to_index_text(line).strip(),
                "prefix": "",
                "suffix": "",
                "section": "transcript",
                "source_segment_ids": refs,
            }
            for line, refs in transcript_mappings
            if normalized.count(markdown_to_index_text(line).strip()) == 1
        ]
        provenance = persist_video_note_provenance(
            self.job_dir,
            note_type="transcript",
            note_artifact="output/transcript.md",
            provenance_segments=mappings,
        )
        return {
            "parts": len(parts),
            "duration_ms": offset_ms,
            "source_segments": len(source_segments),
            "provenance_segments": provenance["segments"],
        }

    @staticmethod
    def _load_list(path: Path) -> list[dict]:
        if not path.is_file():
            return []
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, list):
            raise ValueError(f"expected list artifact: {path}")
        return value

    @staticmethod
    def _asset_name(value: object) -> str:
        if type(value) is not str or not value or Path(value).name != value:
            raise ValueError("part asset filename is invalid")
        return value

    @staticmethod
    def _duration_ms(root: Path, source_manifest: dict) -> int:
        metadata = root / "input" / "metadata.json"
        if metadata.is_file():
            duration = json.loads(metadata.read_text(encoding="utf-8")).get("duration_sec")
            if isinstance(duration, (int, float)) and duration > 0:
                return round(duration * 1000)
        durations = [
            item.get("media_duration_ms")
            for item in source_manifest["source_artifacts"]
            if item.get("media_duration_ms")
        ]
        if not durations:
            raise ValueError("part media duration is missing")
        return max(durations)

    @staticmethod
    def _timestamp(total_seconds: int) -> str:
        minutes, seconds = divmod(total_seconds, 60)
        return f"[{minutes:02d}:{seconds:02d}]"


if __name__ == "__main__":
    MergePartsStep.cli_main("09_merge_parts")
