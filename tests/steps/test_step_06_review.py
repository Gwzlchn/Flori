"""tests for steps/paper/step_06_review.py"""

import json

from steps.paper.step_06_review import PaperReviewStep
from tests.steps.conftest import make_step_config


class TestPaperReviewStep:
    def _setup_job(self, tmp_path):
        job_dir = tmp_path / "job"
        job_dir.mkdir()
        for d in ["intermediate", "output", "logs"]:
            (job_dir / d).mkdir()

        sections = {
            "title": "Test Paper",
            "sections": [
                {"level": 1, "title": "Intro", "page": 1, "text": "text", "children": []},
            ],
        }
        (job_dir / "intermediate" / "sections.json").write_text(json.dumps(sections))
        (job_dir / "output" / "versions").mkdir()
        (job_dir / "output" / "versions" / "notes_smart_anthropic_claude-sonnet-4-6_20260101-000000.md").write_text("## 论文笔记\n\n内容\n")
        return job_dir

    def test_validate_inputs(self, tmp_path):
        job_dir = tmp_path / "job"
        job_dir.mkdir()
        for d in ["intermediate", "output"]:
            (job_dir / d).mkdir()
        config = make_step_config(tmp_path, step_name="06_review")
        step = PaperReviewStep("06_review", job_dir, config)
        assert len(step.validate_inputs()) == 2

    def test_execute_dry_run(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DRY_RUN", "1")
        job_dir = self._setup_job(tmp_path)
        config = make_step_config(tmp_path, step_name="06_review", pool="ai")
        step = PaperReviewStep("06_review", job_dir, config)
        result = step.execute()
        assert (job_dir / "output" / "review.json").exists()
        review = json.loads((job_dir / "output" / "review.json").read_text())
        assert "overall" in review
