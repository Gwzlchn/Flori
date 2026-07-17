"""定义 Document Model、翻译对齐和质量报告的运行时契约。"""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from .document_registry import (
    SOURCE_PROFILE_SPECS,
    validate_document_kind,
)


DOCUMENT_SCHEMA_VERSION = 2
QUALITY_SCHEMA_VERSION = 1
TRANSLATION_SCHEMA_VERSION = 2
QUALITY_STATUSES = frozenset({"complete", "degraded", "rejected"})
BLOCK_KINDS = frozenset({
    "title", "heading", "abstract", "paragraph", "list", "list_item",
    "quote", "code", "formula", "figure", "caption", "table",
    "table_cell", "footnote", "theorem", "proof", "algorithm", "appendix",
    "callout", "embed",
})
CLASSIFICATION_METHODS = frozenset({"source", "metadata", "classifier", "user"})
TABLE_CELL_ROLES = frozenset({"column_header", "row_header", "data"})
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class DocumentContractError(ValueError):
    """Document artifact 不满足共享真相源约束。"""


@dataclass(frozen=True)
class DocumentAdapterInput:
    """adapter 的只读输入；原始 source 不允许原地改写。"""

    job_id: str
    document_kind: str
    source_profile: str
    source_fingerprint: str
    source_path: str
    source_url: str | None = None


class DocumentAdapter(Protocol):
    """所有 HTML/PDF/OCR adapter 必须产相同 Document 与 Quality 契约。"""

    def parse(self, context: DocumentAdapterInput) -> tuple[dict[str, Any], dict[str, Any]]:
        ...


def canonical_json(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def stable_id(prefix: str, *parts: str) -> str:
    """以来源身份和结构位置生成跨重跑稳定、跨来源不碰撞的 id。"""
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:20]
    return f"{prefix}_{digest}"


def _require_mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise DocumentContractError(f"{field} must be an object")
    return value


def _require_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DocumentContractError(f"{field} must be a non-empty string")
    return value


def _require_id(value: object, field: str) -> str:
    parsed = _require_text(value, field)
    if not _ID_RE.fullmatch(parsed):
        raise DocumentContractError(f"{field} has invalid identity syntax")
    return parsed


def _validate_locator(
    locator: object,
    field: str,
    sources: Mapping[str, Mapping[str, Any]],
) -> None:
    item = _require_mapping(locator, field)
    html = item.get("html")
    pdf = item.get("pdf")
    if html is None and pdf is None:
        raise DocumentContractError(f"{field} must contain html or pdf locator")
    if html is not None:
        html_item = _require_mapping(html, f"{field}.html")
        _validate_locator_source(html_item, f"{field}.html", sources, "text/html")
        _require_text(html_item.get("dom_path"), f"{field}.html.dom_path")
        exact = html_item.get("exact")
        if exact is not None and not isinstance(exact, str):
            raise DocumentContractError(f"{field}.html.exact must be text")
    if pdf is not None:
        pdf_item = _require_mapping(pdf, f"{field}.pdf")
        _validate_locator_source(pdf_item, f"{field}.pdf", sources, "application/pdf")
        page = pdf_item.get("page")
        if type(page) is not int or page < 1:
            raise DocumentContractError(f"{field}.pdf.page must be a positive integer")
        bboxes = pdf_item.get("bboxes", [])
        if not isinstance(bboxes, list):
            raise DocumentContractError(f"{field}.pdf.bboxes must be a list")
        for bbox in bboxes:
            if (
                not isinstance(bbox, list) or len(bbox) != 4
                or any(type(value) not in (int, float) for value in bbox)
            ):
                raise DocumentContractError(f"{field}.pdf.bboxes contains invalid bbox")
        confidence = pdf_item.get("ocr_confidence")
        if confidence is not None and (
            type(confidence) not in (int, float) or not 0 <= confidence <= 1
        ):
            raise DocumentContractError(f"{field}.pdf.ocr_confidence is invalid")
    crosswalk = item.get("crosswalk")
    if crosswalk is not None:
        crosswalk_item = _require_mapping(crosswalk, f"{field}.crosswalk")
        confidence = crosswalk_item.get("confidence")
        if type(confidence) not in (int, float) or not 0 <= confidence <= 1:
            raise DocumentContractError(f"{field}.crosswalk.confidence is invalid")
        if crosswalk_item.get("status") not in {"matched", "ambiguous", "unmatched"}:
            raise DocumentContractError(f"{field}.crosswalk.status is invalid")


def _validate_locator_source(
    locator: Mapping[str, Any],
    field: str,
    sources: Mapping[str, Mapping[str, Any]],
    expected_mime: str,
) -> None:
    source_id = _require_id(locator.get("source_id"), f"{field}.source_id")
    fingerprint = _require_text(
        locator.get("source_fingerprint"), f"{field}.source_fingerprint",
    )
    source = sources.get(source_id)
    if source is None:
        raise DocumentContractError(f"{field} references a missing source")
    if fingerprint != source.get("fingerprint"):
        raise DocumentContractError(f"{field}.source_fingerprint does not match source")
    if source.get("mime_type") != expected_mime:
        raise DocumentContractError(f"{field}.source_id has incompatible media type")


def document_sources(document: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    value = document.get("sources")
    if not isinstance(value, list):
        raise DocumentContractError("document.sources must be a list")
    return [_require_mapping(item, "document.source") for item in value]


def primary_document_source(document: Mapping[str, Any]) -> Mapping[str, Any]:
    primary_id = _require_id(
        document.get("primary_source_id"), "document.primary_source_id",
    )
    source = next(
        (item for item in document_sources(document) if item.get("source_id") == primary_id),
        None,
    )
    if source is None:
        raise DocumentContractError("document.primary_source_id is missing")
    return source


def _asset_artifact(asset: Mapping[str, Any] | None) -> str | None:
    if asset is None:
        return None
    state = str(asset.get("state") or asset.get("status") or "")
    if state not in {"available", "available_local"}:
        return None
    value = asset.get("local_path") or asset.get("path")
    if not isinstance(value, str) or not value or value.startswith(("/", "\\")):
        return None
    if "\x00" in value or ".." in value.replace("\\", "/").split("/"):
        return None
    return value.replace("\\", "/")


def _flatten_table_rows(rows: object) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        return []
    result: list[dict[str, Any]] = []
    occupied: set[tuple[int, int]] = set()
    for row_index, row in enumerate(rows):
        if not isinstance(row, Mapping) or not isinstance(row.get("cells"), list):
            continue
        column = 0
        for raw in row["cells"]:
            if not isinstance(raw, Mapping):
                continue
            while (row_index, column) in occupied:
                column += 1
            rowspan = raw.get("rowspan") if type(raw.get("rowspan")) is int else 1
            colspan = raw.get("colspan") if type(raw.get("colspan")) is int else 1
            rowspan = max(1, rowspan)
            colspan = max(1, colspan)
            for row_offset in range(rowspan):
                for column_offset in range(colspan):
                    occupied.add((row_index + row_offset, column + column_offset))
            is_header = raw.get("kind") == "header" or raw.get("role") == "header"
            role = (
                "column_header" if is_header and row.get("section") == "header"
                else "row_header" if is_header else "data"
            )
            result.append({
                "cell_id": raw.get("cell_id"),
                "block_id": raw.get("block_id"),
                "row": row_index,
                "col": column,
                "rowspan": rowspan,
                "colspan": colspan,
                "role": role,
                "text": str(raw.get("text") or ""),
                "source_locator": raw.get("source_locator") or raw.get("locator"),
            })
            column += colspan
    return result


def canonicalize_document_metadata(value: Mapping[str, Any]) -> dict[str, Any]:
    """把不同 adapter 的来源 metadata 收敛为标题、作者和机构关系模型。"""
    document = deepcopy(dict(value))
    raw = document.get("metadata")
    metadata = dict(raw) if isinstance(raw, Mapping) else {}
    titles = dict(metadata.get("titles")) if isinstance(metadata.get("titles"), Mapping) else {}
    original_title = str(
        titles.get("original") or metadata.get("original_title")
        or metadata.get("title") or ""
    ).strip()
    zh_title = str(titles.get("zh") or "").strip() or None
    try:
        fingerprint = str(primary_document_source(document).get("fingerprint") or "")
    except DocumentContractError:
        fingerprint = ""

    affiliation_names: list[str] = []
    raw_affiliations = metadata.get("affiliations") or metadata.get("institutions") or []
    if isinstance(raw_affiliations, list):
        for item in raw_affiliations:
            name = str(item.get("name") if isinstance(item, Mapping) else item).strip()
            if name and name not in affiliation_names:
                affiliation_names.append(name)
    raw_authors = metadata.get("authors") if isinstance(metadata.get("authors"), list) else []
    for item in raw_authors:
        if not isinstance(item, Mapping):
            continue
        values = item.get("affiliations") if isinstance(item.get("affiliations"), list) else []
        for affiliation in values:
            name = str(
                affiliation.get("name") if isinstance(affiliation, Mapping) else affiliation
            ).strip()
            if name and name not in affiliation_names:
                affiliation_names.append(name)
    affiliation_ids = {
        name: stable_id("aff", fingerprint, name) for name in affiliation_names
    }
    affiliations = [
        {"affiliation_id": affiliation_ids[name], "name": name, "name_zh": None}
        for name in affiliation_names
    ]

    raw_author_notes = metadata.get("author_notes")
    note_texts: list[str] = []
    if isinstance(raw_author_notes, list):
        for item in raw_author_notes:
            text = str(item.get("text") if isinstance(item, Mapping) else item).strip()
            if text and text not in note_texts:
                note_texts.append(text)
    for item in raw_authors:
        if not isinstance(item, Mapping) or not isinstance(item.get("notes"), list):
            continue
        for note in item["notes"]:
            text = str(note).strip()
            if text and text not in note_texts:
                note_texts.append(text)
    author_notes = [
        {"note_id": stable_id("author_note", fingerprint, str(index), text), "text": text}
        for index, text in enumerate(note_texts)
    ]
    note_ids = {item["text"]: item["note_id"] for item in author_notes}

    authors: list[dict[str, Any]] = []
    for index, item in enumerate(raw_authors):
        source = item if isinstance(item, Mapping) else {"name": item}
        name = str(source.get("name") or source.get("display_name") or "").strip()
        if not name:
            continue
        author_affiliations = source.get("affiliations")
        names = []
        if isinstance(author_affiliations, list):
            for affiliation in author_affiliations:
                affiliation_name = str(
                    affiliation.get("name") if isinstance(affiliation, Mapping) else affiliation
                ).strip()
                if affiliation_name and affiliation_name not in names:
                    names.append(affiliation_name)
        emails = source.get("emails") if isinstance(source.get("emails"), list) else []
        notes = source.get("notes") if isinstance(source.get("notes"), list) else []
        normalized_notes = [str(value).strip() for value in notes if str(value).strip()]
        authors.append({
            "author_id": stable_id("author", fingerprint, str(index), name),
            "order": index,
            "name": name,
            "affiliation_ids": [affiliation_ids[value] for value in names],
            "affiliations": names,
            "emails": [str(value).strip() for value in emails if str(value).strip()],
            "notes": normalized_notes,
            "note_refs": [note_ids[value] for value in normalized_notes],
            "equal_contribution": bool(source.get("equal_contribution")) or any(
                "equal contribution" in value.lower()
                or "contributed equally" in value.lower()
                or "同等贡献" in value
                for value in normalized_notes
            ),
        })

    result = dict(metadata)
    for legacy in ("title", "original_title", "institutions", "language"):
        result.pop(legacy, None)
    result.update({
        "titles": {"original": original_title, "zh": zh_title},
        "authors": authors,
        "affiliations": affiliations,
        "author_notes": author_notes,
        "abstract": str(metadata.get("abstract") or "").strip(),
        "keywords": list(metadata.get("keywords") or metadata.get("tags") or []),
        "lang": str(metadata.get("lang") or metadata.get("language") or "").strip() or None,
        "license": str(metadata.get("license") or "").strip(),
        "source_license": str(
            metadata.get("source_license") or metadata.get("license") or ""
        ).strip(),
        "rights_notices": [
            str(item).strip() for item in metadata.get("rights_notices", [])
            if str(item).strip()
        ] if isinstance(metadata.get("rights_notices", []), list) else [],
        "identifiers": dict(metadata.get("identifiers") or {}),
    })
    result.pop("tags", None)
    document["metadata"] = result
    return document


def canonicalize_document_visuals(value: Mapping[str, Any]) -> dict[str, Any]:
    """把 adapter 内部表示收敛为唯一 Figure/Table wire schema。"""
    document = deepcopy(dict(value))
    assets = {
        str(item.get("asset_id")): item
        for item in document.get("assets", [])
        if isinstance(item, Mapping) and isinstance(item.get("asset_id"), str)
    }
    block_order = {
        str(item.get("block_id")): item.get("order")
        for item in document.get("blocks", []) if isinstance(item, Mapping)
    }

    figures: list[dict[str, Any]] = []
    for index, raw in enumerate(document.get("figures", [])):
        if not isinstance(raw, Mapping):
            figures.append(raw)
            continue
        if isinstance(raw.get("media"), list):
            media = deepcopy(raw["media"])
        else:
            items = raw.get("panels") if isinstance(raw.get("panels"), list) else [
                {"asset_id": asset_id} for asset_id in raw.get("asset_ids", [])
            ]
            media = []
            for media_index, item in enumerate(items):
                if not isinstance(item, Mapping):
                    continue
                asset_id = str(item.get("asset_id") or "")
                asset = assets.get(asset_id)
                media.append({
                    "media_id": item.get("media_id") or item.get("panel_id")
                    or stable_id("media", str(raw.get("figure_id")), str(media_index)),
                    "role": item.get("role") or item.get("label"),
                    "asset_id": asset_id or None,
                    "artifact": item.get("artifact") or _asset_artifact(asset),
                    "alt": item.get("alt") or (asset or {}).get("alt"),
                    "width": item.get("width") or (asset or {}).get("width"),
                    "height": item.get("height") or (asset or {}).get("height"),
                    "source_locator": item.get("source_locator")
                    or (asset or {}).get("source_locator"),
                })
        reasons = raw.get("quality_reasons") or raw.get("reasons") or []
        figures.append({
            "figure_id": raw.get("figure_id"),
            "block_id": raw.get("block_id"),
            "label": raw.get("label"),
            "caption": raw.get("caption", ""),
            "order": raw.get("order", raw.get("reading_order", block_order.get(str(raw.get("block_id")), index))),
            "media": media,
            "extraction": deepcopy(raw.get("extraction")) if isinstance(raw.get("extraction"), Mapping) else {
                "status": raw.get("quality_status") or raw.get("status") or "complete",
                "reasons": list(reasons) if isinstance(reasons, list) else [],
            },
            "source_locator": raw.get("source_locator"),
        })
    document["figures"] = figures

    tables: list[dict[str, Any]] = []
    for index, raw in enumerate(document.get("tables", [])):
        if not isinstance(raw, Mapping):
            tables.append(raw)
            continue
        if isinstance(raw.get("cells"), list):
            cells = []
            for cell in raw["cells"]:
                if not isinstance(cell, Mapping):
                    cells.append(cell)
                    continue
                role = cell.get("role")
                cells.append({
                    "cell_id": cell.get("cell_id"),
                    "block_id": cell.get("block_id"),
                    "row": cell.get("row"),
                    "col": cell.get("col", cell.get("column")),
                    "rowspan": cell.get("rowspan", 1),
                    "colspan": cell.get("colspan", 1),
                    "role": "column_header" if role == "header" else role or "data",
                    "text": str(cell.get("text") or ""),
                    "source_locator": cell.get("source_locator") or cell.get("locator"),
                })
        else:
            cells = _flatten_table_rows(raw.get("rows"))
        representations = deepcopy(raw.get("representations")) if isinstance(raw.get("representations"), list) else []
        if raw.get("source_crop") is not None and not representations:
            representations.append({
                "kind": "source_crop",
                "artifact": None,
                "source_locator": raw.get("source_locator"),
            })
        reasons = raw.get("quality_reasons") or raw.get("reasons") or []
        tables.append({
            "table_id": raw.get("table_id"),
            "block_id": raw.get("block_id"),
            "label": raw.get("label"),
            "caption": raw.get("caption", ""),
            "order": raw.get("order", raw.get("reading_order", block_order.get(str(raw.get("block_id")), index))),
            "cells": cells,
            "representations": representations,
            "footnotes": list(raw.get("footnotes") or []),
            "extraction": deepcopy(raw.get("extraction")) if isinstance(raw.get("extraction"), Mapping) else {
                "status": raw.get("quality_status") or raw.get("status") or "complete",
                "reasons": list(reasons) if isinstance(reasons, list) else [],
            },
            "source_locator": raw.get("source_locator"),
        })
    document["tables"] = tables
    return document


def canonicalize_document(value: Mapping[str, Any]) -> dict[str, Any]:
    return canonicalize_document_visuals(canonicalize_document_metadata(value))


def validate_document(document: object, *, expected_job_id: str | None = None) -> dict[str, Any]:
    """校验完整 Document Model；失败时不允许下游消费半成品。"""
    root = dict(_require_mapping(document, "document"))
    if root.get("schema_version") != DOCUMENT_SCHEMA_VERSION:
        raise DocumentContractError("unsupported document schema_version")
    job_id = _require_text(root.get("job_id"), "document.job_id")
    if expected_job_id is not None and job_id != expected_job_id:
        raise DocumentContractError("document belongs to another job")
    if root.get("content_type") != "document":
        raise DocumentContractError("document.content_type must be document")
    validate_document_kind(root.get("document_kind"))
    classification = _require_mapping(root.get("classification"), "document.classification")
    if classification.get("method") not in CLASSIFICATION_METHODS:
        raise DocumentContractError("document.classification.method is invalid")
    confidence = classification.get("confidence")
    if type(confidence) not in (int, float) or not 0 <= confidence <= 1:
        raise DocumentContractError("document.classification.confidence must be within 0..1")
    profile = _require_text(root.get("source_profile"), "document.source_profile")
    if profile not in SOURCE_PROFILE_SPECS:
        raise DocumentContractError("document.source_profile is not registered")
    capabilities = root.get("capabilities")
    if not isinstance(capabilities, list):
        raise DocumentContractError("document.capabilities must be a list")
    source_items = document_sources(root)
    if not source_items:
        raise DocumentContractError("document.sources must not be empty")
    sources: dict[str, Mapping[str, Any]] = {}
    for index, source in enumerate(source_items):
        source_id = _require_id(source.get("source_id"), f"document.sources[{index}].source_id")
        if source_id in sources:
            raise DocumentContractError(f"duplicate source_id: {source_id}")
        fingerprint = _require_text(
            source.get("fingerprint"), f"document.sources[{index}].fingerprint",
        )
        if not fingerprint.startswith("sha256:") or len(fingerprint) != 71:
            raise DocumentContractError(
                f"document.sources[{index}].fingerprint must be sha256"
            )
        path = _require_text(source.get("path"), f"document.sources[{index}].path")
        if path.startswith(("/", "\\")) or ".." in path.replace("\\", "/").split("/"):
            raise DocumentContractError(f"document.sources[{index}].path is invalid")
        if source.get("mime_type") not in {"text/html", "application/pdf"}:
            raise DocumentContractError(f"document.sources[{index}].mime_type is invalid")
        source_profile = _require_text(
            source.get("source_profile"), f"document.sources[{index}].source_profile",
        )
        if source_profile not in SOURCE_PROFILE_SPECS:
            raise DocumentContractError(
                f"document.sources[{index}].source_profile is not registered"
            )
        source_capabilities = source.get("capabilities")
        allowed_source = set(SOURCE_PROFILE_SPECS[source_profile]["capabilities"])
        if (
            not isinstance(source_capabilities, list)
            or not set(source_capabilities) <= allowed_source
        ):
            raise DocumentContractError(
                f"document.sources[{index}].capabilities exceed source profile"
            )
        if source.get("immutable") is not True:
            raise DocumentContractError(f"document.sources[{index}] must be immutable")
        sources[source_id] = source
    primary_source = primary_document_source(root)
    if primary_source.get("source_id") not in sources:
        raise DocumentContractError("document.primary_source_id is invalid")
    if primary_source.get("source_profile") != profile:
        raise DocumentContractError("document.source_profile must match primary source")
    allowed = {
        capability
        for source in source_items
        for capability in source.get("capabilities", [])
    }
    if set(capabilities) != allowed:
        raise DocumentContractError("document.capabilities must equal source capability union")
    metadata = _require_mapping(root.get("metadata"), "document.metadata")
    titles = _require_mapping(metadata.get("titles"), "document.metadata.titles")
    if not isinstance(titles.get("original"), str):
        raise DocumentContractError("document.metadata.titles.original must be text")
    if titles.get("zh") is not None and not isinstance(titles.get("zh"), str):
        raise DocumentContractError("document.metadata.titles.zh must be text or null")
    authors = metadata.get("authors")
    if not isinstance(authors, list):
        raise DocumentContractError("document.metadata.authors must be a list")
    for index, author in enumerate(authors):
        author_item = _require_mapping(author, f"document.metadata.authors[{index}]")
        _require_id(author_item.get("author_id"), f"document.metadata.authors[{index}].author_id")
        _require_text(author_item.get("name"), f"document.metadata.authors[{index}].name")
        if type(author_item.get("order")) is not int or author_item["order"] < 0:
            raise DocumentContractError(f"document.metadata.authors[{index}].order is invalid")
        for field in ("affiliation_ids", "affiliations", "emails", "notes", "note_refs"):
            if not isinstance(author_item.get(field), list) or any(
                not isinstance(item, str) for item in author_item[field]
            ):
                raise DocumentContractError(
                    f"document.metadata.authors[{index}].{field} must be a text list"
                )
        if type(author_item.get("equal_contribution")) is not bool:
            raise DocumentContractError(
                f"document.metadata.authors[{index}].equal_contribution must be boolean"
            )
    affiliations = metadata.get("affiliations")
    if not isinstance(affiliations, list):
        raise DocumentContractError("document.metadata.affiliations must be a list")
    known_affiliations: set[str] = set()
    for index, affiliation in enumerate(affiliations):
        item = _require_mapping(affiliation, f"document.metadata.affiliations[{index}]")
        known_affiliations.add(_require_id(
            item.get("affiliation_id"),
            f"document.metadata.affiliations[{index}].affiliation_id",
        ))
        _require_text(item.get("name"), f"document.metadata.affiliations[{index}].name")
    for author in authors:
        missing_affiliations = set(author["affiliation_ids"]) - known_affiliations
        if missing_affiliations:
            raise DocumentContractError("document author references missing affiliation")
    author_notes = metadata.get("author_notes")
    if not isinstance(author_notes, list):
        raise DocumentContractError("document.metadata.author_notes must be a list")
    known_notes: set[str] = set()
    for index, raw_note in enumerate(author_notes):
        note = _require_mapping(raw_note, f"document.metadata.author_notes[{index}]")
        known_notes.add(_require_id(
            note.get("note_id"), f"document.metadata.author_notes[{index}].note_id",
        ))
        _require_text(note.get("text"), f"document.metadata.author_notes[{index}].text")
    if any(set(author["note_refs"]) - known_notes for author in authors):
        raise DocumentContractError("document author references missing author note")
    if not isinstance(metadata.get("abstract"), str):
        raise DocumentContractError("document.metadata.abstract must be text")
    if not isinstance(metadata.get("keywords"), list):
        raise DocumentContractError("document.metadata.keywords must be a list")
    if not isinstance(metadata.get("identifiers"), Mapping):
        raise DocumentContractError("document.metadata.identifiers must be an object")
    if not isinstance(metadata.get("rights_notices"), list) or any(
        not isinstance(item, str) for item in metadata["rights_notices"]
    ):
        raise DocumentContractError("document.metadata.rights_notices must be a text list")

    blocks = root.get("blocks")
    if not isinstance(blocks, list):
        raise DocumentContractError("document.blocks must be a list")
    ids: set[str] = set()
    parents: list[str] = []
    for index, raw in enumerate(blocks):
        block = _require_mapping(raw, f"document.blocks[{index}]")
        block_id = _require_id(block.get("block_id"), f"document.blocks[{index}].block_id")
        if block_id in ids:
            raise DocumentContractError(f"duplicate block_id: {block_id}")
        ids.add(block_id)
        parent = block.get("parent_id")
        if parent is not None:
            parents.append(_require_id(parent, f"document.blocks[{index}].parent_id"))
        if block.get("kind") not in BLOCK_KINDS:
            raise DocumentContractError(f"document.blocks[{index}].kind is invalid")
        if type(block.get("order")) is not int or block["order"] < 0:
            raise DocumentContractError(f"document.blocks[{index}].order is invalid")
        _validate_locator(
            block.get("locator"), f"document.blocks[{index}].locator", sources,
        )
    missing = set(parents) - ids
    if missing:
        raise DocumentContractError(f"block parent is missing: {sorted(missing)[0]}")

    for field, id_field in (("figures", "figure_id"), ("tables", "table_id")):
        items = root.get(field)
        if not isinstance(items, list):
            raise DocumentContractError(f"document.{field} must be a list")
        visual_ids: set[str] = set()
        for index, raw in enumerate(items):
            item = _require_mapping(raw, f"document.{field}[{index}]")
            visual_id = _require_id(item.get(id_field), f"document.{field}[{index}].{id_field}")
            if visual_id in visual_ids:
                raise DocumentContractError(f"duplicate visual id: {visual_id}")
            visual_ids.add(visual_id)
            block_id = _require_id(
                item.get("block_id"), f"document.{field}[{index}].block_id",
            )
            if block_id not in ids:
                raise DocumentContractError(
                    f"document.{field}[{index}].block_id is missing"
                )
            _require_text(item.get("label"), f"document.{field}[{index}].label")
            if not isinstance(item.get("caption", ""), str):
                raise DocumentContractError(f"document.{field}[{index}].caption must be text")
            if type(item.get("order")) is not int or item["order"] < 0:
                raise DocumentContractError(f"document.{field}[{index}].order is invalid")
            extraction = _require_mapping(
                item.get("extraction"), f"document.{field}[{index}].extraction",
            )
            if extraction.get("status") not in QUALITY_STATUSES:
                raise DocumentContractError(
                    f"document.{field}[{index}].extraction.status is invalid"
                )
            reasons = extraction.get("reasons")
            if not isinstance(reasons, list) or any(not isinstance(reason, str) for reason in reasons):
                raise DocumentContractError(
                    f"document.{field}[{index}].extraction.reasons is invalid"
                )
            _validate_locator(
                item.get("source_locator"),
                f"document.{field}[{index}].source_locator",
                sources,
            )
            if field == "figures":
                media = item.get("media")
                if not isinstance(media, list):
                    raise DocumentContractError(f"document.{field}[{index}].media must be a list")
                media_ids: set[str] = set()
                for media_index, raw_media in enumerate(media):
                    media_item = _require_mapping(
                        raw_media, f"document.figures[{index}].media[{media_index}]",
                    )
                    media_id = _require_id(
                        media_item.get("media_id"),
                        f"document.figures[{index}].media[{media_index}].media_id",
                    )
                    if media_id in media_ids:
                        raise DocumentContractError(f"duplicate media id: {media_id}")
                    media_ids.add(media_id)
                    artifact = media_item.get("artifact")
                    if artifact is not None and (
                        not isinstance(artifact, str) or not artifact
                        or artifact.startswith(("/", "\\")) or "\x00" in artifact
                        or ".." in artifact.replace("\\", "/").split("/")
                    ):
                        raise DocumentContractError(
                            f"document.figures[{index}].media[{media_index}].artifact is invalid"
                        )
            else:
                cells = item.get("cells")
                if not isinstance(cells, list):
                    raise DocumentContractError(f"document.{field}[{index}].cells must be a list")
                cell_ids: set[str] = set()
                for cell_index, raw_cell in enumerate(cells):
                    cell = _require_mapping(
                        raw_cell, f"document.tables[{index}].cells[{cell_index}]",
                    )
                    cell_id = _require_id(
                        cell.get("cell_id"),
                        f"document.tables[{index}].cells[{cell_index}].cell_id",
                    )
                    if cell_id in cell_ids:
                        raise DocumentContractError(f"duplicate table cell id: {cell_id}")
                    cell_ids.add(cell_id)
                    cell_block_id = cell.get("block_id")
                    if cell_block_id is not None and _require_id(
                        cell_block_id,
                        f"document.tables[{index}].cells[{cell_index}].block_id",
                    ) not in ids:
                        raise DocumentContractError(
                            f"document.tables[{index}].cells[{cell_index}].block_id is missing"
                        )
                    for coordinate in ("row", "col"):
                        if type(cell.get(coordinate)) is not int or cell[coordinate] < 0:
                            raise DocumentContractError(
                                f"document.tables[{index}].cells[{cell_index}].{coordinate} is invalid"
                            )
                    for span in ("rowspan", "colspan"):
                        if type(cell.get(span)) is not int or cell[span] < 1:
                            raise DocumentContractError(
                                f"document.tables[{index}].cells[{cell_index}].{span} is invalid"
                            )
                    if cell.get("role") not in TABLE_CELL_ROLES:
                        raise DocumentContractError(
                            f"document.tables[{index}].cells[{cell_index}].role is invalid"
                        )
                    if not isinstance(cell.get("text"), str):
                        raise DocumentContractError(
                            f"document.tables[{index}].cells[{cell_index}].text must be text"
                        )
                representations = item.get("representations")
                if not isinstance(representations, list):
                    raise DocumentContractError(
                        f"document.{field}[{index}].representations must be a list"
                    )
                for representation in representations:
                    entry = _require_mapping(representation, "document.table.representation")
                    if entry.get("kind") not in {"structured", "source_crop"}:
                        raise DocumentContractError("document.table.representation.kind is invalid")
                if not isinstance(item.get("footnotes"), list):
                    raise DocumentContractError(f"document.{field}[{index}].footnotes must be a list")
    for field in ("references", "assets"):
        if not isinstance(root.get(field), list):
            raise DocumentContractError(f"document.{field} must be a list")
    return root


def validate_quality(report: object, *, expected_job_id: str | None = None) -> dict[str, Any]:
    root = dict(_require_mapping(report, "quality"))
    if root.get("schema_version") != QUALITY_SCHEMA_VERSION:
        raise DocumentContractError("unsupported quality schema_version")
    job_id = _require_text(root.get("job_id"), "quality.job_id")
    if expected_job_id is not None and job_id != expected_job_id:
        raise DocumentContractError("quality report belongs to another job")
    if root.get("status") not in QUALITY_STATUSES:
        raise DocumentContractError("quality.status is invalid")
    if not isinstance(root.get("reasons"), list) or any(
        not isinstance(reason, str) or not reason for reason in root["reasons"]
    ):
        raise DocumentContractError("quality.reasons must be a list of codes")
    if not isinstance(root.get("metrics"), dict):
        raise DocumentContractError("quality.metrics must be an object")
    if root["status"] != "complete" and not root["reasons"]:
        raise DocumentContractError("non-complete quality requires reasons")
    return root


def validate_translation(value: object, *, expected_job_id: str | None = None) -> dict[str, Any]:
    root = dict(_require_mapping(value, "translation"))
    if root.get("schema_version") != TRANSLATION_SCHEMA_VERSION:
        raise DocumentContractError("unsupported translation schema_version")
    job_id = _require_text(root.get("job_id"), "translation.job_id")
    if expected_job_id is not None and job_id != expected_job_id:
        raise DocumentContractError("translation belongs to another job")
    fingerprint = _require_text(root.get("source_fingerprint"), "translation.source_fingerprint")
    if not fingerprint.startswith("sha256:") or len(fingerprint) != 71:
        raise DocumentContractError("translation.source_fingerprint must be sha256")
    _require_text(root.get("source_lang"), "translation.source_lang")
    if root.get("target_lang") != "zh":
        raise DocumentContractError("translation.target_lang must be zh")
    if root.get("status") not in {"complete", "degraded"}:
        raise DocumentContractError("translation.status is invalid")
    coverage = _require_mapping(root.get("coverage"), "translation.coverage")
    for field in ("source_segments", "translated_segments", "passthrough_segments"):
        if type(coverage.get(field)) is not int or coverage[field] < 0:
            raise DocumentContractError(f"translation.coverage.{field} is invalid")
    segments = root.get("segments")
    if not isinstance(segments, list):
        raise DocumentContractError("translation.segments must be a list")
    seen: set[str] = set()
    covered_sources: set[str] = set()
    translated_count = 0
    passthrough_count = 0
    alignment_rows: list[tuple[str, list[str]]] = []
    source_occurrences: dict[str, int] = {}
    for index, raw in enumerate(segments):
        item = _require_mapping(raw, f"translation.segments[{index}]")
        translated_id = _require_id(
            item.get("translated_segment_id"),
            f"translation.segments[{index}].translated_segment_id",
        )
        if translated_id in seen:
            raise DocumentContractError(f"duplicate translated_segment_id: {translated_id}")
        seen.add(translated_id)
        source_ids = item.get("source_segment_ids")
        if not isinstance(source_ids, list) or not source_ids:
            raise DocumentContractError("translated segment requires source_segment_ids")
        for source_id in source_ids:
            parsed_source_id = _require_id(source_id, "translation.source_segment_id")
            covered_sources.add(parsed_source_id)
            source_occurrences[parsed_source_id] = source_occurrences.get(parsed_source_id, 0) + 1
        text = _require_text(item.get("text"), f"translation.segments[{index}].text")
        if item.get("kind") not in BLOCK_KINDS:
            raise DocumentContractError(f"translation.segments[{index}].kind is invalid")
        if item.get("transform_kind") not in {"translated", "passthrough"}:
            raise DocumentContractError(
                f"translation.segments[{index}].transform_kind is invalid"
            )
        if item["transform_kind"] == "translated":
            translated_count += 1
        else:
            passthrough_count += 1
        alignment_kind = item.get("alignment_kind")
        if alignment_kind not in {"one_to_one", "one_to_many", "many_to_one"}:
            raise DocumentContractError(
                f"translation.segments[{index}].alignment_kind is invalid"
            )
        if len(source_ids) > 1 and alignment_kind != "many_to_one":
            raise DocumentContractError(
                f"translation.segments[{index}] multi-source alignment is inconsistent"
            )
        alignment_rows.append((alignment_kind, list(source_ids)))
        source_ranges = item.get("source_ranges")
        if not isinstance(source_ranges, list) or {
            entry.get("source_segment_id")
            for entry in source_ranges if isinstance(entry, Mapping)
        } != set(source_ids):
            raise DocumentContractError(
                f"translation.segments[{index}].source_ranges do not cover sources"
            )
        for range_index, source_range in enumerate(source_ranges):
            _validate_text_range(
                source_range,
                f"translation.segments[{index}].source_ranges[{range_index}]",
            )
        translated_range = _require_mapping(
            item.get("translated_range"),
            f"translation.segments[{index}].translated_range",
        )
        _validate_text_range(
            translated_range, f"translation.segments[{index}].translated_range",
        )
        if (
            translated_range.get("start") != 0
            or translated_range.get("end") != len(text)
            or translated_range.get("exact") != text
        ):
            raise DocumentContractError(
                f"translation.segments[{index}].translated_range does not bind text"
            )
        for field in ("source_hash", "translated_hash"):
            digest = item.get(field)
            if not isinstance(digest, str) or not digest.startswith("sha256:") or len(digest) != 71:
                raise DocumentContractError(
                    f"translation.segments[{index}].{field} must be sha256"
                )
        tokens = item.get("protected_tokens")
        if not isinstance(tokens, list) or any(not isinstance(token, str) for token in tokens):
            raise DocumentContractError(
                f"translation.segments[{index}].protected_tokens must be a list"
            )
    for alignment_kind, source_ids in alignment_rows:
        if len(source_ids) > 1:
            if any(source_occurrences[source_id] != 1 for source_id in source_ids):
                raise DocumentContractError("many_to_one source cannot target another segment")
            continue
        occurrence_count = source_occurrences[source_ids[0]]
        expected_alignment = "one_to_many" if occurrence_count > 1 else "one_to_one"
        if alignment_kind != expected_alignment:
            raise DocumentContractError("translation alignment cardinality is inconsistent")
    if len(covered_sources) != coverage["source_segments"]:
        raise DocumentContractError("translation source coverage does not close")
    if translated_count != coverage["translated_segments"]:
        raise DocumentContractError("translation translated coverage does not close")
    if passthrough_count != coverage["passthrough_segments"]:
        raise DocumentContractError("translation passthrough coverage does not close")
    return root


def _validate_text_range(value: object, field: str) -> None:
    item = _require_mapping(value, field)
    start, end, exact = item.get("start"), item.get("end"), item.get("exact")
    if type(start) is not int or type(end) is not int or start < 0 or end <= start:
        raise DocumentContractError(f"{field} has invalid offsets")
    if not isinstance(exact, str) or len(exact) != end - start:
        raise DocumentContractError(f"{field}.exact does not match offsets")
