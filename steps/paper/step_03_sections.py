"""Step 03: 章节结构。扁平章节 → 树形结构。"""

from __future__ import annotations

from shared.step_base import StepBase, file_hash
from steps.utils.sections import build_section_tree


class SectionsStep(StepBase):
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
        flat_sections = parsed.get("sections", [])

        tree = build_section_tree(flat_sections)
        result = {
            "title": parsed.get("title", ""),
            "authors": parsed.get("authors", []),
            "abstract": parsed.get("abstract", ""),
            "sections": tree,
            "total_sections": len(flat_sections),
        }

        self.write_output("intermediate/sections.json", result)
        # 可读原文 Markdown(解析版):论文对齐 article 的原文兜底——AI 笔记没跑之前,
        # 笔记 tab 也有全文可读(标题/作者/摘要/章节),而非空态。
        self.write_output("output/original.md", self._original_markdown(result))
        return {"sections": len(tree)}

    @staticmethod
    def _original_markdown(sections_doc: dict) -> str:
        """sections 树 → 可读 Markdown:H1 标题 + 作者行 + 摘要引用块 + 逐章节全文。
        章节标题层级 = 树深 + 1(H1 留给论文标题),递归展开。"""
        lines: list[str] = []
        title = (sections_doc.get("title") or "").strip()
        if title:
            lines += [f"# {title}", ""]
        authors = [str(a).strip() for a in (sections_doc.get("authors") or []) if str(a).strip()]
        if authors:
            lines += [", ".join(authors), ""]
        abstract = (sections_doc.get("abstract") or "").strip()
        if abstract:
            lines += ["> " + " ".join(abstract.split()), ""]

        def walk(nodes: list[dict], depth: int) -> None:
            for node in nodes or []:
                sec_title = (node.get("title") or "").strip()
                if sec_title:
                    lines.append("#" * min(depth, 6) + f" {sec_title}")
                    lines.append("")
                text = (node.get("text") or "").strip()
                if text:
                    lines.append(text)
                    lines.append("")
                walk(node.get("children") or [], depth + 1)

        walk(sections_doc.get("sections") or [], 2)
        return "\n".join(lines).strip() + "\n"


if __name__ == "__main__":
    SectionsStep.cli_main("03_sections")
