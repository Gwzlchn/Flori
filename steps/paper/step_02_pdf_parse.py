"""Step 02: PDF 解析。PyMuPDF 提取文本/章节/图表/公式/元数据。"""

from __future__ import annotations

import json
import re
from pathlib import Path

from shared.step_base import StepBase, file_hash


class PdfParseStep(StepBase):
    def validate_inputs(self) -> list[str]:
        if not (self.job_dir / "input" / "source.pdf").exists():
            return ["input/source.pdf"]
        return []

    def input_hashes(self) -> dict[str, str]:
        return {
            "pdf": file_hash(self.job_dir / "input" / "source.pdf"),
        }

    def execute(self) -> dict | None:
        import fitz  # pymupdf

        pdf_path = self.job_dir / "input" / "source.pdf"
        with fitz.open(str(pdf_path)) as doc:
            title = self._extract_title(doc)
            authors = self._extract_authors(doc)
            abstract = self._extract_abstract(doc)

            sections = []
            for page_num in range(len(doc)):
                self.report_progress(page_num, len(doc), "parsing pages")
                page = doc[page_num]
                page_sections = self._extract_sections(page, page_num + 1)
                sections.extend(page_sections)

            figures = self._extract_figure_refs(doc)
            formulas = self._extract_formulas(doc)
            num_pages = len(doc)

        parsed = {
            "title": title,
            "authors": authors,
            "abstract": abstract,
            "pages": num_pages,
            "sections": sections,
            "figures": figures,
            "formulas": formulas,
        }

        self.report_progress(num_pages, num_pages, "done")
        self.write_output("intermediate/parsed.json", parsed)
        return {"pages": num_pages, "sections": len(sections), "figures": len(figures)}

    def _extract_title(self, doc) -> str:
        meta = doc.metadata
        if meta.get("title"):
            return meta["title"]
        if len(doc) > 0:
            page = doc[0]
            blocks = page.get_text("dict")["blocks"]
            spans = [
                span
                for block in blocks if "lines" in block
                for line in block["lines"]
                for span in line["spans"]
                if span.get("text", "").strip()
            ]
            if spans:
                max_size = max(s["size"] for s in spans)
                # 收集首页最大字号那一档(容差 0.1)的所有 span 按阅读序拼接,
                # 而非只取单个 span(避免多 span 标题被截断)。
                parts = [
                    s["text"].strip() for s in spans
                    if abs(s["size"] - max_size) < 0.1
                ]
                title = " ".join(parts).strip()
                # 兜底:异常长多半误并了页眉/作者块,退回首个最大字号 span。
                if len(title) > 250:
                    title = parts[0] if parts else ""
                return title
        return ""

    def _extract_authors(self, doc) -> list[str]:
        meta = doc.metadata
        if meta.get("author"):
            return [a.strip() for a in meta["author"].split(",") if a.strip()]
        return []

    def _extract_abstract(self, doc) -> str:
        if len(doc) == 0:
            return ""
        text = doc[0].get_text()
        # 终止于「空行 / introduction / 文末(\Z)」:补 \Z 兜底,避免首页无空行又无
        # introduction 时整段匹配失败而直接返回空。
        m = re.search(
            r"(?i)abstract[:\s]*\n?(.*?)(?:\n\s*\n|introduction|\Z)",
            text, re.DOTALL,
        )
        if not m:
            return ""
        abstract = m.group(1).strip()
        # 长度上限:无显式终止符时可能吞进正文,截断到合理范围。
        MAX_ABSTRACT = 3000
        if len(abstract) > MAX_ABSTRACT:
            abstract = abstract[:MAX_ABSTRACT].rstrip()
        if not abstract:
            self.log.warning("abstract_empty", page0_chars=len(text))
        return abstract

    def _extract_sections(self, page, page_num: int) -> list[dict]:
        blocks = page.get_text("dict")["blocks"]
        sections = []
        current_text_parts: list[str] = []
        current_heading: dict | None = None

        for block in blocks:
            if "lines" not in block:
                continue
            for line in block["lines"]:
                for span in line["spans"]:
                    text = span["text"].strip()
                    if not text:
                        continue
                    is_heading = span["size"] >= 12 and (
                        span["flags"] & 2**4  # bold
                        or span["size"] >= 14
                    )
                    if is_heading and len(text) < 200:
                        if current_heading:
                            current_heading["text"] = "\n".join(current_text_parts).strip()
                            sections.append(current_heading)
                            current_text_parts = []
                        level = 1 if span["size"] >= 16 else 2
                        current_heading = {
                            "level": level,
                            "title": text,
                            "page": page_num,
                            "text": "",
                        }
                    else:
                        current_text_parts.append(text)

        if current_heading:
            current_heading["text"] = "\n".join(current_text_parts).strip()
            sections.append(current_heading)
        elif current_text_parts and not sections:
            sections.append({
                "level": 1,
                "title": f"Page {page_num}",
                "page": page_num,
                "text": "\n".join(current_text_parts).strip(),
            })

        return sections

    def _extract_figure_refs(self, doc) -> list[dict]:
        figures = []
        fig_pattern = re.compile(r"(?:Figure|Fig\.?)\s+(\d+)[.:]\s*(.*?)(?:\n|$)", re.IGNORECASE)
        for page_num in range(len(doc)):
            text = doc[page_num].get_text()
            for m in fig_pattern.finditer(text):
                figures.append({
                    "id": f"fig{m.group(1)}",
                    "page": page_num + 1,
                    "caption": m.group(2).strip(),
                })
        return figures

    def _extract_formulas(self, doc) -> list[dict]:
        formulas = []
        eq_pattern = re.compile(r"\$\$(.+?)\$\$", re.DOTALL)
        for page_num in range(len(doc)):
            text = doc[page_num].get_text()
            for m in eq_pattern.finditer(text):
                formulas.append({
                    "id": f"eq{len(formulas) + 1}",
                    "page": page_num + 1,
                    "latex": m.group(1).strip(),
                })
        return formulas


if __name__ == "__main__":
    PdfParseStep.cli_main("02_pdf_parse")
