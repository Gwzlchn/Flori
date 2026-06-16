"""tests for steps/paper/step_05_smart_paper.py"""

import json
import os

import pytest

from steps.paper.step_05_smart_paper import SmartPaperStep
from tests.steps.conftest import make_step_config


class TestSmartPaperStep:
    def _setup_job(self, tmp_path):
        job_dir = tmp_path / "job"
        job_dir.mkdir()
        for d in ["intermediate", "output", "assets", "logs"]:
            (job_dir / d).mkdir()

        sections = {
            "title": "Test Paper",
            "authors": ["Author"],
            "abstract": "Abstract here.",
            "sections": [
                {"level": 1, "title": "Intro", "page": 1, "text": "Intro text", "children": []},
            ],
            "total_sections": 1,
        }
        (job_dir / "intermediate" / "sections.json").write_text(json.dumps(sections))

        figures = [
            {"id": "fig1", "page": 1, "caption": "Architecture", "filename": None, "ocr_text": ""},
        ]
        (job_dir / "intermediate" / "figures.json").write_text(json.dumps(figures))
        return job_dir

    def test_validate_inputs(self, tmp_path):
        job_dir = tmp_path / "job"
        job_dir.mkdir()
        (job_dir / "intermediate").mkdir()
        config = make_step_config(tmp_path, step_name="05_smart_paper")
        step = SmartPaperStep("05_smart_paper", job_dir, config)
        assert len(step.validate_inputs()) == 2

    def test_execute_dry_run(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DRY_RUN", "1")
        job_dir = self._setup_job(tmp_path)
        config = make_step_config(tmp_path, step_name="05_smart_paper", pool="ai")
        step = SmartPaperStep("05_smart_paper", job_dir, config)
        result = step.execute()
        assert result["chars"] > 0
        assert (job_dir / "output" / "notes_smart.md").exists()

    def test_build_prompt(self, tmp_path):
        job_dir = self._setup_job(tmp_path)
        config = make_step_config(tmp_path, step_name="05_smart_paper")
        step = SmartPaperStep("05_smart_paper", job_dir, config)
        sections = step.load_json("intermediate/sections.json")
        figures = step.load_json("intermediate/figures.json")
        prompt = step._build_prompt(sections, figures)
        assert "Test Paper" in prompt
        assert "fig1" in prompt
