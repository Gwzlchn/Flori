"""四类 concepts 的来源,身份与同次执行快照."""

from __future__ import annotations

import hashlib
import json

import pytest

from shared.errors import InputInvalidError
from steps.article.step_05_concepts import ArticleConceptsStep
from tests.steps.conftest import make_job_dir, make_step_config


def _job(tmp_path, pipeline: str):
    job = make_job_dir(tmp_path, "intermediate", "output", "output/versions")
    (job / "job.json").write_text(
        json.dumps({"pipeline": pipeline}), encoding="utf-8",
    )
    return job


def _smart(job, text="SMART SOURCE"):
    path = job / "output" / "versions" / "notes_smart_claude-cli_x_20260101-000000.md"
    path.write_text(text, encoding="utf-8")
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
    step = ArticleConceptsStep(step_name, job, cfg)
    assert step.validate_inputs() == ["output/versions/notes_smart_*.md"]

    _smart(job)
    step = ArticleConceptsStep(step_name, job, cfg)
    assert step.validate_inputs() == []
    assert step._resolve_concept_source().kind == "smart_note"


@pytest.mark.parametrize("pipeline", ["article", "paper"])
def test_article_paper_source_priority(tmp_path, pipeline):
    job = _job(tmp_path, pipeline)
    sections = {"title": "ORIGINAL", "sections": [{"title": "S", "text": "ORIGINAL BODY"}]}
    (job / "intermediate" / "sections.json").write_text(json.dumps(sections), encoding="utf-8")
    cfg = make_step_config(tmp_path, step_name="05_concepts", pool="ai", pipeline=pipeline)

    original = ArticleConceptsStep("05_concepts", job, cfg)
    assert original._resolve_concept_source().kind == "original"

    (job / "output" / "translated.md").write_text("TRANSLATED", encoding="utf-8")
    translated = ArticleConceptsStep("05_concepts", job, cfg)
    assert translated._resolve_concept_source().kind == "translation"

    _smart(job)
    smart = ArticleConceptsStep("05_concepts", job, cfg)
    assert smart._resolve_concept_source().kind == "smart_note"


@pytest.mark.parametrize(
    ("pipeline", "fixture", "expected_kind", "expected_note_type", "expected_path"),
    [
        ("video", "smart", "smart_note", "smart", "output/versions/notes_smart_"),
        ("audio", "smart", "smart_note", "smart", "output/versions/notes_smart_"),
        ("article", "original", "original", "original", "output/original.md"),
        ("paper", "translated", "translation", "translated", "output/translated.md"),
    ],
)
def test_four_pipelines_record_selected_note_identity(
    tmp_path, monkeypatch, pipeline, fixture, expected_kind, expected_note_type, expected_path,
):
    job = _job(tmp_path, pipeline)
    if fixture == "smart":
        _smart(job)
    elif fixture == "translated":
        (job / "output/translated.md").write_text("TRANSLATED", encoding="utf-8")
    else:
        (job / "output/original.md").write_text("ORIGINAL", encoding="utf-8")
    cfg = make_step_config(tmp_path, step_name="05_concepts", pool="ai", pipeline=pipeline)
    step = ArticleConceptsStep("05_concepts", job, cfg)

    source = step._resolve_concept_source()
    monkeypatch.setattr(
        step.ai,
        "call_json",
        lambda *args, **kwargs: ({"summary": "s", "key_terms": [{"term": "T"}]}, False),
    )
    result = step.execute()
    output = json.loads((job / "output/concepts.json").read_text(encoding="utf-8"))

    assert source.kind == expected_kind
    assert source.note_type == expected_note_type
    assert source.path.startswith(expected_path)
    assert result["evidence_note_type"] == expected_note_type
    assert output["evidence_note_type"] == expected_note_type
    assert output["key_terms"][0]["evidence_source_segment_ids"] == []


def test_concepts_validate_hash_execute_share_one_source_snapshot(tmp_path, monkeypatch):
    job = _job(tmp_path, "video")
    path = _smart(job, "FIRST SMART")
    cfg = make_step_config(tmp_path, step_name="12_concepts", pool="ai", pipeline="video")
    cfg["step"]["prompt_template"] = "05_concepts"
    step = ArticleConceptsStep("12_concepts", job, cfg)

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
    step = ArticleConceptsStep("12_concepts", job, cfg)
    resolved = step.ai.resolve_prompt_template("05_concepts")
    assert resolved.text == "VIDEO TEMPLATE <<BODY>>"
    assert resolved.version == 4
    assert resolved.source == "override"


@pytest.mark.parametrize("pipeline", [None, "unknown"])
def test_concepts_missing_or_unknown_pipeline_fails_closed(tmp_path, pipeline):
    job = make_job_dir(tmp_path, "intermediate", "output", "output/versions")
    cfg = make_step_config(tmp_path, step_name="05_concepts", pool="ai", pipeline="article")
    if pipeline is None:
        cfg["step"].pop("pipeline")
        (job / "job.json").write_text("{}", encoding="utf-8")
    else:
        cfg["step"]["pipeline"] = pipeline
    step = ArticleConceptsStep("05_concepts", job, cfg)
    with pytest.raises(InputInvalidError, match="pipeline"):
        step.validate_inputs()


def test_unknown_pipeline_with_smart_note_still_fails_closed(tmp_path):
    job = make_job_dir(tmp_path, "intermediate", "output", "output/versions")
    _smart(job)
    cfg = make_step_config(
        tmp_path, step_name="05_concepts", pool="ai", pipeline="unknown",
    )
    step = ArticleConceptsStep("05_concepts", job, cfg)
    with pytest.raises(InputInvalidError, match="pipeline"):
        step.validate_inputs()
