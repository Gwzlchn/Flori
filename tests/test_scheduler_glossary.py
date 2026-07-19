"""tests for scheduler._collect_glossary —— 评审产物 key_terms 采集为候选术语。

只喂 review["key_terms"](带候选定义),不读 missing_concepts。
用 storage / db stub 直接 await engine._collect_glossary(job_id),最小化依赖。"""

from __future__ import annotations

import asyncio
import json
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from scheduler.scheduler import Scheduler
from shared.db import ConceptConflictError, Database
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
        self.definition_states: dict[str, dict] = {}

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
    ):
        if projection_source_digest is not None:
            assert self.occurrence_projection_sources.get(job_id) \
                == expected_projection_source_digest
            self.occurrence_projection_sources[job_id] = projection_source_digest
        self.occurrence_replacements.append({
            "domain": domain,
            "job_id": job_id,
            "mapping": {term: list(ids) for term, ids in mapping.items()},
        })

    def get_concept_occurrence_projection_source(self, job_id: str):
        return self.occurrence_projection_sources.get(job_id)


def _make_engine(storage, db):
    # _collect_glossary 仅用 self.storage / self.db;config 只需提供 jobs_dir。
    config = SimpleNamespace(jobs_dir=Path("/tmp/does-not-matter"))
    return Scheduler(redis=None, db=db, config=config, storage=storage)


@pytest.mark.asyncio
async def test_collects_key_terms_with_definition():
    # key_terms=[{"term":"X","definition":"d"}] -> 对 X 采集,definition 传 "d"。
    review = {
        "key_terms": [{"term": "X", "definition": "d"}],
        "missing_concepts": ["Y"],
    }
    db = _DBStub(domain="ml", content_type="document", document_kind="article")
    engine = _make_engine(_StorageStub(review), db)

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
    engine = _make_engine(_StorageStub(review), db)

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
    "failure", ["missing", "malformed", "non_object", "unreliable"],
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

    if failure == "missing":
        storage.concepts = storage.review = None
    elif failure == "malformed":
        storage.concepts = b"{broken"
    elif failure == "non_object":
        storage.concepts = b"[]"
    else:
        storage.concepts = None
        storage.review = b'{"schema_version":2,"review_reliable":false}'
    await engine._collect_glossary("j_replay")

    assert db.occurrence_replacements[-1]["mapping"] == {}


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
        await _make_engine(EmptyStorage(), db)._collect_glossary("job-missing")
        assert db.list_concept_occurrences(
            "general", "unused", include_invalid=True,
        ) == []
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
        )
        assert db.list_unreconciled_concept_occurrence_jobs() == []

        # FTS/canonical evidence 新版本与旧 marker 不能同时可见。模拟索引提交后、
        # glossary 对账前崩溃,周期查询必须重新认领这个 Job。
        db.index_job_notes(
            "job-retry-occurrence", "original", "title-v2", "body-v2",
            content_type="document", domain="general",
        )
        assert db.get_concept_occurrence_projection_source(
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

        with pytest.raises(ConceptConflictError, match="source changed"):
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
        )
        assert db.get_concept_occurrence_projection_source(
            "job-occurrence-cas",
        ) == source_b
    finally:
        db.close()


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
