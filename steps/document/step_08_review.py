"""核验智能笔记证据,再以原始 Document、概念和质量报告统一评审。"""

from __future__ import annotations

import json

from shared.document_contract import canonical_quality_text, validate_document
from shared.errors import ProcessingError
from shared.review_contract import (
    MAX_DOCUMENT_REVIEW_PROMPT_BYTES,
    build_document_review_pack,
    persist_review_source,
    source_record,
)
from shared.step_base import StepBase, file_hash
from steps.utils.provenance_attestation import (
    finalize_pending_semantic_provenance,
    semantic_attestation_input_hashes,
)


class DocumentReviewStep(StepBase):
    def validate_inputs(self) -> list[str]:
        missing = []
        if self.artifacts.latest_smart_note() is None:
            missing.append("output/versions/notes_smart_*.md")
        for path in (
            "intermediate/document.json",
            "intermediate/quality.json",
            "intermediate/source_segments.json",
            "output/concepts.json",
            "output/provenance_exact/smart.json",
            "output/provenance_candidates/smart.json",
        ):
            if not (self.job_dir / path).is_file():
                missing.append(path)
        return missing

    def step_input_hashes(self) -> dict[str, str]:
        smart = self.artifacts.latest_smart_note()
        hashes = {
            "smart": file_hash(smart) if smart else "",
            "document": file_hash(self.job_dir / "intermediate/document.json"),
            "quality": file_hash(self.job_dir / "intermediate/quality.json"),
            "concepts": file_hash(self.job_dir / "output/concepts.json"),
        }
        hashes.update(semantic_attestation_input_hashes(
            self.job_dir,
            note_types=("smart",),
            exact_provenance_dir="output/provenance_exact",
        ))
        hashes["template"] = self.ai.template_hash(self.ai.primary_prompt_template())
        hashes["semantic_attestation_template"] = self.ai.template_hash(
            "semantic_attestation"
        )
        hashes["source_manifest"] = file_hash(
            self.job_dir / "intermediate/source_segments.json"
        )
        for key, path in {
            "semantic_batch_commit": self.job_dir / "output/provenance/semantic_batch.json",
            "semantic_ai_log": self.job_dir / "output/ai_logs/08_review.jsonl",
            "semantic_smart_published": self.job_dir / "output/provenance/smart.json",
        }.items():
            if path.is_file():
                hashes[key] = file_hash(path)
        return hashes

    def execute(self) -> dict:
        document = self.artifacts.load_json("intermediate/document.json")
        validate_document(document, expected_job_id=self.job_dir.name)
        quality_text = canonical_quality_text(
            self.artifacts.load_json("intermediate/quality.json"),
            expected_job_id=self.job_dir.name,
        )
        concepts = self.artifacts.load_json("output/concepts.json")
        if not isinstance(concepts, dict) or not isinstance(concepts.get("key_terms"), list):
            raise ValueError("document concepts are invalid")
        semantic = finalize_pending_semantic_provenance(
            self.job_dir,
            pipeline="document",
            ai=self.ai,
            note_types=("smart",),
            exact_provenance_dir="output/provenance_exact",
        )
        smart_clip, coverage, note_file, smart_source = self.review.prepare_smart()
        semantic_provenance = self.artifacts.load_json("output/provenance/smart.json")
        document_data = (self.job_dir / "intermediate/document.json").read_bytes()
        document_pack = build_document_review_pack(
            document, document_data, concepts, semantic_provenance,
        )
        document_text, document_record = persist_review_source(
            self.job_dir, document_pack, label="document",
        )
        quality_text, quality_record = persist_review_source(
            self.job_dir, quality_text, label="quality",
        )
        source_paths = [
            ("output/concepts.json", "concepts"),
        ]
        records = [document_record, quality_record]
        source_texts = {"document": document_text, "quality": quality_text}
        blocks = [
            f"--- document 有界审查包 ---\n{document_text}",
            f"--- quality 规范化全文 ---\n{quality_text}",
        ]
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
        if len(prompt.encode("utf-8")) > MAX_DOCUMENT_REVIEW_PROMPT_BYTES:
            raise ValueError("document review prompt exceeds provider-safe budget")
        score_keys = [key for key, _ in dimensions]
        review, parse_failed = self.review.run_dimension(
            prompt,
            fallback=self.review.fallback(score_keys),
            score_keys=score_keys,
            note_file=note_file,
            coverage=coverage,
            review_sources=[smart_source, *records],
            review_source_texts={"smart": smart_clip, **source_texts},
            prompt_byte_limit=MAX_DOCUMENT_REVIEW_PROMPT_BYTES,
            downgrade_unbound_quotes_after_retry=True,
        )
        if parse_failed or review.get("review_reliable") is not True:
            reasons = review.get("reliability_reasons")
            raise ProcessingError(
                "document review is unreliable: "
                + ",".join(str(item) for item in reasons or ["schema_invalid"])
            )
        return {
            "overall": review.get("overall", 0),
            "parse_failed": parse_failed,
            "note_file": note_file,
            "coverage_truncated": coverage["truncated"],
            "document_pack_truncated": json.loads(document_pack)["coverage"]["truncated"],
            "semantic_accepted": semantic["accepted"],
            "semantic_rejected": semantic["rejected"],
            "semantic_budget_rejected": semantic["budget_rejected"],
        }


if __name__ == "__main__":
    DocumentReviewStep.cli_main("08_review")
