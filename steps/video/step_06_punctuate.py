"""Step 06: 字幕加标点。AI 给无标点字幕补标点，保留时间戳。"""

from __future__ import annotations

import json
from pathlib import Path

from shared.step_base import StepBase, file_hash
from steps.utils.srt_parser import format_timestamp, load_srt


CHUNK_SIZE = 30000


class PunctuateStep(StepBase):
    def _pick_subtitle(self) -> Path | None:
        """口播 = 中文字幕。一个视频常含多语言 srt(英/西/日…),只取中文那一份,
        否则会把多语言混进口播稿。优先中文标记,其次 subtitle.srt,再不行取第一个。"""
        srts = sorted((self.job_dir / "input").glob("*.srt"))
        if not srts:
            return None
        zh = [f for f in srts if any(k in f.name.lower() for k in
              ("中文", "简体", "zh", "chs", "chinese", "cn"))]
        if zh:
            return zh[0]
        primary = [f for f in srts if f.name == "subtitle.srt"]
        return primary[0] if primary else srts[0]

    def validate_inputs(self) -> list[str]:
        if self._pick_subtitle() is None:
            return ["input/*.srt"]
        return []

    def input_hashes(self) -> dict[str, str]:
        sub = self._pick_subtitle()
        return {sub.name: file_hash(sub)} if sub else {}

    def execute(self) -> dict | None:
        sub = self._pick_subtitle()
        all_entries = load_srt(sub) if sub else []

        lines = [
            f"{format_timestamp(e.start_sec)} {e.text}"
            for e in all_entries
        ]
        full_text = "\n".join(lines)

        chunks = self._split_chunks(full_text, CHUNK_SIZE)
        results = []
        for i, chunk in enumerate(chunks):
            self.report_progress(i, len(chunks), f"punctuating chunk {i + 1}/{len(chunks)}")
            prompt = (
                "请给以下字幕文本添加中文标点符号。保留每行开头的 [MM:SS] 时间戳格式不变。"
                "不要修改内容，只添加标点。直接输出结果，不要解释。\n\n"
                f"{chunk}"
            )
            punctuated = self.call_ai(prompt)
            results.append(punctuated.strip())

        self.report_progress(len(chunks), len(chunks), "done")
        transcript = "\n\n".join(results)
        self.write_output("output/transcript.md", transcript)
        return {"lines": len(all_entries), "chunks": len(chunks)}

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
    PunctuateStep.cli_main("06_punctuate")
