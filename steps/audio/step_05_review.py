"""Step 05: 播客笔记质量评审。AI 对智能笔记打分并给改进建议。"""

from __future__ import annotations

from shared.step_base import REVIEW_REF_LIMIT, StepBase, file_hash


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
        }

    def execute(self) -> dict | None:
        smart_path = self.latest_smart_note()
        smart = smart_path.read_text(encoding="utf-8") if smart_path else ""
        note_file = str(smart_path.relative_to(self.job_dir)) if smart_path else None
        smart_clip, coverage = self.clip_note_for_review(smart)
        transcript = self.load_json("intermediate/transcript.json")
        full_text = transcript.get("full_text", "")

        prompt = (
            "请对以下播客笔记进行质量评审。\n\n"
            "评分维度（每项打 1-5 的整数）：\n"
            "1. completeness: 信息完整性（是否遗漏重要内容）\n"
            "2. accuracy: 准确性（是否有事实错误）\n"
            "3. structure: 结构清晰度\n"
            "4. terminology: 术语使用准确性\n"
            "5. conciseness: 口语净化程度（是否去除冗余/停顿）\n"
            "6. readability: 可读性\n\n"
            "另外输出：\n"
            "- key_terms: 这篇笔记**讲清楚**的关键概念 + 一句话候选定义（用于沉淀进概念库）\n"
            "- missing_concepts: 笔记**遗漏**的重要概念（知识缺口，仅供选题/查漏）\n"
            "- top3_improvements: 最重要的 3 条改进建议\n\n"
            "只输出如下扁平 JSON：六个维度为顶层整数键，不要嵌套进 scores 子对象、"
            "不要加 rationale 字段、不要代码围栏、不要任何额外说明文字。\n"
            "{\n"
            '  "completeness": 4, "accuracy": 4, "structure": 4,\n'
            '  "terminology": 4, "conciseness": 4, "readability": 4,\n'
            '  "key_terms": [{"term": "概念名", "definition": "一句话候选定义"}],\n'
            '  "missing_concepts": ["遗漏的重要概念"],\n'
            '  "top3_improvements": ["改进建议1", "改进建议2", "改进建议3"]\n'
            "}\n\n"
            f"--- 转写正文（节选）---\n{full_text[:REVIEW_REF_LIMIT]}\n\n"
            f"--- 笔记 ---\n{smart_clip}"
        )

        review, parse_failed = self.call_ai_json(
            prompt,
            fallback={
                "completeness": 3, "accuracy": 3, "structure": 3,
                "terminology": 3, "conciseness": 3, "readability": 3,
                "overall": 3.0,
                "key_terms": [],
                "missing_concepts": [],
                "top3_improvements": ["AI 返回的不是有效 JSON"],
            },
            score_keys=[
                "completeness", "accuracy", "structure",
                "terminology", "conciseness", "readability",
            ],
        )

        review["review_coverage"] = coverage
        self.write_review(review, note_file)
        return {"overall": review.get("overall", 0), "parse_failed": parse_failed,
                "note_file": note_file, "coverage_truncated": coverage["truncated"]}


if __name__ == "__main__":
    PodcastReviewStep.cli_main("05_review")
