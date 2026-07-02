"""Step 04: 论文翻译。AI 把非中文论文忠实翻译为简体中文 → output/translated.md
(供前端「译文」tab + 05_smart_paper 基于译文做笔记)。

仅非中文论文触发:02_pdf_parse 检测到非中文写 intermediate/needs_translation.json,本步经 rules:exists 门控。
05_smart_paper 是重组为中文笔记;本步是忠实全文翻译,保留章节结构、公式(LaTeX)、图表引用。
"""

from __future__ import annotations

from shared.step_base import StepBase, file_hash


class TranslatePaperStep(StepBase):
    def validate_inputs(self) -> list[str]:
        if not (self.job_dir / "intermediate" / "sections.json").exists():
            return ["intermediate/sections.json"]
        return []

    def input_hashes(self) -> dict[str, str]:
        h = {"sections": file_hash(self.job_dir / "intermediate" / "sections.json")}
        t = self.template_hash("04_translate_paper")
        if t:
            h["template"] = t
        return h

    def execute(self) -> dict | None:
        sections = self.load_json("intermediate/sections.json")
        md = self._paper_markdown(sections)

        prompt = self._build_prompt(md)
        # 全文译文常超默认 4096 output tokens,抬高上限防截断(claude-cli 无视无害)。
        result = self.call_ai(prompt, max_tokens=16384)

        self.write_output("output/translated.md", result)
        return {"chars": len(result), "provider": self.last_ai_provider,
                "model": self.last_ai_model}

    @staticmethod
    def _paper_markdown(sections: dict) -> str:
        """从 sections.json 拼出论文可读 Markdown(标题/作者/摘要/章节树)供翻译。"""
        from steps.utils.sections import render_section_tree
        parts: list[str] = []
        if sections.get("title"):
            parts.append(f"# {sections['title']}\n")
        if sections.get("authors"):
            parts.append(f"Authors: {', '.join(sections['authors'])}\n")
        if sections.get("abstract"):
            parts.append(f"\n## Abstract\n{sections['abstract']}\n")
        parts.append("\n")
        for sec in sections.get("sections", []):
            render_section_tree(sec, parts, level=2)
        return "".join(parts)

    def _build_prompt(self, md: str) -> str:
        # 默认模板外置 templates/04_translate_paper.md(改文件不碰代码);缺失回退 _DEFAULT。
        tmpl = self._load_prompt_template("04_translate_paper", _DEFAULT)
        return tmpl.replace("<<BODY>>", md)


# 静态默认 prompt 骨架(= 外置模板内容;<<BODY>> 注入论文原文)。
_DEFAULT = (
    "请将以下论文【忠实翻译】为简体中文。这是翻译,不是笔记/摘要,要求:\n"
    "- 忠实原意,逐段完整翻译,不增删、不概括、不评论;\n"
    "- 完整保留 Markdown 结构(标题层级、列表、表格、引用等)与原文章节顺序;\n"
    "- 数学公式、变量名、代码、算法伪代码原样保留(LaTeX 不译);\n"
    "- 专有名词/人名/方法名/数据集名首次出现用「中文(English)」;\n"
    "- 只输出翻译后的 Markdown 正文,不要任何前言、说明或结尾提议。\n\n"
    "--- 论文原文 ---\n<<BODY>>"
)


if __name__ == "__main__":
    TranslatePaperStep.cli_main("04_translate_paper")
