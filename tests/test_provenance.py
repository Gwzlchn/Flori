"""来源分段与笔记 provenance v1/v2 的严格契约测试。"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from shared.provenance import (
    EXACT_QUOTE_POLICY,
    build_provenance_manifest,
    build_source_manifest,
    canonical_json,
    canonical_json_bytes,
    extract_exact_quote_markers,
    make_segment_id,
    sha256_bytes,
    validate_provenance_manifest,
    validate_source_manifest,
    write_json_atomic,
    write_provenance_manifest,
    write_source_manifest,
)


NOTE_TEXT = "媒体片段说明。\nPDF 页面说明。\n文章原文说明。\n截图说明。\n"
NOTE_BYTES = NOTE_TEXT.replace("\n", "\r\n").encode("utf-8")


def _artifact(
    source_id: str,
    path: str,
    digest: str,
    *,
    duration: int | None = None,
    pages: int | None = None,
) -> dict:
    return {
        "source_id": source_id,
        "path": path,
        "sha256": digest,
        "revision": None,
        "media_duration_ms": duration,
        "page_count": pages,
    }


def _source_manifest() -> dict:
    artifacts = [
        _artifact("media", "input/media.mp4", "a" * 64, duration=10_000),
        _artifact("paper", "input/paper.pdf", "b" * 64, pages=12),
        _artifact("article", "input/article.html", "c" * 64),
        _artifact(
            "figure", "input/slides.pdf", "d" * 64, duration=20_000, pages=20,
        ),
    ]
    raw_segments = [
        {
            "source_id": "media",
            "start": None,
            "end": None,
            "section": "媒体",
            "locator": {"kind": "media", "start_ms": 1_000, "end_ms": 2_000},
        },
        {
            "source_id": "paper",
            "start": None,
            "end": None,
            "section": "论文",
            "locator": {"kind": "pdf", "page": 3, "bbox": [12.5, 20, 240, 360]},
        },
        {
            "source_id": "article",
            "start": 40,
            "end": 60,
            "section": None,
            "locator": {
                "kind": "text",
                "exact": "原文片段",
                "prefix": None,
                "suffix": "的后文",
                "dom_path": "main > p:nth-child(2)",
            },
        },
        {
            "source_id": "figure",
            "start": None,
            "end": None,
            "section": "图表",
            "locator": {
                "kind": "image",
                "asset_path": "intermediate/frames/0003.png",
                "asset_sha256": "e" * 64,
                "bbox": [0, 0, 1920, 1080],
                "start_ms": 4_000,
                "end_ms": 5_000,
                "page": 3,
            },
        },
    ]
    segments = []
    for raw in raw_segments:
        segments.append({
            "segment_id": make_segment_id(
                raw["source_id"],
                start=raw["start"],
                end=raw["end"],
                section=raw["section"],
                locator=raw["locator"],
            ),
            **raw,
        })
    return build_source_manifest(
        job_id="job-11c",
        pipeline="video",
        source_artifacts=artifacts,
        segments=segments,
    )


def _provenance(
    source: dict | None = None,
    text: str = NOTE_TEXT,
    note_bytes: bytes = NOTE_BYTES,
) -> dict:
    source = source or _source_manifest()
    refs = [item["segment_id"] for item in source["segments"]]
    return build_provenance_manifest(
        job_id="job-11c",
        note_type="original",
        note_artifact="output/notes_smart.md",
        note_bytes=note_bytes,
        normalized_body=text,
        source_manifest_path="intermediate/source_segments.json",
        source_manifest=source,
        segments=[
            {
                "anchor": "媒体片段",
                "prefix": "",
                "suffix": "说明",
                "section": "正文",
                "source_segment_ids": [refs[0]],
            },
            {
                "anchor": "PDF 页面",
                "prefix": "",
                "suffix": "说明",
                "section": "正文",
                "source_segment_ids": [refs[1]],
            },
            {
                "anchor": "文章原文",
                "prefix": "",
                "suffix": "说明",
                "section": "正文",
                "source_segment_ids": [refs[2]],
            },
            {
                "anchor": "截图",
                "prefix": "",
                "suffix": "说明",
                "section": "正文",
                "source_segment_ids": [refs[3]],
            },
        ],
    )


def test_four_locator_union_and_nullable_fields_are_strict() -> None:
    manifest = _source_manifest()
    assert manifest["schema_version"] == 2
    assert [item["locator"]["kind"] for item in manifest["segments"]] == [
        "media", "pdf", "text", "image",
    ]
    assert set(manifest["source_artifacts"][0]) == {
        "source_id", "path", "sha256", "revision", "media_duration_ms", "page_count",
    }
    assert manifest["source_artifacts"][0]["revision"] is None
    assert manifest["source_artifacts"][0]["page_count"] is None
    assert set(manifest["segments"][3]["locator"]) == {
        "kind", "asset_path", "asset_sha256", "bbox", "start_ms", "end_ms", "page",
    }
    assert manifest["segments"][0]["start"] is None
    assert manifest["segments"][1]["end"] is None
    assert all("support_text" in item for item in manifest["segments"])


def test_note_hash_binds_raw_bytes_not_normalized_anchor_body() -> None:
    source = _source_manifest()
    manifest = _provenance(source)
    assert NOTE_BYTES != NOTE_TEXT.encode("utf-8")
    assert manifest["note_sha256"] == sha256_bytes(NOTE_BYTES)
    assert manifest["note_sha256"] != sha256_bytes(NOTE_TEXT.encode("utf-8"))


def test_provenance_allows_explicit_empty_mapping_but_source_stays_nonempty() -> None:
    source = _source_manifest()
    manifest = build_provenance_manifest(
        job_id="job-11c",
        note_type="smart",
        note_artifact="output/notes_smart.md",
        note_bytes=NOTE_BYTES,
        normalized_body=NOTE_TEXT,
        source_manifest_path="intermediate/source_segments.json",
        source_manifest=source,
        segments=[],
    )
    assert manifest["segments"] == []

    source["segments"] = []
    with pytest.raises(ValueError, match="must not be empty"):
        validate_source_manifest(source)


def test_canonical_json_and_segment_id_are_byte_stable() -> None:
    locator_a = {"kind": "media", "start_ms": 1, "end_ms": 2}
    locator_b = {"end_ms": 2, "kind": "media", "start_ms": 1}
    first = make_segment_id("media", start=0, end=5, section=None, locator=locator_a)
    second = make_segment_id("media", start=0, end=5, section=None, locator=locator_b)
    assert first == second
    assert canonical_json({"中": 1, "a": [True, None]}) == '{"a":[true,null],"中":1}'
    assert canonical_json_bytes(_source_manifest()).endswith(b"\n")
    with pytest.raises(ValueError, match="canonical JSON"):
        canonical_json({"bad": float("nan")})


def test_atomic_writers_are_idempotent_and_hash_bound(tmp_path: Path) -> None:
    source = _source_manifest()
    source_path = tmp_path / "intermediate" / "source_segments.json"
    first_hash = write_source_manifest(source_path, source, trusted_root=tmp_path)
    first_bytes = source_path.read_bytes()
    second_hash = write_source_manifest(source_path, source, trusted_root=tmp_path)
    assert source_path.read_bytes() == first_bytes
    assert first_hash == second_hash == sha256_bytes(first_bytes)

    provenance = _provenance(source)
    provenance_path = tmp_path / "output" / "provenance" / "smart.json"
    written_hash = write_provenance_manifest(
        provenance_path,
        provenance,
        trusted_root=tmp_path,
        source_manifest=source,
        note_bytes=NOTE_BYTES,
        normalized_body=NOTE_TEXT,
    )
    assert written_hash == sha256_bytes(provenance_path.read_bytes())
    assert json.loads(provenance_path.read_text(encoding="utf-8")) == provenance
    assert list(tmp_path.rglob("*.tmp")) == []


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update({"extra": True}),
        lambda value: value["source_artifacts"][0].update({"extra": True}),
        lambda value: value["segments"][0].update({"extra": True}),
        lambda value: value["segments"][0]["locator"].update({"extra": True}),
    ],
)
def test_source_manifest_rejects_extra_keys(mutate) -> None:
    manifest = _source_manifest()
    mutate(manifest)
    with pytest.raises(ValueError, match="keys mismatch"):
        validate_source_manifest(manifest)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("artifact", "../secret"),
        ("artifact", "/etc/passwd"),
        ("artifact", "C:\\secret"),
        ("image", "assets/../../secret.png"),
    ],
)
def test_source_manifest_rejects_path_escape(field: str, value: str) -> None:
    manifest = _source_manifest()
    if field == "artifact":
        manifest["source_artifacts"][0]["path"] = value
    else:
        manifest["segments"][3]["locator"]["asset_path"] = value
    with pytest.raises(ValueError, match="path|root|canonical"):
        validate_source_manifest(manifest)


def test_atomic_writer_rejects_target_escape(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="escapes"):
        write_json_atomic(tmp_path.parent / "escaped.json", {}, trusted_root=tmp_path)


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda value: value["source_artifacts"].append(copy.deepcopy(value["source_artifacts"][0])), "duplicate source_id"),
        (lambda value: value["segments"].append(copy.deepcopy(value["segments"][0])), "duplicate segment_id"),
        (lambda value: value["segments"][0].update({"source_id": "missing"}), "unknown source_id"),
    ],
)
def test_source_manifest_rejects_duplicate_and_unknown_ids(mutate, message: str) -> None:
    manifest = _source_manifest()
    mutate(manifest)
    with pytest.raises(ValueError, match=message):
        validate_source_manifest(manifest)


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda value: value["source_artifacts"][0].update({"media_duration_ms": None}), "measured media_duration"),
        (lambda value: value["segments"][0]["locator"].update({"end_ms": 10_001}), "exceeds media"),
        (lambda value: value["source_artifacts"][1].update({"page_count": None}), "measured page_count"),
        (lambda value: value["segments"][1]["locator"].update({"page": 13}), "exceeds page_count"),
        (lambda value: value["segments"][1]["locator"].update({"bbox": [1, 2, 1, 4]}), "x1 > x0"),
        (lambda value: value["segments"][3]["locator"].update({"end_ms": None}), "both be null"),
    ],
)
def test_locator_rejects_fake_extent_and_invalid_bounds(mutate, message: str) -> None:
    manifest = _source_manifest()
    mutate(manifest)
    with pytest.raises(ValueError, match=message):
        validate_source_manifest(manifest)


def test_pdf_bbox_may_be_null_but_image_bbox_is_required() -> None:
    manifest = _source_manifest()
    manifest["segments"][1]["locator"]["bbox"] = None
    validate_source_manifest(manifest)

    manifest["segments"][3]["locator"]["bbox"] = None
    with pytest.raises(ValueError, match="four coordinates"):
        validate_source_manifest(manifest)


def test_only_non_text_segment_ranges_may_be_null() -> None:
    manifest = _source_manifest()
    manifest["segments"][0]["start"] = 0
    with pytest.raises(ValueError, match="both be null"):
        validate_source_manifest(manifest)

    manifest = _source_manifest()
    manifest["segments"][2]["start"] = None
    manifest["segments"][2]["end"] = None
    with pytest.raises(ValueError, match="0 <= start < end"):
        validate_source_manifest(manifest)


@pytest.mark.parametrize("schema_version", [True, 1.0, "1", 3])
def test_schema_version_requires_exact_supported_integer(schema_version) -> None:
    manifest = _source_manifest()
    manifest["schema_version"] = schema_version
    with pytest.raises(ValueError, match="schema_version"):
        validate_source_manifest(manifest)


def test_v1_direct_is_compatible_but_v1_smart_mapping_is_rejected() -> None:
    source = _source_manifest()
    source["schema_version"] = 1
    for segment in source["segments"]:
        segment.pop("support_text")
        segment.pop("support_artifact")
    validate_source_manifest(source)

    direct = _provenance(source)
    direct["schema_version"] = 1
    direct["note_type"] = "original"
    for segment in direct["segments"]:
        segment.pop("verification_policy")
    direct["source_manifest_sha256"] = sha256_bytes(canonical_json_bytes(source))
    validate_provenance_manifest(
        direct,
        source_manifest=source,
        note_bytes=NOTE_BYTES,
        normalized_body=NOTE_TEXT,
    )

    direct["note_type"] = "smart"
    with pytest.raises(ValueError, match="legacy smart"):
        validate_provenance_manifest(
            direct,
            source_manifest=source,
            note_bytes=NOTE_BYTES,
            normalized_body=NOTE_TEXT,
        )


def test_exact_quote_policy_requires_bound_support_and_rejects_paraphrase() -> None:
    source = _source_manifest()
    source["segments"][0]["support_text"] = "媒体片段说明。"
    source["segments"][0]["support_artifact"] = {
        "kind": "video_subtitle",
        "path": "input/subtitle.srt",
        "sha256": "f" * 64,
        "selector": {"index": 0},
    }
    mapping = {
        "anchor": "媒体片段说明。",
        "prefix": "",
        "suffix": "",
        "section": "正文",
        "source_segment_ids": [source["segments"][0]["segment_id"]],
        "verification_policy": EXACT_QUOTE_POLICY,
    }
    note = "媒体片段说明。"
    built = build_provenance_manifest(
        job_id="job-11c",
        note_type="smart",
        note_artifact="output/notes.md",
        note_bytes=note.encode(),
        normalized_body=note,
        source_manifest_path="intermediate/source_segments.json",
        source_manifest=source,
        segments=[mapping],
    )
    assert built["segments"][0]["verification_policy"] == EXACT_QUOTE_POLICY

    direct = {**mapping, "verification_policy": "direct_locator_v1"}
    with pytest.raises(ValueError, match="smart mapping requires"):
        build_provenance_manifest(
            job_id="job-11c",
            note_type="smart",
            note_artifact="output/notes.md",
            note_bytes=note.encode(),
            normalized_body=note,
            source_manifest_path="intermediate/source_segments.json",
            source_manifest=source,
            segments=[direct],
        )
    with pytest.raises(ValueError, match="cross-language attestation"):
        build_provenance_manifest(
            job_id="job-11c",
            note_type="translated",
            note_artifact="output/translated.md",
            note_bytes=note.encode(),
            normalized_body=note,
            source_manifest_path="intermediate/source_segments.json",
            source_manifest=source,
            segments=[direct],
        )

    mapping["anchor"] = "媒体片段大意。"
    with pytest.raises(ValueError, match="not contained"):
        build_provenance_manifest(
            job_id="job-11c",
            note_type="original",
            note_artifact="output/notes.md",
            note_bytes=mapping["anchor"].encode(),
            normalized_body=mapping["anchor"],
            source_manifest_path="intermediate/source_segments.json",
            source_manifest=source,
            segments=[mapping],
        )


def test_exact_quote_marker_requires_consecutive_refs_and_textual_claim() -> None:
    raw_segments = []
    for index, support in enumerate(("alpha exact", "beta quote", "unrelated")):
        locator = {
            "kind": "media", "start_ms": index * 1000, "end_ms": index * 1000 + 500,
        }
        raw_segments.append({
            "segment_id": make_segment_id(
                "audio", start=None, end=None, section="transcript", locator=locator,
            ),
            "source_id": "audio",
            "start": None,
            "end": None,
            "section": "transcript",
            "locator": locator,
            "support_text": support,
            "support_artifact": {
                "kind": "audio_segments",
                "path": "intermediate/segments.json",
                "sha256": "f" * 64,
                "selector": {"index": index},
            },
        })
    source = build_source_manifest(
        job_id="job-exact",
        pipeline="audio",
        source_artifacts=[_artifact(
            "audio", "input/source.mp3", "a" * 64, duration=3000,
        )],
        segments=raw_segments,
    )
    tokens = [item["segment_id"].removeprefix("seg_") for item in raw_segments]
    clean, mappings = extract_exact_quote_markers(
        f"alpha exact beta quote [[source:{tokens[0]}]][[source:{tokens[1]}]]",
        source,
        error_prefix="smart note",
    )
    assert clean == "alpha exact beta quote"
    assert mappings == []

    _, mappings = extract_exact_quote_markers(
        f"alpha exact [[source:{tokens[0]}]][[source:{tokens[2]}]]",
        source,
        error_prefix="smart note",
    )
    assert mappings == []
    _, mappings = extract_exact_quote_markers(
        f"alpha exact beta quote [[source:{tokens[0]}]][[source:{tokens[1]}]]"
        f"[[source:{tokens[2]}]]",
        source,
        error_prefix="smart note",
    )
    assert mappings == []
    source["segments"][0]["support_text"] = "12345"
    _, mappings = extract_exact_quote_markers(
        f"12345 [[source:{tokens[0]}]]", source, error_prefix="smart note",
    )
    assert mappings == []


def test_exact_quote_normalization_applies_nfc_but_not_nfkc() -> None:
    source = _source_manifest()
    source["segments"][0]["support_artifact"] = {
        "kind": "video_subtitle",
        "path": "input/subtitle.srt",
        "sha256": "f" * 64,
        "selector": {"index": 0},
    }
    token = source["segments"][0]["segment_id"].removeprefix("seg_")
    source["segments"][0]["support_text"] = "Cafe\u0301 证据。"

    _, mappings = extract_exact_quote_markers(
        f"Café 证据。 [[source:{token}]]", source, error_prefix="smart note",
    )

    assert len(mappings) == 1

    source["segments"][0]["support_text"] = "公式值是 10²。"

    _, mappings = extract_exact_quote_markers(
        f"公式值是 102。 [[source:{token}]]", source, error_prefix="smart note",
    )

    assert mappings == []


def test_exact_quote_rejects_cross_modal_multi_reference_claim() -> None:
    source = _source_manifest()
    media_segment = source["segments"][0]
    media_segment["support_text"] = "跨模态逐字事实"
    media_segment["support_artifact"] = {
        "kind": "audio_segments",
        "path": "intermediate/segments.json",
        "sha256": "f" * 64,
        "selector": {"index": 0},
    }
    text_segment = source["segments"][2]
    text_segment["support_text"] = "跨模态逐字事实"
    text_segment["support_artifact"] = {
        "kind": "html",
        "path": "input/article.html",
        "sha256": "c" * 64,
        "selector": {"start": 40, "end": 60},
    }
    media_token = media_segment["segment_id"].removeprefix("seg_")
    text_token = text_segment["segment_id"].removeprefix("seg_")

    _, mappings = extract_exact_quote_markers(
        f"跨模态逐字事实 [[source:{media_token}]][[source:{text_token}]]",
        source,
        error_prefix="smart note",
    )

    assert mappings == []


def test_support_text_is_bounded_and_v2_extra_keys_fail_closed() -> None:
    source = _source_manifest()
    source["segments"][0]["support_text"] = "界" * 1366
    with pytest.raises(ValueError, match="4096 bytes"):
        validate_source_manifest(source)


def test_hashes_must_be_exact_lowercase_64hex() -> None:
    source = _source_manifest()
    source["source_artifacts"][0]["sha256"] = "A" * 64
    with pytest.raises(ValueError, match="64 lowercase"):
        validate_source_manifest(source)


@pytest.mark.parametrize("extra_at", ["top", "segment"])
def test_provenance_rejects_extra_keys(extra_at: str) -> None:
    source = _source_manifest()
    manifest = _provenance(source)
    if extra_at == "top":
        manifest["extra"] = True
    else:
        manifest["segments"][0]["extra"] = True
    with pytest.raises(ValueError, match="keys mismatch"):
        validate_provenance_manifest(
            manifest,
            source_manifest=source,
            note_bytes=NOTE_BYTES,
            normalized_body=NOTE_TEXT,
        )


def test_provenance_rejects_tampered_hashes_and_cross_job_source() -> None:
    source = _source_manifest()
    manifest = _provenance(source)
    manifest["source_manifest_sha256"] = "f" * 64
    with pytest.raises(ValueError, match="source_manifest_sha256 mismatch"):
        validate_provenance_manifest(
            manifest,
            source_manifest=source,
            note_bytes=NOTE_BYTES,
            normalized_body=NOTE_TEXT,
        )

    manifest = _provenance(source)
    manifest["note_sha256"] = "f" * 64
    with pytest.raises(ValueError, match="note_sha256 mismatch"):
        validate_provenance_manifest(
            manifest,
            source_manifest=source,
            note_bytes=NOTE_BYTES,
            normalized_body=NOTE_TEXT,
        )

    manifest = _provenance(source)
    source["job_id"] = "another-job"
    with pytest.raises(ValueError, match="another job"):
        validate_provenance_manifest(
            manifest,
            source_manifest=source,
            note_bytes=NOTE_BYTES,
            normalized_body=NOTE_TEXT,
        )


@pytest.mark.parametrize(
    "refs, message",
    [
        (["missing"], "unknown source segment ref"),
        (None, "duplicate source segment ref"),
        ([], "must not be empty"),
    ],
)
def test_provenance_rejects_invalid_and_duplicate_refs(refs, message: str) -> None:
    source = _source_manifest()
    manifest = _provenance(source)
    existing = manifest["segments"][0]["source_segment_ids"][0]
    manifest["segments"][0]["source_segment_ids"] = [existing, existing] if refs is None else refs
    with pytest.raises(ValueError, match=message):
        validate_provenance_manifest(
            manifest,
            source_manifest=source,
            note_bytes=NOTE_BYTES,
            normalized_body=NOTE_TEXT,
        )


def test_builder_rejects_empty_missing_and_ambiguous_anchor() -> None:
    source = _source_manifest()
    refs = [source["segments"][0]["segment_id"]]
    base = {
        "anchor": "重复",
        "prefix": "",
        "suffix": "",
        "section": None,
        "source_segment_ids": refs,
    }
    with pytest.raises(ValueError, match="ambiguous"):
        build_provenance_manifest(
            job_id="job-11c",
            note_type="original",
            note_artifact="output/notes.md",
            note_bytes="原始字节不必等于规范化正文。".encode("utf-8"),
            normalized_body="重复，随后再次重复。",
            source_manifest_path="intermediate/source_segments.json",
            source_manifest=source,
            segments=[base],
        )

    disambiguated = {**base, "prefix": "再次"}
    built = build_provenance_manifest(
        job_id="job-11c",
        note_type="original",
        note_artifact="output/notes.md",
        note_bytes="原始字节不必等于规范化正文。".encode("utf-8"),
        normalized_body="重复，随后再次重复。",
        source_manifest_path="intermediate/source_segments.json",
        source_manifest=source,
        segments=[disambiguated],
    )
    assert built["segments"][0]["anchor"] == "重复"

    for anchor, message in [("", "must not be empty"), ("不存在", "does not match")]:
        invalid = {**base, "anchor": anchor}
        with pytest.raises(ValueError, match=message):
            build_provenance_manifest(
                job_id="job-11c",
                note_type="original",
                note_artifact="output/notes.md",
                note_bytes=b"raw-note",
                normalized_body="唯一内容。",
                source_manifest_path="intermediate/source_segments.json",
                source_manifest=source,
                segments=[invalid],
            )


def test_provenance_rejects_path_escape_and_duplicate_item() -> None:
    source = _source_manifest()
    manifest = _provenance(source)
    manifest["note_artifact"] = "../notes.md"
    with pytest.raises(ValueError, match="root|canonical"):
        validate_provenance_manifest(
            manifest,
            source_manifest=source,
            note_bytes=NOTE_BYTES,
            normalized_body=NOTE_TEXT,
        )

    manifest = _provenance(source)
    manifest["segments"].append(copy.deepcopy(manifest["segments"][0]))
    with pytest.raises(ValueError, match="duplicate provenance"):
        validate_provenance_manifest(
            manifest,
            source_manifest=source,
            note_bytes=NOTE_BYTES,
            normalized_body=NOTE_TEXT,
        )
