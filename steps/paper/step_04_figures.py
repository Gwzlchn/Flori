"""Step 12: 图表提取。从 PDF 裁切图片 + OCR 文字标注。"""

from __future__ import annotations

import json
from pathlib import Path

from shared.step_base import StepBase, file_hash


class FiguresStep(StepBase):
    def validate_inputs(self) -> list[str]:
        missing = []
        if not (self.job_dir / "intermediate" / "parsed.json").exists():
            missing.append("intermediate/parsed.json")
        if not (self.job_dir / "input" / "source.pdf").exists():
            missing.append("input/source.pdf")
        return missing

    def input_hashes(self) -> dict[str, str]:
        return {
            "parsed": file_hash(self.job_dir / "intermediate" / "parsed.json"),
            "pdf": file_hash(self.job_dir / "input" / "source.pdf"),
        }

    def execute(self) -> dict | None:
        import fitz

        parsed = self.load_json("intermediate/parsed.json")
        pdf_path = self.job_dir / "input" / "source.pdf"
        assets_dir = self.job_dir / "assets"
        assets_dir.mkdir(parents=True, exist_ok=True)

        with fitz.open(str(pdf_path)) as doc:
            extracted = self._extract_images_from_pdf(doc, assets_dir)

        figures_info = parsed.get("figures", [])
        results = []
        ocr_engine = self._create_ocr_engine()

        for i, fig in enumerate(figures_info):
            self.report_progress(i, len(figures_info), "processing figures")
            fig_id = fig.get("id", f"fig{i + 1}")
            page = fig.get("page", 1)
            caption = fig.get("caption", "")

            img_filename = None
            for ext_img in extracted:
                if ext_img["page"] == page:
                    img_filename = ext_img["filename"]
                    break

            entry = {
                "id": fig_id,
                "page": page,
                "caption": caption,
                "filename": img_filename,
                "ocr_text": "",
            }

            if img_filename:
                img_path = assets_dir / img_filename
                if img_path.exists():
                    entry["ocr_text"] = self._ocr_figure(ocr_engine, img_path)

            results.append(entry)

        self.report_progress(len(figures_info), len(figures_info), "done")
        self.write_output("intermediate/figures.json", results)
        return {"figures": len(results), "with_image": sum(1 for r in results if r["filename"])}

    def _extract_images_from_pdf(self, doc, assets_dir: Path) -> list[dict]:
        extracted = []
        img_index = 0

        for page_num in range(len(doc)):
            page = doc[page_num]
            images = page.get_images(full=True)

            for img_info in images:
                xref = img_info[0]
                try:
                    pix = fitz.Pixmap(doc, xref)
                    if pix.n < 5:
                        filename = f"fig_{img_index:03d}_p{page_num + 1}.png"
                        pix.save(str(assets_dir / filename))
                    else:
                        pix2 = fitz.Pixmap(fitz.csRGB, pix)
                        filename = f"fig_{img_index:03d}_p{page_num + 1}.png"
                        pix2.save(str(assets_dir / filename))

                    if (assets_dir / filename).stat().st_size > 1024:
                        extracted.append({
                            "page": page_num + 1,
                            "filename": filename,
                        })
                        img_index += 1
                except Exception as e:
                    self.log.warning("figure_extract_error", page=page_num + 1, error=str(e))
                    continue

        return extracted

    def _create_ocr_engine(self):
        try:
            from rapidocr_onnxruntime import RapidOCR
            return RapidOCR()
        except Exception as e:
            self.log.warning("ocr_engine_init_failed", error=str(e))
            return None

    def _ocr_figure(self, engine, img_path: Path) -> str:
        if engine is None:
            return ""
        try:
            result, _ = engine(str(img_path))
            if not result:
                return ""
            return "\n".join(item[1] for item in result)
        except Exception as e:
            self.log.warning("ocr_figure_error", path=str(img_path), error=str(e))
            return ""


if __name__ == "__main__":
    FiguresStep.cli_main("04_figures")
