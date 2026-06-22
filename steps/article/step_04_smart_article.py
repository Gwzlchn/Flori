"""Step 04: 文章智能笔记。AI 将文章正文重组为中文结构化笔记。"""

from __future__ import annotations

import json

from shared.step_base import StepBase, file_hash


class SmartArticleStep(StepBase):
    def validate_inputs(self) -> list[str]:
        if not (self.job_dir / "intermediate" / "sections.json").exists():
            return ["intermediate/sections.json"]
        return []

    def input_hashes(self) -> dict[str, str]:
        hashes: dict[str, str] = {
            "sections": file_hash(self.job_dir / "intermediate" / "sections.json"),
        }
        hashes.update(self.prompt_profile_style_hashes())  # prompt(可选覆盖)+ profile + styles
        return hashes

    def execute(self) -> dict | None:
        sections = self.load_json("intermediate/sections.json")

        prompt = self._build_prompt(sections)
        # 结构化中文笔记常超默认 4096 output tokens,显式抬高上限防被静默截断(claude-cli 无视无害)。
        result = self.call_ai(prompt, max_tokens=8192)

        rel = self.write_smart_note(result)   # 版本化落盘(含生成时间/方式/模型),不再写 notes_smart.md
        return {"chars": len(result), "provider": self.last_ai_provider,
                "model": self.last_ai_model, "note_file": rel}

    def _build_prompt(self, sections: dict) -> str:
        profile = self.load_domain_profile()

        parts = [
            "请将以下文章内容整理为中文结构化学习笔记。\n",
            "要求：\n",
            "- 提炼文章核心观点与关键信息\n",
            "- 梳理论证脉络，按逻辑结构组织\n",
            "- 保留重要事实、数据与结论\n",
            "- 使用 Markdown 格式，含 ## 章节标题\n",
        ]

        if profile and profile.get("terminology"):
            terms = "; ".join(profile["terminology"][:30])
            # 回流(§1.8 ③)：注入本域已沉淀概念的标准定义,命中用统一措辞、不重复展开,
            # 只对未列出的新概念做首次解释——避免同概念每篇换一套说法。
            parts.append(
                "\n本领域已沉淀的标准概念（命中时沿用统一措辞、无需重新展开解释；"
                f"只对下列未涵盖的新概念做首次解释）：\n{terms}\n"
            )

        parts.append(f"\n文章标题：{sections.get('title', '未知')}\n")
        authors = sections.get("authors", [])
        if authors:
            parts.append(f"作者：{', '.join(authors)}\n")

        if sections.get("abstract"):
            parts.append(f"\n摘要：{sections['abstract']}\n")

        parts.append("\n--- 正文内容 ---\n")
        for sec in sections.get("sections", []):
            self._render_section(sec, parts, level=2)

        return "".join(parts)

    def _render_section(self, section: dict, parts: list, level: int) -> None:
        from steps.utils.sections import render_section_tree
        render_section_tree(section, parts, level)

if __name__ == "__main__":
    SmartArticleStep.cli_main("04_smart_article")
