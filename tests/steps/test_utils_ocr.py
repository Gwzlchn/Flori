"""steps/utils/ocr.py 的测试:OCR 引擎工厂。

不触真实 RapidOCR;通过注入 fake 模块到 sys.modules + patch select_ocr_backend
覆盖三条路径:rapidocr 正常构造 / 未实现后端 raise / 导入失败异常传播。
"""

from __future__ import annotations

import sys
import types

import pytest

from steps.utils.ocr import create_ocr_engine


def _install_fake_rapidocr(monkeypatch, ctor):
    """把一个假的 rapidocr_onnxruntime 模块塞进 sys.modules,RapidOCR 由 ctor 提供。"""
    fake_mod = types.ModuleType("rapidocr_onnxruntime")
    fake_mod.RapidOCR = ctor
    monkeypatch.setitem(sys.modules, "rapidocr_onnxruntime", fake_mod)


class TestCreateOcrEngine:
    def test_rapidocr_backend_constructs_engine(self, monkeypatch):
        sentinel = object()
        # 用一个返回 sentinel 的工厂当作 RapidOCR 类。
        monkeypatch.setattr(
            "steps.utils.device.select_ocr_backend", lambda: "rapidocr"
        )
        _install_fake_rapidocr(monkeypatch, lambda: sentinel)

        engine = create_ocr_engine()
        assert engine is sentinel

    def test_unsupported_backend_raises_notimplemented(self, monkeypatch):
        monkeypatch.setattr(
            "steps.utils.device.select_ocr_backend", lambda: "paddleocr"
        )
        with pytest.raises(NotImplementedError) as exc:
            create_ocr_engine()
        assert "paddleocr" in str(exc.value)

    def test_import_failure_propagates(self, monkeypatch):
        # backend 为 rapidocr,但底层 import 失败 → 异常上抛(调用方决定 catch→None)。
        monkeypatch.setattr(
            "steps.utils.device.select_ocr_backend", lambda: "rapidocr"
        )

        broken = types.ModuleType("rapidocr_onnxruntime")
        # 故意不提供 RapidOCR 属性 → from ... import RapidOCR 触发 ImportError。
        monkeypatch.setitem(sys.modules, "rapidocr_onnxruntime", broken)

        with pytest.raises(ImportError):
            create_ocr_engine()

    def test_uses_select_ocr_backend_result(self, monkeypatch):
        # 验证后端字符串来自 select_ocr_backend(被调用)。
        calls = {"n": 0}

        def _backend():
            calls["n"] += 1
            return "rapidocr"

        monkeypatch.setattr("steps.utils.device.select_ocr_backend", _backend)
        marker = object()
        _install_fake_rapidocr(monkeypatch, lambda: marker)

        assert create_ocr_engine() is marker
        assert calls["n"] == 1
