"""tests for steps/paper/step_02_pdf_parse.py (mock pymupdf)"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from steps.paper.step_02_pdf_parse import PdfParseStep
from tests.steps.conftest import make_step_config


class TestPdfParseStep:
    def test_validate_missing(self, tmp_path):
        job_dir = tmp_path / "job"
        job_dir.mkdir()
        (job_dir / "input").mkdir()
        config = make_step_config(tmp_path, step_name="02_pdf_parse")
        step = PdfParseStep("02_pdf_parse", job_dir, config)
        assert step.validate_inputs() == ["input/source.pdf"]

    def test_execute_mock(self, tmp_path, monkeypatch):
        job_dir = tmp_path / "job"
        job_dir.mkdir()
        for d in ["input", "intermediate"]:
            (job_dir / d).mkdir()
        (job_dir / "input" / "source.pdf").write_bytes(b"%PDF-1.4 fake")

        mock_page = MagicMock()
        mock_page.get_text.side_effect = lambda fmt=None: (
            {"blocks": [{"lines": [{"spans": [
                {"text": "Test Title", "size": 18, "flags": 16}
            ]}]}]}
            if fmt == "dict" else "Abstract\nSome text\n\nIntroduction\nIntro text"
        )
        mock_page.get_images.return_value = []

        mock_doc = MagicMock()
        mock_doc.__len__ = lambda self: 1
        mock_doc.__getitem__ = lambda self, i: mock_page
        mock_doc.__iter__ = lambda self: iter([mock_page])
        mock_doc.__enter__ = lambda self: self
        mock_doc.__exit__ = lambda self, *a: None
        mock_doc.metadata = {"title": "Test Paper", "author": "Author A, Author B"}

        mock_fitz = MagicMock()
        mock_fitz.open.return_value = mock_doc
        monkeypatch.setitem(sys.modules, "fitz", mock_fitz)

        config = make_step_config(tmp_path, step_name="02_pdf_parse", pool="cpu")
        step = PdfParseStep("02_pdf_parse", job_dir, config)
        result = step.execute()

        parsed = json.loads((job_dir / "intermediate" / "parsed.json").read_text())
        assert parsed["title"] == "Test Paper"
        assert parsed["pages"] == 1
        assert len(parsed["authors"]) == 2

    def test_input_hashes(self, tmp_path):
        job_dir = tmp_path / "job"
        job_dir.mkdir()
        (job_dir / "input").mkdir()
        (job_dir / "input" / "source.pdf").write_bytes(b"%PDF test")
        config = make_step_config(tmp_path, step_name="02_pdf_parse")
        step = PdfParseStep("02_pdf_parse", job_dir, config)
        hashes = step.input_hashes()
        assert "pdf" in hashes
        assert hashes["pdf"].startswith("sha256:")
