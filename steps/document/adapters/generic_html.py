"""把通用网页 HTML 解析为统一 Document Model，且不改写原始来源。"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import re
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import urljoin, urlparse

from shared.document_contract import (
    DOCUMENT_SCHEMA_VERSION,
    QUALITY_SCHEMA_VERSION,
    DocumentAdapterInput,
    DocumentContractError,
    canonicalize_document,
    stable_id,
    validate_document,
    validate_quality,
)

from .html_dom import HtmlNode, parse_html


_MIN_BODY_CHARS = 200
_NOISE_TAGS = frozenset({
    "script", "style", "noscript", "nav", "header", "footer", "aside",
    "form", "button", "template",
})
_NOISE_TOKEN_RE = re.compile(
    r"(?:^|[-_\s])(advert(?:isement)?|promo|related|recommend(?:ed|ations?)?|"
    r"newsletter|cookie|social[-_]?share|site[-_]?nav|site[-_]?footer)(?:$|[-_\s])",
    re.I,
)
_PAYWALL_TOKEN_RE = re.compile(
    r"(?:^|[-_\s])(paywall|metered|subscriber[-_]?only|subscription[-_]?wall|"
    r"premium[-_]?content|locked[-_]?content)(?:$|[-_\s])",
    re.I,
)
_PAYWALL_TEXT_RE = re.compile(
    r"subscribe to (?:continue|read|keep reading)|members only|for subscribers only|"
    r"登录后(?:查看|阅读)|订阅后阅读|开通会员|仅限会员|购买后阅读",
    re.I,
)
_DYNAMIC_TEXT_RE = re.compile(
    r"enable javascript|__NEXT_DATA__|webpackJsonp|__NUXT__|data-reactroot|"
    r"id=[\"'](?:app|root|__next)[\"']",
    re.I,
)
_READ_MORE_RE = re.compile(
    r"(?:^|[-_\s])(read[-_]?more|continue[-_]?reading|load[-_]?more)"
    r"(?:$|[-_\s])",
    re.I,
)
_PAGINATION_TOKEN_RE = re.compile(
    r"(?:^|[-_\s])(pagination|pager|next[-_]?page|page[-_]?next)(?:$|[-_\s])",
    re.I,
)
_CONTENT_TOKEN_RE = re.compile(
    r"(?:^|[-_\s])(article|content|entry|main|post)(?:$|[-_\s])", re.I,
)
_BLOCK_TAGS = frozenset({
    "article", "main", "section", "div", "p", "h1", "h2", "h3", "h4",
    "h5", "h6", "ul", "ol", "blockquote", "pre", "table", "figure",
    "iframe", "video", "audio", "embed", "hr",
})
_UNSAFE_URL_SCHEMES = frozenset({"javascript", "data", "vbscript"})


def _effective_len(value: str) -> int:
    return len("".join(value.split()))


def _tokens(node: HtmlNode) -> str:
    return " ".join((node.attrs.get("id", ""), node.attrs.get("class", "")))


def _is_noise(node: HtmlNode) -> bool:
    if node.tag in _NOISE_TAGS:
        return True
    if node.attrs.get("role", "").lower() in {
        "banner", "navigation", "complementary", "contentinfo",
    }:
        return True
    return bool(_NOISE_TOKEN_RE.search(_tokens(node)))


def _text(node: HtmlNode, *, preserve: bool = False) -> str:
    if _is_noise(node):
        return ""
    parts: list[str] = []
    for item in node.content:
        parts.append(item if isinstance(item, str) else _text(item, preserve=preserve))
    value = "".join(parts)
    if preserve:
        return value.strip("\n")
    return " ".join(value.split())


def _inline_text(node: HtmlNode) -> str:
    parts: list[str] = []
    for item in node.content:
        if isinstance(item, str):
            parts.append(item)
        elif item.tag not in _BLOCK_TAGS and not _is_noise(item):
            parts.append(_text(item))
    return " ".join("".join(parts).split())


def _nodes(root: HtmlNode, tag: str | None = None) -> list[HtmlNode]:
    return list(root.descendants(tag))


def _first(root: HtmlNode, tag: str) -> HtmlNode | None:
    return next(iter(root.descendants(tag)), None)


def _ancestor(node: HtmlNode, tag: str) -> HtmlNode | None:
    current = node.parent
    while current is not None:
        if current.tag == tag:
            return current
        current = current.parent
    return None


def _nearest_table(node: HtmlNode) -> HtmlNode | None:
    return _ancestor(node, "table")


def _locator(node: HtmlNode, fingerprint: str, exact: str | None = None) -> dict[str, Any]:
    return {
        "html": {
            "source_id": "html",
            "source_fingerprint": fingerprint,
            "dom_path": node.dom_path,
            "exact": (exact if exact is not None else _text(node))[:4096],
        },
    }


def _safe_url(value: str | None, base_url: str | None) -> str | None:
    raw = (value or "").strip()
    if not raw:
        return None
    parsed = urlparse(raw)
    if parsed.scheme.lower() in _UNSAFE_URL_SCHEMES:
        return None
    resolved = urljoin(base_url, raw) if base_url else raw
    parsed = urlparse(resolved)
    if parsed.scheme and parsed.scheme.lower() not in {"http", "https", "mailto"}:
        return None
    return resolved


def _host(value: str | None) -> str:
    return (urlparse(value or "").hostname or "").lower().removeprefix("www.")


def _jsonld_objects(root: HtmlNode) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []

    def add(value: object) -> None:
        if isinstance(value, list):
            for item in value:
                add(item)
        elif isinstance(value, dict):
            graph = value.get("@graph")
            if isinstance(graph, list):
                add(graph)
            result.append(value)

    for node in root.descendants("script"):
        if "ld+json" not in node.attrs.get("type", "").lower():
            continue
        try:
            add(json.loads(node.raw_text().strip()))
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
    return result


def _meta_map(root: HtmlNode) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for node in root.descendants("meta"):
        key = (
            node.attrs.get("property") or node.attrs.get("name")
            or node.attrs.get("itemprop") or ""
        ).strip().lower()
        value = node.attrs.get("content", "").strip()
        if key and value:
            result.setdefault(key, []).append(value)
    return result


def _meta_first(meta: Mapping[str, list[str]], *keys: str) -> str:
    for key in keys:
        values = meta.get(key.lower()) or []
        if values and values[0].strip():
            return values[0].strip()
    return ""


def _author_names(value: object) -> list[str]:
    items = value if isinstance(value, list) else [value]
    result: list[str] = []
    for item in items:
        if isinstance(item, str):
            name = item.strip()
        elif isinstance(item, dict):
            name = str(item.get("name") or "").strip()
        else:
            name = ""
        if name and name not in result:
            result.append(name)
    return result


def _jsonld_article(jsonld: Iterable[dict[str, Any]]) -> dict[str, Any]:
    candidates = []
    for item in jsonld:
        raw_type = item.get("@type")
        types = raw_type if isinstance(raw_type, list) else [raw_type]
        if any(str(value).lower() in {
            "article", "newsarticle", "blogposting", "techarticle", "report",
        } for value in types):
            candidates.append(item)
    if not candidates:
        return {}
    return max(candidates, key=lambda item: _effective_len(str(item.get("articleBody") or "")))


def _select_body(root: HtmlNode) -> tuple[HtmlNode, str]:
    for tag in ("article", "main"):
        candidates = _nodes(root, tag)
        if candidates:
            return max(candidates, key=lambda node: _effective_len(_text(node))), tag
    content_candidates = [
        node for node in root.descendants()
        if node.tag in {"div", "section"}
        and _CONTENT_TOKEN_RE.search(_tokens(node))
        and not _is_noise(node)
    ]
    if content_candidates:
        return max(
            content_candidates, key=lambda node: _effective_len(_text(node)),
        ), "content"
    body = _first(root, "body")
    return (body, "body") if body is not None else (root, "document")


def _positive_int(value: str | None) -> int | None:
    try:
        parsed = int(value or "1")
    except ValueError:
        return None
    return parsed if 1 <= parsed <= 100 else None


def _optional_positive_int(value: str | None) -> int | None:
    if value is None or not value.strip():
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed > 0 else None


class _ContentBuilder:
    """把正文 DOM 投影成有稳定父子关系的 blocks 和视觉注册表。"""

    def __init__(self, fingerprint: str, base_url: str | None, job_dir: Path) -> None:
        self.fingerprint = fingerprint
        self.base_url = base_url
        self.job_dir = job_dir
        self.blocks: list[dict[str, Any]] = []
        self.figures: list[dict[str, Any]] = []
        self.tables: list[dict[str, Any]] = []
        self.assets: list[dict[str, Any]] = []
        self.references: list[dict[str, Any]] = []
        self.reasons: list[str] = []
        self.metrics = {"ignored_nodes": 0, "unsafe_references": 0, "unsafe_embeds": 0}
        self._order = 0
        self._title_id: str | None = None
        self._section_stack: list[tuple[int, str]] = []
        self._owner: dict[HtmlNode, str] = {}
        self._processed_media: set[HtmlNode] = set()
        self._table_nodes: set[HtmlNode] = set()
        self._figure_nodes: set[HtmlNode] = set()

    def build(
        self,
        body: HtmlNode,
        *,
        title: str,
        title_node: HtmlNode | None,
    ) -> None:
        if title:
            source = title_node or body
            self._title_id = self._add_block(source, "title", title, parent_id=None)
            if title_node is not None:
                self._owner[title_node] = self._title_id
        self._walk_container(body)
        self._collect_references(body)
        self._collect_assets(body)
        self._finalize_figures()

    def _parent(self) -> str | None:
        return self._section_stack[-1][1] if self._section_stack else self._title_id

    def _add_block(
        self,
        node: HtmlNode,
        kind: str,
        text: str,
        *,
        parent_id: str | None = None,
        suffix: str = "",
        extra: Mapping[str, Any] | None = None,
    ) -> str:
        block_id = stable_id(
            "blk", self.fingerprint, node.dom_path, kind, suffix,
        )
        block: dict[str, Any] = {
            "block_id": block_id,
            "parent_id": parent_id,
            "order": self._order,
            "kind": kind,
            "text": text,
            "locator": _locator(node, self.fingerprint, text),
        }
        if extra:
            block.update(extra)
        self.blocks.append(block)
        self._order += 1
        self._owner[node] = block_id
        return block_id

    def _walk_container(self, node: HtmlNode, *, parent_override: str | None = None) -> None:
        buffer: list[str] = []

        def flush() -> None:
            value = " ".join("".join(buffer).split())
            buffer.clear()
            if value:
                self._add_block(
                    node, "paragraph", value,
                    parent_id=parent_override if parent_override is not None else self._parent(),
                    suffix=f"direct-{self._order}",
                )

        for item in node.content:
            if isinstance(item, str):
                buffer.append(item)
                continue
            if _is_noise(item):
                self.metrics["ignored_nodes"] += 1
                continue
            if item.tag not in _BLOCK_TAGS:
                buffer.append(_text(item))
                continue
            flush()
            self._walk_block(item, parent_override=parent_override)
        flush()

    def _walk_block(self, node: HtmlNode, *, parent_override: str | None = None) -> None:
        tag = node.tag
        if tag in {"article", "main", "section", "div"}:
            self._walk_container(node, parent_override=parent_override)
            return
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            value = _text(node)
            if not value:
                return
            if tag == "h1" and self._title_id and value == self.blocks[0]["text"]:
                self._owner[node] = self._title_id
                return
            level = int(tag[1])
            while self._section_stack and self._section_stack[-1][0] >= level:
                self._section_stack.pop()
            parent_id = self._section_stack[-1][1] if self._section_stack else self._title_id
            block_id = self._add_block(
                node, "heading", value, parent_id=parent_id, extra={"level": level},
            )
            self._section_stack.append((level, block_id))
            return
        if tag == "p":
            value = _text(node)
            if value:
                kind = "footnote" if self._is_footnote(node) else "paragraph"
                self._add_block(
                    node, kind, value,
                    parent_id=parent_override if parent_override is not None else self._parent(),
                )
            self._add_inline_media(node)
            return
        if tag in {"ul", "ol"}:
            self._add_list(node, parent_override=parent_override)
            return
        if tag == "blockquote":
            if node.has_class_token("twitter-tweet"):
                self._add_embed(node, "twitter", parent_override=parent_override)
            else:
                value = _text(node)
                if value:
                    self._add_block(
                        node, "quote", value,
                        parent_id=parent_override if parent_override is not None else self._parent(),
                    )
            return
        if tag == "pre":
            value = _text(node, preserve=True)
            if value:
                language = self._code_language(node)
                self._add_block(
                    node, "code", value,
                    parent_id=parent_override if parent_override is not None else self._parent(),
                    extra={"language": language},
                )
            return
        if tag == "table":
            self._add_table(node, parent_override=parent_override)
            return
        if tag == "figure":
            self._add_figure(node, parent_override=parent_override)
            return
        if tag == "img":
            self._add_figure(node, parent_override=parent_override, implicit=True)
            return
        if tag in {"iframe", "video", "audio", "embed"}:
            self._add_embed(node, tag, parent_override=parent_override)

    def _add_list(self, node: HtmlNode, *, parent_override: str | None) -> None:
        parent = parent_override if parent_override is not None else self._parent()
        list_id = self._add_block(
            node, "list", "", parent_id=parent,
            extra={"ordered": node.tag == "ol"},
        )
        direct_items = [child for child in node.children if child.tag == "li"]
        for index, item in enumerate(direct_items):
            value = _inline_text(item) or _text(item)
            kind = "footnote" if self._is_footnote(item) else "list_item"
            item_id = self._add_block(
                item, kind, value, parent_id=list_id, suffix=str(index),
            )
            self._add_inline_media(item)
            for child in item.children:
                if child.tag in {"ul", "ol"}:
                    self._add_list(child, parent_override=item_id)

    @staticmethod
    def _is_footnote(node: HtmlNode) -> bool:
        identity = " ".join((
            node.attrs.get("id", ""), node.attrs.get("class", ""),
            node.attrs.get("role", ""),
        )).lower()
        return bool(re.search(r"(?:^|[-_\s])(footnote|endnote|fn\d*)", identity))

    @staticmethod
    def _code_language(node: HtmlNode) -> str | None:
        code = node.first_descendant("code")
        classes = (code or node).attrs.get("class", "")
        match = re.search(r"(?:language|lang)-([A-Za-z0-9_+-]+)", classes)
        return match.group(1) if match else None

    def _add_table(self, node: HtmlNode, *, parent_override: str | None) -> None:
        if node in self._table_nodes:
            return
        self._table_nodes.add(node)
        caption_node = next(
            (child for child in node.children if child.tag == "caption"), None,
        )
        caption = _text(caption_node) if caption_node is not None else ""
        label = self._visual_label(caption, "Table", len(self.tables) + 1)
        parent = parent_override if parent_override is not None else self._parent()
        table_block_id = self._add_block(
            node, "table", caption, parent_id=parent,
            extra={"label": label},
        )
        if caption_node is not None and caption:
            self._add_block(caption_node, "caption", caption, parent_id=table_block_id)

        rows: list[dict[str, Any]] = []
        degraded: list[str] = []
        row_nodes = [row for row in node.descendants("tr") if _nearest_table(row) is node]
        for row_index, row_node in enumerate(row_nodes):
            section = "body"
            ancestor = row_node.parent
            while ancestor is not None and ancestor is not node:
                if ancestor.tag in {"thead", "tbody", "tfoot"}:
                    section = {
                        "thead": "header", "tbody": "body", "tfoot": "footer",
                    }[ancestor.tag]
                    break
                ancestor = ancestor.parent
            cells: list[dict[str, Any]] = []
            for cell_index, cell in enumerate(
                child for child in row_node.children if child.tag in {"th", "td"}
            ):
                rowspan = _positive_int(cell.attrs.get("rowspan"))
                colspan = _positive_int(cell.attrs.get("colspan"))
                if rowspan is None or colspan is None:
                    degraded.append("invalid_table_span")
                    rowspan = rowspan or 1
                    colspan = colspan or 1
                cell_id = stable_id(
                    "cell", self.fingerprint, cell.dom_path, str(row_index), str(cell_index),
                )
                text = _text(cell)
                cell_block_id = self._add_block(
                    cell, "table_cell", text, parent_id=table_block_id,
                    suffix=f"{row_index}-{cell_index}",
                    extra={
                        "cell_id": cell_id, "rowspan": rowspan, "colspan": colspan,
                        "header": cell.tag == "th",
                    },
                )
                entry = {
                    "cell_id": cell_id,
                    "block_id": cell_block_id,
                    "kind": "header" if cell.tag == "th" else "data",
                    "text": text,
                    "rowspan": rowspan,
                    "colspan": colspan,
                    "locator": _locator(cell, self.fingerprint, text),
                }
                cells.append(entry)
            rows.append({"section": section, "cells": cells})

        grid, grid_degraded = self._table_grid(rows)
        degraded.extend(grid_degraded)
        if not rows or not any(row["cells"] for row in rows):
            degraded.append("empty_table")
        table_id = stable_id("tbl", self.fingerprint, node.dom_path)
        reasons = list(dict.fromkeys(degraded))
        if reasons:
            self._reason("table_structure_degraded")
        self.tables.append({
            "table_id": table_id,
            "block_id": table_block_id,
            "label": label,
            "caption": caption,
            "rows": rows,
            "grid": grid,
            "quality_status": "degraded" if reasons else "complete",
            "quality_reasons": reasons,
            "source_locator": _locator(node, self.fingerprint, caption),
        })

    @staticmethod
    def _table_grid(rows: list[dict[str, Any]]) -> tuple[list[list[str | None]], list[str]]:
        occupied: dict[tuple[int, int], str] = {}
        reasons: list[str] = []
        for row_index, row in enumerate(rows):
            column = 0
            for cell in row["cells"]:
                while (row_index, column) in occupied:
                    column += 1
                for row_offset in range(cell["rowspan"]):
                    for column_offset in range(cell["colspan"]):
                        key = (row_index + row_offset, column + column_offset)
                        if key in occupied:
                            reasons.append("overlapping_table_span")
                        else:
                            occupied[key] = cell["cell_id"]
                column += cell["colspan"]
        if not occupied:
            return [], reasons
        height = max(row for row, _ in occupied) + 1
        width = max(column for _, column in occupied) + 1
        return [
            [occupied.get((row, column)) for column in range(width)]
            for row in range(height)
        ], reasons

    def _add_figure(
        self,
        node: HtmlNode,
        *,
        parent_override: str | None,
        implicit: bool = False,
    ) -> None:
        if node in self._figure_nodes or node in self._processed_media:
            return
        self._figure_nodes.add(node)
        caption_node = node.first_descendant("figcaption") if not implicit else None
        caption = _text(caption_node) if caption_node is not None else node.attrs.get("alt", "").strip()
        label = self._visual_label(caption, "Figure", len(self.figures) + 1)
        parent = parent_override if parent_override is not None else self._parent()
        block_id = self._add_block(
            node, "figure", caption, parent_id=parent, extra={"label": label},
        )
        if caption_node is not None and caption:
            self._add_block(caption_node, "caption", caption, parent_id=block_id)
        figure_id = stable_id("fig", self.fingerprint, node.dom_path)
        self.figures.append({
            "figure_id": figure_id,
            "block_id": block_id,
            "label": label,
            "caption": caption,
            "asset_ids": [],
            "quality_status": "complete",
            "quality_reasons": [],
            "source_locator": _locator(node, self.fingerprint, caption),
            "_node": node,
        })
        for media in node.descendants():
            if media.tag in {"img", "video", "audio"}:
                self._processed_media.add(media)
                self._owner.setdefault(media, block_id)
        if implicit:
            self._processed_media.add(node)
            self._owner[node] = block_id
        for table in node.descendants("table"):
            if _nearest_table(table) is table.parent or _nearest_table(table) is None:
                self._add_table(table, parent_override=block_id)

    @staticmethod
    def _visual_label(caption: str, default: str, index: int) -> str:
        match = re.search(
            r"\b(?:figure|fig\.?|table)\s*([A-Za-z0-9.-]+)|"
            r"(?:图|表)\s*([A-Za-z0-9.-]+)",
            caption,
            re.I,
        )
        if match:
            number = next(value for value in match.groups() if value)
            return f"{default} {number.rstrip('.')}"
        return f"{default} {index}"

    def add_hero_asset(self, node: HtmlNode, source_url: str) -> None:
        resolved = _safe_url(source_url, self.base_url)
        if not resolved or any(asset.get("source_url") == resolved for asset in self.assets):
            return
        mime, _ = mimetypes.guess_type(resolved)
        self.assets.append({
            "asset_id": stable_id("asset", self.fingerprint, node.dom_path, resolved),
            "kind": "hero_image",
            "source_url": resolved,
            "local_path": None,
            "sha256": None,
            "bytes": None,
            "mime_type": mime,
            "width": None,
            "height": None,
            "alt": "",
            "title": "",
            "variants": [],
            "owner_block_id": self._title_id,
            "status": "remote",
            "source_locator": _locator(node, self.fingerprint, source_url),
        })

    def _add_inline_media(self, node: HtmlNode) -> None:
        for media in node.descendants():
            if media.tag == "img" and _ancestor(media, "figure") is None:
                self._add_figure(media, parent_override=self._owner.get(node), implicit=True)
            elif media.tag in {"iframe", "video", "audio", "embed"}:
                self._add_embed(media, media.tag, parent_override=self._owner.get(node))

    def _add_embed(
        self,
        node: HtmlNode,
        embed_type: str,
        *,
        parent_override: str | None,
    ) -> None:
        if node in self._processed_media:
            return
        self._processed_media.add(node)
        raw_src = node.attrs.get("src", "")
        if not raw_src and embed_type in {"video", "audio"}:
            source = node.first_descendant("source")
            raw_src = source.attrs.get("src", "") if source is not None else ""
        if embed_type == "twitter":
            link = next(iter(node.descendants("a")), None)
            raw_src = link.attrs.get("href", "") if link is not None else ""
        src = _safe_url(raw_src, self.base_url)
        if raw_src and src is None:
            self.metrics["unsafe_embeds"] += 1
            self._reason("unsafe_embed_ignored")
        title = node.attrs.get("title", "").strip() or _text(node)
        parent = parent_override if parent_override is not None else self._parent()
        self._add_block(
            node, "embed", title, parent_id=parent,
            extra={
                "embed": {
                    "type": embed_type,
                    "source_url": src,
                    "allow_script_execution": False,
                },
            },
        )

    def _collect_references(self, body: HtmlNode) -> None:
        source_host = _host(self.base_url)
        id_targets = {
            node.attrs["id"]: self._nearest_owner(node)
            for node in body.descendants()
            if node.attrs.get("id")
        }
        for node in body.descendants("a"):
            raw = node.attrs.get("href", "").strip()
            resolved = _safe_url(raw, self.base_url)
            if not raw:
                continue
            if resolved is None:
                self.metrics["unsafe_references"] += 1
                continue
            fragment = urlparse(raw).fragment
            if raw.startswith("#") and re.match(r"(?:fn|footnote|endnote)", fragment, re.I):
                kind = "footnote"
            elif raw.startswith("#"):
                kind = "internal"
            elif urlparse(resolved).scheme == "mailto":
                kind = "email"
            elif source_host and _host(resolved) == source_host:
                kind = "internal"
            else:
                kind = "external"
            self.references.append({
                "reference_id": stable_id("ref", self.fingerprint, node.dom_path, resolved),
                "kind": kind,
                "text": _text(node),
                "target": resolved,
                "target_block_id": id_targets.get(fragment),
                "source_block_id": self._nearest_owner(node),
                "source_locator": _locator(node, self.fingerprint, _text(node)),
            })

    def _collect_assets(self, body: HtmlNode) -> None:
        media_nodes = [
            node for node in body.descendants()
            if node.tag in {"img", "video", "audio"}
        ]
        for node in media_nodes:
            candidates = self._media_candidates(node)
            primary = next((item for item in candidates if item), "")
            resolved = _safe_url(primary, self.base_url)
            local_path = None
            status = "missing"
            sha256 = None
            size = None
            if primary and not urlparse(primary).scheme:
                candidate = (self.job_dir / primary.lstrip("/")).resolve()
                try:
                    candidate.relative_to(self.job_dir.resolve())
                except ValueError:
                    candidate = Path()
                if candidate and candidate.is_file():
                    local_path = str(candidate.relative_to(self.job_dir.resolve()))
                    raw = candidate.read_bytes()
                    sha256 = "sha256:" + hashlib.sha256(raw).hexdigest()
                    size = len(raw)
                    status = "available_local"
            if resolved and urlparse(resolved).scheme in {"http", "https"}:
                status = "remote"
            if status == "missing":
                self._reason("asset_missing")
            mime, _ = mimetypes.guess_type(primary)
            asset_id = stable_id("asset", self.fingerprint, node.dom_path, primary)
            self.assets.append({
                "asset_id": asset_id,
                "kind": "image" if node.tag == "img" else node.tag,
                "source_url": resolved,
                "local_path": local_path,
                "sha256": sha256,
                "bytes": size,
                "mime_type": mime,
                "width": _optional_positive_int(node.attrs.get("width")),
                "height": _optional_positive_int(node.attrs.get("height")),
                "alt": node.attrs.get("alt", "").strip(),
                "title": node.attrs.get("title", "").strip(),
                "variants": [
                    _safe_url(item, self.base_url) for item in candidates[1:]
                    if _safe_url(item, self.base_url)
                ],
                "owner_block_id": self._nearest_owner(node),
                "status": status,
                "source_locator": _locator(node, self.fingerprint, node.attrs.get("alt", "")),
            })

    @staticmethod
    def _media_candidates(node: HtmlNode) -> list[str]:
        result: list[str] = []

        def add(value: str | None) -> None:
            raw = (value or "").strip()
            if raw and raw not in result:
                result.append(raw)

        add(node.attrs.get("src"))
        add(node.attrs.get("data-src"))
        add(node.attrs.get("data-original"))
        for key in ("srcset", "data-srcset"):
            for candidate in node.attrs.get(key, "").split(","):
                add(candidate.strip().split(" ", 1)[0])
        picture = _ancestor(node, "picture")
        source_root = picture or node
        for source in source_root.descendants("source"):
            add(source.attrs.get("src"))
            for candidate in source.attrs.get("srcset", "").split(","):
                add(candidate.strip().split(" ", 1)[0])
        return result

    def _finalize_figures(self) -> None:
        asset_by_owner: dict[str, list[str]] = {}
        for asset in self.assets:
            owner = asset.get("owner_block_id")
            if owner:
                asset_by_owner.setdefault(owner, []).append(asset["asset_id"])
        for figure in self.figures:
            node = figure.pop("_node")
            block_id = figure["block_id"]
            ids = list(asset_by_owner.get(block_id, []))
            if not ids:
                descendant_owners = {
                    self._nearest_owner(item) for item in node.descendants()
                    if item.tag in {"img", "video", "audio"}
                }
                for owner in descendant_owners:
                    ids.extend(asset_by_owner.get(owner or "", []))
            figure["asset_ids"] = list(dict.fromkeys(ids))
            if not figure["asset_ids"]:
                figure["quality_status"] = "degraded"
                figure["quality_reasons"] = ["figure_media_missing"]
                self._reason("figure_media_missing")

    def _nearest_owner(self, node: HtmlNode) -> str | None:
        current: HtmlNode | None = node
        while current is not None:
            owner = self._owner.get(current)
            if owner:
                return owner
            current = current.parent
        return self._parent()

    def _reason(self, code: str) -> None:
        if code not in self.reasons:
            self.reasons.append(code)


class GenericHtmlAdapter:
    """解析 generic_html source profile；只返回内存对象，不发布 artifact。"""

    def __init__(self, job_dir: Path, job: Mapping[str, Any]) -> None:
        self.job_dir = job_dir
        self.job = dict(job)

    def parse(
        self,
        context: DocumentAdapterInput,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        source_path = self.job_dir / context.source_path
        raw = source_path.read_bytes()
        actual_fingerprint = "sha256:" + hashlib.sha256(raw).hexdigest()
        reasons: list[str] = []
        rejected: list[str] = []
        if actual_fingerprint != context.source_fingerprint:
            rejected.append("source_fingerprint_mismatch")
        try:
            source = raw.decode("utf-8-sig")
            replacement_chars = 0
        except UnicodeDecodeError:
            source = raw.decode("utf-8", errors="replace")
            replacement_chars = source.count("\ufffd")
            reasons.append("encoding_replacement")

        root = parse_html(source)
        meta = _meta_map(root)
        jsonld = _jsonld_objects(root)
        article_json = _jsonld_article(jsonld)
        body, body_kind = _select_body(root)
        body_text = _text(body)
        body_chars = _effective_len(body_text)

        source_url = context.source_url or self.job.get("url")
        sidecar = self._load_sidecar()
        final_url = (
            self.job.get("final_url") or self.job.get("resolved_url")
            or sidecar.get("final_url")
        )
        base_url = str(final_url or source_url or "") or None
        canonical_url, canonical_reasons = self._canonical_url(root, meta, base_url)
        reasons.extend(canonical_reasons)
        if canonical_url and _host(base_url) and _host(canonical_url) != _host(base_url):
            reasons.append("canonical_cross_origin")

        metadata, title_node = self._metadata(root, body, meta, article_json)
        reasons.extend(self._merge_sidecar_metadata(metadata, sidecar))
        builder = _ContentBuilder(actual_fingerprint, base_url, self.job_dir)
        builder.build(body, title=metadata.get("title", ""), title_node=title_node)
        hero_image = metadata.get("hero_image")
        hero_node = self._meta_node(root, "og:image", "twitter:image")
        if hero_image and hero_node is not None:
            builder.add_hero_asset(hero_node, hero_image)
        reasons.extend(builder.reasons)

        strong_paywall = self._has_structural_paywall(root, article_json)
        paywall_text = bool(_PAYWALL_TEXT_RE.search(body_text))
        dynamic = bool(_DYNAMIC_TEXT_RE.search(source))
        declared_body = str(article_json.get("articleBody") or "")
        declared_chars = _effective_len(declared_body)
        coverage = min(1.0, body_chars / declared_chars) if declared_chars else None
        pagination = self._has_pagination(root)
        read_more = any(_READ_MORE_RE.search(_tokens(node)) for node in body.descendants())
        heading_count = sum(
            node.tag in {"h1", "h2", "h3", "h4", "h5", "h6"}
            for node in body.descendants()
        )
        pdf_candidate = bool(_meta_first(meta, "citation_pdf_url")) or any(
            urlparse(node.attrs.get("href", "")).path.lower().endswith(".pdf")
            for node in body.descendants("a")
        )
        metadata_only = (
            context.document_kind == "research_paper"
            and pdf_candidate
            and body_chars < 4000
            and heading_count < 3
            and not declared_body
        )

        if not body_text:
            rejected.append("body_missing")
        elif body_chars < _MIN_BODY_CHARS:
            rejected.append("body_too_short")
        if strong_paywall or (paywall_text and body_chars < _MIN_BODY_CHARS * 3):
            rejected.append("paywall_detected")
        if dynamic and body_chars < _MIN_BODY_CHARS:
            rejected.append("dynamic_content_unavailable")
        if coverage is not None and coverage < 0.5:
            rejected.append("severe_truncation")
        elif coverage is not None and coverage < 0.9:
            reasons.append("possible_truncation")
        if pagination:
            reasons.append("pagination_detected")
        if read_more:
            reasons.append("read_more_boundary_detected")
        if metadata_only:
            rejected.append("full_text_unavailable")
        if body_kind in {"body", "document"}:
            reasons.append("body_boundary_uncertain")

        reasons = list(dict.fromkeys(rejected + reasons))
        status = "rejected" if rejected else ("degraded" if reasons else "complete")
        document = {
            "schema_version": DOCUMENT_SCHEMA_VERSION,
            "job_id": context.job_id,
            "content_type": "document",
            "document_kind": context.document_kind,
            "classification": {
                "method": "user" if self.job.get("document_kind") else "metadata",
                "confidence": 1.0 if self.job.get("document_kind") else 0.8,
            },
            "source_profile": context.source_profile,
            "capabilities": ["html", "embedded_media"],
            "primary_source_id": "html",
            "sources": [{
                "source_id": "html",
                "source_profile": context.source_profile,
                "capabilities": ["html", "embedded_media"],
                "path": context.source_path,
                "fingerprint": actual_fingerprint,
                "source_url": source_url,
                "final_url": final_url,
                "canonical_url": canonical_url,
                "fetched_at": self.job.get("fetched_at") or sidecar.get("fetched_at"),
                "mime_type": "text/html",
                "encoding": "utf-8",
                "immutable": True,
            }],
            "metadata": metadata,
            "blocks": builder.blocks,
            "figures": builder.figures,
            "tables": builder.tables,
            "references": builder.references,
            "assets": builder.assets,
        }
        quality = {
            "schema_version": QUALITY_SCHEMA_VERSION,
            "job_id": context.job_id,
            "status": status,
            "reasons": reasons,
            "metrics": {
                "source_bytes": len(raw),
                "replacement_chars": replacement_chars,
                "body_candidate": body_kind,
                "body_chars": body_chars,
                "declared_body_chars": declared_chars or None,
                "extraction_coverage": round(coverage, 6) if coverage is not None else None,
                "full_text_candidate": not metadata_only,
                "blocks": len(builder.blocks),
                "headings": sum(block["kind"] == "heading" for block in builder.blocks),
                "lists": sum(block["kind"] == "list" for block in builder.blocks),
                "code_blocks": sum(block["kind"] == "code" for block in builder.blocks),
                "figures": len(builder.figures),
                "tables": len(builder.tables),
                "assets": len(builder.assets),
                "references": len(builder.references),
                "embeds": sum(block["kind"] == "embed" for block in builder.blocks),
                **builder.metrics,
            },
        }
        return (
            validate_document(
                canonicalize_document(document),
                expected_job_id=context.job_id,
            ),
            validate_quality(quality, expected_job_id=context.job_id),
        )

    def _load_sidecar(self) -> dict[str, Any]:
        path = self.job_dir / "input" / "metadata.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _canonical_url(
        root: HtmlNode,
        meta: Mapping[str, list[str]],
        base_url: str | None,
    ) -> tuple[str | None, list[str]]:
        reasons: list[str] = []
        link_value = ""
        for node in root.descendants("link"):
            rel = set(node.attrs.get("rel", "").lower().split())
            if "canonical" in rel:
                link_value = node.attrs.get("href", "").strip()
                if link_value:
                    break
        og_value = _meta_first(meta, "og:url")
        resolved_link = _safe_url(link_value, base_url)
        resolved_og = _safe_url(og_value, base_url)
        canonical = resolved_link or resolved_og
        if (link_value and resolved_link is None) or (og_value and resolved_og is None):
            reasons.append("canonical_url_invalid")
        if resolved_link and resolved_og and resolved_link != resolved_og:
            reasons.append("canonical_conflict")
        return canonical, reasons

    @staticmethod
    def _metadata(
        root: HtmlNode,
        body: HtmlNode,
        meta: Mapping[str, list[str]],
        article_json: Mapping[str, Any],
    ) -> tuple[dict[str, Any], HtmlNode | None]:
        h1 = _first(body, "h1")
        title_tag = _first(root, "title")
        title = (
            (_text(h1) if h1 is not None else "")
            or str(article_json.get("headline") or "").strip()
            or _meta_first(meta, "og:title", "twitter:title")
            or (_text(title_tag) if title_tag is not None else "")
        )
        authors = _author_names(article_json.get("author"))
        if not authors:
            authors = _author_names(_meta_first(meta, "author", "article:author"))
        keywords = article_json.get("keywords") or _meta_first(meta, "keywords", "article:tag")
        if isinstance(keywords, str):
            tags = [item.strip() for item in re.split(r"[,;，、]", keywords) if item.strip()]
        elif isinstance(keywords, list):
            tags = [str(item).strip() for item in keywords if str(item).strip()]
        else:
            tags = []
        html_node = _first(root, "html")
        license_url = ""
        for node in root.descendants("link"):
            if "license" in node.attrs.get("rel", "").lower().split():
                license_url = node.attrs.get("href", "").strip()
                break
        publisher = _meta_first(meta, "og:site_name")
        if not publisher and isinstance(article_json.get("publisher"), dict):
            publisher = str((article_json["publisher"] or {}).get("name") or "").strip()
        return {
            "title": title,
            "authors": authors,
            "abstract": (
                str(article_json.get("description") or "").strip()
                or _meta_first(meta, "description", "og:description")
            ),
            "publisher": publisher,
            "published_at": (
                str(article_json.get("datePublished") or "").strip()
                or _meta_first(meta, "article:published_time")
            ),
            "updated_at": (
                str(article_json.get("dateModified") or "").strip()
                or _meta_first(meta, "article:modified_time", "last-modified")
            ),
            "language": (
                str(article_json.get("inLanguage") or "").strip()
                or ((html_node.attrs.get("lang", "").strip()) if html_node is not None else "")
            ),
            "tags": tags,
            "license": license_url or _meta_first(meta, "license"),
            "hero_image": _meta_first(meta, "og:image", "twitter:image"),
        }, h1 or title_tag

    @staticmethod
    def _merge_sidecar_metadata(
        metadata: dict[str, Any], sidecar: Mapping[str, Any],
    ) -> list[str]:
        reasons: list[str] = []
        sidecar_title = str(sidecar.get("title") or "").strip()
        if sidecar_title and metadata.get("title") and sidecar_title != metadata["title"]:
            reasons.append("metadata_title_conflict")
        for target, source in (
            ("title", "title"), ("publisher", "sitename"),
            ("published_at", "published_at"), ("updated_at", "updated_at"),
        ):
            if not metadata.get(target) and str(sidecar.get(source) or "").strip():
                metadata[target] = str(sidecar[source]).strip()
        if not metadata.get("authors") and str(sidecar.get("author") or "").strip():
            metadata["authors"] = [
                item.strip() for item in str(sidecar["author"]).split(";") if item.strip()
            ]
        return reasons

    @staticmethod
    def _meta_node(root: HtmlNode, *keys: str) -> HtmlNode | None:
        wanted = {key.lower() for key in keys}
        for node in root.descendants("meta"):
            key = (
                node.attrs.get("property") or node.attrs.get("name")
                or node.attrs.get("itemprop") or ""
            ).strip().lower()
            if key in wanted and node.attrs.get("content", "").strip():
                return node
        return None

    @staticmethod
    def _has_structural_paywall(
        root: HtmlNode,
        article_json: Mapping[str, Any],
    ) -> bool:
        accessible = article_json.get("isAccessibleForFree")
        if accessible is False or str(accessible).lower() == "false":
            return True
        return any(_PAYWALL_TOKEN_RE.search(_tokens(node)) for node in root.descendants())

    @staticmethod
    def _has_pagination(root: HtmlNode) -> bool:
        for node in root.descendants("link"):
            if "next" in node.attrs.get("rel", "").lower().split():
                return True
        return any(_PAGINATION_TOKEN_RE.search(_tokens(node)) for node in root.descendants())


def parse_generic_html(job_dir: Path, job: dict) -> tuple[dict, dict]:
    """解析 `input/source.html` 并返回 document、quality；调用方决定是否发布。"""
    source_path = job_dir / "input" / "source.html"
    if not source_path.is_file():
        raise DocumentContractError("generic HTML source is missing")
    raw = source_path.read_bytes()
    fingerprint = "sha256:" + hashlib.sha256(raw).hexdigest()
    declared = job.get("source_fingerprint")
    context = DocumentAdapterInput(
        job_id=str(job.get("id") or job_dir.name),
        document_kind=str(job.get("document_kind") or "unknown"),
        source_profile="generic_html",
        source_fingerprint=str(declared or fingerprint),
        source_path="input/source.html",
        source_url=job.get("url"),
    )
    return GenericHtmlAdapter(job_dir, job).parse(context)
