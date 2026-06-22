"""Step 06: 论文质量评审。与各 review 步共用 StepBase 评审骨架(build_review_prompt/run_dimension_review),
额外检查公式完整性 + 图表引用(把 figures.json 作 figure_references 维度的客观对照)。"""

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
        if not (self.job_dir / "intermediate" / "figures.json").exists():
            missing.append("intermediate/figures.json")
        return missing

    def input_hashes(self) -> dict[str, str]:
        return {
            "smart": file_hash(self.latest_smart_note()) if self.latest_smart_note() else "",
            "sections": file_hash(self.job_dir / "intermediate" / "sections.json"),
            "figures": file_hash(self.job_dir / "intermediate" / "figures.json"),
        }

    def execute(self) -> dict | None:
        smart_clip, coverage, note_file = self.prepare_smart_for_review()
        sections = self.load_json("intermediate/sections.json")
        figures = self.load_json("intermediate/figures.json")

        original_titles = [
            s["title"] for s in sections.get("sections", [])
        ]
        # figure_references 维度的客观对照:原文图表清单(序号 | 图注 | 是否可内嵌),否则该维度只能盲评。
        figure_list = [
            {"ref": f.get("index") if f.get("index") is not None else f.get("id", ""),
             "caption": f.get("caption", ""),
             "embeddable": bool(f.get("filename"))}
            for f in figures
        ]

        dimensions = [
            ("completeness", "信息完整性"),
            ("accuracy", "准确性"),
            ("structure", "结构清晰度"),
            ("terminology", "术语使用"),
            ("formula_integrity", "公式完整性（LaTeX 格式是否正确）"),
            ("figure_references", "图表引用是否恰当"),
        ]
        prompt = self.build_review_prompt(
            intro="请对以下论文笔记进行质量评审。",
            dimensions=dimensions,
            ref_block=(
                f"原文章节：{json.dumps(original_titles, ensure_ascii=False)}\n"
                f"原文图表(ref 序号 | 图注 | 是否可内嵌):{json.dumps(figure_list, ensure_ascii=False)}\n\n"
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
    PaperReviewStep.cli_main("06_review")
