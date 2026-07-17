"""固定检索语料的加载、真实摄入、离线评测与原子工件输出。"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
import unicodedata
from copy import deepcopy
from collections import Counter, defaultdict
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

from httpx import ASGITransport, AsyncClient

from api.main import create_app
from api.mcp_server.server import build_server
from api.services import synthesis
from scheduler.scheduler import Scheduler
from shared.config import load_config
from shared.db import Database
from shared.models import Job, JobStatus
from shared.storage import LocalStorage
from tests.integration.provenance_fixture import publish_provenance_fixture


FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "retrieval_quality"
_ANSWERABLE_STRATA = {"exact", "paraphrase", "synonym", "cross_language"}
_SEMANTIC_STRATA = {"paraphrase", "synonym", "cross_language"}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

CitationValidator = Callable[[str, str, dict], dict]


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def normalize_chunk_body(text: str) -> str:
    """与生产 evidence body hash 共用的稳定文本规范。"""
    normalized = unicodedata.normalize(
        "NFC", (text or "").replace("\r\n", "\n").replace("\r", "\n"),
    )
    return "\n".join(line.rstrip() for line in normalized.split("\n")).strip()


def chunk_body_sha256(text: str) -> str:
    return sha256_bytes(normalize_chunk_body(text).encode("utf-8"))


def note_bytes(snapshot: dict) -> bytes:
    return ("\n".join(snapshot["note"]["lines"]) + "\n").encode("utf-8")


def expected_index_body(snapshot: dict) -> str:
    """把受控 fixture 转成生产 Scheduler 应写入 FTS 的正文。"""
    visible = [
        line.rstrip()
        for line in snapshot["note"]["lines"]
        if not line.lstrip().startswith("```")
    ]
    compact: list[str] = []
    for line in visible:
        if line or (compact and compact[-1]):
            compact.append(line)
    while compact and not compact[-1]:
        compact.pop()
    return "\n".join(compact)


def _expected_chunks(snapshot: dict) -> dict[str, str]:
    """按冻结的生产 chunker 语义提取预期块。"""
    chunks: list[str] = []
    current: list[str] = []
    for line in expected_index_body(snapshot).splitlines():
        if not line.strip():
            continue
        if line.startswith("#") and current:
            chunks.append("\n".join(current).strip())
            current = []
        current.append(line)
    if current:
        chunks.append("\n".join(current).strip())

    resolved: dict[str, str] = {}
    for key, spec in snapshot["chunks"].items():
        matches = [body for body in chunks if spec["anchor"] in body]
        if len(matches) != 1:
            raise AssertionError(
                f"{snapshot['job']['id']}:{key} anchor 匹配 {len(matches)} 个 chunk",
            )
        resolved[key] = matches[0]
    return resolved


def load_fixture(root: Path = FIXTURE_ROOT) -> dict:
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    query_doc = json.loads((root / "queries.json").read_text(encoding="utf-8"))
    corpus = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((root / "corpus").glob("*.json"))
    ]
    fixture = {
        "manifest": manifest,
        "query_doc": query_doc,
        "queries": query_doc["queries"],
        "corpus": corpus,
        "jobs": {item["job"]["id"]: item for item in corpus},
    }
    validate_fixture(fixture)
    return fixture


def validate_fixture(fixture: dict) -> None:
    manifest = fixture["manifest"]
    corpus = fixture["corpus"]
    queries = fixture["queries"]
    jobs = fixture["jobs"]

    assert len(corpus) == manifest["corpus"]["jobs"] == 24
    assert len(jobs) == 24
    assert Counter(item["job"]["content_type"] for item in corpus) == Counter(
        manifest["corpus"]["content_types"],
    )
    assert Counter(
        item["job"].get("document_kind") for item in corpus
        if item["job"]["content_type"] == "document"
    ) == Counter(manifest["corpus"]["document_kinds"])
    assert Counter(item["job"]["primary_language"] for item in corpus) == Counter(
        manifest["corpus"]["primary_languages"],
    )
    assert Counter(item["job"]["topic"] for item in corpus) == Counter(
        {topic: 4 for topic in manifest["corpus"]["topics"]},
    )
    assert sum(
        any(line.lstrip().startswith("```") for line in item["note"]["lines"])
        for item in corpus
    ) == 4
    assert sha256_bytes(canonical_bytes(corpus)) == manifest["corpus_sha256"]
    assert sha256_bytes(canonical_bytes(fixture["query_doc"])) == manifest["queries_sha256"]

    for item in corpus:
        job = item["job"]
        assert job["id"].startswith("rq-")
        assert job["pipeline"] == job["content_type"]
        assert item["note"]["lines"][0].startswith("# ")
        assert sum(line.startswith("## ") for line in item["note"]["lines"]) >= 3
        assert "" in item["note"]["lines"]
        assert _SHA256_RE.fullmatch(item["note"]["source_sha256"])
        assert sha256_bytes(note_bytes(item)) == item["note"]["source_sha256"]
        assert _SHA256_RE.fullmatch(item["note"]["artifact_sha256"])
        assert sha256_bytes(
            expected_index_body(item).encode("utf-8"),
        ) == item["note"]["artifact_sha256"]
        expected = _expected_chunks(item)
        for key, body in expected.items():
            frozen = item["chunks"][key]["body_sha256"]
            assert _SHA256_RE.fullmatch(frozen)
            assert chunk_body_sha256(body) == frozen

    assert len(queries) == manifest["queries"]["total"] == 96
    assert len({query["id"] for query in queries}) == 96
    assert Counter(query["stratum"] for query in queries) == Counter(
        manifest["queries"]["strata"],
    )
    single = [query for query in queries if query["stratum"] in _ANSWERABLE_STRATA]
    assert len(single) == 64
    assert Counter(query["language"] for query in single) == Counter(
        manifest["queries"]["single_source_languages"],
    )
    assert Counter(
        jobs[query["relevant"][0]["job_id"]]["job"]["content_type"]
        for query in single
    ) == Counter(manifest["queries"]["single_source_content_types"])
    assert Counter(
        jobs[query["relevant"][0]["job_id"]]["job"].get("document_kind")
        for query in single
        if jobs[query["relevant"][0]["job_id"]]["job"]["content_type"]
        == "document"
    ) == Counter(manifest["queries"]["single_source_document_kinds"])
    cross_language = [q for q in queries if q["stratum"] == "cross_language"]
    assert Counter(query["direction"] for query in cross_language) == Counter(
        manifest["queries"]["cross_language_directions"],
    )
    citation_cases = manifest["citation_conformance"]
    assert {case["kind"] for case in citation_cases} == {
        "valid", "unknown", "malformed", "unsupported", "zero",
        "cross_task", "tampered", "heading",
    }
    assert len({case["id"] for case in citation_cases}) == len(citation_cases)
    assert manifest["miss_reason_contract"]["classifier"] == "evidence-v1"

    for query in queries:
        assert query["rationale"].strip()
        if query["stratum"] == "unanswerable":
            assert query["relevant"] == []
            assert query["required_source_groups"] == []
            continue
        assert query["relevant"]
        for ref in query["relevant"]:
            snapshot = jobs[ref["job_id"]]
            assert ref["note_type"] == snapshot["note"]["note_type"]
            assert ref["chunk_key"] in snapshot["chunks"]
            if query["stratum"] == "exact":
                body = _expected_chunks(snapshot)[ref["chunk_key"]]
                assert _normalized_probe(query["query"]) in _normalized_probe(body)
        relevant_jobs = {ref["job_id"] for ref in query["relevant"]}
        for group in query["required_source_groups"]:
            assert group and set(group) <= relevant_jobs


def resolved_truth(fixture: dict, query: dict) -> list[dict]:
    truth: list[dict] = []
    for ref in query["relevant"]:
        snapshot = fixture["jobs"][ref["job_id"]]
        artifact_sha = snapshot["note"]["artifact_sha256"]
        body_sha = snapshot["chunks"][ref["chunk_key"]]["body_sha256"]
        identity = {
            **ref,
            "artifact_sha256": artifact_sha,
            "body_sha256": body_sha,
        }
        identity["evidence_fingerprint"] = sha256_bytes(canonical_bytes(identity))
        truth.append(identity)
    return truth


async def _complete_pipeline(
    scheduler: Scheduler, redis, db: Database, config, job_id: str,
) -> None:
    steps = await scheduler._get_job_pipeline_steps(job_id)
    assert steps is not None
    for name in steps:
        if await redis.get_step_status(job_id, name) == "skipped":
            continue
        await redis.set_step_status(job_id, name, "running")
        await scheduler.on_step_done(
            job_id, name, duration=0.001, worker="retrieval-quality-fixture",
        )


async def ingest_fixture(
    fixture: dict, redis, *, data_dir: Path, configs_dir: Path,
) -> tuple[Database, object, LocalStorage]:
    """只通过生产 pipeline completion 摄入，禁止调用底层 index helper。"""
    config = load_config(config_dir=configs_dir, data_dir=data_dir)
    config.jobs_dir = data_dir / "jobs"
    config.jobs_dir.mkdir(parents=True, exist_ok=True)
    config.prompts_dir = data_dir / "prompts"
    config.prompts_dir.mkdir(parents=True, exist_ok=True)
    db = Database(config.db_path)
    db.init_schema()
    storage = LocalStorage(config.jobs_dir)
    scheduler = Scheduler(redis, db, config, storage=storage)

    async def _workers_present(_pool):
        return True

    scheduler._pool_has_workers = _workers_present
    for snapshot in fixture["corpus"]:
        source = snapshot["job"]
        job = Job(
            id=source["id"],
            content_type=source["content_type"],
            pipeline=source["pipeline"],
            document_kind=source.get("document_kind") or "",
            domain=source["domain"],
            title=source["title"],
            meta=source.get("meta") or {},
        )
        db.create_job(job)
        metadata = {
            "title": source["title"],
            "fixture_id": source["id"],
            "primary_language": source["primary_language"],
        }
        concepts = {
            "key_terms": [{
                "term": source["topic"],
                "definition": f"retrieval quality fixture: {source['topic']}",
            }],
        }
        await storage.write_file(
            job.id, "input/metadata.json", canonical_bytes(metadata) + b"\n",
        )
        await storage.write_file(
            job.id, "output/concepts.json", canonical_bytes(concepts) + b"\n",
        )
        note_data = note_bytes(snapshot)
        await storage.write_file(job.id, snapshot["note"]["path"], note_data)
        provenance_notes = {
            snapshot["note"]["note_type"]: (snapshot["note"]["path"], note_data),
        }
        if source["content_type"] == "document":
            original_path = "intermediate/document_index.md"
            await storage.write_file(job.id, original_path, note_data)
            provenance_notes["original"] = (original_path, note_data)
        if source["content_type"] == "video":
            # video 在 smart 前有独立 mechanical completion effect；真实形态必须同时存在。
            await storage.write_file(
                job.id, "output/notes_mechanical.md", note_bytes(snapshot),
            )
            provenance_notes["mechanical"] = (
                "output/notes_mechanical.md", note_bytes(snapshot),
            )
        await publish_provenance_fixture(
            storage,
            job_id=job.id,
            pipeline=job.pipeline,
            notes=provenance_notes,
        )
        await scheduler.submit_job(job)
        await _complete_pipeline(scheduler, redis, db, config, job.id)
        assert db.get_job(job.id).status == JobStatus.DONE
        indexed_types = {
            row[0] for row in db._conn.execute(
                "SELECT DISTINCT note_type FROM note_chunks WHERE job_id=?",
                (job.id,),
            ).fetchall()
        }
        if source["content_type"] == "document":
            assert indexed_types == {snapshot["note"]["note_type"]}
    return db, config, storage


def _mcp_payload(result) -> list[dict]:
    structured = result[1] if isinstance(result, tuple) and len(result) == 2 else None
    if isinstance(structured, dict) and set(structured) == {"result"}:
        structured = structured["result"]
    if isinstance(structured, list):
        return structured
    blocks = result[0] if isinstance(result, tuple) else result
    if blocks:
        parsed = json.loads(blocks[0].text)
        if isinstance(parsed, dict) and set(parsed) == {"result"}:
            parsed = parsed["result"]
        if isinstance(parsed, list):
            return parsed
    raise AssertionError("MCP search 未返回结构化列表")


def _source_row(fixture: dict, row: dict) -> dict:
    snapshot = fixture["jobs"][row["job_id"]]
    note_type = row.get("note_type") or row.get("kind") or snapshot["note"]["note_type"]
    return {
        "job_id": row["job_id"],
        "note_type": note_type,
        "artifact_sha256": snapshot["note"]["artifact_sha256"],
    }


def _ask_row(fixture: dict, row: dict) -> dict:
    snapshot = fixture["jobs"][row["job_id"]]
    evidence = row.get("evidence") or {}
    body_sha = evidence.get("body_sha256") or chunk_body_sha256(row.get("body") or "")
    return {
        "job_id": row["job_id"],
        "note_type": evidence.get("note_type") or snapshot["note"]["note_type"],
        "artifact_sha256": evidence.get("artifact_sha256")
        or snapshot["note"]["artifact_sha256"],
        "body_sha256": body_sha,
        "chunk_id": evidence.get("chunk_id") or "",
    }


def _citation_source(row: dict) -> dict:
    evidence = dict(row.get("evidence") or {})
    return {
        "job_id": row["job_id"],
        "title": row.get("title") or "(无标题)",
        "domain": row.get("domain") or "",
        "content_type": row.get("content_type") or "",
        "note_type": row.get("note_type") or evidence.get("note_type") or "",
        "chunk_id": row.get("chunk_id") or evidence.get("chunk_id") or "",
        "artifact_sha256": row.get("artifact_sha256")
        or evidence.get("artifact_sha256") or "",
        "body_sha256": row.get("body_sha256") or evidence.get("body_sha256") or "",
        "body": row.get("body") or "",
        "section": row.get("section") or evidence.get("section") or "",
        "evidence": evidence,
    }


async def measure_surface_latencies(
    fixture: dict, db: Database, mcp, api_client: AsyncClient,
) -> dict[str, dict]:
    """按 manifest 固定的 5+30 采样区分 engine、ASGI API 与 MCP。"""
    sampling = fixture["manifest"]["sampling"]
    warmup_rounds = sampling["warmup_rounds"]
    measured_rounds = sampling["measured_rounds"]
    probes: list[dict] = []
    seen_strata: set[str] = set()
    for query in fixture["queries"]:
        if query["stratum"] not in seen_strata:
            probes.append(query)
            seen_strata.add(query["stratum"])

    async def run(surface: str, query: dict) -> float:
        domain = (query.get("filters") or {}).get("domain")
        started = time.perf_counter_ns()
        if surface == "fts_engine":
            db.search_notes(query["query"], domain=domain, limit=10)
        elif surface == "search_api":
            params = {"q": query["query"], "limit": 10}
            if domain:
                params["domain"] = domain
            response = await api_client.get(
                "/api/search", params=params,
            )
            assert response.status_code == 200
            response.json()
        elif surface == "ask":
            synthesis.retrieve(db, query["query"], domain=domain, k=8)
        else:
            result = await mcp.call_tool(
                "search", {"query": query["query"], "domain": domain, "limit": 10},
            )
            _mcp_payload(result)
        return (time.perf_counter_ns() - started) / 1_000_000

    result: dict[str, dict] = {}
    for surface in ("fts_engine", "search_api", "mcp", "ask"):
        cold = await run(surface, probes[0])
        for index in range(warmup_rounds):
            await run(surface, probes[index % len(probes)])
        measured = [
            await run(surface, probes[index % len(probes)])
            for index in range(measured_rounds)
        ]
        result[surface] = {
            "cold": cold,
            "warm_p50": _nearest_rank(measured, 0.5),
            "warm_p95": _nearest_rank(measured, 0.95),
            "warmup_rounds": warmup_rounds,
            "measured_rounds": measured_rounds,
        }
    return result


async def evaluate_rankings(fixture: dict, db: Database, storage: LocalStorage) -> dict:
    mcp = build_server(db, storage)
    api_client = AsyncClient(
        transport=ASGITransport(app=create_app(
            db=db,
            config=SimpleNamespace(jobs_dir=storage.jobs_dir),
        )),
        base_url="http://test",
    )
    performance = await measure_surface_latencies(fixture, db, mcp, api_client)
    records: list[dict] = []
    digest_rows: list[dict] = []
    for query in fixture["queries"]:
        params = query.get("filters") or {}
        domain = params.get("domain")
        started = time.perf_counter_ns()
        api_params = {"q": query["query"], "limit": 10}
        if domain:
            api_params["domain"] = domain
        response = await api_client.get(
            "/api/search", params=api_params,
        )
        assert response.status_code == 200
        search_items = response.json()["items"]
        search_ms = (time.perf_counter_ns() - started) / 1_000_000
        search_rows = [_source_row(fixture, item) for item in search_items]

        started = time.perf_counter_ns()
        mcp_result = await mcp.call_tool(
            "search", {"query": query["query"], "domain": domain, "limit": 10},
        )
        mcp_ms = (time.perf_counter_ns() - started) / 1_000_000
        mcp_rows = [_source_row(fixture, item) for item in _mcp_payload(mcp_result)]

        started = time.perf_counter_ns()
        ask_items = synthesis.retrieve(db, query["query"], domain=domain, k=8)
        ask_ms = (time.perf_counter_ns() - started) / 1_000_000
        ask_rows = [_ask_row(fixture, item) for item in ask_items]
        surfaces = {
            "search": {"latency_ms": search_ms, "results": search_rows},
            "mcp": {"latency_ms": mcp_ms, "results": mcp_rows},
            "ask": {
                "latency_ms": ask_ms,
                "results": ask_rows,
                "citation_sources": [_citation_source(item) for item in ask_items],
            },
        }
        records.append({
            "id": query["id"],
            "stratum": query["stratum"],
            "language": query["language"],
            "direction": query.get("direction"),
            "query": query["query"],
            "filters": params,
            "truth": resolved_truth(fixture, query),
            "required_source_groups": query["required_source_groups"],
            "surfaces": surfaces,
        })
        digest_rows.append({
            "id": query["id"],
            "search": search_rows,
            "mcp": mcp_rows,
            "ask": ask_rows,
        })
    await api_client.aclose()
    ranking_bytes = canonical_bytes(digest_rows)
    return {
        "records": records,
        "performance": performance,
        "ranking_bytes": ranking_bytes,
        "ranking_digest": sha256_bytes(ranking_bytes),
    }


def _nearest_rank(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def _result_identity(row: dict, surface: str) -> tuple[str, ...]:
    identity = (
        row.get("job_id") or "",
        row.get("note_type") or "",
        row.get("artifact_sha256") or "",
    )
    if surface == "ask":
        return (*identity, row.get("body_sha256") or "")
    return identity


def _query_score(record: dict, surface: str, k: int) -> tuple[float, float, bool]:
    relevant = {_result_identity(truth, surface) for truth in record["truth"]}
    ordered = [
        _result_identity(row, surface)
        for row in record["surfaces"][surface]["results"][:k]
    ]
    if not relevant:
        return 0.0, 0.0, not ordered
    hits = relevant & set(ordered)
    recall = len(hits) / len(relevant)
    first = next((idx for idx, identity in enumerate(ordered, 1) if identity in relevant), None)
    return recall, (1.0 / first if first else 0.0), not hits


def _score_rows(rows: list[dict], surface: str) -> dict:
    if not rows:
        return {
            "n": 0, "recall_at_1": 0.0, "recall_at_3": 0.0,
            "recall_at_5": 0.0, "recall_at_10": 0.0,
            "mrr_at_10": 0.0, "relevant_no_hit": 0.0,
        }
    result = {"n": len(rows)}
    for k in (1, 3, 5, 10):
        result[f"recall_at_{k}"] = sum(
            _query_score(record, surface, k)[0] for record in rows
        ) / len(rows)
    result["mrr_at_10"] = sum(
        _query_score(record, surface, 10)[1] for record in rows
    ) / len(rows)
    result["relevant_no_hit"] = sum(
        _query_score(record, surface, 10)[2] for record in rows
    ) / len(rows)
    return result


def _aggregate(
    fixture: dict,
    records: list[dict],
    surface: str,
    performance: dict,
) -> dict:
    answerable_strata = _ANSWERABLE_STRATA | {"cross_source"}
    strata = {
        stratum: _score_rows(
            [record for record in records if record["stratum"] == stratum],
            surface,
        )
        for stratum in sorted(answerable_strata)
    }
    cross = [record for record in records if record["stratum"] == "cross_source"]
    coverages: list[float] = []
    for record in cross:
        found = {row["job_id"] for row in record["surfaces"][surface]["results"][:8]}
        groups = record["required_source_groups"]
        coverages.append(sum(bool(set(group) & found) for group in groups) / len(groups))
    unavailable = [record for record in records if record["stratum"] == "unanswerable"]
    empty = [not record["surfaces"][surface]["results"] for record in unavailable]
    answerable = [record for record in records if record["stratum"] in answerable_strata]
    single_source = [
        record for record in records if record["stratum"] in _ANSWERABLE_STRATA
    ]
    no_hit = [_query_score(record, surface, 10)[2] for record in answerable]
    duplicate_jobs: list[float] = []
    duplicate_sources: list[float] = []
    for record in records:
        results = record["surfaces"][surface]["results"][:8]
        jobs = [row["job_id"] for row in results]
        sources = [
            (row["job_id"], row["note_type"], row["artifact_sha256"])
            for row in results
        ]
        duplicate_jobs.append((len(jobs) - len(set(jobs))) / max(1, len(jobs)))
        duplicate_sources.append((len(sources) - len(set(sources))) / max(1, len(sources)))
    by_language = {
        language: _score_rows(
            [record for record in single_source if record["language"] == language],
            surface,
        )
        for language in ("zh", "en")
    }
    by_direction = {
        direction: _score_rows(
            [
                record for record in records
                if record.get("direction") == direction
            ],
            surface,
        )
        for direction in ("zh_to_en", "en_to_zh")
    }
    by_content_type = {}
    for content_type in ("video", "document", "audio"):
        rows = [
            record for record in single_source
            if fixture["jobs"][record["truth"][0]["job_id"]]["job"]["content_type"]
            == content_type
        ]
        by_content_type[content_type] = _score_rows(rows, surface)
    by_document_kind = {}
    for document_kind in ("research_paper", "article"):
        rows = [
            record for record in single_source
            if fixture["jobs"][record["truth"][0]["job_id"]]["job"].get(
                "document_kind"
            ) == document_kind
        ]
        by_document_kind[document_kind] = _score_rows(rows, surface)
    return {
        "strata": strata,
        "answerable": _score_rows(answerable, surface),
        "by_language": by_language,
        "by_direction": by_direction,
        "by_content_type": by_content_type,
        "by_document_kind": by_document_kind,
        "cross_source_coverage_at_8": sum(coverages) / len(coverages),
        "relevant_no_hit": sum(no_hit) / len(no_hit),
        "unanswerable_empty": sum(empty) / len(empty),
        "unanswerable_false_positive": 1.0 - sum(empty) / len(empty),
        "duplicate_job_rate_at_8": sum(duplicate_jobs) / len(duplicate_jobs),
        "duplicate_source_rate_at_8": sum(duplicate_sources) / len(duplicate_sources),
        "latency_ms": performance,
    }


def deterministic_citation_validator(
    _answer: str, source_manifest: list[dict], truth: list[dict],
) -> dict:
    """离线 stub 只验证已检索 source；合并树必须注入真实 Ask validator。"""
    relevant = {row["job_id"] for row in truth}
    cited = [row for row in source_manifest if row["job_id"] in relevant]
    if not cited:
        return {
            "valid": False, "structural": 0.0, "source": 0.0,
            "claim": 0.0, "coverage": 0.0, "reason": "zero_citation",
        }
    return {
        "valid": True,
        "structural": 1.0,
        "source": 1.0,
        "claim": 1.0,
        "coverage": len({row["job_id"] for row in cited}) / len(relevant),
        "reason": "ok",
    }


def production_citation_validator(
    task_id: str, answer: str, manifest: dict,
) -> dict:
    """直接调用生产 Ask validator；case 与 manifest 由冻结矩阵提供。"""
    from shared.ask_citations import validate_ask_citations

    return validate_ask_citations(task_id, answer, manifest)


def _citation_passage(fixture: dict, ref: dict) -> dict:
    snapshot = fixture["jobs"][ref["job_id"]]
    body = _expected_chunks(snapshot)[ref["chunk_key"]]
    return {
        "job_id": ref["job_id"],
        "title": snapshot["job"]["title"],
        "domain": snapshot["job"]["domain"],
        "content_type": snapshot["job"]["content_type"],
        "note_type": snapshot["note"]["note_type"],
        "chunk_id": f"{ref['job_id']}:{ref['chunk_key']}",
        "artifact_sha256": snapshot["note"]["artifact_sha256"],
        "body_sha256": chunk_body_sha256(body),
        "body": body,
        "section": ref["chunk_key"],
        "evidence": {
            "chunk_id": f"{ref['job_id']}:{ref['chunk_key']}",
            "note_type": snapshot["note"]["note_type"],
            "section": ref["chunk_key"],
        },
    }


def _supported_claim(body: str) -> str:
    return next(
        line.strip() for line in body.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )


def evaluate_citation_conformance(
    fixture: dict,
    *,
    citation_validator: CitationValidator = production_citation_validator,
) -> dict:
    """以冻结正反例评估 validator，不读取任何一次召回的正文或排名。"""
    from shared.ask_citations import build_source_manifest

    rows: list[dict] = []
    dimension_results: dict[str, list[bool]] = defaultdict(list)
    for case in fixture["manifest"]["citation_conformance"]:
        task_id = f"rq-citation-{case['id']}"
        passage = _citation_passage(fixture, case["source"])
        manifest = build_source_manifest(task_id, "冻结引用一致性矩阵", [passage])
        claim = _supported_claim(passage["body"])
        kind = case["kind"]
        answer = f"{claim} [来源1]。"
        validation_task_id = task_id
        if kind == "unknown":
            answer = f"{claim} [来源9]。"
        elif kind == "malformed":
            answer = f"{claim} [来源x]。"
        elif kind == "unsupported":
            answer = "模型会自动获得意识 [来源1]。"
        elif kind == "zero":
            answer = claim
        elif kind == "cross_task":
            validation_task_id = f"{task_id}-other"
        elif kind == "tampered":
            manifest = deepcopy(manifest)
            manifest["sources"][0]["body"] += "\n被篡改的正文"
        elif kind == "heading":
            answer = f"## 模型会自动获得意识\n{claim} [来源1]。"

        observed = citation_validator(validation_task_id, answer, manifest)
        expected_errors = set(case.get("expected_errors") or [])
        passed = (
            observed.get("status") == case["expected_status"]
            and expected_errors <= set(observed.get("errors") or [])
        )
        for dimension in case["dimensions"]:
            dimension_results[dimension].append(passed)
        rows.append({
            "id": case["id"],
            "kind": kind,
            "expected_status": case["expected_status"],
            "expected_errors": sorted(expected_errors),
            "observed_status": observed.get("status"),
            "observed_errors": observed.get("errors") or [],
            "observed_metrics": observed.get("metrics") or {},
            "passed": passed,
        })

    scores = {
        dimension: sum(values) / len(values)
        for dimension, values in dimension_results.items()
    }
    failed = [row["id"] for row in rows if not row["passed"]]
    return {
        **scores,
        "evaluated_cases": len(rows),
        "passed_cases": len(rows) - len(failed),
        "failed_cases": len(failed),
        "invalid_queries": len(failed),
        "cases": rows,
    }


def build_metrics(
    fixture: dict,
    records: list[dict],
    performance: dict,
    *,
    citation_validator: CitationValidator = production_citation_validator,
) -> dict:
    latency_by_surface = {
        "search": performance["search_api"],
        "mcp": performance["mcp"],
        "ask": performance["ask"],
    }
    surfaces = {
        name: _aggregate(fixture, records, name, latency_by_surface[name])
        for name in ("search", "mcp", "ask")
    }
    citation = evaluate_citation_conformance(
        fixture, citation_validator=citation_validator,
    )
    surfaces["ask"]["citation"] = citation
    answerable = [
        record for record in records
        if record["stratum"] in _ANSWERABLE_STRATA | {"cross_source"}
    ]
    retrieval_evidence_coverage = sum(
        _query_score(record, "ask", 8)[0] for record in answerable
    ) / len(answerable)
    surfaces["ask"]["retrieval_evidence_coverage_at_8"] = (
        retrieval_evidence_coverage
    )

    thresholds = fixture["manifest"]["thresholds"]
    exact = surfaces["search"]["strata"]["exact"]
    semantic_rows = [
        record for record in records if record["stratum"] in _SEMANTIC_STRATA
    ]
    semantic_recall = sum(
        _query_score(record, "search", 5)[0] for record in semantic_rows
    ) / len(semantic_rows)
    semantic_mrr = sum(
        _query_score(record, "search", 10)[1] for record in semantic_rows
    ) / len(semantic_rows)
    quality_checks = {
        "exact_recall": exact["recall_at_5"] >= thresholds["exact"]["recall_at_5"],
        "exact_mrr": exact["mrr_at_10"] >= thresholds["exact"]["mrr_at_10"],
        "semantic_recall": semantic_recall >= thresholds["semantic"]["recall_at_5"],
        "semantic_mrr": semantic_mrr >= thresholds["semantic"]["mrr_at_10"],
        "cross_source": surfaces["search"]["cross_source_coverage_at_8"]
        >= thresholds["cross_source"]["coverage_at_8"],
        "relevant_no_hit": surfaces["search"]["relevant_no_hit"]
        <= thresholds["relevant_no_hit"]["overall_max"],
        "unanswerable_empty": surfaces["search"]["unanswerable_empty"]
        >= thresholds["unanswerable"]["empty_min"],
        "unanswerable_false_positive": surfaces["search"]["unanswerable_false_positive"]
        <= thresholds["unanswerable"]["false_positive_max"],
        **{
            f"no_hit_{stratum}": row["relevant_no_hit"]
            <= thresholds["relevant_no_hit"]["critical_stratum_max"]
            for stratum, row in surfaces["search"]["strata"].items()
        },
        "citation_structural": citation["structural"]
        >= thresholds["citation"]["structural_min"],
        "citation_source": citation["source"]
        >= thresholds["citation"]["source_min"],
        "citation_claim": citation["claim"]
        >= thresholds["citation"]["claim_min"],
        "citation_coverage": citation["coverage"]
        >= thresholds["citation"]["coverage_min"],
        "retrieval_evidence_coverage": retrieval_evidence_coverage
        >= thresholds["retrieval_evidence"]["coverage_at_8"],
        "engine_latency": max(
            performance["fts_engine"]["warm_p95"],
            performance["ask"]["warm_p95"],
        ) <= thresholds["latency_ms"]["engine_warm_p95_max"],
        "search_api_latency": performance["search_api"]["warm_p95"]
        <= thresholds["latency_ms"]["api_mcp_warm_p95_max"],
        "mcp_latency": surfaces["mcp"]["latency_ms"]["warm_p95"]
        <= thresholds["latency_ms"]["api_mcp_warm_p95_max"],
    }
    return {
        "surfaces": surfaces,
        "latencies": performance,
        "semantic": {"recall_at_5": semantic_recall, "mrr_at_10": semantic_mrr},
        "quality_checks": quality_checks,
        "quality_gate": {"passed": all(quality_checks.values())},
    }


def inspect_ingestion(fixture: dict, db: Database) -> dict:
    rows = db._conn.execute(
        "SELECT job_id,note_type,section,body,evidence_json FROM note_chunks ORDER BY job_id,chunk_id",
    ).fetchall()
    by_job: dict[str, list] = defaultdict(list)
    for row in rows:
        by_job[row["job_id"]].append(row)
    checks = {
        "all_jobs_indexed": set(by_job) == set(fixture["jobs"]),
        "expected_chunks_per_indexed_note": all(
            len(by_job[job_id])
            == (8 if snapshot["job"]["content_type"] == "video" else 4)
            for job_id, snapshot in fixture["jobs"].items()
        ),
        "sections_preserved": all(
            row["section"] for job_rows in by_job.values() for row in job_rows
        ),
        "fenced_code_body_preserved": any(
            "mask = local + global" in row["body"]
            for row in by_job.get("rq-ml-video", [])
        ),
        "two_cjk_parameterized_fallback": any(
            row["job_id"] == "rq-security-article"
            for row in db.search_note_chunks("风控", domain="quality-security", limit=10)[1]
        ),
    }
    fingerprint_ok = True
    for job_id, snapshot in fixture["jobs"].items():
        expected = {
            spec["body_sha256"] for spec in snapshot["chunks"].values()
        }
        actual = {chunk_body_sha256(row["body"]) for row in by_job.get(job_id, [])}
        fingerprint_ok = fingerprint_ok and actual == expected
        for row in by_job.get(job_id, []):
            try:
                evidence = json.loads(row["evidence_json"] or "{}")
            except json.JSONDecodeError:
                evidence = {}
            fingerprint_ok = fingerprint_ok and (
                evidence.get("artifact_sha256")
                == snapshot["note"]["artifact_sha256"]
                and evidence.get("body_sha256") == chunk_body_sha256(row["body"])
            )
    checks["source_and_chunk_fingerprints"] = fingerprint_ok
    return {"checks": checks, "indexed_jobs": len(by_job), "chunk_rows": len(rows)}


def _normalized_probe(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value or "").casefold().split())


def _classify_answerable_miss(fixture: dict, record: dict) -> tuple[str, str]:
    """由结果身份、过滤条件和冻结正文推导原因，不读取 query stratum。"""
    truth = record.get("truth") or []
    if not truth:
        return "unknown", "answerable query missing truth identity"
    required_fields = {"job_id", "note_type", "artifact_sha256", "body_sha256", "chunk_key"}
    if any(not required_fields <= set(item) for item in truth):
        return "unknown", "truth identity incomplete"

    returned = record["surfaces"]["search"]["results"][:10]
    truth_jobs = {item["job_id"] for item in truth}
    if any(
        row.get("job_id") in truth_jobs
        and _result_identity(row, "search")
        not in {_result_identity(item, "search") for item in truth}
        for row in returned
    ):
        return "identity_mismatch", "same job returned with a different note artifact"

    domain = (record.get("filters") or {}).get("domain")
    if domain and any(
        fixture["jobs"][item["job_id"]]["job"]["domain"] != domain
        for item in truth
    ):
        return "filter_truth_mismatch", "frozen truth is outside the requested domain"

    needle = _normalized_probe(record.get("query") or "")
    bodies = [
        _normalized_probe(
            _expected_chunks(fixture["jobs"][item["job_id"]])[item["chunk_key"]],
        )
        for item in truth
    ]
    if needle and any(needle in body for body in bodies):
        return "known_fix_regression", "literal query occurs in the frozen relevant chunk"
    if needle and bodies:
        return "semantic_lexical_gap", "no literal query occurs in any frozen relevant chunk"
    return "unknown", "query or frozen relevant body could not be normalized"


def classify_miss_reasons(fixture: dict, records: list[dict]) -> dict:
    """按冻结 evidence-v1 规则输出逐 query 证据，未知分类会阻断决策。"""
    contract = fixture["manifest"]["miss_reason_contract"]
    allowed = set(contract["reasons"])
    rows: list[dict] = []
    for record in records:
        reason = ""
        evidence = ""
        if record["stratum"] == "unanswerable":
            if record["surfaces"]["search"]["results"]:
                if record.get("filters"):
                    reason = "filtered_unanswerable_false_positive"
                    evidence = "filtered unanswerable query returned at least one result"
                else:
                    reason = "out_of_scope_false_positive"
                    evidence = "out-of-corpus query returned at least one result"
        elif _query_score(record, "search", 10)[2]:
            reason, evidence = _classify_answerable_miss(fixture, record)
        if not reason:
            continue
        if reason not in allowed:
            reason = "unknown"
            evidence = "derived reason is outside the frozen classifier contract"
        rows.append({
            "id": record["id"],
            "reason": reason,
            "evidence": evidence,
            "answerable": record["stratum"] != "unanswerable",
        })
    counts = Counter(row["reason"] for row in rows)
    answerable_rows = [row for row in rows if row["answerable"]]
    return {
        "classifier": contract["classifier"],
        "counts": dict(sorted(counts.items())),
        "unknown": counts["unknown"],
        "answerable_semantic_only": all(
            row["reason"] == "semantic_lexical_gap" for row in answerable_rows
        ),
        "queries": rows,
    }


def decide_vector_stage(
    decision_gate: dict,
    quality_gate: dict,
    quality_checks: dict[str, bool],
    miss_classification: dict,
) -> dict:
    """只有全部 answerable miss 均有语义缺口证据时才触发向量阶段。"""
    semantic_keys = {
        "semantic_recall", "semantic_mrr", "cross_source",
        "no_hit_paraphrase", "no_hit_synonym", "no_hit_cross_language",
        "no_hit_cross_source", "relevant_no_hit",
        "citation_structural", "citation_source", "citation_claim",
        "citation_coverage", "retrieval_evidence_coverage",
    }
    semantic_failures = [
        key for key in sorted(semantic_keys)
        if not quality_checks.get(key, True)
    ]
    nonsemantic_failures = [
        key for key, passed in quality_checks.items()
        if not passed and key not in semantic_keys
    ]
    if (
        not decision_gate["passed"]
        or not miss_classification.get("answerable_semantic_only", False)
    ):
        return {
            "triggered": False,
            "reason": "insufficient_decision_evidence",
            "failed_strata": [],
        }
    if not nonsemantic_failures and semantic_failures:
        return {
            "triggered": True,
            "reason": "semantic_quality_below_threshold_after_known_fixes",
            "failed_strata": semantic_failures,
        }
    if quality_gate["passed"]:
        return {
            "triggered": False,
            "reason": "fts5_meets_declared_thresholds",
            "failed_strata": [],
        }
    return {
        "triggered": False,
        "reason": "quality_failure_not_semantic",
        "failed_strata": nonsemantic_failures,
    }


def build_artifact(
    fixture: dict,
    first: dict,
    second: dict,
    ingestion: dict,
    *,
    main_sha: str,
    citation_validator: CitationValidator = production_citation_validator,
) -> dict:
    metrics = build_metrics(
        fixture, first["records"], first["performance"],
        citation_validator=citation_validator,
    )
    ask_unique = all(
        len([row["job_id"] for row in record["surfaces"]["ask"]["results"]])
        == len({row["job_id"] for row in record["surfaces"]["ask"]["results"]})
        for record in first["records"]
    )
    filtered_unanswerable = [
        record for record in first["records"]
        if record["stratum"] == "unanswerable" and record["filters"]
    ]
    filters_honored = all(
        not record["surfaces"][surface]["results"]
        for record in filtered_unanswerable
        for surface in ("search", "mcp", "ask")
    )
    source_fingerprints_bound = all(
        _SHA256_RE.fullmatch(row.get("artifact_sha256") or "")
        and (
            surface != "ask"
            or _SHA256_RE.fullmatch(row.get("body_sha256") or "")
        )
        for record in first["records"]
        for surface in ("search", "mcp", "ask")
        for row in record["surfaces"][surface]["results"]
    )
    exact_thresholds = fixture["manifest"]["thresholds"]["exact"]
    exact_known_fixes_hold = all(
        metrics["surfaces"][surface]["strata"]["exact"]["recall_at_5"]
        >= exact_thresholds["recall_at_5"]
        and metrics["surfaces"][surface]["strata"]["exact"]["mrr_at_10"]
        >= exact_thresholds["mrr_at_10"]
        for surface in ("search", "mcp", "ask")
    )
    miss_classification = classify_miss_reasons(fixture, first["records"])
    decision_checks = {
        **ingestion["checks"],
        "corpus_count": ingestion["indexed_jobs"] == 24,
        "query_count": len(first["records"]) == 96,
        "main_sha_bound": _GIT_SHA_RE.fullmatch(main_sha) is not None,
        "ranking_digest_equal": first["ranking_bytes"] == second["ranking_bytes"],
        "search_mcp_ranking_equal": all(
            record["surfaces"]["search"]["results"]
            == record["surfaces"]["mcp"]["results"]
            for record in first["records"]
        ),
        "filters_honored": filters_honored,
        "source_fingerprints_bound": source_fingerprints_bound,
        "exact_known_fixes_hold": exact_known_fixes_hold,
        "ask_one_chunk_per_job": ask_unique,
        "ask_deterministic_rrf": first["ranking_bytes"] == second["ranking_bytes"],
        "citation_adapter_executed": (
            metrics["surfaces"]["ask"]["citation"]["evaluated_cases"]
            == len(fixture["manifest"]["citation_conformance"])
        ),
        "citation_validator_consistent": (
            metrics["surfaces"]["ask"]["citation"]["failed_cases"] == 0
        ),
        "miss_reason_classification_complete": miss_classification["unknown"] == 0,
        "answerable_misses_semantic_only": (
            miss_classification["answerable_semantic_only"]
        ),
    }
    decision_gate = {"passed": all(decision_checks.values()), "checks": decision_checks}
    quality_checks = metrics["quality_checks"]
    vector = decide_vector_stage(
        decision_gate, metrics["quality_gate"], quality_checks,
        miss_classification,
    )
    return {
        "schema_version": 1,
        "main_sha": main_sha,
        "corpus_sha256": fixture["manifest"]["corpus_sha256"],
        "queries_sha256": fixture["manifest"]["queries_sha256"],
        "environment": {
            "sqlite": "production Database",
            "redis": "integration real Redis",
            "sampling": fixture["manifest"]["sampling"],
        },
        "ranking_digests": [first["ranking_digest"], second["ranking_digest"]],
        "ingestion": ingestion,
        "metrics": metrics,
        "queries": first["records"],
        "miss_reasons": miss_classification["counts"],
        "miss_reason_evidence": miss_classification,
        "decision_evidence_gate": decision_gate,
        "quality_gate": metrics["quality_gate"],
        "vector_decision": vector,
    }


def atomic_write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)
