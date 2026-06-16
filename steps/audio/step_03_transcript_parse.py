"""Step 20: 播客转写解析。02_whisper 的 SRT → 按时间间隔聚合段落。"""

from __future__ import annotations

from pathlib import Path

from shared.step_base import StepBase, file_hash
from steps.utils.srt_parser import format_timestamp, load_srt

# 段落聚合时间间隔（秒）
SEGMENT_INTERVAL_SEC = 60


class TranscriptParseStep(StepBase):
    def validate_inputs(self) -> list[str]:
        # 02_whisper 写 input/subtitle.srt
        if not (self.job_dir / "input" / "subtitle.srt").exists():
            return ["input/subtitle.srt"]
        return []

    def input_hashes(self) -> dict[str, str]:
        return {
            "subtitle": file_hash(self.job_dir / "input" / "subtitle.srt"),
        }

    def execute(self) -> dict | None:
        entries = load_srt(self.job_dir / "input" / "subtitle.srt")

        # 按固定时间窗口聚合为段落，提供下游 sections 雏形
        segments = self._aggregate(entries)
        full_text = "".join(e.text for e in entries)
        duration_sec = round(entries[-1].end_sec, 1) if entries else 0.0

        transcript = {
            "segments": [
                {"start": round(s["start"], 1), "end": round(s["end"], 1), "text": s["text"]}
                for s in segments
            ],
            "full_text": full_text,
            "duration_sec": duration_sec,
        }
        self.write_output("intermediate/transcript.json", transcript)

        # 顺带产出可读的段落式逐字稿
        md = self._render_markdown(segments)
        self.write_output("output/transcript.md", md)

        # segments.json 供下游对齐 sections 雏形
        self.write_output("intermediate/segments.json", segments)

        return {"segments": len(segments), "duration_sec": duration_sec}

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
                segments.append({"start": seg_start, "end": seg_end, "text": "".join(buf)})
                window_start = e.start_sec
                seg_start = e.start_sec
                buf = []
            buf.append(e.text)
            seg_end = e.end_sec

        if buf:
            segments.append({"start": seg_start, "end": seg_end, "text": "".join(buf)})

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
