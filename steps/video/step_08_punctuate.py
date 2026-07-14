"""Step 08: 口播稿。读音频对应的原生字幕——中文加标点;非中文翻译成中文。均保留 [MM:SS]。"""

from __future__ import annotations

from pathlib import Path

from shared.note_text import markdown_to_index_text
from shared.step_base import StepBase, file_hash
from steps.video.provenance import (
    SOURCE_MANIFEST_PATH,
    build_video_source_manifest,
    persist_video_note_provenance,
    transcript_provenance_segments,
    write_video_source_manifest,
)
from steps.utils.srt_parser import format_timestamp, load_srt, pick_native_srt


CHUNK_SIZE = 30000

class PunctuateStep(StepBase):
    def _pick(self) -> tuple[Path | None, bool]:
        return pick_native_srt(self.job_dir / "input")

    def validate_inputs(self) -> list[str]:
        sub, _ = self._pick()
        return [] if sub else ["input/*.srt"]

    def input_hashes(self) -> dict[str, str]:
        sub, is_zh = self._pick()
        if not sub:
            return {}
        # 语言纳入指纹:同字幕在加标点与翻译两种模式下产物不同,须各自重算。
        h = {sub.name: file_hash(sub), "mode": "zh" if is_zh else "translate"}
        metadata = self.job_dir / "input" / "metadata.json"
        media = self.job_dir / "input" / "source.mp4"
        if metadata.exists():
            h["metadata"] = file_hash(metadata)
        if media.exists():
            h["source_media"] = file_hash(media)
        ocr = self.job_dir / "intermediate" / "ocr.json"
        if ocr.exists():
            h["ocr"] = file_hash(ocr)
        t = self.ai.template_hash("08_punctuate.zh", "08_punctuate.translate")
        if t:
            h["template"] = t
        return h

    def execute(self) -> dict | None:
        sub, is_zh = self._pick()
        all_entries = load_srt(sub) if sub else []

        lines = [f"{format_timestamp(e.start_sec)} {e.text}" for e in all_entries]
        full_text = "\n".join(lines)

        header = self.ai.load_prompt_template(
            "08_punctuate.zh" if is_zh else "08_punctuate.translate",
        )
        chunks = self._split_chunks(full_text, CHUNK_SIZE)
        results = []
        action = "punctuating" if is_zh else "translating"
        for i, chunk in enumerate(chunks):
            self.progress.report(i, len(chunks), f"{action} chunk {i + 1}/{len(chunks)}")
            results.append(self.ai.call(header + chunk).strip())

        self.progress.report(len(chunks), len(chunks), "done")
        # 每条 [MM:SS] 单独成段(空行分隔):否则 Markdown 会把单换行折叠成一坨墙、难读。
        cues = [ln.strip() for r in results for ln in r.splitlines() if ln.strip()]
        transcript = "\n\n".join(cues)
        self.artifacts.write("output/transcript.md", transcript)
        source_manifest = build_video_source_manifest(self.job_dir, all_entries)
        source_count = 0
        provenance = {"status": "legacy_no_source_manifest", "segments": 0}
        if source_manifest is None:
            (self.job_dir / SOURCE_MANIFEST_PATH).unlink(missing_ok=True)
            (self.job_dir / "output" / "provenance" / "transcript.json").unlink(missing_ok=True)
        else:
            write_video_source_manifest(self.job_dir, source_manifest)
            source_count = len(source_manifest["segments"])
            mappings = transcript_provenance_segments(
                cues, source_manifest, markdown_to_index_text(transcript),
            )
            provenance = persist_video_note_provenance(
                self.job_dir,
                note_type="transcript",
                note_artifact="output/transcript.md",
                provenance_segments=mappings,
            )
        return {"lines": len(all_entries), "chunks": len(chunks),
                "mode": "zh" if is_zh else "translate",
                "source_segments": source_count,
                "provenance_segments": provenance["segments"],
                "provenance_status": provenance["status"]}

    def _split_chunks(self, text: str, max_chars: int) -> list[str]:
        if len(text) <= max_chars:
            return [text]

        chunks = []
        lines = text.split("\n")
        current: list[str] = []
        current_len = 0

        for line in lines:
            if current_len + len(line) + 1 > max_chars and current:
                chunks.append("\n".join(current))
                current = []
                current_len = 0
            current.append(line)
            current_len += len(line) + 1

        if current:
            chunks.append("\n".join(current))

        return chunks


if __name__ == "__main__":
    PunctuateStep.cli_main("08_punctuate")
