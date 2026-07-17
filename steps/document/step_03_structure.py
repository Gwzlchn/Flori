"""校验 Document/Quality 并发布 canonical source manifest。"""

from __future__ import annotations

from shared.document_contract import validate_document, validate_quality
from shared.errors import InputInvalidError
from shared.step_base import StepBase, file_hash
from steps.document.provenance import (
    publish_document_index_projection,
    publish_document_source_manifest,
)


class DocumentStructureStep(StepBase):
    def validate_inputs(self) -> list[str]:
        return [
            path for path in ("intermediate/document.json", "intermediate/quality.json")
            if not (self.job_dir / path).is_file()
        ]

    def input_hashes(self) -> dict[str, str]:
        return {
            "document": file_hash(self.job_dir / "intermediate/document.json"),
            "quality": file_hash(self.job_dir / "intermediate/quality.json"),
        }

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
            raise InputInvalidError(
                "document quality rejected: " + ",".join(quality["reasons"])
            )
        manifest = publish_document_source_manifest(self.job_dir, document)
        index_projection = publish_document_index_projection(self.job_dir, document)
        return {
            "blocks": len(document["blocks"]),
            "figures": len(document["figures"]),
            "tables": len(document["tables"]),
            "quality": quality["status"],
            "source_segments": len(manifest["segments"]),
            "index_blocks": index_projection["blocks"],
        }


if __name__ == "__main__":
    DocumentStructureStep.cli_main("03_structure")
