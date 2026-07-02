"""Step 04: 论文翻译。AI 把非中文论文忠实翻译为简体中文 → output/translated.md
(供前端「译文」tab + 05_smart_paper 基于译文做笔记)。

仅非中文论文触发:02_pdf_parse 检测到非中文写 intermediate/needs_translation.json,本步经 rules:exists 门控。
05_smart_paper 是重组为中文笔记;本步是忠实全文翻译,保留章节结构、公式(LaTeX)、图表引用。
"""

from __future__ import annotations

from shared.step_base import StepBase, file_hash
from steps.utils.chunking import split_markdown_chunks

# 单 chunk 字符预算:大论文(GPT-3 75页)整篇单调用必撞步/CLI 双 600s 超时(线上实证)。
# 16000 字英文原文的中文译文 ≈ 万级 tokens,稳在 max_tokens=16384 与单调用几分钟内;
# 小论文 fits 时仍是单块=行为不变。段落边界切,不破坏 Markdown 结构。
CHUNK_CHARS = 16000


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
        t = self.template_hash("04_translate_paper")
        if t:
            h["template"] = t
        return h

    def execute(self) -> dict | None:
        if self._is_pdf_only():
            return self._execute_pdf_direct()
        md = self._source_markdown()

        # 逐 chunk 翻译:每块一次 call_ai(各自有审计记录+transcript sidecar,call_index 自增),
        # 按原顺序聚合;max_tokens 抬高防单块译文截断(claude-cli 无视无害)。
        chunks = split_markdown_chunks(md, CHUNK_CHARS)
        parts: list[str] = []
        for i, chunk in enumerate(chunks):
            self.report_progress(i, len(chunks), f"translating chunk {i + 1}/{len(chunks)}")
            parts.append(self.call_ai(self._build_prompt(chunk), max_tokens=16384).strip())
        self.report_progress(len(chunks), len(chunks), "done")
        result = "\n\n".join(parts)

        self.write_output("output/translated.md", result)
        return {"chars": len(result), "chunks": len(chunks),
                "provider": self.last_ai_provider, "model": self.last_ai_model}

    def _source_markdown(self) -> str:
        """翻译源文:首选 output/original.md(arxiv-html 由 02 产出,公式/图无损,图引用已在原位);
        缺失(老 pymupdf job 未重跑 02)回退 sections+figures 组装。"""
        orig = self.job_dir / "output" / "original.md"
        if orig.exists():
            return orig.read_text(encoding="utf-8")
        sections = self.load_json("intermediate/sections.json")
        figures: list = []
        if (self.job_dir / "intermediate" / "figures.json").exists():
            figures = self.load_json("intermediate/figures.json")
        return self._paper_markdown(sections, figures)

    @staticmethod
    def _paper_markdown(sections: dict, figures: list | None = None) -> str:
        """从 sections.json 拼出论文可读 Markdown(标题/作者/摘要/章节树)供翻译。
        ★max_chars=None 不截断:忠实全文翻译必须喂全文(默认 2000 字/节的截断是笔记类
        prompt 的预算控制,曾让"全文翻译"实际只译了每节前 2000 字);规模由 chunk 管。
        渲染图(04_figures)按页码插到对应顶级章节之后——否则译文 0 图,「保留配图」名不副实;
        prompt 要求 ![](assets/…) 引用行原样保留,图注(斜体行)随文翻译。"""
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

    def _build_prompt(self, md: str) -> str:
        # 默认模板外置 templates/04_translate_paper.md(改文件不碰代码);缺失回退 _DEFAULT。
        tmpl = self._load_prompt_template("04_translate_paper", _DEFAULT)
        return tmpl.replace("<<BODY>>", md)

    # ── pdf-only 直喂:无文本可抽(pymupdf 已废),claude Read 按页区间读 PDF 翻译 ──
    PAGES_PER_CHUNK = 2   # 实测每 2 页一块 ≈30-45s、3 turns;块大易撞轮次/超时,块小浪费轮次开销

    def _is_pdf_only(self) -> bool:
        try:
            parsed = self.load_json("intermediate/parsed.json") or {}
        except Exception:
            return False
        return parsed.get("source_kind") == "pdf-only"

    def _execute_pdf_direct(self) -> dict:
        pdf = (self.job_dir / "input" / "source.pdf").resolve()
        pages = int((self.load_json("intermediate/parsed.json") or {}).get("pages") or 0)
        if pages <= 0:
            from shared.errors import InputInvalidError
            raise InputInvalidError("pdf-only translate needs parsed.json.pages > 0")
        ranges = [(i, min(i + self.PAGES_PER_CHUNK - 1, pages))
                  for i in range(1, pages + 1, self.PAGES_PER_CHUNK)]
        tmpl = self._load_prompt_template("04_translate_paper.pdf", _DEFAULT_PDF)
        parts: list[str] = []
        for n, (a, b) in enumerate(ranges):
            self.report_progress(n, len(ranges), f"translating pages {a}-{b}/{pages}")
            prompt = (tmpl.replace("<<PDF_PATH>>", str(pdf))
                          .replace("<<START>>", str(a)).replace("<<END>>", str(b)))
            # Read 每页一轮 + 思考/生成余量;--add-dir 放行 PDF 所在目录(input/)。
            parts.append(self.call_ai(prompt, max_tokens=16384,
                                      allowed_tools=["Read"], add_dirs=[str(pdf.parent)],
                                      max_turns=(b - a + 1) * 2 + 4).strip())
        self.report_progress(len(ranges), len(ranges), "done")
        result = "\n\n".join(parts)
        self.write_output("output/translated.md", result)
        return {"chars": len(result), "chunks": len(ranges), "mode": "pdf-direct",
                "provider": self.last_ai_provider, "model": self.last_ai_model}


# pdf-only 直喂的分块翻译 prompt(= 外置模板 templates/04_translate_paper.pdf.md;
# <<PDF_PATH>>/<<START>>/<<END>> 运行期注入)。规则经 OSDI04 MapReduce 三轮人工对读验证:
# 层级映射与「截断句照译」是多块聚合一致性的关键。
_DEFAULT_PDF = (
    "用 Read 工具读取 <<PDF_PATH>> 的第 <<START>> 页到第 <<END>> 页,把正文忠实翻译成中文。规则:\n"
    "1. 标题层级固定映射:论文主标题用 #(仅出现在含主标题的页);编号章节 N 用 ##;小节 N.M 用 ###;"
    "N.M.P 用 ####;无编号的段落小标题(如 Worker Failure)用**加粗**不用 #。"
    "标题翻译成中文,括号保留英文原文(如「## 3 实现(Implementation)」)。\n"
    "2. 不增删内容、不概括;代码/伪代码块用 ``` 围栏原样保留(代码不译,注释可译)。\n"
    "3. 图表:在图/表出现位置写一行「【图 N】+图注中文翻译」/「【表 N】+…」。\n"
    "4. 页首/页尾被截断的句子照常翻译,不补全不省略(与相邻页区间自然衔接)。\n"
    "5. 引用标记([1]/作者年份)与专有名词原样保留;只输出译文,不要任何解释。\n"
)

# 静态默认 prompt 骨架(= 外置模板内容;<<BODY>> 注入论文原文)。
_DEFAULT = (
    "请将以下论文【忠实翻译】为简体中文。这是翻译,不是笔记/摘要,要求:\n"
    "- 忠实原意,逐段完整翻译,不增删、不概括、不评论;\n"
    "- 完整保留 Markdown 结构(标题层级、列表、表格、引用等)与原文章节顺序;\n"
    "- 数学公式、变量名、代码、算法伪代码原样保留(LaTeX 不译);\n"
    "- 专有名词/人名/方法名/数据集名首次出现用「中文(English)」;\n"
    "- 图片引用行(![](assets/…))必须原样保留在原位,不译、不改路径;其后的斜体图注行随文翻译;\n"
    "- 只输出翻译后的 Markdown 正文,不要任何前言、说明或结尾提议。\n\n"
    "--- 论文原文 ---\n<<BODY>>"
)


if __name__ == "__main__":
    TranslatePaperStep.cli_main("04_translate_paper")
