"""steps/video/step_06_ocr.py 的测试,RapidOCR 全 mock。"""

import hashlib
import json
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
        assert ocr[0]["asset_sha256"] == hashlib.sha256(
            (job_dir / "assets" / "f0.jpg").read_bytes()
        ).hexdigest()
        assert (ocr[0]["width"], ocr[0]["height"]) == (320, 180)

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
        assert f0["boxes"] == []
        assert f0["asset_sha256"] is None
        assert f0["width"] is None and f0["height"] is None

    @patch("steps.video.step_06_ocr.OcrStep._create_ocr_engine")
    def test_frame_change_while_scanning_fails_closed(
        self, mock_engine_factory, tmp_path, monkeypatch,
    ):
        job_dir = self._setup(tmp_path)
        mock_engine_factory.return_value = MagicMock()
        step = OcrStep(
            "06_ocr", job_dir,
            make_step_config(tmp_path, step_name="06_ocr", pool="cpu"),
        )

        def mutate_frame(_engine, img_path, _threshold):
            from PIL import Image

            Image.new("RGB", (320, 180), color="black").save(img_path)
            return "changed", [{"text": "changed", "box": [0, 0, 10, 10]}]

        monkeypatch.setattr(step, "_ocr_image", mutate_frame)

        with pytest.raises(ValueError, match="frame changed"):
            step.execute()
        assert not (job_dir / "intermediate" / "ocr.json").exists()
