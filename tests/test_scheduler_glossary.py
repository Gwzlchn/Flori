"""tests for scheduler._collect_glossary —— 评审产物 key_terms 采集为候选术语。

只喂 review["key_terms"](带候选定义),不读 missing_concepts。
用 storage / db stub 直接 await engine._collect_glossary(job_id),最小化依赖。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scheduler.scheduler import Scheduler


class _StorageStub:
    """read_file:concepts.json 缺时回 None,回退读 review.json(video/paper/audio 路径)。"""

    def __init__(self, payload: dict):
        self._data = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    async def read_file(self, job_id: str, rel: str) -> bytes | None:
        if rel == "output/concepts.json":
            return None
        assert rel == "output/review.json"
        return self._data


class _ConceptsStorageStub:
    """article 链:concepts.json 存在 → 优先采集自它(不读 review)。"""

    def __init__(self, payload: dict):
        self._data = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    async def read_file(self, job_id: str, rel: str) -> bytes | None:
        assert rel == "output/concepts.json"
        return self._data


class _DBStub:
    """记录 add_glossary_suggestion / add_glossary_relations 调用;get_job 返回固定
    domain/content_type;list_glossary 返回已采集的最小行(供 relations 段 resolve)。"""

    def __init__(self, domain: str = "ml", content_type: str = "video"):
        self._job = SimpleNamespace(domain=domain, content_type=content_type)
        self.calls: list[dict] = []
        self.relations: list[dict] = []

    def get_job(self, job_id: str):
        return self._job

    def add_glossary_suggestion(
        self, domain, term, job_id, content_type="", location=None, definition="", zh_name=""
    ):
        self.calls.append({
            "domain": domain, "term": term, "job_id": job_id,
            "content_type": content_type, "location": location,
            "definition": definition, "zh_name": zh_name,
        })

    def list_glossary(self, domain=None, status=None, q=None):
        return [
            {"term": c["term"], "zh_name": c["zh_name"] or "", "aliases": []}
            for c in self.calls
        ]

    def add_glossary_relations(self, domain, term, relations):
        self.relations.append({"domain": domain, "term": term, "relations": relations})
        return len(relations)


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
    db = _DBStub(domain="ml", content_type="video")
    engine = _make_engine(_StorageStub(review), db)

    await engine._collect_glossary("j_g_001")

    terms = {c["term"]: c for c in db.calls}
    assert "X" in terms
    assert terms["X"]["definition"] == "d"
    assert terms["X"]["domain"] == "ml"
    assert terms["X"]["content_type"] == "video"
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
async def test_bare_string_key_terms_no_definition():
    # 裸串元素:采集 term,definition 留空。
    review = {"key_terms": ["裸词"]}
    db = _DBStub()
    engine = _make_engine(_StorageStub(review), db)

    await engine._collect_glossary("j_g_001")

    assert len(db.calls) == 1
    assert db.calls[0]["term"] == "裸词"
    assert db.calls[0]["definition"] == ""


@pytest.mark.asyncio
async def test_no_key_terms_collects_nothing():
    # 即便有 missing_concepts,没有 key_terms 也不采集任何术语。
    review = {"missing_concepts": ["Y", "Z"]}
    db = _DBStub()
    engine = _make_engine(_StorageStub(review), db)

    await engine._collect_glossary("j_g_001")

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
    db = _DBStub(domain="dl", content_type="article")
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
    # article 链:concepts.json 存在 → 采集源是它(不读 review)。_ConceptsStorageStub
    # 的 read_file 断言只被以 concepts.json 调用,确保不回退 review。
    concepts = {"summary": "一句话", "key_terms": [{"term": "注意力机制", "definition": "权重分配"}]}
    db = _DBStub(domain="dl", content_type="article")
    engine = _make_engine(_ConceptsStorageStub(concepts), db)

    await engine._collect_glossary("j_c_001")

    terms = {c["term"]: c for c in db.calls}
    assert "注意力机制" in terms
    assert terms["注意力机制"]["definition"] == "权重分配"
    assert terms["注意力机制"]["domain"] == "dl"
