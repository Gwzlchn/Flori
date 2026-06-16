"""Step 09: 质量评审。6 维度评分 + 缺失概念 + 改进建议。"""

from __future__ import annotations

import json
from pathlib import Path

from shared.step_base import StepBase, file_hash


class ReviewStep(StepBase):
    def validate_inputs(self) -> list[str]:
        missing = []
        if not (self.job_dir / "output" / "notes_smart.md").exists():
            missing.append("output/notes_smart.md")
        if not (self.job_dir / "output" / "notes_mechanical.md").exists():
            missing.append("output/notes_mechanical.md")
        return missing

    def input_hashes(self) -> dict[str, str]:
        return {
            "smart": file_hash(self.job_dir / "output" / "notes_smart.md"),
            "mechanical": file_hash(self.job_dir / "output" / "notes_mechanical.md"),
            # provider 覆盖纳入指纹:换 provider 重跑时强制重评。
            "provider": self.override_provider(),
        }

    def execute(self) -> dict | None:
        mechanical = (self.job_dir / "output" / "notes_mechanical.md").read_text(encoding="utf-8")
        smart = (self.job_dir / "output" / "notes_smart.md").read_text(encoding="utf-8")

        prompt = (
            "请对比以下两份笔记，对 AI 生成的智能版笔记进行质量评审。\n\n"
            "评分维度（每项 1-5 分）：\n"
            "1. completeness: 信息完整性（是否遗漏重要内容）\n"
            "2. accuracy: 准确性（是否有事实错误）\n"
            "3. structure: 结构清晰度\n"
            "4. terminology: 术语使用准确性\n"
            "5. visual_integration: 截图引用恰当性\n"
            "6. readability: 可读性\n\n"
            "同时输出：\n"
            "- missing_concepts: 遗漏的重要概念列表\n"
            "- top3_improvements: 最重要的 3 条改进建议\n\n"
            "请严格以 JSON 格式输出，不要包含其他文字。\n\n"
            f"--- 机械版笔记 ---\n{mechanical[:5000]}\n\n"
            f"--- 智能版笔记 ---\n{smart[:5000]}"
        )

        review, parse_failed = self.call_ai_json(
            prompt,
            fallback={
                "completeness": 3, "accuracy": 3, "structure": 3,
                "terminology": 3, "visual_integration": 3, "readability": 3,
                "overall": 3.0,
                "missing_concepts": [],
                "top3_improvements": ["AI 返回的不是有效 JSON，请重试"],
            },
            score_keys=[
                "completeness", "accuracy", "structure",
                "terminology", "visual_integration", "readability",
            ],
        )

        self.write_output("output/review.json", review)
        # 版本化:按 provider 另存评分,与对应版本智能笔记配对显示。
        provider = self.last_ai_provider or "unknown"
        review["provider"] = provider
        self.write_output(f"output/versions/review__{provider}.json", review)
        return {"overall": review.get("overall", 0), "parse_failed": parse_failed, "provider": provider}


if __name__ == "__main__":
    ReviewStep.cli_main("11_review")
