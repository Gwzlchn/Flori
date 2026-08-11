"""tests for scheduler._collect_glossary —— 评审产物 key_terms 采集为候选术语。

只喂 review["key_terms"](带候选定义),不读 missing_concepts。
用 storage / db stub 直接 await engine._collect_glossary(job_id),最小化依赖。"""

from __future__ import annotations

import asyncio
import json
import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from scheduler.effects import (
    ConceptSourceUnavailableError,
    _concept_projection_source_digest,
)
from scheduler.scheduler import Scheduler
from shared.concept_projection import CURRENT_CONCEPT_PROJECTOR_VERSION
from shared.db import (
    ConceptConflictError,
    Database,
    _EMPTY_CONCEPT_PROJECTION_DIGEST,
)
from shared.models import Job, JobStatus


class _StorageStub:
    """concepts.json 缺失时提供可重验的 Document review。"""

    def __init__(self, payload: dict):
        smart_rel = "output/versions/notes_smart_openai_m_20260101-000000.md"
        document_rel = "intermediate/document.json"
        quality_rel = "intermediate/quality.json"
        prompt_rel = "output/versions/review_input_openai_m_20260101-000000.md"
        smart = b"# smart\n"
        document = b'{"schema_version":1,"blocks":[]}'
        quality = b'{"schema_version":1,"status":"accepted"}'
        prompt = b"prompt\n# smart\n" + document + b"\n" + quality + b"\n"

        def record(rel, data, label=None):
            value = {
                "artifact": rel, "sha256": "sha256:" + hashlib.sha256(data).hexdigest(),
                "bytes": len(data), "chars": len(data.decode()), "truncated": False,
            }
            if label:
                value["label"] = label
            return value

        scores = [
            "completeness", "accuracy", "structure", "terminology",
            "formula_integrity", "visual_references", "traceability",
        ]
        review = {
            "schema_version": 2, "score_keys": scores,
            **{key: 5 for key in scores}, "overall": 5.0,
            "key_terms": payload.get("key_terms", []),
            "missing_concepts": payload.get("missing_concepts", []),
            "top3_improvements": ["a", "b", "c"], "issues": [],
            "review_reliable": True, "reliability_reasons": [],
            "review_input": {
                **record(prompt_rel, prompt), "sources": [
                    record(smart_rel, smart, "smart"),
                    record(document_rel, document, "document"),
                    record(quality_rel, quality, "quality"),
                ],
            },
            "completion": {
                "schema_version": 2, "status": "complete",
                "raw_finish_reason": "stop", "raw_error": False,
                "tier_used": "primary", "attempts": [{
                    "tier": "primary", "provider": "openai", "model": "m", "ok": True,
                }],
            },
            "parse": {"mode": "strict", "schema_valid": True, "errors": []},
            "citation_validation": {"status": "not_applicable", "checked": 0, "items": []},
            "review_coverage": {
                "note_chars": len(smart.decode()), "reviewed_chars": len(smart.decode()),
                "truncated": False,
            },
            "note_file": smart_rel, "provider": "openai", "model": "m",
            "generated_at": "2026/07/14 12:00:00",
        }
        self._data = json.dumps(review, ensure_ascii=False).encode("utf-8")
        self._files = {
            smart_rel: smart,
            document_rel: document,
            quality_rel: quality,
            prompt_rel: prompt,
        }

    async def read_file(self, job_id: str, rel: str) -> bytes | None:
        if rel == "output/concepts.json":
            return None
        if rel == "output/review.json":
            return self._data
        return self._files.get(rel)

    async def file_size(self, job_id: str, rel: str) -> int | None:
        data = await self.read_file(job_id, rel)
        return len(data) if data is not None else None

    async def open_stream(
        self, job_id: str, rel: str, *, start=0, length=None, chunk_size=1024 * 1024,
    ):
        data = await self.read_file(job_id, rel)
        if data is None:
            return None

        async def chunks():
            end = None if length is None else start + length
            for offset in range(start, len(data if end is None else data[:end]), chunk_size):
                yield data[offset:offset + chunk_size]

        return chunks()


class _ConceptsStorageStub:
    """Document 链:concepts.json 存在时优先采集自它。"""

    def __init__(self, payload: dict):
        self._data = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    async def read_file(self, job_id: str, rel: str) -> bytes | None:
        assert rel == "output/concepts.json"
        return self._data


class _DBStub:
    """记录 add_glossary_suggestion / add_glossary_relations 调用;get_job 返回固定
    domain/content_type;list_glossary 返回已采集的最小行(供 relations 段 resolve)。"""

    def __init__(
        self, domain: str = "ml", content_type: str = "document",
        pipeline: str | None = None, document_kind: str = "article",
    ):
        self._job = SimpleNamespace(
            domain=domain, content_type=content_type, pipeline=pipeline or content_type,
            document_kind=document_kind if content_type == "document" else "",
        )
        self.calls: list[dict] = []
        self.relations: list[dict] = []
        self.canonical_by_segment: dict[str, list[str]] = {}
        self.canonical_queries: list[dict] = []
        self.occurrence_replacements: list[dict] = []
        self.occurrence_projection_sources: dict[str, str] = {}
        self.occurrence_projection_empty: dict[str, bool] = {}
        self.occurrence_projection_versions: dict[str, int] = {}
        self.definition_states: dict[str, dict] = {}
        self.replay_states: dict[str, dict] = {}
        self.replay_failures: list[dict] = []

    def get_job(self, job_id: str):
        return self._job

    def add_glossary_suggestion(
        self, domain, term, job_id, content_type="", location=None, definition="", zh_name="",
        document_kind="",
    ):
        self.calls.append({
            "domain": domain, "term": term, "job_id": job_id,
            "content_type": content_type, "location": location,
            "definition": definition, "zh_name": zh_name,
            "document_kind": document_kind,
        })

    def list_glossary(self, domain=None, status=None, q=None):
        return [
            {
                "term": c["term"],
                "zh_name": c["zh_name"] or "",
                "aliases": [],
                **self.definition_states.get(c["term"], {}),
            }
            for c in self.calls
        ]

    def add_glossary_relations(self, domain, term, relations):
        self.relations.append({"domain": domain, "term": term, "relations": relations})
        return len(relations)

    def canonical_evidence_ids_for_source_segments(
        self, *, job_id, note_type, source_segment_ids,
    ):
        self.canonical_queries.append({
            "job_id": job_id,
            "note_type": note_type,
            "source_segment_ids": list(source_segment_ids),
        })
        return {
            segment_id: list(self.canonical_by_segment.get(segment_id, []))
            for segment_id in source_segment_ids
        }

    def replace_job_concept_occurrences(
        self, *, domain, job_id, mapping,
        projection_source_digest=None,
        expected_projection_source_digest=None,
        expected_projection_projector_version=None,
        projection_empty_reason="no_canonical_evidence",
    ):
        if projection_source_digest is not None:
            assert self.occurrence_projection_sources.get(job_id) \
                == expected_projection_source_digest
            current_version = (
                self.occurrence_projection_versions.get(job_id, 1)
                if job_id in self.occurrence_projection_sources else None
            )
            assert current_version == expected_projection_projector_version
            self.occurrence_projection_sources[job_id] = projection_source_digest
            self.occurrence_projection_empty[job_id] = not mapping
            self.occurrence_projection_versions[job_id] = (
                CURRENT_CONCEPT_PROJECTOR_VERSION
            )
            if mapping:
                self.replay_states.pop(job_id, None)
            else:
                self.replay_states[job_id] = {
                    "state": "verified_empty",
                    "reason": projection_empty_reason,
                    "source_digest": projection_source_digest,
                    "projector_version": CURRENT_CONCEPT_PROJECTOR_VERSION,
                }
        self.occurrence_replacements.append({
            "domain": domain,
            "job_id": job_id,
            "mapping": {term: list(ids) for term, ids in mapping.items()},
        })

    def get_concept_occurrence_projection_pair(self, job_id: str):
        from shared.db import _EMPTY_CONCEPT_PROJECTION_DIGEST

        src = self.occurrence_projection_sources.get(job_id)
        if src is None:
            return None
        empty = self.occurrence_projection_empty.get(job_id, True)
        return (
            src,
            _EMPTY_CONCEPT_PROJECTION_DIGEST if empty else "sha256:nonempty",
            self.occurrence_projection_versions.get(job_id, 1),
        )

    def get_concept_occurrence_replay_state(self, job_id: str):
        return self.replay_states.get(job_id)

    def get_concept_occurrence_projection_source(self, job_id: str):
        return self.occurrence_projection_sources.get(job_id)

    def record_concept_occurrence_replay_failure(
        self, job_id, *, reason, retry_base_seconds, retry_cap_seconds, now=None,
    ):
        previous = self.replay_states.get(job_id) or {}
        attempt = int(previous.get("attempt_count") or 0) + 1
        state = {
            "job_id": job_id, "state": "retry", "reason": reason,
            "attempt_count": attempt,
            "last_attempt_at": "stub-now", "next_retry_at": "stub-later",
        }
        self.replay_states[job_id] = state
        self.replay_failures.append({
            "job_id": job_id, "reason": reason,
            "base": retry_base_seconds, "cap": retry_cap_seconds,
        })
        return dict(state)

def _make_engine(storage, db):
    # _collect_glossary 仅用 self.storage / self.db;config 只需提供 jobs_dir。
    config = SimpleNamespace(jobs_dir=Path("/tmp/does-not-matter"))
    return Scheduler(redis=None, db=db, config=config, storage=storage)


def _set_projection_digest_at_current_version(db, job_id: str, digest: str) -> None:
    """模拟 current writer 发布，绕过专门识别旧 UPDATE writer 的兼容触发器。"""
    row = db._conn.execute(
        "SELECT source_digest, reconciled_at"
        " FROM concept_occurrence_projection WHERE job_id=?",
        (job_id,),
    ).fetchone()
    assert row is not None
    db._conn.execute(
        "DELETE FROM concept_occurrence_projection WHERE job_id=?",
        (job_id,),
    )
    db._conn.execute(
        """INSERT INTO concept_occurrence_projection
           (job_id, source_digest, projection_digest, reconciled_at,
            projector_version)
           VALUES (?, ?, ?, ?, ?)""",
        (
            job_id, row["source_digest"], digest, row["reconciled_at"],
            CURRENT_CONCEPT_PROJECTOR_VERSION,
        ),
    )


@pytest.mark.asyncio
async def test_collects_key_terms_with_definition():
    # key_terms=[{"term":"X","definition":"d"}] -> 对 X 采集,definition 传 "d"。
    review = {
        "key_terms": [{"term": "X", "definition": "d"}],
        "missing_concepts": ["Y"],
    }
    db = _DBStub(domain="ml", content_type="document", document_kind="article")
    engine = _make_engine(_ConceptsStorageStub(review), db)

    await engine._collect_glossary("j_g_001")

    terms = {c["term"]: c for c in db.calls}
    assert "X" in terms
    assert terms["X"]["definition"] == "d"
    assert terms["X"]["domain"] == "ml"
    assert terms["X"]["content_type"] == "document"
    assert terms["X"]["document_kind"] == "article"
    assert terms["X"]["job_id"] == "j_g_001"


@pytest.mark.asyncio
async def test_missing_concepts_not_fed():
    # missing_concepts 只留评审面板,不喂术语库:Y 不应被采集。
    review = {
        "key_terms": [{"term": "X", "definition": "d"}],
        "missing_concepts": ["Y"],
    }
    db = _DBStub()
    engine = _make_engine(_ConceptsStorageStub(review), db)

    await engine._collect_glossary("j_g_001")

    assert "Y" not in {c["term"] for c in db.calls}


@pytest.mark.asyncio
async def test_bare_string_key_terms_in_reliable_review_is_rejected():
    # review v2 要求 term/definition 对象;裸串不得冒充可靠结果。
    review = {"key_terms": ["裸词"]}
    db = _DBStub()
    engine = _make_engine(_StorageStub(review), db)

    await engine._collect_glossary("j_g_001")

    assert db.calls == []


@pytest.mark.asyncio
async def test_no_key_terms_collects_nothing():
    # 即便有 missing_concepts,没有 key_terms 也不采集任何术语。
    review = {"missing_concepts": ["Y", "Z"]}
    db = _DBStub()
    engine = _make_engine(_StorageStub(review), db)

    await engine._collect_glossary("j_g_001")

    assert db.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    {"key_terms": [{"term": "旧版", "definition": "x"}]},
    {"review_reliable": True, "key_terms": [{"term": "伪造旧版", "definition": "x"}]},
    {"schema_version": 1, "review_reliable": True,
     "key_terms": [{"term": "伪造 v1", "definition": "x"}]},
    {"schema_version": 2, "review_reliable": False,
     "key_terms": [{"term": "抢救结果", "definition": "x"}]},
])
async def test_legacy_or_unreliable_review_never_feeds_glossary(payload):
    class RawStorage(_StorageStub):
        def __init__(self, value):
            self._data = json.dumps(value, ensure_ascii=False).encode("utf-8")

        async def read_file(self, job_id: str, rel: str) -> bytes | None:
            if rel == "output/concepts.json":
                return None
            if rel == "output/review.json":
                return self._data
            return None

    db = _DBStub()
    await _make_engine(RawStorage(payload), db)._collect_glossary("j_bad")
    assert db.calls == []


@pytest.mark.asyncio
async def test_unknown_job_pipeline_never_feeds_glossary():
    storage = _StorageStub({"key_terms": [{"term": "X", "definition": "d"}]})
    db = _DBStub(pipeline="unknown-pipeline")

    await _make_engine(storage, db)._collect_glossary("j_unknown")

    assert db.calls == []


@pytest.mark.asyncio
async def test_related_edges_resolved_and_written():
    # related 两端经 resolve 归一后写边;目标未入库(幻觉词)不建边;自指跳过。
    concepts = {
        "key_terms": [
            {"term": "Transformer", "definition": "d1",
             "related": [{"term": "注意力机制", "rel": "part_of"},
                         {"term": "没入库的词", "rel": "related"},
                         {"term": "Transformer", "rel": "related"}]},
            {"term": "注意力机制", "definition": "d2"},
        ],
    }
    db = _DBStub(domain="dl", content_type="document", document_kind="article")
    engine = _make_engine(_ConceptsStorageStub(concepts), db)

    await engine._collect_glossary("j_r_001")

    assert len(db.relations) == 1
    r = db.relations[0]
    assert r["domain"] == "dl" and r["term"] == "Transformer"
    assert r["relations"] == [{"term": "注意力机制", "rel": "part_of"}]


@pytest.mark.asyncio
async def test_no_related_no_relations_call():
    concepts = {"key_terms": [{"term": "X", "definition": "d"}]}
    db = _DBStub()
    engine = _make_engine(_ConceptsStorageStub(concepts), db)
    await engine._collect_glossary("j_r_002")
    assert db.relations == []


@pytest.mark.asyncio
async def test_prefers_concepts_json_when_present():
    # Document 链:concepts.json 存在 → 采集源是它(不读 review)。
    # 的 read_file 断言只被以 concepts.json 调用,确保不回退 review。
    concepts = {"summary": "一句话", "key_terms": [{"term": "注意力机制", "definition": "权重分配"}]}
    db = _DBStub(domain="dl", content_type="document", document_kind="article")
    engine = _make_engine(_ConceptsStorageStub(concepts), db)

    await engine._collect_glossary("j_c_001")

    terms = {c["term"]: c for c in db.calls}
    assert "注意力机制" in terms
    assert terms["注意力机制"]["definition"] == "权重分配"
    assert terms["注意力机制"]["domain"] == "dl"


_SEGMENT_A = "seg_" + "a" * 64
_SEGMENT_B = "seg_" + "b" * 64
_SEGMENT_BLOCK = "blk_0e9bbed914442b095874"
_SEGMENT_HIERARCHICAL = "S1.P1"


@pytest.mark.asyncio
async def test_concept_source_segments_resolve_to_canonical_occurrences():
    concepts = {
        "evidence_note_type": "smart",
        "key_terms": [{
            "term": "Transformer",
            "definition": "d",
            "evidence_source_segment_ids": [_SEGMENT_A, _SEGMENT_B],
        }],
    }
    db = _DBStub(domain="dl")
    db.canonical_by_segment = {
        _SEGMENT_A: ["ev-a"],
        _SEGMENT_B: ["ev-b", "ev-a"],
    }
    engine = _make_engine(_ConceptsStorageStub(concepts), db)

    await engine._collect_glossary("j_evidence")

    assert db.canonical_queries == [{
        "job_id": "j_evidence",
        "note_type": "smart",
        "source_segment_ids": [_SEGMENT_A, _SEGMENT_B],
    }]
    assert db.occurrence_replacements[-1]["mapping"] == {
        "Transformer": ["ev-a", "ev-b"],
    }


@pytest.mark.asyncio
async def test_concept_source_segment_contract_accepts_producer_ids_and_rejects_junk():
    concepts = {
        "evidence_note_type": "smart",
        "key_terms": [{
            "term": "Transformer",
            "definition": "d",
            "evidence_source_segment_ids": [
                _SEGMENT_BLOCK,
                _SEGMENT_HIERARCHICAL,
                "_leading",
                "a" * 129,
                _SEGMENT_BLOCK,
            ],
        }],
    }
    db = _DBStub(domain="dl")
    db.canonical_by_segment = {
        _SEGMENT_BLOCK: ["ev-block"],
        _SEGMENT_HIERARCHICAL: ["ev-hierarchical"],
    }
    engine = _make_engine(_ConceptsStorageStub(concepts), db)

    await engine._collect_glossary("j-producer-ids")

    assert db.canonical_queries == [{
        "job_id": "j-producer-ids",
        "note_type": "smart",
        "source_segment_ids": [_SEGMENT_BLOCK, _SEGMENT_HIERARCHICAL],
    }]
    assert db.occurrence_replacements[-1]["mapping"] == {
        "Transformer": ["ev-block", "ev-hierarchical"],
    }


def test_concept_source_segment_mapping_rejects_over_budget_input():
    db = _DBStub(domain="dl")
    engine = _make_engine(_ConceptsStorageStub({"key_terms": []}), db)

    with pytest.raises(ValueError, match="source refs exceed binding budget"):
        engine._replace_concept_occurrences(
            "dl",
            "j-over-budget",
            [{
                "term": "Transformer",
                "evidence_source_segment_ids": [
                    f"blk_{index}" for index in range(501)
                ],
            }],
            "smart",
        )

    assert db.canonical_queries == []


@pytest.mark.asyncio
async def test_repeated_completion_is_idempotent_and_removed_term_is_omitted():
    storage = _ConceptsStorageStub({
        "evidence_note_type": "original",
        "key_terms": [
            {"term": "A", "evidence_source_segment_ids": [_SEGMENT_A]},
            {"term": "B", "evidence_source_segment_ids": [_SEGMENT_B]},
        ],
    })
    db = _DBStub()
    db.canonical_by_segment = {_SEGMENT_A: ["ev-a"], _SEGMENT_B: ["ev-b"]}
    engine = _make_engine(storage, db)

    await engine._collect_glossary("j_replay")
    await engine._collect_glossary("j_replay")
    assert db.occurrence_replacements[-2] == db.occurrence_replacements[-1]
    assert db.occurrence_replacements[-1]["mapping"] == {"A": ["ev-a"], "B": ["ev-b"]}

    storage._data = json.dumps({
        "evidence_note_type": "original",
        "key_terms": [{"term": "A", "evidence_source_segment_ids": [_SEGMENT_A]}],
    }).encode()
    await engine._collect_glossary("j_replay")

    assert db.occurrence_replacements[-1]["mapping"] == {"A": ["ev-a"]}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", ["malformed", "non_object", "unreliable"],
)
async def test_untrustworthy_replay_clears_previous_job_occurrences(failure):
    class MutableStorage:
        def __init__(self):
            self.concepts = json.dumps({
                "evidence_note_type": "original",
                "key_terms": [{
                    "term": "A",
                    "evidence_source_segment_ids": [_SEGMENT_A],
                }],
            }).encode()
            self.review = None

        async def read_file(self, job_id, rel):
            if rel == "output/concepts.json":
                return self.concepts
            if rel == "output/review.json":
                return self.review
            return None

        async def file_size(self, job_id, rel):
            data = await self.read_file(job_id, rel)
            return len(data) if data is not None else None

        async def open_stream(
            self, job_id, rel, *, start=0, length=None, chunk_size=1024 * 1024,
        ):
            data = await self.read_file(job_id, rel)
            if data is None:
                return None

            async def chunks():
                end = len(data) if length is None else min(len(data), start + length)
                for offset in range(start, end, chunk_size):
                    yield data[offset:min(end, offset + chunk_size)]

            return chunks()

    storage = MutableStorage()
    db = _DBStub()
    db.canonical_by_segment = {_SEGMENT_A: ["ev-a"]}
    engine = _make_engine(storage, db)
    await engine._collect_glossary("j_replay")
    assert db.occurrence_replacements[-1]["mapping"] == {"A": ["ev-a"]}

    if failure == "malformed":
        storage.concepts = b"{broken"
    elif failure == "non_object":
        storage.concepts = b"[]"
    else:
        storage.concepts = None
        storage.review = b'{"schema_version":2,"review_reliable":false}'
    await engine._collect_glossary("j_replay")

    assert db.occurrence_replacements[-1]["mapping"] == {}


@pytest.mark.asyncio
async def test_unreadable_source_keeps_occurrences_and_fails_completion_gate():
    # 真源整体读不到属环境性失败:不清旧投影、不落空 marker,抛错让完成门重试。
    class VanishingStorage:
        def __init__(self):
            self.concepts = json.dumps({
                "evidence_note_type": "original",
                "key_terms": [{
                    "term": "A",
                    "evidence_source_segment_ids": [_SEGMENT_A],
                }],
            }).encode()

        async def read_file(self, job_id, rel):
            if rel == "output/concepts.json":
                return self.concepts
            return None

        async def file_size(self, job_id, rel):
            data = await self.read_file(job_id, rel)
            return len(data) if data is not None else None

        async def open_stream(self, job_id, rel, **kwargs):
            return None

    storage = VanishingStorage()
    db = _DBStub()
    db.canonical_by_segment = {_SEGMENT_A: ["ev-a"]}
    engine = _make_engine(storage, db)
    await engine._collect_glossary("j_replay")
    marker = db.occurrence_projection_sources["j_replay"]

    storage.concepts = None
    with pytest.raises(ConceptSourceUnavailableError):
        await engine._collect_glossary("j_replay")

    assert db.occurrence_replacements[-1]["mapping"] == {"A": ["ev-a"]}
    assert db.occurrence_projection_sources["j_replay"] == marker


@pytest.mark.asyncio
async def test_missing_artifacts_use_real_database_keyword_reconcile(tmp_path):
    class EmptyStorage:
        async def read_file(self, job_id, rel):
            return None

        async def file_size(self, job_id, rel):
            return None

        async def open_stream(self, job_id, rel, **kwargs):
            return None

    db = Database(tmp_path / "scheduler-glossary.db")
    db.init_schema()
    try:
        db.create_job(Job(
            id="job-missing", content_type="document", pipeline="document",
            document_kind="article",
        ))
        with pytest.raises(ConceptSourceUnavailableError):
            await _make_engine(EmptyStorage(), db)._collect_glossary("job-missing")
        assert db.list_concept_occurrences(
            "general", "unused", include_invalid=True,
        ) == []
        assert db.get_concept_occurrence_projection_source("job-missing") is None
    finally:
        db.close()


@pytest.mark.asyncio
async def test_occurrence_projection_replay_has_no_glossary_or_ai_side_effects():
    concepts = {
        "evidence_note_type": "original",
        "key_terms": [{
            "term": "RRF",
            "definition": "rank fusion",
            "evidence_source_segment_ids": [_SEGMENT_A],
        }],
    }
    db = _DBStub()
    db.calls.append({
        "term": "RRF", "zh_name": "", "domain": "ml", "job_id": "seed",
        "content_type": "document", "location": None, "definition": "",
        "document_kind": "article",
    })
    db.canonical_by_segment = {_SEGMENT_A: ["ev-rrf"]}
    engine = _make_engine(_ConceptsStorageStub(concepts), db)
    before = list(db.calls)

    first = await engine.reconcile_concept_occurrences_only("j_restore")
    second = await engine.reconcile_concept_occurrences_only("j_restore")

    assert first == 1
    assert second == 0
    assert db.calls == before
    assert db.relations == []
    assert db.occurrence_replacements[-1:] == [
        {"domain": "ml", "job_id": "j_restore", "mapping": {"RRF": ["ev-rrf"]}},
    ]
    assert db.occurrence_projection_sources["j_restore"].startswith("sha256:")


def test_occurrence_projection_ledger_preserves_retry_after_fts_success(tmp_path):
    db = Database(tmp_path / "occurrence-ledger.db")
    db.init_schema()
    try:
        db.create_job(Job(
            id="job-retry-occurrence",
            content_type="document",
            pipeline="document",
            document_kind="article",
            status=JobStatus.DONE,
        ))
        db.index_job_notes(
            "job-retry-occurrence", "original", "title", "body",
            content_type="document", domain="general",
        )
        assert [
            job.id for job in db.list_unreconciled_concept_occurrence_jobs()
        ] == ["job-retry-occurrence"]

        # occurrence 处理失败时不会调用 marker,下一轮仍能拾取同一个 Job。
        assert [
            job.id for job in db.list_unreconciled_concept_occurrence_jobs()
        ] == ["job-retry-occurrence"]
        db.replace_job_concept_occurrences(
            domain="general",
            job_id="job-retry-occurrence",
            mapping={},
            projection_source_digest="sha256:" + "1" * 64,
            expected_projection_source_digest=None,
            projection_empty_reason="truly_empty",
        )
        # 空投影发布同事务落 verified_empty 判定:离开候选池,不再周期重读。
        state = db.get_concept_occurrence_replay_state("job-retry-occurrence")
        assert state["state"] == "verified_empty"
        assert state["reason"] == "truly_empty"
        assert state["source_digest"] == "sha256:" + "1" * 64
        assert db.list_unreconciled_concept_occurrence_jobs() == []

        # 生产事故形态(pre-v10 固化行/崩溃窗口):空 marker 没有判定行。
        # 必须回到候选池,由 reconcile 读源收敛,而不是永久离池。
        db._conn.execute(
            "DELETE FROM concept_occurrence_replay_state WHERE job_id=?",
            ("job-retry-occurrence",),
        )
        db._conn.commit()
        assert [
            job.id for job in db.list_unreconciled_concept_occurrence_jobs()
        ] == ["job-retry-occurrence"]

        # 非空投影才彻底不需要判定行;模拟 current writer 成功发布后的行。
        _set_projection_digest_at_current_version(
            db, "job-retry-occurrence", "sha256:" + "2" * 64,
        )
        db._conn.commit()
        assert db.list_unreconciled_concept_occurrence_jobs() == []

        # FTS/canonical evidence 新版本与旧 marker/判定不能同时可见。模拟索引提交后、
        # glossary 对账前崩溃,周期查询必须重新认领这个 Job。
        db.index_job_notes(
            "job-retry-occurrence", "original", "title-v2", "body-v2",
            content_type="document", domain="general",
        )
        assert db.get_concept_occurrence_projection_source(
            "job-retry-occurrence",
        ) is None
        assert db.get_concept_occurrence_replay_state(
            "job-retry-occurrence",
        ) is None
        assert [
            job.id for job in db.list_unreconciled_concept_occurrence_jobs()
        ] == ["job-retry-occurrence"]
    finally:
        db.close()


def test_occurrence_projection_source_publish_is_compare_and_swap(tmp_path):
    db = Database(tmp_path / "occurrence-cas.db")
    db.init_schema()
    source_a = "sha256:" + "a" * 64
    source_b = "sha256:" + "b" * 64
    try:
        db.create_job(Job(
            id="job-occurrence-cas",
            content_type="document",
            pipeline="document",
            document_kind="article",
            status=JobStatus.DONE,
        ))
        db.replace_job_concept_occurrences(
            domain="general",
            job_id="job-occurrence-cas",
            mapping={},
            projection_source_digest=source_a,
            expected_projection_source_digest=None,
        )
        assert db.get_concept_occurrence_projection_source(
            "job-occurrence-cas",
        ) == source_a

        with pytest.raises(ConceptConflictError, match="source or version changed"):
            db.replace_job_concept_occurrences(
                domain="general",
                job_id="job-occurrence-cas",
                mapping={},
                projection_source_digest=source_b,
                expected_projection_source_digest=None,
            )
        assert db.get_concept_occurrence_projection_source(
            "job-occurrence-cas",
        ) == source_a

        db.replace_job_concept_occurrences(
            domain="general",
            job_id="job-occurrence-cas",
            mapping={},
            projection_source_digest=source_b,
            expected_projection_source_digest=source_a,
            expected_projection_projector_version=(
                CURRENT_CONCEPT_PROJECTOR_VERSION
            ),
        )
        assert db.get_concept_occurrence_projection_source(
            "job-occurrence-cas",
        ) == source_b
    finally:
        db.close()


def test_empty_projection_digest_constant_matches_published_rows(tmp_path):
    # 锚定常量与真实发布行一致,防止序列化漂移;末段哈希即生产库空投影行的指纹。
    assert _EMPTY_CONCEPT_PROJECTION_DIGEST == (
        "sha256:4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"
    )
    db = Database(tmp_path / "occurrence-empty-digest.db")
    db.init_schema()
    try:
        db.create_job(Job(
            id="job-empty-digest",
            content_type="document",
            pipeline="document",
            document_kind="article",
            status=JobStatus.DONE,
        ))
        db.replace_job_concept_occurrences(
            domain="general",
            job_id="job-empty-digest",
            mapping={},
            projection_source_digest="sha256:" + "a" * 64,
            expected_projection_source_digest=None,
        )
        row = db._conn.execute(
            "SELECT projection_digest FROM concept_occurrence_projection WHERE job_id=?",
            ("job-empty-digest",),
        ).fetchone()
        assert row["projection_digest"] == _EMPTY_CONCEPT_PROJECTION_DIGEST
    finally:
        db.close()


class _AllMissingStorage:
    async def read_file(self, job_id, rel):
        return None

    async def file_size(self, job_id, rel):
        return None

    async def open_stream(self, job_id, rel, **kwargs):
        return None


@pytest.mark.asyncio
async def test_replay_defers_without_marker_when_source_unreadable():
    # 环境性读不到真源:不落 durable marker,改记持久退避账本(source_missing),
    # 重试资格保留但不再每拍占用候选窗口。
    db = _DBStub()
    engine = _make_engine(_AllMissingStorage(), db)

    assert await engine.reconcile_concept_occurrences_only("j_missing") == 0

    assert db.occurrence_replacements == []
    assert db.occurrence_projection_sources == {}
    assert db.replay_failures == [{
        "job_id": "j_missing", "reason": "source_missing",
        "base": 300, "cap": 86400,
    }]
    assert db.replay_states["j_missing"]["state"] == "retry"


class _FlippableConceptsStorage:
    def __init__(self, payload: dict | None):
        self.payload = payload

    async def read_file(self, job_id, rel):
        if rel == "output/concepts.json" and self.payload is not None:
            return json.dumps(self.payload, ensure_ascii=False).encode("utf-8")
        return None

    async def file_size(self, job_id, rel):
        data = await self.read_file(job_id, rel)
        return len(data) if data is not None else None

    async def open_stream(self, job_id, rel, **kwargs):
        return None


@pytest.mark.asyncio
async def test_stale_empty_marker_replays_to_full_projection_roundtrip():
    # 生产形态:老代码把读不到真源固化成空投影 marker。真源恢复可读后,
    # 重放必须发现 digest 不符,把空投影修回非空,并在之后保持幂等。
    stale_digest = "sha256:" + hashlib.sha256(b"source_missing").hexdigest()
    db = _DBStub(domain="ml")
    db.canonical_by_segment = {_SEGMENT_A: ["ev-a"]}
    db.calls.append({
        "term": "Alpha", "zh_name": "", "domain": "ml", "job_id": "seed",
        "content_type": "document", "location": None, "definition": "",
        "document_kind": "article",
    })
    db.occurrence_projection_sources["j_fixated"] = stale_digest
    db.occurrence_replacements.append(
        {"domain": "ml", "job_id": "j_fixated", "mapping": {}},
    )
    storage = _FlippableConceptsStorage({
        "evidence_note_type": "original",
        "key_terms": [{
            "term": "Alpha",
            "evidence_source_segment_ids": [_SEGMENT_A],
        }],
    })
    engine = _make_engine(storage, db)

    first = await engine.reconcile_concept_occurrences_only("j_fixated")
    second = await engine.reconcile_concept_occurrences_only("j_fixated")

    assert first == 1
    assert second == 0
    assert db.occurrence_replacements[-1] == {
        "domain": "ml", "job_id": "j_fixated", "mapping": {"Alpha": ["ev-a"]},
    }
    assert len(db.occurrence_replacements) == 2
    marker = db.occurrence_projection_sources["j_fixated"]
    assert marker != stale_digest
    assert marker.startswith("sha256:")


@pytest.mark.asyncio
async def test_sentinel_marker_with_missing_source_defers_then_replays():
    # 旧 marker 存的是 source_missing 哨兵摘要且真源仍读不到:不得短路进确认缓存,
    # 同进程内真源一就绪就要立即重放,不等调度器重启。
    stale_digest = "sha256:" + hashlib.sha256(b"source_missing").hexdigest()
    db = _DBStub(domain="ml")
    db.canonical_by_segment = {_SEGMENT_A: ["ev-a"]}
    db.calls.append({
        "term": "Alpha", "zh_name": "", "domain": "ml", "job_id": "seed",
        "content_type": "document", "location": None, "definition": "",
        "document_kind": "article",
    })
    db.occurrence_projection_sources["j_sentinel"] = stale_digest
    storage = _FlippableConceptsStorage(None)
    engine = _make_engine(storage, db)

    assert await engine.reconcile_concept_occurrences_only("j_sentinel") == 0
    assert db.occurrence_projection_sources["j_sentinel"] == stale_digest
    assert db.occurrence_replacements == []

    storage.payload = {
        "evidence_note_type": "original",
        "key_terms": [{
            "term": "Alpha",
            "evidence_source_segment_ids": [_SEGMENT_A],
        }],
    }
    assert await engine.reconcile_concept_occurrences_only("j_sentinel") == 1
    assert db.occurrence_replacements[-1]["mapping"] == {"Alpha": ["ev-a"]}
    assert db.occurrence_projection_sources["j_sentinel"] != stale_digest


@pytest.mark.asyncio
async def test_truly_empty_source_publishes_marker_idempotently():
    # 真源可读且确实没有概念:空投影是正确结果,落 digest 绑定的 marker 并保持幂等。
    db = _DBStub()
    storage = _FlippableConceptsStorage({"key_terms": []})
    engine = _make_engine(storage, db)

    first = await engine.reconcile_concept_occurrences_only("j_empty")
    marker = db.occurrence_projection_sources["j_empty"]
    second = await engine.reconcile_concept_occurrences_only("j_empty")

    assert first == 0 and second == 0
    assert db.occurrence_replacements == [
        {"domain": "ml", "job_id": "j_empty", "mapping": {}},
    ]
    assert marker.startswith("sha256:")
    assert db.occurrence_projection_sources["j_empty"] == marker


@pytest.mark.asyncio
async def test_review_fallback_and_rejected_term_never_fabricate_occurrences():
    review_db = _DBStub()
    await _make_engine(
        _StorageStub({"key_terms": [{"term": "ReviewOnly", "definition": "d"}]}),
        review_db,
    )._collect_glossary("j_review")
    assert review_db.canonical_queries == []
    assert review_db.occurrence_replacements[-1]["mapping"] == {}

    class RejectedDB(_DBStub):
        def add_glossary_suggestion(self, *args, **kwargs):
            return None

        def list_glossary(self, domain=None, status=None, q=None):
            return []

    rejected_db = RejectedDB()
    rejected_db.canonical_by_segment = {_SEGMENT_A: ["ev-rejected"]}
    concepts = {
        "evidence_note_type": "original",
        "key_terms": [{
            "term": "Rejected",
            "evidence_source_segment_ids": [_SEGMENT_A],
        }],
    }
    await _make_engine(
        _ConceptsStorageStub(concepts), rejected_db,
    )._collect_glossary("j_rejected")
    assert rejected_db.occurrence_replacements[-1]["mapping"] == {}


def _auto_synthesis_engine(*, locked: bool = False):
    concepts = {
        "evidence_note_type": "original",
        "key_terms": [{
            "term": "AutoTerm",
            "evidence_source_segment_ids": [_SEGMENT_A],
        }],
    }
    db = _DBStub(domain="dl")
    db.canonical_by_segment = {_SEGMENT_A: ["ev-auto"]}
    db.definition_states["AutoTerm"] = {
        "current_definition_version_id": "cdv-current",
        "lock_revision": 4,
        "definition_locked": locked,
    }
    return _make_engine(_ConceptsStorageStub(concepts), db), db


@pytest.mark.asyncio
async def test_occurrence_reconcile_coalesces_one_latest_resynthesis(monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def fake_resynthesize(*args, **kwargs):
        calls.append((args, kwargs))
        started.set()
        await release.wait()
        return {"created": True, "reason": None}

    monkeypatch.setattr(
        "api.services.concepts.maybe_resynthesize_concept",
        fake_resynthesize,
    )
    engine, _ = _auto_synthesis_engine()

    await engine._collect_glossary("j_auto")
    await started.wait()
    await engine._collect_glossary("j_auto")
    await engine._collect_glossary("j_auto")
    await asyncio.sleep(0)

    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[3:5] == ("dl", "AutoTerm")
    assert kwargs == {
        "expected_current_version_id": "cdv-current",
        "expected_lock_revision": 4,
        "actor": "scheduler:auto",
        "strategy": "automatic_resynthesis",
    }
    release.set()
    await asyncio.gather(*list(engine._concept_synthesis_tasks.values()))
    await asyncio.sleep(0)
    if engine._concept_synthesis_tasks:
        await asyncio.gather(*list(engine._concept_synthesis_tasks.values()))
    assert len(calls) == 2
    assert calls[1][0][3:5] == ("dl", "AutoTerm")
    assert calls[1][1] == calls[0][1]
    assert engine._concept_synthesis_tasks == {}
    assert engine._concept_synthesis_pending == {}


@pytest.mark.asyncio
async def test_locked_or_unmapped_concept_never_schedules_resynthesis(monkeypatch):
    calls = []

    async def fake_resynthesize(*args, **kwargs):
        calls.append((args, kwargs))
        return {"created": False, "reason": "unexpected"}

    monkeypatch.setattr(
        "api.services.concepts.maybe_resynthesize_concept",
        fake_resynthesize,
    )
    locked_engine, _ = _auto_synthesis_engine(locked=True)
    await locked_engine._collect_glossary("j_locked")

    unmapped_db = _DBStub(domain="dl")
    unmapped_engine = _make_engine(
        _ConceptsStorageStub({
            "evidence_note_type": "original",
            "key_terms": [{"term": "NoEvidence"}],
        }),
        unmapped_db,
    )
    await unmapped_engine._collect_glossary("j_unmapped")
    await asyncio.sleep(0)

    assert calls == []
    assert locked_engine._concept_synthesis_tasks == {}
    assert unmapped_engine._concept_synthesis_tasks == {}


@pytest.mark.asyncio
async def test_resynthesis_failure_is_best_effort_and_shutdown_cancels(monkeypatch):
    attempts = 0

    async def fail_resynthesize(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(
        "api.services.concepts.maybe_resynthesize_concept",
        fail_resynthesize,
    )
    engine, _ = _auto_synthesis_engine()
    await engine._collect_glossary("j_failure")
    for _ in range(20):
        if not engine._concept_synthesis_tasks:
            break
        await asyncio.sleep(0)
    assert attempts == 1
    assert engine._concept_synthesis_tasks == {}

    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def wait_resynthesize(*args, **kwargs):
        started.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    monkeypatch.setattr(
        "api.services.concepts.maybe_resynthesize_concept",
        wait_resynthesize,
    )
    shutdown_engine, _ = _auto_synthesis_engine()
    await shutdown_engine._collect_glossary("j_shutdown")
    await started.wait()
    await shutdown_engine.shutdown()

    assert cancelled.is_set()
    assert all(task.done() for task in shutdown_engine._concept_synthesis_tasks.values())


class _PerJobConceptsStorage:
    """按 job 提供 concepts.json;None=两路缺失,unreachable 集合=读抛异常。"""

    def __init__(self, payloads: dict):
        self.payloads = dict(payloads)
        self.reads: dict[str, int] = {}
        self.unreachable: set[str] = set()

    async def read_file(self, job_id, rel):
        if rel != "output/concepts.json":
            return None
        self.reads[job_id] = self.reads.get(job_id, 0) + 1
        if job_id in self.unreachable:
            raise OSError("storage unavailable")
        payload = self.payloads.get(job_id)
        if payload is None:
            return None
        if isinstance(payload, (bytes, bytearray)):
            return bytes(payload)
        return json.dumps(payload, ensure_ascii=False).encode("utf-8")

    async def file_size(self, job_id, rel):
        return None

    async def open_stream(self, job_id, rel, **kwargs):
        return None


def _seed_indexed_job(db, job_id: str, created_at: datetime) -> None:
    db.create_job(Job(
        id=job_id, content_type="document", pipeline="document",
        document_kind="article", status=JobStatus.DONE, created_at=created_at,
    ))
    db.index_job_notes(
        job_id, "original", job_id, "body",
        content_type="document", domain="general",
    )


async def _drain_once(engine, db, now: str | None = None) -> list:
    batch = db.list_unreconciled_concept_occurrence_jobs(now=now)
    for job in batch:
        await engine.reconcile_concept_occurrences_only(job.id)
    return batch


_BASE_CREATED = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_hundred_true_empty_rows_do_not_starve_fixable_tail(tmp_path):
    # 100 个真实空集占满第一窗;它们判定后离池,第二拍必须轮到第 101 个。
    db = Database(tmp_path / "starve-empty.db")
    try:
        db.init_schema()
        ids = [f"job-{i:03d}" for i in range(101)]
        for i, job_id in enumerate(ids):
            _seed_indexed_job(db, job_id, _BASE_CREATED + timedelta(minutes=i))
        storage = _PerJobConceptsStorage({
            job_id: {"key_terms": []} for job_id in ids
        })
        engine = _make_engine(storage, db)

        first_batch = await _drain_once(engine, db)
        assert [job.id for job in first_batch] == ids[:100]
        assert db.get_concept_occurrence_projection_source(ids[100]) is None

        second_batch = await _drain_once(engine, db)
        assert [job.id for job in second_batch] == [ids[100]]
        assert db.get_concept_occurrence_projection_source(ids[100]) is not None
        tail_state = db.get_concept_occurrence_replay_state(ids[100])
        assert tail_state["state"] == "verified_empty"
        assert tail_state["reason"] == "truly_empty"
        assert db.list_unreconciled_concept_occurrence_jobs() == []
    finally:
        db.close()


@pytest.mark.asyncio
async def test_hundred_source_missing_rows_do_not_starve_fixable_tail(tmp_path):
    # 100 个真源缺失进退避轨让出窗口;第 101 个第二拍就被处理。
    # 缺失者保留有界重试:到期重新入选,再失败退避继续后移。
    db = Database(tmp_path / "starve-missing.db")
    try:
        db.init_schema()
        ids = [f"job-{i:03d}" for i in range(101)]
        for i, job_id in enumerate(ids):
            _seed_indexed_job(db, job_id, _BASE_CREATED + timedelta(minutes=i))
        storage = _PerJobConceptsStorage({ids[100]: {"key_terms": []}})
        engine = _make_engine(storage, db)

        first_batch = await _drain_once(engine, db)
        assert [job.id for job in first_batch] == ids[:100]
        sample = db.get_concept_occurrence_replay_state(ids[0])
        assert sample["state"] == "retry"
        assert sample["reason"] == "source_missing"
        assert sample["attempt_count"] == 1

        second_batch = await _drain_once(engine, db)
        assert [job.id for job in second_batch] == [ids[100]]
        assert db.get_concept_occurrence_projection_source(ids[100]) is not None
        assert db.list_unreconciled_concept_occurrence_jobs() == []

        # 未到期不入选;到期后重新入选,失败一次退避继续后移。
        due_at = (
            datetime.now(timezone.utc) + timedelta(hours=2)
        ).isoformat()
        due_batch = db.list_unreconciled_concept_occurrence_jobs(now=due_at)
        assert [job.id for job in due_batch] == ids[:100]
        await engine.reconcile_concept_occurrences_only(ids[0])
        retried = db.get_concept_occurrence_replay_state(ids[0])
        assert retried["attempt_count"] == 2
        assert retried["next_retry_at"] > sample["next_retry_at"]
    finally:
        db.close()


@pytest.mark.asyncio
async def test_verified_empty_not_reread_and_survives_restart(tmp_path):
    # 判定持久:不再每拍重读真源;换新 engine(调度器重启)也不回炉。
    db = Database(tmp_path / "verified-durable.db")
    try:
        db.init_schema()
        _seed_indexed_job(db, "job-empty", _BASE_CREATED)
        storage = _PerJobConceptsStorage({"job-empty": {"key_terms": []}})
        engine = _make_engine(storage, db)

        assert [job.id for job in await _drain_once(engine, db)] == ["job-empty"]
        assert storage.reads["job-empty"] == 1

        for _ in range(5):
            assert await _drain_once(engine, db) == []
        assert storage.reads["job-empty"] == 1

        restarted = _make_engine(storage, db)
        assert await _drain_once(restarted, db) == []
        far_future = (
            datetime.now(timezone.utc) + timedelta(days=30)
        ).isoformat()
        assert db.list_unreconciled_concept_occurrence_jobs(now=far_future) == []
        assert storage.reads["job-empty"] == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_transient_storage_failure_recovers_and_converges(tmp_path):
    db = Database(tmp_path / "transient-storage.db")
    try:
        db.init_schema()
        _seed_indexed_job(db, "job-flaky", _BASE_CREATED)
        storage = _PerJobConceptsStorage({"job-flaky": {"key_terms": []}})
        storage.unreachable.add("job-flaky")
        engine = _make_engine(storage, db)

        assert [job.id for job in await _drain_once(engine, db)] == ["job-flaky"]
        state = db.get_concept_occurrence_replay_state("job-flaky")
        assert state["state"] == "retry"
        assert state["reason"] == "storage_unreachable"
        assert db.get_concept_occurrence_projection_source("job-flaky") is None
        assert db.list_unreconciled_concept_occurrence_jobs() == []

        storage.unreachable.clear()
        due_at = (
            datetime.now(timezone.utc) + timedelta(hours=1)
        ).isoformat()
        assert [
            job.id for job in await _drain_once(engine, db, now=due_at)
        ] == ["job-flaky"]
        assert db.get_concept_occurrence_projection_source("job-flaky") is not None
        assert db.get_concept_occurrence_replay_state(
            "job-flaky",
        )["state"] == "verified_empty"
        assert db.list_unreconciled_concept_occurrence_jobs() == []
    finally:
        db.close()


@pytest.mark.asyncio
async def test_deterministic_invalid_content_settles_without_rereads(tmp_path):
    # JSON 坏字节是确定性拒绝:一次读源落判定,之后不再入选、不再重读、不再刷日志。
    db = Database(tmp_path / "invalid-settles.db")
    try:
        db.init_schema()
        _seed_indexed_job(db, "job-broken", _BASE_CREATED)
        storage = _PerJobConceptsStorage({"job-broken": b"{broken"})
        engine = _make_engine(storage, db)

        assert [job.id for job in await _drain_once(engine, db)] == ["job-broken"]
        state = db.get_concept_occurrence_replay_state("job-broken")
        assert state["state"] == "verified_empty"
        assert state["reason"] == "source_invalid"
        assert storage.reads["job-broken"] == 1

        for _ in range(5):
            assert await _drain_once(engine, db) == []
        assert storage.reads["job-broken"] == 1
    finally:
        db.close()


def test_replay_failure_ledger_guards_and_bounded_backoff(tmp_path):
    db = Database(tmp_path / "replay-ledger.db")
    try:
        db.init_schema()
        _seed_indexed_job(db, "job-ledger", _BASE_CREATED)
        digest = "sha256:" + "a" * 64
        db.replace_job_concept_occurrences(
            domain="general", job_id="job-ledger", mapping={},
            projection_source_digest=digest,
            expected_projection_source_digest=None,
            projection_empty_reason="truly_empty",
        )
        # 判定仍绑定当前 marker:失败记录是 CAS 败者,不得覆盖判定。
        assert db.record_concept_occurrence_replay_failure(
            "job-ledger", reason="replay_error",
            retry_base_seconds=60, retry_cap_seconds=3600,
        ) is None
        assert db.get_concept_occurrence_replay_state(
            "job-ledger",
        )["state"] == "verified_empty"

        # 非空投影已离池:同样不写退避账本。
        _set_projection_digest_at_current_version(
            db, "job-ledger", "sha256:" + "2" * 64,
        )
        db._conn.commit()
        assert db.record_concept_occurrence_replay_failure(
            "job-ledger", reason="replay_error",
            retry_base_seconds=60, retry_cap_seconds=3600,
        ) is None

        # 回到可修复空投影形态:退避指数增长且被 cap 封顶,attempt 单调。
        db._conn.execute(
            """UPDATE concept_occurrence_projection
               SET projection_digest=? WHERE job_id=?""",
            (_EMPTY_CONCEPT_PROJECTION_DIGEST, "job-ledger"),
        )
        db._conn.execute(
            "DELETE FROM concept_occurrence_replay_state WHERE job_id=?",
            ("job-ledger",),
        )
        db._conn.commit()
        fixed_now = "2026-01-01T00:00:00+00:00"
        base_dt = datetime(2026, 1, 1, tzinfo=timezone.utc)
        expected_delays = [300, 600, 1200, 2400, 4800, 9600, 19200, 38400,
                           76800, 86400, 86400, 86400]
        for attempt, delay in enumerate(expected_delays, start=1):
            state = db.record_concept_occurrence_replay_failure(
                "job-ledger", reason="source_missing",
                retry_base_seconds=300, retry_cap_seconds=86400, now=fixed_now,
            )
            assert state["attempt_count"] == attempt
            assert state["next_retry_at"] == (
                base_dt + timedelta(seconds=delay)
            ).isoformat()
        persisted = db.get_concept_occurrence_replay_state("job-ledger")
        assert persisted["state"] == "retry"
        assert persisted["reason"] == "source_missing"
        assert persisted["attempt_count"] == len(expected_delays)
    finally:
        db.close()


@pytest.mark.asyncio
async def test_concurrent_cas_conflict_does_not_clobber_verdict(tmp_path):
    # 重放读源期间另一执行者先发布判定:本次 CAS 必败,失败记录必须让路,
    # 胜者的 verified_empty 与 marker 原样保留,job 不回炉。
    db = Database(tmp_path / "cas-conflict.db")
    try:
        db.init_schema()
        _seed_indexed_job(db, "job-race", _BASE_CREATED)
        winner_digest = "sha256:" + "a" * 64

        class RacingStorage(_PerJobConceptsStorage):
            def __init__(self):
                super().__init__({"job-race": {"key_terms": []}})
                self.raced = False

            async def read_file(self, job_id, rel):
                if rel == "output/concepts.json" and not self.raced:
                    self.raced = True
                    db.replace_job_concept_occurrences(
                        domain="general", job_id=job_id, mapping={},
                        projection_source_digest=winner_digest,
                        expected_projection_source_digest=None,
                        projection_empty_reason="truly_empty",
                    )
                return await super().read_file(job_id, rel)

        engine = _make_engine(RacingStorage(), db)
        with pytest.raises(ConceptConflictError):
            await engine.reconcile_concept_occurrences_only("job-race")

        assert db.get_concept_occurrence_projection_source(
            "job-race",
        ) == winner_digest
        state = db.get_concept_occurrence_replay_state("job-race")
        assert state["state"] == "verified_empty"
        assert state["source_digest"] == winner_digest
        assert db.list_unreconciled_concept_occurrence_jobs() == []
    finally:
        db.close()


@pytest.mark.asyncio
async def test_matching_digest_with_nonempty_source_must_recompute():
    """原缺陷的直接反例:marker 摘要与真源一致但投影是空的, 而源里有真概念且证据可映射。

    旧实现按 marker 摘要短路直接落 verified_empty, 把错误永久认证下来。
    marker 只说明发布过这个投影, 不说明投影正确;必须重算, 算出非空就得改回非空。
    """
    db = _DBStub(domain="ml")
    db.canonical_by_segment = {_SEGMENT_A: ["ev-a"]}
    db.calls.append({
        "term": "Alpha", "zh_name": "", "domain": "ml", "job_id": "seed",
        "content_type": "document", "location": None, "definition": "",
        "document_kind": "article",
    })
    source = {
        "evidence_note_type": "original",
        "key_terms": [{"term": "Alpha", "evidence_source_segment_ids": [_SEGMENT_A]}],
    }
    payload = json.dumps(source, ensure_ascii=False).encode("utf-8")
    real_digest = _concept_projection_source_digest("concepts", payload)
    # 错误现场:源有概念且证据可映射, 却按真实源摘要发布了空投影, 且无判定行。
    db.occurrence_projection_sources["j_wrong"] = real_digest
    db.occurrence_projection_empty["j_wrong"] = True
    db.occurrence_replacements.append(
        {"domain": "ml", "job_id": "j_wrong", "mapping": {}},
    )
    storage = _PerJobConceptsStorage({"j_wrong": payload})
    engine = _make_engine(storage, db)

    produced = await engine.reconcile_concept_occurrences_only("j_wrong")

    assert produced == 1
    assert db.occurrence_replacements[-1] == {
        "domain": "ml", "job_id": "j_wrong", "mapping": {"Alpha": ["ev-a"]},
    }
    assert db.replay_states.get("j_wrong") is None


@pytest.mark.asyncio
async def test_stale_projector_version_never_short_circuits_verified_empty():
    payload = {"key_terms": []}
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    digest = _concept_projection_source_digest("concepts", raw)
    db = _DBStub(domain="ml")
    db.occurrence_projection_sources["j_v1"] = digest
    db.occurrence_projection_empty["j_v1"] = True
    db.occurrence_projection_versions["j_v1"] = 1
    db.replay_states["j_v1"] = {
        "state": "verified_empty",
        "reason": "truly_empty",
        "source_digest": digest,
        "projector_version": 1,
    }
    engine = _make_engine(_PerJobConceptsStorage({"j_v1": raw}), db)

    assert await engine.reconcile_concept_occurrences_only("j_v1") == 0
    assert len(db.occurrence_replacements) == 1
    assert db.occurrence_projection_versions["j_v1"] == (
        CURRENT_CONCEPT_PROJECTOR_VERSION
    )
    assert db.replay_states["j_v1"]["projector_version"] == (
        CURRENT_CONCEPT_PROJECTOR_VERSION
    )

    assert await engine.reconcile_concept_occurrences_only("j_v1") == 0
    assert len(db.occurrence_replacements) == 1


@pytest.mark.asyncio
async def test_legacy_empty_marker_with_matching_source_becomes_verified(tmp_path):
    # 旧版合法的空投影行(真实源摘要,无判定行):一次复核读源确认
    # digest 一致后固化为 verified_empty,不重发布、不再回炉。
    db = Database(tmp_path / "legacy-legit-empty.db")
    try:
        db.init_schema()
        _seed_indexed_job(db, "job-legacy", _BASE_CREATED)
        payload = json.dumps(
            {"key_terms": []}, ensure_ascii=False,
        ).encode("utf-8")
        legacy_digest = "sha256:" + hashlib.sha256(
            b"concepts\0" + payload,
        ).hexdigest()
        db.replace_job_concept_occurrences(
            domain="general", job_id="job-legacy", mapping={},
            projection_source_digest=legacy_digest,
            expected_projection_source_digest=None,
            projection_empty_reason="truly_empty",
        )
        db._conn.execute(
            "DELETE FROM concept_occurrence_replay_state WHERE job_id=?",
            ("job-legacy",),
        )
        db._conn.commit()
        reconciled_at = db._conn.execute(
            "SELECT reconciled_at FROM concept_occurrence_projection WHERE job_id=?",
            ("job-legacy",),
        ).fetchone()[0]
        storage = _PerJobConceptsStorage({"job-legacy": payload})
        engine = _make_engine(storage, db)

        assert [job.id for job in await _drain_once(engine, db)] == ["job-legacy"]
        state = db.get_concept_occurrence_replay_state("job-legacy")
        assert state["state"] == "verified_empty"
        # 不再按 marker 摘要认证;真重算一次,算出来确实为空才落判定。
        assert state["reason"] == "truly_empty"
        current_digest = _concept_projection_source_digest("concepts", payload)
        assert state["source_digest"] == current_digest
        assert state["projector_version"] == CURRENT_CONCEPT_PROJECTOR_VERSION
        # 旧实现按 marker 摘要短路, 所以 marker 原样不动;现在必须真重算一次,
        # reconciled_at 会刷新。要守的不变量是投影内容没被改坏:仍绑同一源摘要且仍为空集。
        row = db._conn.execute(
            "SELECT source_digest, projection_digest"
            " FROM concept_occurrence_projection WHERE job_id=?",
            ("job-legacy",),
        ).fetchone()
        assert row[0] == current_digest
        assert row[1] == _EMPTY_CONCEPT_PROJECTION_DIGEST
        assert db.list_unreconciled_concept_occurrence_jobs() == []
        assert storage.reads["job-legacy"] == 1
    finally:
        db.close()
