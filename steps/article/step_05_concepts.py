"""Step 05: 概念提取 + 一句话摘要(必跑)。

有智能笔记则从笔记抽,否则从原文/章节抽;产出 output/concepts.json
({summary, key_terms:[{term,definition}]})供 scheduler._collect_glossary 采集进图谱。
即便关闭智能笔记/评审,本步仍跑 → 概念始终进图谱。"""

from __future__ import annotations

from shared.step_base import StepBase, file_hash


class ArticleConceptsStep(StepBase):
    def validate_inputs(self) -> list[str]:
        if not (self.job_dir / "intermediate" / "sections.json").exists():
            return ["intermediate/sections.json"]
        return []

    def input_hashes(self) -> dict[str, str]:
        hashes: dict[str, str] = {
            "sections": file_hash(self.job_dir / "intermediate" / "sections.json"),
        }
        note = self.latest_smart_note()
        if note:
            hashes["smart"] = file_hash(note)   # 有笔记则随笔记变化重跑
        translated = self.job_dir / "output" / "translated.md"
        if translated.exists():
            hashes["translated"] = file_hash(translated)   # 非中文文章随译文变化重跑
        hashes.update(self.prompt_profile_style_hashes())
        return hashes

    def execute(self) -> dict | None:
        source_text, source = self._source_text()
        prompt = self._build_prompt(source_text)
        result, parse_failed = self.call_ai_json(
            prompt, fallback={"summary": "", "key_terms": []},
        )
        key_terms = result.get("key_terms") or []
        out = {
            "summary": (result.get("summary") or "").strip(),
            "key_terms": key_terms,
            "source": source,            # 'smart_note' | 'translation' | 'original'(概念抽自哪)
            "parse_failed": parse_failed,
        }
        self.write_output("output/concepts.json", out)
        return {"concepts": len(key_terms), "source": source,
                "summary_len": len(out["summary"]), "parse_failed": parse_failed,
                "provider": self.last_ai_provider, "model": self.last_ai_model}

    def _source_text(self) -> tuple[str, str]:
        """概念抽取的源文本优先级:智能笔记 > 译文(非中文文章)> 原文章节。
        非中文文章基于中文译文抽概念/摘要,与译文术语一致(对齐 04_translate 依赖)。"""
        note = self.latest_smart_note()
        if note:
            return note.read_text(encoding="utf-8"), "smart_note"
        translated = self.job_dir / "output" / "translated.md"
        if translated.exists():
            return translated.read_text(encoding="utf-8"), "translation"
        sections = self.load_json("intermediate/sections.json")
        parts: list[str] = []
        if sections.get("title"):
            parts.append(f"# {sections['title']}\n")
        if sections.get("abstract"):
            parts.append(sections["abstract"] + "\n")
        from steps.utils.sections import render_section_tree
        for sec in sections.get("sections", []):
            render_section_tree(sec, parts, level=2)
        return "".join(parts), "original"

    def _build_prompt(self, text: str) -> str:
        profile = self.load_domain_prompt_profile()
        clip = text[:12000]   # 概念抽取不需全文逐字,限长防超
        # 静态指令头外置 templates/05_concepts.md(经 prompt_profile_style_hashes 进指纹);缺失回退 _DEFAULT_HEADER。
        parts = [self._load_prompt_template("05_concepts", _DEFAULT_HEADER)]
        parts.append(self.terminology_block(profile))   # 已沉淀标准概念注入(共用)
        parts.append("\n--- 内容 ---\n")
        parts.append(clip)
        return "".join(parts)


# 静态指令头(= 外置模板 templates/05_concepts.md 内容)。动态(术语/内容)仍在代码拼。
_DEFAULT_HEADER = (
    "请从以下内容提取【核心概念】并写一句话摘要,严格输出 JSON。\n"
    "要求:\n"
    "- key_terms:文中讲清楚的关键概念(术语),每个给一句简洁中文定义;"
    "英文专有名词原样保留、不翻译。\n"
    "- zh_name:该术语的【标准中文译名】(不是解释,是短译名,如 Kelly criterion→凯利准则);"
    "term 本身是中文或无通行译名时为 null。\n"
    "- summary:用一句话(≤60 字)概括全文要点。\n"
    '- 输出格式:{"summary": "...", "key_terms": [{"term": "...", "definition": "...", "zh_name": "...|null"}]}\n'
    "- 只输出 JSON,不要额外解释或代码块标记。\n"
)


if __name__ == "__main__":
    ArticleConceptsStep.cli_main("05_concepts")
