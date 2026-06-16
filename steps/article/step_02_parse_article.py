"""Step 16: 文章解析。trafilatura 从原始 HTML 抽取正文纯文本 + 元数据。"""

from __future__ import annotations

import json
from pathlib import Path

from shared.step_base import StepBase, file_hash


class ParseArticleStep(StepBase):
    def validate_inputs(self) -> list[str]:
        # 仅依赖 01_download 写入的原始 HTML，meta 可选
        if not (self.job_dir / "input" / "source.html").exists():
            return ["input/source.html"]
        return []

    def input_hashes(self) -> dict[str, str]:
        return {
            "html": file_hash(self.job_dir / "input" / "source.html"),
        }

    def execute(self) -> dict | None:
        import trafilatura

        html = (self.job_dir / "input" / "source.html").read_text(encoding="utf-8")

        # 优先用 trafilatura 抽 JSON（含 title/author/date/text）
        extracted = trafilatura.extract(
            html,
            output_format="json",
            with_metadata=True,
            include_comments=False,
            include_tables=True,
        )

        title = ""
        authors: list[str] = []
        date = ""
        text = ""
        if extracted:
            data = json.loads(extracted)
            title = (data.get("title") or "").strip()
            text = (data.get("text") or "").strip()
            date = (data.get("date") or "").strip()
            author = data.get("author")
            if author:
                authors = [a.strip() for a in str(author).split(";") if a.strip()]

        # trafilatura 抽取失败时退回纯文本抽取，仍尽量给正文
        if not text:
            text = (trafilatura.extract(html, include_comments=False) or "").strip()

        # 合并 01_download 写入的 article_meta.json（更可信的来源元数据）
        meta = self._load_meta()
        if not title:
            title = (meta.get("title") or "").strip()
        if not authors and meta.get("author"):
            authors = [a.strip() for a in str(meta["author"]).split(";") if a.strip()]
        if not date:
            date = (meta.get("date") or "").strip()

        # 从纯文本构造单一 section，章节切分留给 03_article_sections
        sections = []
        if text:
            sections.append({
                "level": 1,
                "title": title or "正文",
                "page": 1,
                "text": text,
            })

        parsed = {
            "title": title,
            "authors": authors,
            "abstract": "",
            "url": meta.get("url", ""),
            "sitename": meta.get("sitename", ""),
            "date": date,
            "sections": sections,
            "text": text,
        }

        self.write_output("intermediate/parsed.json", parsed)
        return {"chars": len(text), "title": title}

    def _load_meta(self) -> dict:
        path = self.job_dir / "input" / "article_meta.json"
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return {}
        return {}


if __name__ == "__main__":
    ParseArticleStep.cli_main("02_parse_article")
