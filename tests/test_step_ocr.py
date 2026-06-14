"""tests for steps/video/step_04_ocr.py — 置信度过滤。"""

from __future__ import annotations

from steps.video.step_04_ocr import OcrStep


def _step(tmp_path):
    cfg = {
        "step": {"name": "04_ocr", "pool": "cpu", "timeout_sec": 60, "retries": 0},
        "domain": {"ocr": {"confidence_threshold": 0.6}},
        "paths": {"data_dir": str(tmp_path)},
        "ai": {}, "providers": {},
    }
    return OcrStep("04_ocr", tmp_path, cfg)


def test_confidence_filter_drops_low(tmp_path):
    step = _step(tmp_path)
    fake_engine = lambda p: ([[[[0, 0]], "高置信文本", 0.9], [[[0, 0]], "低置信噪声", 0.3]], None)
    text, boxes = step._ocr_image(fake_engine, tmp_path / "x.jpg", threshold=0.6)
    assert "高置信文本" in text and "低置信噪声" not in text
    assert len(boxes) == 1 and boxes[0]["confidence"] == 0.9


def test_threshold_zero_keeps_all(tmp_path):
    step = _step(tmp_path)
    fake_engine = lambda p: ([[[[0, 0]], "a", 0.9], [[[0, 0]], "b", 0.1]], None)
    text, boxes = step._ocr_image(fake_engine, tmp_path / "x.jpg", threshold=0.0)
    assert len(boxes) == 2
