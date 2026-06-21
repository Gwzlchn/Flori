"""Step 05: 论文智能笔记。AI 将论文内容重组为中文结构化笔记。"""

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
        hashes: dict[str, str] = {
            "sections": file_hash(self.job_dir / "intermediate" / "sections.json"),
            "figures": file_hash(self.job_dir / "intermediate" / "figures.json"),
        }
        hashes.update(self.prompt_profile_style_hashes())  # prompt(可选覆盖)+ profile + styles
        return hashes

    def execute(self) -> dict | None:
        sections = self.load_json("intermediate/sections.json")
        figures = self.load_json("intermediate/figures.json")

        prompt = self._build_prompt(sections, figures)
        # 结构化中文笔记常超默认 4096 output tokens,显式抬高上限防被静默截断(claude-cli 无视无害)。
        result = self.call_ai(prompt, max_tokens=8192)

        rel = self.write_smart_note(result)   # 版本化落盘(含生成时间/方式/模型),不再写 notes_smart.md
        return {"chars": len(result), "provider": self.last_ai_provider,
                "model": self.last_ai_model, "note_file": rel}

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
                # 回流(§1.8 ③)：注入本域已沉淀概念的标准定义,命中用统一措辞、不重复展开,
                # 只对未列出的新概念做首次解释——避免同概念每篇换一套说法。
                parts.append(
                    "\n本领域已沉淀的标准概念（命中时沿用统一措辞、无需重新展开解释；"
                    f"只对下列未涵盖的新概念做首次解释）：\n{terms}\n"
                )

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
        from steps.utils.sections import render_section_tree
        render_section_tree(section, parts, level)

    def _load_profile(self) -> dict:
        return self.load_domain_profile()


if __name__ == "__main__":
    SmartPaperStep.cli_main("05_smart_paper")
