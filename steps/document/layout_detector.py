"""用固定 ONNX 模型在 CPU 上检测 PDF 页面的图表候选边界。"""

from __future__ import annotations

import ast
import hashlib
import math
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


MODEL_PATH_ENV = "FLORI_DOCUMENT_LAYOUT_MODEL"
MODEL_SHA256_ENV = "FLORI_DOCUMENT_LAYOUT_MODEL_SHA256"
MODEL_CONFIDENCE_ENV = "FLORI_DOCUMENT_LAYOUT_CONFIDENCE"
MODEL_THREADS_ENV = "FLORI_DOCUMENT_LAYOUT_THREADS"
MODEL_INPUT_SIZE = 1024
PDF_RENDER_DPI = 144
_KNOWN_LABELS = {
    "title", "plain text", "abandon", "figure", "figure_caption", "table",
    "table_caption", "table_footnote", "isolate_formula", "formula_caption",
}


class LayoutDetectorError(RuntimeError):
    """模型、渲染或输出不可信时停止使用检测结果。"""


@dataclass(frozen=True)
class LayoutDetection:
    kind: str
    confidence: float
    bbox: tuple[float, float, float, float]


def _positive_int(value: str | None, default: int) -> int:
    try:
        parsed = int(value or default)
    except ValueError:
        return default
    return parsed if parsed > 0 else default


def _confidence(value: str | None) -> float:
    try:
        parsed = float(value or 0.2)
    except ValueError:
        return 0.2
    return parsed if math.isfinite(parsed) and 0 < parsed < 1 else 0.2


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _model_names(raw: object) -> dict[int, str]:
    try:
        parsed = ast.literal_eval(str(raw or ""))
    except (SyntaxError, ValueError) as exc:
        raise LayoutDetectorError("layout model names metadata is invalid") from exc
    if not isinstance(parsed, dict):
        raise LayoutDetectorError("layout model names metadata is not a mapping")
    names: dict[int, str] = {}
    for key, value in parsed.items():
        if type(key) is not int or type(value) is not str or value not in _KNOWN_LABELS:
            raise LayoutDetectorError("layout model exposes an unknown label")
        names[key] = value
    if not {"figure", "figure_caption", "table", "table_caption"} <= set(names.values()):
        raise LayoutDetectorError("layout model misses required visual labels")
    return names


class DocumentLayoutDetector:
    """把模型像素框换算回 PDF point 坐标,输出只作候选而非语义真相。"""

    def __init__(
        self,
        model_path: Path,
        *,
        expected_sha256: str | None = None,
        confidence: float = 0.2,
        threads: int = 4,
        session_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.model_path = model_path
        self.expected_sha256 = str(expected_sha256 or "").removeprefix("sha256:")
        self.confidence = confidence
        self.threads = threads
        self._session_factory = session_factory
        self._session: Any | None = None
        self._names: dict[int, str] = {}

    @classmethod
    def from_env(cls) -> DocumentLayoutDetector | None:
        raw_path = os.environ.get(MODEL_PATH_ENV, "").strip()
        if not raw_path:
            return None
        return cls(
            Path(raw_path),
            expected_sha256=os.environ.get(MODEL_SHA256_ENV),
            confidence=_confidence(os.environ.get(MODEL_CONFIDENCE_ENV)),
            threads=_positive_int(
                os.environ.get(MODEL_THREADS_ENV),
                min(4, max(1, os.cpu_count() or 1)),
            ),
        )

    @property
    def model_identity(self) -> str:
        return "sha256:" + self.expected_sha256 if self.expected_sha256 else self.model_path.name

    def _load_session(self) -> Any:
        if self._session is not None:
            return self._session
        if not self.model_path.is_file() or self.model_path.stat().st_size <= 0:
            raise LayoutDetectorError("layout model file is unavailable")
        if self.expected_sha256 and _file_sha256(self.model_path) != self.expected_sha256:
            raise LayoutDetectorError("layout model checksum does not match")
        try:
            if self._session_factory is None:
                import onnxruntime as ort

                options = ort.SessionOptions()
                options.intra_op_num_threads = self.threads
                options.inter_op_num_threads = 1
                session = ort.InferenceSession(
                    str(self.model_path),
                    sess_options=options,
                    providers=["CPUExecutionProvider"],
                )
            else:
                session = self._session_factory(
                    self.model_path,
                    threads=self.threads,
                )
            inputs = session.get_inputs()
            outputs = session.get_outputs()
            if len(inputs) != 1 or not outputs or inputs[0].name != "images":
                raise LayoutDetectorError("layout model IO contract is unsupported")
            metadata = session.get_modelmeta().custom_metadata_map
            self._names = _model_names(metadata.get("names"))
        except LayoutDetectorError:
            raise
        except Exception as exc:
            raise LayoutDetectorError("layout model cannot be loaded") from exc
        self._session = session
        return session

    @staticmethod
    def _letterbox(image: Any) -> tuple[Any, float, int, int]:
        import cv2

        height, width = image.shape[:2]
        if height <= 0 or width <= 0:
            raise LayoutDetectorError("layout page image is empty")
        gain = min(MODEL_INPUT_SIZE / height, MODEL_INPUT_SIZE / width)
        resized_height = max(1, round(height * gain))
        resized_width = max(1, round(width * gain))
        resized = cv2.resize(
            image, (resized_width, resized_height), interpolation=cv2.INTER_LINEAR,
        )
        pad_top = (MODEL_INPUT_SIZE - resized_height) // 2
        pad_left = (MODEL_INPUT_SIZE - resized_width) // 2
        padded = cv2.copyMakeBorder(
            resized,
            pad_top,
            MODEL_INPUT_SIZE - resized_height - pad_top,
            pad_left,
            MODEL_INPUT_SIZE - resized_width - pad_left,
            cv2.BORDER_CONSTANT,
            value=(114, 114, 114),
        )
        return padded, gain, pad_left, pad_top

    def detect_image(
        self,
        image: Any,
        *,
        page_width: float,
        page_height: float,
    ) -> list[LayoutDetection]:
        """检测已渲染页面并换算坐标;异常输出整体拒绝,不返回部分结果。"""
        import cv2
        import numpy as np

        if page_width <= 0 or page_height <= 0:
            raise LayoutDetectorError("layout PDF page dimensions are invalid")
        original_height, original_width = image.shape[:2]
        padded, gain, pad_left, pad_top = self._letterbox(image)
        tensor = cv2.cvtColor(padded, cv2.COLOR_BGR2RGB)
        tensor = np.transpose(tensor, (2, 0, 1))[None].astype(np.float32) / 255.0
        session = self._load_session()
        try:
            output = session.run(None, {"images": tensor})[0]
        except Exception as exc:
            raise LayoutDetectorError("layout model inference failed") from exc
        if (
            not isinstance(output, np.ndarray)
            or output.ndim != 3
            or output.shape[0] != 1
            or output.shape[2] != 6
        ):
            raise LayoutDetectorError("layout model output contract is unsupported")

        detections: list[LayoutDetection] = []
        for raw in output[0]:
            values = [float(value) for value in raw]
            if not all(math.isfinite(value) for value in values):
                raise LayoutDetectorError("layout model output contains non-finite values")
            x0, y0, x1, y1, score, raw_class = values
            if score < self.confidence:
                continue
            class_id = round(raw_class)
            if abs(raw_class - class_id) > 1e-4 or class_id not in self._names:
                raise LayoutDetectorError("layout model output contains an unknown class")
            pixel_box = [
                (x0 - pad_left) / gain,
                (y0 - pad_top) / gain,
                (x1 - pad_left) / gain,
                (y1 - pad_top) / gain,
            ]
            pdf_box = (
                max(0.0, min(page_width, pixel_box[0] * page_width / original_width)),
                max(0.0, min(page_height, pixel_box[1] * page_height / original_height)),
                max(0.0, min(page_width, pixel_box[2] * page_width / original_width)),
                max(0.0, min(page_height, pixel_box[3] * page_height / original_height)),
            )
            if pdf_box[2] <= pdf_box[0] or pdf_box[3] <= pdf_box[1]:
                continue
            detections.append(LayoutDetection(
                kind=self._names[class_id],
                confidence=round(score, 6),
                bbox=tuple(round(value, 3) for value in pdf_box),
            ))
        return sorted(
            detections,
            key=lambda item: (item.bbox[1], item.bbox[0], -item.confidence, item.kind),
        )

    def detect_pdf_page(
        self,
        source: Path,
        *,
        page: int,
        page_width: float,
        page_height: float,
    ) -> list[LayoutDetection]:
        """只渲染目标页;模型失败由调用方显式降级到 PDF 几何算法。"""
        if page <= 0:
            raise LayoutDetectorError("layout PDF page number is invalid")
        try:
            import cv2

            with tempfile.TemporaryDirectory(prefix="flori-pdf-layout-model-") as temp_dir:
                prefix = Path(temp_dir) / "page"
                subprocess.run(
                    [
                        "pdftoppm", "-f", str(page), "-l", str(page),
                        "-r", str(PDF_RENDER_DPI), "-png", "-singlefile",
                        str(source), str(prefix),
                    ],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    timeout=120,
                )
                image = cv2.imread(str(prefix.with_suffix(".png")), cv2.IMREAD_COLOR)
                if image is None:
                    raise LayoutDetectorError("layout PDF page cannot be decoded")
                return self.detect_image(
                    image, page_width=page_width, page_height=page_height,
                )
        except LayoutDetectorError:
            raise
        except (OSError, subprocess.SubprocessError, TimeoutError) as exc:
            raise LayoutDetectorError("layout PDF page cannot be rendered") from exc
