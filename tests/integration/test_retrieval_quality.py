"""检索黄金集的固定合同和真实 pipeline 质量工件。"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.integration.retrieval_quality import (
    FIXTURE_ROOT,
    _query_score,
    atomic_write_json,
    build_artifact,
    decide_vector_stage,
    deterministic_citation_validator,
    evaluate_citation_conformance,
    evaluate_rankings,
    ingest_fixture,
    load_fixture,
    production_citation_validator,
    resolved_truth,
)


pytestmark = pytest.mark.integration


def test_retrieval_quality_fixture_contract():
    fixture = load_fixture()
    assert len(fixture["corpus"]) == 24
    assert len(fixture["queries"]) == 96
    assert len(resolved_truth(fixture, fixture["queries"][0])) == 1


def test_zero_citation_is_not_vacuously_precise():
    result = deterministic_citation_validator(
        "没有引用", [], [{"job_id": "rq-ml-video"}],
    )
    assert result == {
        "valid": False,
        "structural": 0.0,
        "source": 0.0,
        "claim": 0.0,
        "coverage": 0.0,
        "reason": "zero_citation",
    }


def test_relevance_requires_note_artifact_and_ask_body_identity():
    truth = {
        "job_id": "rq-job",
        "note_type": "smart",
        "artifact_sha256": "a" * 64,
        "body_sha256": "b" * 64,
    }
    record = {
        "truth": [truth],
        "surfaces": {
            "search": {"results": [{**truth, "note_type": "mechanical"}]},
            "mcp": {"results": [{**truth, "artifact_sha256": "c" * 64}]},
            "ask": {"results": [{**truth, "body_sha256": "d" * 64}]},
        },
    }
    for surface in ("search", "mcp", "ask"):
        assert _query_score(record, surface, 10) == (0.0, 0.0, True)
    record["surfaces"]["search"]["results"] = [truth]
    assert _query_score(record, "search", 10) == (1.0, 1.0, False)


def test_unsupported_case_lowers_frozen_validator_claim_metric():
    fixture = load_fixture()
    baseline = evaluate_citation_conformance(fixture)

    def accepts_unsupported(task_id, answer, manifest):
        result = production_citation_validator(task_id, answer, manifest)
        if answer.startswith("模型会自动获得意识"):
            result = {
                **result,
                "status": "valid",
                "errors": [],
                "metrics": {
                    "structural_precision": 1.0,
                    "source_precision": 1.0,
                    "claim_precision": 1.0,
                    "coverage": 1.0,
                },
            }
        return result

    compromised = evaluate_citation_conformance(
        fixture, citation_validator=accepts_unsupported,
    )
    assert compromised["claim"] < baseline["claim"]
    assert "citation-unsupported" in {
        row["id"] for row in compromised["cases"] if not row["passed"]
    }


def test_vector_trigger_rejects_nonsemantic_answerable_miss():
    decision = decide_vector_stage(
        {"passed": True},
        {"passed": False},
        {"semantic_recall": False, "semantic_mrr": False},
        {"answerable_semantic_only": False},
    )
    assert decision == {
        "triggered": False,
        "reason": "insufficient_decision_evidence",
        "failed_strata": [],
    }


def test_atomic_json_writer_replaces_complete_document(tmp_path):
    target = tmp_path / "retrieval-quality.json"
    atomic_write_json(target, {"round": 1})
    atomic_write_json(target, {"round": 2, "complete": True})
    assert json.loads(target.read_text(encoding="utf-8")) == {
        "round": 2, "complete": True,
    }
    assert list(tmp_path.glob(".*.tmp")) == []


@pytest.mark.asyncio
async def test_retrieval_quality_artifact_from_two_independent_databases(
    tmp_path, configs_dir, integration_redis,
):
    fixture = load_fixture()
    redis = integration_redis
    runs = []
    databases = []
    try:
        for index in (1, 2):
            await redis.r.flushdb()
            db, _config, storage = await ingest_fixture(
                fixture,
                redis,
                data_dir=tmp_path / f"run-{index}",
                configs_dir=configs_dir,
            )
            databases.append(db)
            runs.append(await evaluate_rankings(fixture, db, storage))
        from tests.integration.retrieval_quality import inspect_ingestion

        ingestion = inspect_ingestion(fixture, databases[0])
        main_sha = os.environ["RETRIEVAL_QUALITY_MAIN_SHA"]
        artifact = build_artifact(
            fixture, runs[0], runs[1], ingestion, main_sha=main_sha,
        )
        target = Path(os.environ["INTEGRATION_ARTIFACT_DIR"]) / "retrieval-quality.json"
        atomic_write_json(target, artifact)

        assert artifact["main_sha"] == main_sha
        assert artifact["ranking_digests"][0] == artifact["ranking_digests"][1]
        assert artifact["decision_evidence_gate"]["passed"] is True, {
            "failed": {
                key: passed
                for key, passed in artifact["decision_evidence_gate"]["checks"].items()
                if not passed
            },
            "exact": {
                surface: artifact["metrics"]["surfaces"][surface]["strata"]["exact"]
                for surface in ("search", "mcp", "ask")
            },
            "ask_exact_misses": [
                {
                    "id": record["id"],
                    "truth": [
                        (
                            row["job_id"], row["note_type"],
                            row["artifact_sha256"][:8], row["body_sha256"][:8],
                        )
                        for row in record["truth"]
                    ],
                    "same_job_results": [
                        (
                            row["job_id"], row["note_type"],
                            row["artifact_sha256"][:8], row["body_sha256"][:8],
                        )
                        for row in record["surfaces"]["ask"]["results"]
                        if row["job_id"] in {
                            truth["job_id"] for truth in record["truth"]
                        }
                    ],
                }
                for record in artifact["queries"]
                if record["stratum"] == "exact"
                and _query_score(record, "ask", 10)[2]
            ],
        }
        assert artifact["vector_decision"]["reason"] in {
            "fts5_meets_declared_thresholds",
            "semantic_quality_below_threshold_after_known_fixes",
        }
        for surface in ("search", "mcp", "ask"):
            metrics = artifact["metrics"]["surfaces"][surface]
            assert metrics["by_language"]["zh"]["n"] == 32
            assert metrics["by_language"]["en"]["n"] == 32
            assert metrics["by_direction"]["zh_to_en"]["n"] == 10
            assert metrics["by_direction"]["en_to_zh"]["n"] == 10
            assert all(
                metrics["by_content_type"][kind]["n"] == 16
                for kind in ("video", "paper", "article", "audio")
            )
            for stratum in (
                "exact", "paraphrase", "synonym", "cross_language", "cross_source",
            ):
                row = metrics["strata"][stratum]
                assert all(f"recall_at_{k}" in row for k in (1, 3, 5, 10))
                assert "mrr_at_10" in row and "relevant_no_hit" in row
            assert metrics["latency_ms"]["warmup_rounds"] == 5
            assert metrics["latency_ms"]["measured_rounds"] == 30
        assert set(artifact["metrics"]["latencies"]) == {
            "fts_engine", "search_api", "mcp", "ask",
        }
        citation = artifact["metrics"]["surfaces"]["ask"]["citation"]
        assert citation["evaluated_cases"] == 8
        assert citation["failed_cases"] == 0
        assert citation["coverage"] >= 0.8
        assert (
            artifact["metrics"]["surfaces"]["ask"]
            ["retrieval_evidence_coverage_at_8"]
            >= 0.0
        )
        assert json.loads(target.read_text(encoding="utf-8"))["main_sha"] == main_sha
    finally:
        for db in databases:
            db.close()
