"""Step 05: 文章笔记质量评审。AI 按维度评分 + 改进建议。"""

from __future__ import annotations

import json

from shared.step_base import StepBase, file_hash


class ArticleReviewStep(StepBase):
    def validate_inputs(self) -> list[str]:
        missing = []
        if self.latest_smart_note() is None:
            missing.append("output/versions/notes_smart_*.md")
        if not (self.job_dir / "intermediate" / "sections.json").exists():
            missing.append("intermediate/sections.json")
        return missing

    def input_hashes(self) -> dict[str, str]:
        return {
            "smart": file_hash(self.latest_smart_note()) if self.latest_smart_note() else "",
            "sections": file_hash(self.job_dir / "intermediate" / "sections.json"),
        }

    def execute(self) -> dict | None:
        smart_clip, coverage, note_file = self.prepare_smart_for_review()
        sections = self.load_json("intermediate/sections.json")

        original_titles = [s["title"] for s in sections.get("sections", [])]

        dimensions = [
            ("completeness", "信息完整性"),
            ("accuracy", "准确性"),
            ("structure", "结构清晰度"),
            ("readability", "可读性"),
            ("insight", "观点提炼深度"),
        ]
        prompt = self.build_review_prompt(
            intro="请对以下文章笔记进行质量评审。",
            dimensions=dimensions,
            ref_block=(
                f"原文章节：{json.dumps(original_titles, ensure_ascii=False)}\n\n"
                f"--- 笔记 ---\n{smart_clip}"
            ),
        )
        score_keys = [key for key, _ in dimensions]
        review, parse_failed = self.run_dimension_review(
            prompt, fallback=self.review_fallback(score_keys), score_keys=score_keys,
            note_file=note_file, coverage=coverage,
        )
        return {"overall": review.get("overall", 0), "parse_failed": parse_failed,
                "note_file": note_file, "coverage_truncated": coverage["truncated"]}


if __name__ == "__main__":
    ArticleReviewStep.cli_main("05_review")
