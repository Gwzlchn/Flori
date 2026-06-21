"""Step 06: 论文质量评审。复用 video 11_review 逻辑 + 额外检查公式/图表引用。"""

from __future__ import annotations

import json

from shared.step_base import StepBase, file_hash


class PaperReviewStep(StepBase):
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
        smart_clip, coverage = self.clip_note_for_review(smart)
        sections = self.load_json("intermediate/sections.json")

        original_titles = [
            s["title"] for s in sections.get("sections", [])
        ]

        prompt = (
            "请对以下论文笔记进行质量评审。\n\n"
            "评分维度（每项 1-5 分）：\n"
            "1. completeness: 信息完整性\n"
            "2. accuracy: 准确性\n"
            "3. structure: 结构清晰度\n"
            "4. terminology: 术语使用\n"
            "5. formula_integrity: 公式完整性（LaTeX 格式是否正确）\n"
            "6. figure_references: 图表引用是否恰当\n\n"
            "另外输出：\n"
            "- key_terms: 这篇笔记**讲清楚**的关键概念 + 一句话候选定义（用于沉淀进概念库）\n"
            "- missing_concepts: 笔记**遗漏**的重要概念（知识缺口，仅供选题/查漏）\n"
            "- top3_improvements: 最重要的 3 条改进建议\n\n"
            "只输出如下扁平 JSON：六个维度为顶层整数键，不要嵌套进 scores 子对象、"
            "不要加 rationale 字段、不要代码围栏、不要任何额外说明文字。\n"
            "{\n"
            '  "completeness": 4, "accuracy": 4, "structure": 4,\n'
            '  "terminology": 4, "formula_integrity": 4, "figure_references": 4,\n'
            '  "key_terms": [{"term": "概念名", "definition": "一句话候选定义"}],\n'
            '  "missing_concepts": ["遗漏的重要概念"],\n'
            '  "top3_improvements": ["改进建议1", "改进建议2", "改进建议3"]\n'
            "}\n\n"
            f"原文章节：{json.dumps(original_titles, ensure_ascii=False)}\n\n"
            f"--- 笔记 ---\n{smart_clip}"
        )

        review, parse_failed = self.call_ai_json(
            prompt,
            fallback={
                "completeness": 3, "accuracy": 3, "structure": 3,
                "terminology": 3, "formula_integrity": 3, "figure_references": 3,
                "overall": 3.0,
                "key_terms": [],
                "missing_concepts": [],
                "top3_improvements": ["AI 返回的不是有效 JSON"],
            },
            score_keys=[
                "completeness", "accuracy", "structure",
                "terminology", "formula_integrity", "figure_references",
            ],
        )

        review["review_coverage"] = coverage
        self.write_review(review, note_file)
        return {"overall": review.get("overall", 0), "parse_failed": parse_failed,
                "note_file": note_file, "coverage_truncated": coverage["truncated"]}


if __name__ == "__main__":
    PaperReviewStep.cli_main("06_review")
