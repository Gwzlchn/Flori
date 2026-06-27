"""Step 04: 图表提取。从 PDF 裁切图片 + OCR 文字标注。"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from shared.step_base import StepBase, file_hash

# 编程错误(代码 bug)永远不该被"图表可缺省"的宽松 catch 吞成 warning 后照常 done——否则像 fitz 未导入
# 致 NameError 这种 bug 会静默抽 0 图、还查不到。这些类型一律【重抛 fail-loud】;只放行预期的数据/环境
# 降级(损坏图、不支持色彩空间、缺 OCR 后端 ImportError)。
_BUG_ERRORS = (NameError, AttributeError, TypeError)


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

        # 同页可有多张内嵌位图与多条图注:按页建可消费队列,逐图注取「下一张未用」位图,
        # 而非每条图注都命中该页第一张(否则多图注引用同一图/图文错配)。
        page_pool: dict[int, list[dict]] = defaultdict(list)
        for ext_img in extracted:
            page_pool[ext_img["page"]].append(ext_img)

        for i, fig in enumerate(figures_info):
            self.report_progress(i, len(figures_info), "processing figures")
            fig_id = fig.get("id", f"fig{i + 1}")
            page = fig.get("page", 1)
            caption = fig.get("caption", "")

            img_filename = None
            img_idx = None
            pool = page_pool.get(page)
            if pool:  # 同页图注多于位图时,后续图注取不到 → 保持 None(优雅降级)
                ext_img = pool.pop(0)
                img_filename = ext_img["filename"]
                img_idx = ext_img["index"]

            entry = {
                "id": fig_id,
                "page": page,
                "caption": caption,
                "filename": img_filename,
                "index": img_idx,   # 占位符 [img:N] 的 N;无内嵌位图时为 None
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
        import fitz  # 本方法用 fitz.Pixmap/csRGB;execute() 的 import 不及于此作用域(曾致 NameError 静默吞图)
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
                        filename = f"figure-{img_index:04d}.png"
                        pix.save(str(assets_dir / filename))
                    else:
                        pix2 = fitz.Pixmap(fitz.csRGB, pix)
                        filename = f"figure-{img_index:04d}.png"
                        pix2.save(str(assets_dir / filename))

                    if (assets_dir / filename).stat().st_size > 1024:
                        extracted.append({
                            "page": page_num + 1,
                            "filename": filename,
                            "index": img_index,   # 稳定资产序号 = 占位符 [img:N] 的 N
                        })
                        img_index += 1
                except _BUG_ERRORS:
                    raise   # 代码 bug(如 fitz 未导入)→ fail-loud,不当"缺图"吞掉
                except Exception as e:
                    # 单张图的数据问题(损坏/不支持色彩空间等)→ 跳过该图,不阻断本步。
                    self.log.warning("figure_extract_error", page=page_num + 1, error=str(e))
                    continue

        return extracted

    def _create_ocr_engine(self):
        # 宽松语义:构造失败(含未实现后端/缺库)记日志返 None,图表 OCR 可缺省不阻断本步。
        from steps.utils.ocr import create_ocr_engine
        try:
            return create_ocr_engine()
        except _BUG_ERRORS:
            raise   # 代码 bug → fail-loud(缺 OCR 后端是 ImportError,仍走下面优雅降级)
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
        except _BUG_ERRORS:
            raise   # 代码 bug → fail-loud
        except Exception as e:
            self.log.warning("ocr_figure_error", path=str(img_path), error=str(e))
            return ""


if __name__ == "__main__":
    FiguresStep.cli_main("04_figures")
