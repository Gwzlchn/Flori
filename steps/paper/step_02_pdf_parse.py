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
            venue = self._extract_venue(doc)        # 来源:会议/期刊 + 年份(arXiv 等)

            sections = []
            for page_num in range(len(doc)):
                self.report_progress(page_num, len(doc), "parsing pages")
                page = doc[page_num]
                page_sections = self._extract_sections(page, page_num + 1)
                sections.extend(page_sections)

            figures = self._extract_figure_refs(doc)
            formulas = self._extract_formulas(doc)
            num_pages = len(doc)

        # arxiv 等:01_download 写的 input/metadata.json 含权威元数据(来自 arxiv API)→ 优先于 PDF 启发
        # (PDF 标题常抓成左边距 arXiv 戳、作者多为空)。仅在 metadata 有该字段时覆盖。
        src_meta = self._load_source_meta()
        if src_meta.get("title"):
            title = src_meta["title"].strip()
        if src_meta.get("authors"):
            authors = src_meta["authors"]
        if src_meta.get("abstract"):
            abstract = src_meta["abstract"].strip()

        # 语言检测(翻译触发,与文章共用判据):标题+摘要+章节文本判中/非中。
        from steps.utils.lang import detect_lang
        sample = " ".join([title or "", abstract or ""] + [s.get("text", "") for s in sections])
        lang = detect_lang(sample)

        parsed = {
            "title": title,
            "authors": authors,
            "abstract": abstract,
            "venue": venue,                         # 来源:会议/期刊 + 年份(如 "OSDI 2023" / "arXiv")
            "pages": num_pages,
            "lang": lang,
            "sections": sections,
            "figures": figures,
            "formulas": formulas,
        }

        self.report_progress(num_pages, num_pages, "done")
        self.write_output("intermediate/parsed.json", parsed)
        # 非中文论文 → 写翻译标记,04_translate_paper 经 rules:exists 门控触发(中文论文不译)。
        if lang != "zh" and len(sample.strip()) > 200:
            self.write_output("intermediate/needs_translation.json", {"lang": lang})
        return {"pages": num_pages, "sections": len(sections),
                "figures": len(figures), "lang": lang}

    def _load_source_meta(self) -> dict:
        """读 01_download 写的 input/metadata.json(arxiv API 等权威元数据);缺/坏 → {}。"""
        p = self.job_dir / "input" / "metadata.json"
        try:
            return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        except (OSError, ValueError):
            return {}

    def _extract_title(self, doc) -> str:
        meta = doc.metadata
        # PDF 内置 title 若是 arXiv 戳(arXiv:xxxx …)则不可信,跳过走字号启发(arxiv 真标题通常靠 API,见 _load_source_meta)。
        if meta.get("title") and not meta["title"].strip().lower().startswith("arxiv:"):
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
                    and not s["text"].strip().lower().startswith("arxiv:")  # 跳过左边距 arXiv 戳
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

    @staticmethod
    def _venue_acronyms() -> dict:
        """会议/期刊全名→缩写映射,从 configs/venues.yaml 读(配置与代码分离,可扩展不改码)。
        缺文件/解析失败 → 空 dict(此时 venue 用全名,不映射)。"""
        import os
        import yaml
        path = os.path.join(os.environ.get("CONFIG_DIR", "configs"), "venues.yaml")
        try:
            with open(path, encoding="utf-8") as f:
                return (yaml.safe_load(f) or {}).get("venue_acronyms", {}) or {}
        except (OSError, yaml.YAMLError):
            return {}

    def _extract_venue(self, doc) -> str:
        """来源:会议/期刊 + 年份(best-effort,扫前 2 页)。arXiv 单独识别;USENIX 等封面页抽
        'Proceedings of the X' 的 X;命中 configs/venues.yaml 的全名 → 用缩写。取不到返空(前端回退类型标签)。"""
        if len(doc) == 0:
            return ""
        text = "\n".join(doc[i].get_text() for i in range(min(2, len(doc))))
        if re.search(r"arXiv:\d", text):
            return "arXiv"
        # re.S:封面 venue 常跨行(如 "…Operating Systems\nDesign and Implementation."),终止于句号;
        # 捕获后归一化空白(含换行)再匹配缩写,否则会在换行处截断、丢后半截致映射不中。
        m = re.search(r"Proceedings of (?:the\s+)?(.{4,120}?)\.", text, re.I | re.S)
        venue = re.sub(r"\s+", " ", m.group(1).strip()) if m else ""
        low = venue.lower()
        for full, ac in self._venue_acronyms().items():
            if full.lower() in low:
                venue = ac
                break
        ym = re.search(r"\b(?:19|20)\d{2}\b", text)
        year = ym.group(0) if ym else ""
        return f"{venue} {year}".strip() if venue else ""

    def _extract_abstract(self, doc) -> str:
        # 扫前几页找 Abstract:会议 PDF(USENIX/OSDI 等)首页常是封面/版权页,真正摘要在第 2-3 页;
        # arxiv 等无封面则首页即命中。终止于「空行 / introduction / 文末(\Z)」。
        MAX_ABSTRACT = 3000
        for i in range(min(3, len(doc))):
            text = doc[i].get_text()
            m = re.search(
                r"(?i)abstract[:\s]*\n?(.*?)(?:\n\s*\n|introduction|\Z)",
                text, re.DOTALL,
            )
            abstract = (m.group(1).strip() if m else "")
            if abstract:
                return abstract[:MAX_ABSTRACT].rstrip() if len(abstract) > MAX_ABSTRACT else abstract
        self.log.warning("abstract_empty", pages_scanned=min(3, len(doc)))
        return ""

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
        # 同一图号在正文里常被多次匹配("Figure 1." inline 引用)+ 真图注一处 → 按图号去重,留 caption 最长者
        # (真图注信息最全;inline 引用 caption 短)。渲染步据此每图渲一张,避免重复/错配。
        fig_pattern = re.compile(r"(?:Figure|Fig\.?)\s+(\d+)[.:]\s*(.*?)(?:\n|$)", re.IGNORECASE)
        best: dict[str, dict] = {}
        for page_num in range(len(doc)):
            text = doc[page_num].get_text()
            for m in fig_pattern.finditer(text):
                num, caption = m.group(1), m.group(2).strip()
                cur = best.get(num)
                if cur is None or len(caption) > len(cur["caption"]):
                    best[num] = {"id": f"fig{num}", "page": page_num + 1, "caption": caption}
        return [best[k] for k in sorted(best, key=lambda n: int(n))]

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
