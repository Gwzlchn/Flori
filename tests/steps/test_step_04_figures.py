"""tests for steps/paper/step_04_figures.py (mock pymupdf)"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from steps.paper.step_04_figures import FiguresStep
from tests.steps.conftest import make_step_config


class TestFiguresStep:
    def test_validate_missing(self, tmp_path):
        job_dir = tmp_path / "job"
        job_dir.mkdir()
        (job_dir / "intermediate").mkdir()
        (job_dir / "input").mkdir()
        config = make_step_config(tmp_path, step_name="04_figures")
        step = FiguresStep("04_figures", job_dir, config)
        assert len(step.validate_inputs()) == 2

    def test_validate_present(self, tmp_path):
        job_dir = tmp_path / "job"
        job_dir.mkdir()
        (job_dir / "intermediate").mkdir()
        (job_dir / "input").mkdir()
        (job_dir / "intermediate" / "parsed.json").write_text('{"figures": []}')
        (job_dir / "input" / "source.pdf").write_bytes(b"%PDF")
        config = make_step_config(tmp_path, step_name="04_figures")
        step = FiguresStep("04_figures", job_dir, config)
        assert step.validate_inputs() == []

    def test_execute_no_figures(self, tmp_path, monkeypatch):
        job_dir = tmp_path / "job"
        job_dir.mkdir()
        for d in ["intermediate", "input", "assets"]:
            (job_dir / d).mkdir()
        (job_dir / "input" / "source.pdf").write_bytes(b"%PDF")
        parsed = {"figures": [], "sections": []}
        (job_dir / "intermediate" / "parsed.json").write_text(json.dumps(parsed))

        mock_page = MagicMock()
        mock_page.get_images.return_value = []
        mock_doc = MagicMock()
        mock_doc.__len__ = lambda self: 1
        mock_doc.__getitem__ = lambda self, i: mock_page
        mock_doc.__enter__ = lambda self: self
        mock_doc.__exit__ = lambda self, *a: None

        mock_fitz = MagicMock()
        mock_fitz.open.return_value = mock_doc
        monkeypatch.setitem(sys.modules, "fitz", mock_fitz)

        config = make_step_config(tmp_path, step_name="04_figures", pool="cpu")
        step = FiguresStep("04_figures", job_dir, config)
        result = step.execute()

        assert result["figures"] == 0
        figures = json.loads((job_dir / "intermediate" / "figures.json").read_text())
        assert figures == []

    def test_ocr_engine_none_returns_empty(self, tmp_path):
        """When OCR engine init fails, _ocr_figure should return empty string."""
        job_dir = tmp_path / "job"
        job_dir.mkdir()
        (job_dir / "assets").mkdir()
        config = make_step_config(tmp_path, step_name="04_figures")
        step = FiguresStep("04_figures", job_dir, config)
        result = step._ocr_figure(None, job_dir / "assets" / "nonexistent.png")
        assert result == ""

    def test_input_hashes(self, tmp_path):
        job_dir = tmp_path / "job"
        job_dir.mkdir()
        for d in ["intermediate", "input"]:
            (job_dir / d).mkdir()
        (job_dir / "intermediate" / "parsed.json").write_text('{}')
        (job_dir / "input" / "source.pdf").write_bytes(b"%PDF")
        config = make_step_config(tmp_path, step_name="04_figures")
        step = FiguresStep("04_figures", job_dir, config)
        hashes = step.input_hashes()
        assert "parsed" in hashes
        assert "pdf" in hashes
