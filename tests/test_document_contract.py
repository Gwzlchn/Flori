"""Document Model 注册表与 fail-closed 契约测试。"""

from __future__ import annotations

import copy

import pytest

from shared.document_contract import (
    DOCUMENT_SCHEMA_VERSION,
    TRANSLATION_SCHEMA_VERSION,
    DocumentContractError,
    stable_id,
    validate_document,
    validate_quality,
    validate_translation,
)
from shared.document_registry import (
    DOCUMENT_KIND_NAMES,
    DocumentRegistryError,
    document_catalog,
    validate_document_kind,
)
from steps.document.provenance import build_document_source_manifest


FINGERPRINT = "sha256:" + "a" * 64


def _locator():
    return {
        "html": {
            "source_id": "html", "source_fingerprint": FINGERPRINT,
            "dom_path": "article > p:nth-of-type(1)", "exact": "hello",
        },
    }


def _document():
    return {
        "schema_version": DOCUMENT_SCHEMA_VERSION,
        "job_id": "jobs_document_fixture",
        "content_type": "document",
        "document_kind": "whitepaper",
        "classification": {"method": "user", "confidence": 1.0},
        "source_profile": "generic_html",
        "capabilities": ["html", "embedded_media"],
        "primary_source_id": "html",
        "sources": [{
            "source_id": "html", "source_profile": "generic_html",
            "capabilities": ["html", "embedded_media"],
            "fingerprint": FINGERPRINT, "path": "input/source.html",
            "mime_type": "text/html", "immutable": True,
        }],
        "metadata": {
            "titles": {"original": "A whitepaper", "zh": "白皮书"},
            "authors": [], "affiliations": [], "author_notes": [],
            "abstract": "", "keywords": [], "lang": "en", "license": "",
            "source_license": "", "rights_notices": [], "identifiers": {},
        },
        "blocks": [
            {
                "block_id": "S1",
                "parent_id": None,
                "order": 0,
                "kind": "heading",
                "level": 1,
                "text": "A whitepaper",
                "locator": _locator(),
            },
            {
                "block_id": "S1.F1B", "parent_id": "S1", "order": 2,
                "kind": "figure", "text": "", "locator": _locator(),
            },
            {
                "block_id": "S1.T1B", "parent_id": "S1", "order": 3,
                "kind": "table", "text": "Results", "locator": _locator(),
            },
            {
                "block_id": "S1.P1",
                "parent_id": "S1",
                "order": 1,
                "kind": "paragraph",
                "level": None,
                "text": "hello",
                "locator": _locator(),
            },
        ],
        "references": [],
        "assets": [],
        "figures": [
            {
                "figure_id": "S1.F1", "block_id": "S1.F1B", "label": "Figure 1",
                "caption": "", "order": 2, "source_locator": _locator(), "media": [],
                "extraction": {"status": "degraded", "reasons": ["media_missing"]},
            }
        ],
        "tables": [
            {
                "table_id": "S1.T1", "block_id": "S1.T1B", "label": "Table 1",
                "caption": "Results", "order": 3, "source_locator": _locator(),
                "cells": [], "representations": [], "footnotes": [],
                "extraction": {"status": "degraded", "reasons": ["cells_missing"]},
            }
        ],
    }


def test_registry_is_extensible_and_unknown_is_explicit():
    assert {"research_paper", "article", "whitepaper", "unknown"} <= set(DOCUMENT_KIND_NAMES)
    assert validate_document_kind(None) == "unknown"
    assert validate_document_kind("whitepaper") == "whitepaper"
    with pytest.raises(DocumentRegistryError, match="unsupported document_kind"):
        validate_document_kind("paper")
    catalog = document_catalog()
    assert any(item["kind"] == "whitepaper" for item in catalog["document_kinds"])
    assert any(item["profile"] == "scanned_pdf" for item in catalog["source_profiles"])


def test_document_contract_accepts_html_whitepaper_and_preserves_empty_visuals():
    result = validate_document(_document(), expected_job_id="jobs_document_fixture")
    assert result["figures"][0]["media"] == []
    assert result["tables"][0]["cells"] == []


@pytest.mark.parametrize(
    ("mutate", "error"),
    [
        (lambda doc: doc.update(content_type="article"), "content_type"),
        (lambda doc: doc["blocks"].append(copy.deepcopy(doc["blocks"][0])), "duplicate block_id"),
        (lambda doc: doc["blocks"][1].update(parent_id="missing"), "parent is missing"),
        (lambda doc: doc["blocks"][0]["locator"].pop("html"), "must contain html or pdf"),
        (lambda doc: doc.update(capabilities=["ocr"]), "capability union"),
    ],
)
def test_document_contract_rejects_cross_type_duplicate_and_forged_capability(mutate, error):
    document = _document()
    mutate(document)
    with pytest.raises(DocumentContractError, match=error):
        validate_document(document)


def test_quality_and_translation_require_explicit_degradation_and_alignment():
    quality = {
        "schema_version": 1,
        "job_id": "jobs_document_fixture",
        "status": "degraded",
        "reasons": ["table_structure_unavailable"],
        "metrics": {"source_table_count": 1, "registry_table_count": 1},
    }
    assert validate_quality(quality)["status"] == "degraded"
    with pytest.raises(DocumentContractError, match="requires reasons"):
        validate_quality({**quality, "reasons": []})

    translation = {
        "schema_version": TRANSLATION_SCHEMA_VERSION,
        "job_id": "jobs_document_fixture",
        "source_fingerprint": FINGERPRINT,
        "source_lang": "en",
        "target_lang": "zh",
        "status": "complete",
        "coverage": {
            "source_segments": 1,
            "translated_segments": 1,
            "passthrough_segments": 0,
        },
        "segments": [
            {
                "translated_segment_id": "tr_S1.P1",
                "source_segment_ids": ["S1.P1"],
                "kind": "paragraph",
                "text": "你好",
                "transform_kind": "translated",
                "alignment_kind": "one_to_one",
                "source_ranges": [{
                    "source_segment_id": "S1.P1", "start": 0,
                    "end": 5, "exact": "hello",
                }],
                "translated_range": {"start": 0, "end": 2, "exact": "你好"},
                "source_hash": FINGERPRINT,
                "translated_hash": FINGERPRINT,
                "protected_tokens": [],
            }
        ],
    }
    assert validate_translation(translation)["segments"][0]["source_segment_ids"] == ["S1.P1"]
    one_to_many = copy.deepcopy(translation)
    one_to_many["segments"][0]["alignment_kind"] = "one_to_many"
    second = copy.deepcopy(one_to_many["segments"][0])
    second.update({
        "translated_segment_id": "tr_S1.P1_2", "text": "世界",
        "translated_range": {"start": 0, "end": 2, "exact": "世界"},
    })
    one_to_many["segments"].append(second)
    one_to_many["coverage"]["translated_segments"] = 2
    assert len(validate_translation(one_to_many)["segments"]) == 2

    many_to_one = copy.deepcopy(translation)
    item = many_to_one["segments"][0]
    item["alignment_kind"] = "many_to_one"
    item["source_segment_ids"] = ["S1.P1", "S1.P2"]
    item["source_ranges"].append({
        "source_segment_id": "S1.P2", "start": 0, "end": 5, "exact": "world",
    })
    many_to_one["coverage"]["source_segments"] = 2
    assert validate_translation(many_to_one)["segments"][0]["alignment_kind"] == "many_to_one"

    broken_cardinality = copy.deepcopy(one_to_many)
    broken_cardinality["segments"][1]["alignment_kind"] = "one_to_one"
    with pytest.raises(DocumentContractError, match="cardinality"):
        validate_translation(broken_cardinality)

    translation["segments"][0]["source_segment_ids"] = []
    with pytest.raises(DocumentContractError, match="requires source_segment_ids"):
        validate_translation(translation)


def test_stable_id_is_deterministic_and_source_bound():
    assert stable_id("seg", FINGERPRINT, "article/p[1]") == stable_id(
        "seg", FINGERPRINT, "article/p[1]"
    )
    assert stable_id("seg", FINGERPRINT, "article/p[1]") != stable_id(
        "seg", "sha256:" + "b" * 64, "article/p[1]"
    )


def test_source_manifest_uses_html_as_primary_when_pdf_crosswalk_exists(tmp_path):
    job_dir = tmp_path / "jobs_document_fixture"
    (job_dir / "input").mkdir(parents=True)
    (job_dir / "input" / "source.html").write_text("<p>hello</p>", encoding="utf-8")
    (job_dir / "input" / "source.pdf").write_bytes(b"%PDF-1.4\n")
    document = _document()
    document["blocks"] = [
        block for block in document["blocks"] if block["block_id"] == "S1.P1"
    ]
    document["blocks"][0]["parent_id"] = None
    document["blocks"][0]["locator"]["pdf"] = {
        "source_id": "pdf",
        "source_fingerprint": "sha256:" + "b" * 64,
        "page": 1,
        "bboxes": [[10, 20, 30, 40]],
        "ocr_confidence": None,
    }
    document["sources"].append({
        "source_id": "pdf", "source_profile": "digital_pdf",
        "capabilities": ["pdf", "text_layer", "page_bbox"],
        "fingerprint": "sha256:" + "b" * 64, "path": "input/source.pdf",
        "mime_type": "application/pdf", "immutable": True,
    })
    document["capabilities"] += ["pdf", "text_layer", "page_bbox"]

    manifest = build_document_source_manifest(job_dir, document)

    assert [segment["segment_id"] for segment in manifest["segments"]] == ["S1.P1"]
    assert manifest["segments"][0]["source_id"] == "html"
    assert {artifact["source_id"] for artifact in manifest["source_artifacts"]} == {
        "html", "pdf",
    }


def test_source_manifest_uses_unique_raw_text_node_when_inline_html_splits_block(
    tmp_path,
):
    job_dir = tmp_path / "jobs_document_fixture"
    (job_dir / "input").mkdir(parents=True)
    source = (
        "<article><p>Evidence <strong>inside</strong> a split paragraph "
        "remains traceable &amp; exact.</p></article>"
    )
    (job_dir / "input" / "source.html").write_text(source, encoding="utf-8")
    document = _document()
    document["blocks"] = [
        {
            **document["blocks"][-1],
            "parent_id": None,
            "text": "Evidence inside a split paragraph remains traceable & exact.",
            "locator": {
                "html": {
                    **document["blocks"][-1]["locator"]["html"],
                    "exact": (
                        "Evidence inside a split paragraph remains traceable & exact."
                    ),
                }
            },
        }
    ]

    manifest = build_document_source_manifest(job_dir, document)

    segment = manifest["segments"][0]
    assert segment["locator"]["exact"] == (
        "a split paragraph remains traceable &amp; exact."
    )
    assert source[segment["start"]:segment["end"]] == segment["locator"]["exact"]
    assert segment["support_text"] == (
        "a split paragraph remains traceable & exact."
    )
