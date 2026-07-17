"""按真实媒介能力选择 adapter 并原子发布统一 Document Model。"""

from __future__ import annotations

from shared.document_contract import (
    primary_document_source,
    validate_document,
    validate_quality,
)
from shared.document_registry import validate_document_kind
from shared.errors import InputInvalidError
from shared.step_base import StepBase, file_hash
from steps.utils.lang import detect_lang


class DocumentParseStep(StepBase):
    def validate_inputs(self) -> list[str]:
        missing = []
        if not (self.job_dir / "job.json").is_file():
            missing.append("job.json")
        if not any(
            (self.job_dir / "input" / name).is_file()
            for name in ("source.html", "source.pdf")
        ):
            missing.append("input/source.html|input/source.pdf")
        return missing

    def input_hashes(self) -> dict[str, str]:
        hashes = {"job": file_hash(self.job_dir / "job.json")}
        for name in ("source.html", "source.pdf", "metadata.json"):
            path = self.job_dir / "input" / name
            if path.is_file():
                hashes[name] = file_hash(path)
        return hashes

    def execute(self) -> dict:
        job = self.artifacts.load_json("job.json")
        if job.get("content_type") != "document":
            raise InputInvalidError("Document parser rejects non-document job")
        kind = validate_document_kind(job.get("document_kind"))
        html = self.job_dir / "input" / "source.html"
        pdf = self.job_dir / "input" / "source.pdf"
        profile = str(job.get("source_profile") or "")
        if html.is_file() and profile == "scholarly_html":
            from steps.document.adapters.scholarly_html import parse_scholarly_html

            document, quality = parse_scholarly_html(self.job_dir, job)
        elif html.is_file():
            from steps.document.adapters.generic_html import parse_generic_html

            document, quality = parse_generic_html(self.job_dir, job)
        elif pdf.is_file():
            from steps.document.adapters.scholarly_pdf import parse_pdf_document

            document, quality = parse_pdf_document(self.job_dir, job)
        else:
            raise InputInvalidError("Document source disappeared during parsing")

        if html.is_file() and pdf.is_file():
            from steps.document.crosswalk import attach_pdf_crosswalk

            document, quality = attach_pdf_crosswalk(
                self.job_dir, document, quality, job,
            )

        if pdf.is_file():
            from steps.document.visual_assets import materialize_pdf_visuals

            document, quality = materialize_pdf_visuals(
                self.job_dir, document, quality,
            )

        document = validate_document(document, expected_job_id=self.job_dir.name)
        quality = validate_quality(quality, expected_job_id=self.job_dir.name)
        self.artifacts.write("intermediate/document.json", document)
        self.artifacts.write("intermediate/quality.json", quality)
        metadata = document.get("metadata") or {}
        sample = " ".join(
            str(value) for value in (
                (metadata.get("titles") or {}).get("original"),
                metadata.get("abstract"),
                *[block.get("text", "") for block in document["blocks"][:80]],
            ) if value
        )
        language = str(metadata.get("lang") or detect_lang(sample))
        if language != "zh" and quality["status"] != "rejected":
            self.artifacts.write(
                "intermediate/needs_translation.json",
                {
                    "lang": language,
                    "source_fingerprint": primary_document_source(document)["fingerprint"],
                },
            )
        else:
            (self.job_dir / "intermediate" / "needs_translation.json").unlink(
                missing_ok=True,
            )
        if quality["status"] == "rejected":
            raise InputInvalidError(
                "document quality rejected: " + ",".join(quality["reasons"])
            )
        return {
            "source_profile": document["source_profile"],
            "document_kind": document["document_kind"],
            "blocks": len(document["blocks"]),
            "quality": quality["status"],
            "lang": language,
        }


if __name__ == "__main__":
    DocumentParseStep.cli_main("02_parse")
