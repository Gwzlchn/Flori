"""Step 05: 文章笔记质量评审。AI 按维度评分 + 改进建议。"""

from __future__ import annotations

import json
from pathlib import Path

from shared.review_contract import persist_review_source, source_record
from shared.step_base import StepBase, file_hash


class ArticleReviewStep(StepBase):
    def validate_inputs(self) -> list[str]:
        missing = []
        if self.artifacts.latest_smart_note() is None:
            missing.append("output/versions/notes_smart_*.md")
        if not (self.job_dir / "intermediate" / "sections.json").exists():
            missing.append("intermediate/sections.json")
        return missing

    def input_hashes(self) -> dict[str, str]:
        hashes = {
            "smart": file_hash(self.artifacts.latest_smart_note()) if self.artifacts.latest_smart_note() else "",
            "sections": file_hash(self.job_dir / "intermediate" / "sections.json"),
            "original": file_hash(self.job_dir / "output" / "original.md")
                        if (self.job_dir / "output" / "original.md").exists() else "",
            "translated": file_hash(self.job_dir / "output" / "translated.md")
                          if (self.job_dir / "output" / "translated.md").exists() else "",
            "provider": self.ai.override_provider(),
        }
        hashes["template"] = self.ai.template_hash(self.ai.primary_prompt_template())
        return hashes

    def execute(self) -> dict | None:
        smart_clip, coverage, note_file, smart_source = self.review.prepare_smart()
        source_paths = [p for p in ("output/original.md", "output/translated.md")
                        if (self.job_dir / p).exists()]
        if not source_paths:
            source_paths = ["intermediate/sections.json"]
        source_blocks = []
        article_sources = []
        article_source_texts = {}
        for source_path in source_paths:
            source_text, article_source = source_record(
                self.job_dir, source_path, label=Path(source_path).stem,
            )
            if source_path.endswith("sections.json"):
                try:
                    sections = json.loads(source_text)
                except (json.JSONDecodeError, TypeError) as exc:
                    raise ValueError("article review sections are invalid") from exc
                if not isinstance(sections, dict):
                    raise ValueError("article review sections are invalid")
                source_text = "\n\n".join(
                    f"## {s.get('title', '')}\n{s.get('text', '')}"
                    for s in sections.get("sections", [])
                )
                if not source_text.strip():
                    raise ValueError("article review source has no section body")
                source_text, article_source = persist_review_source(
                    self.job_dir, source_text, label="sections",
                )
            source_blocks.append(f"--- {article_source['label']} 全文 ---\n{source_text}")
            article_sources.append(article_source)
            article_source_texts[article_source["label"]] = source_text

        dimensions = [
            ("completeness", "信息完整性"),
            ("accuracy", "准确性"),
            ("structure", "结构清晰度"),
            ("readability", "可读性"),
            ("insight", "观点提炼深度"),
        ]
        prompt = self.review.build_prompt(
            intro="请对以下文章笔记进行质量评审。",
            dimensions=dimensions,
            ref_block=(
                "\n\n".join(source_blocks) + "\n\n"
                f"--- 笔记 ---\n{smart_clip}"
            ),
        )
        score_keys = [key for key, _ in dimensions]
        review, parse_failed = self.review.run_dimension(
            prompt, fallback=self.review.fallback(score_keys), score_keys=score_keys,
            note_file=note_file, coverage=coverage,
            review_sources=[smart_source, *article_sources],
            review_source_texts={"smart": smart_clip, **article_source_texts},
        )
        return {"overall": review.get("overall", 0), "parse_failed": parse_failed,
                "note_file": note_file, "coverage_truncated": coverage["truncated"]}


if __name__ == "__main__":
    # 步名须 = pipelines.yaml article 评审步名(06_review),令 worker self.step_name = yaml = API key,
    # 评审模板/DB 覆盖才能按统一 key 命中。各链评审步名: audio=05_review/paper=06_review/video=12_review。
    ArticleReviewStep.cli_main("06_review")
