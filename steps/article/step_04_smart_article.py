"""Step 18: 文章智能笔记。AI 将文章正文重组为中文结构化笔记。"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from shared.step_base import StepBase, file_hash


class SmartArticleStep(StepBase):
    def validate_inputs(self) -> list[str]:
        if not (self.job_dir / "intermediate" / "sections.json").exists():
            return ["intermediate/sections.json"]
        return []

    def input_hashes(self) -> dict[str, str]:
        prompts_dir = Path(self.config["paths"]["prompts_dir"])
        domain_name = self.config["domain"]["name"]
        hashes: dict[str, str] = {
            "sections": file_hash(self.job_dir / "intermediate" / "sections.json"),
        }
        prompt_path = prompts_dir / "04_smart_article.md"
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

        prompt = self._build_prompt(sections)
        result = self.call_ai(prompt)

        rel = self.write_smart_note(result)   # 版本化落盘(含生成时间/方式/模型),不再写 notes_smart.md
        return {"chars": len(result), "provider": self.last_ai_provider,
                "model": self.last_ai_model, "note_file": rel}

    def _build_prompt(self, sections: dict) -> str:
        profile = self._load_profile()

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
            parts.append(f"\n术语参考：{terms}\n")

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
    SmartArticleStep.cli_main("04_smart_article")
