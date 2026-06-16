"""Step 14: 论文智能笔记。AI 将论文内容重组为中文结构化笔记。"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from shared.step_base import StepBase, file_hash


class SmartPaperStep(StepBase):
    def validate_inputs(self) -> list[str]:
        missing = []
        if not (self.job_dir / "intermediate" / "sections.json").exists():
            missing.append("intermediate/sections.json")
        if not (self.job_dir / "intermediate" / "figures.json").exists():
            missing.append("intermediate/figures.json")
        return missing

    def input_hashes(self) -> dict[str, str]:
        prompts_dir = Path(self.config["paths"]["prompts_dir"])
        domain_name = self.config["domain"]["name"]
        hashes: dict[str, str] = {
            "sections": file_hash(self.job_dir / "intermediate" / "sections.json"),
            "figures": file_hash(self.job_dir / "intermediate" / "figures.json"),
        }
        prompt_path = prompts_dir / "05_smart_paper.md"
        if prompt_path.exists():
            hashes["prompt"] = file_hash(prompt_path)
        profile_path = prompts_dir / "profiles" / f"{domain_name}.yaml"
        if profile_path.exists():
            hashes["profile"] = file_hash(profile_path)
        hashes["styles"] = json.dumps({
            tag: file_hash(prompts_dir / "styles" / f"{tag}.yaml")
            for tag in sorted(self.config.get("style_tags", []))
            if (prompts_dir / "styles" / f"{tag}.yaml").exists()
        }, sort_keys=True)
        return hashes

    def execute(self) -> dict | None:
        sections = self.load_json("intermediate/sections.json")
        figures = self.load_json("intermediate/figures.json")

        prompt = self._build_prompt(sections, figures)
        result = self.call_ai(prompt)

        self.write_output("output/notes_smart.md", result)
        return {"chars": len(result)}

    def _build_prompt(self, sections: dict, figures: list) -> str:
        profile = self._load_profile()

        parts = [
            "请将以下论文内容重组为中文结构化学习笔记。\n",
            "要求：\n",
            "- 用中文重述论文核心贡献\n",
            "- 保留关键公式（LaTeX 格式）\n",
            "- 引用重要图表\n",
            "- 按逻辑结构组织，不必按原文章节顺序\n",
        ]

        if profile:
            if profile.get("terminology"):
                terms = "; ".join(profile["terminology"][:30])
                parts.append(f"\n术语参考：{terms}\n")

        parts.append(f"\n论文标题：{sections.get('title', '未知')}\n")
        parts.append(f"作者：{', '.join(sections.get('authors', []))}\n")

        if sections.get("abstract"):
            parts.append(f"\n摘要：{sections['abstract']}\n")

        parts.append("\n--- 章节内容 ---\n")
        for sec in sections.get("sections", []):
            self._render_section(sec, parts, level=2)

        if figures:
            parts.append("\n--- 图表 ---\n")
            for fig in figures:
                caption = fig.get("caption", "")
                ocr = fig.get("ocr_text", "")
                parts.append(f"- {fig['id']}: {caption}")
                if ocr:
                    parts.append(f" (OCR: {ocr[:200]})")
                parts.append("\n")

        return "".join(parts)

    def _render_section(self, section: dict, parts: list, level: int) -> None:
        prefix = "#" * level
        parts.append(f"\n{prefix} {section['title']}\n\n")
        if section.get("text"):
            parts.append(f"{section['text'][:2000]}\n")
        for child in section.get("children", []):
            self._render_section(child, parts, level + 1)

    def _load_profile(self) -> dict:
        prompts_dir = Path(self.config["paths"]["prompts_dir"])
        domain_name = self.config["domain"]["name"]
        path = prompts_dir / "profiles" / f"{domain_name}.yaml"
        if path.exists():
            return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return {}


if __name__ == "__main__":
    SmartPaperStep.cli_main("05_smart_paper")
