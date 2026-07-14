"""Step 05: 播客笔记质量评审。AI 对智能笔记打分并给改进建议。"""

from __future__ import annotations

import json

from shared.review_contract import persist_review_source, source_record
from shared.step_base import StepBase, file_hash


class PodcastReviewStep(StepBase):
    def validate_inputs(self) -> list[str]:
        missing = []
        if self.latest_smart_note() is None:
            missing.append("output/versions/notes_smart_*.md")
        if not (self.job_dir / "intermediate" / "transcript.json").exists():
            missing.append("intermediate/transcript.json")
        return missing

    def input_hashes(self) -> dict[str, str]:
        return {
            "smart": file_hash(self.latest_smart_note()) if self.latest_smart_note() else "",
            "transcript": file_hash(self.job_dir / "intermediate" / "transcript.json"),
            "provider": self.override_provider(),
        }

    def execute(self) -> dict | None:
        smart_clip, coverage, note_file, smart_source = self.prepare_smart_for_review()
        transcript_data, _ = source_record(
            self.job_dir, "intermediate/transcript.json", label="transcript_json",
        )
        try:
            transcript = json.loads(transcript_data)
        except (json.JSONDecodeError, TypeError) as exc:
            raise ValueError("audio review transcript is invalid") from exc
        if not isinstance(transcript, dict):
            raise ValueError("audio review transcript is invalid")
        full_text = transcript.get("full_text", "")
        if not isinstance(full_text, str) or not full_text.strip():
            raise ValueError("audio review source has no transcript body")
        full_text, transcript_source = persist_review_source(
            self.job_dir, full_text, label="transcript",
        )

        dimensions = [
            ("completeness", "信息完整性（是否遗漏重要内容）"),
            ("accuracy", "准确性（是否有事实错误）"),
            ("structure", "结构清晰度"),
            ("terminology", "术语使用准确性"),
            ("conciseness", "口语净化程度（是否去除冗余/停顿）"),
            ("readability", "可读性"),
        ]
        prompt = self.build_review_prompt(
            intro="请对以下播客笔记进行质量评审。",
            dimensions=dimensions,
            ref_block=(
                f"--- 转写正文 ---\n{full_text}\n\n"
                f"--- 笔记 ---\n{smart_clip}"
            ),
        )
        score_keys = [key for key, _ in dimensions]
        review, parse_failed = self.run_dimension_review(
            prompt, fallback=self.review_fallback(score_keys), score_keys=score_keys,
            note_file=note_file, coverage=coverage,
            review_sources=[smart_source, transcript_source],
            review_source_texts={"smart": smart_clip, "transcript": full_text},
        )
        return {"overall": review.get("overall", 0), "parse_failed": parse_failed,
                "note_file": note_file, "coverage_truncated": coverage["truncated"]}


if __name__ == "__main__":
    PodcastReviewStep.cli_main("05_review")
