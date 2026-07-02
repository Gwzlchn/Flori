"""OCR 引擎工厂:集中 RapidOCR 构造 + 后端选择,供 video 06_ocr 与 paper 04_figures 共用。"""

from __future__ import annotations


def create_ocr_engine():
    """构造 OCR 引擎(经 select_ocr_backend 统一选后端)。当前仅 rapidocr;未实现后端
    raise NotImplementedError;导入失败让异常传播,由调用方决定 catch→None 还是上抛。"""
    from steps.utils.device import select_ocr_backend

    backend = select_ocr_backend()
    if backend == "rapidocr":
        from rapidocr_onnxruntime import RapidOCR
        return RapidOCR()
    raise NotImplementedError(f"OCR backend {backend} not yet supported")
