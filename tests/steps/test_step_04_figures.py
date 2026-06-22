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

    def test_same_page_captions_map_to_distinct_images(self, tmp_path, monkeypatch):
        """I-L14: 同页多条图注应各取不同位图(消费式),多于位图的图注降级为 None。"""
        job_dir = tmp_path / "job"
        job_dir.mkdir()
        for d in ["intermediate", "input", "assets"]:
            (job_dir / d).mkdir()
        (job_dir / "input" / "source.pdf").write_bytes(b"%PDF")
        parsed = {"figures": [
            {"id": "fig1", "page": 1, "caption": "c1"},
            {"id": "fig2", "page": 1, "caption": "c2"},
            {"id": "fig3", "page": 1, "caption": "c3"},  # 第三条无位图可取
        ], "sections": []}
        (job_dir / "intermediate" / "parsed.json").write_text(json.dumps(parsed))

        mock_doc = MagicMock()
        mock_doc.__enter__ = lambda self: self
        mock_doc.__exit__ = lambda self, *a: None
        mock_fitz = MagicMock()
        mock_fitz.open.return_value = mock_doc
        monkeypatch.setitem(sys.modules, "fitz", mock_fitz)

        config = make_step_config(tmp_path, step_name="04_figures", pool="cpu")
        step = FiguresStep("04_figures", job_dir, config)
        # 同页两张位图;OCR 关闭(返回 None),不依赖真实图片文件。
        monkeypatch.setattr(step, "_extract_images_from_pdf", lambda doc, assets: [
            {"page": 1, "filename": "figure-0000.png", "index": 0},
            {"page": 1, "filename": "figure-0001.png", "index": 1},
        ])
        monkeypatch.setattr(step, "_create_ocr_engine", lambda: None)
        step.execute()

        figs = json.loads((job_dir / "intermediate" / "figures.json").read_text())
        assert (figs[0]["filename"], figs[0]["index"]) == ("figure-0000.png", 0)
        assert (figs[1]["filename"], figs[1]["index"]) == ("figure-0001.png", 1)
        assert figs[2]["filename"] is None and figs[2]["index"] is None
