"""tests for steps/paper/step_03_sections.py"""

import json

import pytest

from steps.paper.step_03_sections import SectionsStep
from tests.steps.conftest import make_step_config


class TestSectionsStep:
    def _setup_job(self, tmp_path):
        job_dir = tmp_path / "job"
        job_dir.mkdir()
        for d in ["intermediate"]:
            (job_dir / d).mkdir()

        parsed = {
            "title": "Test Paper",
            "authors": ["Author A"],
            "abstract": "This is abstract.",
            "pages": 5,
            "sections": [
                {"level": 1, "title": "Introduction", "page": 1, "text": "Intro text"},
                {"level": 2, "title": "Background", "page": 1, "text": "Background text"},
                {"level": 1, "title": "Method", "page": 2, "text": "Method text"},
                {"level": 2, "title": "Architecture", "page": 2, "text": "Arch text"},
                {"level": 2, "title": "Training", "page": 3, "text": "Train text"},
            ],
            "figures": [],
            "formulas": [],
        }
        (job_dir / "intermediate" / "parsed.json").write_text(json.dumps(parsed))
        return job_dir

    def test_execute(self, tmp_path):
        job_dir = self._setup_job(tmp_path)
        config = make_step_config(tmp_path, step_name="03_sections", pool="cpu")
        step = SectionsStep("03_sections", job_dir, config)
        result = step.execute()

        sections = json.loads((job_dir / "intermediate" / "sections.json").read_text())
        assert sections["title"] == "Test Paper"
        tree = sections["sections"]
        assert len(tree) == 2
        assert tree[0]["title"] == "Introduction"
        assert len(tree[0]["children"]) == 1
        assert tree[1]["title"] == "Method"
        assert len(tree[1]["children"]) == 2

    def test_validate_inputs(self, tmp_path):
        job_dir = tmp_path / "job"
        job_dir.mkdir()
        (job_dir / "intermediate").mkdir()
        config = make_step_config(tmp_path, step_name="03_sections")
        step = SectionsStep("03_sections", job_dir, config)
        assert step.validate_inputs() == ["intermediate/parsed.json"]

    def test_idempotent(self, tmp_path):
        job_dir = self._setup_job(tmp_path)
        config = make_step_config(tmp_path, step_name="03_sections", pool="cpu")
        step = SectionsStep("03_sections", job_dir, config)
        step.execute()
        step.mark_done()
        step2 = SectionsStep("03_sections", job_dir, config)
        assert step2.should_run() is False
