"""GPU/CPU 检测,决定 OCR/Whisper 使用路径。"""

from __future__ import annotations

import os
import shutil
import subprocess


def has_nvidia_gpu() -> bool:
    if not shutil.which("nvidia-smi"):
        return False
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10,
        )
        return result.returncode == 0 and result.stdout.strip() != ""
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def gpu_memory_mb() -> int:
    """返回第一块 GPU 的显存 MB,无 GPU 返回 0。"""
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            first_line = result.stdout.strip().splitlines()[0]
            return int(first_line.strip())
    except (subprocess.TimeoutExpired, FileNotFoundError, ValueError, IndexError):
        pass
    return 0


def select_whisper_model() -> tuple[str, str]:
    """按显存分档选 Whisper 模型,返回 (model_size, compute_type);无 GPU 回退 base+int8。"""
    if not has_nvidia_gpu():
        return ("base", "int8")
    mem = gpu_memory_mb()
    if mem >= 10000:
        return ("large-v3", "float16")
    elif mem >= 6000:
        return ("medium", "float16")
    else:
        return ("small", "float16")


def select_ocr_backend() -> str:
    """选择 OCR 后端。当前仅实现 rapidocr(CPU/ONNX);paddleocr-GPU 尚未接入,
    故即便 GPU 机误设 USE_PADDLE_OCR=1 也回退 rapidocr 并告警,绝不返回会让 OCR 步崩溃的后端。
    step_06_ocr 对未实现后端 raise NotImplementedError,此处在源头挡掉。"""
    if has_nvidia_gpu() and os.environ.get("USE_PADDLE_OCR") == "1":
        import structlog
        structlog.get_logger().warning(
            "paddleocr_not_implemented",
            msg="USE_PADDLE_OCR=1 但 paddleocr-GPU 尚未接入(M5),本次回退 rapidocr",
        )
    return "rapidocr"
