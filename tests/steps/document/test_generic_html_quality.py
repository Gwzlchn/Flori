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


def test_content_container_beats_navigation_h1_and_template_date(tmp_path):
    prose = " ".join(["The assignment implements attention, RoPE, and optimization."] * 8)
    source = f"""<!doctype html><html><head>
    <meta name="date" content="2013-01-01"><title>Main Navigation</title></head><body>
    <div class="trigger"><h1>Main Navigation</h1><p>Menu</p></div>
    <div class="page-content"><h1>Assignment 1: Build Your Own LLaMa</h1>
    <p>{prose}</p><h2>Submission Instructions</h2><p>{prose}</p></div>
    </body></html>"""
    job_dir = tmp_path / "multi-h1-course"
    (job_dir / "input").mkdir(parents=True)
    (job_dir / "input" / "source.html").write_text(source, encoding="utf-8")
    (job_dir / "input" / "metadata.json").write_text(
        '{"title":"Main Navigation","date":"2013-01-01"}', encoding="utf-8",
    )

    document, quality = parse_generic_html(job_dir, {
        "id": job_dir.name,
        "content_type": "document",
        "document_kind": "article",
        "url": "https://cmu.example/assignments/assignment1",
    })

    assert document["metadata"]["titles"]["original"] == (
        "Assignment 1: Build Your Own LLaMa"
    )
    assert document["metadata"]["published_at"] == ""
    assert quality["metrics"]["body_candidate"] == "content"
    assert "body_boundary_uncertain" not in quality["reasons"]
    assert "metadata_title_conflict" in quality["reasons"]


def test_research_paper_landing_page_with_pdf_link_is_rejected(tmp_path):
    source = """<!doctype html><html><head>
    <meta name="citation_pdf_url" content="https://example.org/report.pdf">
    </head><body><main><h1>Research report</h1>
    <p>This page contains bibliographic metadata and an abstract, but not the report body.
    The complete publication is available only from the linked PDF download.</p>
    <a href="https://example.org/report.pdf">Download full text</a>
    </main></body></html>"""
    job_dir = tmp_path / "metadata-only-paper"
    (job_dir / "input").mkdir(parents=True)
    (job_dir / "input" / "source.html").write_text(source, encoding="utf-8")

    _document, quality = parse_generic_html(job_dir, {
        "id": job_dir.name,
        "content_type": "document",
        "document_kind": "research_paper",
        "url": "https://example.org/report",
    })

    assert quality["status"] == "rejected"
    assert "full_text_unavailable" in quality["reasons"]
