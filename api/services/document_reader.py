"""把不可信文档 HTML 投影成可隔离展示的只读页面。"""

from __future__ import annotations

import html
import posixpath
import re
from html.parser import HTMLParser
from typing import Any, Mapping
from urllib.parse import quote, urlparse

from shared.scholarly_html_snapshot import (
    SCHOLARLY_HTML_SANITIZED_CSS_MAX_BYTES,
    ScholarlyHtmlSnapshotLimitError,
    SnapshotCssBudget,
    sanitize_snapshot_stylesheet,
)


DOCUMENT_HTML_MAX_BYTES = 32 * 1024 * 1024
DOCUMENT_HTML_MAX_OUTPUT_BYTES = 48 * 1024 * 1024
DOCUMENT_HTML_MAX_NODES = 50_000
DOCUMENT_HTML_MAX_DEPTH = 128
DOCUMENT_HTML_MAX_ATTRIBUTES = 200_000
DOCUMENT_HTML_MAX_TAG_ATTRIBUTE_BYTES = 256 * 1024
_ESCAPE_CHUNK_CHARS = 16 * 1024


class DocumentReaderError(ValueError):
    """表示不可信HTML无法形成唯一、安全的阅读投影。"""


class DocumentReaderLimitError(DocumentReaderError):
    """表示输入在解析或输出阶段越过确定性资源预算。"""


class _BoundedHtmlBuilder:
    """分块构造完整阅读页，避免先分配越界转义结果再拒绝。"""

    def __init__(self) -> None:
        self._parts: list[str] = []
        self._output_bytes = 0

    def append(self, fragment: str) -> None:
        size = len(fragment.encode("utf-8"))
        if self._output_bytes + size > DOCUMENT_HTML_MAX_OUTPUT_BYTES:
            raise DocumentReaderLimitError("document HTML output exceeds reader limit")
        self._output_bytes += size
        self._parts.append(fragment)

    def append_escaped(self, value: object, *, quote: bool = False) -> None:
        text = str(value or "")
        for offset in range(0, len(text), _ESCAPE_CHUNK_CHARS):
            self.append(html.escape(text[offset:offset + _ESCAPE_CHUNK_CHARS], quote=quote))

    def build(self) -> bytes:
        return "".join(self._parts).encode("utf-8")


_VOID_TAGS = frozenset({
    "area", "base", "br", "col", "hr", "img", "input", "link", "meta",
    "param", "source", "track", "wbr",
})
_DROP_WITH_CONTENT = frozenset({
    "script", "style", "noscript", "template", "iframe", "object", "embed",
    "canvas", "audio", "video", "form", "button", "input", "select", "textarea",
    "dialog", "foreignobject",
})
_DROP_HEAD = frozenset({"html", "head", "body", "base", "link", "meta", "title"})
_CHROME_MARKERS = frozenset({
    "navbar", "site-nav", "site-header", "site-footer", "page-header", "page-footer",
    "sidebar", "conversion-header", "conversion-footer", "ltx-page-header",
    "ltx-page-footer", "ar5iv-nav", "ar5iv-footer",
})
_URL_ATTRS = frozenset({"href", "src", "poster", "xlink:href"})
_SAFE_GLOBAL_ATTRS = frozenset({
    "id", "class", "title", "alt", "role", "lang", "dir", "width", "height",
    "colspan", "rowspan", "scope", "start", "value", "open", "datetime",
    "cite", "download",
})
_SAFE_SCIENCE_ATTRS = frozenset({
    "xmlns", "display", "alttext", "encoding", "mathvariant", "mathsize",
    "mathcolor", "stretchy", "fence", "separator", "accent", "accentunder",
    "columnalign", "rowalign", "columnspacing", "rowspacing", "linethickness",
    "viewbox", "preserveaspectratio", "d", "x", "y", "x1", "x2", "y1", "y2",
    "cx", "cy", "r", "rx", "ry", "points", "transform", "fill", "stroke",
    "stroke-width", "stroke-linecap", "stroke-linejoin", "opacity", "marker-start",
    "marker-mid", "marker-end",
})
_SAFE_HTML_TAGS = frozenset({
    "a", "abbr", "address", "article", "b", "blockquote", "br", "caption",
    "cite", "code", "col", "colgroup", "dd", "details", "dfn", "div", "dl",
    "dt", "em", "figcaption", "figure", "h1", "h2", "h3", "h4", "h5",
    "h6", "hr", "i", "img", "kbd", "li", "main", "mark", "ol", "p",
    "pre", "q", "rp", "rt", "ruby", "s", "samp", "section", "small",
    "span", "strong", "sub", "summary", "sup", "table", "tbody", "td",
    "tfoot", "th", "thead", "time", "tr", "u", "ul", "var", "wbr",
})
_SAFE_MATHML_TAGS = frozenset({
    "annotation", "annotation-xml", "maligngroup", "malignmark", "math",
    "menclose", "merror", "mfenced", "mfrac", "mglyph", "mi", "mlabeledtr",
    "mmultiscripts", "mn", "mo", "mover", "mpadded", "mphantom",
    "mprescripts", "mroot", "mrow", "ms", "mspace", "msqrt", "mstyle",
    "msub", "msubsup", "msup", "mtable", "mtd", "mtext", "mtr", "munder",
    "munderover", "none", "semantics",
})
_SAFE_SVG_TAGS = frozenset({
    "circle", "clippath", "defs", "desc", "ellipse", "g", "image", "line",
    "lineargradient", "marker", "mask", "path", "polygon", "polyline",
    "radialgradient", "rect", "stop", "svg", "symbol", "text", "title",
    "tspan", "use",
})
_SAFE_RENDER_TAGS = _SAFE_HTML_TAGS | _SAFE_MATHML_TAGS | _SAFE_SVG_TAGS
_SAFE_STYLE_TAGS = frozenset({
    "article", "div", "figcaption", "figure", "img", "li", "math", "p",
    "section", "span", "table", "tbody", "td", "tfoot", "th", "thead",
    "tr",
})
_SAFE_DATA_IMAGE = re.compile(
    r"data:image/(?:avif|gif|jpeg|png|webp);base64,[A-Za-z0-9+/=]+\Z", re.I,
)
_CSS_LENGTH = re.compile(r"(?P<number>\d{1,4}(?:\.\d{1,3})?)(?P<unit>px|pt|em|rem|ex|ch|%)\Z")
_SAFE_ATTR_NAME = re.compile(r"[a-z][a-z0-9_.:-]{0,63}\Z")
_CSS_ASPECT_RATIO = re.compile(
    r"(?P<left>[1-9]\d{0,3}(?:\.\d{1,3})?)\s*/\s*"
    r"(?P<right>[1-9]\d{0,3}(?:\.\d{1,3})?)\Z"
)
_SAFE_STYLE_KEYWORDS = {
    "clear": frozenset({"both", "left", "none", "right"}),
    "display": frozenset({"block", "flex", "grid", "inline", "inline-block", "table", "table-cell", "table-row"}),
    "float": frozenset({"left", "none", "right"}),
    "object-fit": frozenset({"contain", "cover", "fill", "scale-down"}),
    "text-align": frozenset({"center", "end", "justify", "left", "right", "start"}),
    "vertical-align": frozenset({"baseline", "bottom", "middle", "sub", "super", "text-bottom", "text-top", "top"}),
    "white-space": frozenset({"normal", "nowrap", "pre", "pre-wrap"}),
}
_SAFE_STYLE_LENGTHS = frozenset({
    "height", "margin-bottom", "margin-left", "margin-right", "margin-top",
    "max-height", "max-width", "width",
})
_DOCUMENT_STYLE = """
:root{color-scheme:light;font-family:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#243142;background:#fff}
*{box-sizing:border-box}body{margin:0}.flori-document{max-width:980px;margin:0 auto;padding:30px 34px 72px;font-size:17px;line-height:1.78}
h1,h2,h3,h4,h5,h6{color:#172235;line-height:1.35;margin:1.7em 0 .65em}h1{font-size:2.15rem;margin-top:.25em}h2{font-size:1.5rem;border-bottom:1px solid #e4e9f0;padding-bottom:.35em}
p,ul,ol,blockquote,pre,figure,table{margin:1em 0}a{color:#1769aa;text-decoration-thickness:.08em;text-underline-offset:.15em}img,svg{max-width:100%;height:auto}
figure{margin:1.5em auto;padding:14px;border:1px solid #e4e9f0;border-radius:10px;background:#fbfcfe}figcaption,caption{color:#5a6778;font-size:.92em;text-align:left}
table{display:block;max-width:100%;overflow:auto;border-collapse:collapse}th,td{border:1px solid #d8dee8;padding:.45em .65em;vertical-align:top}th{background:#f3f6fa}
pre,code{font-family:"SFMono-Regular",Consolas,monospace}pre{overflow:auto;padding:1em;border-radius:8px;background:#f5f7fa}blockquote{border-left:4px solid #d4dce7;padding-left:1em;color:#526174}
math{font-family:"STIX Two Math","Cambria Math",serif}.flori-source-anchor{display:block;position:relative;top:-12px;visibility:hidden}
.flori-source-target{outline:3px solid #f4b942;outline-offset:5px;background:#fff7d6;scroll-margin-top:18px}.flori-exact-target{border-radius:3px;background:#ffe47a;color:inherit}
.flori-document-header{padding-bottom:1.25rem;border-bottom:1px solid #e4e9f0}.flori-document-meta{display:flex;flex-wrap:wrap;gap:.4rem 1rem;color:#5a6778;font-size:.92rem}
.flori-abstract{margin:1.35rem 0;padding:1rem 1.15rem;border-left:4px solid #8aa9c7;background:#f7f9fc}.flori-abstract-label{display:block;margin-bottom:.35rem;color:#526174;font-size:.82rem;font-weight:700;text-transform:uppercase;letter-spacing:.04em}
.ltx_page_main,.ltx_document{display:block;max-width:100%;margin:0 auto}.ltx_title_document{text-align:center;font-size:2rem}.ltx_authors{text-align:center;margin:1rem auto 1.4rem}.ltx_creator{display:inline-block;max-width:100%;margin:.25rem .8rem;vertical-align:top}.ltx_personname{font-weight:650}.ltx_affiliation,.ltx_contact,.ltx_author_notes{display:block;color:#5a6778;font-size:.9em}.ltx_abstract{max-width:860px;margin:1.5rem auto;padding:1rem 1.2rem;border-left:4px solid #8aa9c7;background:#f7f9fc}.ltx_abstract>h6:first-child{margin-top:0;text-transform:uppercase;letter-spacing:.04em}.ltx_section,.ltx_subsection,.ltx_subsubsection{clear:both}.ltx_para{margin:.9em 0}.ltx_float,.ltx_figure,.ltx_table{display:block;clear:both;max-width:100%;margin:1.6rem auto;padding:0;border:0;background:transparent;text-align:center}.ltx_figure>img,.ltx_figure_panel>img,.ltx_graphics{display:block;max-width:100%;height:auto;margin:.5rem auto}.ltx_figure_panel{display:inline-block;max-width:100%;margin:.4rem .7rem;vertical-align:top}.ltx_figure_panel figcaption{max-width:34rem}.ltx_caption,.ltx_figure figcaption,.ltx_table figcaption{display:block;max-width:60rem;margin:.65rem auto 0;color:#526174;text-align:left}.ltx_equation,.ltx_equationgroup,.ltx_math{max-width:100%;overflow-x:auto;overflow-y:hidden;text-align:center}.ltx_equation table,.ltx_equationgroup table{display:table;width:auto;margin:.8rem auto;border:0}.ltx_equation td,.ltx_equationgroup td{border:0;background:transparent}.ltx_eqn_table{display:table;width:auto;margin:0 auto}.ltx_eqn_cell{border:0}.ltx_tag_equation{padding-left:1rem;white-space:nowrap;text-align:right}.ltx_tabular{display:table;width:auto;max-width:100%;margin:.8rem auto;overflow:visible}.ltx_tabular th,.ltx_tabular td{padding:.35em .55em}.ltx_biblist{padding-left:1.7rem}.ltx_bibitem{margin:.55rem 0}.ltx_theorem,.ltx_proof,.ltx_definition,.ltx_lemma{margin:1rem 0;padding:.8rem 1rem;border-left:3px solid #a9b9ca;background:#fafbfd}.ltx_theorem .ltx_title,.ltx_proof .ltx_title{font-weight:700}.ltx_note,.ltx_role_footnote{font-size:.9em;color:#526174}.ltx_ref{white-space:nowrap}.ltx_ERROR{color:#8a3b12;background:#fff2e8}
@media(max-width:640px){.flori-document{padding:18px 16px 48px;font-size:16px}h1{font-size:1.65rem}h2{font-size:1.3rem}}
""".strip()

_DOCUMENT_SNAPSHOT_FRAME_STYLE = """
:root{color-scheme:light;background:#fff}
html,body{margin:0;min-height:100%;background:#fff;color:#111}
.flori-document{min-height:100vh}
img,svg{max-width:100%;height:auto}
.flori-source-anchor{display:block;position:relative;top:-12px;visibility:hidden}
.flori-source-target{outline:3px solid #f4b942;outline-offset:5px;background:#fff7d6;scroll-margin-top:18px}
.flori-exact-target{border-radius:3px;background:#ffe47a;color:inherit}
""".strip()


def _safe_inline_style(value: str) -> str | None:
    """保留有限布局声明;任何资源、函数或解析歧义都丢弃整条声明。"""
    raw = value.strip()
    if (
        not raw or len(raw) > 1024 or raw.count(";") > 16
        or any(token in raw.casefold() for token in ("/*", "*/"))
        or any(char in raw for char in "{}\\\x00\r\n")
    ):
        return None
    result: list[str] = []
    for declaration in raw.split(";"):
        if not declaration.strip():
            continue
        if ":" not in declaration:
            continue
        name, candidate = declaration.split(":", 1)
        name = name.strip().casefold()
        candidate = candidate.strip().casefold()
        if any(token in candidate for token in ("url(", "expression", "var(", "@import", "behavior")):
            continue
        if name in _SAFE_STYLE_LENGTHS:
            if candidate == "auto":
                pass
            elif not _safe_css_length(candidate):
                continue
        elif name == "vertical-align" and _safe_css_length(candidate, signed=True):
            pass
        elif name == "aspect-ratio":
            ratio = _CSS_ASPECT_RATIO.fullmatch(candidate)
            if ratio is None:
                continue
            value = float(ratio.group("left")) / float(ratio.group("right"))
            if not 0.01 <= value <= 100:
                continue
        elif candidate not in _SAFE_STYLE_KEYWORDS.get(name, frozenset()):
            continue
        result.append(f"{name}:{candidate}")
    return ";".join(result) or None


def _safe_css_length(value: str, *, signed: bool = False) -> bool:
    raw = value
    if raw == "0":
        return True
    if raw.startswith("-"):
        if not signed:
            return False
        raw = raw[1:]
    match = _CSS_LENGTH.fullmatch(raw)
    if match is None:
        return False
    number = float(match.group("number"))
    unit = match.group("unit")
    if unit == "%":
        return number <= 100
    if unit in {"em", "rem", "ex", "ch"}:
        return number <= 100
    return number <= 4096


def _safe_dimension_attr(value: str) -> str | None:
    raw = value.strip()
    if raw.endswith("%") and raw[:-1].isdigit():
        number = int(raw[:-1])
        return raw if 0 < number <= 100 else None
    if raw.isdigit():
        number = int(raw)
        return raw if 0 < number <= 4096 else None
    return None


def _safe_science_attr(name: str, value: str) -> str | None:
    raw = value.strip()
    lowered = raw.casefold()
    if (
        len(raw) > 65536
        or any(char in raw for char in "\\\x00\r\n")
        or "/*" in lowered
        or "*/" in lowered
    ):
        return None
    local_paint = re.fullmatch(r"url\(#[A-Za-z][A-Za-z0-9_.:-]{0,127}\)", raw)
    if name in {"marker-start", "marker-mid", "marker-end"}:
        return raw if lowered == "none" or local_paint is not None else None
    if name in {"fill", "stroke"}:
        if local_paint is not None:
            return raw
        if lowered in {
            "none", "currentcolor", "context-fill", "context-stroke", "transparent",
        }:
            return raw
        if re.fullmatch(r"#[0-9A-Fa-f]{3,8}", raw):
            return raw
        if re.fullmatch(r"[A-Za-z]{1,32}", raw):
            return raw
        if re.fullmatch(
            r"rgba?\(\s*\d{1,3}(?:\.\d+)?%?\s*,\s*\d{1,3}(?:\.\d+)?%?"
            r"\s*,\s*\d{1,3}(?:\.\d+)?%?(?:\s*,\s*(?:0|1|0?\.\d+))?\s*\)",
            raw,
            re.I,
        ):
            return raw
        return None
    if name == "transform" and re.fullmatch(r"[A-Za-z0-9 .,+()%-]{0,512}", raw) is None:
        return None
    return raw


def _safe_local_path(value: str) -> str | None:
    raw = value.strip().replace("\\", "/")
    if len(raw) > 4096 or any(ord(char) < 0x20 or ord(char) == 0x7f for char in raw):
        return None
    parsed = urlparse(raw)
    if parsed.scheme or parsed.netloc or raw.startswith(("/", "//")):
        return None
    normalized = posixpath.normpath(parsed.path)
    if normalized in {"", ".", ".."} or normalized.startswith("../"):
        return None
    return normalized


def _snapshot_resource_url(job_id: str, path: str) -> str:
    return (
        f"/api/jobs/{quote(job_id, safe='')}/document/resource"
        f"?path={quote(path, safe='')}"
    )


def _asset_url(
    job_id: str,
    value: str,
    snapshot_resource_digests: Mapping[str, str] | None = None,
) -> str | None:
    raw = value.strip()
    lowered = raw.casefold()
    if lowered.startswith("data:image/"):
        if len(raw) <= 8 * 1024 * 1024 and _SAFE_DATA_IMAGE.fullmatch(raw):
            return raw
        return None
    local = _safe_local_path(raw)
    if local is None:
        return None
    if snapshot_resource_digests is not None and local in snapshot_resource_digests:
        return _snapshot_resource_url(job_id, local)
    if not local.startswith("assets/"):
        return None
    return f"/api/jobs/{quote(job_id, safe='')}/artifact?path={quote(local, safe='')}"


def build_snapshot_css(
    stylesheets: list[tuple[Mapping[str, object], bytes]],
    *,
    job_id: str,
    resources: Mapping[str, Mapping[str, object]],
) -> str:
    """按快照顺序净化CSS;URL只可解析到同一快照声明的本地资源。"""
    aliases: dict[str, str] = {}
    for record in resources.values():
        path = str(record.get("path") or "")
        if not path or record.get("kind") == "stylesheet":
            continue
        endpoint = _snapshot_resource_url(job_id, path)
        for key in ("request_url", "source_url"):
            source = record.get(key)
            if isinstance(source, str):
                aliases[source] = endpoint

    rendered: list[str] = []
    rendered_bytes = 0
    budget = SnapshotCssBudget()
    for record, body in stylesheets:
        base_url = str(record.get("source_url") or record.get("request_url") or "")
        sanitized = sanitize_snapshot_stylesheet(
            body,
            base_url=base_url,
            resolve_resource=aliases.get,
            budget=budget,
        )
        rendered_bytes += len(sanitized.encode("utf-8"))
        if rendered_bytes > SCHOLARLY_HTML_SANITIZED_CSS_MAX_BYTES:
            raise ScholarlyHtmlSnapshotLimitError(
                "combined sanitized stylesheets exceed limit"
            )
        rendered.append(sanitized)
    return "".join(rendered)


def _safe_link(value: str) -> str | None:
    raw = value.strip()
    if len(raw) > 4096 or any(ord(char) < 0x20 or ord(char) == 0x7f for char in raw):
        return None
    if raw.startswith("#"):
        return raw
    parsed = urlparse(raw)
    if parsed.scheme.lower() in {"http", "https", "mailto"}:
        return raw
    return None


def source_anchor_map(document: Mapping[str, Any]) -> dict[str, str]:
    """从 Document block locator 建立 DOM path 到稳定 block id 的映射。"""
    result: dict[str, str] = {}
    blocks = document.get("blocks")
    if not isinstance(blocks, list):
        return result
    for block in blocks:
        if not isinstance(block, Mapping):
            continue
        locator = block.get("locator")
        html_locator = locator.get("html") if isinstance(locator, Mapping) else None
        path = html_locator.get("dom_path") if isinstance(html_locator, Mapping) else None
        block_id = block.get("block_id")
        if isinstance(path, str) and isinstance(block_id, str) and path not in result:
            result[path] = block_id
    return result


def _append_model_text(
    builder: _BoundedHtmlBuilder,
    value: object,
    *,
    target: bool = False,
    target_exact: str | None = None,
) -> None:
    text = str(value or "")
    if target and target_exact and target_exact in text:
        before, matched, after = text.partition(target_exact)
        builder.append_escaped(before)
        builder.append('<mark class="flori-exact-target">')
        builder.append_escaped(matched)
        builder.append("</mark>")
        builder.append_escaped(after)
        return
    builder.append_escaped(text)


def _model_block_attrs(block: Mapping[str, Any], target_segment: str | None) -> str:
    block_id = str(block.get("block_id") or "")
    if not block_id:
        return ""
    target = block_id == target_segment
    anchor = html.escape(f"source-{block_id}", quote=True)
    class_name = ' class="flori-source-target"' if target else ""
    return f' id="{anchor}"{class_name}'


def _metadata_title(metadata: Mapping[str, Any]) -> str:
    titles = metadata.get("titles")
    if isinstance(titles, Mapping):
        return str(titles.get("original") or titles.get("zh") or "").strip()
    return str(metadata.get("title") or "").strip()


def _metadata_authors(metadata: Mapping[str, Any]) -> list[str]:
    values = metadata.get("authors")
    if not isinstance(values, list):
        return []
    result: list[str] = []
    for value in values:
        name = value.get("name") if isinstance(value, Mapping) else value
        normalized = str(name or "").strip()
        if normalized and normalized not in result:
            result.append(normalized)
    return result


def _normalized_text(value: object) -> str:
    return "".join(str(value or "").split()).casefold()


def _abstract_wrapper(block: Mapping[str, Any], abstract: str) -> bool:
    if block.get("kind") != "paragraph" or len(abstract) < 200:
        return False
    block_text = _normalized_text(block.get("text"))
    abstract_text = _normalized_text(abstract)
    return bool(
        block_text and abstract_text in block_text
        and len(abstract_text) / max(len(block_text), 1) >= 0.8
    )


def _append_model_figure(
    builder: _BoundedHtmlBuilder,
    figure: Mapping[str, Any],
    assets: Mapping[str, Mapping[str, Any]],
    *,
    job_id: str,
    attrs: str,
) -> None:
    images: list[tuple[str, str]] = []
    rendered_paths: set[str] = set()

    def add_image(path: object, *, mime: object = "", alt: object = "") -> None:
        normalized_path = str(path or "")
        normalized_mime = str(mime or "")
        if (
            not normalized_path
            or normalized_path in rendered_paths
            or (normalized_mime and not normalized_mime.startswith("image/"))
        ):
            return
        source = _asset_url(job_id, normalized_path)
        if source is None:
            return
        rendered_paths.add(normalized_path)
        images.append((source, str(alt or "")))

    fallback_alt = figure.get("caption") or figure.get("label") or ""
    asset_ids: list[str] = []
    for media in figure.get("media") or []:
        if not isinstance(media, Mapping):
            continue
        asset_id = str(media.get("asset_id") or "")
        asset = assets.get(asset_id) if asset_id else None
        add_image(
            media.get("artifact")
            or (asset or {}).get("local_path")
            or (asset or {}).get("path"),
            mime=(asset or {}).get("mime_type"),
            alt=media.get("alt") or (asset or {}).get("alt") or fallback_alt,
        )
        if asset_id:
            asset_ids.append(asset_id)
    for value in figure.get("asset_ids") or []:
        if isinstance(value, str):
            asset_ids.append(value)
    for panel in figure.get("panels") or []:
        if isinstance(panel, Mapping) and isinstance(panel.get("asset_id"), str):
            asset_ids.append(str(panel["asset_id"]))
    for media in figure.get("media") or []:
        if isinstance(media, Mapping) and isinstance(media.get("asset_id"), str):
            asset_ids.append(str(media["asset_id"]))
    for asset_id in dict.fromkeys(asset_ids):
        asset = assets.get(asset_id)
        if not asset:
            continue
        path = str(asset.get("local_path") or asset.get("path") or "")
        add_image(
            path,
            mime=asset.get("mime_type"),
            alt=asset.get("alt") or fallback_alt,
        )
    caption = str(figure.get("caption") or figure.get("label") or "").strip()
    if not images and not caption:
        return
    builder.append(f"<figure{attrs}>")
    for source, alt in images:
        builder.append('<img src="')
        builder.append_escaped(source, quote=True)
        builder.append('" alt="')
        builder.append_escaped(alt, quote=True)
        builder.append('">')
    if caption:
        builder.append("<figcaption>")
        builder.append_escaped(caption)
        builder.append("</figcaption>")
    builder.append("</figure>")


def _append_model_table(
    builder: _BoundedHtmlBuilder, table: Mapping[str, Any], *, attrs: str,
) -> None:
    rows: list[list[Mapping[str, Any]]] = []
    cells = table.get("cells")
    if isinstance(cells, list) and cells:
        grouped: dict[int, list[Mapping[str, Any]]] = {}
        for cell in cells:
            if not isinstance(cell, Mapping):
                continue
            try:
                row = int(cell.get("row") or 0)
            except (TypeError, ValueError):
                row = 0
            grouped.setdefault(row, []).append(cell)
        for row in sorted(grouped):
            rows.append(sorted(
                grouped[row],
                key=lambda cell: int(cell.get("col") or 0),
            ))
    elif isinstance(table.get("rows"), list):
        for row in table["rows"]:
            if isinstance(row, Mapping) and isinstance(row.get("cells"), list):
                rows.append([cell for cell in row["cells"] if isinstance(cell, Mapping)])
    caption = str(table.get("caption") or table.get("label") or "").strip()
    builder.append(f"<table{attrs}>")
    if caption:
        builder.append("<caption>")
        builder.append_escaped(caption)
        builder.append("</caption>")
    for row in rows:
        builder.append("<tr>")
        for cell in row:
            role = str(cell.get("role") or cell.get("kind") or "")
            tag = "th" if role in {"column_header", "row_header", "header"} else "td"
            rowspan = max(1, int(cell.get("rowspan") or 1))
            colspan = max(1, int(cell.get("colspan") or 1))
            builder.append(
                f'<{tag} rowspan="{rowspan}" colspan="{colspan}">'
            )
            builder.append_escaped(cell.get("text"))
            builder.append(f"</{tag}>")
        builder.append("</tr>")
    builder.append("</table>")


def render_document_model_html(
    document: Mapping[str, Any],
    *,
    job_id: str,
    target_segment: str | None = None,
    target_exact: str | None = None,
) -> bytes:
    """把已校验Document Model投影成正文阅读面,不重放站点DOM。"""
    builder = _BoundedHtmlBuilder()
    metadata = document.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    title = _metadata_title(metadata)
    authors = _metadata_authors(metadata)
    published = str(metadata.get("published_at") or "").strip()
    publisher = str(metadata.get("publisher") or metadata.get("venue") or "").strip()
    abstract = str(metadata.get("abstract") or "").strip()

    page_title = title or "文档原文"
    builder.append(
        '<!doctype html><html lang="und"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>"
    )
    builder.append_escaped(page_title)
    builder.append(f"</title><style>{_DOCUMENT_STYLE}</style></head>")
    builder.append('<body><main class="flori-document">')
    builder.append('<header class="flori-document-header">')
    if title:
        builder.append("<h1>")
        builder.append_escaped(title)
        builder.append("</h1>")
    meta_items: list[tuple[str, str]] = []
    if authors:
        meta_items.append(("span", ", ".join(authors)))
    if published:
        meta_items.append(("time", published))
    if publisher:
        meta_items.append(("span", publisher))
    if meta_items:
        builder.append('<div class="flori-document-meta">')
        for tag, value in meta_items:
            builder.append(f"<{tag}>")
            builder.append_escaped(value)
            builder.append(f"</{tag}>")
        builder.append("</div>")
    builder.append("</header>")
    if abstract:
        builder.append(
            '<section class="flori-abstract">'
            '<span class="flori-abstract-label">Abstract</span><p>'
        )
        builder.append_escaped(abstract)
        builder.append("</p></section>")

    blocks = [item for item in document.get("blocks") or [] if isinstance(item, Mapping)]
    assets = {
        str(item.get("asset_id")): item
        for item in document.get("assets") or []
        if isinstance(item, Mapping) and item.get("asset_id")
    }
    figures = {
        str(item.get("block_id")): item
        for item in document.get("figures") or []
        if isinstance(item, Mapping) and item.get("block_id")
    }
    tables = {
        str(item.get("block_id")): item
        for item in document.get("tables") or []
        if isinstance(item, Mapping) and item.get("block_id")
    }
    list_children: dict[str, list[Mapping[str, Any]]] = {}
    for block in blocks:
        parent_id = block.get("parent_id")
        if isinstance(parent_id, str):
            list_children.setdefault(parent_id, []).append(block)

    for block in sorted(blocks, key=lambda item: int(item.get("order") or 0)):
        kind = str(block.get("kind") or "paragraph")
        block_id = str(block.get("block_id") or "")
        text = str(block.get("text") or "")
        if kind in {"caption", "table_cell", "list_item"}:
            continue
        if kind == "title" and title and _normalized_text(text) == _normalized_text(title):
            continue
        if abstract and _abstract_wrapper(block, abstract):
            continue
        attrs = _model_block_attrs(block, target_segment)
        target = block_id == target_segment
        if kind == "title":
            builder.append(f"<h1{attrs}>")
            _append_model_text(
                builder, text, target=target, target_exact=target_exact,
            )
            builder.append("</h1>")
        elif kind == "heading":
            level = min(6, max(2, int(block.get("level") or 2)))
            builder.append(f"<h{level}{attrs}>")
            _append_model_text(
                builder, text, target=target, target_exact=target_exact,
            )
            builder.append(f"</h{level}>")
        elif kind == "quote":
            builder.append(f"<blockquote{attrs}>")
            _append_model_text(
                builder, text, target=target, target_exact=target_exact,
            )
            builder.append("</blockquote>")
        elif kind == "code":
            builder.append(f"<pre{attrs}><code>")
            _append_model_text(
                builder, text, target=target, target_exact=target_exact,
            )
            builder.append("</code></pre>")
        elif kind == "list":
            tag = "ol" if block.get("ordered") else "ul"
            items = list_children.get(block_id, [])
            builder.append(f"<{tag}{attrs}>")
            for item in sorted(items, key=lambda value: int(value.get("order") or 0)):
                item_target = str(item.get("block_id") or "") == target_segment
                item_attrs = _model_block_attrs(item, target_segment)
                builder.append(f"<li{item_attrs}>")
                _append_model_text(
                    builder, item.get("text"), target=item_target,
                    target_exact=target_exact,
                )
                builder.append("</li>")
            builder.append(f"</{tag}>")
        elif kind == "figure" and block_id in figures:
            _append_model_figure(
                builder, figures[block_id], assets, job_id=job_id, attrs=attrs,
            )
        elif kind == "table" and block_id in tables:
            _append_model_table(builder, tables[block_id], attrs=attrs)
        elif kind == "footnote":
            builder.append(f"<p{attrs}><small>")
            _append_model_text(
                builder, text, target=target, target_exact=target_exact,
            )
            builder.append("</small></p>")
        elif text:
            builder.append(f"<p{attrs}>")
            _append_model_text(
                builder, text, target=target, target_exact=target_exact,
            )
            builder.append("</p>")

    builder.append("</main></body></html>")
    return builder.build()


class _SafeDocumentParser(HTMLParser):
    """保留阅读结构和 MathML/SVG，同时剥离脚本、外链资源与交互能力。"""

    def __init__(
        self,
        job_id: str,
        anchors: Mapping[str, str],
        *,
        builder: _BoundedHtmlBuilder,
        embedded_anchor_ids: frozenset[str] | None = None,
        target_segment: str | None = None,
        target_exact: str | None = None,
        snapshot_resource_digests: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(convert_charrefs=True)
        self.job_id = job_id
        self.anchors = anchors
        self._builder = builder
        self._node_count = 0
        self._attribute_count = 0
        self._stack: list[dict[str, Any]] = [{
            "tag": "#document", "path": "", "counts": {}, "drop": False,
            "rendered": False,
        }]
        self._open_by_tag: dict[str, list[dict[str, Any]]] = {}
        self._target_depth = 0
        self._seen_anchor_ids: set[str] = set()
        self.embedded_anchor_ids = embedded_anchor_ids
        self._body_seen = False
        self.target_segment = target_segment
        self.target_exact = target_exact
        self._target_marked = False
        self.snapshot_resource_digests = snapshot_resource_digests

    def _append(self, fragment: str) -> None:
        self._builder.append(fragment)

    def _append_escaped(self, value: object, *, quote: bool = False) -> None:
        self._builder.append_escaped(value, quote=quote)

    def _count_start_tag(self, attrs: list[tuple[str, str | None]]) -> None:
        self._node_count += 1
        self._attribute_count += len(attrs)
        if self._node_count > DOCUMENT_HTML_MAX_NODES:
            raise DocumentReaderLimitError("document HTML node count exceeds reader limit")
        if self._attribute_count > DOCUMENT_HTML_MAX_ATTRIBUTES:
            raise DocumentReaderLimitError("document HTML attribute count exceeds reader limit")
        if len(attrs) > 256:
            raise DocumentReaderLimitError("document HTML tag has too many attributes")
        attribute_bytes = 0
        for name, value in attrs:
            for item in (name, value or ""):
                remaining = DOCUMENT_HTML_MAX_TAG_ATTRIBUTE_BYTES - attribute_bytes
                if len(item) > remaining:
                    raise DocumentReaderLimitError(
                        "document HTML tag attributes exceed reader limit"
                    )
                attribute_bytes += len(item.encode("utf-8"))
                if attribute_bytes > DOCUMENT_HTML_MAX_TAG_ATTRIBUTE_BYTES:
                    raise DocumentReaderLimitError(
                        "document HTML tag attributes exceed reader limit"
                    )

    def _next_path(self, tag: str) -> str:
        parent = self._stack[-1]
        counts = parent["counts"]
        counts[tag] = counts.get(tag, 0) + 1
        return f"{parent['path']}/{tag}[{counts[tag]}]"

    def _attrs(
        self, tag: str, attrs: list[tuple[str, str | None]], *, target: bool = False,
    ) -> str:
        safe: list[str] = []
        attr_map = {key.lower(): value or "" for key, value in attrs}
        if tag == "img" and not attr_map.get("src") and attr_map.get("data-artifact"):
            source = _asset_url(
                self.job_id, attr_map["data-artifact"], self.snapshot_resource_digests,
            )
            if source is not None:
                safe.append(f'src="{html.escape(source, quote=True)}"')
        class_seen = False
        seen_attributes: set[str] = set()
        for key, value in attrs:
            name = key.lower()
            raw = value or ""
            if _SAFE_ATTR_NAME.fullmatch(name) is None:
                continue
            if name in seen_attributes:
                continue
            seen_attributes.add(name)
            if name.startswith("on") or name in {"srcdoc", "formaction"}:
                continue
            if name == "class":
                if len(raw) > 65536:
                    continue
                class_seen = True
                tokens = [
                    token for token in raw.split()
                    if not token.casefold().startswith("flori-")
                ]
                if target:
                    tokens.append("flori-source-target")
                if tokens:
                    rendered_class = " ".join(dict.fromkeys(tokens))
                    safe.append(f'class="{html.escape(rendered_class, quote=True)}"')
                continue
            if name == "id" and raw.casefold().startswith(("source-", "flori-")):
                continue
            if name == "data-source-segment":
                continue
            if name == "style":
                rendered_style = _safe_inline_style(raw) if tag in _SAFE_STYLE_TAGS else None
                if rendered_style is not None:
                    safe.append(f'style="{html.escape(rendered_style, quote=True)}"')
                continue
            if name in _URL_ATTRS:
                if name in {"src", "poster"} or tag == "image":
                    resolved = _asset_url(
                        self.job_id, raw, self.snapshot_resource_digests,
                    )
                elif tag == "a" and name == "href":
                    resolved = _safe_link(raw)
                else:
                    resolved = raw if raw.startswith("#") else None
                if resolved is None:
                    continue
                safe.append(f'{name}="{html.escape(resolved, quote=True)}"')
                if tag == "a" and name == "href" and not resolved.startswith("#"):
                    safe.extend(['target="_blank"', 'rel="noopener noreferrer"'])
                continue
            if name in {"width", "height"}:
                dimension = _safe_dimension_attr(raw)
                if dimension is not None:
                    safe.append(f'{name}="{dimension}"')
                continue
            if name in {"colspan", "rowspan"}:
                if raw.isdigit() and 0 < int(raw) <= 100:
                    safe.append(f'{name}="{raw}"')
                continue
            if name in _SAFE_SCIENCE_ATTRS:
                science_value = _safe_science_attr(name, raw)
                if science_value is not None:
                    safe.append(f'{name}="{html.escape(science_value, quote=True)}"')
                continue
            if (
                name in _SAFE_GLOBAL_ATTRS
                or name.startswith("aria-") or name.startswith("data-")
            ):
                if len(raw) > 65536:
                    continue
                safe.append(f'{name}="{html.escape(raw, quote=True)}"')
        if target and not class_seen:
            safe.append('class="flori-source-target"')
        return (" " + " ".join(safe)) if safe else ""

    @staticmethod
    def _is_layout_chrome(tag: str, attrs: list[tuple[str, str | None]]) -> bool:
        if tag == "nav":
            return True
        if tag not in {"header", "footer", "aside", "div"}:
            return False
        values = " ".join(
            value or "" for key, value in attrs
            if key.lower() in {"id", "class", "role"}
        ).lower().replace("_", "-")
        tokens = set(values.split())
        return bool(tokens & _CHROME_MARKERS)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._count_start_tag(attrs)
        normalized = tag.lower()
        path = self._next_path(normalized)
        parent_drop = bool(self._stack[-1]["drop"])
        drop = (
            parent_drop
            or normalized in _DROP_WITH_CONTENT
            or self._is_layout_chrome(normalized, attrs)
        )
        if normalized == "body":
            self._body_seen = True
        svg_title = normalized == "title" and any(
            entry["tag"] == "svg" for entry in self._stack
        )
        rendered = (
            not drop
            and (normalized not in _DROP_HEAD or svg_title)
            and normalized in _SAFE_RENDER_TAGS
        )
        if rendered:
            block_id = self.anchors.get(path)
            embedded_values = [
                value or "" for key, value in attrs
                if key.lower() == "data-source-segment"
            ]
            if self.embedded_anchor_ids is not None and len(embedded_values) > 1:
                raise DocumentReaderError("duplicate embedded source segment attribute")
            embedded = embedded_values[0] if embedded_values else None
            if embedded and self.embedded_anchor_ids is not None:
                if embedded in self.embedded_anchor_ids:
                    if embedded in self._seen_anchor_ids:
                        raise DocumentReaderError("duplicate embedded source segment")
                    if block_id is not None and block_id != embedded:
                        raise DocumentReaderError("embedded source segment conflicts with document locator")
                    block_id = embedded
            if block_id is not None:
                if block_id in self._seen_anchor_ids:
                    raise DocumentReaderError("duplicate document source segment")
                self._seen_anchor_ids.add(block_id)
            target = bool(self.target_segment and block_id == self.target_segment)
            if block_id:
                anchor = html.escape(f"source-{block_id}", quote=True)
                self._append(f'<span id="{anchor}" class="flori-source-anchor"></span>')
            self._append(
                f"<{normalized}{self._attrs(normalized, attrs, target=target)}>"
            )
        else:
            target = False
        entry = {
            "tag": normalized, "path": path, "counts": {}, "drop": drop,
            "rendered": rendered, "target": target,
        }
        if normalized not in _VOID_TAGS:
            if len(self._stack) > DOCUMENT_HTML_MAX_DEPTH:
                raise DocumentReaderLimitError("document HTML nesting exceeds reader limit")
            self._stack.append(entry)
            self._open_by_tag.setdefault(normalized, []).append(entry)
            if target:
                self._target_depth += 1

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        normalized = tag.lower()
        if normalized not in _VOID_TAGS and self._stack[-1]["tag"] == normalized:
            self._close_through(self._stack[-1])

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.lower()
        matches = self._open_by_tag.get(normalized)
        if matches:
            self._close_through(matches[-1])

    def _close_through(self, target: dict[str, Any]) -> None:
        while len(self._stack) > 1:
            closing = self._stack.pop()
            matches = self._open_by_tag[closing["tag"]]
            matches.pop()
            if not matches:
                self._open_by_tag.pop(closing["tag"], None)
            if closing.get("target"):
                self._target_depth -= 1
            if closing["rendered"]:
                self._append(f"</{closing['tag']}>")
            if closing is target:
                return

    def finish(self) -> None:
        while len(self._stack) > 1:
            self._close_through(self._stack[-1])

    def handle_data(self, data: str) -> None:
        if self._stack[-1]["drop"]:
            return
        target_active = self._target_depth > 0
        exact = self.target_exact
        if target_active and exact and not self._target_marked and exact in data:
            before, matched, after = data.partition(exact)
            self._append_escaped(before)
            self._append(
                '<mark class="flori-exact-target">'
            )
            self._append_escaped(matched)
            self._append("</mark>")
            self._append_escaped(after)
            self._target_marked = True
            return
        self._append_escaped(data)

    def handle_entityref(self, name: str) -> None:
        if not self._stack[-1]["drop"]:
            self._append("&amp;")
            self._append_escaped(name)
            self._append(";")

    def handle_charref(self, name: str) -> None:
        if not self._stack[-1]["drop"]:
            self._append("&amp;#")
            self._append_escaped(name)
            self._append(";")


def render_document_html(
    source: bytes,
    *,
    job_id: str,
    document: Mapping[str, Any] | None = None,
    embedded_anchor_ids: frozenset[str] | None = None,
    target_segment: str | None = None,
    target_exact: str | None = None,
    snapshot_css: str | None = None,
    snapshot_resource_digests: Mapping[str, str] | None = None,
) -> bytes:
    """生成隔离阅读副本；调用方持有的 source bytes 不会被修改。"""
    try:
        decoded = source.decode("utf-8-sig")
    except UnicodeDecodeError:
        decoded = source.decode("gb18030")
    builder = _BoundedHtmlBuilder()
    style = (
        _DOCUMENT_SNAPSHOT_FRAME_STYLE + (snapshot_css or "")
        if snapshot_css is not None
        else _DOCUMENT_STYLE
    )
    builder.append(
        '<!doctype html><html lang="und"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>文档原文</title><style>{style}</style></head>"
        '<body><main class="flori-document">'
    )
    parser = _SafeDocumentParser(
        job_id,
        source_anchor_map(document or {}),
        builder=builder,
        embedded_anchor_ids=embedded_anchor_ids,
        target_segment=target_segment,
        target_exact=target_exact,
        snapshot_resource_digests=snapshot_resource_digests,
    )
    parser.feed(decoded)
    parser.close()
    parser.finish()
    builder.append("</main></body></html>")
    return builder.build()


def document_html_headers() -> dict[str, str]:
    return {
        "Content-Security-Policy": (
            "default-src 'none'; script-src 'none'; connect-src 'none'; "
            "object-src 'none'; frame-src 'none'; worker-src 'none'; media-src 'none'; "
            "img-src 'self' data:; style-src 'unsafe-inline'; font-src 'self' data:; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'self'; "
            "sandbox allow-same-origin allow-popups allow-popups-to-escape-sandbox"
        ),
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "Permissions-Policy": "camera=(), geolocation=(), microphone=(), payment=(), usb=()",
        "Cross-Origin-Resource-Policy": "same-origin",
        "Cache-Control": "private, no-store",
    }
