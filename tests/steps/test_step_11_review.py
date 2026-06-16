"""tests for steps/video/step_11_review.py"""

import json
import os

import pytest

from steps.video.step_11_review import ReviewStep
from tests.steps.conftest import make_step_config


class TestReviewStep:
    def _setup_job(self, tmp_path):
        job_dir = tmp_path / "job"
        job_dir.mkdir()
        for d in ["output", "logs"]:
            (job_dir / d).mkdir()
        (job_dir / "output" / "notes_mechanical.md").write_text("## 机械版\n\n内容\n")
        (job_dir / "output" / "notes_smart.md").write_text("## 智能版\n\n重组后内容\n")
        return job_dir

    def test_validate_inputs(self, tmp_path):
        job_dir = tmp_path / "job"
        job_dir.mkdir()
        (job_dir / "output").mkdir()
        config = make_step_config(tmp_path, step_name="11_review")
        step = ReviewStep("11_review", job_dir, config)
        missing = step.validate_inputs()
        assert "output/notes_smart.md" in missing

    def test_execute_dry_run(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DRY_RUN", "1")
        job_dir = self._setup_job(tmp_path)
        config = make_step_config(tmp_path, step_name="11_review", pool="ai")
        step = ReviewStep("11_review", job_dir, config)
        result = step.execute()
        assert (job_dir / "output" / "review.json").exists()
        review = json.loads((job_dir / "output" / "review.json").read_text())
        assert "overall" in review

    def test_parse_fallback(self, tmp_path, monkeypatch):
        job_dir = self._setup_job(tmp_path)
        config = make_step_config(tmp_path, step_name="11_review", pool="ai")
        step = ReviewStep("11_review", job_dir, config)
        monkeypatch.setattr(step, "call_ai", lambda *a, **k: "not json at all")
        result = step.execute()
        review = json.loads((job_dir / "output" / "review.json").read_text())
        assert review["overall"] == 3.0
        assert "raw_response" in review
        assert review["parse_failed"] is True
        assert result["parse_failed"] is True
