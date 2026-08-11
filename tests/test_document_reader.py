"""验证 Document 原文阅读副本的隔离、锚点与资源重写。"""

from __future__ import annotations

import hashlib
import json
import mimetypes

import pytest

from api.routes import notes as notes_routes
from api.services import document_reader as document_reader_service
from shared import scholarly_html_snapshot as snapshot_service
from api.services.document_reader import (
    DocumentReaderError,
    DocumentReaderLimitError,
    build_snapshot_css,
    document_html_headers,
    render_document_html,
    render_document_model_html,
)
from shared.document_contract import DOCUMENT_SCHEMA_VERSION
from shared.scholarly_html_snapshot import (
    ScholarlyHtmlSnapshotLimitError,
    canonical_snapshot_bytes,
    sha256_digest,
)
from shared.step_output_commit import StepOutput, build_step_manifest


def _fingerprint(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _write_download_manifest(job_dir, job_id: str, paths: list[str]) -> None:
    outputs = []
    for path in sorted(paths, key=lambda value: value.encode("utf-8")):
        data = (job_dir / path).read_bytes()
        outputs.append(StepOutput(
            path=path,
            job_rel=path,
            size_bytes=len(data),
            sha256=sha256_digest(data),
            media_type=mimetypes.guess_type(path)[0],
        ))
    _, encoded, _ = build_step_manifest(
        job_id=job_id,
        scope_key="job",
        step="01_download",
        part_index=None,
        exec_id="exec_snapshot",
        job_generation=1,
        attempt=1,
        started_at="2026-08-11T01:00:00Z",
        committed_at="2026-08-11T01:01:00Z",
        duration_sec=60,
        input_fingerprints={"job": "sha256:" + "1" * 64},
        definition_digest="sha256:" + "2" * 64,
        outputs=outputs,
        producer={
            "flori_version": "2.8.2",
            "build_sha": None,
            "worker_id": "test",
            "runner": "subprocess",
            "image": None,
            "image_digest": None,
            "tool_versions": {},
        },
    )
    target = job_dir / ".flori/steps/01_download/manifest.json"
    target.parent.mkdir(parents=True)
    target.write_bytes(encoded)


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


def test_snapshot_stylesheets_share_one_sanitized_output_budget(monkeypatch):
    monkeypatch.setattr(
        document_reader_service, "SCHOLARLY_HTML_SANITIZED_CSS_MAX_BYTES", 32,
    )
    resources = {
        "input/html_assets/a.css": {
            "kind": "stylesheet",
            "request_url": "https://cdn.example/a.css",
            "source_url": "https://cdn.example/a.css",
        },
        "input/html_assets/b.css": {
            "kind": "stylesheet",
            "request_url": "https://cdn.example/b.css",
            "source_url": "https://cdn.example/b.css",
        },
    }
    stylesheets = [
        (resources["input/html_assets/a.css"], b".alpha{color:red}"),
        (resources["input/html_assets/b.css"], b".beta{background:blue}"),
    ]

    with pytest.raises(ScholarlyHtmlSnapshotLimitError, match="combined sanitized"):
        build_snapshot_css(stylesheets, job_id="job_budget", resources=resources)


def test_snapshot_stylesheets_share_rule_and_declaration_budgets(monkeypatch):
    monkeypatch.setattr(snapshot_service, "SCHOLARLY_HTML_CSS_MAX_RULES", 1)
    monkeypatch.setattr(snapshot_service, "SCHOLARLY_HTML_CSS_MAX_DECLARATIONS", 1)
    resources = {
        "input/html_assets/a.css": {
            "kind": "stylesheet",
            "request_url": "https://cdn.example/a.css",
            "source_url": "https://cdn.example/a.css",
        },
        "input/html_assets/b.css": {
            "kind": "stylesheet",
            "request_url": "https://cdn.example/b.css",
            "source_url": "https://cdn.example/b.css",
        },
    }

    with pytest.raises(ScholarlyHtmlSnapshotLimitError, match="rule count"):
        build_snapshot_css(
            [
                (resources["input/html_assets/a.css"], b".a{color:red}"),
                (resources["input/html_assets/b.css"], b".b{color:blue}"),
            ],
            job_id="job_budget",
            resources=resources,
        )

    monkeypatch.setattr(snapshot_service, "SCHOLARLY_HTML_CSS_MAX_RULES", 10)
    with pytest.raises(ScholarlyHtmlSnapshotLimitError, match="declaration count"):
        build_snapshot_css(
            [(
                resources["input/html_assets/a.css"],
                b".a{color:red;background:blue}",
            )],
            job_id="job_budget",
            resources=resources,
        )


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


def test_source_anchors_preserve_inline_math_flow_in_both_reader_styles():
    raw = b"<html><body><article><p>Before <math><mi>x</mi></math> after.</p></article></body></html>"
    document = _document("job_inline_anchor", raw)
    document["blocks"].append({
        "block_id": "blk_math",
        "parent_id": "blk_intro",
        "order": 1,
        "kind": "math",
        "text": "x",
        "locator": {
            "html": {
                "source_id": "html",
                "source_fingerprint": _fingerprint(raw),
                "dom_path": "/html[1]/body[1]/article[1]/p[1]/math[1]",
                "exact": "x",
            },
        },
    })

    for snapshot_css in (None, ""):
        rendered = render_document_html(
            raw,
            job_id="job_inline_anchor",
            document=document,
            snapshot_css=snapshot_css,
        ).decode()

        assert (
            '<span id="source-blk_math" class="flori-source-anchor" '
            'style="display:inline!important"></span><math>'
            in rendered
        )
        assert ".flori-source-anchor{display:inline!important;" in rendered
        assert ".flori-source-anchor{display:block;" not in rendered


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


def test_reader_only_materializes_source_anchors_from_verified_document_model():
    raw = b"""<html><body><article>
    <p id="source-blk_intro" data-source-segment="blk_intro"
       class="flori-source-anchor flori-source-target flori-exact-target">fake target term</p>
    <p>real target term</p></article></body></html>"""
    document = _document("job_doc", raw)
    document["blocks"][0]["text"] = "real target term"
    document["blocks"][0]["locator"]["html"]["dom_path"] = "/html[1]/body[1]/article[1]/p[2]"
    document["blocks"][0]["locator"]["html"]["exact"] = "real target term"

    rendered = render_document_html(
        raw, job_id="job_doc", document=document,
        target_segment="blk_intro", target_exact="target term",
    ).decode()

    assert rendered.count('id="source-blk_intro"') == 1
    assert rendered.count('class="flori-source-anchor"') == 1
    assert rendered.count('class="flori-source-target"') == 1
    assert rendered.count('class="flori-exact-target"') == 1
    assert '<mark class="flori-exact-target">target term</mark>' in rendered
    assert 'data-source-segment="blk_intro"' not in rendered


def test_translation_anchor_ids_must_be_verified_and_unique():
    allowed = frozenset({"blk_intro"})
    rendered = render_document_html(
        b'<p data-source-segment="unknown">fake</p><p data-source-segment="blk_intro">real</p>',
        job_id="job_doc", embedded_anchor_ids=allowed,
        target_segment="blk_intro", target_exact="real",
    ).decode()

    assert rendered.count('id="source-blk_intro"') == 1
    assert 'data-source-segment=' not in rendered
    assert '<mark class="flori-exact-target">real</mark>' in rendered
    with pytest.raises(DocumentReaderError, match="duplicate embedded"):
        render_document_html(
            b'<p data-source-segment="blk_intro">one</p><p data-source-segment="blk_intro">two</p>',
            job_id="job_doc", embedded_anchor_ids=allowed,
        )


def test_reader_preserves_bounded_latexml_layout_without_upstream_css():
    raw = b"""<html><head>
    <link rel="stylesheet" href="https://evil.example/paper.css">
    <style>@import url(https://evil.example/import.css);.ltx_document{position:fixed}</style>
    </head><body><article class="ltx_document ltx_authors_1line"
      style="width:95%;position:fixed;background:url(https://evil.example/x)">
      <h1 class="ltx_title_document">Paper title</h1>
      <div class="ltx_authors"><span class="ltx_creator ltx_role_author">Ada</span></div>
      <figure class="ltx_figure" style="width:48%;margin-left:auto;margin-right:auto;text-align:center">
        <img class="ltx_graphics" src="assets/figure.png" width="640" height="480"
          style="width:100%;height:auto;filter:url(https://evil.example/filter)">
        <figcaption class="ltx_caption">Figure 1</figcaption>
      </figure>
      <table class="ltx_equation"><tr><td><math display="block"><mi>x</mi></math></td></tr></table>
    </article></body></html>"""

    rendered = render_document_html(raw, job_id="job_layout").decode()

    assert 'class="ltx_document ltx_authors_1line"' in rendered
    assert '.ltx_figure_panel{display:inline-block' in rendered
    assert 'style="width:95%"' in rendered
    assert 'style="width:48%;margin-left:auto;margin-right:auto;text-align:center"' in rendered
    assert 'style="width:100%;height:auto"' in rendered
    assert 'width="640" height="480"' in rendered
    assert '/api/jobs/job_layout/artifact?path=assets%2Ffigure.png' in rendered
    assert "@import" not in rendered
    assert "evil.example" not in rendered
    assert "position:fixed" not in rendered
    assert "filter:" not in rendered


def test_reader_rejects_svg_escape_remote_resources_and_ambiguous_styles():
    raw = b"""<html><body>
    <custom-widget/><p style="display:none;width:50%;height:9999%;margin-left:-9999px">After custom</p>
    <svg><foreignObject><p>foreign HTML</p></foreignObject>
      <use href="https://evil.example/sprite.svg#x"></use>
      <image href="assets/local.svg" width="320"></image>
    </svg>
    <img id="svg-data" src="data:image/svg+xml,&lt;svg/&gt;">
    <img id="png-data" src="data:image/png;base64,AA==">
    <img id="private-job-file" src="output/ai_logs/05_smart.jsonl">
    <a href="https://example.org/paper">citation</a>
    </body></html>"""

    rendered = render_document_html(raw, job_id="job_svg").decode()

    assert "custom-widget" not in rendered
    assert "After custom" in rendered
    assert 'style="width:50%"' in rendered
    assert "display:none" not in rendered
    assert "9999%" not in rendered
    assert "-9999px" not in rendered
    assert "foreignobject" not in rendered.casefold()
    assert "foreign HTML" not in rendered
    assert "evil.example" not in rendered
    assert '/api/jobs/job_svg/artifact?path=assets%2Flocal.svg' in rendered
    assert 'id="svg-data" src=' not in rendered
    assert 'id="png-data" src="data:image/png;base64,AA=="' in rendered
    assert 'id="private-job-file" src=' not in rendered
    assert 'href="https://example.org/paper" target="_blank" rel="noopener noreferrer"' in rendered


def test_reader_rejects_css_escaped_svg_urls_and_preserves_local_paint():
    raw = br"""<svg><defs><linearGradient id="g"></linearGradient></defs>
    <rect id="escaped" fill="u\72l(https://evil.example/p.svg#x)"></rect>
    <rect id="remote" stroke="URL(https://evil.example/s.svg#x)"></rect>
    <rect id="local" fill="url(#g)" stroke="#123abc"></rect>
    <path marker-start="url(#g)" marker-end="none"></path>
    <title>Safe SVG title</title></svg>"""

    rendered = render_document_html(raw, job_id="job_svg").decode()

    assert "evil.example" not in rendered
    assert 'id="escaped" fill=' not in rendered
    assert 'id="remote" stroke=' not in rendered
    assert 'id="local" fill="url(#g)" stroke="#123abc"' in rendered
    assert 'marker-start="url(#g)" marker-end="none"' in rendered
    assert "<title>Safe SVG title</title>" in rendered


def test_reader_enforces_structural_and_output_budgets(monkeypatch):
    monkeypatch.setattr(document_reader_service, "DOCUMENT_HTML_MAX_DEPTH", 4)
    with pytest.raises(DocumentReaderLimitError, match="nesting"):
        render_document_html(b"<div><div><div><div><div>x", job_id="job_deep")

    monkeypatch.setattr(document_reader_service, "DOCUMENT_HTML_MAX_DEPTH", 128)
    monkeypatch.setattr(document_reader_service, "DOCUMENT_HTML_MAX_NODES", 3)
    with pytest.raises(DocumentReaderLimitError, match="node count"):
        render_document_html(b"<p>1</p><p>2</p><p>3</p><p>4</p>", job_id="job_nodes")

    monkeypatch.setattr(document_reader_service, "DOCUMENT_HTML_MAX_NODES", 50_000)
    monkeypatch.setattr(document_reader_service, "DOCUMENT_HTML_MAX_OUTPUT_BYTES", 16)
    with pytest.raises(DocumentReaderLimitError, match="output"):
        render_document_html(b"<p>&lt;&lt;&lt;&lt;&lt;</p>", job_id="job_output")


def test_raw_reader_output_budget_counts_wrapper_and_chunks_escaping(monkeypatch):
    raw = b"<p>" + b"&" * 256 + b"</p>"
    original_escape = document_reader_service.html.escape
    escaped_input_sizes: list[int] = []

    def observed_escape(value, *, quote=True):
        escaped_input_sizes.append(len(value))
        return original_escape(value, quote=quote)

    monkeypatch.setattr(document_reader_service, "_ESCAPE_CHUNK_CHARS", 16)
    monkeypatch.setattr(document_reader_service.html, "escape", observed_escape)
    rendered = render_document_html(raw, job_id="job_chunked_output")

    assert max(escaped_input_sizes) <= 16
    monkeypatch.setattr(
        document_reader_service, "DOCUMENT_HTML_MAX_OUTPUT_BYTES", len(rendered),
    )
    assert render_document_html(raw, job_id="job_exact_output") == rendered
    monkeypatch.setattr(
        document_reader_service, "DOCUMENT_HTML_MAX_OUTPUT_BYTES", len(rendered) - 1,
    )
    with pytest.raises(DocumentReaderLimitError, match="output"):
        render_document_html(raw, job_id="job_over_output")


def test_reader_rejects_tag_attribute_amplification_before_escaping(monkeypatch):
    escaped_values: list[str] = []
    original_escape = document_reader_service.html.escape

    def observed_escape(value, *, quote=True):
        escaped_values.append(value)
        return original_escape(value, quote=quote)

    monkeypatch.setattr(
        document_reader_service, "DOCUMENT_HTML_MAX_TAG_ATTRIBUTE_BYTES", 64,
    )
    monkeypatch.setattr(document_reader_service.html, "escape", observed_escape)
    attributes = " ".join(
        f'data-value-{index}="{"&" * 16}"' for index in range(8)
    )

    with pytest.raises(DocumentReaderLimitError, match="tag attributes"):
        render_document_html(
            f"<p {attributes}>body</p>".encode(), job_id="job_attr_budget",
        )

    assert escaped_values == []


def test_model_reader_output_budget_counts_complete_page(monkeypatch):
    raw = b"<p>Safe body</p>"
    document = _document("job_model_budget", raw)
    document["blocks"][0]["text"] = "&" * 256
    rendered = render_document_model_html(document, job_id="job_model_budget")

    monkeypatch.setattr(
        document_reader_service, "DOCUMENT_HTML_MAX_OUTPUT_BYTES", len(rendered),
    )
    assert render_document_model_html(document, job_id="job_model_budget") == rendered
    monkeypatch.setattr(
        document_reader_service, "DOCUMENT_HTML_MAX_OUTPUT_BYTES", len(rendered) - 1,
    )
    with pytest.raises(DocumentReaderLimitError, match="output"):
        render_document_model_html(document, job_id="job_model_budget")


def test_reader_drops_layout_amplification_values():
    rendered = render_document_html(
        b'<div style="min-height:4096px;height:4097px;aspect-ratio:1/9999;width:90%">x</div>',
        job_id="job_layout_budget",
    ).decode()

    assert 'style="width:90%"' in rendered
    assert "min-height" not in rendered
    assert "height:4097px" not in rendered
    assert "aspect-ratio" not in rendered


def test_document_reader_csp_explicitly_closes_execution_and_network_surfaces():
    headers = document_html_headers()
    csp = headers["Content-Security-Policy"]

    for directive in (
        "default-src 'none'", "script-src 'none'", "connect-src 'none'",
        "object-src 'none'", "frame-src 'none'", "worker-src 'none'",
        "media-src 'none'", "base-uri 'none'", "form-action 'none'",
        "frame-ancestors 'self'", "sandbox allow-same-origin allow-popups",
    ):
        assert directive in csp
    assert headers["Cross-Origin-Resource-Policy"] == "same-origin"
    assert "camera=()" in headers["Permissions-Policy"]


def test_model_reader_renders_metadata_and_drops_site_dom():
    raw = b"<html><body><nav>Site navigation</nav><article><p>Safe body</p></article><footer>Site footer</footer></body></html>"
    document = _document("job_doc", raw)
    document["metadata"].update({
        "titles": {"original": "Clean article", "zh": None},
        "authors": [{"name": "Ada Reader"}],
        "published_at": "2026-07-19",
        "abstract": "A clean abstract.",
    })
    document["blocks"].append({
        "block_id": "blk_figure",
        "parent_id": None,
        "order": 1,
        "kind": "figure",
        "text": "Figure 1",
        "locator": document["blocks"][0]["locator"],
    })
    document["figures"] = [{
        "block_id": "blk_figure",
        "label": "Figure 1",
        "caption": "Local chart",
        "media": [{"artifact": "assets/chart.png", "alt": "Chart alt"}],
    }]

    rendered = render_document_model_html(document, job_id="job_doc").decode()

    assert "Clean article" in rendered
    assert "Ada Reader" in rendered
    assert "2026-07-19" in rendered
    assert "A clean abstract." in rendered
    assert "Safe body" in rendered
    assert "Site navigation" not in rendered
    assert "Site footer" not in rendered
    assert 'id="source-blk_intro"' in rendered
    assert '/api/jobs/job_doc/artifact?path=assets%2Fchart.png' in rendered
    assert 'alt="Chart alt"' in rendered


@pytest.mark.asyncio
async def test_document_source_route_is_csp_isolated(client, test_config):
    job_id = "job_doc_reader"
    job_dir = test_config.jobs_dir / job_id
    (job_dir / "input").mkdir(parents=True)
    (job_dir / "intermediate").mkdir()
    raw = b"<html><body><nav>Site navigation</nav><article><p>Raw DOM only</p></article><footer>Site footer</footer></body></html>"
    (job_dir / "input/source.html").write_bytes(raw)
    document = _document(job_id, raw)
    document["source_profile"] = "generic_html"
    document["capabilities"] = ["html", "embedded_media"]
    document["sources"][0]["source_profile"] = "generic_html"
    document["sources"][0]["capabilities"] = ["html", "embedded_media"]
    (job_dir / "intermediate/document.json").write_text(
        json.dumps(document), encoding="utf-8",
    )

    response = await client.get(f"/api/jobs/{job_id}/document/source")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "default-src 'none'" in response.headers["content-security-policy"]
    assert "sandbox allow-same-origin" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cross-origin-resource-policy"] == "same-origin"
    assert 'id="source-blk_intro"' in response.text
    assert "Site navigation" not in response.text
    assert "Site footer" not in response.text
    assert "Raw DOM only" not in response.text
    assert (job_dir / "input/source.html").read_bytes() == raw

    targeted = await client.get(
        f"/api/jobs/{job_id}/document/source",
        params={"segment": "blk_intro", "exact": "Safe body"},
    )
    assert targeted.status_code == 200
    assert 'class="flori-source-target"' in targeted.text
    assert '<mark class="flori-exact-target">Safe body</mark>' in targeted.text

    (job_dir / "input/source.html").write_text(
        "<html><body><p>tampered source</p></body></html>", encoding="utf-8",
    )
    tampered = await client.get(f"/api/jobs/{job_id}/document/source")
    assert tampered.status_code == 422
    assert tampered.json()["message"] == "document HTML does not match document model"


@pytest.mark.asyncio
async def test_scholarly_html_source_keeps_sanitized_mathml(client, test_config):
    job_id = "job_scholarly_reader"
    job_dir = test_config.jobs_dir / job_id
    (job_dir / "input").mkdir(parents=True)
    (job_dir / "intermediate").mkdir()
    raw = b'<html><body><article><p>Safe body</p><math><mi>x</mi><mo>=</mo><mn>1</mn></math></article></body></html>'
    (job_dir / "input/source.html").write_bytes(raw)
    (job_dir / "intermediate/document.json").write_text(
        json.dumps(_document(job_id, raw)), encoding="utf-8",
    )

    response = await client.get(f"/api/jobs/{job_id}/document/source")

    assert response.status_code == 200
    assert '<math><mi>x</mi><mo>=</mo><mn>1</mn></math>' in response.text


@pytest.mark.asyncio
async def test_scholarly_html_snapshot_restores_local_css_fonts_and_images(
    client, test_config, monkeypatch,
):
    job_id = "job_scholarly_snapshot"
    job_dir = test_config.jobs_dir / job_id
    (job_dir / "input/html_assets").mkdir(parents=True)
    (job_dir / "intermediate").mkdir()
    css_path = "input/html_assets/paper.css"
    font_path = "input/html_assets/paper.woff2"
    image_path = "input/html_assets/figure.png"
    css = b"""
    @font-face{font-family:Paper;src:url(https://cdn.example/paper.woff2)}
    .ltx_document{max-width:80rem;margin:0 auto;font-family:Paper;background:url(https://evil.example/pixel)}
    """
    font = b"wOF2" + b"f" * 32
    image = b"\x89PNG\r\n\x1a\n" + b"i" * 32
    raw = (
        '<html><head><link rel="stylesheet" href="https://cdn.example/paper.css"></head>'
        f'<body><article class="ltx_document"><p>Safe body</p><img src="{image_path}"></article></body></html>'
    ).encode()
    (job_dir / "input/source.html").write_bytes(raw)
    (job_dir / css_path).write_bytes(css)
    (job_dir / font_path).write_bytes(font)
    (job_dir / image_path).write_bytes(image)
    snapshot = {
        "format": "flori-scholarly-html-snapshot",
        "format_version": 1,
        "job_id": job_id,
        "provider": "ar5iv",
        "document_url": "https://ar5iv.labs.arxiv.org/html/2305.14314",
        "html": {
            "path": "input/source.html",
            "sha256": sha256_digest(raw),
            "size_bytes": len(raw),
            "media_type": "text/html",
        },
        "stylesheets": [css_path],
        "resources": [
            {
                "kind": "stylesheet", "path": css_path,
                "request_url": "https://cdn.example/paper.css",
                "source_url": "https://cdn.example/paper.css",
                "sha256": sha256_digest(css), "size_bytes": len(css),
                "media_type": "text/css",
            },
            {
                "kind": "font", "path": font_path,
                "request_url": "https://cdn.example/paper.woff2",
                "source_url": "https://cdn.example/paper.woff2",
                "sha256": sha256_digest(font), "size_bytes": len(font),
                "media_type": "font/woff2",
            },
            {
                "kind": "image", "path": image_path,
                "request_url": "https://cdn.example/figure.png",
                "source_url": "https://cdn.example/figure.png",
                "sha256": sha256_digest(image), "size_bytes": len(image),
                "media_type": "image/png",
            },
        ],
    }
    snapshot["resources"].sort(key=lambda item: item["path"].encode("utf-8"))
    snapshot_raw = canonical_snapshot_bytes(snapshot)
    (job_dir / "input/html_snapshot.json").write_bytes(snapshot_raw)
    (job_dir / "intermediate/document.json").write_text(
        json.dumps(_document(job_id, raw)), encoding="utf-8",
    )
    paths = [
        "input/source.html", "input/html_snapshot.json", css_path, font_path, image_path,
    ]
    _write_download_manifest(job_dir, job_id, paths)

    response = await client.get(f"/api/jobs/{job_id}/document/source")

    assert response.status_code == 200
    assert ".ltx_document{max-width:80rem" in response.text
    assert "max-width:980px" not in response.text
    assert "evil.example" not in response.text
    assert f"/api/jobs/{job_id}/document/resource?path=input%2Fhtml_assets%2Fpaper.woff2" in response.text
    assert f'src="/api/jobs/{job_id}/document/resource?path=input%2Fhtml_assets%2Ffigure.png"' in response.text
    assert "script-src 'none'" in response.headers["content-security-policy"]

    bounded_reads: list[str] = []
    original_bounded_read = notes_routes.read_file_bounded

    async def track_bounded_read(storage, requested_job_id, rel_path, limit):
        bounded_reads.append(rel_path)
        return await original_bounded_read(storage, requested_job_id, rel_path, limit)

    monkeypatch.setattr(notes_routes, "read_file_bounded", track_bounded_read)
    font_response = await client.get(
        f"/api/jobs/{job_id}/document/resource", params={"path": font_path},
    )
    image_response = await client.get(
        f"/api/jobs/{job_id}/document/resource", params={"path": image_path},
    )
    css_response = await client.get(
        f"/api/jobs/{job_id}/document/resource", params={"path": css_path},
    )
    assert font_response.status_code == image_response.status_code == 200
    assert font_response.content == font
    assert image_response.content == image
    assert css_response.status_code == 404
    assert "input/source.html" not in bounded_reads

    (job_dir / font_path).write_bytes(b"wOF2tampered")
    tampered = await client.get(
        f"/api/jobs/{job_id}/document/resource", params={"path": font_path},
    )
    assert tampered.status_code == 422

    (job_dir / css_path).write_bytes(b".tampered{color:red}")
    tampered_css = await client.get(f"/api/jobs/{job_id}/document/source")
    assert tampered_css.status_code == 422


@pytest.mark.asyncio
async def test_scholarly_snapshot_without_download_manifest_never_falls_back_online(
    client, test_config,
):
    job_id = "job_unbound_scholarly_snapshot"
    job_dir = test_config.jobs_dir / job_id
    (job_dir / "input").mkdir(parents=True)
    (job_dir / "intermediate").mkdir()
    raw = b"<html><body><article><p>Safe body</p></article></body></html>"
    (job_dir / "input/source.html").write_bytes(raw)
    (job_dir / "input/html_snapshot.json").write_text("{}", encoding="utf-8")
    (job_dir / "intermediate/document.json").write_text(
        json.dumps(_document(job_id, raw)), encoding="utf-8",
    )

    response = await client.get(f"/api/jobs/{job_id}/document/source")

    assert response.status_code == 422
    assert "download manifest" in response.json()["message"]


@pytest.mark.asyncio
async def test_document_translation_route_sanitizes_generated_html(client, test_config):
    job_id = "job_doc_translation"
    job_dir = test_config.jobs_dir / job_id
    (job_dir / "output").mkdir(parents=True)
    (job_dir / "intermediate").mkdir()
    raw = b"<html><body><article><p>Safe body</p></article></body></html>"
    (job_dir / "intermediate/document.json").write_text(
        json.dumps(_document(job_id, raw)), encoding="utf-8",
    )
    (job_dir / "output/translated.html").write_text(
        '<html><body><article><h1>译文</h1><p data-source-segment="blk_intro">目标译文</p>'
        '<script>alert(1)</script></article></body></html>',
        encoding="utf-8",
    )

    response = await client.get(
        f"/api/jobs/{job_id}/document/translation",
        params={"segment": "blk_intro", "exact": "目标译文"},
    )

    assert response.status_code == 200
    assert "译文" in response.text
    assert "alert(1)" not in response.text
    assert response.text.count('id="source-blk_intro"') == 1
    assert '<mark class="flori-exact-target">目标译文</mark>' in response.text
    assert "data-source-segment" not in response.text
    assert "default-src 'none'" in response.headers["content-security-policy"]

    (job_dir / "output/translated.html").write_text(
        '<p data-source-segment="blk_intro">一</p><p data-source-segment="blk_intro">二</p>',
        encoding="utf-8",
    )
    duplicate = await client.get(f"/api/jobs/{job_id}/document/translation")
    assert duplicate.status_code == 422


@pytest.mark.asyncio
async def test_document_source_maps_parser_budget_to_413(client, test_config, monkeypatch):
    job_id = "job_doc_reader_limit"
    job_dir = test_config.jobs_dir / job_id
    (job_dir / "input").mkdir(parents=True)
    (job_dir / "intermediate").mkdir()
    raw = b"<html><body><article><div><div><p>deep</p></div></div></article></body></html>"
    (job_dir / "input/source.html").write_bytes(raw)
    (job_dir / "intermediate/document.json").write_text(
        json.dumps(_document(job_id, raw)), encoding="utf-8",
    )
    monkeypatch.setattr(document_reader_service, "DOCUMENT_HTML_MAX_DEPTH", 2)

    response = await client.get(f"/api/jobs/{job_id}/document/source")

    assert response.status_code == 413
    assert response.json()["message"] == "document HTML nesting exceeds reader limit"


@pytest.mark.asyncio
async def test_all_document_reader_paths_map_output_budget_to_413(
    client, test_config, monkeypatch,
):
    raw = b"<html><body><article><p>Safe body</p></article></body></html>"
    for job_id, profile in (
        ("job_generic_output_limit", "generic_html"),
        ("job_scholarly_output_limit", "scholarly_html"),
        ("job_translation_output_limit", "translation"),
    ):
        job_dir = test_config.jobs_dir / job_id
        (job_dir / "input").mkdir(parents=True)
        (job_dir / "intermediate").mkdir()
        (job_dir / "output").mkdir()
        (job_dir / "input/source.html").write_bytes(raw)
        document = _document(job_id, raw)
        if profile == "generic_html":
            document["source_profile"] = "generic_html"
            document["capabilities"] = ["html", "embedded_media"]
            document["sources"][0]["source_profile"] = "generic_html"
            document["sources"][0]["capabilities"] = ["html", "embedded_media"]
        (job_dir / "intermediate/document.json").write_text(
            json.dumps(document), encoding="utf-8",
        )
        (job_dir / "output/translated.html").write_bytes(raw)

    monkeypatch.setattr(
        document_reader_service, "DOCUMENT_HTML_MAX_OUTPUT_BYTES", 128,
    )

    responses = [
        await client.get("/api/jobs/job_generic_output_limit/document/source"),
        await client.get("/api/jobs/job_scholarly_output_limit/document/source"),
        await client.get("/api/jobs/job_translation_output_limit/document/translation"),
    ]

    assert [response.status_code for response in responses] == [413, 413, 413]
    assert all(
        response.json()["message"] == "document HTML output exceeds reader limit"
        for response in responses
    )


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
