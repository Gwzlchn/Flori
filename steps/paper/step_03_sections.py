"""Step 03: 章节结构。扁平章节 → 树形结构 + 关键段落。"""

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
        return {"sections": len(tree)}


if __name__ == "__main__":
    SectionsStep.cli_main("03_sections")
