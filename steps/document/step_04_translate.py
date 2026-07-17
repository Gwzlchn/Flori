"""按 Document block 翻译并发布一一对齐的译文真相源。"""

from __future__ import annotations

from shared.document_contract import (
    TRANSLATION_SCHEMA_VERSION,
    primary_document_source,
    validate_document,
    validate_quality,
    validate_translation,
)
from shared.errors import InputInvalidError
from shared.note_text import markdown_to_index_text
from shared.provenance import MAX_SEMANTIC_CANDIDATES
from shared.step_base import StepBase, file_hash
from steps.document.provenance import (
    load_document_source_manifest,
    persist_document_note_provenance,
)
from steps.document.translation import (
    materialize_translation_segments,
    render_translated_html,
    translation_batches,
    translation_prompt_payload,
    translation_units,
    validate_batch_response,
)
from steps.utils.provenance_attestation import (
    persist_semantic_candidates,
    producer_invocation_id,
)


BATCH_CHARS = 12000


class DocumentTranslateStep(StepBase):
    def validate_inputs(self) -> list[str]:
        return [
            path for path in (
                "intermediate/document.json",
                "intermediate/quality.json",
                "intermediate/source_segments.json",
            )
            if not (self.job_dir / path).is_file()
        ]

    def input_hashes(self) -> dict[str, str]:
        hashes = {
            "document": file_hash(self.job_dir / "intermediate/document.json"),
            "quality": file_hash(self.job_dir / "intermediate/quality.json"),
            "source_segments": file_hash(
                self.job_dir / "intermediate/source_segments.json"
            ),
        }
        template = self.ai.template_hash(self.ai.primary_prompt_template())
        if template:
            hashes["template"] = template
        term_map = self.job_dir / "input" / "term_map.json"
        if term_map.is_file():
            hashes["term_map"] = file_hash(term_map)
        return hashes

    def execute(self) -> dict:
        document = validate_document(
            self.artifacts.load_json("intermediate/document.json"),
            expected_job_id=self.job_dir.name,
        )
        quality = validate_quality(
            self.artifacts.load_json("intermediate/quality.json"),
            expected_job_id=self.job_dir.name,
        )
        if quality["status"] == "rejected":
            raise InputInvalidError("rejected document cannot be translated")
        units = translation_units(document)
        if not units:
            raise InputInvalidError("document contains no translatable blocks")
        batches = translation_batches(units, max_chars=BATCH_CHARS)
        translated_fragments = [item for batch in batches for item in batch]
        translated: dict[str, str] = {}
        invocation_ids: dict[str, str | None] = {}
        for index, batch in enumerate(batches):
            self.progress.report(index, len(batches), f"translating batch {index + 1}/{len(batches)}")
            response = self._translate_batch(batch)
            invocation_id = producer_invocation_id(self.ai)
            translated.update(response)
            invocation_ids.update({
                item["translation_request_id"]: invocation_id for item in batch
            })
        self.progress.report(len(batches), len(batches), "done")

        segments = materialize_translation_segments(
            units, translated, invocation_ids,
            translated_fragments=translated_fragments,
        )
        translated_count = sum(
            item["transform_kind"] == "translated" for item in segments
        )
        artifact = {
            "schema_version": TRANSLATION_SCHEMA_VERSION,
            "job_id": self.job_dir.name,
            "source_fingerprint": primary_document_source(document)["fingerprint"],
            "source_lang": str((document.get("metadata") or {}).get("lang") or "unknown"),
            "target_lang": "zh",
            "status": "complete",
            "coverage": {
                "source_segments": len({
                    source_id
                    for segment in segments
                    for source_id in segment["source_segment_ids"]
                }),
                "translated_segments": translated_count,
                "passthrough_segments": len(segments) - translated_count,
            },
            "attestation": {},
            "segments": segments,
        }
        html = render_translated_html(document, segments)
        normalized = markdown_to_index_text(html)
        candidates, candidate_metrics = self._semantic_candidates(
            segments, normalized,
        )
        artifact["attestation"] = candidate_metrics
        if candidate_metrics["status"] != "complete":
            artifact["status"] = "degraded"
        artifact = validate_translation(artifact, expected_job_id=self.job_dir.name)
        self.artifacts.write("output/translation.json", artifact)
        self.artifacts.write("output/translated.html", html)
        provenance = persist_document_note_provenance(
            self.job_dir,
            note_type="translated",
            note_artifact="output/translated.html",
            candidates=[],
        )
        candidate_state = persist_semantic_candidates(
            self.job_dir,
            pipeline="document",
            note_type="translated",
            note_artifact="output/translated.html",
            candidates=candidates,
        )
        return {
            "segments": len(segments),
            "translated_segments": translated_count,
            "batches": len(batches),
            "status": artifact["status"],
            "provenance_status": provenance["status"],
            "semantic_candidates": candidate_state["candidates"],
            "provider": self.ai.last_provider,
            "model": self.ai.last_model,
        }

    def _translate_batch(self, batch: list[dict]) -> dict[str, str]:
        template = self.ai.load_prompt_template(self.ai.primary_prompt_template())
        prompt = template.replace("<<INPUT>>", translation_prompt_payload(batch))
        last_error: Exception | None = None
        for _attempt in range(2):
            result, parse_failed = self.ai.call_json(
                prompt,
                fallback={"segments": []},
                max_tokens=16384,
            )
            if parse_failed:
                last_error = ValueError("translation response is not JSON")
                continue
            try:
                return validate_batch_response(batch, result)
            except ValueError as exc:
                last_error = exc
        raise InputInvalidError(f"translation validation failed: {last_error}")

    def _semantic_candidates(
        self, segments: list[dict], normalized_body: str,
    ) -> tuple[list[dict], dict]:
        source_manifest = load_document_source_manifest(self.job_dir)
        known = {item["segment_id"] for item in source_manifest["segments"]} if source_manifest else set()
        eligible = []
        cursor = 0
        for segment in segments:
            if segment["transform_kind"] != "translated":
                continue
            source_id = segment["source_segment_ids"][0]
            invocation_id = segment.get("producer_invocation_id")
            anchor = markdown_to_index_text(str(segment["text"])).strip()
            if (
                source_id not in known
                or not invocation_id
                or not anchor
            ):
                continue
            position = normalized_body.find(anchor, cursor)
            if position < 0:
                continue
            prefix = normalized_body[max(0, position - 24):position]
            suffix = normalized_body[
                position + len(anchor):position + len(anchor) + 24
            ]
            matches = 0
            offset = 0
            while True:
                found = normalized_body.find(anchor, offset)
                if found < 0:
                    break
                if (
                    normalized_body[:found].endswith(prefix)
                    and normalized_body[found + len(anchor):].startswith(suffix)
                ):
                    matches += 1
                offset = found + 1
            if matches != 1:
                continue
            cursor = position + len(anchor)
            eligible.append({
                "anchor": anchor,
                "prefix": prefix,
                "suffix": suffix,
                "section": str(segment.get("parent_id") or source_id),
                "source_segment_id": source_id,
                "transform_kind": "translated",
                "producer_component": self.ai.step_name,
                "producer_invocation_id": invocation_id,
            })
        candidates = eligible[:MAX_SEMANTIC_CANDIDATES]
        translated_total = sum(
            item["transform_kind"] == "translated" for item in segments
        )
        if len(candidates) == translated_total:
            status = "complete"
            reason = None
        elif len(eligible) > MAX_SEMANTIC_CANDIDATES:
            status = "degraded"
            reason = "semantic_candidate_limit"
        else:
            status = "degraded"
            reason = "semantic_candidate_unavailable_or_ambiguous"
        return candidates, {
            "status": status,
            "eligible_segments": len(eligible),
            "candidate_segments": len(candidates),
            "reason": reason,
        }


if __name__ == "__main__":
    DocumentTranslateStep.cli_main("04_translate")
