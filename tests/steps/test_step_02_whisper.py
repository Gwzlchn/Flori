"""tests for steps/video/step_02_whisper.py (mock faster-whisper)"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from steps.video.step_02_whisper import WhisperStep
from tests.steps.conftest import make_job_dir, make_step_config


class TestWhisperStep:
    def _setup(self, tmp_path):
        job_dir = make_job_dir(tmp_path, "input", "logs")
        (job_dir / "input" / "source.mp4").write_bytes(b"\x00" * 1024)
        return job_dir

    def test_validate_missing(self, tmp_path):
        job_dir = tmp_path / "job"
        job_dir.mkdir()
        (job_dir / "input").mkdir()
        config = make_step_config(tmp_path, step_name="02_whisper")
        step = WhisperStep("02_whisper", job_dir, config)
        assert step.validate_inputs() == ["input/source.mp4"]

    def test_validate_present(self, tmp_path):
        job_dir = self._setup(tmp_path)
        config = make_step_config(tmp_path, step_name="02_whisper")
        step = WhisperStep("02_whisper", job_dir, config)
        assert step.validate_inputs() == []

    @patch("steps.utils.device.has_nvidia_gpu", return_value=False)
    def test_execute_mock(self, mock_gpu, tmp_path, monkeypatch):
        job_dir = self._setup(tmp_path)
        config = make_step_config(tmp_path, step_name="02_whisper", pool="gpu")

        mock_segment = MagicMock()
        mock_segment.start = 0.0
        mock_segment.end = 2.5
        mock_segment.text = "你好世界"

        mock_info = MagicMock()
        mock_info.language = "zh"
        mock_info.duration = 2.5  # 进度分母走真实时长(不再写死 language="zh",自动检测)

        mock_model = MagicMock()
        mock_model.transcribe.return_value = ([mock_segment], mock_info)

        import sys
        mock_fw = MagicMock()
        mock_fw.WhisperModel.return_value = mock_model
        monkeypatch.setitem(sys.modules, "faster_whisper", mock_fw)

        step = WhisperStep("02_whisper", job_dir, config)
        result = step.execute()

        assert result["segments"] == 1
        assert result["language"] == "zh"
        srt = (job_dir / "input" / "subtitle.srt").read_text()
        assert "你好世界" in srt
        assert "00:00:00,000 --> 00:00:02,500" in srt

    def test_srt_timestamp_format(self, tmp_path):
        job_dir = self._setup(tmp_path)
        config = make_step_config(tmp_path, step_name="02_whisper")
        step = WhisperStep("02_whisper", job_dir, config)
        assert step._format_srt_ts(3661.5) == "01:01:01,500"
        assert step._format_srt_ts(0.0) == "00:00:00,000"
