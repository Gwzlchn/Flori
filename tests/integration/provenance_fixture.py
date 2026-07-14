"""为合成 pipeline 工件发布可复算的来源与笔记溯源 sidecar。"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping

from shared.note_text import markdown_to_index_text
from shared.provenance import (
    EXACT_QUOTE_POLICY,
    build_provenance_manifest,
    build_source_manifest,
    canonical_json_bytes,
    make_segment_id,
)
from shared.storage import StorageBackend


SOURCE_ARTIFACT_PATH = "input/provenance_fixture.html"
SOURCE_MANIFEST_PATH = "intermediate/source_segments.json"


def _anchor_for(note_data: bytes) -> tuple[str, str]:
    markdown = note_data.decode("utf-8")
    body = markdown_to_index_text(markdown)
    candidates = [
        line.strip()
        for line in body.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    anchor = next(
        (
            line for line in candidates
            if body.count(line) == 1 and any(char.isalpha() for char in line)
        ),
        None,
    )
    if anchor is None:
        raise AssertionError("合成笔记缺少唯一的可复算文本锚点")
    return body, anchor


async def publish_provenance_fixture(
    storage: StorageBackend,
    *,
    job_id: str,
    pipeline: str,
    notes: Mapping[str, tuple[str, bytes]],
) -> None:
    """为当前 pipeline fixture 同步写来源清单和每个笔记的 provenance。"""
    prepared: dict[str, tuple[str, bytes, str, str]] = {}
    source_parts = ["<article>\n"]
    offsets: dict[str, tuple[int, int]] = {}
    for note_type in sorted(notes):
        note_path, note_data = notes[note_type]
        body, anchor = _anchor_for(note_data)
        source_parts.append(f'<p data-note-type="{note_type}">')
        start = sum(len(part) for part in source_parts)
        source_parts.append(anchor)
        end = start + len(anchor)
        offsets[note_type] = (start, end)
        source_parts.append("</p>\n")
        prepared[note_type] = (note_path, note_data, body, anchor)
    source_parts.append("</article>\n")
    source_text = "".join(source_parts)
    source_data = source_text.encode("utf-8")
    source_sha256 = hashlib.sha256(source_data).hexdigest()

    segments = []
    segment_ids: dict[str, str] = {}
    for note_type in sorted(prepared):
        _note_path, _note_data, _body, anchor = prepared[note_type]
        start, end = offsets[note_type]
        locator = {
            "kind": "text",
            "exact": anchor,
            "prefix": source_text[max(0, start - 32):start],
            "suffix": source_text[end:end + 32],
            "dom_path": None,
        }
        segment_id = make_segment_id(
            "fixture-html",
            start=start,
            end=end,
            section=note_type,
            locator=locator,
        )
        segment_ids[note_type] = segment_id
        segments.append({
            "segment_id": segment_id,
            "source_id": "fixture-html",
            "start": start,
            "end": end,
            "section": note_type,
            "locator": locator,
            "support_text": anchor,
            "support_artifact": {
                "kind": "html",
                "path": SOURCE_ARTIFACT_PATH,
                "sha256": source_sha256,
                "selector": {"start": start, "end": end},
            },
        })

    source_manifest = build_source_manifest(
        job_id=job_id,
        pipeline=pipeline,
        source_artifacts=[{
            "source_id": "fixture-html",
            "path": SOURCE_ARTIFACT_PATH,
            "sha256": source_sha256,
            "revision": "synthetic-current-pipeline",
            "media_duration_ms": None,
            "page_count": None,
        }],
        segments=segments,
    )
    await storage.write_file(job_id, SOURCE_ARTIFACT_PATH, source_data)
    await storage.write_file(
        job_id, SOURCE_MANIFEST_PATH, canonical_json_bytes(source_manifest),
    )

    for note_type, (note_path, note_data, body, anchor) in prepared.items():
        provenance = build_provenance_manifest(
            job_id=job_id,
            note_type=note_type,
            note_artifact=note_path,
            note_bytes=note_data,
            normalized_body=body,
            source_manifest_path=SOURCE_MANIFEST_PATH,
            source_manifest=source_manifest,
            segments=[{
                "anchor": anchor,
                "prefix": "",
                "suffix": "",
                "section": note_type,
                "source_segment_ids": [segment_ids[note_type]],
                "verification_policy": EXACT_QUOTE_POLICY,
            }],
        )
        await storage.write_file(
            job_id,
            f"output/provenance/{note_type}.json",
            canonical_json_bytes(provenance),
        )
