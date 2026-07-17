"""论文落地页解析,替代源和下载诊断回归."""

from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from urllib.request import Request

import pytest

from shared.document_source_resolver import (
    DocumentSourceAlternative,
    canonical_source_url,
    load_document_source_alternatives,
    resolve_document_source_alternative,
)
from shared.errors import InputInvalidError
from steps.common.step_01_download import (
    DownloadStep,
    HttpFetchResult,
    _PublicUrlRedirectHandler,
)
from tests.steps.conftest import make_step_config


def _make_step(tmp_path: Path, url: str) -> DownloadStep:
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    for name in ("input", "intermediate", "output", "assets", "logs"):
        (job_dir / name).mkdir()
    (job_dir / "job.json").write_text(json.dumps({
        "url": url,
        "source": "http_article",
        "content_type": "document",
        "document_kind": "research_paper",
    }))
    config = make_step_config(tmp_path, step_name="01_download", pool="io")
    return DownloadStep("01_download", job_dir, config)


def _pdf_bytes(pages: int = 3) -> bytes:
    del pages
    return b"%PDF-1.7\n" + (b"research-paper-body\n" * 256) + b"%%EOF\n"


def _response(
    body: bytes,
    *,
    url: str,
    status: int = 200,
    content_type: str = "text/html; charset=utf-8",
) -> HttpFetchResult:
    return HttpFetchResult(
        body=body,
        final_url=url,
        status_code=status,
        content_type=content_type,
        error=None,
    )


def test_canonical_source_url_normalizes_host_port_query_and_fragment():
    assert canonical_source_url(
        "HTTPS://Papers.SSRN.com:443/sol3/papers.cfm?b=2&abstract_id=2326253#top"
    ) == (
        "https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2326253&b=2"
    )


def test_alternative_registry_is_extensible_and_rejects_duplicate_canonical_urls(tmp_path):
    config = tmp_path / "alternatives.yaml"
    config.write_text("""
schema_version: 1
alternatives:
  - original_url: https://example.com/paper?b=2&a=1
    resolved_url: https://archive.example.org/paper.pdf
    document_kind: research_paper
    reason: publisher_challenge
    min_pages: 3
  - original_url: https://EXAMPLE.com:443/paper?a=1&b=2#copy
    resolved_url: https://archive.example.org/other.pdf
    document_kind: research_paper
    reason: duplicate
    min_pages: 2
""", encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate canonical original_url"):
        load_document_source_alternatives(config)


def test_default_registry_resolves_q36_to_stable_pdf():
    alternative = resolve_document_source_alternative(
        "https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2326253"
    )

    assert alternative is not None
    assert alternative.document_kind == "research_paper"
    assert alternative.resolved_url == (
        "https://escholarship.org/content/qt4hn4t174/qt4hn4t174.pdf"
    )
    assert alternative.min_pages >= 2


def test_nber_citation_pdf_replaces_landing_html_and_records_provenance(tmp_path):
    source_url = "https://www.nber.org/papers/w25398"
    pdf_url = "https://www.nber.org/system/files/working_papers/w25398/w25398.pdf"
    step = _make_step(tmp_path, source_url)
    (step.job_dir / "input" / "source.html").write_text("stale landing")
    landing = f'''<html><head>
      <meta name="citation_title" content="Empirical Asset Pricing via Machine Learning">
      <meta name="citation_pdf_url" content="{pdf_url}">
    </head><body>Abstract only.</body></html>'''.encode()

    with patch.object(step, "_fetch_response", side_effect=[
        _response(landing, url=source_url),
        _response(_pdf_bytes(4), url=pdf_url, content_type="application/pdf"),
    ]) as fetch, patch.object(
        step, "_pdf_page_count", return_value=4,
    ), patch("shared.net.assert_public_url") as assert_public:
        step._download_article(source_url, document_kind="research_paper")

    assert fetch.call_count == 2
    assert_public.assert_any_call(source_url)
    assert_public.assert_any_call(pdf_url)
    assert (step.job_dir / "input" / "source.pdf").read_bytes().startswith(b"%PDF")
    assert not (step.job_dir / "input" / "source.html").exists()
    resolution = step._document_source_meta["source_resolution"]
    assert resolution == {
        "strategy": "citation_pdf",
        "original_url": source_url,
        "original_status": 200,
        "original_content_type": "text/html; charset=utf-8",
        "resolved_url": pdf_url,
        "resolved_status": 200,
        "resolved_content_type": "application/pdf",
        "pdf_pages": 4,
    }
    assert step._document_source_meta["source_url"] == source_url
    assert step._document_source_meta["final_url"] == pdf_url


def test_cloudflare_challenge_without_alternative_fails_once_with_diagnostics(tmp_path):
    source_url = "https://blocked.example.org/paper"
    step = _make_step(tmp_path, source_url)
    challenge = b"<html><title>Just a moment...</title><div id='cf-chl-widget'></div></html>"

    with patch.object(step, "_fetch_response", return_value=_response(
        challenge, url=source_url, status=403,
    )) as fetch, patch(
        "steps.common.step_01_download.resolve_document_source_alternative",
        return_value=None,
    ), patch("shared.net.assert_public_url"):
        with pytest.raises(InputInvalidError) as error:
            step._download_article(source_url, document_kind="research_paper")

    assert fetch.call_count == 1
    message = str(error.value)
    assert "http_status=403" in message
    assert "challenge=cloudflare" in message
    assert f"final_url={source_url}" in message
    assert "resolver=no_alternative" in message


def test_http_error_response_keeps_status_mime_url_and_body(tmp_path):
    source_url = "https://blocked.example.org/paper"
    step = _make_step(tmp_path, source_url)
    challenge = b"<html><title>Just a moment...</title><div id='cf-chl-widget'></div>"
    http_error = HTTPError(
        source_url, 403, "Forbidden", {"Content-Type": "text/html"},
        BytesIO(challenge),
    )

    opener = MagicMock()
    opener.open.side_effect = http_error
    with patch("urllib.request.build_opener", return_value=opener) as build_opener:
        response = step._fetch_response(source_url, timeout=30)

    redirect_handler = build_opener.call_args.args[0]
    assert isinstance(redirect_handler, _PublicUrlRedirectHandler)
    assert response.status_code == 403
    assert response.final_url == source_url
    assert response.content_type == "text/html"
    assert response.body == challenge
    assert response.error == "HTTPError:403"


def test_redirect_handler_blocks_internal_target_before_next_request():
    handler = _PublicUrlRedirectHandler()
    handler.parent = MagicMock()
    request = Request("https://public.example.org/paper")
    response = MagicMock()

    with pytest.raises(InputInvalidError, match="refusing to fetch internal address"):
        handler.http_error_302(
            request, response, 302, "Found",
            {"location": "http://127.0.0.1/private"},
        )

    handler.parent.open.assert_not_called()


def test_redirect_handler_resolves_relative_location_before_validation():
    handler = _PublicUrlRedirectHandler()
    request = Request("https://public.example.org/papers/landing")

    with patch("shared.net.assert_public_url") as validate:
        redirected = handler.redirect_request(
            request, None, 302, "Found", {}, "../files/paper.pdf",
        )

    target = "https://public.example.org/files/paper.pdf"
    validate.assert_called_once_with(target)
    assert redirected is not None
    assert redirected.full_url == target


def test_redirect_handler_enforces_five_hop_limit_before_next_request():
    handler = _PublicUrlRedirectHandler()
    handler.parent = MagicMock()
    request = Request("https://public.example.org/start")
    request.redirect_dict = {
        f"https://public.example.org/hop-{index}": 1
        for index in range(handler.max_redirections)
    }
    response = MagicMock()

    with patch("shared.net.assert_public_url"):
        with pytest.raises(HTTPError, match="redirect error"):
            handler.http_error_302(
                request, response, 302, "Found",
                {"location": "/too-many"},
            )

    assert handler.max_redirections == 5
    handler.parent.open.assert_not_called()


def test_resolved_candidate_challenge_reports_candidate_and_strategy(tmp_path):
    source_url = "https://publisher.example.org/paper"
    pdf_url = "https://archive.example.org/paper.pdf"
    step = _make_step(tmp_path, source_url)
    landing = f'<meta name="citation_pdf_url" content="{pdf_url}">'.encode()
    challenge = b"<html><title>Just a moment...</title><div id='cf-chl-widget'></div>"

    with patch.object(step, "_fetch_response", side_effect=[
        _response(landing, url=source_url),
        _response(challenge, url=pdf_url),
    ]), patch("shared.net.assert_public_url"):
        with pytest.raises(InputInvalidError) as error:
            step._download_article(source_url, document_kind="research_paper")

    message = str(error.value)
    assert f"original_url={source_url}" in message
    assert f"final_url={pdf_url}" in message
    assert "challenge=cloudflare" in message
    assert "resolver=citation_pdf" in message


def test_configured_alternative_is_used_only_after_deterministic_primary_failure(tmp_path):
    source_url = "https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2326253"
    pdf_url = "https://escholarship.org/content/qt4hn4t174/qt4hn4t174.pdf"
    step = _make_step(tmp_path, source_url)
    alternative = DocumentSourceAlternative(
        original_url=canonical_source_url(source_url),
        resolved_url=pdf_url,
        document_kind="research_paper",
        reason="publisher_cloudflare_challenge",
        min_pages=2,
    )
    challenge = b"<html><title>Just a moment...</title>cloudflare cf-ray</html>"
    transient = HttpFetchResult(
        body=b"", final_url=pdf_url, status_code=None,
        content_type="", error="TimeoutError:mock",
    )

    with patch.object(step, "_fetch_response", side_effect=[
        _response(challenge, url=source_url, status=403),
        transient,
        _response(_pdf_bytes(5), url=pdf_url, content_type="application/pdf"),
    ]) as fetch, patch.object(
        step, "_pdf_page_count", return_value=5,
    ), patch(
        "steps.common.step_01_download.resolve_document_source_alternative",
        return_value=alternative,
    ), patch("shared.net.assert_public_url") as assert_public:
        step._download_article(source_url, document_kind="research_paper")

    assert fetch.call_count == 3
    assert_public.assert_any_call(pdf_url)
    resolution = step._document_source_meta["source_resolution"]
    assert resolution["strategy"] == "configured_alternative"
    assert resolution["original_status"] == 403
    assert resolution["challenge"] == "cloudflare"
    assert resolution["resolved_url"] == pdf_url
    assert resolution["pdf_pages"] == 5


def test_configured_alternative_does_not_mask_transient_primary_failure(tmp_path):
    source_url = "https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2326253"
    step = _make_step(tmp_path, source_url)
    alternative = DocumentSourceAlternative(
        original_url=canonical_source_url(source_url),
        resolved_url="https://archive.example.org/paper.pdf",
        document_kind="research_paper",
        reason="publisher_cloudflare_challenge",
        min_pages=2,
    )
    transient = HttpFetchResult(
        body=b"", final_url=source_url, status_code=None,
        content_type="", error="TimeoutError:mock",
    )

    with patch.object(step, "_fetch_response", return_value=transient) as fetch, patch(
        "steps.common.step_01_download.resolve_document_source_alternative",
        return_value=alternative,
    ), patch("shared.net.assert_public_url"):
        with pytest.raises(InputInvalidError) as error:
            step._download_article(source_url, document_kind="research_paper")

    assert fetch.call_count == 5
    assert "resolver=alternative_skipped_transient" in str(error.value)
    assert "TimeoutError:mock" in str(error.value)


@pytest.mark.parametrize(
    ("body", "content_type", "diagnostic"),
    [
        (b"<html>login required</html>", "text/html", "pdf_signature=invalid"),
        (_pdf_bytes(1), "application/pdf", "pdf_pages=1"),
    ],
)
def test_resolved_pdf_rejects_html_or_too_short_document(
    tmp_path, body, content_type, diagnostic,
):
    source_url = "https://publisher.example.org/paper"
    pdf_url = "https://publisher.example.org/paper.pdf"
    step = _make_step(tmp_path, source_url)
    landing = (
        f'<meta name="citation_pdf_url" content="{pdf_url}">'
    ).encode()

    page_count = 1 if diagnostic == "pdf_pages=1" else 3
    with patch.object(step, "_fetch_response", side_effect=[
        _response(landing, url=source_url),
        _response(body, url=pdf_url, content_type=content_type),
    ]), patch.object(
        step, "_pdf_page_count", return_value=page_count,
    ), patch("shared.net.assert_public_url"):
        with pytest.raises(InputInvalidError, match=diagnostic):
            step._download_article(source_url, document_kind="research_paper")

    assert not (step.job_dir / "input" / "source.pdf").exists()


def test_regular_article_keeps_html_and_does_not_follow_citation_pdf(tmp_path):
    source_url = "https://news.example.org/story"
    pdf_url = "https://news.example.org/attachment.pdf"
    step = _make_step(tmp_path, source_url)
    (step.job_dir / "input" / "source.pdf").write_bytes(_pdf_bytes())
    html = f'<meta name="citation_pdf_url" content="{pdf_url}"><main>Story</main>'.encode()

    with patch.object(step, "_fetch_response", return_value=_response(
        html, url=source_url,
    )) as fetch, patch("shared.net.assert_public_url"):
        step._download_article(source_url, document_kind="article")

    assert fetch.call_count == 1
    assert (step.job_dir / "input" / "source.html").read_text() == html.decode()
    assert not (step.job_dir / "input" / "source.pdf").exists()
