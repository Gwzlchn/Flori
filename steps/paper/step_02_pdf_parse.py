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
        self.artifacts.write("intermediate/parsed.json", parsed)
        self.artifacts.write("output/original.md", md)
        if lang != "zh" and len(md.strip()) > 200:
            self.artifacts.write("intermediate/needs_translation.json", {"lang": lang})
        return {"source_kind": "arxiv-html", "sections": len(sections),
                "chars": len(md), "lang": lang}

    # ── pdf-only 模式(无 HTML 源:会议论文/直链 PDF/老论文 LaTeX 编译失败)──
    # 去 pymupdf(断词/公式丢,已废):不再抽正文文本——AI 步(翻译/笔记)直接喂 PDF(claude Read,
    # worker 镜像带 poppler)。这里只出元数据(metadata.json 权威源)+ pdfinfo 页数 + 页区间伪章节。
    PAGES_PER_SECTION = 4

    def _parse_pdf_only(self) -> dict:
        pdf_path = self.job_dir / "input" / "source.pdf"
        num_pages = self._pdf_page_count(pdf_path)

        meta = self._load_source_meta()
        title = (meta.get("title") or "").strip()
        authors = meta.get("authors") or []
        abstract = (meta.get("abstract") or "").strip()

        # pdf-only 的标题唯一来源是 PDF 内嵌 metadata,常为垃圾(编译文件名 "10things"/"paper.dvi"、
        # 系列名页眉)→ 垃圾时从 pdftotext 首页启发式提真标题(shared.titles,与 scheduler 覆盖判定同套)。
        from shared.titles import is_suspicious_title, title_from_first_page
        if is_suspicious_title(title):
            extracted = self._first_page_title(pdf_path)
            if extracted:
                title = extracted

        # 语言不可判(不抽正文):按用户约定默认需要翻译(中文会议论文极少;翻译步对中文输入无害)。
        sections = [
            {"level": 1, "title": f"Pages {i}-{min(i + self.PAGES_PER_SECTION - 1, num_pages)}",
             "page": i, "text": "", "kind": "page-range"}
            for i in range(1, num_pages + 1, self.PAGES_PER_SECTION)
        ] if num_pages else []

        parsed = {
            "title": title,
            "authors": authors,
            "abstract": abstract,
            "venue": "",
            "pages": num_pages,
            "lang": "unknown",
            "sections": sections,
            "source_kind": "pdf-only",
        }
        self.artifacts.write("intermediate/parsed.json", parsed)
        self.artifacts.write("intermediate/needs_translation.json", {"lang": "unknown"})
        return {"source_kind": "pdf-only", "pages": num_pages, "lang": "unknown"}

    def _first_page_title(self, pdf_path) -> str | None:
        """pdftotext 第 1 页 → shared.titles 启发式提标题;任何失败返 None(保留 metadata 原值)。"""
        from shared.titles import title_from_first_page
        try:
            r = self.commands.run(
                ["pdftotext", "-f", "1", "-l", "1", str(pdf_path), "-"], timeout=60)
            return title_from_first_page(r.stdout or "")
        except Exception:
            return None

    def _pdf_page_count(self, pdf_path) -> int:
        """poppler `pdfinfo` 取页数;失败(损坏 PDF/缺 poppler)fail-loud——页数是直喂分块的地基。"""
        import re as _re
        r = self.commands.run(["pdfinfo", str(pdf_path)], timeout=60)
        m = _re.search(r"^Pages:\s+(\d+)", r.stdout or "", _re.M)
        if not m:
            from shared.errors import InputInvalidError
            raise InputInvalidError(f"pdfinfo cannot read page count: {pdf_path.name}")
        return int(m.group(1))

    def _load_source_meta(self) -> dict:
        """读 01_download 写的 input/metadata.json(arxiv API 等权威元数据);缺/坏 → {}。"""
        p = self.job_dir / "input" / "metadata.json"
        try:
            return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        except (OSError, ValueError):
            return {}

if __name__ == "__main__":
    PdfParseStep.cli_main("02_pdf_parse")
