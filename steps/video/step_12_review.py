"""Step 12: 质量评审。6 维度评分 + 缺失概念 + 改进建议。评最新版智能笔记,review.json 标 note_file。"""

from __future__ import annotations

import json

from shared.evidence_contract import (
    validate_citations_from_loaded,
    validate_manifest_loaded,
)
from shared.review_contract import source_record, source_record_from_data
from shared.step_base import StepBase, file_hash


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
        ev = self.job_dir / "output" / "evidence.json"
        return {
            "smart": file_hash(smart) if smart else "",
            "mechanical": file_hash(self.job_dir / "output" / "notes_mechanical.md"),
            # 取证产物纳入指纹:evidence 更新→重评(核 [E#] 忠实性)。非案例类无则空。
            "evidence": file_hash(ev) if ev.exists() else "",
            # provider 覆盖纳入指纹:换 provider 重跑时强制重评。
            "provider": self.override_provider(),
        }

    def _evidence_for_review(
        self, smart: str | None = None, mechanical: str | None = None,
    ) -> tuple[str, str, list[dict], dict[str, str], dict, dict | None]:
        """只把通过 v2 manifest 校验的证据全文送评并返回引用核验结果。"""
        if smart is None:
            smart_path = self.latest_smart_note()
            if smart_path is None:
                smart = ""
            else:
                rel = str(smart_path.relative_to(self.job_dir))
                smart, _ = source_record(self.job_dir, rel, label="smart")
        p = self.job_dir / "output" / "evidence.json"
        if not p.exists():
            citation = validate_citations_from_loaded(smart, {}, {}, [])
            return "", "", [], {}, citation, None
        manifest_text, manifest_record = source_record(
            self.job_dir, "output/evidence.json", label="evidence_manifest",
        )
        try:
            manifest = json.loads(manifest_text)
        except (ValueError, OSError):
            return "", "", [], {}, {
                "status": "invalid", "checked": 0, "items": [],
                "manifest_errors": ["manifest_parse_failed"],
            }, manifest_record
        if not isinstance(manifest, dict):
            return "", "", [], {}, {
                "status": "invalid", "checked": 0, "items": [],
                "manifest_errors": ["legacy_or_invalid_schema"],
            }, manifest_record
        valid, manifest_errors, loaded_texts = validate_manifest_loaded(
            self.job_dir, self.job_dir.name, manifest, mechanical_text=mechanical,
        )
        lines = ["\n\n--- 权威来源全文(受控取证) ---"]
        sources = []
        source_texts = {}
        for evidence_id, item in valid.items():
            text = loaded_texts[evidence_id]
            _, record = source_record_from_data(
                text.encode("utf-8"), item["artifact"], label=evidence_id,
            )
            sources.append(record)
            source_texts[evidence_id] = text
            lines.append(f"\n[{evidence_id}] {item.get('title', '')}\n{text}")
        intro_extra = ("（笔记若用 [E#] 引用了上方「权威来源」，请核对被引精确数据与该来源是否相符；"
                       "不符或列表外的臆造精确数字，在 top3_improvements 中指出。）")
        citation = validate_citations_from_loaded(
            smart, valid, loaded_texts, manifest_errors,
        )
        return (
            "\n".join(lines) if valid else "", intro_extra, sources, source_texts,
            citation, manifest_record,
        )

    def execute(self) -> dict | None:
        mechanical, mechanical_source = source_record(
            self.job_dir, "output/notes_mechanical.md", label="mechanical",
        )
        smart_clip, coverage, note_file, smart_source = self.prepare_smart_for_review()

        dimensions = [
            ("completeness", "信息完整性（是否遗漏重要内容）"),
            ("accuracy", "准确性（是否有事实错误）"),
            ("structure", "结构清晰度"),
            ("terminology", "术语使用准确性"),
            ("visual_integration", "截图引用恰当性"),
            ("readability", "可读性"),
        ]
        (
            ev_ref, intro_extra, evidence_sources, evidence_texts, citation,
            evidence_manifest_record,
        ) = self._evidence_for_review(smart_clip, mechanical)
        prompt = self.build_review_prompt(
            intro="请对比以下两份笔记，对 AI 生成的智能版笔记进行质量评审。" + intro_extra,
            dimensions=dimensions,
            ref_block=(
                f"--- 机械版笔记 ---\n{mechanical}\n\n"
                f"--- 智能版笔记 ---\n{smart_clip}" + ev_ref
            ),
        )
        score_keys = [key for key, _ in dimensions]
        review, parse_failed = self.run_dimension_review(
            prompt, fallback=self.review_fallback(score_keys), score_keys=score_keys,
            note_file=note_file, coverage=coverage,
            review_sources=[smart_source, mechanical_source, *evidence_sources],
            review_source_texts={
                "smart": smart_clip, "mechanical": mechanical, **evidence_texts,
            },
            citation_validation=citation,
            evidence_manifest_record=evidence_manifest_record,
        )
        return {"overall": review.get("overall", 0), "parse_failed": parse_failed,
                "provider": review.get("provider"), "note_file": note_file,
                "coverage_truncated": coverage["truncated"]}


if __name__ == "__main__":
    ReviewStep.cli_main("12_review")
