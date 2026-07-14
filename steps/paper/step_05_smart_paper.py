"""Step 05: 论文智能笔记。AI 将论文内容重组为中文结构化笔记。"""

from __future__ import annotations

import json

from shared.step_base import StepBase, file_hash
from shared.storage import read_path_bounded


MAX_PAPER_TEXT_SOURCE_BYTES = 8 * 1024 * 1024


class SmartPaperStep(StepBase):
    def validate_inputs(self) -> list[str]:
        if not (self.job_dir / "intermediate" / "sections.json").exists():
            return ["intermediate/sections.json"]
        return []

    def input_hashes(self) -> dict[str, str]:
        hashes: dict[str, str] = {
            "sections": file_hash(self.job_dir / "intermediate" / "sections.json"),
        }
        figs = self.job_dir / "intermediate" / "figures.json"
        if figs.exists():   # 仅旧 pymupdf job 有(04_figures 已删);arxiv-html 图在正文,pdf-only 图在 PDF
            hashes["figures"] = file_hash(figs)
        translated = self.job_dir / "output" / "translated.md"
        if translated.exists():
            hashes["translated"] = file_hash(translated)   # 非中文论文随译文变化重跑
        hashes.update(self.prompt_profile_style_hashes())  # prompt(可选覆盖)+ profile + styles
        return hashes

    def execute(self) -> dict | None:
        sections = self._load_json_bounded("intermediate/sections.json")
        figures: list = []
        try:
            loaded_figures = self._load_json_bounded("intermediate/figures.json")
        except FileNotFoundError:
            loaded_figures = []
        if isinstance(loaded_figures, list):
            figures = loaded_figures
        # 正文来源优先级:中文译文(非中文论文,术语与译文一致)> 干净原文(arxiv-html/中文论文)。
        body = None
        body_source = None
        for rel in ("output/translated.md", "output/original.md"):
            body = self._read_optional_text(rel)
            if body is not None:
                body_source = "translation" if rel.endswith("translated.md") else "original"
                break
        # pdf-only 且无任何文本正文(翻译被跳过/失败后手动重跑笔记):直喂 PDF,claude Read 逐页读。
        if body is None and self._source_kind() == "pdf-only":
            return self._execute_pdf_direct(sections, figures)

        # 有内嵌位图的图(filename 非空、index 有值)给 AI 用 ![中文图注](img:N) 占位符引用,落盘回填。
        image_assets = [{"n": f["index"], "filename": f["filename"]}
                        for f in figures
                        if f.get("filename") and f.get("index") is not None]

        prompt = self._build_prompt(sections, figures, body)
        # 结构化中文笔记常超默认 4096 output tokens,显式抬高上限防被静默截断(claude-cli 无视无害)。
        result = self.call_ai(prompt, max_tokens=8192)

        rel = self.write_smart_note(result, image_assets=image_assets)  # 回填占位符 + 版本化落盘
        return {"chars": len(result), "provider": self.last_ai_provider,
                "model": self.last_ai_model, "note_file": rel,
                "source": body_source or "original"}

    def _read_optional_text(self, rel_path: str) -> str | None:
        try:
            data = read_path_bounded(
                self.job_dir / rel_path,
                MAX_PAPER_TEXT_SOURCE_BYTES,
                trusted_root=self.job_dir,
            )
        except FileNotFoundError:
            return None
        if len(data) > MAX_PAPER_TEXT_SOURCE_BYTES:
            raise ValueError(f"paper source exceeds {MAX_PAPER_TEXT_SOURCE_BYTES} bytes")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"paper source is not UTF-8: {rel_path}") from exc
        return text if text.strip() else None

    def _load_json_bounded(self, rel_path: str):
        text = self._read_optional_text(rel_path)
        if text is None:
            raise FileNotFoundError(rel_path)
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"paper JSON source is invalid: {rel_path}") from exc

    def _source_kind(self) -> str | None:
        try:
            parsed = self._load_json_bounded("intermediate/parsed.json") or {}
            return parsed.get("source_kind") if isinstance(parsed, dict) else None
        except Exception:
            return None

    def _execute_pdf_direct(self, sections: dict, figures: list) -> dict:
        """pdf-only 直喂:无文本正文可用时,让 claude 用 Read 工具直接读 PDF 产笔记
        (worker 镜像带 poppler,Read 按页渲染;上限 60 页防超长 agentic)。"""
        pdf = (self.job_dir / "input" / "source.pdf").resolve()
        parsed = self._load_json_bounded("intermediate/parsed.json") or {}
        pages = int(parsed.get("pages") or 0) if isinstance(parsed, dict) else 0
        cap = min(pages or 30, 60)
        prompt = (self._load_prompt_template("05_smart_paper")
                  + self.terminology_block(self.load_domain_prompt_profile())
                  + f"\n论文标题：{sections.get('title', '未知')}\n"
                  + f"\n用 Read 工具阅读论文 PDF:{pdf}(共 {pages} 页,读前 {cap} 页),"
                    "然后按上面要求产出中文结构化学习笔记(不内嵌图片占位符)。\n")
        result = self.call_ai(prompt, max_tokens=8192,
                              allowed_tools=["Read"], add_dirs=[str(pdf.parent)],
                              max_turns=cap * 2 + 6)
        rel = self.write_smart_note(result, image_assets=[])
        return {"chars": len(result), "provider": self.last_ai_provider,
                "model": self.last_ai_model, "note_file": rel, "source": "pdf-direct"}

    def _build_prompt(self, sections: dict, figures: list, body: str | None = None) -> str:
        profile = self.load_domain_prompt_profile()

        parts = [self._load_prompt_template("05_smart_paper")]

        parts.append(self.terminology_block(profile))  # 注入已沉淀的标准概念,各 smart 步共用

        parts.append(f"\n论文标题：{sections.get('title', '未知')}\n")
        parts.append(f"作者：{', '.join(sections.get('authors', []))}\n")

        if sections.get("abstract"):
            parts.append(f"\n摘要：{sections['abstract']}\n")

        parts.append("\n--- 章节内容 ---\n")
        if body is not None:                              # 非中文论文:用中文译文(已含章节结构)
            parts.append(body)
        else:                                             # 中文论文:用原文章节树
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
