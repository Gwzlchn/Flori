"""把不可信文档 HTML 投影成可隔离展示的只读页面。"""

from __future__ import annotations

import html
import posixpath
from html.parser import HTMLParser
from typing import Any, Mapping
from urllib.parse import quote, urlparse


DOCUMENT_HTML_MAX_BYTES = 32 * 1024 * 1024

_VOID_TAGS = frozenset({
    "area", "base", "br", "col", "hr", "img", "input", "link", "meta",
    "param", "source", "track", "wbr",
})
_DROP_WITH_CONTENT = frozenset({
    "script", "style", "noscript", "template", "iframe", "object", "embed",
    "canvas", "audio", "video", "form", "button", "input", "select", "textarea",
    "dialog",
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
@media(max-width:640px){.flori-document{padding:18px 16px 48px;font-size:16px}h1{font-size:1.65rem}h2{font-size:1.3rem}}
""".strip()


def _safe_local_path(value: str) -> str | None:
    raw = value.strip().replace("\\", "/")
    parsed = urlparse(raw)
    if parsed.scheme or parsed.netloc or raw.startswith(("/", "//")):
        return None
    normalized = posixpath.normpath(parsed.path)
    if normalized in {"", ".", ".."} or normalized.startswith("../"):
        return None
    return normalized


def _asset_url(job_id: str, value: str) -> str | None:
    raw = value.strip()
    if raw.startswith("data:image/"):
        return raw
    local = _safe_local_path(raw)
    if local is None:
        return None
    return f"/api/jobs/{quote(job_id, safe='')}/artifact?path={quote(local, safe='')}"


def _safe_link(value: str) -> str | None:
    raw = value.strip()
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


def _model_text(
    value: object,
    *,
    target: bool = False,
    target_exact: str | None = None,
) -> str:
    text = str(value or "")
    if target and target_exact and target_exact in text:
        before, matched, after = text.partition(target_exact)
        return (
            html.escape(before)
            + f'<mark class="flori-exact-target">{html.escape(matched)}</mark>'
            + html.escape(after)
        )
    return html.escape(text)


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


def _model_figure_html(
    figure: Mapping[str, Any],
    assets: Mapping[str, Mapping[str, Any]],
    *,
    job_id: str,
    attrs: str,
) -> str:
    images: list[str] = []
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
        images.append(
            f'<img src="{html.escape(source, quote=True)}" '
            f'alt="{html.escape(str(alt or ""), quote=True)}">'
        )

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
    body = "".join(images)
    if caption:
        body += f"<figcaption>{html.escape(caption)}</figcaption>"
    return f"<figure{attrs}>{body}</figure>" if body else ""


def _model_table_html(table: Mapping[str, Any], *, attrs: str) -> str:
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
    parts = [f"<table{attrs}>"]
    if caption:
        parts.append(f"<caption>{html.escape(caption)}</caption>")
    for row in rows:
        parts.append("<tr>")
        for cell in row:
            role = str(cell.get("role") or cell.get("kind") or "")
            tag = "th" if role in {"column_header", "row_header", "header"} else "td"
            rowspan = max(1, int(cell.get("rowspan") or 1))
            colspan = max(1, int(cell.get("colspan") or 1))
            parts.append(
                f'<{tag} rowspan="{rowspan}" colspan="{colspan}">'
                f'{html.escape(str(cell.get("text") or ""))}</{tag}>'
            )
        parts.append("</tr>")
    parts.append("</table>")
    return "".join(parts)


def render_document_model_html(
    document: Mapping[str, Any],
    *,
    job_id: str,
    target_segment: str | None = None,
    target_exact: str | None = None,
) -> bytes:
    """把已校验Document Model投影成正文阅读面,不重放站点DOM。"""
    metadata = document.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    title = _metadata_title(metadata)
    authors = _metadata_authors(metadata)
    published = str(metadata.get("published_at") or "").strip()
    publisher = str(metadata.get("publisher") or metadata.get("venue") or "").strip()
    abstract = str(metadata.get("abstract") or "").strip()

    parts = ['<header class="flori-document-header">']
    if title:
        parts.append(f"<h1>{html.escape(title)}</h1>")
    meta_items = []
    if authors:
        meta_items.append(f"<span>{html.escape(', '.join(authors))}</span>")
    if published:
        meta_items.append(f"<time>{html.escape(published)}</time>")
    if publisher:
        meta_items.append(f"<span>{html.escape(publisher)}</span>")
    if meta_items:
        parts.append('<div class="flori-document-meta">' + "".join(meta_items) + "</div>")
    parts.append("</header>")
    if abstract:
        parts.append(
            '<section class="flori-abstract"><span class="flori-abstract-label">Abstract</span>'
            f"<p>{html.escape(abstract)}</p></section>"
        )

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
        rendered_text = _model_text(text, target=target, target_exact=target_exact)
        if kind == "title":
            parts.append(f"<h1{attrs}>{rendered_text}</h1>")
        elif kind == "heading":
            level = min(6, max(2, int(block.get("level") or 2)))
            parts.append(f"<h{level}{attrs}>{rendered_text}</h{level}>")
        elif kind == "quote":
            parts.append(f"<blockquote{attrs}>{rendered_text}</blockquote>")
        elif kind == "code":
            parts.append(f"<pre{attrs}><code>{rendered_text}</code></pre>")
        elif kind == "list":
            tag = "ol" if block.get("ordered") else "ul"
            items = list_children.get(block_id, [])
            parts.append(f"<{tag}{attrs}>")
            for item in sorted(items, key=lambda value: int(value.get("order") or 0)):
                item_target = str(item.get("block_id") or "") == target_segment
                item_attrs = _model_block_attrs(item, target_segment)
                parts.append(
                    f"<li{item_attrs}>{_model_text(item.get('text'), target=item_target, target_exact=target_exact)}</li>"
                )
            parts.append(f"</{tag}>")
        elif kind == "figure" and block_id in figures:
            parts.append(_model_figure_html(
                figures[block_id], assets, job_id=job_id, attrs=attrs,
            ))
        elif kind == "table" and block_id in tables:
            parts.append(_model_table_html(tables[block_id], attrs=attrs))
        elif kind == "footnote":
            parts.append(f"<p{attrs}><small>{rendered_text}</small></p>")
        elif text:
            parts.append(f"<p{attrs}>{rendered_text}</p>")

    page_title = title or "文档原文"
    page = (
        '<!doctype html><html lang="und"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{html.escape(page_title)}</title><style>{_DOCUMENT_STYLE}</style></head>"
        f'<body><main class="flori-document">{"".join(parts)}</main></body></html>'
    )
    return page.encode("utf-8")


class _SafeDocumentParser(HTMLParser):
    """保留阅读结构和 MathML/SVG，同时剥离脚本、外链资源与交互能力。"""

    def __init__(
        self,
        job_id: str,
        anchors: Mapping[str, str],
        *,
        target_segment: str | None = None,
        target_exact: str | None = None,
    ) -> None:
        super().__init__(convert_charrefs=True)
        self.job_id = job_id
        self.anchors = anchors
        self.output: list[str] = []
        self._stack: list[dict[str, Any]] = [{
            "tag": "#document", "path": "", "counts": {}, "drop": False,
            "rendered": False,
        }]
        self._body_seen = False
        self.target_segment = target_segment
        self.target_exact = target_exact
        self._target_marked = False

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
        if target:
            attr_map["class"] = " ".join(
                value for value in (attr_map.get("class"), "flori-source-target") if value
            )
        if tag == "img" and not attr_map.get("src") and attr_map.get("data-artifact"):
            source = _asset_url(self.job_id, attr_map["data-artifact"])
            if source is not None:
                safe.append(f'src="{html.escape(source, quote=True)}"')
        source_attrs = list(attrs)
        if target:
            source_attrs = [(key, value) for key, value in source_attrs if key.lower() != "class"]
            source_attrs.append(("class", attr_map["class"]))
        for key, value in source_attrs:
            name = key.lower()
            raw = value or ""
            if name.startswith("on") or name in {"style", "srcdoc", "formaction"}:
                continue
            if name in _URL_ATTRS:
                resolved = _asset_url(self.job_id, raw) if name in {"src", "poster"} else _safe_link(raw)
                if resolved is None:
                    continue
                safe.append(f'{name}="{html.escape(resolved, quote=True)}"')
                if name == "href" and not resolved.startswith("#"):
                    safe.extend(['target="_blank"', 'rel="noopener noreferrer"'])
                continue
            if (
                name in _SAFE_GLOBAL_ATTRS or name in _SAFE_SCIENCE_ATTRS
                or name.startswith("aria-") or name.startswith("data-")
            ):
                safe.append(f'{name}="{html.escape(raw, quote=True)}"')
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
        rendered = not drop and normalized not in _DROP_HEAD
        if rendered:
            block_id = self.anchors.get(path)
            attr_map = {key.lower(): value or "" for key, value in attrs}
            target = bool(
                self.target_segment
                and (
                    block_id == self.target_segment
                    or attr_map.get("data-source-segment") == self.target_segment
                )
            )
            if block_id:
                anchor = html.escape(f"source-{block_id}", quote=True)
                self.output.append(f'<span id="{anchor}" class="flori-source-anchor"></span>')
            if target and not block_id:
                anchor = html.escape(f"source-{self.target_segment}", quote=True)
                self.output.append(f'<span id="{anchor}" class="flori-source-anchor"></span>')
            self.output.append(
                f"<{normalized}{self._attrs(normalized, attrs, target=target)}>"
            )
        else:
            target = False
        self._stack.append({
            "tag": normalized, "path": path, "counts": {}, "drop": drop,
            "rendered": rendered, "target": target,
        })
        if normalized in _VOID_TAGS:
            self._stack.pop()

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.lower()
        for index in range(len(self._stack) - 1, 0, -1):
            entry = self._stack[index]
            if entry["tag"] != normalized:
                continue
            for closing in reversed(self._stack[index:]):
                if closing["rendered"] and closing["tag"] not in _VOID_TAGS:
                    self.output.append(f"</{closing['tag']}>")
            del self._stack[index:]
            return

    def handle_data(self, data: str) -> None:
        if self._stack[-1]["drop"]:
            return
        target_active = any(bool(item.get("target")) for item in self._stack)
        exact = self.target_exact
        if target_active and exact and not self._target_marked and exact in data:
            before, matched, after = data.partition(exact)
            self.output.append(html.escape(before))
            self.output.append(
                f'<mark class="flori-exact-target">{html.escape(matched)}</mark>'
            )
            self.output.append(html.escape(after))
            self._target_marked = True
            return
        self.output.append(html.escape(data))

    def handle_entityref(self, name: str) -> None:
        if not self._stack[-1]["drop"]:
            self.output.append(f"&amp;{html.escape(name)};")

    def handle_charref(self, name: str) -> None:
        if not self._stack[-1]["drop"]:
            self.output.append(f"&amp;#{html.escape(name)};")


def render_document_html(
    source: bytes,
    *,
    job_id: str,
    document: Mapping[str, Any] | None = None,
    target_segment: str | None = None,
    target_exact: str | None = None,
) -> bytes:
    """生成隔离阅读副本；调用方持有的 source bytes 不会被修改。"""
    try:
        decoded = source.decode("utf-8-sig")
    except UnicodeDecodeError:
        decoded = source.decode("gb18030")
    parser = _SafeDocumentParser(
        job_id,
        source_anchor_map(document or {}),
        target_segment=target_segment,
        target_exact=target_exact,
    )
    parser.feed(decoded)
    parser.close()
    while len(parser._stack) > 1:
        entry = parser._stack.pop()
        if entry["rendered"] and entry["tag"] not in _VOID_TAGS:
            parser.output.append(f"</{entry['tag']}>")
    title = "文档原文"
    rendered = "".join(parser.output)
    page = (
        '<!doctype html><html lang="und"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{title}</title><style>{_DOCUMENT_STYLE}</style></head>"
        f'<body><main class="flori-document">{rendered}</main></body></html>'
    )
    return page.encode("utf-8")


def document_html_headers() -> dict[str, str]:
    return {
        "Content-Security-Policy": (
            "default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; "
            "font-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'self'"
        ),
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "Cache-Control": "private, no-store",
    }
