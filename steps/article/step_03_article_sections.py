"""Step 03: 文章章节结构。纯正文切分为章节 → 树形结构。"""

from __future__ import annotations

import re

from shared.step_base import StepBase, file_hash
from steps.utils.sections import build_section_tree


class ArticleSectionsStep(StepBase):
    def validate_inputs(self) -> list[str]:
        if not (self.job_dir / "intermediate" / "parsed.json").exists():
            return ["intermediate/parsed.json"]
        return []

    def input_hashes(self) -> dict[str, str]:
        return {
            "parsed": file_hash(self.job_dir / "intermediate" / "parsed.json"),
        }

    def execute(self) -> dict | None:
        parsed = self.load_json("intermediate/parsed.json")

        # 文章无原生标题层级，从正文切分扁平章节
        flat = parsed.get("sections", [])
        if flat and len(flat) == 1 and flat[0].get("text"):
            flat = self._split_text(flat[0]["text"])
        elif not flat and parsed.get("text"):
            flat = self._split_text(parsed["text"])

        tree = build_section_tree(flat)
        result = {
            "title": parsed.get("title", ""),
            "authors": parsed.get("authors", []),
            "abstract": parsed.get("abstract", ""),
            "sections": tree,
            "total_sections": len(flat),
        }

        self.write_output("intermediate/sections.json", result)
        return {"sections": len(tree), "total_sections": len(flat)}

    def _split_text(self, text: str) -> list[dict]:
        """纯正文按 Markdown 标题/短行启发式切分为扁平章节。"""
        lines = text.splitlines()
        sections: list[dict] = []
        current: dict | None = None
        buf: list[str] = []

        def flush() -> None:
            nonlocal current, buf
            if current is not None:
                current["text"] = "\n".join(buf).strip()
                sections.append(current)
            buf = []

        for raw in lines:
            line = raw.rstrip()
            stripped = line.strip()
            if not stripped:
                buf.append("")
                continue

            level, heading = self._as_heading(stripped)
            if heading is not None:
                flush()
                current = {"level": level, "title": heading, "page": 1, "text": ""}
            else:
                if current is None:
                    current = {"level": 1, "title": "正文", "page": 1, "text": ""}
                buf.append(stripped)

        flush()

        # 无任何切分时，整篇作为单一章节兜底
        if not sections and text.strip():
            sections.append({"level": 1, "title": "正文", "page": 1, "text": text.strip()})
        return sections

    def _as_heading(self, line: str) -> tuple[int, str | None]:
        """识别标题行：Markdown # / 编号标题 / 短独立行。返回 (level, title|None)。"""
        m = re.match(r"^(#{1,6})\s+(.+)$", line)
        if m:
            return min(len(m.group(1)), 3), m.group(2).strip()

        # 形如 "1. 标题" / "二、标题" / "第一章 标题" 的编号标题
        if re.match(r"^(\d+(\.\d+)*[\.、]|[一二三四五六七八九十]+[、.]|第[一二三四五六七八九十\d]+[章节])\s*\S", line):
            level = 2 if re.match(r"^\d+\.\d+", line) else 1
            return level, line

        # 短独立行（无句末标点、长度有限）视为二级标题
        if len(line) <= 40 and not re.search(r"[。！？.!?；;，,：:]$", line):
            return 2, line

        return 0, None


if __name__ == "__main__":
    ArticleSectionsStep.cli_main("03_article_sections")
