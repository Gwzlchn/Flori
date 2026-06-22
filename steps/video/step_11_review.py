"""Step 11: 质量评审。6 维度评分 + 缺失概念 + 改进建议。评最新版智能笔记,review.json 标 note_file。"""

from __future__ import annotations

from shared.step_base import REVIEW_REF_LIMIT, StepBase, file_hash


class ReviewStep(StepBase):
    def validate_inputs(self) -> list[str]:
        missing = []
        if self.latest_smart_note() is None:
            missing.append("output/versions/notes_smart_*.md")
        if not (self.job_dir / "output" / "notes_mechanical.md").exists():
            missing.append("output/notes_mechanical.md")
        return missing

    def input_hashes(self) -> dict[str, str]:
        smart = self.latest_smart_note()
        return {
            "smart": file_hash(smart) if smart else "",
            "mechanical": file_hash(self.job_dir / "output" / "notes_mechanical.md"),
            # provider 覆盖纳入指纹:换 provider 重跑时强制重评。
            "provider": self.override_provider(),
        }

    def execute(self) -> dict | None:
        mechanical = (self.job_dir / "output" / "notes_mechanical.md").read_text(encoding="utf-8")
        smart_clip, coverage, note_file = self.prepare_smart_for_review()

        dimensions = [
            ("completeness", "信息完整性（是否遗漏重要内容）"),
            ("accuracy", "准确性（是否有事实错误）"),
            ("structure", "结构清晰度"),
            ("terminology", "术语使用准确性"),
            ("visual_integration", "截图引用恰当性"),
            ("readability", "可读性"),
        ]
        prompt = self.build_review_prompt(
            intro="请对比以下两份笔记，对 AI 生成的智能版笔记进行质量评审。",
            dimensions=dimensions,
            ref_block=(
                f"--- 机械版笔记 ---\n{mechanical[:REVIEW_REF_LIMIT]}\n\n"
                f"--- 智能版笔记 ---\n{smart_clip}"
            ),
        )
        score_keys = [key for key, _ in dimensions]
        review, parse_failed = self.run_dimension_review(
            prompt, fallback=self.review_fallback(score_keys), score_keys=score_keys,
            note_file=note_file, coverage=coverage,
        )
        return {"overall": review.get("overall", 0), "parse_failed": parse_failed,
                "provider": review.get("provider"), "note_file": note_file,
                "coverage_truncated": coverage["truncated"]}


if __name__ == "__main__":
    ReviewStep.cli_main("11_review")
