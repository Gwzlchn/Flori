"""Step 03: 播客转写解析。02_whisper 的 SRT → 按时间间隔聚合段落。"""

from __future__ import annotations

from shared.note_text import markdown_to_index_text
from shared.step_base import StepBase, file_hash
from steps.audio.provenance import (
    SOURCE_MANIFEST_PATH,
    build_audio_source_manifest,
    persist_audio_note_provenance,
    transcript_provenance_segments,
    write_audio_source_manifest,
)
from steps.utils.srt_parser import format_timestamp, load_srt

# 段落聚合时间间隔(秒)
SEGMENT_INTERVAL_SEC = 60


def _is_word_char(ch: str) -> bool:
    return ch.isascii() and ch.isalnum()


def _join_cues(texts) -> str:
    """拼接相邻字幕文本:英文词边界处补空格,避免 "hello""world" 粘连;
    CJK 之间直接相连,不在中文字间插空格。"""
    result = ""
    for t in texts:
        if not t:
            continue
        if result and _is_word_char(result[-1]) and _is_word_char(t[0]):
            result += " "
        result += t
    return result


class TranscriptParseStep(StepBase):
    def validate_inputs(self) -> list[str]:
        # 02_whisper 写 input/subtitle.srt
        if not (self.job_dir / "input" / "subtitle.srt").exists():
            return ["input/subtitle.srt"]
        return []

    def input_hashes(self) -> dict[str, str]:
        hashes = {
            "subtitle": file_hash(self.job_dir / "input" / "subtitle.srt"),
        }
        metadata = self.job_dir / "input" / "metadata.json"
        if metadata.exists():
            hashes["metadata"] = file_hash(metadata)
        for suffix in (".mp3", ".m4a", ".wav", ".aac", ".flac", ".mp4"):
            media = self.job_dir / "input" / f"source{suffix}"
            if media.exists():
                hashes["source_media"] = file_hash(media)
                break
        return hashes

    def execute(self) -> dict | None:
        entries = load_srt(self.job_dir / "input" / "subtitle.srt")

        # 按固定时间窗口聚合为段落,提供下游 sections 雏形
        segments = self._aggregate(entries)
        full_text = _join_cues([e.text for e in entries])
        duration_sec = round(entries[-1].end_sec, 1) if entries else 0.0

        transcript = {
            "segments": [
                {"start": round(s["start"], 1), "end": round(s["end"], 1), "text": s["text"]}
                for s in segments
            ],
            "full_text": full_text,
            "duration_sec": duration_sec,
        }
        self.artifacts.write("intermediate/transcript.json", transcript)

        # 顺带产出可读的段落式逐字稿
        md = self._render_markdown(segments)
        self.artifacts.write("output/transcript.md", md)

        # segments.json 供下游对齐 sections 雏形
        self.artifacts.write("intermediate/segments.json", segments)

        source_manifest = build_audio_source_manifest(self.job_dir, segments)
        source_count = 0
        provenance = {"status": "no_reliable_refs", "segments": 0}
        if source_manifest is None:
            (self.job_dir / SOURCE_MANIFEST_PATH).unlink(missing_ok=True)
        else:
            write_audio_source_manifest(self.job_dir, source_manifest)
            source_count = len(source_manifest["segments"])
            normalized_body = markdown_to_index_text(md)
            mappings = transcript_provenance_segments(
                segments, source_manifest, normalized_body,
            )
            provenance = persist_audio_note_provenance(
                self.job_dir,
                note_type="transcript",
                note_artifact="output/transcript.md",
                provenance_segments=mappings,
            )

        return {
            "segments": len(segments),
            "duration_sec": duration_sec,
            "source_segments": source_count,
            "provenance_segments": provenance["segments"],
            "provenance_status": provenance["status"],
        }

    def _aggregate(self, entries) -> list[dict]:
        # 把零碎 SRT 条目按 SEGMENT_INTERVAL_SEC 窗口合并成段落
        if not entries:
            return []

        segments: list[dict] = []
        window_start = entries[0].start_sec
        buf: list[str] = []
        seg_start = entries[0].start_sec
        seg_end = entries[0].end_sec

        for e in entries:
            if e.start_sec - window_start >= SEGMENT_INTERVAL_SEC and buf:
                segments.append({"start": seg_start, "end": seg_end, "text": _join_cues(buf)})
                window_start = e.start_sec
                seg_start = e.start_sec
                buf = []
            buf.append(e.text)
            seg_end = e.end_sec

        if buf:
            segments.append({"start": seg_start, "end": seg_end, "text": _join_cues(buf)})

        return segments

    def _render_markdown(self, segments) -> str:
        if not segments:
            return "# 逐字稿\n\n（无内容）\n"

        parts = ["# 逐字稿\n"]
        for seg in segments:
            ts = format_timestamp(seg["start"])
            parts.append(f"\n**{ts}** {seg['text']}\n")
        return "".join(parts)


if __name__ == "__main__":
    TranscriptParseStep.cli_main("03_transcript_parse")
