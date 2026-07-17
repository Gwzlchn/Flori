"""验证 HTML/PDF crosswalk 只发布唯一高置信匹配。"""

from __future__ import annotations

from copy import deepcopy

import pytest

from shared.document_contract import DOCUMENT_SCHEMA_VERSION, validate_document
from steps.document import crosswalk


HTML_FP = "sha256:" + "a" * 64
PDF_FP = "sha256:" + "b" * 64


def _metadata() -> dict:
    return {
        "titles": {"original": "Crosswalk", "zh": None},
        "authors": [], "affiliations": [], "author_notes": [],
        "abstract": "", "keywords": [], "lang": "en", "license": "",
        "source_license": "", "rights_notices": [], "identifiers": {},
    }


def _block(block_id: str, text: str, source: str, order: int) -> dict:
    if source == "html":
        locator = {"html": {
            "source_id": "html", "source_fingerprint": HTML_FP,
            "dom_path": f"/article[1]/p[{order + 1}]", "exact": text,
        }}
    else:
        locator = {"pdf": {
            "source_id": "pdf", "source_fingerprint": PDF_FP,
            "page": 2, "bboxes": [[20, 40 + order * 20, 420, 55 + order * 20]],
        }}
    return {
        "block_id": block_id, "parent_id": None, "order": order,
        "kind": "paragraph", "text": text, "locator": locator,
    }


def _html_document() -> dict:
    return validate_document({
        "schema_version": DOCUMENT_SCHEMA_VERSION,
        "job_id": "jobs_crosswalk", "content_type": "document",
        "document_kind": "research_paper",
        "classification": {"method": "source", "confidence": 1.0},
        "source_profile": "scholarly_html",
        "capabilities": ["html", "math", "bibliography", "embedded_media"],
        "primary_source_id": "html",
        "sources": [{
            "source_id": "html", "source_profile": "scholarly_html",
            "capabilities": ["html", "math", "bibliography", "embedded_media"],
            "path": "input/source.html", "mime_type": "text/html",
            "fingerprint": HTML_FP, "immutable": True,
        }],
        "metadata": _metadata(),
        "blocks": [_block("blk_intro", "Unique aligned paragraph text.", "html", 0)],
        "figures": [], "tables": [], "assets": [], "references": [],
    })


def _pdf_document(*texts: str) -> dict:
    return validate_document({
        "schema_version": DOCUMENT_SCHEMA_VERSION,
        "job_id": "jobs_crosswalk", "content_type": "document",
        "document_kind": "research_paper",
        "classification": {"method": "source", "confidence": 1.0},
        "source_profile": "digital_pdf",
        "capabilities": ["pdf", "text_layer", "page_bbox"],
        "primary_source_id": "pdf",
        "sources": [{
            "source_id": "pdf", "source_profile": "digital_pdf",
            "capabilities": ["pdf", "text_layer", "page_bbox"],
            "path": "input/source.pdf", "mime_type": "application/pdf",
            "fingerprint": PDF_FP, "immutable": True,
        }],
        "metadata": _metadata(),
        "blocks": [
            _block(f"pdf_{index}", text, "pdf", index)
            for index, text in enumerate(texts)
        ],
        "figures": [], "tables": [], "assets": [], "references": [],
    })


def _quality(status: str = "complete") -> dict:
    return {
        "schema_version": 1, "job_id": "jobs_crosswalk", "status": status,
        "reasons": [] if status == "complete" else ["fixture_degraded"],
        "metrics": {},
    }


def test_crosswalk_attaches_second_source_and_unique_pdf_locator(monkeypatch, tmp_path):
    pdf = _pdf_document("Unique aligned paragraph text.")
    monkeypatch.setattr(crosswalk, "parse_pdf_document", lambda *_args: (pdf, _quality()))

    document, quality = crosswalk.attach_pdf_crosswalk(
        tmp_path, _html_document(), _quality(), {},
    )

    assert {source["source_id"] for source in document["sources"]} == {"html", "pdf"}
    locator = document["blocks"][0]["locator"]
    assert locator["crosswalk"] == {
        "status": "matched", "confidence": 1.0,
        "method": "normalized_text_unique",
    }
    assert locator["pdf"]["page"] == 2
    assert quality["metrics"]["pdf_crosswalk_blocks"] == 1
    assert quality["status"] == "complete"


def test_crosswalk_duplicate_text_is_ambiguous_and_never_guesses(monkeypatch, tmp_path):
    pdf = _pdf_document(
        "Unique aligned paragraph text.", "Unique aligned paragraph text.",
    )
    monkeypatch.setattr(crosswalk, "parse_pdf_document", lambda *_args: (pdf, _quality()))

    document, quality = crosswalk.attach_pdf_crosswalk(
        tmp_path, deepcopy(_html_document()), _quality(), {},
    )

    locator = document["blocks"][0]["locator"]
    assert locator["crosswalk"]["status"] == "ambiguous"
    assert "pdf" not in locator
    assert quality["status"] == "degraded"
    assert "pdf_crosswalk_partial" in quality["reasons"]


@pytest.mark.parametrize(
    ("text", "candidates", "status"),
    [
        ("short", [{"text": "short"}], "unmatched"),
        ("A sufficiently long sentence", [{"text": "different sentence"}], "unmatched"),
        ("A sufficiently long sentence", [
            {"text": "A sufficiently long sentence"},
            {"text": "A sufficiently long sentence"},
        ], "ambiguous"),
    ],
)
def test_crosswalk_matching_is_fail_closed(text, candidates, status):
    candidate, _confidence, actual = crosswalk._match(text, candidates)
    assert actual == status
    assert candidate is None
