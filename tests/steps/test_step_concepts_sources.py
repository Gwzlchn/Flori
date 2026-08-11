"""通用 concepts 的来源、身份与同次执行快照。"""

from __future__ import annotations

import hashlib
import json

import pytest

from shared.errors import InputInvalidError
from shared.note_text import markdown_to_index_text
from shared.provenance import (
    EXACT_QUOTE_POLICY,
    build_provenance_manifest,
    build_source_manifest,
    canonical_json_bytes,
)
from steps.common.step_concepts import ConceptsStep
from tests.steps.conftest import make_job_dir, make_step_config


def _job(tmp_path, pipeline: str):
    job = make_job_dir(tmp_path, "intermediate", "output", "output/versions")
    (job / "job.json").write_text(
        json.dumps({"pipeline": pipeline}), encoding="utf-8",
    )
    return job


def _smart(job, text="SMART SOURCE", *, anchor: str | None = None):
    path = job / "output" / "versions" / "notes_smart_claude-cli_x_20260101-000000.md"
    path.write_text(text, encoding="utf-8")
    raw = text.encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    segment_id = "blk_concept-source.1"
    locator = {
        "kind": "text", "exact": text, "prefix": "", "suffix": "",
        "dom_path": None,
    }
    job_payload = (
        json.loads((job / "job.json").read_text())
        if (job / "job.json").is_file() else {}
    )
    source = build_source_manifest(
        job_id=job.name,
        pipeline=job_payload.get("pipeline") or "document",
        source_artifacts=[{
            "source_id": "source", "path": "input/source.html", "sha256": digest,
            "revision": None, "media_duration_ms": None, "page_count": None,
        }],
        segments=[{
            "segment_id": segment_id, "source_id": "source", "start": 0,
            "end": len(raw), "section": "smart", "locator": locator,
            "support_text": text,
            "support_artifact": {
                "kind": "html", "path": "input/source.html", "sha256": digest,
                "selector": {"start": 0, "end": len(raw)},
            },
        }],
    )
    rel = path.relative_to(job).as_posix()
    provenance = build_provenance_manifest(
        job_id=job.name,
        note_type="smart",
        note_artifact=rel,
        note_bytes=raw,
        normalized_body=markdown_to_index_text(text),
        source_manifest_path="intermediate/source_segments.json",
        source_manifest=source,
        segments=[] if anchor is None else [{
            "anchor": anchor, "prefix": "", "suffix": "", "section": "smart",
            "source_segment_ids": [segment_id],
            "verification_policy": EXACT_QUOTE_POLICY,
        }],
    )
    (job / "intermediate" / "source_segments.json").write_bytes(
        canonical_json_bytes(source),
    )
    provenance_path = job / "output" / "provenance" / "smart.json"
    provenance_path.parent.mkdir(parents=True, exist_ok=True)
    provenance_path.write_bytes(canonical_json_bytes(provenance))
    return path


@pytest.mark.parametrize(
    ("pipeline", "step_name"),
    [("video", "12_concepts"), ("audio", "05_concepts")],
)
def test_video_audio_require_only_smart_note(tmp_path, pipeline, step_name):
    job = _job(tmp_path, pipeline)
    (job / "intermediate" / "sections.json").write_text(
        '{"title":"must not be used","sections":[]}', encoding="utf-8",
    )
    cfg = make_step_config(tmp_path, step_name=step_name, pool="ai", pipeline=pipeline)
    if pipeline == "video":
        cfg["step"]["prompt_template"] = "05_concepts"
    step = ConceptsStep(step_name, job, cfg)
    assert step.validate_inputs() == ["output/versions/notes_smart_*.md"]

    _smart(job)
    step = ConceptsStep(step_name, job, cfg)
    assert step.validate_inputs() == []
    assert step._resolve_concept_source().kind == "smart_note"


@pytest.mark.parametrize(
    ("pipeline", "expected_path"),
    [
        ("video", "output/versions/notes_smart_"),
        ("audio", "output/versions/notes_smart_"),
    ],
)
def test_common_pipelines_record_selected_note_identity(
    tmp_path, monkeypatch, pipeline, expected_path,
):
    job = _job(tmp_path, pipeline)
    _smart(job)
    cfg = make_step_config(tmp_path, step_name="05_concepts", pool="ai", pipeline=pipeline)
    step = ConceptsStep("05_concepts", job, cfg)

    source = step._resolve_concept_source()
    monkeypatch.setattr(
        step.ai,
        "call_json",
        lambda *args, **kwargs: ({"summary": "s", "key_terms": [{"term": "T"}]}, False),
    )
    result = step.execute()
    output = json.loads((job / "output/concepts.json").read_text(encoding="utf-8"))

    assert source.kind == "smart_note"
    assert source.note_type == "smart"
    assert source.path.startswith(expected_path)
    assert result["evidence_note_type"] == "smart"
    assert output["evidence_note_type"] == "smart"
    assert output["key_terms"][0]["evidence_source_segment_ids"] == []


def test_concepts_validate_hash_execute_share_one_source_snapshot(tmp_path, monkeypatch):
    job = _job(tmp_path, "video")
    path = _smart(job, "FIRST SMART")
    cfg = make_step_config(tmp_path, step_name="12_concepts", pool="ai", pipeline="video")
    cfg["step"]["prompt_template"] = "05_concepts"
    step = ConceptsStep("12_concepts", job, cfg)

    assert step.validate_inputs() == []
    hashes = step.input_hashes()
    expected = "sha256:" + hashlib.sha256(b"FIRST SMART").hexdigest()
    assert hashes["source_hash"] == expected
    path.write_text("SECOND SMART", encoding="utf-8")

    captured = {}

    def fake_call(prompt, **kwargs):
        captured["prompt"] = prompt
        return {"summary": "", "key_terms": []}, False

    monkeypatch.setattr(step.ai, "call_json", fake_call)
    result = step.execute()
    assert "FIRST SMART" in captured["prompt"]
    assert "SECOND SMART" not in captured["prompt"]
    assert result["source"] == "smart_note"


def test_empty_provenance_rejects_invalid_term_shape_without_publishing(
    tmp_path, monkeypatch,
):
    job = _job(tmp_path, "document")
    _smart(job)
    cfg = make_step_config(
        tmp_path, step_name="07_concepts", pool="ai", pipeline="document",
    )
    cfg["step"]["prompt_template"] = "05_concepts"
    step = ConceptsStep("07_concepts", job, cfg)
    monkeypatch.setattr(
        step.ai,
        "call_json",
        lambda *args, **kwargs: ({
            "summary": "",
            "key_terms": [{"term": "T", "definition": {"bad": True}}],
        }, False),
    )

    with pytest.raises(InputInvalidError, match="invalid structure"):
        step.execute()

    assert not (job / "output" / "concepts.json").exists()


def test_video_runtime_override_targets_12_concepts_with_05_template(tmp_path):
    job = _job(tmp_path, "video")
    _smart(job)
    (job / "job.json").write_text(json.dumps({
        "pipeline": "video",
        "prompt_overrides": {
            "12_concepts": {"content": "VIDEO TEMPLATE <<BODY>>", "version": 4},
        },
    }), encoding="utf-8")
    cfg = make_step_config(tmp_path, step_name="12_concepts", pool="ai", pipeline="video")
    cfg["step"]["prompt_template"] = "05_concepts"
    step = ConceptsStep("12_concepts", job, cfg)
    resolved = step.ai.resolve_prompt_template("05_concepts")
    assert resolved.text == "VIDEO TEMPLATE <<BODY>>"
    assert resolved.version == 4
    assert resolved.source == "override"


@pytest.mark.parametrize("pipeline", [None, "unknown"])
def test_concepts_missing_or_unknown_pipeline_fails_closed(tmp_path, pipeline):
    job = make_job_dir(tmp_path, "intermediate", "output", "output/versions")
    cfg = make_step_config(tmp_path, step_name="05_concepts", pool="ai", pipeline="document")
    if pipeline is None:
        cfg["step"].pop("pipeline")
        (job / "job.json").write_text("{}", encoding="utf-8")
    else:
        cfg["step"]["pipeline"] = pipeline
    step = ConceptsStep("05_concepts", job, cfg)
    with pytest.raises(InputInvalidError, match="pipeline"):
        step.validate_inputs()


def test_unknown_pipeline_with_smart_note_still_fails_closed(tmp_path):
    job = make_job_dir(tmp_path, "intermediate", "output", "output/versions")
    _smart(job)
    cfg = make_step_config(
        tmp_path, step_name="05_concepts", pool="ai", pipeline="unknown",
    )
    step = ConceptsStep("05_concepts", job, cfg)
    with pytest.raises(InputInvalidError, match="pipeline"):
        step.validate_inputs()


def test_a20_nonempty_provenance_retries_exact_feedback_then_binds(
    tmp_path, monkeypatch,
):
    job = _job(tmp_path, "document")
    anchor = "流水线并行：层被分片到多设备，各设备处理不同的微批次。"
    _smart(job, anchor, anchor=anchor)
    cfg = make_step_config(
        tmp_path, step_name="07_concepts", pool="ai", pipeline="document",
    )
    cfg["step"]["prompt_template"] = "05_concepts"
    step = ConceptsStep("07_concepts", job, cfg)
    prompts = []
    responses = iter([
        ({
            "summary": "bad",
            "key_terms": [{
                "term": "流水线模型并行", "zh_name": None,
                "evidence_source_segment_ids": ["forged"],
            }],
        }, False),
        ({
            "summary": "ok",
            "key_terms": [{"term": "流水线并行", "zh_name": None}],
        }, False),
    ])

    def fake_call(prompt, **kwargs):
        prompts.append(prompt)
        return next(responses)

    monkeypatch.setattr(step.ai, "call_json", fake_call)

    result = step.execute()
    output = json.loads((job / "output/concepts.json").read_text(encoding="utf-8"))

    assert result["concepts"] == 1
    assert len(prompts) == 2
    assert '"error":"concept_evidence_binding_required"' in prompts[1]
    assert output["key_terms"][0]["evidence_source_segment_ids"] == [
        "blk_concept-source.1",
    ]


@pytest.mark.parametrize(
    "second",
    [
        ({"summary": "", "key_terms": []}, False),
        ({"summary": "", "key_terms": [{"term": "Parallelism"}]}, False),
        ({"summary": "", "key_terms": []}, True),
        ({
            "summary": "",
            "key_terms": [{"term": "Transformer"}] * 65,
        }, False),
        ({
            "summary": "",
            "key_terms": [{
                "term": "Transformer",
                "related": [
                    {"term": "Attention", "rel": "related"},
                    {"term": "Attention", "rel": "related"},
                ],
            }],
        }, False),
        ({"summary": "", "key_terms": [{"term": "T" * 257}]}, False),
        ({
            "summary": "",
            "key_terms": [{"term": "Transformer", "definition": {"bad": True}}],
        }, False),
    ],
)
def test_nonempty_provenance_second_invalid_result_does_not_publish(
    tmp_path, monkeypatch, second,
):
    job = _job(tmp_path, "document")
    anchor = "Transformer 使用注意力机制。"
    _smart(job, anchor, anchor=anchor)
    cfg = make_step_config(
        tmp_path, step_name="07_concepts", pool="ai", pipeline="document",
    )
    cfg["step"]["prompt_template"] = "05_concepts"
    step = ConceptsStep("07_concepts", job, cfg)
    responses = iter([
        ({"summary": "", "key_terms": []}, False),
        second,
    ])
    monkeypatch.setattr(step.ai, "call_json", lambda *args, **kwargs: next(responses))

    with pytest.raises(InputInvalidError, match="no evidence-bound"):
        step.execute()

    assert not (job / "output" / "concepts.json").exists()


def test_second_partial_binding_publishes_only_the_evidence_bound_subset(
    tmp_path, monkeypatch,
):
    job = _job(tmp_path, "document")
    anchor = "Transformer 使用注意力机制。"
    _smart(job, anchor, anchor=anchor)
    cfg = make_step_config(
        tmp_path, step_name="07_concepts", pool="ai", pipeline="document",
    )
    cfg["step"]["prompt_template"] = "05_concepts"
    step = ConceptsStep("07_concepts", job, cfg)
    partial = {
        "summary": "保留有来源的概念",
        "key_terms": [
            {
                "term": "Transformer", "definition": "bound",
                "related": [{"term": "Parallelism", "rel": "related"}],
            },
            {"term": "Parallelism", "definition": "unbound"},
        ],
    }
    responses = iter([
        ({"summary": "", "key_terms": []}, False),
        (partial, False),
    ])
    monkeypatch.setattr(step.ai, "call_json", lambda *args, **kwargs: next(responses))

    result = step.execute()
    output = json.loads((job / "output/concepts.json").read_text())

    assert result["concepts"] == 1
    assert [item["term"] for item in output["key_terms"]] == ["Transformer"]
    assert output["key_terms"][0]["related"] == []
    assert output["key_terms"][0]["evidence_source_segment_ids"] == [
        "blk_concept-source.1",
    ]


def test_nonempty_provenance_retry_provider_failure_does_not_publish(
    tmp_path, monkeypatch,
):
    job = _job(tmp_path, "document")
    anchor = "Transformer 使用注意力机制。"
    _smart(job, anchor, anchor=anchor)
    cfg = make_step_config(
        tmp_path, step_name="07_concepts", pool="ai", pipeline="document",
    )
    cfg["step"]["prompt_template"] = "05_concepts"
    step = ConceptsStep("07_concepts", job, cfg)
    calls = 0

    def fake_call(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"summary": "", "key_terms": []}, False
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(step.ai, "call_json", fake_call)

    with pytest.raises(RuntimeError, match="provider unavailable"):
        step.execute()

    assert calls == 2
    assert not (job / "output" / "concepts.json").exists()
