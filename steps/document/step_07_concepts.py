"""只从 Document 智能笔记、译文或结构块提取概念。"""

from __future__ import annotations

from shared.document_contract import validate_document, validate_translation
from shared.errors import InputInvalidError
from steps.common.step_concepts import (
    ConceptsStep,
    _ConceptSource,
    _sha256,
)


class DocumentConceptsStep(ConceptsStep):
    def _pipeline(self) -> str:
        return "document"

    def _resolve_concept_source(self) -> _ConceptSource | None:
        if hasattr(self, "_concept_source_snapshot"):
            return self._concept_source_snapshot
        smart = self.artifacts.latest_smart_note()
        if smart is not None:
            source = self._read_text(
                smart,
                str(smart.relative_to(self.job_dir)),
                kind="smart_note",
                note_type="smart",
            )
            self._concept_source_snapshot = source
            return source
        translation_path = self.job_dir / "output" / "translation.json"
        if translation_path.is_file():
            translation = validate_translation(
                self.artifacts.load_json("output/translation.json"),
                expected_job_id=self.job_dir.name,
            )
            text = "\n\n".join(item["text"] for item in translation["segments"])
            raw = text.encode("utf-8")
            source = _ConceptSource(
                text=text,
                raw=raw,
                kind="translation",
                sha256=_sha256(raw),
                path="output/translated.html",
                note_type="translated",
                source_manifest_data=self._read_optional_bytes(
                    self.job_dir / "intermediate" / "source_segments.json",
                ),
                provenance_data=self._read_optional_bytes(
                    self.job_dir / "output" / "provenance" / "translated.json",
                ),
            )
            self._concept_source_snapshot = source
            return source
        document_path = self.job_dir / "intermediate" / "document.json"
        if not document_path.is_file():
            self._concept_source_snapshot = None
            return None
        document = validate_document(
            self.artifacts.load_json("intermediate/document.json"),
            expected_job_id=self.job_dir.name,
        )
        text = "\n\n".join(
            str(item.get("text") or "")
            for item in sorted(document["blocks"], key=lambda value: value["order"])
            if str(item.get("text") or "").strip()
        )
        if not text:
            raise InputInvalidError("document concept source is empty")
        raw = text.encode("utf-8")
        source = _ConceptSource(
            text=text,
            raw=raw,
            kind="document",
            sha256=_sha256(raw),
            path="intermediate/document.json",
            note_type=None,
            source_manifest_data=self._read_optional_bytes(
                self.job_dir / "intermediate" / "source_segments.json",
            ),
            provenance_data=None,
        )
        self._concept_source_snapshot = source
        return source

    def validate_inputs(self) -> list[str]:
        return [] if self._resolve_concept_source() is not None else [
            "intermediate/document.json"
        ]


if __name__ == "__main__":
    DocumentConceptsStep.cli_main("07_concepts")
