"""只从原始 Document 真相源生成确定性命名的中文智能笔记。"""

from __future__ import annotations

import json
import re

from shared.document_contract import (
    MAX_QUALITY_JSON_BYTES,
    validate_document,
    validate_quality,
)
from shared.errors import InputInvalidError
from shared.step_base import StepBase, file_hash
from steps.document.provenance import (
    extract_attestable_document_markers,
    load_document_source_manifest,
    persist_document_note_provenance,
)
from steps.utils.provenance_attestation import persist_semantic_candidates


MAX_DOCUMENT_SMART_PROMPT_BYTES = 4 * 1024 * 1024
_QUALITY_NOTE_METRIC_KEYS = (
    "pdf_source_quality",
    "pdf_crosswalk_blocks",
    "pdf_crosswalk_visuals",
    "pdf_crosswalk_ambiguous",
    "pdf_crosswalk_visual_ambiguous",
    "pdf_layout_detector_failures",
    "html_visual_asset_failures",
    "visual_asset_failures",
)


class DocumentSmartStep(StepBase):
    def validate_inputs(self) -> list[str]:
        return [
            path for path in (
                "intermediate/document.json",
                "intermediate/quality.json",
                "intermediate/source_segments.json",
            )
            if not (self.job_dir / path).is_file()
        ]

    def step_input_hashes(self) -> dict[str, str]:
        hashes = {
            "document": file_hash(self.job_dir / "intermediate/document.json"),
            "quality": file_hash(self.job_dir / "intermediate/quality.json"),
            "source_segments": file_hash(
                self.job_dir / "intermediate/source_segments.json"
            ),
        }
        hashes.update(self.ai.prompt_profile_style_hashes())
        return hashes

    def execute(self) -> dict:
        document = validate_document(
            self.artifacts.load_json("intermediate/document.json"),
            expected_job_id=self.job_dir.name,
        )
        with (self.job_dir / "intermediate/quality.json").open("rb") as handle:
            quality_data = handle.read(MAX_QUALITY_JSON_BYTES + 1)
        if len(quality_data) > MAX_QUALITY_JSON_BYTES:
            raise InputInvalidError("document quality exceeds byte limit")
        quality = validate_quality(
            json.loads(quality_data),
            expected_job_id=self.job_dir.name,
        )
        if quality["status"] == "rejected":
            raise InputInvalidError("document smart note rejects rejected source quality")
        source_manifest = load_document_source_manifest(self.job_dir)
        if source_manifest is None:
            raise ValueError("document smart note requires source manifest")
        body, body_source, zh_title = self._body(document)
        prompt = self._build_prompt(document, quality, body, source_manifest)
        result, exact, semantic = self._generate_attestable_note(
            prompt, source_manifest,
        )
        result = self._strip_model_title(result)
        quality_notice = self._quality_notice(quality)
        if quality_notice:
            result = f"{quality_notice}\n\n{result}"
        note_title = f"{zh_title} - 笔记"
        rel = self.review.write_smart_note(result, title=note_title)
        provenance = persist_document_note_provenance(
            self.job_dir,
            note_type="smart",
            note_artifact=rel,
            candidates=exact,
            provenance_dir="output/provenance_exact",
        )
        candidate_state = persist_semantic_candidates(
            self.job_dir,
            pipeline="document",
            note_type="smart",
            note_artifact=rel,
            candidates=semantic,
        )
        return {
            "chars": len(result),
            "note_file": rel,
            "title": note_title,
            "source": body_source,
            "quality_status": quality["status"],
            "quality_disclosed": bool(quality_notice),
            "provider": self.ai.last_provider,
            "model": self.ai.last_model,
            "provenance_segments": provenance["segments"],
            "provenance_status": provenance["status"],
            "semantic_candidates": candidate_state["candidates"],
        }

    def _generate_attestable_note(
        self, prompt: str, source_manifest: dict,
    ) -> tuple[str, list[dict], list[dict]]:
        """生成并验证来源 marker;首轮失败时把精确错误反馈给唯一一次重试。"""
        attempt_prompt = prompt
        last_error: ValueError | None = None
        for _attempt in range(2):
            result = self._call_with_prompt_limit(attempt_prompt)
            try:
                return extract_attestable_document_markers(
                    result, source_manifest, ai=self.ai,
                )
            except ValueError as exc:
                last_error = exc
                feedback = json.dumps(
                    {"validation_error": str(exc)},
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                attempt_prompt = (
                    f"{prompt}\n\n"
                    "上一次笔记未通过确定性来源校验。重新生成完整笔记，不要只返回局部修正。"
                    "每个[[source:ID]]在整篇输出中最多出现一次，不得编造或改写ID。"
                    f"校验反馈={feedback}"
                )
        assert last_error is not None
        raise last_error

    def _call_with_prompt_limit(self, prompt: str) -> str:
        if len(prompt.encode("utf-8")) > MAX_DOCUMENT_SMART_PROMPT_BYTES:
            raise InputInvalidError("document smart prompt exceeds byte limit")
        return self.ai.call(prompt, max_tokens=8192)

    def _body(self, document: dict) -> tuple[str, str, str]:
        metadata = document.get("metadata") or {}
        titles = metadata.get("titles") or {}
        lines = [
            f"[{item['block_id']}] {item.get('text', '')}"
            for item in sorted(document["blocks"], key=lambda value: value["order"])
            if str(item.get("text") or "").strip()
        ]
        title = str(titles.get("zh") or titles.get("original") or "未命名文档")
        return "\n\n".join(lines), "document", title

    def _build_prompt(
        self, document: dict, quality: dict, body: str, source_manifest: dict,
    ) -> str:
        metadata = document.get("metadata") or {}
        titles = metadata.get("titles") or {}
        references = self._source_reference_block(source_manifest)
        visual_lines = [
            f"- {item.get('figure_id')} {item.get('label')}: {item.get('caption', '')}"
            for item in document.get("figures", [])
        ] + [
            f"- {item.get('table_id')} {item.get('label')}: {item.get('caption', '')}"
            for item in document.get("tables", [])
        ]
        template = self.ai.load_prompt_template(self.ai.primary_prompt_template())
        return (
            template
            .replace("<<DOCUMENT_KIND>>", str(document["document_kind"]))
            .replace("<<TITLE>>", str(titles.get("original") or "未命名文档"))
            .replace("<<QUALITY>>", self._quality_prompt_block(quality))
            .replace("<<BODY>>", body)
            .replace("<<VISUALS>>", "\n".join(visual_lines) or "无")
            + self.ai.terminology_block(self.ai.load_domain_prompt_profile())
            + references
        )

    @staticmethod
    def _quality_prompt_block(quality: dict) -> str:
        return json.dumps(
            {
                "status": quality["status"],
                "reasons": quality["reasons"],
                "metrics": {
                    key: quality["metrics"][key]
                    for key in _QUALITY_NOTE_METRIC_KEYS if key in quality["metrics"]
                },
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    def _quality_notice(quality: dict) -> str:
        if quality["status"] == "complete":
            return ""
        reasons = "、".join(quality["reasons"])
        metrics = "、".join(
            f"{key}={quality['metrics'][key]}"
            for key in _QUALITY_NOTE_METRIC_KEYS
            if key in quality["metrics"]
        )
        detail = f"；相关指标：{metrics}" if metrics else ""
        return (
            f"> 来源质量提示：结构化解析状态为 {quality['status']}；"
            f"已知限制代码：{reasons}{detail}。"
            "涉及公式、图表、页码与定位的结论需回到原始 HTML/PDF 核验。"
        )

    @staticmethod
    def _source_reference_block(source_manifest: dict) -> str:
        lines = [
            "\n--- 可引用来源坐标 ---\n",
            "引用事实时在相关句末保留一个 [[source:ID]]。只能使用下列 ID，"
            "不得编造或重复；内部标记落盘前会移除。\n",
        ]
        for segment in source_manifest["segments"]:
            support = segment.get("support_text")
            if not isinstance(support, str) or not support.strip():
                continue
            token = str(segment["segment_id"]).removeprefix("seg_")
            excerpt = re.sub(r"\s+", " ", support).strip().replace("[[source:", "[source:")
            lines.append(f"[[source:{token}]] {excerpt}\n")
        return "".join(lines)

    @staticmethod
    def _strip_model_title(value: str) -> str:
        lines = value.strip().splitlines()
        while lines and not lines[0].strip():
            lines.pop(0)
        if lines and re.match(r"^#\s+", lines[0]):
            lines.pop(0)
            while lines and not lines[0].strip():
                lines.pop(0)
        return "\n".join(lines).strip()


if __name__ == "__main__":
    DocumentSmartStep.cli_main("05_smart")
