"""Step 04: 文章智能笔记。AI 将文章正文重组为中文结构化笔记。"""

from __future__ import annotations

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
        translated = self.job_dir / "output" / "translated.md"
        if translated.exists():
            hashes["translated"] = file_hash(translated)   # 非中文文章随译文变化重跑
        hashes.update(self.prompt_profile_style_hashes())  # prompt(可选覆盖)+ profile + styles
        return hashes

    def execute(self) -> dict | None:
        sections = self.load_json("intermediate/sections.json")
        # 非中文文章:基于中文译文做笔记(对齐 04_translate 依赖),术语与译文一致,避免重复英译中。
        translated = self.job_dir / "output" / "translated.md"
        body = translated.read_text(encoding="utf-8") if translated.exists() else None

        prompt = self._build_prompt(sections, body)
        # 结构化中文笔记常超默认 4096 output tokens,显式抬高上限防被静默截断(claude-cli 无视无害)。
        result = self.call_ai(prompt, max_tokens=8192)

        rel = self.write_smart_note(result)   # 版本化落盘,含生成时间/方式/模型
        return {"chars": len(result), "provider": self.last_ai_provider,
                "model": self.last_ai_model, "note_file": rel,
                "source": "translation" if body else "original"}

    def _build_prompt(self, sections: dict, body: str | None = None) -> str:
        profile = self.load_domain_prompt_profile()

        # 静态指令头外置 templates/04_smart_article.md(改文件不碰代码,经 prompt_profile_style_hashes 进指纹);缺失回退 _DEFAULT_HEADER。
        parts = [self._load_prompt_template("04_smart_article", _DEFAULT_HEADER)]

        parts.append(self.terminology_block(profile))  # 已沉淀标准概念注入(共用)

        parts.append(f"\n文章标题：{sections.get('title', '未知')}\n")
        authors = sections.get("authors", [])
        if authors:
            parts.append(f"作者：{', '.join(authors)}\n")

        if sections.get("abstract"):
            parts.append(f"\n摘要：{sections['abstract']}\n")

        parts.append("\n--- 正文内容 ---\n")
        if body is not None:                              # 非中文文章:用中文译文(已含结构标题)
            parts.append(body)
        else:                                             # 中文文章:用原文章节树
            for sec in sections.get("sections", []):
                self._render_section(sec, parts, level=2)

        return "".join(parts)

    def _render_section(self, section: dict, parts: list, level: int) -> None:
        from steps.utils.sections import render_section_tree
        render_section_tree(section, parts, level)


# 静态指令头(= 外置模板内容;templates/04_smart_article.md 由此生成)。动态部分(标题/正文/术语)仍在代码拼。
_DEFAULT_HEADER = (
    "请将以下文章内容整理为中文结构化学习笔记。\n"
    "要求：\n"
    "- 提炼文章核心观点与关键信息\n"
    "- 梳理论证脉络，按逻辑结构组织\n"
    "- 保留重要事实、数据与结论\n"
    "- 使用 Markdown 格式，含 ## 章节标题\n"
)

if __name__ == "__main__":
    SmartArticleStep.cli_main("04_smart_article")
