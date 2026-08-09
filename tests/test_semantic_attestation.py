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
    materialize_semantic_attestations,
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

    def __init__(self, job_dir: Path) -> None:
        self.job_dir = job_dir
        self.last_response = None
        self.call_index = 0
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


def test_dual_note_types_are_attested_in_one_call(tmp_path: Path) -> None:
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
    record = json.loads((
        job_dir / "output/ai_logs/06_semantic_attestation.jsonl"
    ).read_text())
    items = json.loads(record["prompt"]["rendered"]["user"].split("INPUT=", 1)[1])[
        "items"
    ]
    assert len(record["prompt"]["rendered"]["user"].encode("utf-8")) <= 64 * 1024
    assert [item["note_type"] for item in items] == ["smart"] * 51 + [
        "translated"
    ] * 49
    assert result["note_types"] == 2
    assert result["budget_rejected"] == 51 and result["degraded"] is True
    assert result["accepted"] + result["rejected"] - result["budget_rejected"] == 100
    assert result["failed"] == 0 and result["calls"] == 1
    assert attestor.call_index == 1


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
    log_path = job_dir / "output/ai_logs/06_semantic_attestation.jsonl"
    original = log_path.read_bytes()
    record = json.loads(original)
    record["output"]["content"] = json.dumps({"schema_version": 1, "decisions": []})
    log_path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    with pytest.raises(CanonicalEvidenceError, match="record changed"):
        await _records(job_dir, note_data)
    log_path.write_bytes(original + b"{}\n" * 128)
    with pytest.raises(CanonicalEvidenceError, match="too many records"):
        await _records(job_dir, note_data)


def test_utf8_prompt_budget_degrades_to_a_single_bounded_call(tmp_path: Path) -> None:
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
    attestor = _Attestor(job_dir)
    result = finalize_pending_semantic_provenance(
        job_dir, pipeline="document", ai=attestor,
    )
    record = json.loads((
        job_dir / "output/ai_logs/06_semantic_attestation.jsonl"
    ).read_text())
    prompt = record["prompt"]["rendered"]["user"]
    selected_count = len(json.loads(prompt.split("INPUT=", 1)[1])["items"])
    assert len(prompt.encode("utf-8")) <= 64 * 1024
    assert 0 < result["accepted"] < len(claims)
    assert result["budget_rejected"] == len(claims) - selected_count
    assert result["accepted"] + result["rejected"] - result["budget_rejected"] == (
        selected_count
    )
    assert result["degraded"] is True
    assert attestor.call_index == 1


def test_oversized_candidate_does_not_starve_later_small_candidate(
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
    result = finalize_pending_semantic_provenance(
        job_dir, pipeline="document", ai=attestor,
    )
    record = json.loads((
        job_dir / "output/ai_logs/06_semantic_attestation.jsonl"
    ).read_text())
    items = json.loads(record["prompt"]["rendered"]["user"].split("INPUT=", 1)[1])[
        "items"
    ]
    assert [item["claim"] for item in items] == [small]
    assert result["accepted"] == 1
    assert result["budget_rejected"] == 1
    assert result["degraded"] is True


@pytest.mark.asyncio
async def test_single_oversized_candidate_publishes_degraded_batch_without_call(
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
    result = finalize_pending_semantic_provenance(
        job_dir, pipeline="document", ai=attestor,
    )
    commit = json.loads((
        job_dir / "output/provenance/semantic_batch.json"
    ).read_text())
    assert result["accepted"] == 0 and result["rejected"] == 1
    assert result["budget_rejected"] == 1 and result["degraded"] is True
    assert result["calls"] == 0 and attestor.call_index == 0
    assert commit["ai_log"] is None
    assert await _records(job_dir, note_data) == []


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
async def test_reader_rejects_mapping_for_budget_rejected_candidate(
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
    overflow_candidate_id = candidate_manifest["candidates"][-1]["candidate_id"]
    provenance_path = job_dir / "output/provenance/translated.json"
    provenance = json.loads(provenance_path.read_text())
    provenance["segments"][0]["attestation"]["candidate_id"] = overflow_candidate_id
    provenance_data = canonical_json_bytes(provenance)
    provenance_path.write_bytes(provenance_data)
    commit_path = job_dir / "output/provenance/semantic_batch.json"
    commit = json.loads(commit_path.read_text())
    next(
        item for item in commit["provenance_manifests"]
        if item["note_type"] == "translated"
    )["sha256"] = _sha(provenance_data)
    commit_path.write_bytes(canonical_json_bytes(commit))
    with pytest.raises(CanonicalEvidenceError, match="unselected semantic candidate"):
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
