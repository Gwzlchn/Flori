"""Step 19: 文章笔记质量评审。AI 按维度评分 + 改进建议。"""

from __future__ import annotations

import json
from pathlib import Path

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
        smart_path = self.latest_smart_note()
        smart = smart_path.read_text(encoding="utf-8") if smart_path else ""
        note_file = str(smart_path.relative_to(self.job_dir)) if smart_path else None
        sections = self.load_json("intermediate/sections.json")

        original_titles = [s["title"] for s in sections.get("sections", [])]

        prompt = (
            "请对以下文章笔记进行质量评审。\n\n"
            "评分维度（每项 1-5 分）：\n"
            "1. completeness: 信息完整性\n"
            "2. accuracy: 准确性\n"
            "3. structure: 结构清晰度\n"
            "4. readability: 可读性\n"
            "5. insight: 观点提炼深度\n\n"
            "同时输出：\n"
            "- missing_concepts: 遗漏的要点\n"
            "- top3_improvements: 改进建议\n\n"
            "请严格以 JSON 格式输出。\n\n"
            f"原文章节：{json.dumps(original_titles, ensure_ascii=False)}\n\n"
            f"--- 笔记 ---\n{smart[:5000]}"
        )

        review, parse_failed = self.call_ai_json(
            prompt,
            fallback={
                "completeness": 3, "accuracy": 3, "structure": 3,
                "readability": 3, "insight": 3,
                "overall": 3.0,
                "missing_concepts": [],
                "top3_improvements": ["AI 返回的不是有效 JSON"],
            },
            score_keys=[
                "completeness", "accuracy", "structure",
                "readability", "insight",
            ],
        )

        self.write_review(review, note_file)
        return {"overall": review.get("overall", 0), "parse_failed": parse_failed, "note_file": note_file}


if __name__ == "__main__":
    ArticleReviewStep.cli_main("05_review")
