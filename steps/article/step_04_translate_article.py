"""Step 04: 文章翻译。AI 把非中文正文忠实翻译为简体中文,保留 Markdown 结构与图片引用。

仅非中文文章触发:02_parse 检测到非中文写 intermediate/needs_translation.json,本步经 rules:exists 门控。
与 04_smart(意译重组为笔记)不同——这里是忠实全文翻译,产出 output/translated.md 供前端「译文」tab。
译原文 markdown(已含内联图)→ 译文天然保留图位。
"""

from __future__ import annotations

from shared.step_base import StepBase, file_hash
from shared.terms import extract_pairs, hit_terms, render_term_block
from steps.article.provenance import persist_note_provenance
from steps.utils.chunking import split_markdown_chunks

# 单 chunk 字符预算(与 04_translate_paper 同理):超长文整篇单调用会撞步/CLI 双 600s 超时;
# 段落边界切不破坏 Markdown/图位,小文 fits 时单块=行为不变。
CHUNK_CHARS = 16000


class TranslateArticleStep(StepBase):
    def validate_inputs(self) -> list[str]:
        if not (self.job_dir / "output" / "original.md").exists():
            return ["output/original.md"]
        return []

    def input_hashes(self) -> dict[str, str]:
        h = {"original": file_hash(self.job_dir / "output" / "original.md")}
        t = self.ai.template_hash("04_translate_article")
        if t:
            h["template"] = t
        source_manifest = self.job_dir / "intermediate" / "source_segments.json"
        if source_manifest.exists():
            h["source_segments"] = file_hash(source_manifest)
        return h

    def execute(self) -> dict | None:
        (self.job_dir / "output" / "provenance" / "translated.json").unlink(missing_ok=True)
        md = (self.job_dir / "output" / "original.md").read_text(encoding="utf-8")

        # 逐 chunk 翻译(每块一次 call_ai=各自审计+transcript sidecar),按原顺序聚合;
        # max_tokens 抬高防单块译文截断(claude-cli 无视无害)。
        chunks = split_markdown_chunks(md, CHUNK_CHARS)
        base_map = self._load_term_map()
        new_pairs: dict[str, str] = {}   # L3:本篇滚动新定译名(chunk 间传递,收尾落盘回流)
        parts: list[str] = []
        for i, chunk in enumerate(chunks):
            self.progress.report(i, len(chunks), f"translating chunk {i + 1}/{len(chunks)}")
            merged = {**base_map, **new_pairs}
            block = render_term_block(hit_terms(chunk, merged))
            part = self.ai.call(self._build_prompt(chunk, block), max_tokens=16384).strip()
            parts.append(part)
            for en, zh in extract_pairs(part).items():
                if en not in merged:      # 只收新词:已注入的恒定,避免中途改名
                    new_pairs[en] = zh
        self.progress.report(len(chunks), len(chunks), "done")
        result = "\n\n".join(parts)

        self.artifacts.write("output/translated.md", result)
        provenance = persist_note_provenance(
            self.job_dir,
            pipeline="article",
            note_type="translated",
            note_artifact="output/translated.md",
            candidates=[],
        )
        if new_pairs:
            import json as _json
            self.artifacts.write("output/term_pairs.json",
                              _json.dumps(new_pairs, ensure_ascii=False, indent=1))
        return {"chars": len(result), "chunks": len(chunks), "new_terms": len(new_pairs),
                "provider": self.ai.last_provider, "model": self.ai.last_model,
                "provenance_segments": provenance["segments"],
                "provenance_status": provenance["status"]}

    def _build_prompt(self, md: str, term_block: str = "") -> str:
        # <<TERMS>> = 本 chunk 命中的术语对照段(shared/terms.py;无命中为空串,prompt 无痕)。
        tmpl = self.ai.load_prompt_template("04_translate_article")
        return tmpl.replace("<<TERMS>>", term_block).replace("<<BODY>>", md)

    def _load_term_map(self) -> dict[str, str]:
        """input/term_map.json(scheduler 导出的 L1/L2 快照);缺失/坏 JSON 返回空表(降级无害)。"""
        try:
            m = self.artifacts.load_json("input/term_map.json")
            return m if isinstance(m, dict) else {}
        except Exception:
            return {}


if __name__ == "__main__":
    TranslateArticleStep.cli_main("04_translate_article")
