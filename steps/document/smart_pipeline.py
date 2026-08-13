"""构造并校验分层论文智能笔记的有界中间协议。"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import OrderedDict
from pathlib import Path
from typing import Any, Mapping


MAX_CHAPTER_TEXT_BYTES = 46 * 1024
MAX_SEGMENT_PART_BYTES = 30 * 1024
MAX_STAGE_PROMPT_BYTES = 1024 * 1024
MAX_STAGE_RESULT_BYTES = 2 * 1024 * 1024
MAX_IMAGE_ATTACHMENTS = 5
MAX_PACKAGE_SOURCE_ALIASES = 32
MAX_PARALLEL_CALLS = 4
MAX_PACKAGES = 64
MAX_KNOWLEDGE_ITEMS = 128
_BIBLIOGRAPHY_RE = re.compile(
    r"^(?:(?:\d+(?:\.\d+)*[.)]?)\s+)?(?:references|bibliography|参考文献|参考书目)(?:\s|$)",
    re.I,
)
_FIGURE_RE = re.compile(r"\{\{FIGURE:([a-z0-9-]+)\}\}")
_EVIDENCE_RE = re.compile(r"\[证据:\s*([^\]]+)\]")
_DIRECT_IMAGE_RE = re.compile(
    r"!\s*\[|<\s*/?\s*(?:img|picture|source|image|svg)\b",
    re.IGNORECASE,
)


def canonical_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )


def canonical_bytes(value: Any) -> bytes:
    return canonical_json(value).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def split_utf8(value: str, limit: int) -> list[str]:
    if len(value.encode("utf-8")) <= limit:
        return [value]
    parts: list[str] = []
    rest = value.strip()
    while rest:
        low, high = 1, len(rest)
        while low < high:
            middle = (low + high + 1) // 2
            if len(rest[:middle].encode("utf-8")) <= limit:
                low = middle
            else:
                high = middle - 1
        cut = low
        for marker in ("\n\n", "\n", ". ", "; "):
            boundary = rest.rfind(marker, 0, cut)
            if boundary >= cut // 2:
                cut = boundary + len(marker)
                break
        parts.append(rest[:cut].strip())
        rest = rest[cut:].strip()
    return [part for part in parts if part]


def _image_paths(figures: list[dict[str, Any]]) -> list[str]:
    return sorted({
        str(media["artifact_path"])
        for figure in figures
        for media in figure["media"]
        if media.get("artifact_path")
    })


def _package_suffix(index: int) -> str:
    """把零起点序号编码为 a..z,aa..，覆盖单父包内的合法拆分数。"""
    value = index + 1
    parts: list[str] = []
    while value:
        value, remainder = divmod(value - 1, 26)
        parts.append(chr(ord("a") + remainder))
    return "".join(reversed(parts))


def _figure_media(item: Mapping[str, Any]) -> list[dict[str, Any]]:
    media = []
    for value in item.get("media") or []:
        artifact = value.get("artifact")
        if not isinstance(artifact, str) or not artifact.startswith("assets/"):
            continue
        media.append({
            "artifact_path": artifact,
            "role": value.get("role"),
            "width": value.get("width"),
            "height": value.get("height"),
        })
    return media


def build_chapter_packages(
    document: Mapping[str, Any], source_manifest: Mapping[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    """每个来源分片只分配一次；图片附件超限时按来源顺序拆执行包。"""
    blocks = {str(item["block_id"]): item for item in document["blocks"]}

    def block_text(block_id: str) -> str:
        block = blocks.get(block_id, {})
        return str(block.get("text") or block.get("content") or "").strip()

    heading_paths: dict[str, list[str]] = {}
    heading_stack: list[tuple[int, str]] = []
    headings: list[dict[str, Any]] = []
    for block in sorted(document["blocks"], key=lambda item: int(item["order"])):
        if block.get("kind") not in {"title", "heading", "appendix"}:
            continue
        title = block_text(str(block["block_id"]))
        if not title:
            continue
        level = int(block.get("level") or 2)
        while heading_stack and heading_stack[-1][0] >= level:
            heading_stack.pop()
        path = [item[1] for item in heading_stack] + [title]
        heading_paths[str(block["block_id"])] = path
        heading_stack.append((level, title))
        headings.append({
            "section_id": block["block_id"], "level": level,
            "title": title, "order": block["order"],
        })

    visuals_by_block: dict[str, list[dict[str, Any]]] = {}
    for kind, items, id_key in (
        ("figure", document.get("figures") or [], "figure_id"),
        ("table", document.get("tables") or [], "table_id"),
    ):
        for item in items:
            block_id = str(item.get("block_id") or "")
            if not block_id:
                continue
            visuals_by_block.setdefault(block_id, []).append({
                "visual_id": str(item[id_key]),
                "kind": kind,
                "label": item.get("label"),
                "caption": item.get("caption"),
                "media": _figure_media(item),
            })

    sections: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
    excluded: list[dict[str, str]] = []
    seen_segments: set[str] = set()
    for segment in source_manifest["segments"]:
        segment_id = str(segment["segment_id"])
        if segment_id in seen_segments:
            raise ValueError(f"duplicate source segment: {segment_id}")
        seen_segments.add(segment_id)
        section_id = str(segment.get("section") or segment_id)
        path = list(heading_paths.get(section_id, []))
        section_title = path[-1] if path else block_text(section_id)
        if _BIBLIOGRAPHY_RE.match(section_title.strip()):
            excluded.append({"segment_id": segment_id, "reason": "bibliography"})
            continue
        text = block_text(segment_id) or str(segment.get("support_text") or "").strip()
        if not text:
            excluded.append({"segment_id": segment_id, "reason": "empty"})
            continue
        parts = split_utf8(text, MAX_SEGMENT_PART_BYTES)
        for index, part in enumerate(parts, 1):
            sections.setdefault(section_id, []).append({
                "segment_id": segment_id,
                "part": index,
                "parts": len(parts),
                "kind": blocks.get(segment_id, {}).get("kind", "text"),
                "order": blocks.get(segment_id, {}).get("order"),
                "text": part,
                "section_path": path,
            })

    units = [
        {"section_id": section_id, "section_path": items[0]["section_path"], "items": items}
        for section_id, items in sections.items()
    ]
    grouped: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_bytes = 0
    for unit in units:
        for item in unit["items"]:
            size = len(item["text"].encode("utf-8"))
            if current and (
                current_bytes + size > MAX_CHAPTER_TEXT_BYTES
                or len(current) >= MAX_PACKAGE_SOURCE_ALIASES
            ):
                grouped.append(current)
                current, current_bytes = [], 0
            current.append({**item, "section_id": unit["section_id"]})
            current_bytes += size
    if current:
        grouped.append(current)
    if not grouped or len(grouped) > MAX_PACKAGES:
        raise ValueError("document chapter package count is invalid")

    paper_map = {
        "job_id": document["job_id"],
        "title": document.get("metadata", {}).get("titles", {}).get("original")
        or block_text(str(document["blocks"][0]["block_id"])),
        "abstract": document.get("metadata", {}).get("abstract") or "",
        "headings": headings,
    }
    packages: list[dict[str, Any]] = []
    assignments: list[dict[str, Any]] = []
    for package_index, items in enumerate(grouped, 1):
        parent_id = f"p{package_index:03d}"
        parent_aliases: dict[str, dict[str, Any]] = {}
        contents: list[dict[str, Any]] = []
        figures: list[dict[str, Any]] = []
        for alias_index, item in enumerate(items, 1):
            alias = f"s{alias_index:03d}"
            parent_aliases[alias] = {
                "segment_id": item["segment_id"],
                "part": item["part"], "parts": item["parts"],
            }
            contents.append({
                "source_alias": alias, "kind": item["kind"],
                "section_path": item["section_path"], "text": item["text"],
            })
            for visual in (
                visuals_by_block.get(item["segment_id"], [])
                if item["part"] == 1 else []
            ):
                figures.append({
                    "figure_alias": f"f{len(figures) + 1:02d}",
                    "visual_id": visual["visual_id"], "kind": visual["kind"],
                    "source_alias": alias, "label": visual["label"],
                    "caption": visual["caption"], "media": visual["media"],
                })

        figures_by_source: dict[str, list[dict[str, Any]]] = {}
        for figure in figures:
            figures_by_source.setdefault(figure["source_alias"], []).append(figure)
        split_groups: list[list[dict[str, Any]]] = []
        split_current: list[dict[str, Any]] = []
        split_paths: set[str] = set()
        for content in contents:
            attached = {
                path for figure in figures_by_source.get(content["source_alias"], [])
                for path in _image_paths([figure])
            }
            if len(attached) > MAX_IMAGE_ATTACHMENTS:
                raise ValueError("a single source segment exceeds image attachment limit")
            if split_current and (
                len(split_current) >= MAX_PACKAGE_SOURCE_ALIASES
                or len(split_paths | attached) > MAX_IMAGE_ATTACHMENTS
            ):
                split_groups.append(split_current)
                split_current, split_paths = [], set()
            split_current.append(content)
            split_paths |= attached
        if split_current:
            split_groups.append(split_current)
        for group_index, split_contents in enumerate(split_groups):
            package_id = parent_id if len(split_groups) == 1 else (
                parent_id + _package_suffix(group_index)
            )
            old_to_new = {
                item["source_alias"]: f"s{index:03d}"
                for index, item in enumerate(split_contents, 1)
            }
            child_figures: list[dict[str, Any]] = []
            for old_alias in old_to_new:
                for figure in figures_by_source.get(old_alias, []):
                    child_figures.append({
                        **figure,
                        "figure_alias": f"f{len(child_figures) + 1:02d}",
                        "source_alias": old_to_new[old_alias],
                        "logical_figure_ref": f"{parent_id}-{figure['figure_alias']}",
                    })
            package = {
                "schema_version": 1,
                "package_id": package_id,
                "logical_parent": parent_id,
                "section_paths": list(dict.fromkeys(
                    tuple(item["section_path"]) for item in items
                )),
                "source_aliases": {
                    new: parent_aliases[old] for old, new in old_to_new.items()
                },
                "contents": [
                    {**item, "source_alias": old_to_new[item["source_alias"]]}
                    for item in split_contents
                ],
                "figures": child_figures,
                "bibliography_excluded": True,
            }
            if len(_image_paths(child_figures)) > MAX_IMAGE_ATTACHMENTS:
                raise ValueError("chapter package image split failed")
            if len(package["source_aliases"]) > MAX_PACKAGE_SOURCE_ALIASES:
                raise ValueError("chapter package source alias split failed")
            packages.append(package)
            for alias, identity in package["source_aliases"].items():
                assignments.append({
                    "package_id": package_id, "logical_parent": parent_id,
                    "source_alias": alias, **identity,
                })

    if len(packages) > MAX_PACKAGES:
        raise ValueError("document chapter package count is invalid after image split")

    expected = {
        (item["segment_id"], item["part"])
        for values in sections.values() for item in values
    }
    actual = {(item["segment_id"], item["part"]) for item in assignments}
    if actual != expected or len(assignments) != len(actual):
        raise ValueError("chapter package coverage mismatch")
    receipt = {
        "schema_version": 1,
        "source_segments_total": len(source_manifest["segments"]),
        "covered_unique_segments": len({item["segment_id"] for item in assignments}),
        "covered_segment_parts": len(assignments),
        "excluded": excluded,
        "assignments": assignments,
    }
    return paper_map, packages, receipt


def build_themes(packages: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    parents = list(dict.fromkeys(str(item["logical_parent"]) for item in packages))
    groups = min(3, len(parents))
    size = math.ceil(len(parents) / groups)
    names = (
        "研究背景、问题定义与方法基础",
        "方法链、评估设计与主要结果",
        "后续论证、局限、附录与适用边界",
    )
    return [
        {"theme_id": f"t{index + 1:02d}", "name": names[index], "packages": chunk}
        for index in range(groups)
        if (chunk := parents[index * size:(index + 1) * size])
    ]


def validate_schema(value: Any, schema: Mapping[str, Any], path: str = "result") -> None:
    expected_type = schema.get("type")
    types = {
        "object": dict, "array": list, "string": str,
        "boolean": bool, "integer": int, "number": (int, float),
    }
    if expected_type in types and (
        not isinstance(value, types[expected_type])
        or expected_type in {"integer", "number"} and isinstance(value, bool)
    ):
        raise ValueError(f"{path} has invalid type")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{path} is outside enum")
    if isinstance(value, str):
        if len(value) < int(schema.get("minLength", 0)):
            raise ValueError(f"{path} is empty")
        if "maxLength" in schema and len(value) > int(schema["maxLength"]):
            raise ValueError(f"{path} is too long")
        if "pattern" in schema and re.fullmatch(str(schema["pattern"]), value) is None:
            raise ValueError(f"{path} has invalid format")
    if isinstance(value, list):
        if len(value) < int(schema.get("minItems", 0)):
            raise ValueError(f"{path} has too few items")
        if "maxItems" in schema and len(value) > int(schema["maxItems"]):
            raise ValueError(f"{path} has too many items")
        if schema.get("uniqueItems") and len({canonical_json(item) for item in value}) != len(value):
            raise ValueError(f"{path} contains duplicates")
        item_schema = schema.get("items")
        if isinstance(item_schema, Mapping):
            for index, item in enumerate(value):
                validate_schema(item, item_schema, f"{path}[{index}]")
    if isinstance(value, dict):
        required = set(schema.get("required") or [])
        if not required <= set(value):
            raise ValueError(f"{path} misses required fields")
        properties = schema.get("properties") or {}
        unknown = set(value) - set(properties)
        if schema.get("additionalProperties") is False and unknown:
            raise ValueError(_unknown_fields_message(path, unknown, set(properties)))
        for key, item in value.items():
            if key in properties:
                validate_schema(item, properties[key], f"{path}.{key}")


def _bounded_field_names(values: set[str]) -> str:
    names = sorted(values)
    shown = [name if len(name) <= 128 else name[:125] + "..." for name in names[:16]]
    suffix = f" (+{len(names) - len(shown)} more)" if len(names) > len(shown) else ""
    return json.dumps(
        shown, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
    ) + suffix


def _unknown_fields_message(path: str, unknown: set[str], allowed: set[str]) -> str:
    return (
        f"{path} contains unknown fields {_bounded_field_names(unknown)}; "
        f"allowed fields are {_bounded_field_names(allowed)}"
    )


def _collect_unknown_fields(
    value: Any, schema: Mapping[str, Any], path: str = "result",
) -> list[tuple[str, set[str], set[str]]]:
    """收集同一响应里的全部额外字段,让一次反馈足以修正多个对象。"""
    violations: list[tuple[str, set[str], set[str]]] = []
    if isinstance(value, dict):
        properties = schema.get("properties") or {}
        unknown = set(value) - set(properties)
        if schema.get("additionalProperties") is False and unknown:
            violations.append((path, unknown, set(properties)))
        for key, item in value.items():
            child_schema = properties.get(key)
            if isinstance(child_schema, Mapping):
                violations.extend(
                    _collect_unknown_fields(item, child_schema, f"{path}.{key}")
                )
    elif isinstance(value, list):
        item_schema = schema.get("items")
        if isinstance(item_schema, Mapping):
            for index, item in enumerate(value):
                violations.extend(
                    _collect_unknown_fields(item, item_schema, f"{path}[{index}]")
                )
    return violations


def _unknown_fields_feedback(
    violations: list[tuple[str, set[str], set[str]]],
) -> str:
    shown = violations[:16]
    message = "; ".join(
        _unknown_fields_message(path, unknown, allowed)
        for path, unknown, allowed in shown
    )
    if len(violations) > len(shown):
        message += f"; {len(violations) - len(shown)} additional objects contain unknown fields"
    return message


def parse_stage_result(raw: str, schema: Mapping[str, Any]) -> dict[str, Any]:
    data = raw.encode("utf-8")
    if len(data) > MAX_STAGE_RESULT_BYTES:
        raise ValueError("AI stage result exceeds byte limit")
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text).strip()
    try:
        result = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("AI stage result is not valid JSON") from exc
    unknown_fields = _collect_unknown_fields(result, schema)
    if unknown_fields:
        raise ValueError(_unknown_fields_feedback(unknown_fields))
    validate_schema(result, schema)
    return result


def validate_chapter_card(
    result: dict[str, Any], package: Mapping[str, Any],
) -> dict[str, Any]:
    if result["package_id"] != package["package_id"]:
        raise ValueError("chapter package identity mismatch")
    valid_sources = set(package["source_aliases"])
    coverage = result["coverage_refs"]
    if set(coverage) != valid_sources or len(coverage) != len(valid_sources):
        raise ValueError("chapter source coverage is not exact")
    if not result["knowledge"] or len(result["knowledge"]) > MAX_KNOWLEDGE_ITEMS:
        raise ValueError("chapter knowledge count is invalid")

    def check_refs(values: Any, *, required: bool = True) -> None:
        if not isinstance(values, list) or required and not values:
            raise ValueError("chapter source refs are invalid")
        if len(values) != len(set(values)) or not set(values) <= valid_sources:
            raise ValueError("chapter contains unknown source ref")

    for item in result["knowledge"]:
        check_refs(item["source_refs"])
    for name in ("cross_section_links", "unresolved"):
        for item in result[name]:
            check_refs(item["source_refs"], required=name == "cross_section_links")
    expected = {item["figure_alias"]: item for item in package["figures"]}
    returned = {item["figure_alias"]: item for item in result["figures"]}
    if set(returned) != set(expected) or len(returned) != len(result["figures"]):
        raise ValueError("chapter figure closure mismatch")
    for alias, item in returned.items():
        check_refs(item["source_refs"])
        if expected[alias]["source_alias"] not in item["source_refs"]:
            raise ValueError("chapter figure misses its source")
        has_media = bool(expected[alias]["media"])
        if bool(item["visual_analysis"].strip()) != has_media:
            raise ValueError("chapter visual analysis does not match attachment")
    return result


def enrich_cards(
    packages: list[Mapping[str, Any]], cards: Mapping[str, Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, str]]:
    enriched_cards: list[dict[str, Any]] = []
    knowledge_catalog: dict[str, dict[str, Any]] = {}
    figure_catalog: dict[str, dict[str, Any]] = {}
    source_map: dict[str, str] = {}
    for package in packages:
        package_id = str(package["package_id"])
        card = cards[package_id]
        knowledge = []
        for index, item in enumerate(card["knowledge"], 1):
            knowledge_id = f"{package_id}-k{index:03d}"
            refs = [f"{package_id}-{ref}" for ref in item["source_refs"]]
            enriched = {**item, "knowledge_id": knowledge_id, "source_refs": refs}
            knowledge.append(enriched)
            knowledge_catalog[knowledge_id] = {"package_id": package_id, **enriched}
        figures = []
        package_figures = {item["figure_alias"]: item for item in package["figures"]}
        for item in card["figures"]:
            figure_ref = f"{package_id}-{item['figure_alias']}"
            definition = package_figures[item["figure_alias"]]
            enriched = {
                **item, "figure_ref": figure_ref,
                "source_refs": [f"{package_id}-{ref}" for ref in item["source_refs"]],
                "artifact_paths": _image_paths([definition]),
                "label": definition.get("label"), "caption": definition.get("caption"),
            }
            figures.append(enriched)
            figure_catalog[figure_ref] = {"package_id": package_id, **enriched}
        for alias, identity in package["source_aliases"].items():
            source_map[f"{package_id}-{alias}"] = str(identity["segment_id"])
        enriched_cards.append({
            "package_id": package_id, "logical_parent": package["logical_parent"],
            "overview": card["overview"], "knowledge": knowledge,
            "cross_section_links": card["cross_section_links"], "figures": figures,
            "unresolved": card["unresolved"], "coverage_refs": [
                f"{package_id}-{ref}" for ref in card["coverage_refs"]
            ], "synthesis": card["synthesis"],
        })
    return enriched_cards, knowledge_catalog, figure_catalog, source_map


def project_theme_card(card: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "package_id": card["package_id"], "logical_parent": card["logical_parent"],
        "overview": card["overview"],
        "knowledge": [
            {key: item[key] for key in (
                "knowledge_id", "kind", "topic", "claim", "author_claim", "source_refs",
            )}
            for item in card["knowledge"]
        ],
        "cross_section_links": card["cross_section_links"],
        "figure_refs": [item["figure_ref"] for item in card["figures"]],
        "unresolved": card["unresolved"],
        "synthesis": {
            "analysis": card["synthesis"]["analysis"],
            "uncertainty": card["synthesis"]["uncertainty"],
        },
    }


def validate_theme(
    result: dict[str, Any], theme: Mapping[str, Any], knowledge_refs: list[str],
    figure_refs: list[str],
) -> dict[str, Any]:
    valid_knowledge, valid_figures = set(knowledge_refs), set(figure_refs)
    if result["theme_id"] != theme["theme_id"]:
        raise ValueError("theme identity mismatch")
    if set(result["coverage_refs"]) != valid_knowledge or len(result["coverage_refs"]) != len(valid_knowledge):
        raise ValueError("theme knowledge coverage is not exact")
    for name in ("learning_sections", "cross_theme_links", "tensions", "limitations"):
        if name == "learning_sections" and not result[name]:
            raise ValueError("theme learning sections are empty")
        for item in result[name]:
            refs = item["knowledge_refs"]
            if not refs or not set(refs) <= valid_knowledge:
                raise ValueError("theme contains unknown knowledge ref")
            if name == "learning_sections" and not set(item["figure_refs"]) <= valid_figures:
                raise ValueError("theme contains unknown figure ref")
    seen_figures: set[str] = set()
    for guide in result["figure_guides"]:
        if guide["figure_ref"] not in valid_figures or guide["figure_ref"] in seen_figures:
            raise ValueError("theme figure guide is invalid")
        seen_figures.add(guide["figure_ref"])
        if not set(guide["knowledge_refs"]) <= valid_knowledge:
            raise ValueError("theme figure guide has unknown knowledge ref")
    if valid_figures and not seen_figures:
        raise ValueError("theme omitted all available figures")
    return result


def validate_final(
    result: dict[str, Any], theme_refs: list[str],
    knowledge_catalog: Mapping[str, Mapping[str, Any]],
    figure_refs: list[str],
) -> dict[str, Any]:
    valid_themes, valid_knowledge, valid_figures = (
        set(theme_refs), set(knowledge_catalog), set(figure_refs),
    )
    if set(result["theme_coverage_refs"]) != valid_themes:
        raise ValueError("final theme coverage is not exact")
    used = result["used_knowledge_refs"]
    if not used or len(used) != len(set(used)) or not set(used) <= valid_knowledge:
        raise ValueError("final used knowledge refs are invalid")
    evidence = {
        value.strip()
        for group in _EVIDENCE_RE.findall(result["note_markdown"])
        for value in group.split(",") if value.strip()
    }
    if not evidence:
        raise ValueError("final markdown has no evidence refs")
    if not evidence <= valid_knowledge:
        raise ValueError("final markdown evidence closure is invalid")
    _require_single_source_evidence(
        _EVIDENCE_RE.findall(result["note_markdown"]), knowledge_catalog,
        label="final markdown",
    )
    _reject_direct_images(result["note_markdown"], label="final markdown")
    _validate_note_title(result["title"])
    if re.search(r"(?m)^#{2,4}\s+[^\n]*模型综合", result["note_markdown"]) is not None:
        raise ValueError("final markdown must leave model synthesis to the renderer")
    placement_knowledge = {
        ref for placement in result["figure_placements"]
        for ref in placement["knowledge_refs"]
    }
    synthesis_refs = set(result["synthesis"]["knowledge_refs"])
    if not synthesis_refs or not synthesis_refs <= valid_knowledge:
        raise ValueError("final synthesis refs are invalid")
    _require_single_source_evidence(
        [", ".join(result["synthesis"]["knowledge_refs"])], knowledge_catalog,
        label="final synthesis",
    )
    if (
        set(used) != evidence | placement_knowledge | synthesis_refs
    ):
        raise ValueError("final markdown evidence closure is invalid")
    placeholders = _FIGURE_RE.findall(result["note_markdown"])
    placement_refs = [item["figure_ref"] for item in result["figure_placements"]]
    if (
        len(placeholders) != len(set(placeholders))
        or len(placement_refs) != len(set(placement_refs))
        or set(placeholders) != set(placement_refs)
        or not set(placeholders) <= valid_figures
    ):
        raise ValueError("final figure closure is invalid")
    if valid_figures and not placeholders:
        raise ValueError("final omitted all available figures")
    for placement in result["figure_placements"]:
        if not placement["knowledge_refs"] or not set(placement["knowledge_refs"]) <= set(used):
            raise ValueError("final figure placement has unknown knowledge ref")
        _require_single_source_evidence(
            [", ".join(placement["knowledge_refs"])], knowledge_catalog,
            label="final figure placement",
        )
        for field in ("reading_guide", "limits"):
            _reject_renderer_markup(
                placement[field], label=f"final figure {field}",
            )
    for field in ("analysis", "basis", "uncertainty"):
        _reject_renderer_markup(
            result["synthesis"][field], label=f"final synthesis {field}",
        )
    return result


def _reject_direct_images(value: str, *, label: str) -> None:
    if _DIRECT_IMAGE_RE.search(value):
        raise ValueError(f"{label} must not contain direct images")


def _reject_renderer_markup(value: str, *, label: str) -> None:
    _reject_direct_images(value, label=label)
    if "{{FIGURE:" in value or "[[source:" in value or _EVIDENCE_RE.search(value):
        raise ValueError(f"{label} must not contain evidence markup")


def _validate_note_title(value: str) -> None:
    title = value.strip()
    if not title or "\n" in value or "\r" in value:
        raise ValueError("final title must be a single line")
    _reject_renderer_markup(title, label="final title")


def _require_single_source_evidence(
    groups: list[str], knowledge_catalog: Mapping[str, Mapping[str, Any]],
    *, label: str,
) -> None:
    for group in groups:
        knowledge_refs = [value.strip() for value in group.split(",") if value.strip()]
        source_refs = {
            str(source_ref)
            for knowledge_ref in knowledge_refs
            for source_ref in knowledge_catalog[knowledge_ref]["source_refs"]
        }
        if len(source_refs) != 1:
            raise ValueError(f"{label} evidence must resolve to exactly one source")


def render_model_synthesis(result: Mapping[str, Any]) -> str:
    """把结构化模型分析确定性写入正文，防止空标题或依据丢失。"""
    synthesis = result["synthesis"]
    refs = ", ".join(synthesis["knowledge_refs"])
    return (
        str(result["note_markdown"]).strip()
        + "\n\n## 模型综合\n\n"
        + str(synthesis["analysis"]).strip()
        + " **依据：** " + str(synthesis["basis"]).strip()
        + " **不确定性：** " + str(synthesis["uncertainty"]).strip()
        + f" [证据: {refs}]"
    )


def validate_introduction(
    result: dict[str, Any], knowledge_catalog: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    markdown = result["introduction_markdown"].strip()
    expected = (
        "## 论文导读：这篇论文要解决什么",
        "### 背景与问题", "### 解决思路", "### 如何验证",
        "### 主要结论与阅读边界",
    )
    positions = [markdown.find(heading) for heading in expected]
    if positions[0] != 0 or any(value < 0 for value in positions) or positions != sorted(positions):
        raise ValueError("paper introduction headings are invalid")
    used = result["used_knowledge_refs"]
    valid = set(knowledge_catalog)
    evidence = {
        value.strip()
        for group in _EVIDENCE_RE.findall(markdown)
        for value in group.split(",") if value.strip()
    }
    if not used or len(used) != len(set(used)) or not set(used) <= valid:
        raise ValueError("paper introduction refs are invalid")
    if not evidence or evidence != set(used):
        raise ValueError("paper introduction evidence is invalid")
    _require_single_source_evidence(
        _EVIDENCE_RE.findall(markdown), knowledge_catalog,
        label="paper introduction",
    )
    if "{{FIGURE:" in markdown:
        raise ValueError("paper introduction must not contain figures")
    _reject_direct_images(markdown, label="paper introduction")
    return result


def render_figures(
    markdown: str, placements: list[Mapping[str, Any]],
    figure_catalog: Mapping[str, Mapping[str, Any]], job_dir: Path,
) -> str:
    placement_map = {str(item["figure_ref"]): item for item in placements}

    def replace(match: re.Match[str]) -> str:
        figure_ref = match.group(1)
        figure = figure_catalog[figure_ref]
        placement = placement_map[figure_ref]
        paths = []
        for path in figure["artifact_paths"]:
            if not isinstance(path, str) or not path.startswith("assets/"):
                raise ValueError("figure artifact path is invalid")
            if not (job_dir / path).is_file():
                raise ValueError("figure artifact is missing")
            label = re.sub(r"[\]\n\r]", " ", str(figure.get("label") or figure_ref))
            paths.append(f"![{label}]({path})")
        if not paths:
            raise ValueError("selected figure has no media")
        guide = str(placement["reading_guide"]).strip()
        limits = str(placement["limits"]).strip()
        refs = ", ".join(placement["knowledge_refs"])
        return (
            "\n\n".join(paths)
            + f"\n\n> 读图：{guide} 边界：{limits}"
            + f" [证据: {refs}]"
        )

    return _FIGURE_RE.sub(replace, markdown)


def inject_source_markers(
    markdown: str, knowledge_catalog: Mapping[str, Mapping[str, Any]],
    source_map: Mapping[str, str], used_segments: set[str] | None = None,
    *, deduplicate_sources_by_evidence: bool = True,
) -> str:
    consumed = used_segments if used_segments is not None else set()

    def replace(match: re.Match[str]) -> str:
        markers = []
        local: set[str] = set()
        refs = [value.strip() for value in match.group(1).split(",") if value.strip()]
        for knowledge_ref in refs:
            item = knowledge_catalog[knowledge_ref]
            for source_ref in item["source_refs"]:
                segment_id = source_map[source_ref]
                if segment_id in local or (
                    deduplicate_sources_by_evidence and segment_id in consumed
                ):
                    continue
                local.add(segment_id)
                consumed.add(segment_id)
                token = segment_id.removeprefix("seg_")
                markers.append(f"[[source:{token}]]")
        return "".join(markers)

    return _EVIDENCE_RE.sub(replace, markdown)
