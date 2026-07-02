"""steps/video/step_03_scene.py 的测试,scenedetect 全 mock。"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from steps.video.step_03_scene import SceneStep
from tests.steps.conftest import make_job_dir, make_step_config


class TestSceneStep:
    def _setup(self, tmp_path):
        job_dir = make_job_dir(tmp_path, "input", "intermediate")
        (job_dir / "input" / "source.mp4").write_bytes(b"\x00" * 2048)
        return job_dir

    def test_validate_missing(self, tmp_path):
        job_dir = tmp_path / "job"
        job_dir.mkdir()
        (job_dir / "input").mkdir()
        config = make_step_config(tmp_path, step_name="03_scene")
        step = SceneStep("03_scene", job_dir, config)
        assert step.validate_inputs() == ["input/source.mp4"]

    def test_validate_present(self, tmp_path):
        job_dir = self._setup(tmp_path)
        config = make_step_config(tmp_path, step_name="03_scene")
        step = SceneStep("03_scene", job_dir, config)
        assert step.validate_inputs() == []

    def test_input_hashes_includes_config(self, tmp_path):
        job_dir = self._setup(tmp_path)
        config = make_step_config(tmp_path, step_name="03_scene")
        config["domain"]["scene"] = {"adaptive_threshold": 3.0}
        step = SceneStep("03_scene", job_dir, config)
        hashes = step.input_hashes()
        assert "video" in hashes
        assert "config" in hashes
        assert "3.0" in hashes["config"]

    def test_execute_mock(self, tmp_path, monkeypatch):
        import sys

        job_dir = self._setup(tmp_path)
        config = make_step_config(tmp_path, step_name="03_scene", pool="scene")

        mock_start = MagicMock()
        mock_start.get_seconds.return_value = 0.0
        mock_start.get_frames.return_value = 0
        mock_end = MagicMock()
        mock_end.get_seconds.return_value = 10.0
        mock_end.get_frames.return_value = 300
        mock_end.__sub__ = lambda self, other: MagicMock(get_seconds=lambda: 10.0)

        mock_video = MagicMock()
        mock_video.frame_rate = 30
        mock_video.duration.frame_num = 900
        mock_video.duration.get_frames.return_value = 900

        mock_sm_instance = MagicMock()
        mock_sm_instance.get_scene_list.return_value = [(mock_start, mock_end)]

        mock_scenedetect = MagicMock()
        mock_scenedetect.open_video.return_value = mock_video
        mock_scenedetect.SceneManager.return_value = mock_sm_instance
        mock_detectors = MagicMock()
        monkeypatch.setitem(sys.modules, "scenedetect", mock_scenedetect)
        monkeypatch.setitem(sys.modules, "scenedetect.detectors", mock_detectors)

        step = SceneStep("03_scene", job_dir, config)
        result = step.execute()

        assert result["scenes"] == 1
        scenes = json.loads((job_dir / "intermediate" / "scenes.json").read_text())
        assert len(scenes) == 1
        # 钉死从 mock 边界导出的派生字段:只断 start_sec 时,end/duration 变量互换不会被发现。
        assert scenes[0]["start_sec"] == 0.0
        assert scenes[0]["end_sec"] == 10.0
        assert scenes[0]["duration_sec"] == 10.0
