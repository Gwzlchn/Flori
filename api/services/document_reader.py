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
