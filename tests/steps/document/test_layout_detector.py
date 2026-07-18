"""验证 DocLayout-YOLO ONNX 的 CPU 输入输出和 fail-closed 边界。"""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from steps.document.layout_detector import (
    DocumentLayoutDetector,
    LayoutDetectorError,
    _model_names,
)


_NAMES = {
    0: "title",
    1: "plain text",
    2: "abandon",
    3: "figure",
    4: "figure_caption",
    5: "table",
    6: "table_caption",
    7: "table_footnote",
    8: "isolate_formula",
    9: "formula_caption",
}


class _FakeSession:
    def __init__(self, output: np.ndarray) -> None:
        self.output = output
        self.last_tensor: np.ndarray | None = None

    @staticmethod
    def get_inputs() -> list[SimpleNamespace]:
        return [SimpleNamespace(name="images")]

    @staticmethod
    def get_outputs() -> list[SimpleNamespace]:
        return [SimpleNamespace(name="output0")]

    @staticmethod
    def get_modelmeta() -> SimpleNamespace:
        return SimpleNamespace(custom_metadata_map={"names": repr(_NAMES)})

    def run(self, _outputs: object, inputs: dict[str, np.ndarray]) -> list[np.ndarray]:
        self.last_tensor = inputs["images"]
        return [self.output]


def _detector(
    tmp_path: Path,
    output: np.ndarray,
    *,
    expected_sha256: str | None = None,
) -> tuple[DocumentLayoutDetector, _FakeSession]:
    model = tmp_path / "layout.onnx"
    model.write_bytes(b"fixed-model")
    session = _FakeSession(output)
    detector = DocumentLayoutDetector(
        model,
        expected_sha256=expected_sha256,
        confidence=0.2,
        threads=2,
        session_factory=lambda _path, **_kwargs: session,
    )
    return detector, session


def test_detect_image_letterboxes_and_maps_boxes_to_pdf_points(tmp_path: Path) -> None:
    gain = 1024 / 200
    pad_left = 256
    figure = [
        10 * gain + pad_left, 20 * gain,
        90 * gain + pad_left, 120 * gain,
        0.9, 3,
    ]
    ignored = [pad_left, 0, pad_left + 10, 10, 0.1, 5]
    detector, session = _detector(
        tmp_path, np.asarray([[figure, ignored]], dtype=np.float32),
    )
    image = np.zeros((200, 100, 3), dtype=np.uint8)

    detections = detector.detect_image(
        image, page_width=50, page_height=100,
    )

    assert session.last_tensor is not None
    assert session.last_tensor.shape == (1, 3, 1024, 1024)
    assert session.last_tensor.dtype == np.float32
    assert len(detections) == 1
    assert detections[0].kind == "figure"
    assert detections[0].confidence == pytest.approx(0.9)
    assert detections[0].bbox == pytest.approx((5, 10, 45, 60), abs=0.01)


def test_model_checksum_mismatch_blocks_session_loading(tmp_path: Path) -> None:
    detector, _session = _detector(
        tmp_path,
        np.empty((1, 0, 6), dtype=np.float32),
        expected_sha256=hashlib.sha256(b"different").hexdigest(),
    )

    with pytest.raises(LayoutDetectorError, match="checksum"):
        detector.detect_image(
            np.zeros((20, 20, 3), dtype=np.uint8),
            page_width=20,
            page_height=20,
        )


def test_unknown_model_label_is_rejected() -> None:
    with pytest.raises(LayoutDetectorError, match="unknown label"):
        _model_names({**_NAMES, 10: "malicious"})


def test_non_finite_output_rejects_entire_page(tmp_path: Path) -> None:
    output = np.asarray([[[0, 0, 10, 10, np.nan, 3]]], dtype=np.float32)
    detector, _session = _detector(tmp_path, output)

    with pytest.raises(LayoutDetectorError, match="non-finite"):
        detector.detect_image(
            np.zeros((20, 20, 3), dtype=np.uint8),
            page_width=20,
            page_height=20,
        )
