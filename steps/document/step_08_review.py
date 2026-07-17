"""以完整 Document、Translation 和质量报告评审智能笔记。"""

from __future__ import annotations

from shared.document_contract import validate_document, validate_quality, validate_translation
from shared.review_contract import source_record
from shared.step_base import StepBase, file_hash


class DocumentReviewStep(StepBase):
    def validate_inputs(self) -> list[str]:
        missing = []
        if self.artifacts.latest_smart_note() is None:
            missing.append("output/versions/notes_smart_*.md")
        for path in ("intermediate/document.json", "intermediate/quality.json"):
            if not (self.job_dir / path).is_file():
                missing.append(path)
        return missing

    def input_hashes(self) -> dict[str, str]:
        smart = self.artifacts.latest_smart_note()
        hashes = {
            "smart": file_hash(smart) if smart else "",
            "document": file_hash(self.job_dir / "intermediate/document.json"),
            "quality": file_hash(self.job_dir / "intermediate/quality.json"),
            "provider": self.ai.override_provider(),
        }
        translation = self.job_dir / "output" / "translation.json"
        if translation.is_file():
            hashes["translation"] = file_hash(translation)
        hashes["template"] = self.ai.template_hash(self.ai.primary_prompt_template())
        return hashes

    def execute(self) -> dict:
        validate_document(
            self.artifacts.load_json("intermediate/document.json"),
            expected_job_id=self.job_dir.name,
        )
        validate_quality(
            self.artifacts.load_json("intermediate/quality.json"),
            expected_job_id=self.job_dir.name,
        )
        smart_clip, coverage, note_file, smart_source = self.review.prepare_smart()
        source_paths = [
            ("intermediate/document.json", "document"),
            ("intermediate/quality.json", "quality"),
        ]
        if (self.job_dir / "output" / "translation.json").is_file():
            validate_translation(
                self.artifacts.load_json("output/translation.json"),
                expected_job_id=self.job_dir.name,
            )
            source_paths.append(("output/translation.json", "translation"))
        records = []
        source_texts = {}
        blocks = []
        for path, label in source_paths:
            text, record = source_record(self.job_dir, path, label=label)
            records.append(record)
            source_texts[label] = text
            blocks.append(f"--- {label} 全文 ---\n{text}")
        dimensions = [
            ("completeness", "覆盖来源中的核心内容与重要图表"),
            ("accuracy", "事实、数字、条件和结论与来源一致"),
            ("structure", "层级与论证关系清晰"),
            ("terminology", "术语稳定且定义准确"),
            ("formula_integrity", "公式与符号未被改写或误解释"),
            ("visual_references", "Figure/Table 引用连续且可回到来源"),
            ("traceability", "重要主张具有可核验来源 locator"),
        ]
        prompt = self.review.build_prompt(
            intro="请对以下 Document 智能笔记进行质量评审。",
            dimensions=dimensions,
            ref_block="\n\n".join(blocks) + f"\n\n--- 笔记 ---\n{smart_clip}",
        )
        score_keys = [key for key, _ in dimensions]
        review, parse_failed = self.review.run_dimension(
            prompt,
            fallback=self.review.fallback(score_keys),
            score_keys=score_keys,
            note_file=note_file,
            coverage=coverage,
            review_sources=[smart_source, *records],
            review_source_texts={"smart": smart_clip, **source_texts},
        )
        return {
            "overall": review.get("overall", 0),
            "parse_failed": parse_failed,
            "note_file": note_file,
            "coverage_truncated": coverage["truncated"],
        }


if __name__ == "__main__":
    DocumentReviewStep.cli_main("08_review")
