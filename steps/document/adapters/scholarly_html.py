"""把学术 HTML 解析为统一 Document Model，保留公式、图表和引用坐标。"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import re
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin, urlparse

from ._common import (
    base_document,
    html_locator,
    make_id,
    quality_report,
    sha256_fingerprint,
    source_context,
)
from ._html_tree import HtmlNode, closest, dom_path, first_node, parse_html_tree


_NOISE_TAGS = frozenset({
    "script", "style", "nav", "footer", "aside", "dialog", "form",
    "button", "noscript",
})
_NOISE_CLASSES = frozenset({
    "ltx_page_header", "ltx_page_footer", "ltx_page_logo", "ltx_rdf",
    "ltx_pagination", "ltx_role_versionnotice", "ltx_ERROR", "ds-announcement",
})
_FIGURE_LABEL = re.compile(r"(?:Figure|Fig\.?|图)\s*([A-Za-z]?\d+(?:[.:-]\d+)*)", re.I)
_TABLE_LABEL = re.compile(r"(?:Table|表)\s*([A-Za-z]?\d+(?:[.:-]\d+)*)", re.I)
_EMAIL = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
_ARXIV_PATH = re.compile(r"/(?:abs|html|pdf)/([0-9]{4}\.[0-9]{4,5})(v\d+)?(?:\.pdf)?(?:$|[/?#])", re.I)


def _has_class(node: HtmlNode, *names: str) -> bool:
    return any(name in node.classes for name in names)


def _class_contains(node: HtmlNode, *parts: str) -> bool:
    return any(any(part in cls.lower() for part in parts) for cls in node.classes)


def _nodes(root: HtmlNode, tag: str | None = None) -> Iterable[HtmlNode]:
    return root.descendants(lambda node: tag is None or node.tag == tag)


def _decode_html(raw: bytes) -> tuple[str, str | None]:
    try:
        return raw.decode("utf-8-sig"), None
    except UnicodeDecodeError:
        return raw.decode("gb18030"), "html_charset_fallback"


class ScholarlyHtmlAdapter:
    def __init__(self, job_dir: Path, job: dict[str, Any]) -> None:
        self.job_dir = job_dir
        self.job = job
        self.job_id, self.document_kind, self.path, self.fingerprint = source_context(
            job_dir, job, relative_path="input/source.html",
        )
        self.raw = self.path.read_bytes()
        text, decode_reason = _decode_html(self.raw)
        self.root = parse_html_tree(text)
        self.reasons = [decode_reason] if decode_reason else []
        self.blocks: list[dict[str, Any]] = []
        self.assets: list[dict[str, Any]] = []
        self.references: list[dict[str, Any]] = []
        self.figures: list[dict[str, Any]] = []
        self.tables: list[dict[str, Any]] = []
        self._block_by_node: dict[int, str] = {}
        self._asset_by_node: dict[int, str] = {}
        self._heading_stack: list[tuple[int, str]] = []
        self._order = 0

    def parse(self) -> tuple[dict[str, Any], dict[str, Any]]:
        metadata = self._metadata()
        body = first_node(self.root, lambda node: node.tag in {"article", "main"})
        self._visit(body or self.root)
        self._collect_references()

        text_chars = sum(len(str(block.get("text") or "")) for block in self.blocks)
        if not metadata.get("title"):
            self.reasons.append("html_title_missing")
        rejected = not self.blocks or text_chars == 0
        if rejected:
            self.reasons.append("html_body_empty")
        if self.path.read_bytes() != self.raw:
            raise ValueError("HTML source changed while parsing")

        document = base_document(
            job_id=self.job_id,
            document_kind=self.document_kind,
            source_profile="scholarly_html",
            capabilities=["html", "math", "bibliography", "embedded_media"],
            relative_path="input/source.html",
            source_path=self.path,
            source_fingerprint=self.fingerprint,
            source_url=self.job.get("url"),
            metadata=metadata,
            blocks=self.blocks,
            assets=self.assets,
            references=self.references,
            figures=self.figures,
            tables=self.tables,
        )
        report = quality_report(
            self.job_id,
            reasons=self.reasons,
            rejected=rejected,
            metrics={
                "block_count": len(self.blocks),
                "text_chars": text_chars,
                "formula_count": sum(1 for block in self.blocks if block["kind"] == "formula"),
                "figure_count": len(self.figures),
                "figure_panel_count": sum(len(item["panels"]) for item in self.figures),
                "table_count": len(self.tables),
                "table_cell_count": sum(len(item["cells"]) for item in self.tables),
                "asset_count": len(self.assets),
                "reference_count": len(self.references),
            },
        )
        return document, report

    def _metadata(self) -> dict[str, Any]:
        meta: dict[str, list[str]] = {}
        for node in _nodes(self.root, "meta"):
            key = (node.attrs.get("name") or node.attrs.get("property") or "").lower()
            value = node.attrs.get("content", "").strip()
            if key and value:
                meta.setdefault(key, []).append(value)

        sidecar = self._sidecar_metadata()
        title_node = first_node(
            self.root,
            lambda node: _class_contains(node, "title_document") or node.tag == "h1",
        )
        title = str(sidecar.get("title") or "").strip()
        title = title or self._meta_first(meta, "citation_title", "dc.title", "og:title")
        title = title or (title_node.text() if title_node else "")
        abstract_node = first_node(self.root, lambda node: _class_contains(node, "abstract"))
        abstract = self._meta_first(meta, "citation_abstract", "dc.description")
        if not abstract and abstract_node:
            abstract = re.sub(r"^Abstract\s*", "", abstract_node.text(), flags=re.I).strip()

        authors = self._dom_authors(sidecar)
        if not authors:
            names = meta.get("citation_author", []) or meta.get("author", [])
            affiliations = meta.get("citation_author_institution", [])
            emails = meta.get("citation_author_email", [])
            authors = [{
                "name": name,
                "affiliations": [affiliations[index]] if index < len(affiliations) else [],
                "emails": [emails[index]] if index < len(emails) else [],
                "notes": [],
            } for index, name in enumerate(names)]
        sidecar_authors = self._sidecar_authors(sidecar)
        if not authors or (len(sidecar_authors) > 1 and len(authors) != len(sidecar_authors)):
            authors = sidecar_authors
        institutions = list(dict.fromkeys(
            affiliation for author in authors for affiliation in author["affiliations"]
        ))
        license_node = first_node(self.root, lambda node: _class_contains(node, "license"))
        license_text = self._meta_first(meta, "citation_license", "dc.rights")
        license_text = license_text or (license_node.text() if license_node else "")
        rights_notices = list(dict.fromkeys(
            node.text() for node in _nodes(self.root)
            if node.text() and (
                _class_contains(node, "copyright", "rights_notice", "license_notice")
                or (node.tag == "p" and self._is_rights_notice(node.text()))
            )
        ))
        author_notes = list(dict.fromkeys([
            *(
                node.text() for node in _nodes(self.root)
                if _has_class(node, "ltx_author_notes") and node.text()
            ),
            *(note for author in authors for note in author.get("notes", []) if note),
        ]))
        entry_match = self._arxiv_entry_match(sidecar)
        citation_arxiv_id = self._normalized_arxiv_id(
            self._meta_first(meta, "citation_arxiv_id"),
        )
        sidecar_arxiv_id = self._normalized_arxiv_id(
            str(sidecar.get("arxiv_id") or ""),
        )
        arxiv_id = (
            entry_match.group(1) if entry_match
            else sidecar_arxiv_id or citation_arxiv_id
        )
        if entry_match and citation_arxiv_id and citation_arxiv_id != arxiv_id:
            self.reasons.append("metadata_identifier_conflict")
        arxiv_version = self._meta_first(meta, "citation_arxiv_version")
        arxiv_version = arxiv_version or (entry_match.group(2) if entry_match else "")
        return {
            "title": title or str(sidecar.get("title") or ""),
            "original_title": title,
            "authors": authors,
            "institutions": institutions,
            "author_notes": author_notes,
            "abstract": str(sidecar.get("abstract") or "").strip() or abstract,
            "keywords": self._meta_values(meta, "citation_keywords", "keywords"),
            "published_at": str(sidecar.get("published_at") or "").strip() or self._meta_first(
                meta, "citation_publication_date", "article:published_time",
            ),
            "updated_at": str(sidecar.get("updated_at") or "").strip() or self._meta_first(
                meta, "citation_online_date", "article:modified_time",
            ),
            "publisher": self._meta_first(meta, "citation_publisher", "og:site_name"),
            "venue": self._meta_first(meta, "citation_journal_title", "citation_conference_title"),
            "license": license_text,
            "source_license": license_text,
            "rights_notices": rights_notices,
            "version": arxiv_version or None,
            "categories": self._meta_values(
                meta, "citation_arxiv_category", "citation_subject", "dc.subject",
            ),
            "identifiers": {
                "doi": self._meta_first(meta, "citation_doi", "dc.identifier"),
                "arxiv_id": arxiv_id,
            },
            "language": str(
                sidecar.get("language") or sidecar.get("lang")
                or self._html_language() or ""
            ).strip(),
        }

    def _sidecar_metadata(self) -> dict[str, Any]:
        path = self.job_dir / "input" / "metadata.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _sidecar_authors(sidecar: dict[str, Any]) -> list[dict[str, Any]]:
        raw = sidecar.get("authors")
        if not isinstance(raw, list):
            return []
        result: list[dict[str, Any]] = []
        for item in raw:
            if isinstance(item, dict):
                name = str(item.get("name") or "").strip()
                affiliations = item.get("affiliations")
                emails = item.get("emails")
                notes = item.get("notes")
            else:
                name = str(item).strip()
                affiliations = emails = notes = None
            if not name:
                continue
            result.append({
                "name": name,
                "affiliations": ScholarlyHtmlAdapter._metadata_values(affiliations),
                "emails": ScholarlyHtmlAdapter._metadata_values(emails),
                "notes": ScholarlyHtmlAdapter._metadata_values(notes),
            })
        return result

    @staticmethod
    def _metadata_values(value: object) -> list[str]:
        raw = value if isinstance(value, list) else [value]
        return [str(item).strip() for item in raw if item is not None and str(item).strip()]

    @staticmethod
    def _normalized_arxiv_id(value: str) -> str:
        match = re.search(r"(?:arXiv:)?(\d{4}\.\d{4,5})(?:v\d+)?", value, re.I)
        return match.group(1) if match else ""

    def _html_language(self) -> str:
        node = first_node(self.root, lambda item: item.tag == "html")
        return node.attrs.get("lang", "") if node is not None else ""

    def _arxiv_entry_match(self, sidecar: dict[str, Any]) -> re.Match[str] | None:
        for value in (
            self.job.get("url"), self.job.get("final_url"),
            sidecar.get("source_url"), sidecar.get("final_url"),
        ):
            parsed = urlparse(str(value or ""))
            if parsed.hostname and parsed.hostname.lower().removeprefix("www.") in {
                "arxiv.org", "ar5iv.labs.arxiv.org",
            }:
                match = _ARXIV_PATH.search(parsed.path + "?")
                if match:
                    return match
        return None

    @staticmethod
    def _meta_first(meta: dict[str, list[str]], *keys: str) -> str:
        for key in keys:
            values = meta.get(key)
            if values:
                return values[0].strip()
        return ""

    @staticmethod
    def _meta_values(meta: dict[str, list[str]], *keys: str) -> list[str]:
        values: list[str] = []
        for key in keys:
            for value in meta.get(key, []):
                values.extend(part.strip() for part in re.split(r"[,;]", value) if part.strip())
        return list(dict.fromkeys(values))

    @staticmethod
    def _is_rights_notice(text: str) -> bool:
        lowered = text.lower()
        return "permission to reproduce" in lowered and (
            "table" in lowered or "figure" in lowered
        )

    @staticmethod
    def _line_text(node: HtmlNode) -> list[str]:
        parts: list[str] = []

        def walk(item: HtmlNode) -> None:
            if item.tag == "br":
                parts.append("\n")
                return
            if any(name.startswith("ltx_note") for name in item.classes):
                return
            for child in item.children:
                if isinstance(child, str):
                    parts.append(child)
                else:
                    walk(child)

        walk(node)
        return [" ".join(line.split()) for line in "".join(parts).splitlines() if line.strip()]

    def _grouped_authors(self, node: HtmlNode, names: list[str]) -> list[dict[str, Any]]:
        lines = self._line_text(node)
        email_rows = [
            (index, match.group(0))
            for index, line in enumerate(lines)
            if (match := _EMAIL.search(line)) is not None
        ]
        if len(email_rows) != len(names):
            return []
        result: list[dict[str, Any]] = []
        previous_email_row = -1
        for index, name in enumerate(names):
            email_row, email = email_rows[index]
            between = lines[previous_email_row + 1:email_row]
            # ar5iv 把下一位作者接在上一位邮箱的 <br> 后。每段首行是作者名，
            # 其余非脚注文本才可能是该作者机构；没有机构时保持空列表。
            candidates = [
                value.lstrip("&").strip() for value in between[1:]
                if "@" not in value and "footnotemark" not in value.lower()
                and not value.strip().isdigit()
            ]
            affiliations = [candidates[-1]] if candidates else []
            result.append({
                "name": name,
                "affiliations": affiliations,
                "emails": [email],
                "notes": [],
            })
            previous_email_row = email_row
        return result

    def _dom_authors(self, sidecar: dict[str, Any]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        sidecar_names = [
            str(item.get("name") if isinstance(item, dict) else item).strip()
            for item in sidecar.get("authors", [])
            if str(item.get("name") if isinstance(item, dict) else item).strip()
        ] if isinstance(sidecar.get("authors"), list) else []
        for name_node in _nodes(self.root):
            if not _class_contains(name_node, "author_name", "personname"):
                continue
            name = name_node.text()
            if not name:
                continue
            owner = closest(name_node, lambda node: _class_contains(node, "creator", "author")) or name_node
            if len(sidecar_names) > 1 and len(_EMAIL.findall(owner.text())) > 1:
                grouped = self._grouped_authors(name_node, sidecar_names)
                if grouped:
                    return grouped
            identity = (dom_path(owner), name)
            if identity in seen:
                continue
            seen.add(identity)
            affiliations = list(dict.fromkeys(
                node.text() for node in owner.descendants(
                    lambda item: _class_contains(item, "affiliation", "institution")
                ) if node.text()
            ))
            owner_text = owner.text()
            emails = list(dict.fromkeys(_EMAIL.findall(owner_text)))
            notes = list(dict.fromkeys(
                node.text() for node in owner.descendants(
                    lambda item: _class_contains(item, "author_notes", "role_author")
                ) if node.text()
            ))
            result.append({
                "name": name,
                "affiliations": affiliations,
                "emails": emails,
                "notes": notes,
            })
        return result

    def _visit(self, node: HtmlNode) -> None:
        if node.tag in _NOISE_TAGS or node.classes & _NOISE_CLASSES:
            return
        if (
            (node.tag == "p" or _has_class(node, "ltx_para"))
            and self._is_rights_notice(node.text())
        ):
            return
        # ar5iv 会在整篇 article 上标 `ltx_authors_1line` 描述作者排版。
        # 这里只跳过真正的头部容器，不能用 authors 子串吞掉全文。
        if node.classes & {
            "ltx_authors", "ltx_author_notes", "ltx_role_author",
            "ltx_license", "ltx_copyright",
        }:
            return
        semantic_kind = self._semantic_kind(node)
        if semantic_kind == "appendix":
            # appendix 是结构容器，不是叶子正文；必须继续收集其中的图、表和段落。
            for child in node.children:
                if isinstance(child, HtmlNode):
                    self._visit(child)
            return
        if semantic_kind == "algorithm":
            # ar5iv 用 <figure> 承载算法，但算法不能占用 Figure 编号或图表目录。
            self._add_block("algorithm", node.text(exclude=_NOISE_TAGS), node)
            return
        if node.tag == "figure":
            if _class_contains(node, "table"):
                panels = [
                    child for child in node.descendants(
                        lambda item: item.tag == "figure" and _class_contains(item, "figure_panel")
                    )
                    if _TABLE_LABEL.search(
                        (self._caption_node(child).text() if self._caption_node(child) else "")
                    )
                ]
                if panels:
                    for panel in panels:
                        table = next(panel.descendants(lambda child: child.tag == "table"), None)
                        if table is not None:
                            self._add_table(table, wrapper=panel)
                else:
                    table = next(node.descendants(lambda child: child.tag == "table"), None)
                    if table is not None:
                        self._add_table(table, wrapper=node)
            else:
                self._add_figure(node)
            return
        if node.tag == "table":
            if _class_contains(node, "equation"):
                for math in node.descendants(lambda child: child.tag == "math"):
                    latex = math.attrs.get("alttext", "").strip() or math.text()
                    if latex:
                        self._add_block(
                            "formula", latex, math, latex=latex,
                            display=math.attrs.get("display") == "block",
                        )
            elif self._caption_node(node) is not None or _class_contains(node, "tabular"):
                self._add_table(node)
            return
        if _class_contains(node, "abstract"):
            text = re.sub(r"^Abstract\s*", "", node.text(exclude=_NOISE_TAGS), flags=re.I).strip()
            if text:
                self._add_block("abstract", text, node)
            return
        if node.tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            level = int(node.tag[1])
            kind = "title" if level == 1 and not any(block["kind"] == "title" for block in self.blocks) else "heading"
            block_id = self._add_block(kind, node.text(), node, level=level)
            if kind == "heading" and block_id:
                self._heading_stack = [item for item in self._heading_stack if item[0] < level]
                self._heading_stack.append((level, block_id))
            return
        if semantic_kind is not None:
            self._add_block(semantic_kind, node.text(exclude=_NOISE_TAGS), node)
            return
        if node.tag in {"ul", "ol"}:
            list_id = self._add_block("list", node.text(), node, list_style=node.tag)
            for child in node.children:
                if isinstance(child, HtmlNode) and child.tag == "li":
                    self._add_block("list_item", child.text(), child, parent_id=list_id)
            return
        if node.tag in {"p", "blockquote", "pre"}:
            kind = {"p": "paragraph", "blockquote": "quote", "pre": "code"}[node.tag]
            block_id = self._add_block(kind, node.text(), node)
            for math in node.descendants(lambda child: child.tag == "math"):
                latex = math.attrs.get("alttext", "").strip() or math.text()
                if latex:
                    self._add_block(
                        "formula", latex, math, parent_id=block_id,
                        latex=latex, display=math.attrs.get("display") == "block",
                    )
            return
        for child in node.children:
            if isinstance(child, HtmlNode):
                self._visit(child)

    @staticmethod
    def _semantic_kind(node: HtmlNode) -> str | None:
        classes = " ".join(node.classes).lower()
        for needle, kind in (
            ("theorem", "theorem"), ("proof", "proof"), ("algorithm", "algorithm"),
            ("appendix", "appendix"), ("footnote", "footnote"),
        ):
            if needle in classes:
                return kind
        return None

    def _current_parent(self) -> str | None:
        return self._heading_stack[-1][1] if self._heading_stack else None

    def _add_block(
        self,
        kind: str,
        text: str,
        node: HtmlNode,
        *,
        parent_id: str | None = None,
        **extra: Any,
    ) -> str | None:
        normalized = " ".join((text or "").split())
        if not normalized and kind not in {"figure", "table"}:
            return None
        path = dom_path(node)
        block_id = make_id("blk", self.fingerprint, kind, path)
        block = {
            "block_id": block_id,
            "parent_id": parent_id if parent_id is not None else self._current_parent(),
            "order": self._order,
            "kind": kind,
            "text": normalized,
            "locator": html_locator(self.fingerprint, path, exact=normalized or None),
            **extra,
        }
        self._order += 1
        self.blocks.append(block)
        self._block_by_node[id(node)] = block_id
        return block_id

    def _caption_node(self, node: HtmlNode) -> HtmlNode | None:
        direct = next(
            (
                child for child in node.children
                if isinstance(child, HtmlNode) and child.tag in {"figcaption", "caption"}
            ),
            None,
        )
        if direct is not None:
            return direct
        return next(
            (
                child for child in node.descendants(
                    lambda item: item.tag in {"figcaption", "caption"},
                )
                if self._nearest_figure(child) in {None, node}
            ),
            None,
        )

    @staticmethod
    def _nearest_figure(node: HtmlNode) -> HtmlNode | None:
        current = node.parent
        while current is not None:
            if current.tag == "figure":
                return current
            current = current.parent
        return None

    def _add_figure(self, node: HtmlNode) -> None:
        caption_node = self._caption_node(node)
        caption = caption_node.text() if caption_node else ""
        match = _FIGURE_LABEL.search(caption)
        label = f"Figure {match.group(1)}" if match else f"Figure {len(self.figures) + 1}"
        figure_id = make_id("fig", self.fingerprint, dom_path(node), label)
        block_id = self._add_block("figure", caption, node, figure_id=figure_id)
        if caption_node and caption:
            self._add_block("caption", caption, caption_node, parent_id=block_id)
        nested_panels = [
            child for child in node.descendants(
                lambda item: item.tag == "figure" and _class_contains(item, "figure_panel"),
            )
            if self._nearest_figure(child) is node
        ]
        panels: list[dict[str, Any]] = []
        panel_sources: list[tuple[HtmlNode, HtmlNode | None, str]] = []
        if nested_panels:
            for panel_node in nested_panels:
                panel_caption = self._caption_node(panel_node)
                panel_label = panel_caption.text() if panel_caption else ""
                media_nodes = [
                    child for child in panel_node.descendants()
                    if child.tag in {"img", "svg", "object", "embed"}
                    and self._nearest_figure(child) is panel_node
                ]
                if media_nodes:
                    panel_sources.extend(
                        (panel_node, media_node, panel_label) for media_node in media_nodes
                    )
                else:
                    panel_sources.append((panel_node, None, panel_label))
        else:
            panel_sources = [
                (node, child, "") for child in node.descendants()
                if child.tag in {"img", "svg", "object", "embed"}
                and self._nearest_figure(child) is node
            ]
        for index, (panel_node, media_node, panel_caption) in enumerate(panel_sources):
            asset_id = (
                self._asset_for_node(media_node, figure_id=figure_id)
                if media_node is not None else None
            )
            panel_label = panel_caption or (
                media_node.attrs.get("alt", "").strip() if media_node is not None else ""
            ) or chr(ord("a") + index)
            panels.append({
                "panel_id": make_id("panel", self.fingerprint, figure_id, index),
                "label": panel_label,
                "asset_id": asset_id,
                "source_locator": html_locator(
                    self.fingerprint, dom_path(media_node or panel_node),
                ),
            })
        status = "complete" if panels and all(panel["asset_id"] for panel in panels) else "degraded"
        if status != "complete":
            self.reasons.append("html_figure_media_incomplete")
        self.figures.append({
            "figure_id": figure_id,
            "label": label,
            "caption": caption,
            "reading_order": self._order,
            "block_id": block_id,
            "panels": panels,
            "status": status,
            "source_locator": html_locator(self.fingerprint, dom_path(node), exact=caption or None),
        })

    def _add_table(self, table: HtmlNode, *, wrapper: HtmlNode | None = None) -> None:
        owner = wrapper or table
        caption_node = self._caption_node(owner)
        caption = caption_node.text() if caption_node else ""
        match = _TABLE_LABEL.search(caption)
        label = f"Table {match.group(1)}" if match else f"Table {len(self.tables) + 1}"
        table_id = make_id("tbl", self.fingerprint, dom_path(table), label)
        block_id = self._add_block("table", caption, table, table_id=table_id)
        if caption_node and caption:
            self._add_block("caption", caption, caption_node, parent_id=block_id)
        cells: list[dict[str, Any]] = []
        rows = list(table.descendants(lambda child: child.tag == "tr"))
        for row_index, row in enumerate(rows):
            column = 0
            for cell in (
                child for child in row.children
                if isinstance(child, HtmlNode) and child.tag in {"th", "td"}
            ):
                text = cell.text()
                rowspan = self._positive_span(cell.attrs.get("rowspan"))
                colspan = self._positive_span(cell.attrs.get("colspan"))
                cell_id = make_id("cell", self.fingerprint, table_id, dom_path(cell))
                locator = html_locator(self.fingerprint, dom_path(cell), exact=text or None)
                cell_block_id = self._add_block(
                    "table_cell", text, cell, parent_id=block_id,
                    table_id=table_id, row=row_index, column=column,
                    rowspan=rowspan, colspan=colspan,
                )
                cells.append({
                    "cell_id": cell_id,
                    "block_id": cell_block_id,
                    "row": row_index,
                    "column": column,
                    "rowspan": rowspan,
                    "colspan": colspan,
                    "role": "header" if cell.tag == "th" else "data",
                    "text": text,
                    "source_locator": locator,
                })
                column += colspan
        status = "complete" if cells else "degraded"
        if not cells:
            self.reasons.append("html_table_empty")
        self.tables.append({
            "table_id": table_id,
            "label": label,
            "caption": caption,
            "reading_order": self._order,
            "block_id": block_id,
            "cells": cells,
            "status": status,
            "source_locator": html_locator(self.fingerprint, dom_path(owner), exact=caption or None),
        })

    @staticmethod
    def _positive_span(value: str | None) -> int:
        try:
            parsed = int(value or 1)
        except ValueError:
            return 1
        return max(1, parsed)

    def _asset_for_node(self, node: HtmlNode, *, figure_id: str) -> str | None:
        existing = self._asset_by_node.get(id(node))
        if existing:
            return existing
        reference = (
            node.attrs.get("src") or node.attrs.get("data")
            or node.attrs.get("srcset", "").split(",", 1)[0].strip().split(" ", 1)[0]
        )
        if node.tag == "svg" and not reference:
            reference = dom_path(node)
            state = "embedded"
            mime_type = "image/svg+xml"
            sha256 = "sha256:" + hashlib.sha256(node.text().encode("utf-8")).hexdigest()
            size_bytes = None
        elif not reference:
            self.reasons.append("html_asset_reference_missing")
            return None
        else:
            parsed = urlparse(reference)
            mime_type = mimetypes.guess_type(parsed.path)[0] or "application/octet-stream"
            sha256 = None
            size_bytes = None
            if parsed.scheme == "data":
                state = "embedded"
                sha256 = "sha256:" + hashlib.sha256(reference.encode("utf-8")).hexdigest()
            elif parsed.scheme in {"http", "https"}:
                state = "remote"
                self.reasons.append("html_asset_remote")
            else:
                candidate = (self.job_dir / parsed.path).resolve()
                if candidate.is_relative_to(self.job_dir.resolve()) and candidate.is_file():
                    state = "available"
                    sha256 = sha256_fingerprint(candidate)
                    size_bytes = candidate.stat().st_size
                else:
                    state = "missing"
                    self.reasons.append("html_asset_missing")
        asset_id = make_id("asset", self.fingerprint, dom_path(node), reference)
        self.assets.append({
            "asset_id": asset_id,
            "kind": "image",
            "path": reference,
            "mime_type": mime_type,
            "sha256": sha256,
            "size_bytes": size_bytes,
            "state": state,
            "alt": node.attrs.get("alt", ""),
            "figure_id": figure_id,
            "source_locator": html_locator(self.fingerprint, dom_path(node)),
        })
        self._asset_by_node[id(node)] = asset_id
        return asset_id

    def _collect_references(self) -> None:
        unsafe_found = False
        for node in _nodes(self.root, "a"):
            href = node.attrs.get("href", "").strip()
            if not href:
                continue
            parsed = urlparse(href)
            target = href
            safe = href.startswith("#") or parsed.scheme in {"http", "https", "mailto"}
            if not safe and not parsed.scheme:
                resolved = urljoin(str(self.job.get("url") or ""), href)
                resolved_scheme = urlparse(resolved).scheme
                if resolved_scheme in {"http", "https"}:
                    target = resolved
                    safe = True
            if not safe:
                unsafe_found = True
                continue
            source_block_id = None
            current: HtmlNode | None = node
            while current is not None and source_block_id is None:
                source_block_id = self._block_by_node.get(id(current))
                current = current.parent
            kind = "citation" if _class_contains(node, "bib", "cite", "ref") else (
                "internal" if href.startswith("#") else "external"
            )
            self.references.append({
                "reference_id": make_id("ref", self.fingerprint, dom_path(node), target),
                "kind": kind,
                "target": target,
                "label": node.text(),
                "source_block_id": source_block_id,
                "source_locator": html_locator(self.fingerprint, dom_path(node), exact=node.text() or None),
            })
        if unsafe_found:
            self.reasons.append("html_unsafe_reference_ignored")


def parse_scholarly_html(
    job_dir: Path,
    job: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """解析 input/source.html，不写文件且不改写原始来源。"""
    return ScholarlyHtmlAdapter(job_dir, job).parse()
