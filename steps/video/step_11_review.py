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
        smart_path = self.latest_smart_note()
        smart = smart_path.read_text(encoding="utf-8") if smart_path else ""
        note_file = str(smart_path.relative_to(self.job_dir)) if smart_path else None
        smart_clip, coverage = self.clip_note_for_review(smart)

        prompt = (
            "请对比以下两份笔记，对 AI 生成的智能版笔记进行质量评审。\n\n"
            "评分维度（每项打 1-5 的整数）：\n"
            "1. completeness: 信息完整性（是否遗漏重要内容）\n"
            "2. accuracy: 准确性（是否有事实错误）\n"
            "3. structure: 结构清晰度\n"
            "4. terminology: 术语使用准确性\n"
            "5. visual_integration: 截图引用恰当性\n"
            "6. readability: 可读性\n\n"
            "另外输出：\n"
            "- key_terms: 这篇笔记**讲清楚**的关键概念 + 一句话候选定义（用于沉淀进概念库）\n"
            "- missing_concepts: 笔记**遗漏**的重要概念（知识缺口，仅供选题/查漏）\n"
            "- top3_improvements: 最重要的 3 条改进建议\n\n"
            "只输出如下扁平 JSON：六个维度为顶层整数键，不要嵌套进 scores 子对象、"
            "不要加 rationale 字段、不要代码围栏、不要任何额外说明文字。\n"
            "{\n"
            '  "completeness": 4, "accuracy": 4, "structure": 4,\n'
            '  "terminology": 4, "visual_integration": 4, "readability": 4,\n'
            '  "key_terms": [{"term": "概念名", "definition": "一句话候选定义"}],\n'
            '  "missing_concepts": ["遗漏的重要概念"],\n'
            '  "top3_improvements": ["改进建议1", "改进建议2", "改进建议3"]\n'
            "}\n\n"
            f"--- 机械版笔记 ---\n{mechanical[:REVIEW_REF_LIMIT]}\n\n"
            f"--- 智能版笔记 ---\n{smart_clip}"
        )

        review, parse_failed = self.call_ai_json(
            prompt,
            fallback={
                "completeness": 3, "accuracy": 3, "structure": 3,
                "terminology": 3, "visual_integration": 3, "readability": 3,
                "overall": 3.0,
                "key_terms": [],
                "missing_concepts": [],
                "top3_improvements": ["AI 返回的不是有效 JSON"],
            },
            score_keys=[
                "completeness", "accuracy", "structure",
                "terminology", "visual_integration", "readability",
            ],
        )

        review["review_coverage"] = coverage   # 评分覆盖范围(超长笔记标 truncated,分数只对覆盖段负责)
        self.write_review(review, note_file)   # 补记 生成时间/方式/模型 + 评的是哪一版
        return {"overall": review.get("overall", 0), "parse_failed": parse_failed,
                "provider": review["provider"], "note_file": note_file,
                "coverage_truncated": coverage["truncated"]}


if __name__ == "__main__":
    ReviewStep.cli_main("11_review")
