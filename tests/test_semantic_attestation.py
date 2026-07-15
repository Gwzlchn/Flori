"""验证语义候选只能由下游 concepts 独立提升为 canonical evidence。"""

from __future__ import annotations

import hashlib
import json
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
    build_provenance_manifest,
    build_source_manifest,
    canonical_json_bytes,
    make_segment_id,
    write_provenance_manifest,
    write_source_manifest,
)
from steps.utils.provenance_attestation import (
    finalize_pending_semantic_provenance,
    persist_semantic_candidates,
)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _job(tmp_path: Path) -> tuple[Path, dict, bytes, str]:
    job_dir = tmp_path / "job-semantic"
    source_data = b"The model does not exceed 5 kg."
    source_path = job_dir / "output" / "original.md"
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
        "article:body", start=0, end=len(source_data), section="body", locator=locator,
    )
    source_manifest = build_source_manifest(
        job_id=job_dir.name,
        pipeline="article",
        source_artifacts=[{
            "source_id": "article:body",
            "path": "output/original.md",
            "sha256": _sha(source_data),
            "revision": None,
            "media_duration_ms": None,
            "page_count": None,
        }],
        segments=[{
            "segment_id": segment_id,
            "source_id": "article:body",
            "start": 0,
            "end": len(source_data),
            "section": "body",
            "locator": locator,
            "support_text": source_data.decode(),
            "support_artifact": {
                "kind": "html",
                "path": "output/original.md",
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
        pipeline="article",
        note_type="smart",
        note_artifact="output/smart.md",
        candidates=[{
            "anchor": "该模型不超过 5 kg。",
            "prefix": "",
            "suffix": "",
            "section": "smart",
            "source_segment_id": segment_id,
            "transform_kind": "cross_language",
            "producer_component": "04_smart_article",
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
    note_artifact = "output/smart.md" if note_type == "smart" else "output/translated.md"
    component = "04_smart_article" if note_type == "smart" else "04_translate_article"
    claims = [f"Semantic {note_type} claim {index} remains stable." for index in range(count)]
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
        pipeline="article",
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
    step_name = "04_semantic_attestation"

    def __init__(self, job_dir: Path) -> None:
        self.job_dir = job_dir
        self.last_response = None
        self.call_index = 0
        self.log = _Log()

    def call(self, prompt: str, **_kwargs) -> str:
        request = json.loads(prompt.split("INPUT=", 1)[1])
        decisions = [{
            "candidate_id": item["candidate_id"],
            "decision": "supported",
            "confidence_ppm": 990_000,
            "reason_codes": ["semantic_equivalent", "critical_facts_match"],
        } for item in request["items"]]
        content = json.dumps({"schema_version": 1, "decisions": decisions})
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
        self.call_index += 1
        return content


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
        pipeline="article",
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
        "producer_component": "04_translate_article",
        "producer_invocation_id": "producer-session",
    }
    persist_semantic_candidates(
        job_dir,
        pipeline="article",
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
        job_dir, pipeline="article", ai=_Attestor(job_dir),
    )

    assert {key: result[key] for key in ("note_types", "accepted", "rejected", "failed", "calls")} == {
        "note_types": 1, "accepted": 1, "rejected": 0, "failed": 0, "calls": 1,
    }
    final = json.loads((
        job_dir / "output" / "provenance" / "translated.json"
    ).read_text())
    assert final["schema_version"] == 3
    attestation = final["segments"][0]["attestation"]
    assert attestation["producer_component"] == "04_translate_article"
    assert attestation["attestor_component"] == "04_semantic_attestation"
    assert len(await _records(job_dir, note_data)) == 1


@pytest.mark.asyncio
async def test_final_attestation_tampering_fails_closed(tmp_path: Path) -> None:
    job_dir, _source_manifest, note_data, segment_id = _job(tmp_path)
    persist_semantic_candidates(
        job_dir,
        pipeline="article",
        note_type="translated",
        note_artifact="output/translated.md",
        candidates=[{
            "anchor": "该模型不超过 5 kg。",
            "prefix": "",
            "suffix": "",
            "section": "translated",
            "source_segment_id": segment_id,
            "transform_kind": "translated",
            "producer_component": "04_translate_article",
            "producer_invocation_id": "producer-session",
        }],
    )
    finalize_pending_semantic_provenance(
        job_dir, pipeline="article", ai=_Attestor(job_dir),
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
        pipeline="article",
        note_type="translated",
        note_artifact="output/translated.md",
        candidates=[{
            "anchor": "该模型不超过 5 kg。",
            "prefix": "",
            "suffix": "",
            "section": "translated",
            "source_segment_id": segment_id,
            "transform_kind": "translated",
            "producer_component": "04_translate_article",
            "producer_invocation_id": "producer-session",
        }],
    )
    finalize_pending_semantic_provenance(
        job_dir, pipeline="article", ai=_Attestor(job_dir),
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
        pipeline="article",
        note_type="smart",
        note_artifact="output/smart.md",
        candidates=[{
            "anchor": "该模型不超过 5 kg。",
            "prefix": "",
            "suffix": "",
            "section": "智能笔记",
            "source_segment_id": segment_id,
            "transform_kind": "translated",
            "producer_component": "04_smart_article",
            "producer_invocation_id": "producer-session",
        }],
    )
    before = provenance_path.read_bytes()

    attestor = _FlakyAttestor(job_dir)
    with pytest.raises(AIProviderError, match="semantic attestation failed"):
        finalize_pending_semantic_provenance(
            job_dir, pipeline="article", ai=attestor,
        )
    assert provenance_path.read_bytes() == before
    assert json.loads(before)["schema_version"] == 2
    candidate_path = job_dir / "output" / "provenance_candidates" / "smart.json"
    assert candidate_path.is_file()

    result = finalize_pending_semantic_provenance(
        job_dir, pipeline="article", ai=attestor,
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
        "producer_component": "04_translate_article",
        "producer_invocation_id": "producer-session",
    }
    persist_semantic_candidates(
        job_dir, pipeline="article", note_type="translated",
        note_artifact="output/translated.md", candidates=[candidate],
    )
    state = persist_semantic_candidates(
        job_dir, pipeline="article", note_type="translated",
        note_artifact="output/translated.md", candidates=[],
    )
    manifest = json.loads((
        job_dir / "output/provenance_candidates/translated.json"
    ).read_text())
    assert state == {"status": "empty", "candidates": 0}
    assert manifest["status"] == "empty" and manifest["candidates"] == []


def test_dual_note_types_are_attested_in_one_call(tmp_path: Path) -> None:
    job_dir, source, _note_data, segment_id = _job(tmp_path)
    _add_smart_candidate(job_dir, source, segment_id)
    persist_semantic_candidates(
        job_dir, pipeline="article", note_type="translated",
        note_artifact="output/translated.md", candidates=[{
            "anchor": "该模型不超过 5 kg。", "prefix": "", "suffix": "",
            "section": "translated", "source_segment_id": segment_id,
            "transform_kind": "translated",
            "producer_component": "04_translate_article",
            "producer_invocation_id": "producer-translate-session",
        }],
    )
    attestor = _Attestor(job_dir)
    result = finalize_pending_semantic_provenance(
        job_dir, pipeline="article", ai=attestor,
    )
    commit = json.loads((job_dir / "output/provenance/semantic_batch.json").read_text())
    assert result["note_types"] == 2 and result["accepted"] == 2
    assert result["calls"] == 1 and attestor.call_index == 1
    assert [item["note_type"] for item in commit["provenance_manifests"]] == [
        "smart", "translated",
    ]


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
        job_dir, pipeline="article", ai=attestor,
    )
    assert result["calls"] == 1 and attestor.call_index == 1
    assert result["accepted"] + result["rejected"] == 100


def test_batch_candidate_limit_rejects_dual_101_before_ai_call(tmp_path: Path) -> None:
    job_dir, source, _note_data, segment_id = _job(tmp_path)
    _replace_note_candidates(
        job_dir, source, segment_id, note_type="smart", count=51,
    )
    _replace_note_candidates(
        job_dir, source, segment_id, note_type="translated", count=50,
    )
    attestor = _Attestor(job_dir)
    with pytest.raises(ValueError, match="batch candidates exceed limit"):
        finalize_pending_semantic_provenance(
            job_dir, pipeline="article", ai=attestor,
        )
    assert attestor.call_index == 0
    assert not (job_dir / "output/provenance/semantic_batch.json").exists()


@pytest.mark.parametrize("failure", [RuntimeError("second write"), KeyboardInterrupt()])
def test_second_final_publish_failure_or_interrupt_rolls_back_batch(
    tmp_path: Path, monkeypatch, failure: BaseException,
) -> None:
    job_dir, source, _note_data, segment_id = _job(tmp_path)
    _add_smart_candidate(job_dir, source, segment_id)
    persist_semantic_candidates(
        job_dir, pipeline="article", note_type="translated",
        note_artifact="output/translated.md", candidates=[{
            "anchor": "该模型不超过 5 kg。", "prefix": "", "suffix": "",
            "section": "translated", "source_segment_id": segment_id,
            "transform_kind": "translated",
            "producer_component": "04_translate_article",
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
            job_dir, pipeline="article", ai=_Attestor(job_dir),
        )
    assert [path.read_bytes() for path in finals] == before
    assert not (job_dir / "output/provenance/semantic_batch.json").exists()


@pytest.mark.asyncio
async def test_cross_job_batch_replay_fails_closed(tmp_path: Path) -> None:
    job_dir, _source, note_data, segment_id = _job(tmp_path)
    persist_semantic_candidates(
        job_dir, pipeline="article", note_type="translated",
        note_artifact="output/translated.md", candidates=[{
            "anchor": "该模型不超过 5 kg。", "prefix": "", "suffix": "",
            "section": "translated", "source_segment_id": segment_id,
            "transform_kind": "translated",
            "producer_component": "04_translate_article",
            "producer_invocation_id": "producer-session",
        }],
    )
    finalize_pending_semantic_provenance(
        job_dir, pipeline="article", ai=_Attestor(job_dir),
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
        job_dir, pipeline="article", note_type="translated",
        note_artifact="output/translated.md", candidates=[{
            "anchor": "该模型不超过 5 kg。", "prefix": "", "suffix": "",
            "section": "translated", "source_segment_id": segment_id,
            "transform_kind": "translated",
            "producer_component": "04_translate_article",
            "producer_invocation_id": "producer-session",
        }],
    )
    finalize_pending_semantic_provenance(
        job_dir, pipeline="article", ai=_Attestor(job_dir),
    )
    log_path = job_dir / "output/ai_logs/04_semantic_attestation.jsonl"
    original = log_path.read_bytes()
    record = json.loads(original)
    record["output"]["content"] = json.dumps({"schema_version": 1, "decisions": []})
    log_path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    with pytest.raises(CanonicalEvidenceError, match="record changed"):
        await _records(job_dir, note_data)
    log_path.write_bytes(original + b"{}\n" * 128)
    with pytest.raises(CanonicalEvidenceError, match="too many records"):
        await _records(job_dir, note_data)


def test_utf8_prompt_budget_fails_before_ai_call(tmp_path: Path) -> None:
    job_dir, source, _note_data, segment_id = _job(tmp_path)
    source["segments"][0]["support_text"] = "S" * 4000
    write_source_manifest(
        job_dir / "intermediate/source_segments.json", source, trusted_root=job_dir,
    )
    claims = [f"Claim {index} remains semantically equivalent." for index in range(20)]
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
        pipeline="article",
        note_type="translated",
        note_artifact="output/translated.md",
        candidates=[{
            "anchor": claim,
            "prefix": "",
            "suffix": "",
            "section": "translated",
            "source_segment_id": segment_id,
            "transform_kind": "translated",
            "producer_component": "04_translate_article",
            "producer_invocation_id": "producer-session",
        } for claim in claims],
    )
    attestor = _Attestor(job_dir)
    with pytest.raises(ValueError, match="UTF-8 byte budget"):
        finalize_pending_semantic_provenance(
            job_dir, pipeline="article", ai=attestor,
        )
    assert attestor.call_index == 0
