"""概念佐证只计入重验可靠且真正独立的来源。"""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest

from api.services import concepts


def _evidence_id(index: int) -> str:
    return f"ce_{index:064x}"


def _occurrence(
    index: int,
    job_id: str,
    source_fingerprint: str,
    content_type: str,
    document_kind: str | None = None,
) -> dict:
    excerpt = f"fact-{index}"
    return {
        "evidence_id": _evidence_id(index),
        "job_id": job_id,
        "source_fingerprint": source_fingerprint,
        "content_type": content_type,
        "document_kind": document_kind,
        "evidence_excerpt": excerpt,
        "chunk_body_sha256": hashlib.sha256(excerpt.encode("utf-8")).hexdigest(),
    }


class FakeDatabase:
    def __init__(self, occurrences: list[dict], jobs: set[str]):
        self.occurrences = occurrences
        self.jobs = jobs

    def list_concept_occurrences(
        self, domain: str, term: str, *, include_invalid: bool = False,
    ) -> list[dict]:
        assert (domain, term, include_invalid) == ("ml", "RRF", True)
        return list(self.occurrences)

    def get_job(self, job_id: str):
        return SimpleNamespace(pipeline="document") if job_id in self.jobs else None

    def canonical_evidence_database_states(self, evidence_ids: list[str]) -> dict:
        by_id = {item["evidence_id"]: item for item in self.occurrences}
        def default_note_sha(item: dict) -> str:
            suffix = str(item["job_id"]).rsplit("-", 1)[-1]
            return f"{int(suffix) if suffix.isdigit() else 1:064x}"

        return {
            evidence_id: {
                "note_path": by_id[evidence_id].get(
                    "note_path", f"output/{by_id[evidence_id]['job_id']}.md",
                ),
                "note_sha256": by_id[evidence_id].get(
                    "note_sha256", default_note_sha(by_id[evidence_id]),
                ),
            }
            for evidence_id in evidence_ids
            if evidence_id in by_id
        }


class MemoryStorage:
    def __init__(self, reviews: dict[str, bytes]):
        self.reviews = reviews

    async def file_size(self, job_id: str, rel_path: str) -> int | None:
        value = self.reviews.get(job_id) if rel_path == "output/review.json" else None
        return len(value) if value is not None else None

    async def open_stream(
        self, job_id: str, rel_path: str, *, start: int = 0,
        length: int | None = None, chunk_size: int = 1,
    ):
        del chunk_size
        value = self.reviews.get(job_id) if rel_path == "output/review.json" else None
        if value is None:
            return None
        end = None if length is None else start + length

        async def chunks():
            yield value[start:end]

        return chunks()


def _install_fakes(monkeypatch, projections: dict[str, dict]):
    calls: list[str] = []

    async def resolve(_db, _storage, evidence_ids):
        return [projections[evidence_id] for evidence_id in evidence_ids]

    async def verify(review, *, job_id, pipeline, read_file):
        del pipeline, read_file
        calls.append(job_id)
        reliable = review.get("reliable") is True
        index = int(job_id.rsplit("-", 1)[-1]) if job_id.rsplit("-", 1)[-1].isdigit() else 1
        return {
            "review_reliable": reliable,
            "reliability_reasons": [] if reliable else ["stored_unreliable"],
            "review_input": {"sources": [{
                "label": "smart",
                "artifact": review.get("smart_artifact", f"output/{job_id}.md"),
                "sha256": review.get("smart_sha256", f"sha256:{index:064x}"),
            }]},
        }

    monkeypatch.setattr(concepts, "resolve_canonical_evidence_batch", resolve)
    monkeypatch.setattr(concepts, "verify_persisted_review", verify)
    return calls


def _projection(occurrence: dict, *, status: str = "valid", reason=None) -> dict:
    return {
        **occurrence,
        "status": status,
        "reason": reason,
        "note_type": "smart",
        "chunk_id": f"{occurrence['job_id']}:smart:0",
        "section": "S",
        "locator": {"kind": "text", "exact": "quote"},
        "link": {"href": "/safe"},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("locator_kind", ["media", "pdf", "image"])
async def test_non_text_locator_uses_only_bound_note_chunk_excerpt(
    monkeypatch, locator_kind: str,
):
    occurrence = _occurrence(1, "job-1", "source-1", "video")
    projection = _projection(occurrence)
    projection["locator"] = {"kind": locator_kind, "start_ms": 100}
    _install_fakes(monkeypatch, {occurrence["evidence_id"]: projection})

    result = await concepts.project_concept_attestation(
        FakeDatabase([occurrence], {"job-1"}),
        MemoryStorage({"job-1": b'{"reliable":true}'}),
        "ml",
        "RRF",
    )

    assert result["included"][0]["excerpt"] == "fact-1"
    assert result["included"][0]["locator"]["kind"] == locator_kind


@pytest.mark.asyncio
@pytest.mark.parametrize(("size", "expected"), [
    (0, "none"),
    (1, "supported"),
    (2, "corroborated"),
    (3, "strong"),
])
async def test_level_boundaries_require_independent_jobs_sources_and_types(
    monkeypatch, size: int, expected: str,
):
    content_types = [
        ("document", "article"),
        ("document", "research_paper"),
        ("video", None),
    ]
    occurrences = [
        _occurrence(
            index, f"job-{index}", f"source-{index}",
            content_types[index - 1][0], content_types[index - 1][1],
        )
        for index in range(1, size + 1)
    ]
    projections = {item["evidence_id"]: _projection(item) for item in occurrences}
    calls = _install_fakes(monkeypatch, projections)
    storage = MemoryStorage({
        item["job_id"]: b'{"reliable":true}' for item in occurrences
    })

    result = await concepts.project_concept_attestation(
        FakeDatabase(occurrences, {item["job_id"] for item in occurrences}),
        storage,
        "ml",
        "RRF",
    )

    assert result["level"] == expected
    assert result["evidence_count"] == size
    assert result["job_count"] == size
    assert result["source_fingerprint_count"] == size
    assert result["content_type_count"] == size
    assert sorted(calls) == sorted(item["job_id"] for item in occurrences)


@pytest.mark.asyncio
@pytest.mark.parametrize("same_dimension", ["job", "source", "content_type"])
async def test_one_repeated_independence_dimension_cannot_claim_corroboration(
    monkeypatch, same_dimension: str,
):
    values = [
        ["job-1", "job-2"],
        ["source-1", "source-2"],
        [("document", "article"), ("document", "research_paper")],
    ]
    if same_dimension == "job":
        values[0] = ["job-1", "job-1"]
    elif same_dimension == "source":
        values[1] = ["source-1", "source-1"]
    else:
        values[2] = [("document", "article"), ("document", "article")]
    occurrences = [
        _occurrence(
            index + 1, values[0][index], values[1][index],
            values[2][index][0], values[2][index][1],
        )
        for index in range(2)
    ]
    projections = {item["evidence_id"]: _projection(item) for item in occurrences}
    _install_fakes(monkeypatch, projections)

    result = await concepts.project_concept_attestation(
        FakeDatabase(occurrences, set(values[0])),
        MemoryStorage({job_id: b'{"reliable":true}' for job_id in values[0]}),
        "ml",
        "RRF",
    )

    assert result["level"] == "supported"


@pytest.mark.asyncio
@pytest.mark.parametrize(("case", "expected_reason"), [
    ("stale", "note_changed"),
    ("missing", "evidence_not_found"),
    ("deleted_job", "job_missing"),
    ("unreliable_review", "review_unreliable:stored_unreliable"),
    ("invalid_review", "review_invalid"),
])
async def test_invalid_and_unreliable_evidence_is_excluded_without_locator(
    monkeypatch, case: str, expected_reason: str,
):
    occurrence = _occurrence(1, "job-1", "source-1", "document", "article")
    projection = _projection(occurrence)
    jobs = {"job-1"}
    review = b'{"reliable":true}'
    if case in {"stale", "missing"}:
        projection["status"] = case
        projection["reason"] = expected_reason
    elif case == "deleted_job":
        jobs = set()
    elif case == "unreliable_review":
        review = b'{"reliable":false}'
    elif case == "invalid_review":
        review = b"{broken"
    _install_fakes(monkeypatch, {occurrence["evidence_id"]: projection})

    result = await concepts.project_concept_attestation(
        FakeDatabase([occurrence], jobs),
        MemoryStorage({"job-1": review}),
        "ml",
        "RRF",
    )

    assert result["level"] == "none"
    assert result["included"] == []
    assert result["excluded"] == [{
        "evidence_id": occurrence["evidence_id"],
        "job_id": "job-1",
        "content_type": "document",
        "document_kind": "article",
        "source_fingerprint": "source-1",
        "reason": expected_reason,
        "locator": None,
        "link": None,
    }]
    assert "excerpt" not in result["excluded"][0]


@pytest.mark.asyncio
async def test_projection_sort_fingerprint_and_review_cache_are_deterministic(monkeypatch):
    occurrences = [
        _occurrence(2, "job-1", "source-2", "document", "research_paper"),
        _occurrence(1, "job-1", "source-1", "document", "article"),
    ]
    projections = {item["evidence_id"]: _projection(item) for item in occurrences}
    calls = _install_fakes(monkeypatch, projections)

    result = await concepts.project_concept_attestation(
        FakeDatabase(occurrences, {"job-1"}),
        MemoryStorage({"job-1": b'{"reliable":true}'}),
        "ml",
        "RRF",
    )

    evidence_ids = sorted(item["evidence_id"] for item in occurrences)
    canonical = json.dumps(evidence_ids, separators=(",", ":"))
    assert [item["evidence_id"] for item in result["included"]] == evidence_ids
    assert result["source_set_fingerprint"] == hashlib.sha256(
        canonical.encode("utf-8")
    ).hexdigest()
    assert calls == ["job-1"]


@pytest.mark.asyncio
async def test_deleted_occurrences_leave_empty_attestation(monkeypatch):
    _install_fakes(monkeypatch, {})
    result = await concepts.project_concept_attestation(
        FakeDatabase([], set()), MemoryStorage({}), "ml", "RRF",
    )
    assert result["level"] == "none"
    assert result["included"] == result["excluded"] == []
    assert result["source_set_fingerprint"] == hashlib.sha256(b"[]").hexdigest()


@pytest.mark.asyncio
@pytest.mark.parametrize(("excerpt", "digest", "reason"), [
    (None, None, "evidence_excerpt_missing"),
    ("fact", "0" * 64, "evidence_excerpt_mismatch"),
    ("x" * (8 * 1024 + 1), None, "evidence_excerpt_too_large"),
])
async def test_unbound_or_oversized_excerpt_is_excluded(
    monkeypatch, excerpt, digest, reason,
):
    occurrence = _occurrence(1, "job-1", "source-1", "document", "article")
    occurrence["evidence_excerpt"] = excerpt
    occurrence["chunk_body_sha256"] = digest
    projection = _projection(occurrence)
    _install_fakes(monkeypatch, {occurrence["evidence_id"]: projection})
    result = await concepts.project_concept_attestation(
        FakeDatabase([occurrence], {"job-1"}),
        MemoryStorage({"job-1": b'{"reliable":true}'}),
        "ml", "RRF",
    )
    assert result["included"] == []
    assert result["excluded"][0]["reason"] == reason
    assert "excerpt" not in result["excluded"][0]


@pytest.mark.asyncio
@pytest.mark.parametrize(("review_change", "expected_reason"), [
    ({"smart_artifact": "output/old-smart.md"}, "review_note_mismatch"),
    ({"smart_sha256": "sha256:" + "f" * 64}, "review_note_mismatch"),
])
async def test_old_reliable_review_cannot_authorize_new_smart_evidence(
    monkeypatch, review_change: dict, expected_reason: str,
):
    occurrence = _occurrence(1, "job-1", "source-1", "document", "article")
    projection = _projection(occurrence)
    _install_fakes(monkeypatch, {occurrence["evidence_id"]: projection})
    review = json.dumps({"reliable": True, **review_change}).encode("utf-8")

    result = await concepts.project_concept_attestation(
        FakeDatabase([occurrence], {"job-1"}),
        MemoryStorage({"job-1": review}),
        "ml",
        "RRF",
    )

    assert result["included"] == []
    assert result["excluded"][0]["reason"] == expected_reason
    assert result["excluded"][0]["locator"] is None
