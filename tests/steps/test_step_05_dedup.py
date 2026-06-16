"""tests for steps/video/step_05_dedup.py (mock imagehash/SSIM)"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from steps.video.step_05_dedup import DedupStep
from tests.steps.conftest import make_step_config


class TestDedupStep:
    def _setup(self, tmp_path):
        job_dir = tmp_path / "job"
        job_dir.mkdir()
        for d in ["intermediate", "assets"]:
            (job_dir / d).mkdir()

        candidates = [
            {"index": 0, "scene_index": 0, "timestamp_sec": 5.0, "filename": "f0.jpg"},
            {"index": 1, "scene_index": 1, "timestamp_sec": 15.0, "filename": "f1.jpg"},
            {"index": 2, "scene_index": 2, "timestamp_sec": 25.0, "filename": "f2.jpg"},
        ]
        (job_dir / "intermediate" / "candidates.json").write_text(json.dumps(candidates))

        from PIL import Image
        for name in ["f0.jpg", "f1.jpg", "f2.jpg"]:
            img = Image.new("RGB", (320, 180), color="red")
            img.save(str(job_dir / "assets" / name))
        return job_dir

    def test_validate_missing(self, tmp_path):
        job_dir = tmp_path / "job"
        job_dir.mkdir()
        (job_dir / "intermediate").mkdir()
        config = make_step_config(tmp_path, step_name="05_dedup")
        step = DedupStep("05_dedup", job_dir, config)
        assert step.validate_inputs() == ["intermediate/candidates.json"]

    def test_execute_all_identical(self, tmp_path):
        job_dir = self._setup(tmp_path)
        config = make_step_config(tmp_path, step_name="05_dedup", pool="cpu")
        step = DedupStep("05_dedup", job_dir, config)
        result = step.execute()

        dedup = json.loads((job_dir / "intermediate" / "dedup.json").read_text())
        assert len(dedup) == 3
        assert result["total"] == 3
        kept = sum(1 for d in dedup if d["keep"])
        assert kept == 1  # all identical → only first kept

    def test_execute_all_different(self, tmp_path):
        job_dir = self._setup(tmp_path)
        from PIL import Image, ImageDraw
        img1 = Image.new("RGB", (320, 180), color="white")
        draw1 = ImageDraw.Draw(img1)
        draw1.rectangle([0, 0, 160, 90], fill="black")
        img1.save(str(job_dir / "assets" / "f0.jpg"))

        img2 = Image.new("RGB", (320, 180), color="black")
        draw2 = ImageDraw.Draw(img2)
        draw2.rectangle([160, 90, 320, 180], fill="white")
        img2.save(str(job_dir / "assets" / "f1.jpg"))

        img3 = Image.new("RGB", (320, 180), color="blue")
        draw3 = ImageDraw.Draw(img3)
        draw3.ellipse([50, 30, 270, 150], fill="yellow")
        img3.save(str(job_dir / "assets" / "f2.jpg"))

        config = make_step_config(tmp_path, step_name="05_dedup", pool="cpu")
        step = DedupStep("05_dedup", job_dir, config)
        result = step.execute()

        assert result["kept"] == 3

    def test_missing_file_marked_not_kept(self, tmp_path):
        job_dir = self._setup(tmp_path)
        (job_dir / "assets" / "f1.jpg").unlink()
        config = make_step_config(tmp_path, step_name="05_dedup", pool="cpu")
        step = DedupStep("05_dedup", job_dir, config)
        result = step.execute()

        dedup = json.loads((job_dir / "intermediate" / "dedup.json").read_text())
        f1 = next(d for d in dedup if d["filename"] == "f1.jpg")
        assert f1["keep"] is False
