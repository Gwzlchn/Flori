"""tests for steps/utils/device.py — GPU/CPU 检测 + Whisper/OCR 后端选择分支。

全程 monkeypatch shutil.which / subprocess.run,不触真实 nvidia-smi,保证确定性。
"""

from __future__ import annotations

import subprocess

import pytest

from steps.utils import device


def _fake_run(stdout="", returncode=0):
    """构造一个返回固定结果的 subprocess.run 替身。"""
    def _run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0] if args else [], returncode=returncode, stdout=stdout, stderr=""
        )
    return _run


class TestHasNvidiaGpu:
    def test_no_nvidia_smi_binary(self, monkeypatch):
        # which 返回 None → 直接 False,不调用 subprocess。
        monkeypatch.setattr(device.shutil, "which", lambda name: None)
        called = {"run": False}

        def _boom(*a, **k):
            called["run"] = True
            raise AssertionError("subprocess.run should not be called")

        monkeypatch.setattr(device.subprocess, "run", _boom)
        assert device.has_nvidia_gpu() is False
        assert called["run"] is False

    def test_smi_success_with_output(self, monkeypatch):
        monkeypatch.setattr(device.shutil, "which", lambda name: "/usr/bin/nvidia-smi")
        monkeypatch.setattr(
            device.subprocess, "run", _fake_run(stdout="24576\n", returncode=0)
        )
        assert device.has_nvidia_gpu() is True

    def test_smi_success_but_empty_output(self, monkeypatch):
        # returncode 0 但 stdout 为空(被驱动异常等情况)→ False。
        monkeypatch.setattr(device.shutil, "which", lambda name: "/usr/bin/nvidia-smi")
        monkeypatch.setattr(
            device.subprocess, "run", _fake_run(stdout="   \n", returncode=0)
        )
        assert device.has_nvidia_gpu() is False

    def test_smi_nonzero_returncode(self, monkeypatch):
        monkeypatch.setattr(device.shutil, "which", lambda name: "/usr/bin/nvidia-smi")
        monkeypatch.setattr(
            device.subprocess, "run", _fake_run(stdout="24576\n", returncode=1)
        )
        assert device.has_nvidia_gpu() is False

    def test_smi_timeout(self, monkeypatch):
        monkeypatch.setattr(device.shutil, "which", lambda name: "/usr/bin/nvidia-smi")

        def _timeout(*a, **k):
            raise subprocess.TimeoutExpired(cmd="nvidia-smi", timeout=10)

        monkeypatch.setattr(device.subprocess, "run", _timeout)
        assert device.has_nvidia_gpu() is False

    def test_smi_filenotfound(self, monkeypatch):
        # which 命中但执行时二进制消失(竞态)→ FileNotFoundError 被吞,返回 False。
        monkeypatch.setattr(device.shutil, "which", lambda name: "/usr/bin/nvidia-smi")

        def _missing(*a, **k):
            raise FileNotFoundError("nvidia-smi")

        monkeypatch.setattr(device.subprocess, "run", _missing)
        assert device.has_nvidia_gpu() is False


class TestGpuMemoryMb:
    def test_parses_first_line(self, monkeypatch):
        monkeypatch.setattr(
            device.subprocess, "run", _fake_run(stdout="24576\n8192\n", returncode=0)
        )
        assert device.gpu_memory_mb() == 24576

    def test_strips_whitespace(self, monkeypatch):
        monkeypatch.setattr(
            device.subprocess, "run", _fake_run(stdout="  16384  \n", returncode=0)
        )
        assert device.gpu_memory_mb() == 16384

    def test_nonzero_returncode_returns_zero(self, monkeypatch):
        monkeypatch.setattr(
            device.subprocess, "run", _fake_run(stdout="24576\n", returncode=2)
        )
        assert device.gpu_memory_mb() == 0

    def test_non_integer_output_returns_zero(self, monkeypatch):
        # int() 抛 ValueError → 被吞,返回 0。
        monkeypatch.setattr(
            device.subprocess, "run", _fake_run(stdout="N/A\n", returncode=0)
        )
        assert device.gpu_memory_mb() == 0

    def test_empty_output_returns_zero(self, monkeypatch):
        # splitlines()[0] 抛 IndexError → 被吞,返回 0。
        monkeypatch.setattr(
            device.subprocess, "run", _fake_run(stdout="", returncode=0)
        )
        assert device.gpu_memory_mb() == 0

    def test_timeout_returns_zero(self, monkeypatch):
        def _timeout(*a, **k):
            raise subprocess.TimeoutExpired(cmd="nvidia-smi", timeout=10)

        monkeypatch.setattr(device.subprocess, "run", _timeout)
        assert device.gpu_memory_mb() == 0

    def test_filenotfound_returns_zero(self, monkeypatch):
        def _missing(*a, **k):
            raise FileNotFoundError("nvidia-smi")

        monkeypatch.setattr(device.subprocess, "run", _missing)
        assert device.gpu_memory_mb() == 0


class TestSelectWhisperModel:
    def test_no_gpu_uses_base_int8(self, monkeypatch):
        monkeypatch.setattr(device, "has_nvidia_gpu", lambda: False)
        # gpu_memory_mb 不应被调用(短路),但即便调用也无副作用。
        assert device.select_whisper_model() == ("base", "int8")

    def test_large_gpu(self, monkeypatch):
        monkeypatch.setattr(device, "has_nvidia_gpu", lambda: True)
        monkeypatch.setattr(device, "gpu_memory_mb", lambda: 24576)
        assert device.select_whisper_model() == ("large-v3", "float16")

    def test_boundary_10000_is_large(self, monkeypatch):
        monkeypatch.setattr(device, "has_nvidia_gpu", lambda: True)
        monkeypatch.setattr(device, "gpu_memory_mb", lambda: 10000)
        assert device.select_whisper_model() == ("large-v3", "float16")

    def test_medium_gpu(self, monkeypatch):
        monkeypatch.setattr(device, "has_nvidia_gpu", lambda: True)
        monkeypatch.setattr(device, "gpu_memory_mb", lambda: 8000)
        assert device.select_whisper_model() == ("medium", "float16")

    def test_boundary_6000_is_medium(self, monkeypatch):
        monkeypatch.setattr(device, "has_nvidia_gpu", lambda: True)
        monkeypatch.setattr(device, "gpu_memory_mb", lambda: 6000)
        assert device.select_whisper_model() == ("medium", "float16")

    def test_small_gpu(self, monkeypatch):
        monkeypatch.setattr(device, "has_nvidia_gpu", lambda: True)
        monkeypatch.setattr(device, "gpu_memory_mb", lambda: 4096)
        assert device.select_whisper_model() == ("small", "float16")


class TestSelectOcrBackend:
    def test_no_gpu_returns_rapidocr(self, monkeypatch):
        monkeypatch.setattr(device, "has_nvidia_gpu", lambda: False)
        monkeypatch.setenv("USE_PADDLE_OCR", "1")  # 即便置位,无 GPU 也不告警
        assert device.select_ocr_backend() == "rapidocr"

    def test_gpu_without_paddle_flag(self, monkeypatch):
        monkeypatch.setattr(device, "has_nvidia_gpu", lambda: True)
        monkeypatch.delenv("USE_PADDLE_OCR", raising=False)
        assert device.select_ocr_backend() == "rapidocr"

    def test_gpu_with_paddle_flag_warns_but_falls_back(self, monkeypatch):
        # GPU + USE_PADDLE_OCR=1 → 走告警分支,但仍回退 rapidocr(绝不返回未实现后端)。
        monkeypatch.setattr(device, "has_nvidia_gpu", lambda: True)
        monkeypatch.setenv("USE_PADDLE_OCR", "1")

        warnings = []

        class _FakeLogger:
            def warning(self, event, **kw):
                warnings.append((event, kw))

        import structlog

        monkeypatch.setattr(structlog, "get_logger", lambda *a, **k: _FakeLogger())
        assert device.select_ocr_backend() == "rapidocr"
        assert warnings and warnings[0][0] == "paddleocr_not_implemented"

    def test_gpu_with_paddle_flag_wrong_value(self, monkeypatch):
        # 值非 "1" 不触发告警分支。
        monkeypatch.setattr(device, "has_nvidia_gpu", lambda: True)
        monkeypatch.setenv("USE_PADDLE_OCR", "true")
        assert device.select_ocr_backend() == "rapidocr"
