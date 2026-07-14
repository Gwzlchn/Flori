"""概念提取与摘要步骤,四类内容复用同一来源快照."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from shared.concept_evidence import attach_concept_source_segments
from shared.errors import InputInvalidError
from shared.note_text import markdown_to_index_text
from shared.step_base import StepBase


@dataclass(frozen=True)
class _ConceptSource:
    text: str
    raw: bytes
    kind: str
    sha256: str
    path: str
    note_type: str | None
    source_manifest_data: bytes | None
    provenance_data: bytes | None


def _sha256(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


class ArticleConceptsStep(StepBase):
    def _pipeline(self) -> str:
        step = self.config.get("step") or {}
        pipeline = step.get("pipeline")
        if isinstance(pipeline, str) and pipeline:
            return pipeline
        try:
            job = self.artifacts.load_json("job.json")
        except (OSError, ValueError, TypeError):
            job = {}
        if isinstance(job, dict):
            pipeline = job.get("pipeline") or job.get("content_type")
            if isinstance(pipeline, str) and pipeline:
                return pipeline
        raise InputInvalidError("concepts pipeline identity is missing")

    def _read_text(
        self,
        path: Path,
        rel: str,
        *,
        kind: str,
        note_type: str | None,
    ) -> _ConceptSource:
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise InputInvalidError(f"concept source is unreadable: {rel}") from exc
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise InputInvalidError(f"concept source is not UTF-8: {rel}") from exc
        if not text:
            raise InputInvalidError(f"concept source is empty: {rel}")
        source_manifest_data = self._read_optional_bytes(
            self.job_dir / "intermediate" / "source_segments.json",
        )
        provenance_data = self._read_optional_bytes(
            self.job_dir / "output" / "provenance" / f"{note_type}.json",
        ) if note_type else None
        return _ConceptSource(
            text=text,
            raw=raw,
            kind=kind,
            sha256=_sha256(raw),
            path=rel,
            note_type=note_type,
            source_manifest_data=source_manifest_data,
            provenance_data=provenance_data,
        )

    @staticmethod
    def _read_optional_bytes(path: Path) -> bytes | None:
        try:
            return path.read_bytes()
        except OSError:
            return None

    def _resolve_concept_source(self) -> _ConceptSource | None:
        if hasattr(self, "_concept_source_snapshot"):
            return self._concept_source_snapshot

        pipeline = self._pipeline()
        if pipeline not in {"video", "audio", "article", "paper"}:
            raise InputInvalidError(f"unsupported concepts pipeline: {pipeline}")

        smart = self.artifacts.latest_smart_note()
        if smart is not None:
            rel = str(smart.relative_to(self.job_dir))
            source = self._read_text(
                smart, rel, kind="smart_note", note_type="smart",
            )
            self._concept_source_snapshot = source
            return source

        if pipeline in {"video", "audio"}:
            self._concept_source_snapshot = None
            return None
        translated = self.job_dir / "output" / "translated.md"
        if translated.is_file():
            source = self._read_text(
                translated,
                "output/translated.md",
                kind="translation",
                note_type="translated",
            )
            self._concept_source_snapshot = source
            return source

        original = self.job_dir / "output" / "original.md"
        if original.is_file():
            source = self._read_text(
                original,
                "output/original.md",
                kind="original",
                note_type="original",
            )
            self._concept_source_snapshot = source
            return source

        sections_path = self.job_dir / "intermediate" / "sections.json"
        if not sections_path.is_file():
            self._concept_source_snapshot = None
            return None
        source = self._read_text(
            sections_path,
            "intermediate/sections.json",
            kind="original",
            note_type=None,
        )
        try:
            sections = json.loads(source.text)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise InputInvalidError("concept sections source is invalid") from exc
        if not isinstance(sections, dict):
            raise InputInvalidError("concept sections source is invalid")
        parts: list[str] = []
        if sections.get("title"):
            parts.append(f"# {sections['title']}\n")
        if sections.get("abstract"):
            parts.append(str(sections["abstract"]) + "\n")
        from steps.utils.sections import render_section_tree
        for section in sections.get("sections", []):
            render_section_tree(section, parts, level=2)
        rendered = "".join(parts)
        if not rendered:
            raise InputInvalidError("concept sections source is empty")
        resolved = _ConceptSource(
            text=rendered,
            raw=rendered.encode("utf-8"),
            kind="original",
            sha256=_sha256(rendered.encode("utf-8")),
            path=source.path,
            note_type=None,
            source_manifest_data=source.source_manifest_data,
            provenance_data=None,
        )
        self._concept_source_snapshot = resolved
        return resolved

    def validate_inputs(self) -> list[str]:
        if self._resolve_concept_source() is not None:
            return []
        if self._pipeline() in {"video", "audio"}:
            return ["output/versions/notes_smart_*.md"]
        return ["intermediate/sections.json"]

    def input_hashes(self) -> dict[str, str]:
        source = self._resolve_concept_source()
        if source is None:
            return {}
        hashes = {
            "source": source.kind,
            "source_hash": source.sha256,
            "source_path": source.path,
            "evidence_note_type": source.note_type or "none",
            "source_manifest_hash": (
                _sha256(source.source_manifest_data)
                if source.source_manifest_data is not None else "missing"
            ),
            "provenance_hash": (
                _sha256(source.provenance_data)
                if source.provenance_data is not None else "missing"
            ),
        }
        hashes.update(self.ai.prompt_profile_style_hashes())
        return hashes

    def execute(self) -> dict | None:
        source = self._resolve_concept_source()
        if source is None:
            raise InputInvalidError("concept source is missing")
        prompt = self._build_prompt(source.text)
        result, parse_failed = self.ai.call_json(
            prompt, fallback={"summary": "", "key_terms": []},
        )
        key_terms = attach_concept_source_segments(
            result.get("key_terms") or [],
            job_id=self.job_dir.name,
            pipeline=self._pipeline(),
            note_type=source.note_type,
            note_path=source.path,
            note_bytes=source.raw,
            normalized_body=markdown_to_index_text(source.text),
            source_manifest_path="intermediate/source_segments.json",
            source_manifest_data=source.source_manifest_data,
            provenance_path=(
                f"output/provenance/{source.note_type}.json"
                if source.note_type else None
            ),
            provenance_data=source.provenance_data,
        )
        out = {
            "summary": (result.get("summary") or "").strip(),
            "key_terms": key_terms,
            "source": source.kind,
            "evidence_note_type": source.note_type,
            "parse_failed": parse_failed,
        }
        self.artifacts.write("output/concepts.json", out)
        return {
            "concepts": len(key_terms),
            "source": source.kind,
            "source_path": source.path,
            "evidence_note_type": source.note_type,
            "summary_len": len(out["summary"]),
            "parse_failed": parse_failed,
            "provider": self.ai.last_provider,
            "model": self.ai.last_model,
        }

    def _build_prompt(self, text: str) -> str:
        profile = self.ai.load_domain_prompt_profile()
        parts = [self.ai.load_prompt_template(self.ai.primary_prompt_template())]
        parts.append(self.ai.terminology_block(profile))
        parts.append("\n--- 内容 ---\n")
        parts.append(text[:12000])
        return "".join(parts)


if __name__ == "__main__":
    ArticleConceptsStep.cli_main("05_concepts")
