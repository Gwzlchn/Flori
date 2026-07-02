"""Step 04: 图表提取。从 PDF 裁切图片 + OCR 文字标注。"""

from __future__ import annotations

import json
from pathlib import Path

from shared.step_base import StepBase, file_hash

# 编程错误(代码 bug)永远不该被"图表可缺省"的宽松 catch 吞成 warning 后照常 done——否则像 fitz 未导入
# 致 NameError 这种 bug 会静默抽 0 图、还查不到。这些类型一律重抛 fail-loud;只放行预期的数据/环境
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

        figures_info = parsed.get("figures", [])
        ocr_engine = self._create_ocr_engine()
        results = []
        asset_idx = 0   # 渲染出图者的递增序号 = 占位符 img:N 的 N(05_smart 内联回填用)

        # 按图注渲染 PDF 页面区域:caption 上方、同列、drawings+图片矩形的并集 = 图区域,
        # get_pixmap(clip=区域) 渲染——矢量图与栅格图通吃(get_images 只能抽栅格,会漏正文矢量图)。
        with fitz.open(str(pdf_path)) as doc:
            for i, fig in enumerate(figures_info):
                self.report_progress(i, len(figures_info), "rendering figures")
                fig_id = fig.get("id", f"fig{i + 1}")
                page_no = fig.get("page", 1)
                caption = fig.get("caption", "")

                filename = None
                if 1 <= page_no <= len(doc):
                    filename = self._render_figure_region(
                        doc[page_no - 1], self._fig_number(fig_id), caption, assets_dir, asset_idx)

                entry = {
                    "id": fig_id,
                    "page": page_no,
                    "caption": caption,
                    "filename": filename,
                    "index": asset_idx if filename else None,   # img:N 的 N;渲染不出图则 None(仅文字图注)
                    "ocr_text": "",
                }
                if filename:
                    entry["ocr_text"] = self._ocr_figure(ocr_engine, assets_dir / filename)
                    asset_idx += 1
                results.append(entry)

        self.report_progress(len(figures_info), len(figures_info), "done")
        self.write_output("intermediate/figures.json", results)
        return {"figures": len(results), "with_image": sum(1 for r in results if r["filename"])}

    @staticmethod
    def _fig_number(fig_id: str) -> str:
        import re
        m = re.search(r"\d+", fig_id or "")
        return m.group(0) if m else ""

    def _render_figure_region(self, page, fig_num: str, caption: str, assets_dir: Path, idx: int, zoom: float = 2.0):
        """渲染图注上方的图区域为 PNG。取不到 caption 位置 / 上方无图形 → None(仅文字图注,优雅降级)。"""
        import fitz
        try:
            cap = self._caption_rect(page, fig_num, caption)
            if cap is None:
                return None
            region = self._figure_bbox_above(page, cap)
            if region is None:
                return None
            pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=region)
            if pix.width < 24 or pix.height < 24:
                return None
            fn = f"figure-{idx:04d}.png"
            pix.save(str(assets_dir / fn))
            return fn
        except _BUG_ERRORS:
            raise   # 代码 bug → fail-loud,不静默吞
        except Exception as e:
            self.log.warning("figure_render_error", fig=fig_num, error=str(e))
            return None

    @staticmethod
    def _caption_rect(page, fig_num: str, caption: str):
        """定位图注矩形:先用 "Figure N: <头部>" 精确匹配,退回 "Figure N" 标签。"""
        probes = []
        head = (caption or "").strip()[:24]
        if head:
            probes += [f"Figure {fig_num}: {head}", f"Figure {fig_num}. {head}"]
        probes += [f"Figure {fig_num}:", f"Figure {fig_num}.", f"Fig. {fig_num}", f"Fig {fig_num}"]
        for p in probes:
            rs = page.search_for(p)
            if rs:
                return rs[0]
        return None

    @staticmethod
    def _figure_bbox_above(page, cap):
        """图区域 = caption 与其上方最近正文段落之间、同列内 drawings+图片矩形的并集。"""
        import fitz
        pw, ph = page.rect.width, page.rect.height
        mid = pw / 2
        # 列:用图注所在文本块宽度判栏(search 探针 rect 只是文字片段,会把全宽图误判成单栏)。
        # 块宽 < 55% 页宽 → 双栏,取所在半栏;否则(全宽图注)整页宽。
        cap_block_w = cap.width
        for b in page.get_text("blocks"):
            if b[1] <= cap.y0 <= b[3] + 1 and len(b[4].strip()) > 8:
                cap_block_w = max(cap_block_w, b[2] - b[0])
        if cap_block_w < pw * 0.55:
            col_l, col_r = (0.0, mid) if (cap.x0 + cap.x1) / 2 < mid else (mid, pw)
        else:
            col_l, col_r = 0.0, pw

        def in_col(x0, x1):
            return col_l - 6 <= (x0 + x1) / 2 <= col_r + 6

        # 上界:caption 上方最近的正文段落(长文本块)底——避免并入更上方的图/段落。
        text_top = max(0.0, cap.y0 - ph * 0.75)
        for b in page.get_text("blocks"):
            if len(b[4].strip()) < 80:          # 跳过短块(轴标签/图例/页眉)
                continue
            if in_col(b[0], b[2]) and b[3] <= cap.y0 - 2:
                text_top = max(text_top, b[3])

        # ink:drawings + 图片 bbox,落在 [text_top, caption] 之间、同列、尺寸合理。
        ink = [fitz.Rect(d["rect"]) for d in page.get_drawings()]
        for img in page.get_images(full=True):
            try:
                ink.append(page.get_image_bbox(img))
            except Exception:
                pass
        sel = []
        for r in ink:
            if r.is_empty or r.is_infinite:
                continue
            if not in_col(r.x0, r.x1):
                continue
            if r.y1 > cap.y0 - 1 or r.y0 < text_top - 2:
                continue
            if r.width > pw * 0.98 and r.height < 3:   # 跨页横线(rule)跳过
                continue
            sel.append(r)
        if not sel:
            return None
        region = sel[0]
        for r in sel[1:]:
            region |= r
        region = fitz.Rect(max(region.x0, col_l) - 2, region.y0 - 2,
                           min(region.x1, col_r) + 2, min(region.y1 + 2, cap.y0 - 1))
        if region.height < 24 or region.width < 24:
            return None
        return region

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
