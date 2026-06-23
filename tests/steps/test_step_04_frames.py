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

        class FakeReader:   # 对齐 PyAV _VideoReader:.fps + read_at_frame(frame_no) + close()
            fps = 25.0
            def read_at_frame(self, n): return fake_frame
            def close(self): pass

        def fake_imwrite(path, *a, **k):
            Path(path).write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 2000)
            return True

        with patch("steps.video.step_04_frames._VideoReader", return_value=FakeReader()), \
             patch.object(cv2, "imwrite", side_effect=fake_imwrite):
            result = step.execute()

        assert result["total"] >= 2  # 两个场景各一代表帧
        candidates = json.loads((job_dir / "intermediate" / "candidates.json").read_text())
        assert len(candidates) >= 2
        assert all({"index", "scene_index", "timestamp_sec", "filename"} <= set(c) for c in candidates)

    # ── 代表帧选取算法(_pick_representative)的三条分支:之前 FakeCap 恒返回同一全 0 帧
    #    → SSIM 恒=1 永远走静止分支,动态/clamp/超长采样从未被驱动,断言也只数 total。
    #    下面用按帧号返回差异化内容的 FakeCap,真正驱动并断言"选了哪一帧/时间戳"。──

    def _one_scene_job(self, tmp_path, start, end):
        job_dir = tmp_path / "job"
        job_dir.mkdir()
        for d in ["input", "intermediate", "assets"]:
            (job_dir / d).mkdir()
        (job_dir / "input" / "source.mp4").write_bytes(b"\x00" * 2048)
        scenes = [{"index": 0, "start_sec": start, "end_sec": end, "duration_sec": end - start}]
        (job_dir / "intermediate" / "scenes.json").write_text(json.dumps(scenes))
        return job_dir

    @staticmethod
    def _fake_imwrite(path, *a, **k):
        Path(path).write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 2000)   # >1024B 才被 _save 收录
        return True

    def test_dynamic_scene_picks_ratio_frame(self, tmp_path):
        """画面在变(head 与 ratio 帧 SSIM 低)→ 取 ratio 位置帧(mf)而非靠前帧。"""
        import cv2

        job_dir = self._one_scene_job(tmp_path, 0.0, 10.0)
        config = make_step_config(tmp_path, step_name="04_frames", pool="cpu")
        step = FramesStep("04_frames", job_dir, config)

        fps = 25.0
        sf, ef = 0, int(10.0 * fps)            # 0, 250
        mf = sf + int((ef - sf) * 0.7)         # 175(默认 dynamic_pick_ratio=0.7)

        class DynReader:   # read_at_frame(frame_no):head 黑 / ratio 帧白 → SSIM≈0 < 0.85
            fps = 25.0
            def read_at_frame(self, n):
                v = 0 if n < mf else 255
                return np.full((180, 320, 3), v, dtype=np.uint8)
            def close(self): pass

        with patch("steps.video.step_04_frames._VideoReader", return_value=DynReader()), \
             patch.object(cv2, "imwrite", side_effect=self._fake_imwrite):
            step.execute()

        scene_cands = [c for c in json.loads(
            (job_dir / "intermediate" / "candidates.json").read_text()) if c["source"] == "scene"]
        assert len(scene_cands) == 1
        assert scene_cands[0]["timestamp_sec"] == round(mf / fps, 2)   # 7.0,取到 ratio 位置帧

    def test_static_scene_picks_start_plus_5(self, tmp_path):
        """基本静止(SSIM 高)→ 取靠前帧 min(sf+5, ef-1),避开画到一半的过渡态。"""
        import cv2

        job_dir = self._one_scene_job(tmp_path, 0.0, 10.0)
        config = make_step_config(tmp_path, step_name="04_frames", pool="cpu")
        step = FramesStep("04_frames", job_dir, config)

        class StaticReader:
            fps = 25.0
            def read_at_frame(self, n): return np.full((180, 320, 3), 128, dtype=np.uint8)
            def close(self): pass

        with patch("steps.video.step_04_frames._VideoReader", return_value=StaticReader()), \
             patch.object(cv2, "imwrite", side_effect=self._fake_imwrite):
            step.execute()

        scene_cands = [c for c in json.loads(
            (job_dir / "intermediate" / "candidates.json").read_text()) if c["source"] == "scene"]
        assert len(scene_cands) == 1
        assert scene_cands[0]["timestamp_sec"] == round(5 / 25.0, 2)   # 0.2 = (sf+5)/fps

    def test_long_scene_forced_sampling(self, tmp_path):
        """超长场景(>max_gap)固定间隔保底采样,产出 source=='sample' 帧,避免长讲解只剩一帧。"""
        import cv2

        job_dir = self._one_scene_job(tmp_path, 0.0, 100.0)   # 100s > 默认 max_gap 60
        config = make_step_config(tmp_path, step_name="04_frames", pool="cpu")
        step = FramesStep("04_frames", job_dir, config)

        class StaticReader:
            fps = 25.0
            def read_at_frame(self, n): return np.full((180, 320, 3), 128, dtype=np.uint8)
            def close(self): pass

        with patch("steps.video.step_04_frames._VideoReader", return_value=StaticReader()), \
             patch.object(cv2, "imwrite", side_effect=self._fake_imwrite):
            step.execute()

        samples = [c for c in json.loads(
            (job_dir / "intermediate" / "candidates.json").read_text()) if c["source"] == "sample"]
        # forced_interval 15s,t 从 start+15 起到 < end-5(95):15,30,45,60,75,90 → 6 帧保底
        assert [c["timestamp_sec"] for c in samples] == [15.0, 30.0, 45.0, 60.0, 75.0, 90.0]
