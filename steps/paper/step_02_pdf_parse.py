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
import tempfile
from pathlib import Path

from shared.provenance import (
    MAX_PROVENANCE_BYTES,
    MAX_SUPPORT_TEXT_BYTES,
    bounded_support_text,
    write_json_atomic,
)
from shared.step_base import StepBase, file_hash
from steps.article.provenance import (
    build_html_source_manifest,
    build_pdf_source_manifest,
    PDF_SUPPORT_PATH,
    direct_text_provenance_candidates,
    persist_note_provenance,
    publish_source_manifest,
)


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
        (self.job_dir / "intermediate" / "source_segments.json").unlink(missing_ok=True)
        (self.job_dir / "output" / "provenance" / "original.json").unlink(missing_ok=True)
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
        source_manifest = publish_source_manifest(
            self.job_dir,
            build_html_source_manifest(self.job_dir, pipeline="paper"),
        )
        provenance = {"status": "legacy_no_source_manifest", "segments": 0}
        if source_manifest is not None:
            provenance = persist_note_provenance(
                self.job_dir,
                pipeline="paper",
                note_type="original",
                note_artifact="output/original.md",
                candidates=direct_text_provenance_candidates(
                    source_manifest, md, section="original",
                ),
            )
        if lang != "zh" and len(md.strip()) > 200:
            self.artifacts.write("intermediate/needs_translation.json", {"lang": lang})
        return {"source_kind": "arxiv-html", "sections": len(sections),
                "chars": len(md), "lang": lang,
                "provenance_segments": provenance["segments"],
                "provenance_status": provenance["status"]}

    # ── pdf-only 模式(无 HTML 源:会议论文/直链 PDF/老论文 LaTeX 编译失败)──
    # 业务正文仍由 AI 直接读 PDF,不把 pdftotext 结果写进 parsed sections。Poppler 文本只用于
    # provenance exact-quote 复算,与 PDF SHA 和真实页码一起发布。
    PAGES_PER_SECTION = 4

    def _parse_pdf_only(self) -> dict:
        pdf_path = self.job_dir / "input" / "source.pdf"
        source_sha256 = file_hash(pdf_path).removeprefix("sha256:")
        num_pages = self._pdf_page_count(pdf_path)
        page_support_texts, first_page_text = self._pdf_page_support_texts(
            pdf_path, num_pages,
        )
        if file_hash(pdf_path).removeprefix("sha256:") != source_sha256:
            from shared.errors import InputInvalidError
            raise InputInvalidError("PDF changed while extracting page support")
        write_json_atomic(
            self.job_dir / PDF_SUPPORT_PATH,
            {
                "schema_version": 1,
                "source_sha256": source_sha256,
                "pages": [
                    {"page": page, "support_text": page_support_texts[page - 1]}
                    for page in range(1, num_pages + 1)
                ],
            },
            trusted_root=self.job_dir,
        )

        meta = self._load_source_meta()
        title = (meta.get("title") or "").strip()
        authors = meta.get("authors") or []
        abstract = (meta.get("abstract") or "").strip()

        # pdf-only 的标题唯一来源是 PDF 内嵌 metadata,常为垃圾(编译文件名 "10things"/"paper.dvi"、
        # 系列名页眉)→ 垃圾时从 pdftotext 首页启发式提真标题(shared.titles,与 scheduler 覆盖判定同套)。
        from shared.titles import is_suspicious_title, title_from_first_page
        if is_suspicious_title(title):
            extracted = title_from_first_page(first_page_text)
            if extracted:
                title = extracted

        from steps.utils.lang import detect_lang
        language_sample = " ".join(
            [title, abstract, first_page_text]
            + [text for text in page_support_texts if text]
        )
        lang = detect_lang(language_sample)

        # 无法识别时仍进入翻译,避免旧的 PDF-only 行为因提取失败而漏译.
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
            "lang": lang,
            "sections": sections,
            "source_kind": "pdf-only",
        }
        self.artifacts.write("intermediate/parsed.json", parsed)
        if lang != "zh":
            self.artifacts.write("intermediate/needs_translation.json", {"lang": lang})
        publish_source_manifest(
            self.job_dir,
            build_pdf_source_manifest(
                self.job_dir,
                pipeline="paper",
                page_count=num_pages,
                page_support_texts=page_support_texts,
            ),
        )
        return {"source_kind": "pdf-only", "pages": num_pages, "lang": lang}

    def _pdf_page_support_texts(
        self, pdf_path: Path, page_count: int,
    ) -> tuple[list[str | None], str]:
        """一次提取整文并按 form-feed 对页;失败或边界异常时不影响业务解析。"""
        empty = [None] * page_count
        first_page_text = ""
        with tempfile.TemporaryDirectory(prefix="flori-pdf-support-") as temp_dir:
            output = Path(temp_dir) / "document.txt"
            try:
                self.commands.run(
                    ["pdftotext", "-enc", "UTF-8", str(pdf_path), str(output)],
                    timeout=120,
                )
                if not output.is_file():
                    return empty, first_page_text
                with output.open("rb") as stream:
                    preview = stream.read(MAX_SUPPORT_TEXT_BYTES)
                first_page_text = preview.split(b"\f", 1)[0].decode("utf-8")
                if output.stat().st_size > MAX_PROVENANCE_BYTES:
                    return empty, first_page_text
                text = output.read_text(encoding="utf-8")
            except Exception:
                return empty, first_page_text
        pages = text.split("\f")
        if pages and not pages[-1].strip():
            pages.pop()
        if len(pages) != page_count:
            return empty, first_page_text
        return [bounded_support_text(page) for page in pages], first_page_text

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
