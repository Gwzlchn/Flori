"""评审 v2 严格解析、finish 归一和完整输入门禁。"""

from __future__ import annotations

import json
import copy
from pathlib import Path

import pytest

from shared.models import LLMResponse
from shared.review_contract import (
    MAX_REVIEW_SOURCE_AGGREGATE_BYTES,
    MAX_REVIEW_SOURCE_BYTES,
    MAX_REVIEW_SOURCES,
    completion_from_response,
    parse_review,
    project_review,
    sha256_bytes,
    source_record,
    verify_persisted_review,
)


SCORES = ["accuracy", "completeness"]


def _response(provider="openai", reason="stop", **kwargs):
    kwargs.setdefault("tier_used", "primary")
    kwargs.setdefault("attempts", [{
        "tier": "primary", "provider": provider, "model": "m", "ok": True,
    }])
    return LLMResponse(content="", model="m", provider=provider,
                       finish_reason=reason, **kwargs)


def _payload(**changes):
    data = {
        "accuracy": 5,
        "completeness": 4,
        "key_terms": [{"term": "FTS", "definition": "全文搜索"}],
        "missing_concepts": [],
        "top3_improvements": ["a", "b", "c"],
        "issues": [{
            "type": "traceability", "severity": "warning", "dimension": "accuracy",
            "claim": "笔记中的结论缺少定位", "message": "补定位",
            "evidence_status": "insufficient", "reason": "原文无页码",
        }],
    }
    data.update(changes)
    return json.dumps(data, ensure_ascii=False)


def _input():
    return {"artifact": "output/review_input.md", "sha256": "sha256:x",
            "bytes": 1, "chars": 1, "truncated": False, "sources": []}


@pytest.mark.parametrize(("provider", "reason", "status"), [
    ("anthropic", "end_turn", "complete"),
    ("anthropic", "max_tokens", "truncated"),
    ("openai", "length", "truncated"),
    ("claude-cli", "success", "unknown"),
    ("claude-cli", "max_turns", "unknown"),
    ("codex-cli", "turn.completed", "unknown"),
    ("codex-cli", None, "unknown"),
])
def test_finish_reason_projection(provider, reason, status):
    assert completion_from_response(_response(provider, reason))["status"] == status


def test_claude_error_flag_overrides_success_finish_reason():
    response = _response("claude-cli", "success", raw={"is_error": True})
    completion = completion_from_response(response)
    assert completion["status"] == "error"
    assert completion["schema_version"] == 2
    assert completion["raw_error"] is True


@pytest.mark.parametrize(("provider", "reason", "raw", "status", "raw_error"), [
    ("anthropic", "end_turn", {}, "complete", False),
    ("anthropic", "max_tokens", {}, "truncated", False),
    ("openai", "content_filter", {}, "error", False),
    ("claude-cli", "success", {"is_error": False}, "complete", False),
    ("claude-cli", "max_turns", {"is_error": False}, "truncated", False),
    ("claude-cli", "success", {"is_error": True}, "error", True),
    ("claude-cli", "success", {}, "unknown", None),
    ("claude-cli", "success", {"is_error": 0}, "unknown", None),
    ("codex-cli", "turn.completed", {"errors": []}, "complete", False),
    ("codex-cli", "turn.completed", {"errors": ["boom"]}, "error", True),
    ("codex-cli", "turn.completed", {}, "unknown", None),
    ("codex-cli", "turn.completed", {"errors": "boom"}, "unknown", None),
])
def test_completion_v2_persists_recomputable_terminal_proof(
    provider, reason, raw, status, raw_error,
):
    completion = completion_from_response(_response(provider, reason, raw=raw))
    assert set(completion) == {
        "schema_version", "status", "raw_finish_reason", "raw_error",
        "tier_used", "attempts",
    }
    assert completion["status"] == status
    assert completion["raw_error"] is raw_error


@pytest.mark.parametrize("reason", [False, 0, 1, [], {}, object()])
def test_completion_finish_reason_is_total_and_non_string_is_unknown(reason):
    completion = completion_from_response(
        _response("claude-cli", reason, raw={"is_error": False}),
    )
    assert completion["raw_finish_reason"] is None
    assert completion["status"] == "unknown"
    assert completion["raw_error"] is False


def test_strict_complete_review_is_reliable():
    review, failed = parse_review(_payload(), SCORES, _response(), review_input=_input())
    assert failed is False
    assert review["review_reliable"] is True
    assert review["overall"] == 4.5


def test_supported_issue_requires_exact_quote_from_named_source():
    issue = {
        "type": "consistency", "severity": "error", "dimension": "accuracy",
        "claim": "罚款金额为 123 万元", "message": "核对金额",
        "evidence_status": "supported",
        "locator": {"source": "E1", "quote": "罚款 123 万元"},
    }
    review, failed = parse_review(
        _payload(issues=[issue]), SCORES, _response(), review_input=_input(),
        review_source_texts={"E1": "处罚决定载明罚款 123 万元。"},
    )
    assert failed is False and review["review_reliable"] is True
    assert review["issues"][0]["locator"]["offset"] == 6


@pytest.mark.parametrize("issue", [
    {
        "type": "consistency", "severity": "error", "dimension": "accuracy",
        "claim": "罚款金额", "message": "核对", "evidence_status": "supported",
        "locator": {"source": "E1", "quote": "罚款 123 万元"},
        "reason": "不应与 locator 共存",
    },
    {
        "type": "traceability", "severity": "warning", "dimension": "accuracy",
        "claim": "罚款金额", "message": "缺证据", "evidence_status": "insufficient",
        "reason": "来源不足", "locator": {"source": "E1", "quote": "罚款 123 万元"},
    },
    {
        "type": "traceability", "severity": "warning", "dimension": "accuracy",
        "claim": "罚款金额", "message": "缺证据", "evidence_status": "insufficient",
        "reason": "来源不足", "debug": {"trusted": True},
    },
])
def test_issue_fields_and_locator_reason_are_mutually_exclusive(issue):
    review, failed = parse_review(
        _payload(issues=[issue]), SCORES, _response(), review_input=_input(),
        review_source_texts={"E1": "处罚决定载明罚款 123 万元。"},
    )
    assert failed is True and review["review_reliable"] is False


@pytest.mark.parametrize("extra", [
    {"debug": {"trusted": True}},
    {"key_terms": [{"term": "FTS", "definition": "全文搜索", "trusted": True}]},
])
def test_response_and_key_term_unknown_fields_fail_closed(extra):
    payload = json.loads(_payload())
    payload.update(extra)
    review, failed = parse_review(
        json.dumps(payload, ensure_ascii=False), SCORES, _response(), review_input=_input(),
    )
    assert failed is True and review["review_reliable"] is False


@pytest.mark.parametrize("locator", [
    {"source": "E2", "quote": "罚款 123 万元"},
    {"source": "E1", "quote": "不存在的原文"},
    "第 3 页",
])
def test_forged_or_unstructured_issue_locator_fails_closed(locator):
    issue = {
        "type": "consistency", "severity": "error", "dimension": "accuracy",
        "claim": "罚款金额为 123 万元", "message": "核对金额",
        "evidence_status": "supported", "locator": locator,
    }
    review, failed = parse_review(
        _payload(issues=[issue]), SCORES, _response(), review_input=_input(),
        review_source_texts={"E1": "处罚决定载明罚款 123 万元。"},
    )
    assert failed is True and review["review_reliable"] is False


@pytest.mark.parametrize("value", [True, 4.0, "4", 0, 6, -1])
def test_score_type_and_bounds_fail_closed(value):
    review, failed = parse_review(_payload(accuracy=value), SCORES, _response(),
                                  review_input=_input())
    assert failed is True
    assert review["review_reliable"] is False
    assert review["overall"] is None


def test_empty_key_term_definition_fails_closed():
    review, failed = parse_review(
        _payload(key_terms=[{"term": "FTS", "definition": ""}]),
        SCORES, _response(), review_input=_input(),
    )
    assert failed is True and review["review_reliable"] is False


def test_prose_wrapped_json_is_diagnostic_only():
    review, failed = parse_review("前言\n" + _payload() + "\n结语", SCORES, _response(),
                                  review_input=_input())
    assert failed is True
    assert review["parse"]["mode"] == "extracted"
    assert review["review_reliable"] is False


@pytest.mark.parametrize("response", [
    _response("openai", "length"),
    _response("codex-cli", None),
])
def test_truncated_or_unknown_completion_is_unreliable(response):
    review, _ = parse_review(_payload(), SCORES, response, review_input=_input())
    assert review["review_reliable"] is False
    assert review["completion"]["status"] in {"truncated", "unknown"}


def test_full_source_keeps_tail_and_hash(tmp_path):
    job = tmp_path / "j"
    job.mkdir()
    text = "头" + ("中" * 25_000) + "TAIL-SENTINEL"
    (job / "note.md").write_text(text, encoding="utf-8")
    loaded, record = source_record(job, "note.md", label="note")
    assert loaded.endswith("TAIL-SENTINEL")
    assert record["chars"] == len(text) and record["truncated"] is False
    assert record["sha256"].startswith("sha256:")


def test_source_over_defensive_limit_fails_instead_of_slicing(tmp_path, monkeypatch):
    from shared import review_contract

    job = tmp_path / "j"
    job.mkdir()
    (job / "note.md").write_text("abcdef", encoding="utf-8")
    monkeypatch.setattr(review_contract, "MAX_REVIEW_SOURCE_BYTES", 5)
    with pytest.raises(ValueError, match="exceeds"):
        source_record(job, "note.md", label="note")


def test_source_record_does_not_use_unbounded_path_read_bytes(tmp_path, monkeypatch):
    job = tmp_path / "j"
    job.mkdir()
    (job / "note.md").write_text("bounded", encoding="utf-8")

    def reject_unbounded_read(_path):
        raise AssertionError("source_record must use a limit+1 file read")

    monkeypatch.setattr(Path, "read_bytes", reject_unbounded_read)
    loaded, record = source_record(job, "note.md", label="note")
    assert loaded == "bounded"
    assert record["bytes"] == len(b"bounded")


def test_empty_review_source_fails_explicitly(tmp_path):
    job = tmp_path / "j"
    job.mkdir()
    (job / "note.md").write_text("  \n", encoding="utf-8")
    with pytest.raises(ValueError, match="empty"):
        source_record(job, "note.md", label="note")


def test_api_projection_has_three_states():
    assert project_review({"schema_version": 2, "review_reliable": True, "overall": 5})["reliability_state"] == "reliable"
    bad = project_review({"schema_version": 2, "review_reliable": False, "overall": 2, "key_terms": ["x"]})
    assert bad["reliability_state"] == "unreliable" and bad["overall"] is None and bad["key_terms"] == []
    old = project_review({"overall": 4})
    assert old["reliability_state"] == "legacy_unverified" and old["overall"] is None


@pytest.mark.parametrize("shape", [True, False, 1, 1.5, "x", {}, None])
@pytest.mark.parametrize("schema_version", [None, 2])
def test_projection_is_total_for_legacy_and_downgraded_v2_collection_shapes(
    shape, schema_version,
):
    review = {
        "schema_version": schema_version,
        "review_reliable": False,
        "overall": 4.5,
        "reliability_reasons": shape,
        "review_input": shape,
        "issues": shape,
        "missing_concepts": shape,
        "top3_improvements": shape,
        "key_terms": shape,
        "debug": {"artifact": "../../secret"},
    }

    projected = project_review(review)

    assert projected["reliability_state"] == (
        "unreliable" if schema_version == 2 else "legacy_unverified"
    )
    assert projected["review_reliable"] is False
    assert projected["overall"] is None
    assert projected["key_terms"] == []
    assert isinstance(projected["reliability_reasons"], list)
    assert isinstance(projected["missing_concepts"], list)
    assert isinstance(projected["top3_improvements"], list)
    assert isinstance(projected["issues"], list)
    assert isinstance(projected["review_input"], dict)
    assert isinstance(projected["review_input"]["sources"], list)
    assert "debug" not in projected


@pytest.mark.parametrize("schema_version", [1, 2])
def test_unverified_projection_keeps_text_diagnostics_but_strips_artifact_locators(
    schema_version,
):
    projected = project_review({
        "schema_version": schema_version,
        "review_reliable": False,
        "overall": 4.2,
        "reliability_reasons": ["artifact_tampered"],
        "missing_concepts": ["事务边界", 1],
        "top3_improvements": ["补充回滚证据", {"unsafe": True}],
        "issues": [{
            "type": "traceability", "severity": "warning", "dimension": "accuracy",
            "claim": "金额需核验", "message": "缺少可信定位",
            "evidence_status": "supported",
            "locator": {"source": "E1", "quote": "罚款 100 万元", "offset": 2},
        }],
        "review_input": {"sources": [{
            "label": "E1", "artifact": "output/evidence/evidence-01.md",
            "sha256": "sha256:forged", "bytes": 1, "chars": 1, "truncated": False,
        }]},
        "note_file": "output/versions/notes_smart_forged.md",
    })

    assert projected.get("diagnostic_overall") is None
    assert projected["missing_concepts"] == ["事务边界"]
    assert projected["top3_improvements"] == ["补充回滚证据"]
    assert projected["issues"][0]["message"] == "缺少可信定位"
    assert projected["issues"][0]["locator"] is None
    assert projected["review_input"]["sources"][0]["artifact"] is None
    assert projected["note_file"] is None


def test_reliable_projection_retains_only_typed_verified_locator_and_artifact():
    projected = project_review({
        "schema_version": 2, "review_reliable": True, "reliability_reasons": [],
        "score_keys": ["accuracy"], "accuracy": 5, "overall": 5,
        "key_terms": [{"term": "FTS", "definition": "全文检索"}],
        "issues": [{
            "type": "traceability", "severity": "info", "dimension": "accuracy",
            "claim": "已定位", "message": "已定位", "evidence_status": "supported",
            "locator": {"source": "smart", "quote": "精确原文", "offset": 3},
        }],
        "review_input": {"sources": [{
            "label": "smart", "artifact": "output/versions/notes_smart_x.md",
            "sha256": "sha256:x", "bytes": 10, "chars": 10, "truncated": False,
        }]},
        "note_file": "output/versions/notes_smart_x.md",
    })

    assert projected["reliability_state"] == "reliable"
    assert projected["accuracy"] == 5 and projected["overall"] == 5
    assert projected["issues"][0]["locator"] == {
        "source": "smart", "quote": "精确原文", "offset": 3,
    }
    assert projected["review_input"]["sources"][0]["artifact"] == (
        "output/versions/notes_smart_x.md"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [
    {"review_reliable": True, "key_terms": [{"term": "forged", "definition": "x"}]},
    {"schema_version": 1, "review_reliable": True,
     "key_terms": [{"term": "forged", "definition": "x"}]},
])
async def test_persisted_legacy_self_report_is_forced_unreliable(legacy):
    async def reader(_rel):
        raise AssertionError("legacy review must not read artifacts")

    verified = await verify_persisted_review(
        legacy, job_id="job", pipeline="article", read_file=reader,
    )
    assert verified["review_reliable"] is False
    assert verified["reliability_reasons"] == ["legacy_schema"]


def _persisted_review(job):
    (job / "output/versions").mkdir(parents=True)
    note_rel = "output/versions/notes_smart_openai_m_20260101-000000.md"
    (job / note_rel).write_text("# 笔记\n精确原文", encoding="utf-8")
    smart, smart_record = source_record(job, note_rel, label="smart")
    (job / "intermediate").mkdir()
    document_rel = "intermediate/document.json"
    quality_rel = "intermediate/quality.json"
    (job / document_rel).write_text(
        '{"schema_version":2,"job_id":"job","content_type":"document"}',
        encoding="utf-8",
    )
    (job / quality_rel).write_text(
        '{"schema_version":1,"job_id":"job","status":"complete","reasons":[]}',
        encoding="utf-8",
    )
    document, document_record = source_record(job, document_rel, label="document")
    quality, quality_record = source_record(job, quality_rel, label="quality")
    prompt_rel = "output/versions/review_input_openai_m_20260101-000000.md"
    (job / prompt_rel).write_text(
        "prompt\n" + smart + document + quality, encoding="utf-8",
    )
    _, prompt_record = source_record(job, prompt_rel, label="prompt")
    prompt_record.pop("label")
    prompt_record["sources"] = [smart_record, document_record, quality_record]
    issue = {
        "type": "traceability", "severity": "warning", "dimension": "accuracy",
        "claim": "原文可定位", "message": "已定位", "evidence_status": "supported",
        "locator": {"source": "smart", "quote": "精确原文"},
    }
    score_keys = [
        "completeness", "accuracy", "structure", "terminology",
        "formula_integrity", "visual_references", "traceability",
    ]
    raw = json.dumps({
        **{key: 5 for key in score_keys},
        "key_terms": [{"term": "FTS", "definition": "全文搜索"}],
        "missing_concepts": [], "top3_improvements": ["a", "b", "c"],
        "issues": [issue],
    }, ensure_ascii=False)
    review, _ = parse_review(
        raw, score_keys, _response(), review_input=prompt_record,
        review_source_texts={
            "smart": smart, "document": document, "quality": quality,
        },
    )
    review.update({
        "note_file": note_rel, "provider": "openai", "model": "m",
        "generated_at": "2026/07/14 12:00:00",
        "review_coverage": {
            "note_chars": len(smart), "reviewed_chars": len(smart), "truncated": False,
        },
    })
    return review


@pytest.mark.asyncio
async def test_persisted_review_verifier_accepts_complete_bound_artifact(tmp_path):
    job = tmp_path / "job"; job.mkdir()
    review = _persisted_review(job)

    async def reader(rel):
        path = job / rel
        return path.read_bytes() if path.exists() else None

    verified = await verify_persisted_review(
        review, job_id="job", pipeline="document", read_file=reader,
    )
    assert verified["review_reliable"] is True


@pytest.mark.asyncio
async def test_persisted_review_uses_one_snapshot_per_artifact(tmp_path):
    job = tmp_path / "job"; job.mkdir()
    review = _persisted_review(job)
    calls: dict[str, int] = {}

    async def reader(rel):
        calls[rel] = calls.get(rel, 0) + 1
        if calls[rel] > 1:
            raise OSError("second read observes a different artifact")
        path = job / rel
        return path.read_bytes() if path.exists() else None

    verified = await verify_persisted_review(
        review, job_id="job", pipeline="document", read_file=reader,
    )

    assert verified["review_reliable"] is True
    assert calls
    assert set(calls.values()) == {1}


@pytest.mark.asyncio
async def test_persisted_video_review_read_error_downgrades_instead_of_escaping(tmp_path):
    job = tmp_path / "job"; job.mkdir()
    review = _persisted_review(job)
    calls = 0

    async def reader(rel):
        nonlocal calls
        if rel == "output/evidence.json":
            calls += 1
            raise OSError("bounded reader rejected an unsafe artifact")
        path = job / rel
        return path.read_bytes() if path.exists() else None

    verified = await verify_persisted_review(
        review, job_id="job", pipeline="video", read_file=reader,
    )

    assert verified["review_reliable"] is False
    assert "evidence_manifest_unreadable" in verified["reliability_reasons"]
    assert calls == 1


def _source_record_for_test(label, artifact, data, *, declared_bytes=None):
    return {
        "label": label,
        "artifact": artifact,
        "sha256": sha256_bytes(data),
        "bytes": len(data) if declared_bytes is None else declared_bytes,
        "chars": len(data.decode("utf-8")),
        "truncated": False,
    }


@pytest.mark.asyncio
async def test_review_source_count_preflight_stops_all_artifact_reads(tmp_path):
    job = tmp_path / "job"; job.mkdir()
    review = _persisted_review(job)
    review["review_input"]["sources"] = [
        _source_record_for_test(f"S{i}", f"output/review_sources/s{i}.md", b"x")
        for i in range(MAX_REVIEW_SOURCES + 1)
    ]
    calls = []

    async def reader(rel):
        calls.append(rel)
        raise AssertionError("source envelope must fail before any read")

    verified = await verify_persisted_review(
        review, job_id="job", pipeline="document", read_file=reader,
    )
    assert verified["review_reliable"] is False
    assert "review_sources_too_many" in verified["reliability_reasons"]
    assert calls == []


@pytest.mark.asyncio
async def test_review_declared_source_aggregate_stops_all_artifact_reads(tmp_path):
    job = tmp_path / "job"; job.mkdir()
    review = _persisted_review(job)
    half = MAX_REVIEW_SOURCE_AGGREGATE_BYTES // 2 + 1
    review["review_input"]["sources"] = [
        _source_record_for_test("S1", "output/review_sources/s1.md", b"x", declared_bytes=half),
        _source_record_for_test("S2", "output/review_sources/s2.md", b"x", declared_bytes=half),
    ]
    calls = []

    async def reader(rel):
        calls.append(rel)
        raise AssertionError("declared aggregate must fail before any read")

    verified = await verify_persisted_review(
        review, job_id="job", pipeline="document", read_file=reader,
    )
    assert "review_sources_declared_too_large" in verified["reliability_reasons"]
    assert calls == []


@pytest.mark.asyncio
async def test_review_actual_source_aggregate_stops_before_next_artifact(tmp_path):
    job = tmp_path / "job"; job.mkdir()
    review = _persisted_review(job)
    first = b"a" * (5 * 1024 * 1024)
    second_declared = 3 * 1024 * 1024
    second = b"b" * (second_declared + 1)
    third = b"c"
    records = [
        _source_record_for_test("S1", "output/review_sources/s1.md", first),
        _source_record_for_test(
            "S2", "output/review_sources/s2.md", second,
            declared_bytes=second_declared,
        ),
        _source_record_for_test(
            "S3", "output/review_sources/s3.md", third, declared_bytes=0,
        ),
    ]
    review["review_input"]["sources"] = records
    prompt_rel = review["review_input"]["artifact"]
    prompt = (job / prompt_rel).read_bytes()
    bodies = {record["artifact"]: body for record, body in zip(records, [first, second, third])}
    calls = []

    async def reader(rel):
        calls.append(rel)
        if rel == prompt_rel:
            return prompt
        return bodies.get(rel)

    verified = await verify_persisted_review(
        review, job_id="job", pipeline="document", read_file=reader,
    )
    assert "review_sources_actual_too_large" in verified["reliability_reasons"]
    assert calls == [prompt_rel, records[0]["artifact"], records[1]["artifact"]]


@pytest.mark.asyncio
async def test_review_accepts_fourteen_source_envelopes_before_profile_checks(tmp_path):
    job = tmp_path / "job"; job.mkdir()
    review = _persisted_review(job)
    body = b"x"
    records = [
        _source_record_for_test(f"S{i}", f"output/review_sources/s{i}.md", body)
        for i in range(MAX_REVIEW_SOURCES)
    ]
    review["review_input"]["sources"] = records
    prompt_rel = review["review_input"]["artifact"]
    prompt = (job / prompt_rel).read_bytes()
    calls = []

    async def reader(rel):
        calls.append(rel)
        if rel == prompt_rel:
            return prompt
        if rel in {record["artifact"] for record in records}:
            return body
        return None

    verified = await verify_persisted_review(
        review, job_id="job", pipeline="document", read_file=reader,
    )
    assert verified["review_reliable"] is False
    assert "review_sources_too_many" not in verified["reliability_reasons"]
    assert all(record["artifact"] in calls for record in records)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("pipeline", "reason"),
    [("video", "score_profile_mismatch"), (None, "review_pipeline_unknown")],
)
async def test_persisted_review_profile_uses_trusted_pipeline(tmp_path, pipeline, reason):
    job = tmp_path / "job"; job.mkdir()
    review = _persisted_review(job)

    async def reader(rel):
        path = job / rel
        return path.read_bytes() if path.exists() else None

    verified = await verify_persisted_review(
        review, job_id="job", pipeline=pipeline, read_file=reader,
    )

    assert verified["review_reliable"] is False
    assert reason in verified["reliability_reasons"]


@pytest.mark.asyncio
async def test_persisted_review_accepts_canonical_gateway_attempts(tmp_path):
    job = tmp_path / "job"; job.mkdir()
    review = _persisted_review(job)
    review["completion"]["tier_used"] = "fallback"
    review["completion"]["attempts"] = [
        {
            "tier": "primary", "provider": "openai", "model": "m1", "ok": False,
            "error_class": "AIProviderError", "error": "down",
        },
        {"tier": "fallback", "provider": "openai", "model": "m2", "ok": True},
    ]
    review["model"] = "m2"

    async def reader(rel):
        path = job / rel
        return path.read_bytes() if path.exists() else None

    verified = await verify_persisted_review(
        review, job_id="job", pipeline="document", read_file=reader,
    )
    assert verified["review_reliable"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(("provider", "reason"), [
    ("anthropic", "max_tokens"),
    ("openai", "length"),
    ("openai", "content_filter"),
])
async def test_persisted_completion_status_is_recomputed_from_finish_reason(
    tmp_path, provider, reason,
):
    job = tmp_path / "job"; job.mkdir()
    review = _persisted_review(job)
    review["provider"] = provider
    review["completion"]["attempts"][-1]["provider"] = provider
    review["completion"]["raw_finish_reason"] = reason
    review["completion"]["status"] = "complete"

    async def reader(rel):
        path = job / rel
        return path.read_bytes() if path.exists() else None

    verified = await verify_persisted_review(
        review, job_id="job", pipeline="document", read_file=reader,
    )
    assert verified["review_reliable"] is False
    assert "completion_status_mismatch" in verified["reliability_reasons"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("provider", "reason", "raw_error", "status", "reliable"), [
    ("claude-cli", "success", False, "complete", True),
    ("claude-cli", "success", None, "unknown", False),
    ("codex-cli", "turn.completed", False, "complete", True),
    ("codex-cli", "turn.completed", None, "unknown", False),
])
async def test_persisted_cli_completion_recomputes_only_explicit_raw_proof(
    tmp_path, provider, reason, raw_error, status, reliable,
):
    job = tmp_path / "job"; job.mkdir()
    review = _persisted_review(job)
    review["provider"] = provider
    review["completion"]["attempts"][-1]["provider"] = provider
    review["completion"].update({
        "raw_finish_reason": reason,
        "raw_error": raw_error,
        "status": status,
    })

    async def reader(rel):
        path = job / rel
        return path.read_bytes() if path.exists() else None

    verified = await verify_persisted_review(
        review, job_id="job", pipeline="document", read_file=reader,
    )
    assert verified["review_reliable"] is reliable
    if not reliable:
        assert "completion_not_complete" in verified["reliability_reasons"]


@pytest.mark.asyncio
async def test_persisted_review_reapplies_source_size_gate_before_decode_or_hash(tmp_path):
    job = tmp_path / "job"; job.mkdir()
    review = _persisted_review(job)
    review["issues"] = []
    oversized = b"x" * (MAX_REVIEW_SOURCE_BYTES + 1)
    smart_record = review["review_input"]["sources"][0]
    (job / smart_record["artifact"]).write_bytes(oversized)
    smart_record.update({
        "sha256": sha256_bytes(oversized), "bytes": len(oversized),
        "chars": len(oversized),
    })
    document_record = review["review_input"]["sources"][1]
    document = (job / document_record["artifact"]).read_bytes()
    quality_record = review["review_input"]["sources"][2]
    quality = (job / quality_record["artifact"]).read_bytes()
    prompt = b"prompt\n" + oversized + document + quality
    (job / review["review_input"]["artifact"]).write_bytes(prompt)
    review["review_input"].update({
        "sha256": sha256_bytes(prompt), "bytes": len(prompt), "chars": len(prompt),
    })
    review["review_coverage"] = {
        "note_chars": len(oversized), "reviewed_chars": len(oversized),
        "truncated": False,
    }

    async def reader(rel):
        path = job / rel
        return path.read_bytes() if path.exists() else None

    verified = await verify_persisted_review(
        review, job_id="job", pipeline="document", read_file=reader,
    )
    assert verified["review_reliable"] is False
    assert any("too_large" in reason for reason in verified["reliability_reasons"])


@pytest.mark.asyncio
@pytest.mark.parametrize(("mutation", "reason"), [
    ("empty", "completion_attempts_invalid"),
    ("all_failed", "completion_success_count_invalid"),
    ("double_success", "completion_success_count_invalid"),
    ("success_not_last", "completion_success_not_last"),
    ("tier_mismatch", "completion_tier_mismatch"),
    ("provider_mismatch", "completion_provider_mismatch"),
    ("model_mismatch", "completion_model_mismatch"),
])
async def test_persisted_review_completion_chain_is_bound_to_final_success(
    tmp_path, mutation, reason,
):
    job = tmp_path / "job"; job.mkdir()
    review = _persisted_review(job)
    failure = {
        "tier": "primary", "provider": "openai", "model": "m", "ok": False,
        "error_class": "AIProviderError", "error": "down",
    }
    success = {"tier": "fallback", "provider": "openai", "model": "m", "ok": True}
    review["completion"]["tier_used"] = "fallback"
    review["completion"]["attempts"] = [failure, success]
    if mutation == "empty":
        review["completion"]["attempts"] = []
    elif mutation == "all_failed":
        review["completion"]["attempts"] = [failure]
        review["completion"]["tier_used"] = "primary"
    elif mutation == "double_success":
        review["completion"]["attempts"] = [
            {"tier": "primary", "provider": "openai", "model": "m", "ok": True},
            success,
        ]
    elif mutation == "success_not_last":
        review["completion"]["attempts"] = [success, failure]
    elif mutation == "tier_mismatch":
        review["completion"]["tier_used"] = "primary"
    elif mutation == "provider_mismatch":
        review["provider"] = "deepseek"
    elif mutation == "model_mismatch":
        review["model"] = "other"

    async def reader(rel):
        path = job / rel
        return path.read_bytes() if path.exists() else None

    verified = await verify_persisted_review(
        review, job_id="job", pipeline="document", read_file=reader,
    )

    assert verified["review_reliable"] is False
    assert reason in verified["reliability_reasons"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", [
    "nested_score_keys", "nested_reasons", "nested_issue_type", "schema_float",
    "overall_bool", "missing_metadata", "arbitrary_note_artifact", "coverage_bool",
    "parse_bool", "unknown_provider", "prompt_omits_source", "top_extra",
    "issue_extra", "supported_reason", "insufficient_locator", "source_extra",
    "review_input_extra", "completion_attempt_extra", "completion_non_json",
    "completion_tier_nested", "key_term_extra",
])
async def test_persisted_review_verifier_is_total_and_fail_closed(tmp_path, mutation):
    job = tmp_path / "job"; job.mkdir()
    review = copy.deepcopy(_persisted_review(job))
    if mutation == "nested_score_keys":
        review["score_keys"] = [{}]
    elif mutation == "nested_reasons":
        review["reliability_reasons"] = [{}]
    elif mutation == "nested_issue_type":
        review["issues"][0]["type"] = []
    elif mutation == "schema_float":
        review["schema_version"] = 2.0
    elif mutation == "overall_bool":
        review["overall"] = True
    elif mutation == "missing_metadata":
        review.pop("provider")
    elif mutation == "coverage_bool":
        review["review_coverage"]["note_chars"] = True
    elif mutation == "parse_bool":
        review["parse"]["schema_valid"] = 1
    elif mutation == "unknown_provider":
        review["provider"] = "unknown"
    elif mutation == "prompt_omits_source":
        prompt_path = job / review["review_input"]["artifact"]
        prompt_path.write_text("prompt without source", encoding="utf-8")
        _, prompt_record = source_record(job, review["review_input"]["artifact"], label="prompt")
        prompt_record.pop("label")
        prompt_record["sources"] = review["review_input"]["sources"]
        review["review_input"] = prompt_record
    elif mutation == "top_extra":
        review["debug"] = {"trusted": True}
    elif mutation == "issue_extra":
        review["issues"][0]["debug"] = {"trusted": True}
    elif mutation == "supported_reason":
        review["issues"][0]["reason"] = "与 locator 互斥"
    elif mutation == "insufficient_locator":
        review["issues"][0]["evidence_status"] = "insufficient"
        review["issues"][0]["reason"] = "仍恶意夹带 locator"
    elif mutation == "source_extra":
        review["review_input"]["sources"][0]["debug"] = {"trusted": True}
    elif mutation == "review_input_extra":
        review["review_input"]["debug"] = {"trusted": True}
    elif mutation == "completion_attempt_extra":
        review["completion"]["tier_used"] = "primary"
        review["completion"]["attempts"] = [{
            "tier": "primary", "provider": "openai", "model": "m", "ok": True,
            "debug": {"trusted": True},
        }]
    elif mutation == "completion_non_json":
        review["completion"]["attempts"] = [object()]
    elif mutation == "completion_tier_nested":
        review["completion"]["tier_used"] = []
    elif mutation == "key_term_extra":
        review["key_terms"][0]["trusted"] = True
    else:
        review["note_file"] = review["review_input"]["artifact"]
        review["review_input"]["sources"][0] = {
            **review["review_input"], "label": "smart", "sources": [],
        }

    async def reader(rel):
        path = job / rel
        return path.read_bytes() if path.exists() else None

    verified = await verify_persisted_review(
        review, job_id="job", pipeline="document", read_file=reader,
    )
    projected = project_review(verified)
    assert projected["review_reliable"] is False
    assert projected["reliability_state"] in {"unreliable", "legacy_unverified"}
