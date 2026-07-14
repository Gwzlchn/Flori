"""Step 04: 文章智能笔记。AI 将文章正文重组为中文结构化笔记。"""

from __future__ import annotations

from shared.step_base import StepBase, file_hash
from steps.article.provenance import (
    extract_note_markers,
    load_source_manifest,
    persist_note_provenance,
    source_reference_block,
)


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
        source_manifest = self.job_dir / "intermediate" / "source_segments.json"
        if source_manifest.exists():
            hashes["source_segments"] = file_hash(source_manifest)
        hashes.update(self.ai.prompt_profile_style_hashes())  # prompt(可选覆盖)+ profile + styles
        return hashes

    def execute(self) -> dict | None:
        (self.job_dir / "output" / "provenance" / "smart.json").unlink(missing_ok=True)
        sections = self.artifacts.load_json("intermediate/sections.json")
        # 非中文文章:基于中文译文做笔记(对齐 04_translate 依赖),术语与译文一致,避免重复英译中。
        translated = self.job_dir / "output" / "translated.md"
        body = translated.read_text(encoding="utf-8") if translated.exists() else None

        source_manifest = load_source_manifest(self.job_dir, pipeline="article")
        prompt = self._build_prompt(sections, body, source_manifest=source_manifest)
        # 结构化中文笔记常超默认 4096 output tokens,显式抬高上限防被静默截断(claude-cli 无视无害)。
        result = self.ai.call(prompt, max_tokens=8192)

        candidates = []
        if source_manifest is not None:
            result, candidates = extract_note_markers(result, source_manifest)
        elif "[[source:" in result:
            raise ValueError("article note contains a source marker without a manifest")
        rel = self.review.write_smart_note(result)   # 版本化落盘,含生成时间/方式/模型
        provenance = persist_note_provenance(
            self.job_dir,
            pipeline="article",
            note_type="smart",
            note_artifact=rel,
            candidates=candidates,
        )
        return {"chars": len(result), "provider": self.ai.last_provider,
                "model": self.ai.last_model, "note_file": rel,
                "source": "translation" if body else "original",
                "provenance_segments": provenance["segments"],
                "provenance_status": provenance["status"]}

    def _build_prompt(
        self,
        sections: dict,
        body: str | None = None,
        *,
        source_manifest: dict | None = None,
    ) -> str:
        profile = self.ai.load_domain_prompt_profile()

        parts = [self.ai.load_prompt_template("04_smart_article")]

        parts.append(self.ai.terminology_block(profile))  # 已沉淀标准概念注入(共用)

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

        source_block = source_reference_block(source_manifest)
        if source_block:
            parts.append(source_block)

        return "".join(parts)

    def _render_section(self, section: dict, parts: list, level: int) -> None:
        from steps.utils.sections import render_section_tree
        render_section_tree(section, parts, level)


if __name__ == "__main__":
    SmartArticleStep.cli_main("04_smart_article")
