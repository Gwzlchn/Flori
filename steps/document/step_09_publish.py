"""在统一评审可靠且达到质量门后发布 Document 知识产物。"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Any

from shared.evidence_contract import build_canonical_evidence_records_with_reader
from shared.errors import InputInvalidError
from shared.note_text import markdown_to_index_text
from shared.review_contract import sha256_bytes, verify_persisted_review
from shared.step_base import StepBase, file_hash


MIN_OVERALL_SCORE = 3.0
MIN_CRITICAL_SCORE = 3
MIN_DIMENSION_SCORE = 2


class DocumentPublishStep(StepBase):
    """重验评审及其来源后写发布清单;拒绝时不产生可消费产物。"""

    def validate_inputs(self) -> list[str]:
        required = (
            "output/review.json",
            "output/concepts.json",
            "intermediate/source_segments.json",
            "output/provenance/smart.json",
        )
        missing = [path for path in required if not (self.job_dir / path).is_file()]
        if self.artifacts.latest_smart_note() is None:
            missing.append("output/versions/notes_smart_*.md")
        return missing

    def step_input_hashes(self) -> dict[str, str]:
        hashes = {
            "review": file_hash(self.job_dir / "output/review.json"),
            "concepts": file_hash(self.job_dir / "output/concepts.json"),
            "source_segments": file_hash(
                self.job_dir / "intermediate/source_segments.json"
            ),
            "smart_provenance": file_hash(
                self.job_dir / "output/provenance/smart.json"
            ),
            "publication_policy": "review-v1:overall-3:critical-3:dimension-2:no-error",
        }
        smart = self.artifacts.latest_smart_note()
        hashes["smart"] = file_hash(smart) if smart else ""
        semantic = self.job_dir / "output/provenance/semantic_batch.json"
        if semantic.is_file():
            hashes["semantic_batch"] = file_hash(semantic)
        return hashes

    def execute(self) -> dict[str, Any]:
        review = self.artifacts.load_json("output/review.json")
        verified = asyncio.run(verify_persisted_review(
            review,
            job_id=self.job_dir.name,
            pipeline="document",
            read_file=self._read_review_file,
        ))
        reasons = self._rejection_reasons(verified)
        if reasons:
            raise InputInvalidError(
                "document publication rejected: " + ",".join(reasons)
            )
        canonical_evidence = asyncio.run(self._verify_evidence_chain(
            verified["note_file"],
        ))

        artifacts = []
        paths = [
            "output/review.json",
            "output/concepts.json",
            "intermediate/source_segments.json",
            "output/provenance/smart.json",
            verified["note_file"],
        ]
        semantic = "output/provenance/semantic_batch.json"
        if (self.job_dir / semantic).is_file():
            paths.append(semantic)
        for rel in paths:
            data = (self.job_dir / rel).read_bytes()
            artifacts.append({"path": rel, "sha256": sha256_bytes(data)})
        manifest = {
            "schema_version": 1,
            "job_id": self.job_dir.name,
            "pipeline": "document",
            "status": "published",
            "review_policy": {
                "minimum_overall": MIN_OVERALL_SCORE,
                "minimum_critical": MIN_CRITICAL_SCORE,
                "minimum_dimension": MIN_DIMENSION_SCORE,
                "error_issues_allowed": False,
            },
            "review": {
                "overall": verified["overall"],
                "accuracy": verified["accuracy"],
                "traceability": verified["traceability"],
            },
            "artifacts": artifacts,
        }
        self.artifacts.write("output/publication.json", manifest)
        return {
            "published": True,
            "overall": verified["overall"],
            "artifacts": len(artifacts),
            "canonical_evidence": len(canonical_evidence),
        }

    async def _verify_evidence_chain(self, note_file: str) -> list[dict[str, Any]]:
        """复用 canonical reader 验证完整 semantic 链,发布门不另造宽松判据。"""
        note_data = await self._read_evidence_file(note_file, 8 * 1024 * 1024)
        source_data = await self._read_evidence_file(
            "intermediate/source_segments.json", 8 * 1024 * 1024,
        )
        provenance_data = await self._read_evidence_file(
            "output/provenance/smart.json", 8 * 1024 * 1024,
        )
        if note_data is None or source_data is None or provenance_data is None:
            raise InputInvalidError("document publication evidence is incomplete")
        try:
            normalized_body = markdown_to_index_text(note_data.decode("utf-8"))
        except UnicodeDecodeError as exc:
            raise InputInvalidError("document publication note is not UTF-8") from exc
        if not normalized_body:
            raise InputInvalidError("document publication note is empty")
        chunks = [{
            "chunk_id": f"{self.job_dir.name}:smart:publication",
            "body": normalized_body,
            "section": "",
            "char_start": 0,
            "char_end": len(normalized_body),
        }]
        return await build_canonical_evidence_records_with_reader(
            job_id=self.job_dir.name,
            pipeline="document",
            note_type="smart",
            note_path=note_file,
            note_data=note_data,
            normalized_body=normalized_body,
            chunks=chunks,
            source_manifest_data=source_data,
            source_manifest_path="intermediate/source_segments.json",
            provenance_path="output/provenance/smart.json",
            provenance_data=provenance_data,
            read_file=self._read_evidence_file,
            sha256_file=self._sha256_evidence_file,
            attestation_protocol=lambda: self.ai.load_prompt_template(
                "semantic_attestation"
            ),
        )

    async def _read_review_file(self, rel: str) -> bytes | None:
        root = self.job_dir.resolve()
        path = (self.job_dir / rel).resolve()
        if path != root and root not in path.parents:
            raise ValueError("review artifact escapes job dir")
        try:
            return path.read_bytes()
        except FileNotFoundError:
            return None

    async def _read_evidence_file(self, rel: str, max_bytes: int) -> bytes | None:
        data = await self._read_review_file(rel)
        if data is None or len(data) > max_bytes:
            return None
        return data

    async def _sha256_evidence_file(self, rel: str) -> str | None:
        data = await self._read_review_file(rel)
        return hashlib.sha256(data).hexdigest() if data is not None else None

    @staticmethod
    def _rejection_reasons(review: dict[str, Any]) -> list[str]:
        reasons: list[str] = []
        if review.get("review_reliable") is not True:
            reasons.append("review_unreliable")
        overall = review.get("overall")
        if not isinstance(overall, (int, float)) or overall < MIN_OVERALL_SCORE:
            reasons.append("overall_below_minimum")
        for key in review.get("score_keys") or []:
            value = review.get(key)
            if type(value) is not int or value < MIN_DIMENSION_SCORE:
                reasons.append(f"{key}_below_minimum")
        for key in ("accuracy", "traceability"):
            value = review.get(key)
            if type(value) is not int or value < MIN_CRITICAL_SCORE:
                reasons.append(f"{key}_below_critical")
        if any(
            isinstance(issue, dict) and issue.get("severity") == "error"
            for issue in (review.get("issues") or [])
        ):
            reasons.append("review_has_error_issue")
        return list(dict.fromkeys(reasons))


if __name__ == "__main__":
    DocumentPublishStep.cli_main("09_publish")
