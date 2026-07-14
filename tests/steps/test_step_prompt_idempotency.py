"""Review 与 PDF 翻译的实际 Prompt 模板必须参与幂等指纹."""

from __future__ import annotations

import json

import pytest

from steps.article.step_05_review import ArticleReviewStep
from steps.audio.step_05_review import PodcastReviewStep
from steps.paper.step_04_translate_paper import TranslatePaperStep
from steps.paper.step_06_review import PaperReviewStep
from steps.video.step_12_review import ReviewStep
from tests.steps.conftest import make_job_dir, make_step_config


REVIEW_CASES = (
    ("video", ReviewStep, "12_review"),
    ("paper", PaperReviewStep, "06_review"),
    ("article", ArticleReviewStep, "06_review"),
    ("audio", PodcastReviewStep, "05_review"),
)


def _review_job(tmp_path):
    job = make_job_dir(
        tmp_path, "intermediate", "output", "output/versions",
    )
    smart = job / "output/versions/notes_smart_test_x_20260101-000000.md"
    smart.write_text("SMART", encoding="utf-8")
    (job / "intermediate/sections.json").write_text(
        '{"title":"T","sections":[]}', encoding="utf-8",
    )
    (job / "intermediate/transcript.json").write_text(
        '{"full_text":"AUDIO"}', encoding="utf-8",
    )
    (job / "output/notes_mechanical.md").write_text("MECHANICAL", encoding="utf-8")
    return job


@pytest.mark.parametrize(("pipeline", "step_cls", "step_name"), REVIEW_CASES)
@pytest.mark.parametrize("template_source", ["hot", "override"])
def test_review_template_change_invalidates_done(
    tmp_path, pipeline, step_cls, step_name, template_source,
):
    job = _review_job(tmp_path)
    hot = tmp_path / "prompts/templates"
    hot.mkdir(parents=True)
    if template_source == "hot":
        (hot / f"{step_name}.md").write_text("TEMPLATE V1", encoding="utf-8")
        (job / "job.json").write_text("{}", encoding="utf-8")
    else:
        (job / "job.json").write_text(json.dumps({
            "prompt_overrides": {
                step_name: {"content": "OVERRIDE V1", "version": 1},
            },
        }), encoding="utf-8")

    config = make_step_config(
        tmp_path, step_name=step_name, pool="ai", pipeline=pipeline,
    )
    step = step_cls(step_name, job, config)
    first_hashes = step.input_hashes()
    assert "template" in first_hashes
    step.mark_done()

    if template_source == "hot":
        (hot / f"{step_name}.md").write_text("TEMPLATE V2", encoding="utf-8")
    else:
        (job / "job.json").write_text(json.dumps({
            "prompt_overrides": {
                step_name: {"content": "OVERRIDE V2", "version": 2},
            },
        }), encoding="utf-8")
    rerun = step_cls(step_name, job, config)
    assert rerun.input_hashes()["template"] != first_hashes["template"]
    assert rerun.should_run() is True


def test_pdf_direct_template_change_invalidates_done(tmp_path):
    job = make_job_dir(tmp_path, "input", "intermediate", "output")
    (job / "job.json").write_text("{}", encoding="utf-8")
    (job / "input/source.pdf").write_bytes(b"%PDF fake")
    (job / "intermediate/sections.json").write_text("{}", encoding="utf-8")
    (job / "intermediate/parsed.json").write_text(json.dumps({
        "source_kind": "pdf-only", "pages": 1,
    }), encoding="utf-8")
    hot = tmp_path / "prompts/templates"
    hot.mkdir(parents=True)
    (hot / "04_translate_paper.md").write_text("TEXT", encoding="utf-8")
    pdf_template = hot / "04_translate_paper.pdf.md"
    pdf_template.write_text("PDF V1", encoding="utf-8")
    config = make_step_config(
        tmp_path, step_name="04_translate_paper", pool="ai", pipeline="paper",
    )
    step = TranslatePaperStep("04_translate_paper", job, config)
    first_hashes = step.input_hashes()
    assert set(json.loads(first_hashes["template"])) == {"04_translate_paper.pdf"}
    step.mark_done()

    pdf_template.write_text("PDF V2", encoding="utf-8")
    rerun = TranslatePaperStep("04_translate_paper", job, config)
    assert rerun.input_hashes()["template"] != first_hashes["template"]
    assert rerun.should_run() is True
