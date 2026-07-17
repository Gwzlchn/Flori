"""生成严格对齐的译文契约和可再生安全 HTML 阅读版。"""

from __future__ import annotations

import hashlib
import html
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from shared.document_contract import primary_document_source, stable_id


TRANSLATABLE_KINDS = frozenset({
    "title", "heading", "abstract", "paragraph", "list", "list_item",
    "quote", "caption", "table_cell", "footnote", "theorem", "proof",
    "algorithm", "appendix", "callout",
})
PASSTHROUGH_KINDS = frozenset({"code", "formula"})
_PROTECTED_RE = re.compile(
    r"(?:https?://\S+|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|"
    r"\$[^$\n]+\$|\\\([^\n]+?\\\)|\\\[[\s\S]+?\\\]|"
    r"\[[0-9,;\-–— ]+\]|(?:[+\-−]?\d+(?:[.,]\d+)*)\s*"
    r"(?:%|％|ms|s|GB|MB|KB|TB|Hz|kHz|MHz|GHz|B|K|M|×|x)?)",
    re.IGNORECASE,
)


def text_hash(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def protected_tokens(text: str) -> list[str]:
    """抽取译文不得改写的公式、数量、引用、地址和邮箱。"""
    return list(dict.fromkeys(match.group(0) for match in _PROTECTED_RE.finditer(text)))


def translation_units(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    """按 Document reading order 投影全部有文字的可译或直通 block。"""
    units: list[dict[str, Any]] = []
    for block in sorted(document["blocks"], key=lambda item: int(item["order"])):
        text = str(block.get("text") or "").strip()
        kind = str(block.get("kind") or "")
        if not text or kind not in TRANSLATABLE_KINDS | PASSTHROUGH_KINDS:
            continue
        units.append({
            "source_segment_id": str(block["block_id"]),
            "translated_segment_id": stable_id(
                "tr", str(primary_document_source(document)["fingerprint"]), str(block["block_id"]),
            ),
            "parent_id": block.get("parent_id"),
            "order": int(block["order"]),
            "kind": kind,
            "source_text": text,
            "protected_tokens": protected_tokens(text),
            "transform_kind": "translated" if kind in TRANSLATABLE_KINDS else "passthrough",
        })
    return units


def translation_batches(
    units: Sequence[Mapping[str, Any]], *, max_chars: int,
) -> list[list[dict[str, Any]]]:
    """只按完整 block 分批；单 block 超预算时显式拒绝，不截断来源。"""
    batches: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_chars = 0
    for raw in units:
        unit = dict(raw)
        if unit["transform_kind"] != "translated":
            continue
        size = len(str(unit["source_text"]))
        if size > max_chars:
            raise ValueError(
                f"document translation block exceeds {max_chars} chars: "
                f"{unit['source_segment_id']}"
            )
        if current and current_chars + size > max_chars:
            batches.append(current)
            current = []
            current_chars = 0
        current.append(unit)
        current_chars += size
    if current:
        batches.append(current)
    return batches


def validate_batch_response(
    batch: Sequence[Mapping[str, Any]], response: object,
) -> dict[str, str]:
    """响应必须与输入 id、数量和顺序完全一致，且冻结 token 不丢失。"""
    if not isinstance(response, Mapping) or set(response) != {"segments"}:
        raise ValueError("translation response must contain only segments")
    items = response["segments"]
    if not isinstance(items, list) or len(items) != len(batch):
        raise ValueError("translation response segment count mismatch")
    expected_ids = [str(item["source_segment_id"]) for item in batch]
    actual_ids = [
        item.get("id") if isinstance(item, Mapping) else None for item in items
    ]
    if actual_ids != expected_ids:
        raise ValueError("translation response segment order mismatch")
    translated: dict[str, str] = {}
    for source, item in zip(batch, items, strict=True):
        if not isinstance(item, Mapping) or set(item) != {"id", "text"}:
            raise ValueError("translation response item fields are invalid")
        text = item.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("translation response contains empty text")
        missing = [token for token in source["protected_tokens"] if token not in text]
        if missing:
            raise ValueError(
                "translation changed protected token for "
                f"{source['source_segment_id']}: {missing[0]}"
            )
        translated[str(source["source_segment_id"])] = text.strip()
    return translated


def materialize_translation_segments(
    units: Sequence[Mapping[str, Any]],
    translated: Mapping[str, str],
    invocation_ids: Mapping[str, str | None],
) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    for unit in units:
        source_id = str(unit["source_segment_id"])
        if unit["transform_kind"] == "passthrough":
            text = str(unit["source_text"])
        else:
            text = translated.get(source_id, "")
            if not text:
                raise ValueError(f"translation is missing source segment: {source_id}")
        segments.append({
            "translated_segment_id": str(unit["translated_segment_id"]),
            "source_segment_ids": [source_id],
            "parent_id": unit.get("parent_id"),
            "order": int(unit["order"]),
            "kind": str(unit["kind"]),
            "text": text,
            "transform_kind": str(unit["transform_kind"]),
            "alignment_kind": "one_to_one",
            "source_ranges": [{
                "source_segment_id": source_id,
                "start": 0,
                "end": len(str(unit["source_text"])),
                "exact": str(unit["source_text"]),
            }],
            "translated_range": {"start": 0, "end": len(text), "exact": text},
            "source_hash": text_hash(str(unit["source_text"])),
            "translated_hash": text_hash(text),
            "protected_tokens": list(unit["protected_tokens"]),
            "producer_invocation_id": invocation_ids.get(source_id),
        })
    return segments


def _metadata_values(items: object, *keys: str) -> list[str]:
    values: list[str] = []
    if not isinstance(items, list):
        return values
    for item in items:
        if isinstance(item, str) and item.strip():
            values.append(item.strip())
            continue
        if not isinstance(item, Mapping):
            continue
        for key in keys:
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                values.append(value.strip())
                break
    return values


def render_translated_html(
    document: Mapping[str, Any], segments: Sequence[Mapping[str, Any]],
) -> str:
    """渲染无脚本、无事件属性的独立译文；所有锚点来自稳定 segment id。"""
    by_source: dict[str, list[Mapping[str, Any]]] = {}
    for item in segments:
        for source_id in item["source_segment_ids"]:
            by_source.setdefault(str(source_id), []).append(item)
    metadata = document.get("metadata") if isinstance(document.get("metadata"), Mapping) else {}
    titles = metadata.get("titles") if isinstance(metadata.get("titles"), Mapping) else {}
    title_block = next(
        (
            item for item in segments
            if item["kind"] == "title" and item["transform_kind"] == "translated"
        ),
        None,
    )
    zh_title = str(
        titles.get("zh") or (title_block or {}).get("text") or titles.get("original") or "未命名文档"
    )
    original_title = str(titles.get("original") or "")
    authors = _metadata_values(metadata.get("authors"), "name", "display_name")
    affiliations = _metadata_values(
        metadata.get("affiliations"), "name_zh", "name", "display_name",
    )
    lines = [
        "<!doctype html>",
        '<html lang="zh-CN"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        f"<title>{html.escape(zh_title)}</title></head>",
        '<body><article class="document-translation">',
        '<header class="document-metadata">',
        f"<h1>{html.escape(zh_title)}</h1>",
    ]
    if original_title:
        lines.append(
            '<p class="document-original-title"><strong>英文标题：</strong>'
            f"{html.escape(original_title)}</p>"
        )
    if authors:
        lines.append(
            '<p class="document-authors"><strong>作者：</strong>'
            f"{html.escape('；'.join(authors))}</p>"
        )
    if affiliations:
        lines.append(
            '<p class="document-affiliations"><strong>机构：</strong>'
            f"{html.escape('；'.join(affiliations))}</p>"
        )
    author_items = metadata.get("authors") if isinstance(metadata.get("authors"), list) else []
    if author_items:
        lines.append('<details class="document-author-details"><summary>作者与机构详情</summary><ol>')
        for author in author_items:
            if not isinstance(author, Mapping):
                continue
            name = html.escape(str(author.get("name") or ""))
            author_affiliations = "；".join(
                str(value) for value in author.get("affiliations", []) if str(value).strip()
            )
            emails = "；".join(
                str(value) for value in author.get("emails", []) if str(value).strip()
            )
            details = " · ".join(
                value for value in (author_affiliations, emails) if value
            )
            suffix = f" <span>{html.escape(details)}</span>" if details else ""
            lines.append(f"<li><strong>{name}</strong>{suffix}</li>")
        lines.append("</ol></details>")
    author_notes = metadata.get("author_notes") if isinstance(metadata.get("author_notes"), list) else []
    if author_notes:
        lines.append('<details class="document-author-notes"><summary>作者说明</summary><ul>')
        for note in author_notes:
            if not isinstance(note, Mapping):
                continue
            lines.append(f"<li>{html.escape(str(note.get('text') or ''))}</li>")
        lines.append("</ul></details>")
    source_license = str(metadata.get("source_license") or "").strip()
    rights_notices = [
        str(value) for value in metadata.get("rights_notices", []) if str(value).strip()
    ] if isinstance(metadata.get("rights_notices"), list) else []
    if source_license or rights_notices:
        lines.append('<details class="document-rights"><summary>来源与许可</summary>')
        if source_license:
            lines.append(f"<p>{html.escape(source_license)}</p>")
        for notice in rights_notices:
            lines.append(f"<p>{html.escape(notice)}</p>")
        lines.append("</details>")
    lines.append("</header>")

    skipped_title = False
    for block in sorted(document["blocks"], key=lambda item: int(item["order"])):
        source_id = str(block["block_id"])
        aligned = by_source.get(source_id, [])
        if not aligned:
            continue
        segment = aligned[0]
        kind = str(segment["kind"])
        if kind == "title" and not skipped_title:
            skipped_title = True
            continue
        text = html.escape("\n\n".join(str(item["text"]) for item in aligned))
        anchor = html.escape(str(segment["translated_segment_id"]), quote=True)
        source = html.escape(source_id, quote=True)
        attrs = f'id="{anchor}" data-source-segment="{source}"'
        if kind in {"heading", "appendix"}:
            level = block.get("level")
            heading = min(6, max(2, int(level) + 1)) if type(level) is int else 2
            lines.append(f"<h{heading} {attrs}>{text}</h{heading}>")
        elif kind == "abstract":
            lines.append(f'<section {attrs} class="document-abstract"><h2>摘要</h2><p>{text}</p></section>')
        elif kind == "code":
            lines.append(f"<pre {attrs}><code>{text}</code></pre>")
        elif kind == "formula":
            lines.append(f'<div {attrs} class="document-formula"><code>{text}</code></div>')
        elif kind == "quote":
            lines.append(f"<blockquote {attrs}>{text}</blockquote>")
        elif kind in {"list", "list_item"}:
            lines.append(f'<p {attrs} class="document-list-item">{text}</p>')
        elif kind == "caption":
            lines.append(f'<p {attrs} class="document-caption">{text}</p>')
        else:
            lines.append(f"<p {attrs}>{text}</p>")

    lines.extend(_render_visual_catalog(document, by_source))
    lines.append("</article></body></html>")
    return "\n".join(lines) + "\n"


def _translated_text(
    by_source: Mapping[str, list[Mapping[str, Any]]],
    source_id: object,
    fallback: object,
) -> str:
    aligned = by_source.get(str(source_id), [])
    return "\n\n".join(str(item["text"]) for item in aligned) if aligned else str(fallback or "")


def _render_visual_catalog(
    document: Mapping[str, Any],
    by_source: Mapping[str, list[Mapping[str, Any]]],
) -> list[str]:
    lines: list[str] = []
    figures = document.get("figures") if isinstance(document.get("figures"), list) else []
    tables = document.get("tables") if isinstance(document.get("tables"), list) else []
    if not figures and not tables:
        return lines
    lines.append('<section class="document-visuals"><h2>图表</h2>')
    for figure in figures:
        if not isinstance(figure, Mapping):
            continue
        visual_id = html.escape(str(figure.get("figure_id") or ""), quote=True)
        label = html.escape(str(figure.get("label") or "图"))
        caption = html.escape(_translated_text(
            by_source, figure.get("block_id"), figure.get("caption"),
        ))
        lines.append(f'<figure id="{visual_id}" data-visual-id="{visual_id}"><figcaption><strong>{label}</strong> {caption}</figcaption>')
        for media in figure.get("media") or []:
            if not isinstance(media, Mapping):
                continue
            artifact = media.get("artifact")
            if isinstance(artifact, str) and artifact.startswith("assets/") and ".." not in artifact:
                lines.append(
                    '<img loading="lazy" '
                    f'data-artifact="{html.escape(artifact, quote=True)}" '
                    f'alt="{caption}">'
                )
        lines.append("</figure>")
    for table in tables:
        if not isinstance(table, Mapping):
            continue
        visual_id = html.escape(str(table.get("table_id") or ""), quote=True)
        label = html.escape(str(table.get("label") or "表"))
        caption = html.escape(_translated_text(
            by_source, table.get("block_id"), table.get("caption"),
        ))
        cells = [item for item in (table.get("cells") or []) if isinstance(item, Mapping)]
        lines.append(f'<section id="{visual_id}" data-visual-id="{visual_id}" class="document-table"><h3>{label}</h3><p>{caption}</p>')
        if cells:
            max_row = max(int(item.get("row") or 0) for item in cells)
            max_col = max(int(item.get("col") or 0) for item in cells)
            grid = {(int(item.get("row") or 0), int(item.get("col") or 0)): item for item in cells}
            occupied: set[tuple[int, int]] = set()
            lines.append("<table><tbody>")
            for row in range(max_row + 1):
                lines.append("<tr>")
                for col in range(max_col + 1):
                    if (row, col) in occupied:
                        continue
                    cell = grid.get((row, col))
                    if cell is None:
                        lines.append("<td></td>")
                        continue
                    tag = "th" if cell.get("role") in {"column_header", "row_header"} else "td"
                    rowspan = max(1, int(cell.get("rowspan") or 1))
                    colspan = max(1, int(cell.get("colspan") or 1))
                    for row_offset in range(rowspan):
                        for col_offset in range(colspan):
                            if row_offset or col_offset:
                                occupied.add((row + row_offset, col + col_offset))
                    cell_text = _translated_text(
                        by_source, cell.get("block_id"), cell.get("text"),
                    )
                    lines.append(
                        f'<{tag} rowspan="{rowspan}" colspan="{colspan}">'
                        f'{html.escape(cell_text)}</{tag}>'
                    )
                lines.append("</tr>")
            lines.append("</tbody></table>")
        else:
            lines.append('<p class="document-degraded">表格结构不可用，请查看原文位置。</p>')
        lines.append("</section>")
    lines.append("</section>")
    return lines


def translation_prompt_payload(batch: Sequence[Mapping[str, Any]]) -> str:
    value = {
        "schema_version": 1,
        "segments": [
            {
                "id": item["source_segment_id"],
                "kind": item["kind"],
                "text": item["source_text"],
                "protected_tokens": item["protected_tokens"],
            }
            for item in batch
        ],
    }
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
