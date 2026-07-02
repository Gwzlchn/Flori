"""Step 02: 论文解析(HTML 优先)。

源头重做:arxiv 论文用 01_download 抓的 HTML 源(LaTeXML,结构/公式无损)转干净 Markdown,
原文展示/翻译/笔记全吃它;只有 PDF 的(会议论文等)标记 pdf-only,AI 步直喂 PDF(claude Read)。
步名保持 "02_pdf_parse"(历史 job 的步身份/重跑兼容),语义已是「论文解析」。

产物:
- intermediate/parsed.json:title/authors/abstract/venue/lang/sections/source_kind("arxiv-html"|"pdf-only")
- intermediate/needs_translation.json:非中文 → 04_translate_paper rules:exists 门控
- output/original.md(仅 html 模式):干净原文 MD(标题层级/$公式$/![](assets/…)图+图注)
"""

from __future__ import annotations

import json

from shared.step_base import StepBase, file_hash


class PdfParseStep(StepBase):
    def validate_inputs(self) -> list[str]:
        # HTML 或 PDF 至少其一(arxiv 双有;直链 PDF 只有 source.pdf)。
        input_dir = self.job_dir / "input"
        if not (input_dir / "source.html").exists() and not (input_dir / "source.pdf").exists():
            return ["input/source.html|input/source.pdf"]
        return []

    def input_hashes(self) -> dict[str, str]:
        h: dict[str, str] = {}
        html = self.job_dir / "input" / "source.html"
        pdf = self.job_dir / "input" / "source.pdf"
        if html.exists():
            h["html"] = file_hash(html)
        if pdf.exists():
            h["pdf"] = file_hash(pdf)
        return h

    def execute(self) -> dict | None:
        if (self.job_dir / "input" / "source.html").exists():
            return self._parse_html()
        return self._parse_pdf_only()

    # ── arxiv-html 模式:HTML → 干净 MD + 章节 ──
    def _parse_html(self) -> dict:
        from steps.utils.html_paper import arxiv_html_to_markdown

        html = (self.job_dir / "input" / "source.html").read_text(encoding="utf-8")
        # 01 已把图下载到 assets/ 并把 src 重写为 assets/<名>,转换器直通即可(src_map 恒等)。
        doc = arxiv_html_to_markdown(html)
        md, sections = doc["markdown"], doc["sections"]

        meta = self._load_source_meta()          # arxiv API 权威元数据(01_download 写)
        title = (meta.get("title") or doc.get("title") or "").strip()
        authors = meta.get("authors") or []
        abstract = (meta.get("abstract") or "").strip()

        from steps.utils.lang import detect_lang
        lang = detect_lang(" ".join([title, abstract, md[:20000]]))

        parsed = {
            "title": title,
            "authors": authors,
            "abstract": abstract,
            "venue": "arXiv",
            "lang": lang,
            "sections": sections,
            "source_kind": "arxiv-html",
        }
        self.write_output("intermediate/parsed.json", parsed)
        self.write_output("output/original.md", md)
        if lang != "zh" and len(md.strip()) > 200:
            self.write_output("intermediate/needs_translation.json", {"lang": lang})
        return {"source_kind": "arxiv-html", "sections": len(sections),
                "chars": len(md), "lang": lang}

    # ── pdf-only 模式(无 HTML 源:会议论文/直链 PDF/老论文 LaTeX 编译失败)──
    # 注:本模式过渡期仍用 pymupdf 抽文本;下一提交去 pymupdf 后改为「元数据 + 页区间伪章节,
    #     AI 步直喂 PDF(claude Read)」。
    def _parse_pdf_only(self) -> dict:
        import fitz  # pymupdf(过渡期)

        pdf_path = self.job_dir / "input" / "source.pdf"
        with fitz.open(str(pdf_path)) as doc:
            title = self._extract_title(doc)
            abstract = self._extract_abstract(doc)
            venue = self._extract_venue(doc)
            sections = []
            for page_num in range(len(doc)):
                self.report_progress(page_num, len(doc), "parsing pages")
                sections.extend(self._extract_sections(doc[page_num], page_num + 1))
            num_pages = len(doc)

        meta = self._load_source_meta()
        if meta.get("title"):
            title = meta["title"].strip()
        authors = meta.get("authors") or []
        if meta.get("abstract"):
            abstract = meta["abstract"].strip()

        from steps.utils.lang import detect_lang
        sample = " ".join([title or "", abstract or ""] + [s.get("text", "") for s in sections])
        lang = detect_lang(sample)

        parsed = {
            "title": title,
            "authors": authors,
            "abstract": abstract,
            "venue": venue,
            "pages": num_pages,
            "lang": lang,
            "sections": sections,
            "source_kind": "pdf-only",
        }
        self.write_output("intermediate/parsed.json", parsed)
        if lang != "zh" and len(sample.strip()) > 200:
            self.write_output("intermediate/needs_translation.json", {"lang": lang})
        return {"source_kind": "pdf-only", "pages": num_pages,
                "sections": len(sections), "lang": lang}

    def _load_source_meta(self) -> dict:
        """读 01_download 写的 input/metadata.json(arxiv API 等权威元数据);缺/坏 → {}。"""
        p = self.job_dir / "input" / "metadata.json"
        try:
            return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        except (OSError, ValueError):
            return {}

    # ── 以下 pymupdf 启发式(仅 pdf-only 过渡期用)──
    def _extract_title(self, doc) -> str:
        meta = doc.metadata
        # PDF 内置 title 若是 arXiv 戳(arXiv:xxxx 开头)则不可信,跳过走字号启发。
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
                parts = [
                    s["text"].strip() for s in spans
                    if abs(s["size"] - max_size) < 0.1
                    and not s["text"].strip().lower().startswith("arxiv:")
                ]
                title = " ".join(parts).strip()
                if len(title) > 250:   # 异常长多半误并页眉/作者块
                    title = parts[0] if parts else ""
                return title
        return ""

    @staticmethod
    def _venue_acronyms() -> dict:
        """会议/期刊全名→缩写映射,从 configs/venues.yaml 读。缺/坏 → {}。"""
        import os
        import yaml
        path = os.path.join(os.environ.get("CONFIG_DIR", "configs"), "venues.yaml")
        try:
            with open(path, encoding="utf-8") as f:
                return (yaml.safe_load(f) or {}).get("venue_acronyms", {}) or {}
        except (OSError, yaml.YAMLError):
            return {}

    def _extract_venue(self, doc) -> str:
        """来源:会议/期刊 + 年份(best-effort,扫前 2 页)。取不到返空。"""
        import re
        if len(doc) == 0:
            return ""
        text = "\n".join(doc[i].get_text() for i in range(min(2, len(doc))))
        if re.search(r"arXiv:\d", text):
            return "arXiv"
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
        import re
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


if __name__ == "__main__":
    PdfParseStep.cli_main("02_pdf_parse")
