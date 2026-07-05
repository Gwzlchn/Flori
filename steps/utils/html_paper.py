"""arxiv/ar5iv HTML(LaTeXML 产物)→ 干净 Markdown + 扁平章节。

论文源头重做(HTML 优先):pymupdf 从 PDF 逆向文本有结构性损伤(断词逐行、标题拆行、
公式全丢——线上 BERT 实证);arxiv 官方 HTML(arxiv.org/html,LaTeXML 渲染)与 ar5iv
结构/公式无损。本转换器只针对 LaTeXML 的 class 约定(ltx_*)做轻量转换,stdlib 实现,
不引第三方依赖(trafilatura 会掉标题层级/公式)。

要点:
- 标题:ltx_title_document → H1,section/subsection/subsubsection → H2/H3/H4;
  同时产扁平 sections(level/title/text)供 build_section_tree(与 pymupdf 时代 schema 兼容)。
- 公式:<math alttext="..."> → $...$ / display="block" → $$...$$(跳过 MathML 子树)。
- 图:<img src> 经调用方传入的 src_map(01_download 已把图下载到 assets/ 并给出映射)
  → ![](assets/xx);<figcaption> → 斜体图注行。
- 表:best-effort 转 Markdown 表(首行后补分隔行);嵌套/富表格降级为逐行文本。
- 跳过:script/style/nav/页眉脚/LaTeXML 报错标记(ltx_ERROR)/参考文献锚点标号等装饰。
"""

from __future__ import annotations

import html as _html
import re
from html.parser import HTMLParser

# 标题 class → Markdown 级别(ltx_title_<kind>)。未识别的 hN 按 N 兜底。
_TITLE_LEVEL = {
    "document": 1,
    "part": 2,
    "section": 2,
    "subsection": 3,
    "subsubsection": 4,
    "paragraph": 5,
    "appendix": 2,
    "bibliography": 2,
}

# 整棵子树跳过的 class(装饰/导航/LaTeXML 内部)。
_SKIP_CLASSES = {
    "ltx_page_header", "ltx_page_footer", "ltx_page_logo", "ltx_ERROR",
    "ltx_rdf", "ltx_pagination", "ltx_role_versionnotice",
}
# dialog/form/header/footer/aside = 页级 chrome(arxiv 官方 HTML 的「Report GitHub Issue」弹窗、
# 页眉脚 arxiv-html-header/footer、ds-site-footer 等)。已核 BERT 官方 HTML:ltx_document 正文区
# 无这些标签(内容全在 div/section/h1-6/figure),整树跳过零内容损失;不滤则 chrome 文案混进
# original.md 顶部并被翻译(线上踩过:「##### 報告 GitHub Issue」)。
_SKIP_TAGS = {"script", "style", "nav", "head", "button",
              "dialog", "form", "header", "footer", "aside"}
# HTML5 void 元素:无闭合标签(裸 <input>/<br> 只触发 handle_starttag)。skip 子树内的深度计数
# 必须对它们免计,否则 skip_depth 只加不减、永久失衡 → 之后整页正文被吞
# (线上踩过:滤 <form> 后其内 <input> 把 BERT original.md 吞成 1 字符)。
_VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input",
              "link", "meta", "param", "source", "track", "wbr"}


class _PaperHTMLParser(HTMLParser):
    """单遍状态机:边走边发 Markdown 词元;标题落 flat sections。"""

    def __init__(self, src_map: dict[str, str] | None = None):
        super().__init__(convert_charrefs=True)
        self.src_map = src_map or {}
        self.out: list[str] = []          # markdown 词元(块间由 _block 控制空行)
        self.sections: list[dict] = []    # 扁平章节 {level,title,text}
        self._buf: list[str] = []         # 当前行内缓冲
        self._skip_depth = 0              # >0 = 在被跳过的子树里
        self._math_depth = 0              # >0 = 在 <math> 子树里(文本忽略,用 alttext)
        self._heading: int | None = None  # 正在收集的标题级别
        self._heading_buf: list[str] = []
        self._in_caption = False
        self._caption_buf: list[str] = []
        self._table: list[list[str]] | None = None   # 收集中的表格行
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._list_depth = 0
        self._pending_li = False

    # ── 小工具 ──
    @staticmethod
    def _classes(attrs) -> set[str]:
        d = dict(attrs)
        return set((d.get("class") or "").split())

    def _block(self, text: str) -> None:
        """落一个块级元素(段落/标题/图/表行),块间空行。"""
        t = text.strip()
        if not t:
            return
        self.out.append(t)

    def _flush_par(self) -> None:
        t = " ".join("".join(self._buf).split())
        self._buf = []
        if t:
            prefix = "- " if self._pending_li else ""
            self._pending_li = False
            self._block(prefix + t)

    # ── 标签处理 ──
    def handle_starttag(self, tag, attrs):
        cls = self._classes(attrs)
        if self._skip_depth or tag in _SKIP_TAGS or (cls & _SKIP_CLASSES):
            # void 元素无闭合标签,不计深度(否则 skip_depth 失衡吞掉整页正文,见 _VOID_TAGS)。
            if tag not in _VOID_TAGS:
                self._skip_depth += 1
            return
        if tag == "math":
            # MathML 子树忽略正文,取 alttext(LaTeX 原文)。display="block" 为独立公式行。
            self._math_depth += 1
            alt = dict(attrs).get("alttext", "").strip()
            if alt:
                alt = _html.unescape(alt)
                if dict(attrs).get("display") == "block":
                    self._flush_par()
                    self._block(f"$${alt}$$")
                else:
                    self._buf.append(f" ${alt}$ ")
            return
        if self._math_depth:
            return
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._flush_par()
            kind = next((c[len("ltx_title_"):] for c in cls if c.startswith("ltx_title_")), None)
            self._heading = _TITLE_LEVEL.get(kind, int(tag[1]))
            self._heading_buf = []
            return
        if tag == "figcaption":
            self._flush_par()
            self._in_caption = True
            self._caption_buf = []
            return
        if tag == "img":
            d = dict(attrs)
            src = d.get("src", "")
            mapped = self.src_map.get(src)
            if mapped is None:
                # 未下载成功的图:保留原引用(绝对 URL 可在线渲染;相对路径也留痕不丢信息)。
                mapped = src
            if mapped:
                self._flush_par()
                self._block(f"![]({mapped})")
            return
        if tag == "table":
            self._flush_par()
            self._table = []
            return
        if tag == "tr" and self._table is not None:
            self._row = []
            return
        if tag in ("td", "th") and self._row is not None:
            self._cell = []
            return
        if tag in ("ul", "ol"):
            self._flush_par()
            self._list_depth += 1
            return
        if tag == "li":
            self._flush_par()
            self._pending_li = True
            return
        if tag == "p" or (tag == "div" and "ltx_para" in cls):
            self._flush_par()
            return
        if tag == "br":
            self._buf.append(" ")

    def handle_endtag(self, tag):
        if self._skip_depth:
            # 对称免计:XHTML 自闭合(<br/>)经 HTMLParser 默认 startendtag 也会走到这里,
            # starttag 侧已对 void 免计,这里不对称会把深度减成负、提前"逃出"skip 子树。
            if tag not in _VOID_TAGS:
                self._skip_depth -= 1
            return
        if tag == "math":
            if self._math_depth:
                self._math_depth -= 1
            return
        if self._math_depth:
            return
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6") and self._heading is not None:
            title = " ".join("".join(self._heading_buf).split())
            level = self._heading
            self._heading = None
            if title:
                self._block("#" * min(level, 6) + f" {title}")
                if level > 1:  # 文档主标题不入章节表(H1 由调用方兜底元数据)
                    self.sections.append({"level": level - 1, "title": title, "page": 1,
                                          "text": ""})   # text 由 handle_data 回填
            return
        if tag == "figcaption":
            self._in_caption = False
            cap = " ".join("".join(self._caption_buf).split())
            if cap:
                self._block(f"*{cap}*")
            return
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
            return
        if tag == "tr" and self._row is not None and self._table is not None:
            if any(c for c in self._row):
                self._table.append(self._row)
            self._row = None
            return
        if tag == "table" and self._table is not None:
            rows = self._table
            self._table = None
            if rows:
                width = max(len(r) for r in rows)
                lines = []
                for i, r in enumerate(rows):
                    cells = [c.replace("|", "\\|") for c in r] + [""] * (width - len(r))
                    lines.append("| " + " | ".join(cells) + " |")
                    if i == 0:
                        lines.append("|" + "---|" * width)
                self._block("\n".join(lines))
            return
        if tag in ("ul", "ol"):
            self._flush_par()
            self._list_depth = max(0, self._list_depth - 1)
            return
        if tag in ("p", "li", "div", "figure", "section"):
            self._flush_par()

    def handle_data(self, data):
        if self._skip_depth or self._math_depth:
            return
        if self._heading is not None:
            self._heading_buf.append(data)
        elif self._in_caption:
            self._caption_buf.append(data)
        elif self._cell is not None:
            self._cell.append(data)
        else:
            self._buf.append(data)
        # sections text 回填(供语言检测/树渲染兜底):追加到最后一个章节
        if (not self._skip_depth and not self._math_depth and self._heading is None
                and not self._in_caption and self._cell is None and self.sections
                and data.strip()):
            s = self.sections[-1]
            if len(s["text"]) < 200_000:  # 防御性上限
                s["text"] = (s["text"] + " " + data.strip()).strip()


def arxiv_html_to_markdown(html_text: str, src_map: dict[str, str] | None = None) -> dict:
    """ar5iv/arxiv HTML → {"markdown": str, "sections": list[dict], "title": str|None}。

    sections 为扁平 {level,title,page,text}(page 恒 1:HTML 无页概念,占位保持 schema 兼容),
    交 build_section_tree 组树。转换 best-effort:未知结构降级为纯文本段,不抛。
    """
    # LaTeXML 把正文放 <article>;只喂 body 起的部分,避免 head 里的 meta 文本混入。
    m = re.search(r"<body[^>]*>", html_text, re.I)
    if m:
        html_text = html_text[m.start():]
    p = _PaperHTMLParser(src_map)
    p.feed(html_text)
    p._flush_par()
    md = "\n\n".join(p.out).strip() + "\n"
    title = None
    for tok in p.out:
        if tok.startswith("# "):
            title = tok[2:].strip()
            break
    return {"markdown": md, "sections": p.sections, "title": title}
