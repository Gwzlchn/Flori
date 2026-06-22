"""Step 05: 论文智能笔记。AI 将论文内容重组为中文结构化笔记。"""

from __future__ import annotations

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

        # 有内嵌位图的图(filename 非空、index 有值)给 AI 用 ![中文图注](img:N) 占位符引用,落盘回填。
        image_assets = [{"n": f["index"], "filename": f["filename"]}
                        for f in figures
                        if f.get("filename") and f.get("index") is not None]

        prompt = self._build_prompt(sections, figures)
        # 结构化中文笔记常超默认 4096 output tokens,显式抬高上限防被静默截断(claude-cli 无视无害)。
        result = self.call_ai(prompt, max_tokens=8192)

        rel = self.write_smart_note(result, image_assets=image_assets)  # 回填占位符 + 版本化落盘
        return {"chars": len(result), "provider": self.last_ai_provider,
                "model": self.last_ai_model, "note_file": rel}

    def _build_prompt(self, sections: dict, figures: list) -> str:
        profile = self.load_domain_prompt_profile()

        parts = [
            "请将以下论文内容重组为中文结构化学习笔记。\n",
            "要求：\n",
            "- 用中文重述论文核心贡献\n",
            "- 保留关键公式（LaTeX 格式）\n",
            "- 在合适处用 ![中文图注](img:N) 占位符内嵌重要图表(N 见下方图表列表;不要写文件名/路径)\n",
            "- 按逻辑结构组织，不必按原文章节顺序\n",
        ]

        parts.append(self.terminology_block(profile))  # 已沉淀标准概念注入(共用,审计 R-M9)

        parts.append(f"\n论文标题：{sections.get('title', '未知')}\n")
        parts.append(f"作者：{', '.join(sections.get('authors', []))}\n")

        if sections.get("abstract"):
            parts.append(f"\n摘要：{sections['abstract']}\n")

        parts.append("\n--- 章节内容 ---\n")
        for sec in sections.get("sections", []):
            self._render_section(sec, parts, level=2)

        if figures:
            parts.append("\n--- 图表(有 img:N 的可内嵌:写 ![中文图注](img:N),不要写文件名;无 img:N 的仅文字图注)---\n")
            for fig in figures:
                caption = fig.get("caption", "")
                ocr = fig.get("ocr_text", "")
                if fig.get("filename") and fig.get("index") is not None:
                    parts.append(f"- img:{fig['index']} | {caption}")
                else:
                    parts.append(f"- {fig.get('id', '')}: {caption}")
                if ocr:
                    parts.append(f" (OCR: {ocr[:200]})")
                parts.append("\n")

        return "".join(parts)

    def _render_section(self, section: dict, parts: list, level: int) -> None:
        from steps.utils.sections import render_section_tree
        render_section_tree(section, parts, level)

if __name__ == "__main__":
    SmartPaperStep.cli_main("05_smart_paper")
