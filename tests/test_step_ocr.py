"""tests for steps/video/step_06_ocr.py — 置信度过滤。"""

from __future__ import annotations

from steps.video.step_06_ocr import OcrStep


def _step(tmp_path):
    cfg = {
        "step": {"name": "06_ocr", "pool": "cpu", "timeout_sec": 60, "retries": 0},
        "domain": {"ocr": {"confidence_threshold": 0.6}},
        "paths": {"data_dir": str(tmp_path)},
        "ai": {}, "providers": {},
    }
    return OcrStep("06_ocr", tmp_path, cfg)


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
    assert text == "a\nb"   # 不止数 box:多行按出现顺序用 \n 拼接


def test_multi_box_joined_with_newline(tmp_path):
    # confidence_filter_drops_low 只剩 1 个 box,验不到拼接;这里 3 个 box 全过阈值,
    # 钉死"\n".join 的换行与顺序(防被改成空格/" "/反序而存活)。
    step = _step(tmp_path)
    fake_engine = lambda p: (
        [[[[0, 0]], "第一行", 0.9], [[[0, 0]], "第二行", 0.8], [[[0, 0]], "第三行", 0.7]],
        None,
    )
    text, boxes = step._ocr_image(fake_engine, tmp_path / "x.jpg", threshold=0.6)
    assert len(boxes) == 3
    assert text == "第一行\n第二行\n第三行"
