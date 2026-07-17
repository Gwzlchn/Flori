"""验证 generic_html adapter 的结构、身份和不可变输入。"""

from __future__ import annotations

from pathlib import Path

from shared.document_contract import validate_document, validate_quality
from steps.document.adapters.generic_html import parse_generic_html


FIXTURES = Path(__file__).parent / "fixtures"


def _job(tmp_path: Path, fixture: str, **overrides):
    job_dir = tmp_path / "job-document"
    (job_dir / "input").mkdir(parents=True)
    source = (FIXTURES / fixture).read_bytes()
    (job_dir / "input" / "source.html").write_bytes(source)
    job = {
        "id": job_dir.name,
        "content_type": "document",
        "document_kind": "article",
        "url": "https://example.com/requested?id=1",
        "final_url": "https://example.com/posts/document-model?ref=redirect",
        **overrides,
    }
    return job_dir, job, source


def test_preserves_document_structure_and_contract(tmp_path):
    job_dir, job, _ = _job(tmp_path, "generic_article.html")

    document, quality = parse_generic_html(job_dir, job)

    assert validate_document(document, expected_job_id=job_dir.name) == document
    assert validate_quality(quality, expected_job_id=job_dir.name) == quality
    assert document["content_type"] == "document"
    assert document["document_kind"] == "article"
    assert document["source_profile"] == "generic_html"
    assert quality["status"] == "complete"
    metadata = document["metadata"]
    assert metadata["titles"] == {"original": "A Structured Document Model", "zh": None}
    assert [author["name"] for author in metadata["authors"]] == ["Ada Example"]
    assert metadata["affiliations"] == []
    assert metadata["publisher"] == "Example Engineering"
    assert metadata["published_at"] == "2026-07-01T08:00:00Z"
    assert metadata["updated_at"] == "2026-07-02T09:00:00Z"
    assert metadata["lang"] == "en"
    assert metadata["keywords"] == ["documents", "evidence"]
    kinds = [block["kind"] for block in document["blocks"]]
    assert {
        "title", "heading", "paragraph", "list", "list_item", "code", "figure",
        "caption", "table", "table_cell", "footnote", "embed",
    } <= set(kinds)
    assert all(block["locator"]["html"]["dom_path"].startswith("/") for block in document["blocks"])
    source = document["sources"][0]
    assert all(
        block["locator"]["html"]["source_fingerprint"] == source["fingerprint"]
        for block in document["blocks"]
    )


def test_keeps_requested_final_and_canonical_urls_separate(tmp_path):
    job_dir, job, _ = _job(tmp_path, "generic_article.html")

    document, _ = parse_generic_html(job_dir, job)

    source = document["sources"][0]
    assert source["source_url"] == "https://example.com/requested?id=1"
    assert source["final_url"] == "https://example.com/posts/document-model?ref=redirect"
    assert source["canonical_url"] == "https://example.com/posts/document-model"


def test_preserves_table_spans_sections_and_grid(tmp_path):
    job_dir, job, _ = _job(tmp_path, "generic_article.html")

    document, _ = parse_generic_html(job_dir, job)

    assert len(document["tables"]) == 1
    table = document["tables"][0]
    assert table["label"] == "Table 1"
    assert table["extraction"]["status"] == "complete"
    assert {cell["row"] for cell in table["cells"]} == {0, 1, 2, 3, 4}
    assert table["cells"][0]["rowspan"] == 2
    assert table["cells"][1]["colspan"] == 2
    assert {cell["role"] for cell in table["cells"]} >= {"column_header", "data"}


def test_preserves_assets_figure_references_and_safe_embeds(tmp_path):
    job_dir, job, _ = _job(tmp_path, "generic_article.html")

    document, quality = parse_generic_html(job_dir, job)

    assert len(document["figures"]) == 1
    figure = document["figures"][0]
    assert figure["label"] == "Figure 1"
    assert len(figure["media"]) == 1
    assert figure["media"][0]["asset_id"]
    image = next(asset for asset in document["assets"] if asset["kind"] == "image")
    assert image["source_url"] == "https://example.com/img/chart.png"
    assert image["width"] == 800 and image["height"] == 450
    assert "https://example.com/img/chart.webp" in image["variants"]
    assert any(ref["kind"] == "footnote" and ref["target_block_id"] for ref in document["references"])
    embeds = [block["embed"] for block in document["blocks"] if block["kind"] == "embed"]
    assert {embed["type"] for embed in embeds} == {"iframe", "audio"}
    assert all(embed["allow_script_execution"] is False for embed in embeds)
    assert next(embed for embed in embeds if embed["type"] == "audio")["source_url"] == (
        "https://example.com/media/summary.mp3"
    )
    assert quality["metrics"]["unsafe_embeds"] == 0


def test_ids_are_deterministic_and_raw_source_is_immutable(tmp_path):
    job_dir, job, before = _job(tmp_path, "generic_article.html")

    first, first_quality = parse_generic_html(job_dir, job)
    second, second_quality = parse_generic_html(job_dir, job)

    assert first == second
    assert first_quality == second_quality
    assert (job_dir / "input" / "source.html").read_bytes() == before
    assert not (job_dir / "intermediate").exists()
    assert not (job_dir / "output").exists()


def test_html_whitepaper_uses_same_document_contract_with_generic_profile(tmp_path):
    job_dir, job, _ = _job(
        tmp_path, "generic_article.html", document_kind="whitepaper",
    )

    document, quality = parse_generic_html(job_dir, job)

    assert document["content_type"] == "document"
    assert document["document_kind"] == "whitepaper"
    assert document["source_profile"] == "generic_html"
    assert {"html", "embedded_media"} == set(document["capabilities"])
    assert quality["status"] == "complete"
