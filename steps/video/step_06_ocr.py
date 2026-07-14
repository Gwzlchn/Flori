"""Step 06: OCR。RapidOCR (CPU) 或 PaddleOCR (GPU)。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from shared.step_base import StepBase, file_hash


class OcrStep(StepBase):
    def validate_inputs(self) -> list[str]:
        if not (self.job_dir / "intermediate" / "dedup.json").exists():
            return ["intermediate/dedup.json"]
        return []

    def input_hashes(self) -> dict[str, str]:
        return {
            "dedup": file_hash(self.job_dir / "intermediate" / "dedup.json"),
            "config": json.dumps(self.config.get("domain", {}).get("ocr", {}), sort_keys=True),
        }

    def execute(self) -> dict | None:
        dedup = self.artifacts.load_json("intermediate/dedup.json")
        assets_dir = self.job_dir / "assets"
        keep_frames = [d for d in dedup if d.get("keep", False)]

        ocr_engine = self._create_ocr_engine()
        # 置信度过滤:挡台标/花字等低置信噪声直灌下游 AI,经验基线 0.6。
        threshold = float(self.config.get("domain", {}).get("ocr", {}).get("confidence_threshold", 0.0))
        results = []
        nonempty = 0

        for i, frame in enumerate(keep_frames):
            self.progress.report(i, len(keep_frames), "OCR scanning")
            img_path = self._frame_path(assets_dir, frame.get("filename"))

            if img_path is None or not img_path.is_file():
                results.append({
                    "index": frame["index"],
                    "filename": frame["filename"],
                    "timestamp_sec": frame["timestamp_sec"],
                    "asset_sha256": None,
                    "width": None,
                    "height": None,
                    "text": "",
                    "boxes": [],
                })
                continue

            asset_sha256 = self._asset_sha256(img_path)
            width, height = self._image_size(img_path)
            text, boxes = self._ocr_image(ocr_engine, img_path, threshold)
            if (
                not img_path.is_file()
                or self._asset_sha256(img_path) != asset_sha256
            ):
                raise ValueError("OCR frame changed while it was being scanned")
            if text.strip():
                nonempty += 1
            results.append({
                "index": frame["index"],
                "filename": frame["filename"],
                "timestamp_sec": frame["timestamp_sec"],
                "asset_sha256": asset_sha256,
                "width": width,
                "height": height,
                "text": text,
                "boxes": boxes,
            })

        self.progress.report(len(keep_frames), len(keep_frames), "done")
        self.artifacts.write("intermediate/ocr.json", results)
        return {"total": len(results), "nonempty": nonempty}

    def _create_ocr_engine(self):
        # 严格语义:未实现后端由工厂 raise NotImplementedError,不静默降级(刻意防御)。
        from steps.utils.ocr import create_ocr_engine
        return create_ocr_engine()

    @staticmethod
    def _frame_path(assets_dir: Path, filename: object) -> Path | None:
        if (
            type(filename) is not str
            or not filename
            or "/" in filename
            or "\\" in filename
        ):
            return None
        root = assets_dir.resolve()
        path = (root / filename).resolve()
        return path if path.parent == root else None

    @staticmethod
    def _image_size(path: Path) -> tuple[int, int]:
        from PIL import Image

        with Image.open(path) as image:
            width, height = image.size
        if (
            type(width) is not int
            or type(height) is not int
            or width <= 0
            or height <= 0
        ):
            raise ValueError("OCR frame dimensions must be positive integers")
        return width, height

    @staticmethod
    def _asset_sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _ocr_image(self, engine, img_path: Path, threshold: float = 0.0) -> tuple[str, list[dict]]:
        try:
            result, _ = engine(str(img_path))
            if not result:
                return ("", [])

            texts = []
            boxes = []
            for item in result:
                box, text, confidence = item
                if confidence < threshold:
                    continue
                texts.append(text)
                boxes.append({
                    "text": text,
                    "confidence": round(confidence, 3),
                    "box": box,
                })
            return ("\n".join(texts), boxes)
        except Exception as e:
            self.log.warn("ocr_error", path=str(img_path), error=str(e))
            return ("", [])


if __name__ == "__main__":
    OcrStep.cli_main("06_ocr")
