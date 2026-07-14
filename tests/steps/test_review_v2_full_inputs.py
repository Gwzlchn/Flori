"""四类真实 review step 的完整输入与可靠结果回归。"""

from __future__ import annotations

import copy
import json
import hashlib

import pytest

from shared.models import LLMResponse
from shared.review_contract import verify_persisted_review
from steps.article.step_05_review import ArticleReviewStep
from steps.audio.step_05_review import PodcastReviewStep
from steps.paper.step_06_review import PaperReviewStep
from steps.video.step_12_review import ReviewStep
from tests.steps.conftest import make_step_config


def _valid(keys, *, source=None, quote=None):
    issues = []
    if source and quote:
        issues = [{
            "type": "traceability", "severity": "warning", "dimension": keys[0],
            "claim": "可追溯主张", "message": "已定位", "evidence_status": "supported",
            "locator": {"source": source, "quote": quote},
        }]
    return json.dumps({
        **{key: 5 for key in keys},
        "key_terms": [{"term": "可靠概念", "definition": "定义"}],
        "missing_concepts": [],
        "top3_improvements": ["a", "b", "c"],
        "issues": issues,
    }, ensure_ascii=False)


def _base_job(tmp_path, smart_tail):
    job = tmp_path / "job"
    job.mkdir()
    for name in ("output", "intermediate", "logs"):
        (job / name).mkdir()
    (job / "output/versions").mkdir()
    smart = "S" * 22_000 + smart_tail
    (job / "output/versions/notes_smart_openai_gpt-4o_20260101-000000.md").write_text(smart)
    return job


def _run(step, keys, tail, *, locator_source=None):
    step.ai.last_response = LLMResponse(
        content="", model="gpt-4o", provider="openai", finish_reason="stop",
        tier_used="primary", attempts=[{
            "tier": "primary", "provider": "openai", "model": "gpt-4o", "ok": True,
        }],
    )
    step.ai.last_provider = "openai"
    step.ai.last_model = "gpt-4o"
    step.ai.call = lambda *_a, **_k: _valid(
        keys, source=locator_source, quote=tail if locator_source else None,
    )
    result = step.execute()
    prompt = (step.job_dir / "output/review_input.md").read_text(encoding="utf-8")
    review = json.loads((step.job_dir / "output/review.json").read_text())
    assert tail in prompt
    assert "SMART-TAIL" in prompt
    assert review["review_reliable"] is True
    assert review["review_input"]["truncated"] is False
    assert all(source["truncated"] is False for source in review["review_input"]["sources"])
    assert result["parse_failed"] is False
    return review


def test_video_review_uses_full_mechanical_and_smart(tmp_path):
    job = _base_job(tmp_path, "SMART-TAIL")
    tail = "VIDEO-MECHANICAL-TAIL"
    (job / "output/notes_mechanical.md").write_text("M" * 10_000 + tail)
    keys = ["completeness", "accuracy", "structure", "terminology", "visual_integration", "readability"]
    step = ReviewStep("12_review", job, make_step_config(tmp_path, step_name="12_review", pool="ai"))
    _run(step, keys, tail, locator_source="mechanical")


@pytest.mark.parametrize(("kind", "cls", "step_name", "tail", "keys"), [
    ("paper", PaperReviewStep, "06_review", "PAPER-SOURCE-TAIL",
     ["completeness", "accuracy", "structure", "terminology", "formula_integrity", "figure_references"]),
    ("article", ArticleReviewStep, "06_review", "ARTICLE-SOURCE-TAIL",
     ["completeness", "accuracy", "structure", "readability", "insight"]),
])
def test_document_reviews_use_full_section_text(tmp_path, kind, cls, step_name, tail, keys):
    job = _base_job(tmp_path, "SMART-TAIL")
    sections = {"sections": [{"title": "正文", "text": "D" * 10_000 + tail}]}
    (job / "intermediate/sections.json").write_text(json.dumps(sections))
    step = cls(step_name, job, make_step_config(tmp_path, step_name=step_name, pool="ai"))
    review = _run(step, keys, tail, locator_source="sections")
    record = next(source for source in review["review_input"]["sources"]
                  if source["label"] == "sections")
    artifact = job / record["artifact"]
    body = artifact.read_bytes()
    text = body.decode("utf-8")
    locator = review["issues"][0]["locator"]
    assert record["artifact"].startswith("output/review_sources/sections-")
    assert record["sha256"] == "sha256:" + hashlib.sha256(body).hexdigest()
    assert record["bytes"] == len(body) and record["chars"] == len(text)
    assert text[locator["offset"]:locator["offset"] + len(locator["quote"])] == locator["quote"]


@pytest.mark.asyncio
async def test_paper_figures_are_content_addressed_and_revalidated_against_current_fact(tmp_path):
    job = _base_job(tmp_path, "SMART-TAIL")
    (job / "intermediate/sections.json").write_text(json.dumps({
        "sections": [{"title": "正文", "text": "PAPER-SOURCE-TAIL"}],
    }))
    figures_path = job / "intermediate/figures.json"
    figures_path.write_text(json.dumps([
        {"index": 1, "caption": "Figure 1", "filename": "fig1.png"},
    ]))
    keys = [
        "completeness", "accuracy", "structure", "terminology",
        "formula_integrity", "figure_references",
    ]
    review = _run(
        PaperReviewStep("06_review", job, make_step_config(
            tmp_path, step_name="06_review", pool="ai",
        )),
        keys, "PAPER-SOURCE-TAIL", locator_source="sections",
    )
    figures_record = next(
        source for source in review["review_input"]["sources"]
        if source["label"] == "figures"
    )
    assert figures_record["artifact"].startswith("output/review_sources/figures-")

    async def reader(rel):
        path = job / rel
        return path.read_bytes() if path.exists() else None

    assert (await verify_persisted_review(
        review, job_id=job.name, pipeline="paper", read_file=reader,
    ))["review_reliable"] is True

    figures_path.write_text(json.dumps([
        {"index": 1, "caption": "forged caption", "filename": "fig1.png"},
    ]))
    verified = await verify_persisted_review(
        review, job_id=job.name, pipeline="paper", read_file=reader,
    )
    assert verified["review_reliable"] is False
    assert "paper_figures_source_mismatch" in verified["reliability_reasons"]


def test_audio_review_uses_full_transcript(tmp_path):
    job = _base_job(tmp_path, "SMART-TAIL")
    tail = "AUDIO-TRANSCRIPT-TAIL"
    transcript = {"segments": [], "full_text": "A" * 10_000 + tail, "duration_sec": 1}
    (job / "intermediate/transcript.json").write_text(json.dumps(transcript))
    keys = ["completeness", "accuracy", "structure", "terminology", "conciseness", "readability"]
    step = PodcastReviewStep("05_review", job, make_step_config(tmp_path, step_name="05_review", pool="ai"))
    review = _run(step, keys, tail, locator_source="transcript")
    record = next(source for source in review["review_input"]["sources"]
                  if source["label"] == "transcript")
    artifact = job / record["artifact"]
    body = artifact.read_bytes()
    locator = review["issues"][0]["locator"]
    assert record["artifact"].startswith("output/review_sources/transcript-")
    assert record["sha256"] == "sha256:" + hashlib.sha256(body).hexdigest()
    assert record["bytes"] == len(body) and record["chars"] == len(body.decode())
    assert body.decode()[locator["offset"]:locator["offset"] + len(locator["quote"])] == locator["quote"]


@pytest.mark.parametrize(("kind", "cls", "step_name", "source_rel", "payload", "keys"), [
    (
        "video", ReviewStep, "12_review", "output/notes_mechanical.md", "video source",
        ["completeness", "accuracy", "structure", "terminology", "visual_integration", "readability"],
    ),
    (
        "paper", PaperReviewStep, "06_review", "intermediate/sections.json",
        json.dumps({"sections": [{"title": "正文", "text": "paper source"}]}),
        ["completeness", "accuracy", "structure", "terminology", "formula_integrity", "figure_references"],
    ),
    (
        "article", ArticleReviewStep, "06_review", "intermediate/sections.json",
        json.dumps({"sections": [{"title": "正文", "text": "article source"}]}),
        ["completeness", "accuracy", "structure", "readability", "insight"],
    ),
    (
        "audio", PodcastReviewStep, "05_review", "intermediate/transcript.json",
        json.dumps({"full_text": "audio source"}),
        ["completeness", "accuracy", "structure", "terminology", "conciseness", "readability"],
    ),
])
def test_review_steps_load_smart_and_source_once(
    tmp_path, monkeypatch, kind, cls, step_name, source_rel, payload, keys,
):
    from shared import review_contract

    job = _base_job(tmp_path, "SMART-TAIL")
    (job / source_rel).write_text(payload, encoding="utf-8")
    calls: list[str] = []
    real_read = review_contract.read_path_bounded

    def counted(path, *args, **kwargs):
        calls.append(str(path))
        return real_read(path, *args, **kwargs)

    monkeypatch.setattr(review_contract, "read_path_bounded", counted)
    step = cls(step_name, job, make_step_config(tmp_path, step_name=step_name, pool="ai"))
    _run(step, keys, payload if kind == "video" else f"{kind} source")

    smart = next((job / "output/versions").glob("notes_smart_*.md"))
    assert calls.count(str(smart)) == 1
    assert calls.count(str(job / source_rel)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(("kind", "cls", "step_name", "source_label"), [
    ("paper", PaperReviewStep, "06_review", "sections"),
    ("article", ArticleReviewStep, "06_review", "sections"),
    ("audio", PodcastReviewStep, "05_review", "transcript"),
])
async def test_persisted_exact_review_source_tamper_is_downgraded(
    tmp_path, kind, cls, step_name, source_label,
):
    job = _base_job(tmp_path, "SMART-TAIL")
    tail = f"{kind.upper()}-TAIL"
    if kind == "audio":
        (job / "intermediate/transcript.json").write_text(json.dumps({"full_text": "body " + tail}))
        keys = ["completeness", "accuracy", "structure", "terminology", "conciseness", "readability"]
    else:
        (job / "intermediate/sections.json").write_text(json.dumps({
            "sections": [{"title": "正文", "text": "body " + tail}],
        }))
        keys = (["completeness", "accuracy", "structure", "terminology", "formula_integrity", "figure_references"]
                if kind == "paper" else
                ["completeness", "accuracy", "structure", "readability", "insight"])
    step = cls(step_name, job, make_step_config(tmp_path, step_name=step_name, pool="ai"))
    review = _run(step, keys, tail, locator_source=source_label)

    async def reader(rel):
        path = job / rel
        return path.read_bytes() if path.exists() else None

    assert (await verify_persisted_review(
        review, job_id=job.name, pipeline=kind, read_file=reader,
    ))["review_reliable"] is True
    record = next(source for source in review["review_input"]["sources"]
                  if source["label"] == source_label)
    (job / record["artifact"]).write_text("tampered", encoding="utf-8")
    verified = await verify_persisted_review(
        review, job_id=job.name, pipeline=kind, read_file=reader,
    )
    assert verified["review_reliable"] is False
    assert any("sha256_mismatch" in reason for reason in verified["reliability_reasons"])


@pytest.mark.asyncio
@pytest.mark.parametrize(("kind", "cls", "step_name", "source_label"), [
    ("video", ReviewStep, "12_review", "mechanical"),
    ("paper", PaperReviewStep, "06_review", "sections"),
    ("article", ArticleReviewStep, "06_review", "sections"),
    ("audio", PodcastReviewStep, "05_review", "transcript"),
])
async def test_pipeline_source_profile_rejects_arbitrary_output_substitution(
    tmp_path, kind, cls, step_name, source_label,
):
    job = _base_job(tmp_path, "SMART-TAIL")
    tail = f"{kind.upper()}-SOURCE"
    if kind == "video":
        (job / "output/notes_mechanical.md").write_text("body " + tail)
        keys = [
            "completeness", "accuracy", "structure", "terminology",
            "visual_integration", "readability",
        ]
    elif kind == "audio":
        (job / "intermediate/transcript.json").write_text(json.dumps({"full_text": "body " + tail}))
        keys = ["completeness", "accuracy", "structure", "terminology", "conciseness", "readability"]
    else:
        (job / "intermediate/sections.json").write_text(json.dumps({
            "sections": [{"title": "正文", "text": "body " + tail}],
        }))
        keys = (
            ["completeness", "accuracy", "structure", "terminology", "formula_integrity", "figure_references"]
            if kind == "paper" else
            ["completeness", "accuracy", "structure", "readability", "insight"]
        )
    review = _run(
        cls(step_name, job, make_step_config(tmp_path, step_name=step_name, pool="ai")),
        keys, tail, locator_source=source_label,
    )
    forged = copy.deepcopy(review)
    record = next(source for source in forged["review_input"]["sources"]
                  if source["label"] == source_label)
    source_data = (job / record["artifact"]).read_bytes()
    fake_rel = "output/arbitrary-review-source.md"
    (job / fake_rel).write_bytes(source_data)
    record["artifact"] = fake_rel

    async def reader(rel):
        path = job / rel
        return path.read_bytes() if path.exists() else None

    verified = await verify_persisted_review(
        forged, job_id=job.name, pipeline=kind, read_file=reader,
    )
    assert verified["review_reliable"] is False
    assert any("source_invalid" in reason for reason in verified["reliability_reasons"])


@pytest.mark.asyncio
@pytest.mark.parametrize(("kind", "cls", "keys"), [
    ("paper", PaperReviewStep,
     ["completeness", "accuracy", "structure", "terminology", "formula_integrity", "figure_references"]),
    ("article", ArticleReviewStep,
     ["completeness", "accuracy", "structure", "readability", "insight"]),
])
@pytest.mark.parametrize("direct_labels", [
    ("original",), ("translated",), ("original", "translated"),
])
async def test_document_source_profile_uses_all_present_direct_sources(
    tmp_path, kind, cls, keys, direct_labels,
):
    job = _base_job(tmp_path, "SMART-TAIL")
    (job / "intermediate/sections.json").write_text(json.dumps({
        "sections": [{"title": "正文", "text": "section fallback"}],
    }))
    for label in direct_labels:
        (job / f"output/{label}.md").write_text(f"{label.upper()}-SOURCE")
    locator_label = direct_labels[0]
    tail = f"{locator_label.upper()}-SOURCE"
    review = _run(
        cls("06_review", job, make_step_config(tmp_path, step_name="06_review", pool="ai")),
        keys, tail, locator_source=locator_label,
    )
    assert {source["label"] for source in review["review_input"]["sources"]} == {
        "smart", *direct_labels,
    }

    async def reader(rel):
        path = job / rel
        return path.read_bytes() if path.exists() else None

    assert (await verify_persisted_review(
        review, job_id=job.name, pipeline=kind, read_file=reader,
    ))["review_reliable"] is True
    forged = copy.deepcopy(review)
    forged["review_input"]["sources"] = [
        source for source in forged["review_input"]["sources"]
        if source["label"] != direct_labels[-1]
    ]
    verified = await verify_persisted_review(
        forged, job_id=job.name, pipeline=kind, read_file=reader,
    )
    assert verified["review_reliable"] is False
    assert f"{kind}_source_profile_mismatch" in verified["reliability_reasons"]
    mixed = copy.deepcopy(review)
    sections = copy.deepcopy(next(
        source for source in mixed["review_input"]["sources"]
        if source["label"] == locator_label
    ))
    sections["label"] = "sections"
    mixed["review_input"]["sources"].append(sections)
    mixed_result = await verify_persisted_review(
        mixed, job_id=job.name, pipeline=kind, read_file=reader,
    )
    assert mixed_result["review_reliable"] is False
    assert f"{kind}_source_profile_mismatch" in mixed_result["reliability_reasons"]


def test_audio_review_rejects_missing_transcript_body(tmp_path):
    job = _base_job(tmp_path, "SMART-TAIL")
    (job / "intermediate/transcript.json").write_text(json.dumps({"segments": []}))
    step = PodcastReviewStep(
        "05_review", job, make_step_config(tmp_path, step_name="05_review", pool="ai"),
    )
    with pytest.raises(ValueError, match="no transcript body"):
        step.execute()
