"""Step 06: 论文质量评审。与各 review 步共用 StepBase 评审骨架(build_review_prompt/run_dimension_review)。
额外检查公式完整性 + 图表引用,把 figures.json 作 figure_references 维度的客观对照。"""

from __future__ import annotations

import json
from pathlib import Path

from shared.review_contract import (
    paper_figures_review_text,
    persist_review_source,
    source_record,
)
from shared.step_base import StepBase, file_hash


class PaperReviewStep(StepBase):
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
            "figures": file_hash(self.job_dir / "intermediate" / "figures.json")
                       if (self.job_dir / "intermediate" / "figures.json").exists() else "",
            "provider": self.ai.override_provider(),
        }
        hashes["template"] = self.ai.template_hash(self.ai.primary_prompt_template())
        return hashes

    def execute(self) -> dict | None:
        smart_clip, coverage, note_file, smart_source = self.review.prepare_smart()
        figures: list = []
        if (self.job_dir / "intermediate" / "figures.json").exists():   # 仅旧 pymupdf job 有
            figure_data, _ = source_record(
                self.job_dir, "intermediate/figures.json", label="figures_json",
            )
            try:
                figures = json.loads(figure_data)
            except (json.JSONDecodeError, TypeError) as exc:
                raise ValueError("paper review figures are invalid") from exc
            if not isinstance(figures, list):
                raise ValueError("paper review figures are invalid")

        source_paths = [p for p in ("output/original.md", "output/translated.md")
                        if (self.job_dir / p).exists()]
        if not source_paths:
            source_paths = ["intermediate/sections.json"]
        source_blocks = []
        paper_sources = []
        paper_source_texts = {}
        for source_path in source_paths:
            source_text, paper_source = source_record(
                self.job_dir, source_path, label=Path(source_path).stem,
            )
            if source_path.endswith("sections.json"):
                try:
                    sections = json.loads(source_text)
                except (json.JSONDecodeError, TypeError) as exc:
                    raise ValueError("paper review sections are invalid") from exc
                if not isinstance(sections, dict):
                    raise ValueError("paper review sections are invalid")
                source_text = "\n\n".join(
                    f"## {s.get('title', '')}\n{s.get('text', '')}"
                    for s in sections.get("sections", [])
                )
                if not source_text.strip():
                    raise ValueError("paper review source has no section body")
                source_text, paper_source = persist_review_source(
                    self.job_dir, source_text, label="sections",
                )
            source_blocks.append(f"--- {paper_source['label']} 全文 ---\n{source_text}")
            paper_sources.append(paper_source)
            paper_source_texts[paper_source["label"]] = source_text
        # figures.json 是可变当前事实。送评时投影并内容寻址,读时再与当前文件重算比对。
        figure_text = paper_figures_review_text(figures)
        if (self.job_dir / "intermediate" / "figures.json").exists():
            figure_text, figure_source = persist_review_source(
                self.job_dir, figure_text, label="figures",
            )
            source_blocks.append(f"--- figures 全文 ---\n{figure_text}")
            paper_sources.append(figure_source)
            paper_source_texts["figures"] = figure_text

        dimensions = [
            ("completeness", "信息完整性"),
            ("accuracy", "准确性"),
            ("structure", "结构清晰度"),
            ("terminology", "术语使用"),
            ("formula_integrity", "公式完整性（LaTeX 格式是否正确）"),
            ("figure_references", "图表引用是否恰当"),
        ]
        prompt = self.review.build_prompt(
            intro="请对以下论文笔记进行质量评审。",
            dimensions=dimensions,
            ref_block=(
                "\n\n".join(source_blocks) + "\n\n"
                f"原文图表(ref 序号 | 图注 | 是否可内嵌):{figure_text}\n\n"
                f"--- 笔记 ---\n{smart_clip}"
            ),
        )
        score_keys = [key for key, _ in dimensions]
        review, parse_failed = self.review.run_dimension(
            prompt, fallback=self.review.fallback(score_keys), score_keys=score_keys,
            note_file=note_file, coverage=coverage,
            review_sources=[smart_source, *paper_sources],
            review_source_texts={"smart": smart_clip, **paper_source_texts},
        )
        return {"overall": review.get("overall", 0), "parse_failed": parse_failed,
                "note_file": note_file, "coverage_truncated": coverage["truncated"]}


if __name__ == "__main__":
    PaperReviewStep.cli_main("06_review")
