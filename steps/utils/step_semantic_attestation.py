"""在 producer 与 concepts 之间独立批量提升语义证据。"""

from __future__ import annotations

from shared.errors import InputInvalidError
from shared.step_base import StepBase
from steps.utils.provenance_attestation import (
    finalize_pending_semantic_provenance,
    semantic_attestation_input_hashes,
)


class SemanticAttestationStep(StepBase):
    def _pipeline(self) -> str:
        step = self.config.get("step") or {}
        pipeline = step.get("pipeline")
        if isinstance(pipeline, str) and pipeline in {"video", "paper", "article", "audio"}:
            return pipeline
        raise InputInvalidError("semantic attestation pipeline identity is missing")

    def validate_inputs(self) -> list[str]:
        # 上游 producer 可因规则跳过;无候选也必须执行以撤销旧 batch commit。
        return []

    def input_hashes(self) -> dict[str, str]:
        hashes = semantic_attestation_input_hashes(self.job_dir)
        source = self.job_dir / "intermediate" / "source_segments.json"
        owned = {
            "semantic_batch_commit": self.job_dir / "output/provenance/semantic_batch.json",
            "semantic_ai_log": self.job_dir / "output/ai_logs" / f"{self.step_name}.jsonl",
        }
        if source.is_file():
            from shared.step_artifacts import file_hash
            hashes["source_manifest"] = file_hash(source)
            for key, path in owned.items():
                if path.is_file():
                    hashes[key] = file_hash(path)
        return hashes

    def execute(self) -> dict:
        return finalize_pending_semantic_provenance(
            self.job_dir,
            pipeline=self._pipeline(),
            ai=self.ai,
        )


if __name__ == "__main__":
    SemanticAttestationStep.cli_main("semantic_attestation")
