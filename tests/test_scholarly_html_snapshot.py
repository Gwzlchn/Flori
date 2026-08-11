"""验证论文HTML离线快照Schema与CSS净化边界。"""

from __future__ import annotations

import pytest

from shared import scholarly_html_snapshot as snapshot_service
from shared.scholarly_html_snapshot import (
    ScholarlyHtmlSnapshotError,
    ScholarlyHtmlSnapshotLimitError,
    iter_css_urls,
    sanitize_snapshot_stylesheet,
    sha256_digest,
    validate_scholarly_html_snapshot,
)


def _snapshot(source: bytes) -> dict:
    css = b'@font-face{font-family:"Paper";src:url(https://cdn.example/paper.woff2)}'
    font = b"wOF2" + b"x" * 32
    image = b"\x89PNG\r\n\x1a\n" + b"x" * 32
    return {
        "format": "flori-scholarly-html-snapshot",
        "format_version": 1,
        "job_id": "job_paper",
        "provider": "ar5iv",
        "document_url": "https://ar5iv.labs.arxiv.org/html/2305.14314",
        "html": {
            "path": "input/source.html",
            "sha256": sha256_digest(source),
            "size_bytes": len(source),
            "media_type": "text/html",
        },
        "stylesheets": ["input/html_assets/style.css"],
        "resources": [
            {
                "kind": "image",
                "path": "input/html_assets/image.png",
                "request_url": "https://ar5iv.labs.arxiv.org/html/2305.14314v1/x1.png",
                "source_url": "https://ar5iv.labs.arxiv.org/html/2305.14314v1/x1.png",
                "sha256": sha256_digest(image),
                "size_bytes": len(image),
                "media_type": "image/png",
            },
            {
                "kind": "font",
                "path": "input/html_assets/paper.woff2",
                "request_url": "https://cdn.example/paper.woff2",
                "source_url": "https://cdn.example/paper.woff2",
                "sha256": sha256_digest(font),
                "size_bytes": len(font),
                "media_type": "font/woff2",
            },
            {
                "kind": "stylesheet",
                "path": "input/html_assets/style.css",
                "request_url": "https://ar5iv.labs.arxiv.org/assets/ar5iv.css",
                "source_url": "https://ar5iv.labs.arxiv.org/assets/ar5iv.css",
                "sha256": sha256_digest(css),
                "size_bytes": len(css),
                "media_type": "text/css",
            },
        ],
    }


def test_snapshot_binds_html_identity_and_sorted_resources():
    source = b"<html><body><article>paper</article></body></html>"
    validated = validate_scholarly_html_snapshot(
        _snapshot(source), expected_job_id="job_paper", source_html=source,
    )

    assert validated["provider"] == "ar5iv"
    assert validated["stylesheets"] == ["input/html_assets/style.css"]
    assert [item["kind"] for item in validated["resources"]] == [
        "image", "font", "stylesheet",
    ]


@pytest.mark.parametrize(
    "mutation",
    [
        "job", "digest", "provider_host", "remote_path",
        "duplicate_request", "duplicate_source", "cross_alias",
    ],
)
def test_snapshot_rejects_identity_path_and_alias_tampering(mutation):
    source = b"<html><body>paper</body></html>"
    snapshot = _snapshot(source)
    if mutation == "job":
        snapshot["job_id"] = "other_job"
    elif mutation == "digest":
        snapshot["html"]["sha256"] = "sha256:" + "0" * 64
    elif mutation == "provider_host":
        snapshot["document_url"] = "https://evil.example/html/2305.14314"
    elif mutation == "remote_path":
        snapshot["resources"][1]["path"] = "../paper.woff2"
    elif mutation == "duplicate_request":
        snapshot["resources"][1]["request_url"] = snapshot["resources"][0]["request_url"]
    elif mutation == "duplicate_source":
        snapshot["resources"][1]["source_url"] = snapshot["resources"][0]["source_url"]
    else:
        snapshot["resources"][0]["source_url"] = "https://cdn.example/final-image.png"
        snapshot["resources"][1]["request_url"] = "https://cdn.example/final-image.png"

    with pytest.raises(ScholarlyHtmlSnapshotError):
        validate_scholarly_html_snapshot(
            snapshot, expected_job_id="job_paper", source_html=source,
        )


def test_css_dependency_parser_separates_imports_and_assets():
    imports, resources = iter_css_urls(b"""
        @import url('https://fonts.example/family.css');
        @font-face{font-family:Paper;src:url(../paper.woff2) format('woff2')}
        .figure{background-image:url(https://img.example/grid.png)}
        .retina{background-image:image-set("../paper@2x.png" 2x)}
    """)

    assert imports == ["https://fonts.example/family.css"]
    assert resources == [
        "../paper.woff2",
        "https://img.example/grid.png",
        "../paper@2x.png",
    ]


def test_css_sanitizer_rewrites_only_declared_resources_and_drops_active_edges():
    aliases = {
        "https://cdn.example/paper.woff2": "/api/jobs/job_paper/document/resource?path=font",
    }
    rendered = sanitize_snapshot_stylesheet(
        b"""
        @import url(https://evil.example/import.css);
        :root{--paper-color:#123;color:var(--paper-color)}
        @font-face{font-family:Paper;src:url(../paper.woff2)}
        .paper{position:fixed;background:url(https://evil.example/track);behavior:url(x.htc)}
        .safe{position:relative;color:red}
        """,
        base_url="https://cdn.example/css/main.css",
        resolve_resource=aliases.get,
    )

    assert "@import" not in rendered
    assert "evil.example" not in rendered
    assert "behavior" not in rendered
    assert "position:fixed" not in rendered
    assert "--paper-color:#123" in rendered
    assert "/api/jobs/job_paper/document/resource?path=font" in rendered
    assert ".safe{position:relative;color:red;}" in rendered


def test_css_sanitizer_localizes_image_set_strings_and_drops_unknown_same_origin_path():
    aliases = {
        "https://cdn.example/css/paper@2x.png": (
            "/api/jobs/job_paper/document/resource?path=image"
        ),
    }
    rendered = sanitize_snapshot_stylesheet(
        b"""
        .safe{background-image:image-set("paper@2x.png" 2x)}
        .cross-job{background-image:image-set("/api/jobs/other/resource" 1x)}
        """,
        base_url="https://cdn.example/css/main.css",
        resolve_resource=aliases.get,
    )

    assert "/api/jobs/job_paper/document/resource?path=image" in rendered
    assert "other/resource" not in rendered
    assert ".cross-job" not in rendered


def test_css_sanitizer_drops_dynamic_resource_substitution():
    rendered = sanitize_snapshot_stylesheet(
        b"""
        :root{--u:"/api/jobs/other/document/resource?path=x";--chain:var(--u);--caption:"Paper"}
        .direct{background-image:image-set(var(--u) 1x)}
        .fallback{background-image:image-set(var(--missing,"/api/jobs/other/x") 1x)}
        .chained{background-image:var(--chain)}
        .non-resource{color:var(--paper-color)}
        """,
        base_url="https://cdn.example/css/main.css",
        resolve_resource=lambda _: None,
    )

    assert "other/document" not in rendered
    assert "other/x" not in rendered
    assert ".direct" not in rendered
    assert ".fallback" not in rendered
    assert ".chained" not in rendered
    assert '--caption:"Paper"' in rendered
    assert ".non-resource{color:var(--paper-color);}" in rendered


def test_css_sanitizer_cannot_close_style_element():
    with pytest.raises(ScholarlyHtmlSnapshotError, match="unsafe text"):
        sanitize_snapshot_stylesheet(
            b'.paper::after{content:"</style><script>alert(1)</script>"}',
            base_url="https://cdn.example/main.css",
            resolve_resource=lambda _: None,
        )


def test_css_dependency_and_sanitizer_share_token_budget(monkeypatch):
    monkeypatch.setattr(snapshot_service, "SCHOLARLY_HTML_CSS_MAX_TOKENS", 1)
    css = b".paper{color:red;background:blue}"

    with pytest.raises(ScholarlyHtmlSnapshotLimitError, match="token count"):
        iter_css_urls(css)
    with pytest.raises(ScholarlyHtmlSnapshotLimitError, match="token count"):
        sanitize_snapshot_stylesheet(
            css,
            base_url="https://cdn.example/main.css",
            resolve_resource=lambda _: None,
        )


def test_css_sanitizer_bounds_nested_selector_before_serialize(monkeypatch):
    monkeypatch.setattr(snapshot_service, "SCHOLARLY_HTML_CSS_MAX_DEPTH", 4)
    css = (":not(" * 6 + ".paper" + ")" * 6 + "{color:red}").encode()

    with pytest.raises(ScholarlyHtmlSnapshotLimitError, match="nesting"):
        sanitize_snapshot_stylesheet(
            css,
            base_url="https://cdn.example/main.css",
            resolve_resource=lambda _: None,
        )
