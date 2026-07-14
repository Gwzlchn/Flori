"""Step 08: 口播稿。读音频对应的原生字幕——中文加标点;非中文翻译成中文。均保留 [MM:SS]。"""

from __future__ import annotations

from pathlib import Path

from shared.step_base import StepBase, file_hash
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
        t = self.template_hash("08_punctuate.zh", "08_punctuate.translate")
        if t:
            h["template"] = t
        return h

    def execute(self) -> dict | None:
        sub, is_zh = self._pick()
        all_entries = load_srt(sub) if sub else []

        lines = [f"{format_timestamp(e.start_sec)} {e.text}" for e in all_entries]
        full_text = "\n".join(lines)

        header = self._load_prompt_template(
            "08_punctuate.zh" if is_zh else "08_punctuate.translate",
        )
        chunks = self._split_chunks(full_text, CHUNK_SIZE)
        results = []
        action = "punctuating" if is_zh else "translating"
        for i, chunk in enumerate(chunks):
            self.report_progress(i, len(chunks), f"{action} chunk {i + 1}/{len(chunks)}")
            results.append(self.call_ai(header + chunk).strip())

        self.report_progress(len(chunks), len(chunks), "done")
        # 每条 [MM:SS] 单独成段(空行分隔):否则 Markdown 会把单换行折叠成一坨墙、难读。
        cues = [ln.strip() for r in results for ln in r.splitlines() if ln.strip()]
        self.write_output("output/transcript.md", "\n\n".join(cues))
        return {"lines": len(all_entries), "chunks": len(chunks),
                "mode": "zh" if is_zh else "translate"}

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
