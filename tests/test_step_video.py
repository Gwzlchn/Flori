"""tests:视频步骤的质量/健壮性移植(代表帧 SSIM / dedup 三段式 / 下载验收)。"""

from __future__ import annotations

import numpy as np
import pytest

from steps.common.step_01_download import DownloadStep
from steps.video.step_04_frames import FramesStep
from steps.video.step_05_dedup import DedupStep


def _cfg(tmp):
    return {
        "step": {"name": "x", "pool": "cpu", "timeout_sec": 60, "retries": 0},
        "domain": {}, "paths": {"data_dir": str(tmp)}, "ai": {}, "providers": {},
    }


class _FakeCap:
    """鸭子类型替身,按帧号返回预置 numpy 帧。"""
    def __init__(self, frames):
        self.frames = frames; self.pos = 0
    def set(self, prop, val):
        self.pos = int(val)
    def read(self):
        f = self.frames.get(self.pos)
        return (f is not None, f)


def _img(seed):
    rng = np.random.default_rng(seed)
    return rng.integers(0, 256, (180, 320, 3), dtype=np.uint8)


class TestFramePick:
    def test_static_scene_picks_early_frame(self, tmp_path):
        step = FramesStep("04_frames", tmp_path, _cfg(tmp_path))
        same = _img(1)
        cap = _FakeCap({0: same, 70: same.copy(), 5: _img(99)})
        _, target = step._pick_representative(cap, 0, 100, 0.7, 0.85)
        assert target == 5   # 静止(SSIM 高)→ 取 start+5,避开过渡态

    def test_dynamic_scene_picks_mid_frame(self, tmp_path):
        step = FramesStep("04_frames", tmp_path, _cfg(tmp_path))
        cap = _FakeCap({0: _img(1), 70: _img(2)})
        _, target = step._pick_representative(cap, 0, 100, 0.7, 0.85)
        assert target == 70  # 画面在变(SSIM 低)→ 取 ratio 位置帧


class TestPhashBand:
    def test_three_bands(self):
        assert DedupStep._phash_band(0, 6) == "duplicate"
        assert DedupStep._phash_band(6, 6) == "duplicate"
        assert DedupStep._phash_band(8, 6) == "gray"        # 6 < 8 <= 10
        assert DedupStep._phash_band(10, 6) == "gray"
        assert DedupStep._phash_band(11, 6) == "different"  # > 阈值+4


class TestVerifyDownload:
    def test_missing_raises(self, tmp_path):
        from shared.errors import InputInvalidError
        step = DownloadStep("01_download", tmp_path, _cfg(tmp_path))
        with pytest.raises(InputInvalidError):
            step._verify_download(tmp_path / "nope.mp4")

    def test_too_small_raises(self, tmp_path):
        from shared.errors import InputInvalidError
        step = DownloadStep("01_download", tmp_path, _cfg(tmp_path))
        f = tmp_path / "source.mp4"; f.write_bytes(b"x" * 100)
        with pytest.raises(InputInvalidError):
            step._verify_download(f)

    def test_valid_passes(self, tmp_path, monkeypatch):
        step = DownloadStep("01_download", tmp_path, _cfg(tmp_path))
        f = tmp_path / "source.mp4"; f.write_bytes(b"x" * 2_000_000)
        monkeypatch.setattr(step, "_get_video_duration", lambda p: 123.0)
        step._verify_download(f)  # 不抛
