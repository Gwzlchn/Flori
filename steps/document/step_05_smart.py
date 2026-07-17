"""从 Document/Translation 真相源生成确定性命名的中文智能笔记。"""

from __future__ import annotations

import re

from shared.document_contract import validate_document, validate_translation
from shared.step_base import StepBase, file_hash
from steps.document.provenance import (
    extract_attestable_document_markers,
    load_document_source_manifest,
    persist_document_note_provenance,
)
from steps.utils.provenance_attestation import persist_semantic_candidates


class DocumentSmartStep(StepBase):
    def validate_inputs(self) -> list[str]:
        return [
            path for path in (
                "intermediate/document.json",
                "intermediate/source_segments.json",
            )
            if not (self.job_dir / path).is_file()
        ]

    def input_hashes(self) -> dict[str, str]:
        hashes = {
            "document": file_hash(self.job_dir / "intermediate/document.json"),
            "source_segments": file_hash(
                self.job_dir / "intermediate/source_segments.json"
            ),
        }
        translation = self.job_dir / "output" / "translation.json"
        if translation.is_file():
            hashes["translation"] = file_hash(translation)
        hashes.update(self.ai.prompt_profile_style_hashes())
        return hashes

    def execute(self) -> dict:
        document = validate_document(
            self.artifacts.load_json("intermediate/document.json"),
            expected_job_id=self.job_dir.name,
        )
        source_manifest = load_document_source_manifest(self.job_dir)
        if source_manifest is None:
            raise ValueError("document smart note requires source manifest")
        body, body_source, zh_title = self._body(document)
        prompt = self._build_prompt(document, body, source_manifest)
        result = self.ai.call(prompt, max_tokens=8192)
        result, exact, semantic = extract_attestable_document_markers(
            result, source_manifest, ai=self.ai,
        )
        result = self._strip_model_title(result)
        note_title = f"{zh_title} - 笔记"
        rel = self.review.write_smart_note(result, title=note_title)
        provenance = persist_document_note_provenance(
            self.job_dir,
            note_type="smart",
            note_artifact=rel,
            candidates=exact,
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
            "provider": self.ai.last_provider,
            "model": self.ai.last_model,
            "provenance_segments": provenance["segments"],
            "provenance_status": provenance["status"],
            "semantic_candidates": candidate_state["candidates"],
        }

    def _body(self, document: dict) -> tuple[str, str, str]:
        metadata = document.get("metadata") or {}
        titles = metadata.get("titles") or {}
        translation_path = self.job_dir / "output" / "translation.json"
        if translation_path.is_file():
            translation = validate_translation(
                self.artifacts.load_json("output/translation.json"),
                expected_job_id=self.job_dir.name,
            )
            lines = [
                f"[{item['source_segment_ids'][0]}] {item['text']}"
                for item in translation["segments"]
            ]
            title_segment = next(
                (item for item in translation["segments"] if item["kind"] == "title"),
                None,
            )
            zh_title = str(
                titles.get("zh")
                or (title_segment or {}).get("text")
                or titles.get("original")
                or "未命名文档"
            )
            return "\n\n".join(lines), "translation", zh_title
        lines = [
            f"[{item['block_id']}] {item.get('text', '')}"
            for item in sorted(document["blocks"], key=lambda value: value["order"])
            if str(item.get("text") or "").strip()
        ]
        title = str(titles.get("zh") or titles.get("original") or "未命名文档")
        return "\n\n".join(lines), "document", title

    def _build_prompt(
        self, document: dict, body: str, source_manifest: dict,
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
            .replace("<<BODY>>", body)
            .replace("<<VISUALS>>", "\n".join(visual_lines) or "无")
            + self.ai.terminology_block(self.ai.load_domain_prompt_profile())
            + references
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
