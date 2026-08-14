"""验证语义候选只能由下游 concepts 独立提升为 canonical evidence。"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path

import pytest

from scheduler.scheduler import _markdown_to_text
from shared.db import _chunk_note_body
from shared.errors import AIProviderError
from shared.evidence_contract import (
    CanonicalEvidenceError,
    build_canonical_evidence_records_with_reader,
)
from shared.models import LLMResponse
from shared.provenance import (
    GROUPED_SEMANTIC_ATTESTATION_POLICY,
    MAX_SEMANTIC_AI_LOG_HISTORY_BYTES,
    SEMANTIC_ATTESTOR_RESPONSE_SCHEMA_VERSION,
    build_semantic_attestation_prompt,
    build_provenance_manifest,
    build_source_manifest,
    canonical_json_bytes,
    make_segment_id,
    materialize_semantic_attestations,
    semantic_attestation_batch_id,
    write_provenance_manifest,
    write_source_manifest,
)
from steps.utils.provenance_attestation import (
    _run_semantic_attestation_batches,
    finalize_pending_semantic_provenance,
    persist_semantic_candidates,
    semantic_attestation_input_hashes,
)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _job(tmp_path: Path) -> tuple[Path, dict, bytes, str]:
    job_dir = tmp_path / "job-semantic"
    source_data = b"The model does not exceed 5 kg."
    source_path = job_dir / "input" / "source.html"
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(source_data)
    locator = {
        "kind": "text",
        "exact": source_data.decode(),
        "prefix": None,
        "suffix": None,
        "dom_path": None,
    }
    segment_id = make_segment_id(
        "html", start=0, end=len(source_data), section="body", locator=locator,
    )
    source_manifest = build_source_manifest(
        job_id=job_dir.name,
        pipeline="document",
        source_artifacts=[{
            "source_id": "html",
            "path": "input/source.html",
            "sha256": _sha(source_data),
            "revision": None,
            "media_duration_ms": None,
            "page_count": None,
        }],
        segments=[{
            "segment_id": segment_id,
            "source_id": "html",
            "start": 0,
            "end": len(source_data),
            "section": "body",
            "locator": locator,
            "support_text": source_data.decode(),
            "support_artifact": {
                "kind": "html",
                "path": "input/source.html",
                "sha256": _sha(source_data),
                "selector": {"start": 0, "end": len(source_data)},
            },
        }],
    )
    source_manifest_path = job_dir / "intermediate" / "source_segments.json"
    write_source_manifest(
        source_manifest_path, source_manifest, trusted_root=job_dir,
    )

    note = "# 翻译\n\n该模型不超过 5 kg。"
    note_path = job_dir / "output" / "translated.md"
    note_path.parent.mkdir(parents=True, exist_ok=True)
    note_path.write_text(note, encoding="utf-8")
    normalized = _markdown_to_text(note)
    empty = build_provenance_manifest(
        job_id=job_dir.name,
        note_type="translated",
        note_artifact="output/translated.md",
        note_bytes=note.encode(),
        normalized_body=normalized,
        source_manifest_path="intermediate/source_segments.json",
        source_manifest=source_manifest,
        segments=[],
    )
    write_provenance_manifest(
        job_dir / "output" / "provenance" / "translated.json",
        empty,
        trusted_root=job_dir,
        source_manifest=source_manifest,
        note_bytes=note.encode(),
        normalized_body=normalized,
    )
    return job_dir, source_manifest, note.encode(), segment_id


def _add_smart_candidate(
    job_dir: Path, source_manifest: dict, segment_id: str,
) -> bytes:
    note = "# 智能笔记\n\n该模型不超过 5 kg。"
    note_data = note.encode()
    note_path = job_dir / "output" / "smart.md"
    note_path.write_bytes(note_data)
    normalized = _markdown_to_text(note)
    empty = build_provenance_manifest(
        job_id=job_dir.name,
        note_type="smart",
        note_artifact="output/smart.md",
        note_bytes=note_data,
        normalized_body=normalized,
        source_manifest_path="intermediate/source_segments.json",
        source_manifest=source_manifest,
        segments=[],
    )
    write_provenance_manifest(
        job_dir / "output" / "provenance" / "smart.json",
        empty,
        trusted_root=job_dir,
        source_manifest=source_manifest,
        note_bytes=note_data,
        normalized_body=normalized,
    )
    persist_semantic_candidates(
        job_dir,
        pipeline="document",
        note_type="smart",
        note_artifact="output/smart.md",
        candidates=[{
            "anchor": "该模型不超过 5 kg。",
            "prefix": "",
            "suffix": "",
            "section": "smart",
            "source_segment_id": segment_id,
            "transform_kind": "cross_language",
            "producer_component": "05_smart",
            "producer_invocation_id": "producer-smart-session",
        }],
    )
    return note_data


def _replace_note_candidates(
    job_dir: Path,
    source_manifest: dict,
    segment_id: str,
    *,
    note_type: str,
    count: int,
) -> None:
    claims = [
        (
            f"The model does not exceed 5 kg; semantic {note_type} "
            f"variant {chr(0x4e00 + index)}."
        )
        for index in range(count)
    ]
    _replace_note_candidates_with_claims(
        job_dir,
        source_manifest,
        segment_id,
        note_type=note_type,
        claims=claims,
    )


def _replace_note_candidates_with_claims(
    job_dir: Path,
    source_manifest: dict,
    segment_id: str,
    *,
    note_type: str,
    claims: list[str],
) -> None:
    note_artifact = "output/smart.md" if note_type == "smart" else "output/translated.md"
    component = "05_smart" if note_type == "smart" else "04_translate"
    note = f"# {note_type}\n\n" + "\n\n".join(claims)
    note_data = note.encode()
    (job_dir / note_artifact).write_bytes(note_data)
    normalized = _markdown_to_text(note)
    empty = build_provenance_manifest(
        job_id=job_dir.name,
        note_type=note_type,
        note_artifact=note_artifact,
        note_bytes=note_data,
        normalized_body=normalized,
        source_manifest_path="intermediate/source_segments.json",
        source_manifest=source_manifest,
        segments=[],
    )
    write_provenance_manifest(
        job_dir / "output" / "provenance" / f"{note_type}.json",
        empty,
        trusted_root=job_dir,
        source_manifest=source_manifest,
        note_bytes=note_data,
        normalized_body=normalized,
    )
    persist_semantic_candidates(
        job_dir,
        pipeline="document",
        note_type=note_type,
        note_artifact=note_artifact,
        candidates=[{
            "anchor": claim,
            "prefix": "",
            "suffix": "",
            "section": note_type,
            "source_segment_id": segment_id,
            "transform_kind": "cross_language",
            "producer_component": component,
            "producer_invocation_id": f"{component}-session",
        } for claim in claims],
    )


class _Attestor:
    step_name = "06_semantic_attestation"

    def __init__(self, job_dir: Path, *, response_schema: int = 4) -> None:
        self.job_dir = job_dir
        self.response_schema = response_schema
        self.last_response = None
        self.call_index = 0
        self.ai_log_records = []
        self.log = _Log()

    def load_prompt_template(self, name: str) -> str:
        # 与真实步同源:协议文本来自 tracked 模板(prompt_locked 步不吃覆盖)。
        assert name == "semantic_attestation"
        template = Path(__file__).resolve().parent.parent / (
            "configs/prompts/templates/semantic_attestation.md"
        )
        return template.read_text(encoding="utf-8")

    def call(self, prompt: str, **_kwargs) -> str:
        request = json.loads(prompt.split("INPUT=", 1)[1])
        decisions = [{
            "decision_id": item["decision_id"],
            "decision": "supported",
            "confidence_ppm": 990_000,
            "reason_codes": ["semantic_equivalent", "critical_facts_match"],
        } for item in request["items"]]
        content = json.dumps({
            "schema_version": self.response_schema,
            "decisions": decisions,
        })
        self.last_response = LLMResponse(
            content=content,
            provider="claude-cli",
            model="claude-opus-4-8",
            session_id="attestor-session",
        )
        record = {
            "job_id": self.job_dir.name,
            "step": self.step_name,
            "session_id": "attestor-session",
            "call_index": self.call_index,
            "ok": True,
            "prompt": {"rendered": {"user": prompt}},
            "routing": {"provider": "claude-cli", "model": "claude-opus-4-8"},
            "output": {"content": content},
        }
        log_path = self.job_dir / "output" / "ai_logs" / f"{self.step_name}.jsonl"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.ai_log_records.append(record)
        self.call_index += 1
        return content


class _ParallelAttestor(_Attestor):
    def __init__(
        self, job_dir: Path, *, parallelism: int = 4,
        fail_audit_name: str | None = None,
        invalid_audit_name: str | None = None,
    ) -> None:
        super().__init__(job_dir)
        self.config = {"providers": {"providers": {
            "qoder-cli": {"document_smart_parallelism": parallelism},
        }}}
        self.parallelism = parallelism
        self.fail_audit_name = fail_audit_name
        self.invalid_audit_name = invalid_audit_name
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.started: list[str] = []
        self.start_barrier = (
            threading.Barrier(parallelism)
            if fail_audit_name is not None or invalid_audit_name is not None
            else None
        )

    def selection(self) -> dict:
        return {"tiers": [{"provider": "qoder-cli"}]}

    def fork(self, audit_name: str):
        return _ParallelAttestorFork(self, audit_name)

    def merge_forks(self, invocations: list) -> None:
        records = [record for invocation in invocations for record in invocation.ai_log_records]
        for index, record in enumerate(records):
            record["call_index"] = index
        self.ai_log_records = records
        self.call_index = len(records)
        self.last_response = invocations[-1].last_response


class _ParallelAttestorFork(_Attestor):
    def __init__(self, root: _ParallelAttestor, audit_name: str) -> None:
        super().__init__(root.job_dir)
        self.root = root
        self.audit_name = audit_name

    def call(self, prompt: str, **_kwargs) -> str:
        with self.root.lock:
            self.root.active += 1
            self.root.max_active = max(self.root.max_active, self.root.active)
            self.root.started.append(self.audit_name)
        try:
            if self.root.start_barrier is not None:
                self.root.start_barrier.wait(timeout=2)
            if self.audit_name == self.root.fail_audit_name:
                raise RuntimeError("attestor batch failed")
            if self.audit_name == self.root.invalid_audit_name:
                content = '{"schema_version":4,"decisions":['
            else:
                time.sleep(0.04)
                request = json.loads(prompt.split("INPUT=", 1)[1])
                content = json.dumps({
                    "schema_version": 4,
                    "decisions": [{
                        "decision_id": item["decision_id"],
                        "decision": "supported",
                        "confidence_ppm": 990_000,
                        "reason_codes": [
                            "semantic_equivalent", "critical_facts_match",
                        ],
                    } for item in request["items"]],
                })
            session_id = f"session-{self.audit_name}"
            self.last_response = LLMResponse(
                content=content, provider="qoder-cli", model="ultimate",
                session_id=session_id,
            )
            self.ai_log_records.append({
                "job_id": self.job_dir.name,
                "step": self.step_name,
                "session_id": session_id,
                "exec_id": f"exec-{self.audit_name}",
                "call_index": 0,
                "ok": True,
                "prompt": {"rendered": {"user": prompt}},
                "routing": {"provider": "qoder-cli", "model": "ultimate"},
                "output": {"content": content},
            })
            self.call_index = 1
            return content
        finally:
            with self.root.lock:
                self.root.active -= 1


def _parallel_selections(count: int) -> list[dict]:
    return [{
        "selected_candidate_ids": [f"candidate-{index}"],
        "prompt": "PROTOCOL\nINPUT=" + json.dumps({
            "items": [{"decision_id": "d000"}],
        }),
    } for index in range(count)]


def test_semantic_batches_use_provider_sliding_window(tmp_path: Path) -> None:
    job_dir = tmp_path / "parallel-attestation"
    attestor = _ParallelAttestor(job_dir, parallelism=4)

    completed = _run_semantic_attestation_batches(
        job_dir, attestor, _parallel_selections(7),
    )

    assert len(completed) == attestor.call_index == 7
    assert attestor.max_active == 4
    assert [item["ai_log_binding"]["call_index"] for item in completed] == list(range(7))
    assert len(list((job_dir / "output/ai_logs").glob("*.semantic.*.jsonl"))) == 7


def test_semantic_batch_failure_stops_sliding_window_without_proof(
    tmp_path: Path,
) -> None:
    job_dir = tmp_path / "parallel-attestation-failure"
    attestor = _ParallelAttestor(
        job_dir, parallelism=4, fail_audit_name="semantic-0001",
    )

    with pytest.raises(RuntimeError, match="attestor batch failed"):
        _run_semantic_attestation_batches(
            job_dir, attestor, _parallel_selections(7),
        )

    assert set(attestor.started) == {
        "semantic-0000", "semantic-0001", "semantic-0002", "semantic-0003",
    }
    assert attestor.ai_log_records == []
    assert not list((job_dir / "output/ai_logs").glob("*.semantic.*.jsonl"))


def test_semantic_invalid_response_stops_refill_before_later_batches(
    tmp_path: Path,
) -> None:
    job_dir = tmp_path / "parallel-attestation-invalid"
    attestor = _ParallelAttestor(
        job_dir, parallelism=4, invalid_audit_name="semantic-0001",
    )

    with pytest.raises(ValueError, match="not strict JSON"):
        _run_semantic_attestation_batches(
            job_dir, attestor, _parallel_selections(7),
        )

    assert set(attestor.started) == {
        "semantic-0000", "semantic-0001", "semantic-0002", "semantic-0003",
    }
    assert attestor.ai_log_records == []
    assert not list((job_dir / "output/ai_logs").glob("*.semantic.*.jsonl"))


def test_parallel_invalid_response_is_retryable_without_partial_publish(
    tmp_path: Path,
) -> None:
    job_dir, source, _note_data, segment_id = _job(tmp_path)
    source["segments"][0]["support_text"] = "S" * 4000
    write_source_manifest(
        job_dir / "intermediate/source_segments.json", source,
        trusted_root=job_dir,
    )
    _replace_note_candidates(
        job_dir, source, segment_id, note_type="translated", count=100,
    )
    provenance_path = job_dir / "output/provenance/translated.json"
    before = provenance_path.read_bytes()
    commit_path = job_dir / "output/provenance/semantic_batch.json"
    commit_path.write_bytes(b"previous-commit")
    attestor = _ParallelAttestor(
        job_dir, parallelism=4, invalid_audit_name="semantic-0001",
    )

    with pytest.raises(AIProviderError, match="not strict JSON"):
        finalize_pending_semantic_provenance(
            job_dir, pipeline="document", ai=attestor,
        )

    assert set(attestor.started) == {
        "semantic-0000", "semantic-0001", "semantic-0002", "semantic-0003",
    }
    assert provenance_path.read_bytes() == before
    assert commit_path.read_bytes() == b"previous-commit"


class _Log:
    def warning(self, _event: str, **_kwargs) -> None:
        pass


class _FlakyAttestor(_Attestor):
    log = _Log()

    def __init__(self, job_dir: Path) -> None:
        super().__init__(job_dir)
        self.calls = 0

    def call(self, prompt: str, **kwargs) -> str:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("attestor unavailable")
        return super().call(prompt, **kwargs)


class _MemoryProofAttestor(_Attestor):
    def call(self, prompt: str, **kwargs) -> str:
        content = super().call(prompt, **kwargs)
        (self.job_dir / "output/ai_logs" / f"{self.step_name}.jsonl").unlink()
        return content


class _SecondCallFailAttestor(_Attestor):
    def call(self, prompt: str, **kwargs) -> str:
        if self.call_index == 1:
            raise RuntimeError("second attestation call failed")
        return super().call(prompt, **kwargs)


async def _records(
    job_dir: Path,
    note_data: bytes,
    *,
    provenance_path: str = "output/provenance/translated.json",
    provenance_data: bytes | None = None,
) -> list[dict]:
    normalized = _markdown_to_text(note_data.decode())
    chunks = [{
        "chunk_id": f"{job_dir.name}:translated:{index}",
        "body": chunk["body"],
        "section": chunk["section"],
        "char_start": chunk["char_start"],
        "char_end": chunk["char_end"],
    } for index, chunk in enumerate(_chunk_note_body(normalized))]

    async def read_file(rel: str, max_bytes: int) -> bytes | None:
        try:
            data = (job_dir / rel).read_bytes()
        except OSError:
            return None
        return data[:max_bytes + 1]

    async def sha256_file(rel: str) -> str | None:
        try:
            return _sha((job_dir / rel).read_bytes())
        except OSError:
            return None

    return await build_canonical_evidence_records_with_reader(
        job_id=job_dir.name,
        pipeline="document",
        note_type="translated",
        note_path="output/translated.md",
        note_data=note_data,
        normalized_body=normalized,
        chunks=chunks,
        source_manifest_data=(
            job_dir / "intermediate" / "source_segments.json"
        ).read_bytes(),
        source_manifest_path="intermediate/source_segments.json",
        provenance_path=provenance_path,
        provenance_data=(
            provenance_data
            if provenance_data is not None
            else (job_dir / provenance_path).read_bytes()
        ),
        read_file=read_file,
        sha256_file=sha256_file,
        attestation_protocol=lambda: (
            Path(__file__).resolve().parent.parent
            / "configs/prompts/templates/semantic_attestation.md"
        ).read_text(encoding="utf-8"),
    )


@pytest.mark.asyncio
async def test_candidate_is_untrusted_until_concepts_publishes_final_v3(
    tmp_path: Path,
) -> None:
    job_dir, _source_manifest, note_data, segment_id = _job(tmp_path)
    candidate = {
        "anchor": "该模型不超过 5 kg。",
        "prefix": "",
        "suffix": "",
        "section": "translated",
        "source_segment_id": segment_id,
        "transform_kind": "translated",
        "producer_component": "04_translate",
        "producer_invocation_id": "producer-session",
    }
    persist_semantic_candidates(
        job_dir,
        pipeline="document",
        note_type="translated",
        note_artifact="output/translated.md",
        candidates=[candidate],
    )
    candidate_json = json.loads((
        job_dir / "output" / "provenance_candidates" / "translated.json"
    ).read_text())
    assert "decision" not in candidate_json["candidates"][0]
    assert "attestor" not in candidate_json["candidates"][0]
    assert await _records(job_dir, note_data) == []

    candidate_path = "output/provenance_candidates/translated.json"
    with pytest.raises(CanonicalEvidenceError, match="keys mismatch"):
        await _records(
            job_dir,
            note_data,
            provenance_path=candidate_path,
            provenance_data=(job_dir / candidate_path).read_bytes(),
        )

    result = finalize_pending_semantic_provenance(
        job_dir, pipeline="document", ai=_Attestor(job_dir),
    )

    assert {key: result[key] for key in ("note_types", "accepted", "rejected", "failed", "calls")} == {
        "note_types": 1, "accepted": 1, "rejected": 0, "failed": 0, "calls": 1,
    }
    final = json.loads((
        job_dir / "output" / "provenance" / "translated.json"
    ).read_text())
    assert final["schema_version"] == 3
    attestation = final["segments"][0]["attestation"]
    assert attestation["producer_component"] == "04_translate"
    assert attestation["attestor_component"] == "06_semantic_attestation"
    assert len(await _records(job_dir, note_data)) == 1


@pytest.mark.asyncio
async def test_grouped_attestation_reaches_one_canonical_evidence_group(
    tmp_path: Path,
) -> None:
    job_dir, _source_manifest, note_data, _segment_id = _job(tmp_path)
    source_data = (job_dir / "input/source.html").read_bytes()
    split = source_data.index(b"5 kg")
    segments = []
    segment_ids = []
    for start, end in ((0, split), (split, len(source_data))):
        support = source_data[start:end].decode().strip()
        support_start = source_data.decode().index(support)
        support_end = support_start + len(support)
        locator = {
            "kind": "text", "exact": support,
            "prefix": None, "suffix": None, "dom_path": None,
        }
        segment_id = make_segment_id(
            "html", start=support_start, end=support_end,
            section="body", locator=locator,
        )
        segment_ids.append(segment_id)
        segments.append({
            "segment_id": segment_id,
            "source_id": "html",
            "start": support_start,
            "end": support_end,
            "section": "body",
            "locator": locator,
            "support_text": support,
            "support_artifact": {
                "kind": "html",
                "path": "input/source.html",
                "sha256": _sha(source_data),
                "selector": {"start": support_start, "end": support_end},
            },
        })
    source_manifest = build_source_manifest(
        job_id=job_dir.name,
        pipeline="document",
        source_artifacts=[{
            "source_id": "html",
            "path": "input/source.html",
            "sha256": _sha(source_data),
            "revision": None,
            "media_duration_ms": None,
            "page_count": None,
        }],
        segments=segments,
    )
    write_source_manifest(
        job_dir / "intermediate/source_segments.json",
        source_manifest,
        trusted_root=job_dir,
    )
    normalized = _markdown_to_text(note_data.decode())
    exact = build_provenance_manifest(
        job_id=job_dir.name,
        note_type="translated",
        note_artifact="output/translated.md",
        note_bytes=note_data,
        normalized_body=normalized,
        source_manifest_path="intermediate/source_segments.json",
        source_manifest=source_manifest,
        segments=[],
    )
    write_provenance_manifest(
        job_dir / "output/provenance/translated.json",
        exact,
        trusted_root=job_dir,
        source_manifest=source_manifest,
        note_bytes=note_data,
        normalized_body=normalized,
    )
    persist_semantic_candidates(
        job_dir,
        pipeline="document",
        note_type="translated",
        note_artifact="output/translated.md",
        candidates=[{
            "anchor": "该模型不超过 5 kg。",
            "prefix": "",
            "suffix": "",
            "section": "translated",
            "source_segment_ids": segment_ids,
            "transform_kind": "translated",
            "producer_component": "04_translate",
            "producer_invocation_id": "producer-group-session",
        }],
    )

    result = finalize_pending_semantic_provenance(
        job_dir, pipeline="document", ai=_Attestor(job_dir),
    )
    final = json.loads((
        job_dir / "output/provenance/translated.json"
    ).read_text())
    records = await _records(job_dir, note_data)

    assert result["accepted"] == 1
    assert final["schema_version"] == 4
    assert final["segments"][0]["verification_policy"] == (
        GROUPED_SEMANTIC_ATTESTATION_POLICY
    )
    assert final["segments"][0]["source_segment_ids"] == segment_ids
    assert len(records) == 1
    assert [source["source_segment_id"] for source in records[0]["sources"]] == (
        segment_ids
    )
    assert [source["ordinal"] for source in records[0]["sources"]] == [0, 1]


@pytest.mark.asyncio
async def test_final_attestation_tampering_fails_closed(tmp_path: Path) -> None:
    job_dir, _source_manifest, note_data, segment_id = _job(tmp_path)
    persist_semantic_candidates(
        job_dir,
        pipeline="document",
        note_type="translated",
        note_artifact="output/translated.md",
        candidates=[{
            "anchor": "该模型不超过 5 kg。",
            "prefix": "",
            "suffix": "",
            "section": "translated",
            "source_segment_id": segment_id,
            "transform_kind": "translated",
            "producer_component": "04_translate",
            "producer_invocation_id": "producer-session",
        }],
    )
    finalize_pending_semantic_provenance(
        job_dir, pipeline="document", ai=_Attestor(job_dir),
    )
    provenance_path = job_dir / "output" / "provenance" / "translated.json"
    final = json.loads(provenance_path.read_text())
    final["segments"][0]["attestation"]["producer_component"] = "05_concepts"
    provenance_path.write_bytes(canonical_json_bytes(final))

    with pytest.raises(CanonicalEvidenceError, match="independent|incomplete"):
        await _records(job_dir, note_data)


@pytest.mark.asyncio
async def test_candidate_section_binding_survives_rehashed_final_commit(
    tmp_path: Path,
) -> None:
    job_dir, _source_manifest, note_data, segment_id = _job(tmp_path)
    persist_semantic_candidates(
        job_dir,
        pipeline="document",
        note_type="translated",
        note_artifact="output/translated.md",
        candidates=[{
            "anchor": "该模型不超过 5 kg。",
            "prefix": "",
            "suffix": "",
            "section": "translated",
            "source_segment_id": segment_id,
            "transform_kind": "translated",
            "producer_component": "04_translate",
            "producer_invocation_id": "producer-session",
        }],
    )
    finalize_pending_semantic_provenance(
        job_dir, pipeline="document", ai=_Attestor(job_dir),
    )
    provenance_path = job_dir / "output/provenance/translated.json"
    final = json.loads(provenance_path.read_text())
    final["segments"][0]["section"] = "replayed-section"
    final_data = canonical_json_bytes(final)
    provenance_path.write_bytes(final_data)
    commit_path = job_dir / "output/provenance/semantic_batch.json"
    commit = json.loads(commit_path.read_text())
    translated = next(
        item for item in commit["provenance_manifests"]
        if item["note_type"] == "translated"
    )
    translated["sha256"] = _sha(final_data)
    commit_path.write_bytes(canonical_json_bytes(commit))
    with pytest.raises(CanonicalEvidenceError, match="binding changed"):
        await _records(job_dir, note_data)


def test_exact_v2_survives_failure_then_retry_publishes_v3(tmp_path: Path) -> None:
    job_dir, source_manifest, _translated_data, segment_id = _job(tmp_path)
    note = "# 智能笔记\n\nThe model does not exceed 5 kg.\n\n该模型不超过 5 kg。"
    note_data = note.encode()
    normalized = _markdown_to_text(note)
    note_path = job_dir / "output" / "smart.md"
    note_path.write_bytes(note_data)
    exact = build_provenance_manifest(
        job_id=job_dir.name,
        note_type="smart",
        note_artifact="output/smart.md",
        note_bytes=note_data,
        normalized_body=normalized,
        source_manifest_path="intermediate/source_segments.json",
        source_manifest=source_manifest,
        segments=[{
            "anchor": "The model does not exceed 5 kg.",
            "prefix": "",
            "suffix": "",
            "section": "智能笔记",
            "source_segment_ids": [segment_id],
            "verification_policy": "exact_quote_v1",
        }],
    )
    provenance_path = job_dir / "output" / "provenance" / "smart.json"
    write_provenance_manifest(
        provenance_path,
        exact,
        trusted_root=job_dir,
        source_manifest=source_manifest,
        note_bytes=note_data,
        normalized_body=normalized,
    )
    persist_semantic_candidates(
        job_dir,
        pipeline="document",
        note_type="smart",
        note_artifact="output/smart.md",
        candidates=[{
            "anchor": "该模型不超过 5 kg。",
            "prefix": "",
            "suffix": "",
            "section": "智能笔记",
            "source_segment_id": segment_id,
            "transform_kind": "translated",
            "producer_component": "05_smart",
            "producer_invocation_id": "producer-session",
        }],
    )
    before = provenance_path.read_bytes()

    attestor = _FlakyAttestor(job_dir)
    with pytest.raises(AIProviderError, match="semantic attestation failed"):
        finalize_pending_semantic_provenance(
            job_dir, pipeline="document", ai=attestor,
        )
    assert provenance_path.read_bytes() == before
    assert json.loads(before)["schema_version"] == 2
    candidate_path = job_dir / "output" / "provenance_candidates" / "smart.json"
    assert candidate_path.is_file()

    result = finalize_pending_semantic_provenance(
        job_dir, pipeline="document", ai=attestor,
    )
    assert {key: result[key] for key in ("note_types", "accepted", "rejected", "failed", "calls")} == {
        "note_types": 1, "accepted": 1, "rejected": 0, "failed": 0, "calls": 1,
    }
    final = json.loads(provenance_path.read_text())
    assert final["schema_version"] == 3
    assert [item["verification_policy"] for item in final["segments"]] == [
        "exact_quote_v1", "semantic_attestation_v1",
    ]
    assert candidate_path.is_file()


def test_worker_rotation_empty_candidate_overwrites_old_manifest(tmp_path: Path) -> None:
    job_dir, _source, _note_data, segment_id = _job(tmp_path)
    candidate = {
        "anchor": "该模型不超过 5 kg。",
        "prefix": "",
        "suffix": "",
        "section": "translated",
        "source_segment_id": segment_id,
        "transform_kind": "translated",
        "producer_component": "04_translate",
        "producer_invocation_id": "producer-session",
    }
    persist_semantic_candidates(
        job_dir, pipeline="document", note_type="translated",
        note_artifact="output/translated.md", candidates=[candidate],
    )
    state = persist_semantic_candidates(
        job_dir, pipeline="document", note_type="translated",
        note_artifact="output/translated.md", candidates=[],
    )
    manifest = json.loads((
        job_dir / "output/provenance_candidates/translated.json"
    ).read_text())
    assert state == {"status": "empty", "candidates": 0}
    assert manifest["status"] == "empty" and manifest["candidates"] == []


@pytest.mark.parametrize("mode", ["malformed", "valid_tombstone"])
def test_no_source_manifest_is_rejected_when_canonical_source_exists(
    tmp_path: Path,
    mode: str,
) -> None:
    job_dir, _source, _note_data, segment_id = _job(tmp_path)
    source_path = job_dir / "intermediate/source_segments.json"
    if mode == "malformed":
        persist_semantic_candidates(
            job_dir,
            pipeline="document",
            note_type="translated",
            note_artifact="output/translated.md",
            candidates=[{
                "anchor": "该模型不超过 5 kg。",
                "prefix": "",
                "suffix": "",
                "section": "translated",
                "source_segment_id": segment_id,
                "transform_kind": "translated",
                "producer_component": "04_translate",
                "producer_invocation_id": "producer-session",
            }],
        )
        candidate_path = (
            job_dir / "output/provenance_candidates/translated.json"
        )
        manifest = json.loads(candidate_path.read_text())
        manifest["status"] = "no_source"
        candidate_path.write_bytes(canonical_json_bytes(manifest))
    else:
        source_data = source_path.read_bytes()
        source_path.unlink()
        persist_semantic_candidates(
            job_dir,
            pipeline="document",
            note_type="translated",
            note_artifact="output/translated.md",
            candidates=[],
        )
        source_path.write_bytes(source_data)

    attestor = _Attestor(job_dir)
    with pytest.raises(ValueError, match="no_source candidate"):
        finalize_pending_semantic_provenance(
            job_dir, pipeline="document", ai=attestor,
        )
    assert attestor.call_index == 0
    assert not (job_dir / "output/provenance/semantic_batch.json").exists()


def test_candidate_note_type_must_match_manifest_path_before_ai_call(
    tmp_path: Path,
) -> None:
    job_dir, source, _note_data, segment_id = _job(tmp_path)
    _add_smart_candidate(job_dir, source, segment_id)
    persist_semantic_candidates(
        job_dir,
        pipeline="document",
        note_type="translated",
        note_artifact="output/translated.md",
        candidates=[{
            "anchor": "该模型不超过 5 kg。",
            "prefix": "",
            "suffix": "",
            "section": "translated",
            "source_segment_id": segment_id,
            "transform_kind": "translated",
            "producer_component": "04_translate",
            "producer_invocation_id": "producer-session",
        }],
    )
    candidate_root = job_dir / "output/provenance_candidates"
    (candidate_root / "smart.json").write_bytes(
        (candidate_root / "translated.json").read_bytes()
    )

    attestor = _Attestor(job_dir)
    with pytest.raises(ValueError, match="note_type does not match its path"):
        finalize_pending_semantic_provenance(
            job_dir, pipeline="document", ai=attestor,
        )
    assert attestor.call_index == 0
    assert not (job_dir / "output/provenance/semantic_batch.json").exists()


@pytest.mark.asyncio
async def test_dual_note_types_are_attested_in_one_call(tmp_path: Path) -> None:
    job_dir, source, note_data, segment_id = _job(tmp_path)
    _add_smart_candidate(job_dir, source, segment_id)
    persist_semantic_candidates(
        job_dir, pipeline="document", note_type="translated",
        note_artifact="output/translated.md", candidates=[{
            "anchor": "该模型不超过 5 kg。", "prefix": "", "suffix": "",
            "section": "translated", "source_segment_id": segment_id,
            "transform_kind": "translated",
            "producer_component": "04_translate",
            "producer_invocation_id": "producer-translate-session",
        }],
    )
    attestor = _Attestor(job_dir)
    result = finalize_pending_semantic_provenance(
        job_dir, pipeline="document", ai=attestor,
    )
    commit = json.loads((job_dir / "output/provenance/semantic_batch.json").read_text())
    assert result["note_types"] == 2 and result["accepted"] == 2
    assert result["calls"] == 1 and attestor.call_index == 1
    assert [item["note_type"] for item in commit["provenance_manifests"]] == [
        "smart", "translated",
    ]
    assert len(await _records(job_dir, note_data)) == 1


def test_batch_candidate_limit_allows_dual_fifty_with_one_call(tmp_path: Path) -> None:
    job_dir, source, _note_data, segment_id = _job(tmp_path)
    _replace_note_candidates(
        job_dir, source, segment_id, note_type="smart", count=50,
    )
    _replace_note_candidates(
        job_dir, source, segment_id, note_type="translated", count=50,
    )
    attestor = _Attestor(job_dir)
    result = finalize_pending_semantic_provenance(
        job_dir, pipeline="document", ai=attestor,
    )
    assert result["calls"] == 1 and attestor.call_index == 1
    assert result["accepted"] + result["rejected"] == 100


def test_document_review_can_attest_smart_without_reading_translation(
    tmp_path: Path,
) -> None:
    job_dir, source, _note_data, segment_id = _job(tmp_path)
    _add_smart_candidate(job_dir, source, segment_id)
    persist_semantic_candidates(
        job_dir, pipeline="document", note_type="translated",
        note_artifact="output/translated.md", candidates=[{
            "anchor": "该模型不超过 5 kg。", "prefix": "", "suffix": "",
            "section": "translated", "source_segment_id": segment_id,
            "transform_kind": "translated", "producer_component": "04_translate",
            "producer_invocation_id": "translate-session",
        }],
    )
    translated_before = (
        job_dir / "output/provenance/translated.json"
    ).read_bytes()
    exact_dir = job_dir / "output/provenance_exact"
    exact_dir.mkdir()
    (job_dir / "output/provenance/smart.json").replace(exact_dir / "smart.json")
    hashes = semantic_attestation_input_hashes(
        job_dir,
        note_types=("smart",),
        exact_provenance_dir="output/provenance_exact",
    )

    result = finalize_pending_semantic_provenance(
        job_dir, pipeline="document", ai=_Attestor(job_dir),
        note_types=("smart",),
        exact_provenance_dir="output/provenance_exact",
    )
    commit = json.loads((job_dir / "output/provenance/semantic_batch.json").read_text())

    assert set(hashes) == {"semantic_smart_candidate", "semantic_smart_final"}
    assert result["note_types"] == 1 and result["accepted"] == 1
    assert (job_dir / "output/provenance/smart.json").is_file()
    assert (exact_dir / "smart.json").is_file()
    assert [item["note_type"] for item in commit["candidate_manifests"]] == ["smart"]
    assert (job_dir / "output/provenance/translated.json").read_bytes() == translated_before


@pytest.mark.parametrize("note_types", [(), ("smart", "smart"), ("unknown",)])
def test_semantic_attestation_note_type_scope_fails_closed(
    tmp_path: Path, note_types,
) -> None:
    job_dir, _source, _note_data, _segment_id = _job(tmp_path)
    with pytest.raises(ValueError, match="note type"):
        finalize_pending_semantic_provenance(
            job_dir, pipeline="document", ai=_Attestor(job_dir),
            note_types=note_types,
        )


def test_batch_selects_smart_then_translated_with_one_bounded_call(
    tmp_path: Path,
) -> None:
    job_dir, source, _note_data, segment_id = _job(tmp_path)
    _replace_note_candidates(
        job_dir, source, segment_id, note_type="smart", count=51,
    )
    _replace_note_candidates(
        job_dir, source, segment_id, note_type="translated", count=100,
    )
    attestor = _Attestor(job_dir)
    result = finalize_pending_semantic_provenance(
        job_dir, pipeline="document", ai=attestor,
    )
    records = [json.loads(line) for line in (
        job_dir / "output/ai_logs/06_semantic_attestation.jsonl"
    ).read_text().splitlines()]
    items = [
        item
        for record in records
        for item in json.loads(
            record["prompt"]["rendered"]["user"].split("INPUT=", 1)[1]
        )["items"]
    ]
    assert all(
        len(record["prompt"]["rendered"]["user"].encode("utf-8")) <= 64 * 1024
        for record in records
    )
    assert [item["note_type"] for item in items] == ["smart"] * 51 + [
        "translated"
    ] * 100
    assert result["note_types"] == 2
    assert result["budget_rejected"] == 0 and result["degraded"] is False
    assert result["accepted"] + result["rejected"] == 151
    assert result["failed"] == 0 and result["calls"] == 2
    assert attestor.call_index == 2


@pytest.mark.parametrize("failure", [RuntimeError("second write"), KeyboardInterrupt()])
def test_second_final_publish_failure_or_interrupt_rolls_back_batch(
    tmp_path: Path, monkeypatch, failure: BaseException,
) -> None:
    job_dir, source, _note_data, segment_id = _job(tmp_path)
    _add_smart_candidate(job_dir, source, segment_id)
    persist_semantic_candidates(
        job_dir, pipeline="document", note_type="translated",
        note_artifact="output/translated.md", candidates=[{
            "anchor": "该模型不超过 5 kg。", "prefix": "", "suffix": "",
            "section": "translated", "source_segment_id": segment_id,
            "transform_kind": "translated",
            "producer_component": "04_translate",
            "producer_invocation_id": "producer-translate-session",
        }],
    )
    finals = [
        job_dir / "output/provenance/smart.json",
        job_dir / "output/provenance/translated.json",
    ]
    before = [path.read_bytes() for path in finals]
    original_replace = Path.replace
    replaced = 0

    def fail_second_staged(self: Path, target: Path):
        nonlocal replaced
        if self.name.endswith(".staged"):
            replaced += 1
            if replaced == 2:
                raise failure
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", fail_second_staged)
    with pytest.raises(type(failure)):
        finalize_pending_semantic_provenance(
            job_dir, pipeline="document", ai=_Attestor(job_dir),
        )
    assert [path.read_bytes() for path in finals] == before
    assert not (job_dir / "output/provenance/semantic_batch.json").exists()


@pytest.mark.asyncio
async def test_cross_job_batch_replay_fails_closed(tmp_path: Path) -> None:
    job_dir, _source, note_data, segment_id = _job(tmp_path)
    persist_semantic_candidates(
        job_dir, pipeline="document", note_type="translated",
        note_artifact="output/translated.md", candidates=[{
            "anchor": "该模型不超过 5 kg。", "prefix": "", "suffix": "",
            "section": "translated", "source_segment_id": segment_id,
            "transform_kind": "translated",
            "producer_component": "04_translate",
            "producer_invocation_id": "producer-session",
        }],
    )
    finalize_pending_semantic_provenance(
        job_dir, pipeline="document", ai=_Attestor(job_dir),
    )
    commit_path = job_dir / "output/provenance/semantic_batch.json"
    commit = json.loads(commit_path.read_text())
    commit["job_id"] = "another-job"
    commit_path.write_bytes(canonical_json_bytes(commit))
    with pytest.raises(CanonicalEvidenceError, match="identity"):
        await _records(job_dir, note_data)


@pytest.mark.asyncio
async def test_ai_log_record_replacement_and_unbounded_history_fail_closed(
    tmp_path: Path,
) -> None:
    job_dir, _source, note_data, segment_id = _job(tmp_path)
    persist_semantic_candidates(
        job_dir, pipeline="document", note_type="translated",
        note_artifact="output/translated.md", candidates=[{
            "anchor": "该模型不超过 5 kg。", "prefix": "", "suffix": "",
            "section": "translated", "source_segment_id": segment_id,
            "transform_kind": "translated",
            "producer_component": "04_translate",
            "producer_invocation_id": "producer-session",
        }],
    )
    finalize_pending_semantic_provenance(
        job_dir, pipeline="document", ai=_Attestor(job_dir),
    )
    commit = json.loads((
        job_dir / "output/provenance/semantic_batch.json"
    ).read_text())
    log_path = job_dir / commit["ai_logs"][0]["path"]
    original = log_path.read_bytes()
    record = json.loads(original)
    record["output"]["content"] = json.dumps({"schema_version": 1, "decisions": []})
    log_path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    with pytest.raises(CanonicalEvidenceError, match="ai_log identity changed"):
        await _records(job_dir, note_data)
    log_path.write_bytes(original + b"{}\n" * 128)
    with pytest.raises(CanonicalEvidenceError, match="too many records"):
        await _records(job_dir, note_data)


@pytest.mark.asyncio
async def test_growing_ai_log_history_is_reduced_to_an_immutable_record_proof(
    tmp_path: Path,
) -> None:
    job_dir, _source, note_data, segment_id = _job(tmp_path)
    persist_semantic_candidates(
        job_dir, pipeline="document", note_type="translated",
        note_artifact="output/translated.md", candidates=[{
            "anchor": "该模型不超过 5 kg。", "prefix": "", "suffix": "",
            "section": "translated", "source_segment_id": segment_id,
            "transform_kind": "translated",
            "producer_component": "04_translate",
            "producer_invocation_id": "producer-session",
        }],
    )
    history = job_dir / "output/ai_logs/06_semantic_attestation.jsonl"
    history.parent.mkdir(parents=True, exist_ok=True)
    history.write_bytes(
        b'{"history":"' + b"x" * MAX_SEMANTIC_AI_LOG_HISTORY_BYTES + b'"}\n'
    )
    assert history.stat().st_size > MAX_SEMANTIC_AI_LOG_HISTORY_BYTES

    finalize_pending_semantic_provenance(
        job_dir, pipeline="document", ai=_Attestor(job_dir),
    )

    commit = json.loads((
        job_dir / "output/provenance/semantic_batch.json"
    ).read_text())
    proof = job_dir / commit["ai_logs"][0]["path"]
    assert proof != history
    assert proof.stat().st_size < 2 * 1024 * 1024
    assert len(proof.read_text().splitlines()) == 1
    assert await _records(job_dir, note_data)


@pytest.mark.asyncio
async def test_immutable_proof_survives_missing_cumulative_log_flush(
    tmp_path: Path,
) -> None:
    job_dir, source_manifest, note_data, segment_id = _job(tmp_path)
    _replace_note_candidates(
        job_dir, source_manifest, segment_id, note_type="translated", count=1,
    )
    note_data = (job_dir / "output/translated.md").read_bytes()

    finalize_pending_semantic_provenance(
        job_dir, pipeline="document", ai=_MemoryProofAttestor(job_dir),
    )

    commit = json.loads((
        job_dir / "output/provenance/semantic_batch.json"
    ).read_text())
    assert not (
        job_dir / "output/ai_logs/06_semantic_attestation.jsonl"
    ).exists()
    assert (job_dir / commit["ai_logs"][0]["path"]).is_file()
    assert await _records(job_dir, note_data)


@pytest.mark.asyncio
async def test_reader_rejects_resigned_v4_decision_ref_tamper(
    tmp_path: Path,
) -> None:
    job_dir, _source, note_data, segment_id = _job(tmp_path)
    persist_semantic_candidates(
        job_dir, pipeline="document", note_type="translated",
        note_artifact="output/translated.md", candidates=[{
            "anchor": "该模型不超过 5 kg。", "prefix": "", "suffix": "",
            "section": "translated", "source_segment_id": segment_id,
            "transform_kind": "translated",
            "producer_component": "04_translate",
            "producer_invocation_id": "producer-session",
        }],
    )
    finalize_pending_semantic_provenance(
        job_dir, pipeline="document", ai=_Attestor(job_dir),
    )
    commit_path = job_dir / "output/provenance/semantic_batch.json"
    commit = json.loads(commit_path.read_text())
    log_path = job_dir / commit["ai_logs"][0]["path"]
    record = json.loads(log_path.read_text())
    response = json.loads(record["output"]["content"])
    response["decisions"][0]["decision_id"] = "D000"
    response_content = json.dumps(response)
    record["output"]["content"] = response_content
    log_path.write_bytes(canonical_json_bytes(record) + b"\n")

    ai_log = commit["ai_logs"][0]
    ai_log["response_content_sha256"] = _sha(response_content.encode())
    ai_log["response_decision_sha256"] = _sha(
        canonical_json_bytes(response["decisions"])
    )
    ai_log["record_sha256"] = _sha(canonical_json_bytes(record))
    commit["batch_id"] = semantic_attestation_batch_id(
        job_id=commit["job_id"],
        pipeline=commit["pipeline"],
        attestor_component=commit["attestor_component"],
        candidate_manifests=commit["candidate_manifests"],
        ai_logs=commit["ai_logs"],
    )

    provenance_path = job_dir / "output/provenance/translated.json"
    provenance = json.loads(provenance_path.read_text())
    attestation = provenance["segments"][0]["attestation"]
    attestation["batch_id"] = commit["batch_id"]
    attestation["ai_log"] = {
        **ai_log,
        "response_decision_sha256": _sha(
            canonical_json_bytes(response["decisions"][0])
        ),
    }
    provenance_data = canonical_json_bytes(provenance)
    provenance_path.write_bytes(provenance_data)
    next(
        item for item in commit["provenance_manifests"]
        if item["note_type"] == "translated"
    )["sha256"] = _sha(provenance_data)
    commit_path.write_bytes(canonical_json_bytes(commit))

    with pytest.raises(CanonicalEvidenceError, match="decision set changed"):
        await _records(job_dir, note_data)


@pytest.mark.parametrize("parallel", [False, True])
def test_utf8_prompt_budget_uses_multiple_complete_bounded_calls(
    tmp_path: Path, parallel: bool,
) -> None:
    job_dir, source, _note_data, segment_id = _job(tmp_path)
    source["segments"][0]["support_text"] = "S" * 4000
    write_source_manifest(
        job_dir / "intermediate/source_segments.json", source, trusted_root=job_dir,
    )
    claims = [
        f"Claim {chr(0x4e00 + index)} remains semantically equivalent."
        for index in range(20)
    ]
    note = "# Translation\n\n" + "\n\n".join(claims)
    note_data = note.encode()
    (job_dir / "output/translated.md").write_bytes(note_data)
    normalized = _markdown_to_text(note)
    empty = build_provenance_manifest(
        job_id=job_dir.name,
        note_type="translated",
        note_artifact="output/translated.md",
        note_bytes=note_data,
        normalized_body=normalized,
        source_manifest_path="intermediate/source_segments.json",
        source_manifest=source,
        segments=[],
    )
    write_provenance_manifest(
        job_dir / "output/provenance/translated.json",
        empty,
        trusted_root=job_dir,
        source_manifest=source,
        note_bytes=note_data,
        normalized_body=normalized,
    )
    persist_semantic_candidates(
        job_dir,
        pipeline="document",
        note_type="translated",
        note_artifact="output/translated.md",
        candidates=[{
            "anchor": claim,
            "prefix": "",
            "suffix": "",
            "section": "translated",
            "source_segment_id": segment_id,
            "transform_kind": "translated",
            "producer_component": "04_translate",
            "producer_invocation_id": "producer-session",
        } for claim in claims],
    )
    attestor = (
        _ParallelAttestor(job_dir, parallelism=4)
        if parallel else _Attestor(job_dir)
    )
    result = finalize_pending_semantic_provenance(
        job_dir, pipeline="document", ai=attestor,
    )
    commit = json.loads((
        job_dir / "output/provenance/semantic_batch.json"
    ).read_text())
    records = [
        json.loads((job_dir / item["path"]).read_text())
        for item in commit["ai_logs"]
    ]
    assert all(
        len(record["prompt"]["rendered"]["user"].encode("utf-8")) <= 64 * 1024
        for record in records
    )
    assert result["accepted"] + result["rejected"] == len(claims)
    assert result["budget_rejected"] == 0 and result["degraded"] is False
    assert result["calls"] == attestor.call_index == len(records) > 1


def test_oversized_candidate_fails_closed_without_skipping_to_later_candidate(
    tmp_path: Path,
) -> None:
    job_dir, source, _note_data, segment_id = _job(tmp_path)
    giant = "The model does not exceed 5 kg. " + "G" * (65 * 1024)
    small = "A later small claim says the model does not exceed 5 kg."
    _replace_note_candidates_with_claims(
        job_dir,
        source,
        segment_id,
        note_type="translated",
        claims=[giant, small],
    )
    attestor = _Attestor(job_dir)
    before = (job_dir / "output/provenance/translated.json").read_bytes()
    with pytest.raises(ValueError, match="candidate group exceeds prompt budget"):
        finalize_pending_semantic_provenance(
            job_dir, pipeline="document", ai=attestor,
        )
    assert attestor.call_index == 0
    assert (job_dir / "output/provenance/translated.json").read_bytes() == before
    assert not (job_dir / "output/provenance/semantic_batch.json").exists()


@pytest.mark.asyncio
async def test_single_oversized_candidate_fails_without_commit_or_call(
    tmp_path: Path,
) -> None:
    job_dir, source, _note_data, segment_id = _job(tmp_path)
    _replace_note_candidates_with_claims(
        job_dir,
        source,
        segment_id,
        note_type="translated",
        claims=["G" * (65 * 1024)],
    )
    note_data = (job_dir / "output/translated.md").read_bytes()
    attestor = _Attestor(job_dir)
    before = (job_dir / "output/provenance/translated.json").read_bytes()
    with pytest.raises(ValueError, match="candidate group exceeds prompt budget"):
        finalize_pending_semantic_provenance(
            job_dir, pipeline="document", ai=attestor,
        )
    assert attestor.call_index == 0
    assert (job_dir / "output/provenance/translated.json").read_bytes() == before
    assert not (job_dir / "output/provenance/semantic_batch.json").exists()
    assert await _records(job_dir, note_data) == []


def test_221_candidates_use_three_calls_and_commit_all(tmp_path: Path) -> None:
    job_dir, source, _note_data, segment_id = _job(tmp_path)
    _replace_note_candidates(
        job_dir, source, segment_id, note_type="translated", count=221,
    )
    attestor = _Attestor(job_dir)

    result = finalize_pending_semantic_provenance(
        job_dir, pipeline="document", ai=attestor,
    )

    commit = json.loads((job_dir / "output/provenance/semantic_batch.json").read_text())
    assert result["calls"] == attestor.call_index == 3
    assert result["accepted"] + result["rejected"] == 221
    assert commit["schema_version"] == 2
    assert len(commit["ai_logs"]) == 3


def test_513_candidates_are_rejected_by_global_limit(tmp_path: Path) -> None:
    job_dir, source, _note_data, segment_id = _job(tmp_path)
    with pytest.raises(ValueError, match="candidates exceed limit"):
        _replace_note_candidates(
            job_dir, source, segment_id, note_type="translated", count=513,
        )


def test_thirty_two_batches_commit_all(tmp_path: Path) -> None:
    job_dir, source, _note_data, segment_id = _job(tmp_path)
    source["segments"][0]["support_text"] = "S" * 4000
    write_source_manifest(
        job_dir / "intermediate/source_segments.json", source, trusted_root=job_dir,
    )
    _replace_note_candidates(
        job_dir, source, segment_id, note_type="translated", count=435,
    )
    attestor = _Attestor(job_dir)

    result = finalize_pending_semantic_provenance(
        job_dir, pipeline="document", ai=attestor,
    )

    commit = json.loads((job_dir / "output/provenance/semantic_batch.json").read_text())
    assert result["calls"] == attestor.call_index == 32
    assert result["accepted"] + result["rejected"] == 435
    assert len(commit["ai_logs"]) == 32


def test_thirty_three_batches_fail_before_first_ai_call(tmp_path: Path) -> None:
    job_dir, source, _note_data, segment_id = _job(tmp_path)
    source["segments"][0]["support_text"] = "S" * 4000
    write_source_manifest(
        job_dir / "intermediate/source_segments.json", source, trusted_root=job_dir,
    )
    _replace_note_candidates(
        job_dir, source, segment_id, note_type="translated", count=450,
    )
    before = (job_dir / "output/provenance/translated.json").read_bytes()
    attestor = _Attestor(job_dir)

    with pytest.raises(ValueError, match="batches exceed global limit"):
        finalize_pending_semantic_provenance(
            job_dir, pipeline="document", ai=attestor,
        )

    assert attestor.call_index == 0
    assert (job_dir / "output/provenance/translated.json").read_bytes() == before
    assert not (job_dir / "output/provenance/semantic_batch.json").exists()


def test_second_batch_failure_leaves_no_partial_commit(tmp_path: Path) -> None:
    job_dir, source, _note_data, segment_id = _job(tmp_path)
    _replace_note_candidates(
        job_dir, source, segment_id, note_type="translated", count=101,
    )
    before = (job_dir / "output/provenance/translated.json").read_bytes()
    attestor = _SecondCallFailAttestor(job_dir)

    with pytest.raises(AIProviderError, match="second attestation call failed"):
        finalize_pending_semantic_provenance(
            job_dir, pipeline="document", ai=attestor,
        )

    assert attestor.call_index == 1
    assert (job_dir / "output/provenance/translated.json").read_bytes() == before
    assert not (job_dir / "output/provenance/semantic_batch.json").exists()


@pytest.mark.asyncio
async def test_unselected_candidate_manifest_is_still_fully_validated(
    tmp_path: Path,
) -> None:
    job_dir, source, _note_data, segment_id = _job(tmp_path)
    _replace_note_candidates(
        job_dir, source, segment_id, note_type="smart", count=51,
    )
    _replace_note_candidates(
        job_dir, source, segment_id, note_type="translated", count=100,
    )
    finalize_pending_semantic_provenance(
        job_dir, pipeline="document", ai=_Attestor(job_dir),
    )
    candidate_path = job_dir / "output/provenance_candidates/translated.json"
    candidate_manifest = json.loads(candidate_path.read_text())
    candidate_manifest["candidates"][-1]["anchor"] = "tampered overflow claim"
    candidate_data = canonical_json_bytes(candidate_manifest)
    candidate_path.write_bytes(candidate_data)
    commit_path = job_dir / "output/provenance/semantic_batch.json"
    commit = json.loads(commit_path.read_text())
    next(
        item for item in commit["candidate_manifests"]
        if item["note_type"] == "translated"
    )["sha256"] = _sha(candidate_data)
    commit_path.write_bytes(canonical_json_bytes(commit))
    with pytest.raises(CanonicalEvidenceError, match="anchor does not match"):
        await _records(job_dir, (job_dir / "output/translated.md").read_bytes())


@pytest.mark.asyncio
async def test_reader_rejects_mapping_rebound_to_another_selected_candidate(
    tmp_path: Path,
) -> None:
    job_dir, source, _note_data, segment_id = _job(tmp_path)
    _replace_note_candidates(
        job_dir, source, segment_id, note_type="smart", count=51,
    )
    _replace_note_candidates(
        job_dir, source, segment_id, note_type="translated", count=100,
    )
    finalize_pending_semantic_provenance(
        job_dir, pipeline="document", ai=_Attestor(job_dir),
    )
    candidate_manifest = json.loads((
        job_dir / "output/provenance_candidates/translated.json"
    ).read_text())
    other_candidate_id = candidate_manifest["candidates"][-1]["candidate_id"]
    provenance_path = job_dir / "output/provenance/translated.json"
    provenance = json.loads(provenance_path.read_text())
    provenance["segments"][0]["attestation"]["candidate_id"] = other_candidate_id
    provenance_data = canonical_json_bytes(provenance)
    provenance_path.write_bytes(provenance_data)
    commit_path = job_dir / "output/provenance/semantic_batch.json"
    commit = json.loads(commit_path.read_text())
    next(
        item for item in commit["provenance_manifests"]
        if item["note_type"] == "translated"
    )["sha256"] = _sha(provenance_data)
    commit_path.write_bytes(canonical_json_bytes(commit))
    with pytest.raises(CanonicalEvidenceError, match="semantic attestation binding changed"):
        await _records(job_dir, (job_dir / "output/translated.md").read_bytes())


@pytest.mark.parametrize("mode", ["missing", "reordered"])
def test_materializer_rejects_missing_or_reordered_selected_decisions(
    tmp_path: Path,
    mode: str,
) -> None:
    job_dir, source, _note_data, segment_id = _job(tmp_path)
    _replace_note_candidates(
        job_dir, source, segment_id, note_type="translated", count=2,
    )
    manifest = json.loads((
        job_dir / "output/provenance_candidates/translated.json"
    ).read_text())
    candidate_ids = [item["candidate_id"] for item in manifest["candidates"]]
    decisions = [{
        "candidate_id": candidate_id,
        "decision": "supported",
        "confidence_ppm": 990_000,
        "reason_codes": ["semantic_equivalent", "critical_facts_match"],
    } for candidate_id in candidate_ids]
    decisions = decisions[:1] if mode == "missing" else list(reversed(decisions))
    with pytest.raises(ValueError, match="response is incomplete"):
        materialize_semantic_attestations(
            manifest,
            source,
            response_text=json.dumps({"schema_version": 1, "decisions": decisions}),
            attestor_component="06_semantic_attestation",
            attestor_invocation_id="attestor-session",
            attestor_provider="claude-cli",
            attestor_model="claude-opus-4-8",
            attestor_prompt="prompt",
            ai_log_binding={},
            batch_id="0" * 64,
            response_candidate_ids=candidate_ids,
        )


def _v4_decision(decision_id: object) -> dict:
    return {
        "decision_id": decision_id,
        "decision": "rejected",
        "confidence_ppm": 990_000,
        "reason_codes": ["semantic_mismatch"],
    }


def test_legacy_direct_parser_compatibility_does_not_relax_v3_write_gate(
    tmp_path: Path,
) -> None:
    job_dir, source, _note_data, segment_id = _job(tmp_path)
    _replace_note_candidates(
        job_dir, source, segment_id, note_type="translated", count=2,
    )
    manifest = json.loads((
        job_dir / "output/provenance_candidates/translated.json"
    ).read_text())
    candidate_ids = [item["candidate_id"] for item in manifest["candidates"]]
    response_text = json.dumps({
        "schema_version": 1,
        "decisions": [{
            "candidate_id": candidate_id,
            "decision": "rejected",
            "confidence_ppm": 990_000,
            "reason_codes": ["semantic_mismatch"],
        } for candidate_id in candidate_ids],
    })
    kwargs = {
        "response_text": response_text,
        "attestor_component": "06_semantic_attestation",
        "attestor_invocation_id": "attestor-session",
        "attestor_provider": "qoder-cli",
        "attestor_model": "ultimate",
        "attestor_prompt": "prompt",
        "ai_log_binding": {},
        "batch_id": "0" * 64,
        "response_candidate_ids": candidate_ids,
    }
    accepted, rejected = materialize_semantic_attestations(
        manifest, source, **kwargs,
    )
    assert accepted == []
    assert [item["candidate_id"] for item in rejected] == candidate_ids
    with pytest.raises(ValueError, match="response schema is invalid"):
        materialize_semantic_attestations(
            manifest,
            source,
            required_response_schema=SEMANTIC_ATTESTOR_RESPONSE_SCHEMA_VERSION,
            **kwargs,
        )


def test_writer_rejects_legacy_response_without_partial_publish(
    tmp_path: Path,
) -> None:
    job_dir, _source, _note_data, segment_id = _job(tmp_path)
    persist_semantic_candidates(
        job_dir, pipeline="document", note_type="translated",
        note_artifact="output/translated.md", candidates=[{
            "anchor": "该模型不超过 5 kg。", "prefix": "", "suffix": "",
            "section": "translated", "source_segment_id": segment_id,
            "transform_kind": "translated",
            "producer_component": "04_translate",
            "producer_invocation_id": "producer-session",
        }],
    )
    provenance_path = job_dir / "output/provenance/translated.json"
    before = provenance_path.read_bytes()
    with pytest.raises(AIProviderError, match="response schema is invalid"):
        finalize_pending_semantic_provenance(
            job_dir,
            pipeline="document",
            ai=_Attestor(job_dir, response_schema=1),
        )
    assert provenance_path.read_bytes() == before
    assert not (job_dir / "output/provenance/semantic_batch.json").exists()


@pytest.mark.parametrize(
    "decision_ids",
    [
        ["d000"],
        ["d000", "d001", "d002"],
        ["d000", "d000"],
        ["d001", "d000"],
        ["d000", "d002"],
        [0, "d001"],
        ["D000", "d001"],
    ],
    ids=["missing", "extra", "duplicate", "reordered", "skipped", "type", "case"],
)
def test_v4_materializer_rejects_invalid_decision_ref_vectors(
    tmp_path: Path,
    decision_ids: list[object],
) -> None:
    job_dir, source, _note_data, segment_id = _job(tmp_path)
    _replace_note_candidates(
        job_dir, source, segment_id, note_type="translated", count=2,
    )
    manifest = json.loads((
        job_dir / "output/provenance_candidates/translated.json"
    ).read_text())
    candidate_ids = [item["candidate_id"] for item in manifest["candidates"]]
    with pytest.raises(ValueError, match="response is incomplete"):
        materialize_semantic_attestations(
            manifest,
            source,
            response_text=json.dumps({
                "schema_version": 4,
                "decisions": [_v4_decision(item) for item in decision_ids],
            }),
            attestor_component="06_semantic_attestation",
            attestor_invocation_id="attestor-session",
            attestor_provider="qoder-cli",
            attestor_model="ultimate",
            attestor_prompt="prompt",
            ai_log_binding={},
            batch_id="0" * 64,
            response_candidate_ids=candidate_ids,
        )


def test_v4_short_refs_map_back_to_full_candidate_ids(tmp_path: Path) -> None:
    job_dir, source, _note_data, segment_id = _job(tmp_path)
    _replace_note_candidates(
        job_dir, source, segment_id, note_type="translated", count=3,
    )
    manifest = json.loads((
        job_dir / "output/provenance_candidates/translated.json"
    ).read_text())
    candidate_ids = [item["candidate_id"] for item in manifest["candidates"]]
    prompt = build_semantic_attestation_prompt(
        manifest, source, protocol="Return the required response.",
    )
    request = json.loads(prompt.split("INPUT=", 1)[1])
    assert [item["decision_id"] for item in request["items"]] == [
        "d000", "d001", "d002",
    ]
    assert all(candidate_id not in prompt for candidate_id in candidate_ids)

    accepted, rejected = materialize_semantic_attestations(
        manifest,
        source,
        response_text=json.dumps({
            "schema_version": 4,
            "decisions": [
                _v4_decision(f"d{index:03d}") for index in range(3)
            ],
        }),
        attestor_component="06_semantic_attestation",
        attestor_invocation_id="attestor-session",
        attestor_provider="qoder-cli",
        attestor_model="ultimate",
        attestor_prompt=prompt,
        ai_log_binding={},
        batch_id="0" * 64,
        response_candidate_ids=candidate_ids,
    )
    assert accepted == []
    assert [item["candidate_id"] for item in rejected] == candidate_ids
