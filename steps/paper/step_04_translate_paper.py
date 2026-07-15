"""Step 04: 论文翻译。AI 把非中文论文忠实翻译为简体中文 → output/translated.md
(供前端「译文」tab + 05_smart_paper 基于译文做笔记)。

仅非中文论文触发:02_pdf_parse 检测到非中文写 intermediate/needs_translation.json,本步经 rules:exists 门控。
05_smart_paper 是重组为中文笔记;本步是忠实全文翻译,保留章节结构、公式(LaTeX)、图表引用。
"""

from __future__ import annotations

import json

from shared.step_base import StepBase, file_hash
from shared.storage import read_path_bounded
from shared.terms import extract_pairs, hit_terms, render_term_block
from shared.note_text import markdown_to_index_text
from steps.article.provenance import (
    extract_attestable_note_markers,
    load_source_manifest,
    persist_note_provenance,
    translation_reference_block,
)
from steps.utils.chunking import split_markdown_chunks
from steps.utils.provenance_attestation import persist_semantic_candidates

# 单 chunk 字符预算:大论文(GPT-3 75页)整篇单调用必撞步/CLI 双 600s 超时(线上实证)。
# 16000 字英文原文的中文译文 ≈ 万级 tokens,稳在 max_tokens=16384 与单调用几分钟内;
# 小论文 fits 时仍是单块=行为不变。段落边界切,不破坏 Markdown 结构。
CHUNK_CHARS = 16000
MAX_PAPER_TEXT_SOURCE_BYTES = 8 * 1024 * 1024


class TranslatePaperStep(StepBase):
    def validate_inputs(self) -> list[str]:
        # 首选 output/original.md(arxiv-html 干净原文 / 文本解析兜底);备选 sections.json(遗留组装)。
        if (self.job_dir / "output" / "original.md").exists():
            return []
        if not (self.job_dir / "intermediate" / "sections.json").exists():
            return ["output/original.md|intermediate/sections.json"]
        return []

    def input_hashes(self) -> dict[str, str]:
        h: dict[str, str] = {}
        orig = self.job_dir / "output" / "original.md"
        if orig.exists():                      # 主源:干净原文(含图/公式),变了要重译
            h["original"] = file_hash(orig)
        else:
            h["sections"] = file_hash(self.job_dir / "intermediate" / "sections.json")
            figs = self.job_dir / "intermediate" / "figures.json"
            if figs.exists():                  # 遗留组装路径:译文含图表引用,图变了要重译
                h["figures"] = file_hash(figs)
        active_template = self.ai.active_prompt_name
        if active_template in {"04_translate_paper", "04_translate_paper.pdf"}:
            template_name = active_template
        else:
            template_name = (
                "04_translate_paper.pdf"
                if self._is_pdf_only()
                else "04_translate_paper"
            )
        t = self.ai.template_hash(template_name)
        if t:
            h["template"] = t
        source_manifest = self.job_dir / "intermediate" / "source_segments.json"
        if source_manifest.exists():
            h["source_segments"] = file_hash(source_manifest)
        return h

    def execute(self) -> dict | None:
        (self.job_dir / "output" / "provenance" / "translated.json").unlink(missing_ok=True)
        original = self._read_optional_text("output/original.md")
        if original is None and self._source_kind() == "pdf-only":
            return self._execute_pdf_direct()
        md = original if original is not None else self._source_markdown_from_sections()

        # 逐 chunk 翻译:每块一次 call_ai(各自有审计记录+transcript sidecar,call_index 自增),
        # 按原顺序聚合;max_tokens 抬高防单块译文截断(claude-cli 无视无害)。
        chunks = split_markdown_chunks(md, CHUNK_CHARS)
        base_map = self._load_term_map()
        new_pairs: dict[str, str] = {}   # L3:本篇滚动新定译名(chunk 间传递,收尾落盘回流)
        parts: list[str] = []
        semantic_candidates: list[dict] = []
        source_manifest = load_source_manifest(self.job_dir, pipeline="paper")
        for i, chunk in enumerate(chunks):
            self.progress.report(i, len(chunks), f"translating chunk {i + 1}/{len(chunks)}")
            merged = {**base_map, **new_pairs}
            block = render_term_block(hit_terms(chunk, merged))
            reference_block = translation_reference_block(
                source_manifest, source_text=chunk,
            )
            part = self.ai.call(
                self._build_prompt(chunk, block, reference_block), max_tokens=16384,
            ).strip()
            if source_manifest is not None and reference_block:
                part, _exact, pending = extract_attestable_note_markers(
                    part, source_manifest, ai=self.ai, force_semantic=True,
                )
                semantic_candidates.extend(pending)
            parts.append(part)
            for en, zh in extract_pairs(part).items():
                if en not in merged:      # 只收新词:已注入的恒定,避免中途改名
                    new_pairs[en] = zh
        self.progress.report(len(chunks), len(chunks), "done")
        result = "\n\n".join(parts)
        normalized_result = markdown_to_index_text(result)
        semantic_candidates = [
            candidate for candidate in semantic_candidates
            if normalized_result.count(candidate["anchor"]) == 1
        ]

        self.artifacts.write("output/translated.md", result)
        self._write_term_pairs(new_pairs)
        provenance = self._persist_translation_provenance(semantic_candidates)
        return {"chars": len(result), "chunks": len(chunks), "new_terms": len(new_pairs),
                "provider": self.ai.last_provider, "model": self.ai.last_model,
                "provenance_segments": provenance["segments"],
                "provenance_status": provenance["status"],
                "semantic_candidates": provenance["semantic_candidates"]}

    def _source_markdown(self) -> str:
        """翻译源文:首选 output/original.md(arxiv-html 由 02 产出,公式/图无损,图引用已在原位);
        缺失(老 pymupdf job 未重跑 02)回退 sections+figures 组装。"""
        original = self._read_optional_text("output/original.md")
        return original if original is not None else self._source_markdown_from_sections()

    def _source_markdown_from_sections(self) -> str:
        sections = self._load_json_bounded("intermediate/sections.json")
        figures: list = []
        try:
            loaded_figures = self._load_json_bounded("intermediate/figures.json")
        except FileNotFoundError:
            loaded_figures = []
        if isinstance(loaded_figures, list):
            figures = loaded_figures
        return self._paper_markdown(sections, figures)

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

    @staticmethod
    def _paper_markdown(sections: dict, figures: list | None = None) -> str:
        """从 sections.json 渲染论文 Markdown,供全文翻译。

        max_chars=None 是全文翻译不变量,输入规模由 chunk 管理。旧 job 若仍含
        figures.json,按页码插入渲染图;当前 arXiv 图片已在 original.md,PDF-only
        由模型直接读取 PDF。assets 引用必须原样保留。
        """
        from steps.utils.sections import render_section_tree
        parts: list[str] = []
        if sections.get("title"):
            parts.append(f"# {sections['title']}\n")
        if sections.get("authors"):
            parts.append(f"Authors: {', '.join(sections['authors'])}\n")
        if sections.get("abstract"):
            parts.append(f"\n## Abstract\n{sections['abstract']}\n")
        parts.append("\n")

        figs = sorted(
            (f for f in (figures or []) if f.get("filename")),
            key=lambda f: (f.get("page") or 0, f.get("index") or 0),
        )
        top = sections.get("sections", [])
        fi = 0
        for i, sec in enumerate(top):
            render_section_tree(sec, parts, level=2, max_chars=None)
            next_page = top[i + 1].get("page") if i + 1 < len(top) else None
            while fi < len(figs) and (next_page is None or (figs[fi].get("page") or 0) < next_page):
                f = figs[fi]
                parts.append(f"\n![](assets/{f['filename']})\n")
                caption = " ".join((f.get("caption") or "").split())
                if caption:
                    parts.append(f"*{caption}*\n")
                fi += 1
        return "".join(parts)

    def _build_prompt(
        self, md: str, term_block: str = "", reference_block: str = "",
    ) -> str:
        # <<TERMS>> = 本 chunk 命中的术语对照段(shared/terms.py;无命中为空串,prompt 无痕)。
        tmpl = self.ai.load_prompt_template("04_translate_paper")
        return (
            tmpl.replace("<<TERMS>>", term_block).replace("<<BODY>>", md)
            + reference_block
        )

    def _load_term_map(self) -> dict[str, str]:
        """input/term_map.json(scheduler 导出的 L1/L2 快照);缺失/坏 JSON 返回空表(降级无害)。"""
        try:
            m = self.artifacts.load_json("input/term_map.json")
            return m if isinstance(m, dict) else {}
        except Exception:
            return {}

    def _write_term_pairs(self, pairs: dict[str, str]) -> None:
        """本篇新定译名落产物,供 scheduler 回流 glossary/集合表(空表不写,省一个对象)。"""
        if pairs:
            import json as _json
            self.artifacts.write("output/term_pairs.json",
                              _json.dumps(pairs, ensure_ascii=False, indent=1))

    # ── pdf-only 直喂:无文本可抽(pymupdf 已废),claude Read 按页区间读 PDF 翻译 ──
    PAGES_PER_CHUNK = 2   # 实测每 2 页一块 ≈30-45s、3 turns;块大易撞轮次/超时,块小浪费轮次开销

    def _is_pdf_only(self) -> bool:
        if self._read_optional_text("output/original.md") is not None:
            return False
        return self._source_kind() == "pdf-only"

    def _source_kind(self) -> str | None:
        try:
            parsed = self._load_json_bounded("intermediate/parsed.json") or {}
        except Exception:
            return None
        return parsed.get("source_kind") if isinstance(parsed, dict) else None

    def _execute_pdf_direct(self) -> dict:
        pdf = (self.job_dir / "input" / "source.pdf").resolve()
        parsed = self._load_json_bounded("intermediate/parsed.json") or {}
        pages = int(parsed.get("pages") or 0) if isinstance(parsed, dict) else 0
        if pages <= 0:
            from shared.errors import InputInvalidError
            raise InputInvalidError("pdf-only translate needs parsed.json.pages > 0")
        ranges = [(i, min(i + self.PAGES_PER_CHUNK - 1, pages))
                  for i in range(1, pages + 1, self.PAGES_PER_CHUNK)]
        tmpl = self.ai.load_prompt_template("04_translate_paper.pdf")
        base_map = self._load_term_map()
        new_pairs: dict[str, str] = {}
        parts: list[str] = []
        semantic_candidates: list[dict] = []
        source_manifest = load_source_manifest(self.job_dir, pipeline="paper")
        for n, (a, b) in enumerate(ranges):
            self.progress.report(n, len(ranges), f"translating pages {a}-{b}/{pages}")
            # pdf 直喂看不到原文文本 → 术语命中退化为「已收对照 + 全库表」直接给
            # (表通常远小于 40 上限;超限按注入表意义排序:L3 新词优先保留)。
            merged = {**base_map, **new_pairs}
            hits = (list(new_pairs.items()) + [kv for kv in base_map.items() if kv[0] not in new_pairs])[:40]
            block = render_term_block(hits)
            prompt = (tmpl.replace("<<TERMS>>", block)
                          .replace("<<PDF_PATH>>", str(pdf))
                          .replace("<<START>>", str(a)).replace("<<END>>", str(b))
                      + translation_reference_block(
                          source_manifest, page_range=(a, b),
                      ))
            # Read 每页一轮 + 思考/生成余量;--add-dir 放行 PDF 所在目录(input/)。
            part = self.ai.call(prompt, max_tokens=16384,
                                allowed_tools=["Read"], add_dirs=[str(pdf.parent)],
                                max_turns=(b - a + 1) * 2 + 4).strip()
            if source_manifest is not None:
                part, _exact, pending = extract_attestable_note_markers(
                    part, source_manifest, ai=self.ai, force_semantic=True,
                )
                semantic_candidates.extend(pending)
            parts.append(part)
            for en, zh in extract_pairs(part).items():
                if en not in merged:
                    new_pairs[en] = zh
        self.progress.report(len(ranges), len(ranges), "done")
        result = "\n\n".join(parts)
        result, fig_pages = self._link_figure_pages(result, pages)
        normalized_result = markdown_to_index_text(result)
        semantic_candidates = [
            candidate for candidate in semantic_candidates
            if normalized_result.count(candidate["anchor"]) == 1
        ]
        self.artifacts.write("output/translated.md", result)
        self._write_term_pairs(new_pairs)
        provenance = self._persist_translation_provenance(semantic_candidates)
        return {"chars": len(result), "chunks": len(ranges), "mode": "pdf-direct",
                "figure_pages": fig_pages,
                "provider": self.ai.last_provider, "model": self.ai.last_model,
                "provenance_segments": provenance["segments"],
                "provenance_status": provenance["status"],
                "semantic_candidates": provenance["semantic_candidates"]}

    def _persist_translation_provenance(
        self, semantic_candidates: list[dict],
    ) -> dict:
        """producer 发布空 final 与无信任候选,由下游 concepts 独立证明。"""
        provenance = persist_note_provenance(
            self.job_dir,
            pipeline="paper",
            note_type="translated",
            note_artifact="output/translated.md",
            candidates=[],
        )
        candidate_state = persist_semantic_candidates(
            self.job_dir,
            pipeline="paper",
            note_type="translated",
            note_artifact="output/translated.md",
            candidates=semantic_candidates,
        )
        return {**provenance, "semantic_candidates": candidate_state["candidates"]}

    # 占位行:【图 N|第 p 页】/【表 N|第 p 页】(prompt 规则 3;旧译文无 |页码 → 不匹配自然跳过)。
    _FIG_PAGE_RE = __import__("re").compile(r"【[图表]\s*[\d.]+[^】|]*\|\s*第\s*(\d+)\s*页】")

    def _link_figure_pages(self, text: str, total_pages: int) -> tuple[str, int]:
        """图表占位 → 跳原文链接:【图 N|第 p 页】改写为「图注 + [查看原图(原文第 p 页)](#pdf-page=p)」。
        前端 MarkdownViewer 拦截 #pdf-page= 链接切「原文」tab 并让 PDF iframe 跳 #page=p——原生渲染
        =图 100% 保真。旧方案(pdftoppm 渲染整页 A4 插图)已废:整页截图含原文正文,插进译文把
        阅读流切碎(线上 101 Alphas 实证不可读)。旧译文无 |页码 的占位不匹配自然跳过。"""
        n = 0
        out_lines: list[str] = []
        for line in text.splitlines():
            m = self._FIG_PAGE_RE.search(line)
            if m and 1 <= int(m.group(1)) <= total_pages:
                p = int(m.group(1))
                out_lines.append(line.rstrip() + f"  [查看原图(原文第 {p} 页)](#pdf-page={p})")
                n += 1
            else:
                out_lines.append(line)
        return "\n".join(out_lines), n



if __name__ == "__main__":
    TranslatePaperStep.cli_main("04_translate_paper")
