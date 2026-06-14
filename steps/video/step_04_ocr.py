"""Step 04: OCR。RapidOCR (CPU) 或 PaddleOCR (GPU)。"""

from __future__ import annotations

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
        dedup = self.load_json("intermediate/dedup.json")
        assets_dir = self.job_dir / "assets"
        keep_frames = [d for d in dedup if d.get("keep", False)]

        ocr_engine = self._create_ocr_engine()
        # 置信度过滤:挡台标/花字等低置信噪声直灌下游 AI(老原型基线 0.6)。
        threshold = float(self.config.get("domain", {}).get("ocr", {}).get("confidence_threshold", 0.0))
        results = []
        nonempty = 0

        for i, frame in enumerate(keep_frames):
            self.report_progress(i, len(keep_frames), "OCR scanning")
            img_path = assets_dir / frame["filename"]

            if not img_path.exists():
                results.append({
                    "index": frame["index"],
                    "filename": frame["filename"],
                    "timestamp_sec": frame["timestamp_sec"],
                    "text": "",
                    "boxes": [],
                })
                continue

            text, boxes = self._ocr_image(ocr_engine, img_path, threshold)
            if text.strip():
                nonempty += 1
            results.append({
                "index": frame["index"],
                "filename": frame["filename"],
                "timestamp_sec": frame["timestamp_sec"],
                "text": text,
                "boxes": boxes,
            })

        self.report_progress(len(keep_frames), len(keep_frames), "done")
        self.write_output("intermediate/ocr.json", results)
        return {"total": len(results), "nonempty": nonempty}

    def _create_ocr_engine(self):
        from steps.utils.device import select_ocr_backend
        backend = select_ocr_backend()

        if backend == "rapidocr":
            from rapidocr_onnxruntime import RapidOCR
            return RapidOCR()
        else:
            raise NotImplementedError(f"OCR backend {backend} not yet supported")

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
    OcrStep.cli_main("04_ocr")
