"""steps/video/step_02_whisper.py 的测试,faster-whisper 全 mock。"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from steps.video.step_02_whisper import WhisperStep, _model_options, _model_source
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
        mock_info.duration = 2.5  # 进度分母走真实时长;language 靠自动检测,不写死

        mock_model = MagicMock()
        mock_model.transcribe.return_value = ([mock_segment], mock_info)

        import sys
        mock_fw = MagicMock()
        mock_fw.WhisperModel.return_value = mock_model
        monkeypatch.setitem(sys.modules, "faster_whisper", mock_fw)

        step = WhisperStep("02_whisper", job_dir, config)
        result = step.execute()

        mock_fw.WhisperModel.assert_called_once_with(
            "base", compute_type="int8", download_root=None,
        )
        assert result["segments"] == 1
        assert result["language"] == "zh"
        srt = (job_dir / "input" / "subtitle.srt").read_text()
        assert "你好世界" in srt
        assert "00:00:00,000 --> 00:00:02,500" in srt

    def test_local_model_path_bypasses_hub_lookup(self, tmp_path, monkeypatch):
        model_dir = tmp_path / "model"
        model_dir.mkdir()
        for name in ("config.json", "model.bin", "tokenizer.json", "vocabulary.txt"):
            (model_dir / name).write_bytes(b"model")
        monkeypatch.setenv("WHISPER_MODEL_PATH", str(model_dir))
        monkeypatch.setenv("WHISPER_MODEL_NAME", "base")

        assert _model_source("base") == str(model_dir)
        assert _model_options("base", "int8") == (
            str(model_dir), {"compute_type": "int8"},
        )

    @pytest.mark.parametrize("value", ["relative/model", "/missing/model"])
    def test_local_model_path_rejects_invalid_directory(self, value, monkeypatch):
        monkeypatch.setenv("WHISPER_MODEL_PATH", value)
        monkeypatch.setenv("WHISPER_MODEL_NAME", "base")

        with pytest.raises(RuntimeError, match="existing absolute directory"):
            _model_source("base")

    def test_local_model_path_requires_complete_snapshot(self, tmp_path, monkeypatch):
        model_dir = tmp_path / "model"
        model_dir.mkdir()
        (model_dir / "config.json").write_bytes(b"model")
        monkeypatch.setenv("WHISPER_MODEL_PATH", str(model_dir))
        monkeypatch.setenv("WHISPER_MODEL_NAME", "base")

        with pytest.raises(RuntimeError, match="misses required files"):
            _model_source("base")

    @pytest.mark.parametrize("name", [None, "large-v3"])
    def test_local_model_path_rejects_model_mismatch(self, tmp_path, monkeypatch, name):
        model_dir = tmp_path / "model"
        model_dir.mkdir()
        monkeypatch.setenv("WHISPER_MODEL_PATH", str(model_dir))
        if name is not None:
            monkeypatch.setenv("WHISPER_MODEL_NAME", name)

        with pytest.raises(RuntimeError, match="must match the selected model"):
            _model_source("base")

    @patch("steps.utils.device.has_nvidia_gpu", return_value=False)
    def test_execute_uses_local_model_without_download_root(
        self, mock_gpu, tmp_path, monkeypatch,
    ):
        job_dir = self._setup(tmp_path)
        config = make_step_config(tmp_path, step_name="02_whisper", pool="cpu")
        model_dir = tmp_path / "model"
        model_dir.mkdir()
        for name in ("config.json", "model.bin", "tokenizer.json", "vocabulary.txt"):
            (model_dir / name).write_bytes(b"model")
        monkeypatch.setenv("WHISPER_MODEL_PATH", str(model_dir))
        monkeypatch.setenv("WHISPER_MODEL_NAME", "base")

        mock_model = MagicMock()
        mock_info = MagicMock(language="en", duration=0)
        mock_model.transcribe.return_value = ([], mock_info)
        mock_fw = MagicMock()
        mock_fw.WhisperModel.return_value = mock_model
        monkeypatch.setitem(__import__("sys").modules, "faster_whisper", mock_fw)

        result = WhisperStep("02_whisper", job_dir, config).execute()

        mock_fw.WhisperModel.assert_called_once_with(
            str(model_dir), compute_type="int8",
        )
        assert result == {"segments": 0, "language": "en", "model": "base"}

    def test_srt_timestamp_format(self, tmp_path):
        job_dir = self._setup(tmp_path)
        config = make_step_config(tmp_path, step_name="02_whisper")
        step = WhisperStep("02_whisper", job_dir, config)
        assert step._format_srt_ts(3661.5) == "01:01:01,500"
        assert step._format_srt_ts(0.0) == "00:00:00,000"
