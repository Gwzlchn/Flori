"""概念只从严格 provenance 锚点取得来源段 ID。"""

from __future__ import annotations

import copy
import hashlib

import pytest

from shared.concept_evidence import attach_concept_source_segments
from shared.note_text import markdown_to_index_text
from shared.provenance import (
    build_provenance_manifest,
    build_source_manifest,
    canonical_json_bytes,
    make_segment_id,
)


def _sidecars(
    *,
    job_id: str = "job-concepts",
    pipeline: str = "document",
    note_type: str = "original",
    note_path: str = "intermediate/document_index.md",
    mappings: list[dict] | None = None,
):
    source_bytes = b"first source second source"
    segments = []
    for index, (start, end) in enumerate(((0, 12), (13, 26)), start=1):
        exact = source_bytes[start:end].decode()
        locator = {
            "kind": "text",
            "exact": exact,
            "prefix": "",
            "suffix": "",
            "dom_path": None,
        }
        segment_id = make_segment_id(
            "source", start=start, end=end, section=f"s{index}", locator=locator,
        )
        segments.append({
            "segment_id": segment_id,
            "source_id": "source",
            "start": start,
            "end": end,
            "section": f"s{index}",
            "locator": locator,
        })
    source = build_source_manifest(
        job_id=job_id,
        pipeline=pipeline,
        source_artifacts=[{
            "source_id": "source",
            "path": "input/source.html",
            "sha256": hashlib.sha256(source_bytes).hexdigest(),
            "revision": None,
            "media_duration_ms": None,
            "page_count": None,
        }],
        segments=segments,
    )
    note = (
        "# Notes\n\nAlpha Transformer 注意力机制。\n\n"
        "Beta Transformer works.\n"
    ).encode()
    mappings = mappings if mappings is not None else [
        {
            "anchor": "Alpha Transformer 注意力机制。",
            "prefix": "",
            "suffix": "",
            "section": "alpha",
            "source_segment_ids": [segments[0]["segment_id"]],
        },
        {
            "anchor": "Beta Transformer works.",
            "prefix": "",
            "suffix": "",
            "section": "beta",
            "source_segment_ids": [segments[1]["segment_id"]],
        },
    ]
    provenance = build_provenance_manifest(
        job_id=job_id,
        note_type=note_type,
        note_artifact=note_path,
        note_bytes=note,
        normalized_body=markdown_to_index_text(note.decode()),
        source_manifest_path="intermediate/source_segments.json",
        source_manifest=source,
        segments=mappings,
    )
    return note, source, provenance, [item["segment_id"] for item in segments]


def _attach(key_terms, *, note, source, provenance, **overrides):
    arguments = {
        "job_id": "job-concepts",
        "pipeline": "document",
        "note_type": "original",
        "note_path": "intermediate/document_index.md",
        "note_bytes": note,
        "normalized_body": markdown_to_index_text(note.decode()),
        "source_manifest_path": "intermediate/source_segments.json",
        "source_manifest_data": canonical_json_bytes(source),
        "provenance_path": "output/provenance/original.json",
        "provenance_data": canonical_json_bytes(provenance),
    }
    arguments.update(overrides)
    return attach_concept_source_segments(key_terms, **arguments)


def test_exact_anchor_matches_multiple_terms_and_multiple_anchors():
    note, source, provenance, segment_ids = _sidecars()
    terms = [
        {"term": "Transformer", "evidence_source_segment_ids": ["seg_" + "f" * 64]},
        {"term": "注意力机制", "zh_name": None},
        {"term": "form"},
    ]

    attached = _attach(terms, note=note, source=source, provenance=provenance)

    assert attached[0]["evidence_source_segment_ids"] == segment_ids
    assert attached[1]["evidence_source_segment_ids"] == [segment_ids[0]]
    assert attached[2]["evidence_source_segment_ids"] == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"job_id": "another-job"},
        {"note_path": "output/other.md"},
        {"note_bytes": b"tampered note", "normalized_body": "tampered note"},
        {"source_manifest_data": b'{"not":"canonical"}'},
        {"provenance_data": None},
    ],
)
def test_invalid_identity_path_hash_or_sidecar_fails_closed(overrides):
    note, source, provenance, _ = _sidecars()
    attached = _attach(
        [{"term": "Transformer", "evidence_source_segment_ids": ["forged"]}],
        note=note,
        source=source,
        provenance=provenance,
        **overrides,
    )
    assert attached[0]["evidence_source_segment_ids"] == []


def test_valid_empty_translation_provenance_never_binds():
    note, source, provenance, _ = _sidecars(
        note_type="translated",
        note_path="output/translated.html",
        mappings=[],
    )
    attached = _attach(
        [{"term": "Transformer", "evidence_source_segment_ids": ["forged"]}],
        note=note,
        source=source,
        provenance=provenance,
        note_type="translated",
        note_path="output/translated.html",
        provenance_path="output/provenance/translated.json",
    )
    assert attached[0]["evidence_source_segment_ids"] == []


def test_source_manifest_hash_change_fails_closed():
    note, source, provenance, _ = _sidecars()
    changed_source = copy.deepcopy(source)
    changed_source["source_artifacts"][0]["sha256"] = "0" * 64

    attached = _attach(
        [{"term": "Transformer"}],
        note=note,
        source=source,
        provenance=provenance,
        source_manifest_data=canonical_json_bytes(changed_source),
    )

    assert attached[0]["evidence_source_segment_ids"] == []
