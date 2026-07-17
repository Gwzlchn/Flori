"""验证 generic_html 对不完整来源 fail-closed。"""

from __future__ import annotations

from pathlib import Path

from steps.document.adapters.generic_html import parse_generic_html


FIXTURES = Path(__file__).parent / "fixtures"


def _parse(tmp_path: Path, fixture: str, **job_overrides):
    job_dir = tmp_path / fixture.removesuffix(".html")
    (job_dir / "input").mkdir(parents=True)
    (job_dir / "input" / "source.html").write_bytes((FIXTURES / fixture).read_bytes())
    job = {
        "id": job_dir.name,
        "content_type": "document",
        "document_kind": "article",
        "url": "https://example.com/source",
        "final_url": "https://example.com/final",
        **job_overrides,
    }
    return parse_generic_html(job_dir, job)


def test_long_paywall_teaser_is_rejected(tmp_path):
    _, quality = _parse(tmp_path, "paywall_article.html")

    assert quality["status"] == "rejected"
    assert "paywall_detected" in quality["reasons"]
    assert quality["metrics"]["body_chars"] >= 200


def test_dynamic_shell_is_rejected(tmp_path):
    document, quality = _parse(tmp_path, "dynamic_shell.html")

    assert document["blocks"]
    assert quality["status"] == "rejected"
    assert "dynamic_content_unavailable" in quality["reasons"]
    assert "body_too_short" in quality["reasons"] or "body_missing" in quality["reasons"]


def test_declared_body_coverage_rejects_severe_truncation(tmp_path):
    _, quality = _parse(tmp_path, "truncated_article.html")

    assert quality["status"] == "rejected"
    assert "severe_truncation" in quality["reasons"]
    assert quality["metrics"]["extraction_coverage"] < 0.5


def test_canonical_conflict_is_degraded_not_silently_resolved(tmp_path):
    source = (FIXTURES / "generic_article.html").read_text(encoding="utf-8")
    source = source.replace(
        'content="https://example.com/posts/document-model"',
        'content="https://example.com/posts/another-copy"',
    )
    job_dir = tmp_path / "canonical-conflict"
    (job_dir / "input").mkdir(parents=True)
    (job_dir / "input" / "source.html").write_text(source, encoding="utf-8")

    document, quality = parse_generic_html(job_dir, {
        "id": job_dir.name,
        "content_type": "document",
        "document_kind": "article",
        "url": "https://example.com/request",
        "final_url": "https://example.com/posts/document-model",
    })

    assert document["sources"][0]["canonical_url"] == "https://example.com/posts/document-model"
    assert quality["status"] == "degraded"
    assert "canonical_conflict" in quality["reasons"]


def test_declared_source_fingerprint_mismatch_is_rejected(tmp_path):
    _, quality = _parse(
        tmp_path,
        "generic_article.html",
        source_fingerprint="sha256:" + "0" * 64,
    )

    assert quality["status"] == "rejected"
    assert "source_fingerprint_mismatch" in quality["reasons"]


def test_unsafe_embed_is_sanitized_and_degrades_quality(tmp_path):
    source = """<!doctype html><html><body><article>
    <h1>Unsafe embed</h1>
    <p>This sufficiently long article body explains why an untrusted embed URL must never be
    executed or copied into a downstream renderer. The remaining prose makes the extraction a
    valid document while the unsafe media remains visible as an explicit sanitized descriptor.</p>
    <iframe src="javascript:alert(1)" title="Untrusted player"></iframe>
    </article></body></html>"""
    job_dir = tmp_path / "unsafe-embed"
    (job_dir / "input").mkdir(parents=True)
    (job_dir / "input" / "source.html").write_text(source, encoding="utf-8")

    document, quality = parse_generic_html(job_dir, {
        "id": job_dir.name,
        "content_type": "document",
        "document_kind": "article",
        "url": "https://example.com/source",
    })

    embed = next(block["embed"] for block in document["blocks"] if block["kind"] == "embed")
    assert embed["source_url"] is None
    assert embed["allow_script_execution"] is False
    assert quality["status"] == "degraded"
    assert "unsafe_embed_ignored" in quality["reasons"]
    assert quality["metrics"]["unsafe_embeds"] == 1
