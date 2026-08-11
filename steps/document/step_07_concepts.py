"""从智能笔记提取概念,再用原始 Document 来源段确定性绑定证据。"""

from __future__ import annotations

from shared.concept_evidence import validate_source_concept_evidence_snapshot
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
        manifest_path = self.job_dir / "intermediate" / "source_segments.json"
        if smart is None or not manifest_path.is_file():
            self._concept_source_snapshot = None
            return None
        try:
            raw = smart.read_bytes()
            text = raw.decode("utf-8")
            manifest_data = manifest_path.read_bytes()
            snapshot = validate_source_concept_evidence_snapshot(
                job_id=self.job_dir.name,
                pipeline="document",
                source_manifest_data=manifest_data,
            )
        except (OSError, UnicodeDecodeError, TypeError, ValueError) as exc:
            raise InputInvalidError("document concept evidence source is invalid") from exc
        if not text.strip():
            raise InputInvalidError("document smart note is empty")
        source = _ConceptSource(
            text=text,
            raw=raw,
            kind="smart_note",
            sha256=_sha256(raw),
            path=str(smart.relative_to(self.job_dir)),
            note_type="original",
            source_manifest_data=manifest_data,
            provenance_data=None,
            evidence_snapshot=snapshot,
        )
        self._concept_source_snapshot = source
        return source

    def validate_inputs(self) -> list[str]:
        return [] if self._resolve_concept_source() is not None else [
            "output/versions/notes_smart_*.md",
            "intermediate/source_segments.json",
        ]


if __name__ == "__main__":
    DocumentConceptsStep.cli_main("07_concepts")
