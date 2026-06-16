"""tests for steps/video/step_04_frames.py (cv2 代表帧;mock VideoCapture/imwrite)"""

import json
from pathlib import Path
from unittest.mock import patch

import numpy as np

from steps.video.step_04_frames import FramesStep
from tests.steps.conftest import make_step_config


class TestFramesStep:
    def _setup(self, tmp_path):
        job_dir = tmp_path / "job"
        job_dir.mkdir()
        for d in ["input", "intermediate", "assets"]:
            (job_dir / d).mkdir()
        (job_dir / "input" / "source.mp4").write_bytes(b"\x00" * 2048)
        scenes = [
            {"index": 0, "start_sec": 0.0, "end_sec": 10.0, "duration_sec": 10.0},
            {"index": 1, "start_sec": 10.0, "end_sec": 25.0, "duration_sec": 15.0},
        ]
        (job_dir / "intermediate" / "scenes.json").write_text(json.dumps(scenes))
        return job_dir

    def test_validate_missing(self, tmp_path):
        job_dir = tmp_path / "job"
        job_dir.mkdir()
        for d in ["input", "intermediate"]:
            (job_dir / d).mkdir()
        config = make_step_config(tmp_path, step_name="04_frames")
        step = FramesStep("04_frames", job_dir, config)
        assert len(step.validate_inputs()) == 2

    def test_execute_mock(self, tmp_path):
        import cv2

        job_dir = self._setup(tmp_path)
        config = make_step_config(tmp_path, step_name="04_frames", pool="cpu")
        step = FramesStep("04_frames", job_dir, config)

        fake_frame = np.zeros((180, 320, 3), dtype=np.uint8)

        class FakeCap:
            def get(self, prop): return 25.0
            def set(self, prop, val): pass
            def read(self): return (True, fake_frame)
            def release(self): pass

        def fake_imwrite(path, *a, **k):
            Path(path).write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 2000)
            return True

        with patch.object(cv2, "VideoCapture", return_value=FakeCap()), \
             patch.object(cv2, "imwrite", side_effect=fake_imwrite):
            result = step.execute()

        assert result["total"] >= 2  # 两个场景各一代表帧
        candidates = json.loads((job_dir / "intermediate" / "candidates.json").read_text())
        assert len(candidates) >= 2
        assert all({"index", "scene_index", "timestamp_sec", "filename"} <= set(c) for c in candidates)
