"""steps/video/step_06_ocr.py 的测试,RapidOCR 全 mock。"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from steps.video.step_06_ocr import OcrStep
from tests.steps.conftest import make_step_config


class TestOcrStep:
    def _setup(self, tmp_path):
        job_dir = tmp_path / "job"
        job_dir.mkdir()
        for d in ["intermediate", "assets"]:
            (job_dir / d).mkdir()

        dedup = [
            {"index": 0, "filename": "f0.jpg", "timestamp_sec": 5.0, "keep": True, "phash": "abc"},
            {"index": 1, "filename": "f1.jpg", "timestamp_sec": 15.0, "keep": False, "phash": "def"},
            {"index": 2, "filename": "f2.jpg", "timestamp_sec": 25.0, "keep": True, "phash": "ghi"},
        ]
        (job_dir / "intermediate" / "dedup.json").write_text(json.dumps(dedup))

        from PIL import Image
        for name in ["f0.jpg", "f2.jpg"]:
            Image.new("RGB", (320, 180), color="white").save(str(job_dir / "assets" / name))
        return job_dir

    def test_validate_missing(self, tmp_path):
        job_dir = tmp_path / "job"
        job_dir.mkdir()
        (job_dir / "intermediate").mkdir()
        config = make_step_config(tmp_path, step_name="06_ocr")
        step = OcrStep("06_ocr", job_dir, config)
        assert step.validate_inputs() == ["intermediate/dedup.json"]

    @patch("steps.video.step_06_ocr.OcrStep._create_ocr_engine")
    def test_execute_mock(self, mock_engine_factory, tmp_path):
        job_dir = self._setup(tmp_path)
        config = make_step_config(tmp_path, step_name="06_ocr", pool="cpu")

        mock_engine = MagicMock()
        mock_engine.return_value = (
            [([0, 0, 100, 100], "Hello World", 0.95)],
            None,
        )
        mock_engine_factory.return_value = mock_engine

        step = OcrStep("06_ocr", job_dir, config)
        result = step.execute()

        assert result["total"] == 2  # only keep=True frames
        ocr = json.loads((job_dir / "intermediate" / "ocr.json").read_text())
        assert len(ocr) == 2
        assert ocr[0]["text"] == "Hello World"

    @patch("steps.video.step_06_ocr.OcrStep._create_ocr_engine")
    def test_missing_image(self, mock_engine_factory, tmp_path):
        job_dir = self._setup(tmp_path)
        (job_dir / "assets" / "f0.jpg").unlink()
        config = make_step_config(tmp_path, step_name="06_ocr", pool="cpu")

        mock_engine = MagicMock()
        mock_engine_factory.return_value = mock_engine

        step = OcrStep("06_ocr", job_dir, config)
        result = step.execute()

        ocr = json.loads((job_dir / "intermediate" / "ocr.json").read_text())
        f0 = ocr[0]
        assert f0["text"] == ""
