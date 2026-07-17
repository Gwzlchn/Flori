"""验证 Document 原文阅读副本的隔离、锚点与资源重写。"""

from __future__ import annotations

import hashlib
import json

import pytest

from api.services.document_reader import render_document_html
from shared.document_contract import DOCUMENT_SCHEMA_VERSION


def _fingerprint(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _document(job_id: str, raw: bytes) -> dict:
    fingerprint = _fingerprint(raw)
    return {
        "schema_version": DOCUMENT_SCHEMA_VERSION,
        "job_id": job_id,
        "content_type": "document",
        "document_kind": "research_paper",
        "classification": {"method": "source", "confidence": 1.0},
        "source_profile": "scholarly_html",
        "capabilities": ["html", "math"],
        "primary_source_id": "html",
        "sources": [{
            "source_id": "html", "source_profile": "scholarly_html",
            "capabilities": ["html", "math"], "fingerprint": fingerprint,
            "path": "input/source.html", "mime_type": "text/html",
            "immutable": True,
        }],
        "metadata": {
            "titles": {"original": "Reader", "zh": None},
            "authors": [], "affiliations": [], "author_notes": [],
            "abstract": "", "keywords": [], "lang": "en", "license": "",
            "source_license": "", "rights_notices": [], "identifiers": {},
        },
        "blocks": [{
            "block_id": "blk_intro",
            "parent_id": None,
            "order": 0,
            "kind": "paragraph",
            "text": "Safe body",
            "locator": {
                "html": {
                    "source_id": "html", "source_fingerprint": fingerprint,
                    "dom_path": "/html[1]/body[1]/article[1]/p[1]",
                    "exact": "Safe body",
                },
            },
        }],
        "figures": [],
        "tables": [],
        "references": [],
        "assets": [],
    }


def test_reader_sanitizes_active_content_and_preserves_source_bytes():
    raw = b"""<!doctype html><html><head><script>alert(1)</script></head><body>
    <nav>noise</nav><article><p onclick="steal()">Safe body</p>
    <img src="assets/figure 1.png" onerror="steal()"><a href="javascript:steal()">bad</a>
    <math display="block"><mi>x</mi><mo>=</mo><mn>1</mn></math></article></body></html>"""
    before = bytes(raw)
    rendered = render_document_html(raw, job_id="job_doc", document=_document("job_doc", raw)).decode()

    assert raw == before
    assert "alert(1)" not in rendered
    assert "onclick" not in rendered
    assert "onerror" not in rendered
    assert "javascript:" not in rendered
    assert "noise" not in rendered
    assert 'id="source-blk_intro"' in rendered
    assert "Safe body" in rendered
    assert "<math display=\"block\">" in rendered
    assert "/api/jobs/job_doc/artifact?path=assets%2Ffigure%201.png" in rendered


def test_reader_materializes_translation_artifact_images():
    rendered = render_document_html(
        b'<html><body><img data-artifact="assets/chart.png" alt="chart"></body></html>',
        job_id="job_doc",
    ).decode()

    assert 'src="/api/jobs/job_doc/artifact?path=assets%2Fchart.png"' in rendered


def test_reader_highlights_target_segment_and_exact_text():
    raw = b"<html><body><article><p>Safe body with target term.</p></article></body></html>"
    document = _document("job_doc", raw)
    document["blocks"][0]["text"] = "Safe body with target term."
    document["blocks"][0]["locator"]["html"]["exact"] = "Safe body with target term."

    rendered = render_document_html(
        raw, job_id="job_doc", document=document,
        target_segment="blk_intro", target_exact="target term",
    ).decode()

    assert 'class="flori-source-target"' in rendered
    assert '<mark class="flori-exact-target">target term</mark>' in rendered


@pytest.mark.asyncio
async def test_document_source_route_is_csp_isolated(client, test_config):
    job_id = "job_doc_reader"
    job_dir = test_config.jobs_dir / job_id
    (job_dir / "input").mkdir(parents=True)
    (job_dir / "intermediate").mkdir()
    raw = b"<html><body><article><p>Safe body</p></article></body></html>"
    (job_dir / "input/source.html").write_bytes(raw)
    (job_dir / "intermediate/document.json").write_text(
        json.dumps(_document(job_id, raw)), encoding="utf-8",
    )

    response = await client.get(f"/api/jobs/{job_id}/document/source")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "default-src 'none'" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert 'id="source-blk_intro"' in response.text
    assert (job_dir / "input/source.html").read_bytes() == raw

    targeted = await client.get(
        f"/api/jobs/{job_id}/document/source",
        params={"segment": "blk_intro", "exact": "Safe body"},
    )
    assert targeted.status_code == 200
    assert 'class="flori-source-target"' in targeted.text
    assert '<mark class="flori-exact-target">Safe body</mark>' in targeted.text


@pytest.mark.asyncio
async def test_document_translation_route_sanitizes_generated_html(client, test_config):
    job_id = "job_doc_translation"
    job_dir = test_config.jobs_dir / job_id / "output"
    job_dir.mkdir(parents=True)
    (job_dir / "translated.html").write_text(
        '<html><body><article><h1>译文</h1><script>alert(1)</script></article></body></html>',
        encoding="utf-8",
    )

    response = await client.get(f"/api/jobs/{job_id}/document/translation")

    assert response.status_code == 200
    assert "译文" in response.text
    assert "alert(1)" not in response.text
    assert "default-src 'none'" in response.headers["content-security-policy"]


@pytest.mark.asyncio
async def test_document_source_rejects_invalid_model(client, test_config):
    job_id = "job_doc_invalid"
    job_dir = test_config.jobs_dir / job_id
    (job_dir / "input").mkdir(parents=True)
    (job_dir / "intermediate").mkdir()
    (job_dir / "input/source.html").write_text("<p>x</p>", encoding="utf-8")
    (job_dir / "intermediate/document.json").write_text("{}", encoding="utf-8")

    response = await client.get(f"/api/jobs/{job_id}/document/source")

    assert response.status_code == 422
