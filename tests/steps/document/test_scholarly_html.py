"""验证学术 HTML 结构化解析、稳定定位和显式质量降级。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from PIL import Image

from shared.document_contract import validate_document, validate_quality
from steps.document.adapters import parse_scholarly_html
from steps.document.adapters._html_tree import parse_html_tree


def _fingerprint(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def test_html_text_uses_math_alttext_once_and_ignores_annotations() -> None:
    root = parse_html_tree("""
    <p>At <math alttext="t+1"><semantics><mrow><mi>t</mi><mo>+</mo><mn>1</mn></mrow>
      <annotation-xml><apply><ci>t</ci><cn>1</cn></apply></annotation-xml>
      <annotation>t+1</annotation></semantics></math> now.</p>
    <p>Fallback <math><semantics><mi>x</mi><annotation>x</annotation></semantics></math>.</p>
    """)
    paragraphs = list(root.descendants(lambda node: node.tag == "p"))

    assert [node.text() for node in paragraphs] == ["At t+1 now.", "Fallback x."]


@pytest.fixture
def scholarly_html_job(tmp_path: Path) -> tuple[Path, dict[str, str], bytes]:
    job_dir = tmp_path / "jobs_arxiv_1706.03762"
    (job_dir / "input").mkdir(parents=True)
    (job_dir / "assets").mkdir()
    for name in ("overview.png", "panel-a.png", "panel-b.png"):
        Image.new("RGB", (4, 3)).save(job_dir / "assets" / name)
    raw = b"""<!doctype html>
<html><head>
  <meta name="citation_title" content="Attention Is All You Need">
  <meta name="citation_keywords" content="transformer; attention">
  <meta name="citation_doi" content="10.5555/attention">
  <meta name="citation_arxiv_id" content="1706.03762">
  <meta name="citation_arxiv_version" content="v7">
  <meta name="citation_arxiv_category" content="cs.CL">
  <meta name="citation_license" content="arXiv perpetual non-exclusive license">
</head><body><article class="ltx_document ltx_authors_1line">
  <header>
    <h1 class="ltx_title_document">Attention Is All You Need</h1>
    <span class="ltx_creator ltx_role_author">
      <span class="ltx_personname">Ashish Vaswani</span>
      <span class="ltx_affiliation">Google Brain</span>
      <span class="ltx_contact ltx_role_email">avaswani@example.org</span>
      <span class="ltx_author_notes">Equal contribution</span>
    </span>
    <div class="ltx_copyright">Tables and figures may be reproduced with attribution.</div>
  </header>
  <div class="ltx_abstract"><h6>Abstract</h6>We introduce the Transformer.</div>
  <section><h2>1 Introduction</h2>
    <p>Sequence transduction uses attention
      <math alttext="\\operatorname{softmax}(QK^T)V" display="block"></math>.
      See <a class="ltx_ref ltx_ref_bib" href="#bib1">[1]</a>.
    </p>
    <div class="ltx_theorem">The model is permutation equivariant.</div>
    <figure id="fig1">
      <img src="assets/overview.png" alt="overview">
      <figcaption>Figure 1: Model architecture.</figcaption>
    </figure>
    <figure id="fig2">
      <img src="assets/panel-a.png" alt="encoder">
      <img src="assets/panel-b.png" alt="decoder">
      <figcaption>Figure 2: Encoder and decoder panels.</figcaption>
    </figure>
    <figure class="ltx_table" id="tab1">
      <figcaption>Table 1: Evaluation results.</figcaption>
      <table><tbody>
        <tr><th rowspan="2">Model</th><th colspan="2">BLEU</th></tr>
        <tr><th>EN-DE</th><th>EN-FR</th></tr>
        <tr><td>Transformer</td><td>28.4</td><td>41.8</td></tr>
      </tbody></table>
    </figure>
    <p id="bib1">External artifact <a href="https://example.org/paper">paper</a>.</p>
  </section>
</article></body></html>"""
    (job_dir / "input" / "source.html").write_bytes(raw)
    job = {
        "job_id": "jobs_arxiv_1706.03762",
        "document_kind": "research_paper",
        "url": "https://arxiv.org/html/1706.03762",
        "source_fingerprint": _fingerprint(raw),
    }
    return job_dir, job, raw


def test_scholarly_html_preserves_structure_and_raw_source(
    scholarly_html_job: tuple[Path, dict[str, str], bytes],
) -> None:
    job_dir, job, raw = scholarly_html_job
    before_paths = sorted(path.relative_to(job_dir) for path in job_dir.rglob("*"))

    document, quality = parse_scholarly_html(job_dir, job)

    assert validate_document(document, expected_job_id=job["job_id"]) == document
    assert validate_quality(quality, expected_job_id=job["job_id"]) == quality
    assert (job_dir / "input" / "source.html").read_bytes() == raw
    assert sorted(path.relative_to(job_dir) for path in job_dir.rglob("*")) == before_paths
    assert document["source_profile"] == "scholarly_html"
    assert document["metadata"]["titles"]["original"] == "Attention Is All You Need"
    author = document["metadata"]["authors"][0]
    assert {key: author[key] for key in ("name", "affiliations", "emails", "notes")} == {
        "name": "Ashish Vaswani", "affiliations": ["Google Brain"],
        "emails": ["avaswani@example.org"], "notes": ["Equal contribution"],
    }
    assert author["equal_contribution"] is True
    assert author["note_refs"] == [document["metadata"]["author_notes"][0]["note_id"]]
    assert [item["name"] for item in document["metadata"]["affiliations"]] == ["Google Brain"]
    assert document["metadata"]["abstract"] == "We introduce the Transformer."
    assert document["metadata"]["identifiers"]["doi"] == "10.5555/attention"
    assert document["metadata"]["identifiers"]["arxiv_id"] == "1706.03762"
    assert document["metadata"]["version"] == "v7"
    assert document["metadata"]["categories"] == ["cs.CL"]
    assert document["metadata"]["source_license"] == "arXiv perpetual non-exclusive license"
    assert document["metadata"]["rights_notices"] == [
        "Tables and figures may be reproduced with attribution.",
    ]
    assert all(
        "Equal contribution" not in str(block.get("text"))
        for block in document["blocks"]
    )
    assert any(block["kind"] == "formula" for block in document["blocks"])
    assert any(block["kind"] == "theorem" for block in document["blocks"])
    assert all("html" in block["locator"] for block in document["blocks"])
    assert [figure["label"] for figure in document["figures"]] == ["Figure 1", "Figure 2"]
    assert [len(figure["media"]) for figure in document["figures"]] == [1, 2]
    assert all(asset["state"] == "available" for asset in document["assets"])
    assert len(document["tables"]) == 1
    cells = document["tables"][0]["cells"]
    assert len(cells) == 7
    assert {key: cells[0][key] for key in (
        "row", "col", "rowspan", "colspan", "role",
    )} == {"row": 0, "col": 0, "rowspan": 2, "colspan": 1, "role": "column_header"}
    assert cells[1]["colspan"] == 2
    assert {reference["kind"] for reference in document["references"]} == {
        "citation", "external",
    }
    assert quality["status"] == "complete"
    assert quality["metrics"]["figure_panel_count"] == 3
    assert quality["metrics"]["table_cell_count"] == 7

    rerun_document, rerun_quality = parse_scholarly_html(job_dir, job)
    assert rerun_document == document
    assert rerun_quality == quality


def test_scholarly_html_degrades_for_missing_media_and_unsafe_link(tmp_path: Path) -> None:
    job_dir = tmp_path / "job_html_degraded"
    (job_dir / "input").mkdir(parents=True)
    raw = b"""<html><body><article><h1>Paper</h1>
      <figure><img><figcaption>Figure 1: Missing panel.</figcaption></figure>
      <p><a href="javascript:alert(1)">unsafe</a></p>
    </article></body></html>"""
    (job_dir / "input" / "source.html").write_bytes(raw)

    document, quality = parse_scholarly_html(job_dir, {
        "job_id": job_dir.name,
        "document_kind": "research_paper",
        "source_fingerprint": _fingerprint(raw),
    })

    assert document["figures"][0]["extraction"]["status"] == "degraded"
    assert document["figures"][0]["media"][0]["asset_id"] is None
    assert document["references"] == []
    assert quality["status"] == "degraded"
    assert set(quality["reasons"]) >= {
        "html_asset_reference_missing",
        "html_figure_media_incomplete",
        "html_unsafe_reference_ignored",
    }


def test_ar5iv_grouped_author_header_uses_sidecar_names_and_line_mapping(tmp_path: Path) -> None:
    job_dir = tmp_path / "job_grouped_authors"
    (job_dir / "input").mkdir(parents=True)
    raw = b"""<html><body><article class="ltx_document ltx_authors_1line">
      <p>Provided proper attribution is provided, Example grants permission to reproduce
      the tables and figures in this paper.</p>
      <h1>Grouped authors</h1><div class="ltx_authors">
      <span class="ltx_creator ltx_role_author"><span class="ltx_personname">
      Ada Lovelace<br>Example Lab<br><span>ada@example.org<br></span>
      &amp;Alan Turing<br>Example University<br><span>alan@example.org<br></span>
      </span><span class="ltx_author_notes">Equal contribution.</span></span></div>
      <div class="ltx_abstract"><p>Abstract text.</p></div><p>Body text.</p>
    </article></body></html>"""
    (job_dir / "input" / "source.html").write_bytes(raw)
    (job_dir / "input" / "metadata.json").write_text(
        json.dumps({"authors": ["Ada Lovelace", "Alan Turing"]}), encoding="utf-8",
    )

    document, _quality = parse_scholarly_html(job_dir, {
        "job_id": job_dir.name,
        "document_kind": "research_paper",
        "source_fingerprint": _fingerprint(raw),
    })

    assert [author["name"] for author in document["metadata"]["authors"]] == [
        "Ada Lovelace", "Alan Turing",
    ]
    assert [author["affiliations"] for author in document["metadata"]["authors"]] == [
        ["Example Lab"], ["Example University"],
    ]
    assert [author["emails"] for author in document["metadata"]["authors"]] == [
        ["ada@example.org"], ["alan@example.org"],
    ]
    assert document["metadata"]["author_notes"][0]["text"] == "Equal contribution."
    assert document["metadata"]["rights_notices"] == [
        "Provided proper attribution is provided, Example grants permission to reproduce the tables and figures in this paper.",
    ]
    assert all(
        "permission to reproduce" not in block["text"]
        for block in document["blocks"]
    )


def test_arxiv_identity_uses_entry_url_and_sidecar_not_reference_text(tmp_path: Path) -> None:
    job_dir = tmp_path / "jobs_arxiv_2309.06180"
    (job_dir / "input").mkdir(parents=True)
    raw = b"""<html><body><article class="ltx_document ltx_authors_1line">
      <h1 class="ltx_title_document">PagedAttention</h1>
      <div class="ltx_authors"><span class="ltx_creator ltx_role_author">
        <span class="ltx_personname">Ada One1 Bob Two2 Carol Three1</span>
        <span class="ltx_affiliation">1 Example University 2 Example Lab</span>
      </span></div>
      <div class="ltx_abstract">Serving systems need deterministic identity.</div>
      <section><h2>References</h2><p>Related work. arXiv:2207.00032.</p></section>
    </article></body></html>"""
    (job_dir / "input" / "source.html").write_bytes(raw)
    (job_dir / "input" / "metadata.json").write_text(json.dumps({
        "title": "Efficient Memory Management for Large Language Model Serving with PagedAttention",
        "authors": ["Ada One", "Bob Two", "Carol Three"],
        "published_at": "2023-09-12",
        "updated_at": "2023-09-12",
    }), encoding="utf-8")

    document, quality = parse_scholarly_html(job_dir, {
        "job_id": job_dir.name,
        "document_kind": "research_paper",
        "url": "https://arxiv.org/abs/2309.06180",
        "source_fingerprint": _fingerprint(raw),
    })

    metadata = document["metadata"]
    assert metadata["titles"]["original"].startswith("Efficient Memory Management")
    assert metadata["identifiers"]["arxiv_id"] == "2309.06180"
    assert [author["name"] for author in metadata["authors"]] == [
        "Ada One", "Bob Two", "Carol Three",
    ]
    assert metadata["published_at"] == "2023-09-12"
    assert metadata["updated_at"] == "2023-09-12"
    assert "metadata_identifier_conflict" not in quality["reasons"]


def test_arxiv_entry_identity_wins_conflicting_citation_meta(tmp_path: Path) -> None:
    job_dir = tmp_path / "jobs_arxiv_2205.14135"
    (job_dir / "input").mkdir(parents=True)
    raw = b"""<html><head><meta name="citation_arxiv_id" content="2102.08602">
      </head><body><article><h1>FlashAttention</h1>
      <p>This paper has enough scholarly body text for identity validation.</p>
      </article></body></html>"""
    (job_dir / "input" / "source.html").write_bytes(raw)

    document, quality = parse_scholarly_html(job_dir, {
        "job_id": job_dir.name,
        "document_kind": "research_paper",
        "url": "https://arxiv.org/abs/2205.14135",
        "source_fingerprint": _fingerprint(raw),
    })

    assert document["metadata"]["identifiers"]["arxiv_id"] == "2205.14135"
    assert quality["status"] == "degraded"
    assert "metadata_identifier_conflict" in quality["reasons"]


def test_appendix_visuals_are_collected_and_algorithms_are_not_figures(tmp_path: Path) -> None:
    job_dir = tmp_path / "job_appendix_visuals"
    (job_dir / "input").mkdir(parents=True)
    (job_dir / "assets").mkdir()
    Image.new("RGB", (4, 3)).save(job_dir / "assets" / "appendix.png")
    raw = b"""<html><body><article><h1>Paper</h1><p>Body.</p>
      <figure class="ltx_float ltx_float_algorithm"><figcaption>Algorithm 1</figcaption>
      <p>Fused kernel.</p></figure>
      <table class="ltx_equation"><tr><td><math alttext="x=y" display="block"></math></td></tr></table>
      <section class="ltx_appendix"><h2>Appendix A</h2>
        <figure class="ltx_figure"><img src="assets/appendix.png">
          <figcaption>Figure 9: Appendix result.</figcaption></figure>
        <figure class="ltx_table"><figcaption>Table 7: Appendix values.</figcaption>
          <table><tr><th>Metric</th><td>1.0</td></tr></table></figure>
      </section></article></body></html>"""
    (job_dir / "input" / "source.html").write_bytes(raw)

    document, quality = parse_scholarly_html(job_dir, {
        "job_id": job_dir.name,
        "document_kind": "research_paper",
        "source_fingerprint": _fingerprint(raw),
    })

    assert [figure["label"] for figure in document["figures"]] == ["Figure 9"]
    assert [table["label"] for table in document["tables"]] == ["Table 7"]
    assert any(block["kind"] == "algorithm" for block in document["blocks"])
    assert any(block["kind"] == "formula" and block["text"] == "x=y" for block in document["blocks"])
    assert quality["status"] == "complete"


def test_nested_figure_uses_outer_caption_and_tracks_missing_panel(tmp_path: Path) -> None:
    job_dir = tmp_path / "job_nested_figure"
    (job_dir / "input").mkdir(parents=True)
    (job_dir / "assets").mkdir()
    Image.new("RGB", (4, 3)).save(job_dir / "assets" / "right.png")
    raw = b"""<html><body><article><h1>Paper</h1><p>Body.</p>
      <figure class="ltx_figure" id="fig11">
        <figure class="ltx_figure_panel"><figcaption>(a) Missing left panel.</figcaption></figure>
        <figure class="ltx_figure_panel"><img src="assets/right.png" alt="right">
          <figcaption>(b) Available right panel.</figcaption></figure>
        <figcaption>Figure 11: Complete outer comparison caption.</figcaption>
      </figure></article></body></html>"""
    (job_dir / "input" / "source.html").write_bytes(raw)

    document, quality = parse_scholarly_html(job_dir, {
        "job_id": job_dir.name,
        "document_kind": "research_paper",
        "source_fingerprint": _fingerprint(raw),
    })

    figure = document["figures"][0]
    assert figure["label"] == "Figure 11"
    assert figure["caption"] == "Figure 11: Complete outer comparison caption."
    assert [media["role"] for media in figure["media"]] == [
        "(a) Missing left panel.", "(b) Available right panel.",
    ]
    assert [media["artifact"] for media in figure["media"]] == [
        None, "assets/right.png",
    ]
    assert figure["extraction"]["status"] == "degraded"
    assert "html_figure_media_incomplete" in quality["reasons"]


def test_relative_reference_is_resolved_but_active_scheme_is_rejected(tmp_path: Path) -> None:
    job_dir = tmp_path / "job_relative_reference"
    (job_dir / "input").mkdir(parents=True)
    raw = b"""<html><body><article><h1>Paper</h1>
      <p><a href="/abs/1234.5678">source</a><a href="javascript:alert(1)">bad</a></p>
    </article></body></html>"""
    (job_dir / "input" / "source.html").write_bytes(raw)

    document, quality = parse_scholarly_html(job_dir, {
        "job_id": job_dir.name,
        "document_kind": "research_paper",
        "source_fingerprint": _fingerprint(raw),
        "url": "https://arxiv.org/html/1234.5678",
    })

    assert [reference["target"] for reference in document["references"]] == [
        "https://arxiv.org/abs/1234.5678",
    ]
    assert quality["reasons"] == ["html_unsafe_reference_ignored"]


def test_scholarly_html_rejects_empty_body_without_fabricating_content(tmp_path: Path) -> None:
    job_dir = tmp_path / "job_html_empty"
    (job_dir / "input").mkdir(parents=True)
    raw = b"<html><head><title>Ignored browser chrome</title></head><body></body></html>"
    (job_dir / "input" / "source.html").write_bytes(raw)

    document, quality = parse_scholarly_html(job_dir, {"job_id": job_dir.name})

    assert document["blocks"] == []
    assert quality["status"] == "rejected"
    assert set(quality["reasons"]) == {"html_title_missing", "html_body_empty"}


def test_scholarly_html_rejects_source_fingerprint_mismatch(tmp_path: Path) -> None:
    job_dir = tmp_path / "job_html_hash"
    (job_dir / "input").mkdir(parents=True)
    (job_dir / "input" / "source.html").write_text("<h1>Paper</h1>", encoding="utf-8")

    with pytest.raises(ValueError, match="fingerprint mismatch"):
        parse_scholarly_html(job_dir, {
            "job_id": job_dir.name,
            "source_fingerprint": "sha256:" + "0" * 64,
        })


def test_listing_float_is_code_and_never_consumes_figure_number(tmp_path: Path) -> None:
    job_dir = tmp_path / "job_listing"
    (job_dir / "input").mkdir(parents=True)
    raw = b"""<html><body><article><h1>Paper</h1>
      <figure class="ltx_float ltx_lstlisting"><pre>return x</pre>
        <figcaption>Code 1: Identity implementation.</figcaption></figure>
      <figure class="ltx_figure"><figcaption>Figure 1: Real model.</figcaption>
        <img src="assets/model.png" alt="model"></figure>
    </article></body></html>"""
    (job_dir / "input" / "source.html").write_bytes(raw)
    (job_dir / "assets").mkdir()
    Image.new("RGB", (5, 4)).save(job_dir / "assets" / "model.png")

    document, _quality = parse_scholarly_html(job_dir, {
        "job_id": job_dir.name,
        "document_kind": "research_paper",
        "source_fingerprint": _fingerprint(raw),
    })

    assert [item["label"] for item in document["figures"]] == ["Figure 1"]
    assert any(
        block["kind"] == "code" and "Identity" in block["text"]
        for block in document["blocks"]
    )


def test_empty_figure_wrapper_preserves_nested_appendix_without_fake_visual(
    tmp_path: Path,
) -> None:
    job_dir = tmp_path / "job_empty_wrapper"
    (job_dir / "input").mkdir(parents=True)
    raw = b"""<html><body><article><h1>Paper</h1>
      <figure class="ltx_figure"><section class="ltx_appendix">
        <h2>Appendix B Results</h2><p>Nested appendix body.</p>
      </section></figure>
    </article></body></html>"""
    (job_dir / "input" / "source.html").write_bytes(raw)

    document, _quality = parse_scholarly_html(job_dir, {
        "job_id": job_dir.name,
        "document_kind": "research_paper",
        "source_fingerprint": _fingerprint(raw),
    })

    assert document["figures"] == []
    assert any(block["text"] == "Appendix B Results" for block in document["blocks"])
    assert any(block["text"] == "Nested appendix body." for block in document["blocks"])


def test_uncaptioned_media_uses_image_label_without_colliding_with_figure(
    tmp_path: Path,
) -> None:
    job_dir = tmp_path / "job_unlabeled_media"
    (job_dir / "input").mkdir(parents=True)
    (job_dir / "assets").mkdir()
    Image.new("RGB", (5, 4)).save(job_dir / "assets" / "overview.png")
    Image.new("RGB", (5, 4)).save(job_dir / "assets" / "result.png")
    raw = b"""<html><body><article><h1>Paper</h1>
      <figure class="ltx_figure"><img src="assets/overview.png" alt="Overview"></figure>
      <figure class="ltx_figure"><img src="assets/result.png" alt="Result">
        <figcaption>Figure 1: Numbered result.</figcaption></figure>
    </article></body></html>"""
    (job_dir / "input" / "source.html").write_bytes(raw)

    document, _quality = parse_scholarly_html(job_dir, {
        "job_id": job_dir.name,
        "document_kind": "research_paper",
        "source_fingerprint": _fingerprint(raw),
    })

    assert [item["label"] for item in document["figures"]] == [
        "Image 1", "Figure 1",
    ]


def test_uncaptioned_outer_wrapper_preserves_captioned_panel_figures(
    tmp_path: Path,
) -> None:
    job_dir = tmp_path / "job_panel_wrapper"
    (job_dir / "input").mkdir(parents=True)
    (job_dir / "assets").mkdir()
    Image.new("RGB", (8, 5)).save(job_dir / "assets" / "left.png")
    Image.new("RGB", (6, 5)).save(job_dir / "assets" / "right.png")
    raw = b"""<html><body><article><h1>Paper</h1>
      <figure class="ltx_figure"><div>
        <figure class="ltx_figure_panel"><img src="assets/left.png">
          <figcaption>Figure 4: Throughput.</figcaption></figure>
        <figure class="ltx_figure_panel"><img src="assets/right.png">
          <figcaption>Figure 5: Model scale.</figcaption></figure>
      </div></figure>
    </article></body></html>"""
    (job_dir / "input" / "source.html").write_bytes(raw)

    document, _quality = parse_scholarly_html(job_dir, {
        "job_id": job_dir.name,
        "document_kind": "research_paper",
        "source_fingerprint": _fingerprint(raw),
    })

    assert [(item["label"], item["caption"]) for item in document["figures"]] == [
        ("Figure 4", "Figure 4: Throughput."),
        ("Figure 5", "Figure 5: Model scale."),
    ]


def test_list_preserves_top_level_uncaptioned_figure_children(tmp_path: Path) -> None:
    job_dir = tmp_path / "job_list_figures"
    (job_dir / "input").mkdir(parents=True)
    (job_dir / "assets").mkdir()
    Image.new("RGB", (10, 4)).save(job_dir / "assets" / "first.png")
    Image.new("RGB", (10, 4)).save(job_dir / "assets" / "second.png")
    raw = b"""<html><body><article><h1>Paper</h1><ul>
      <li>First case<figure><img src="assets/first.png" width="100" height="40"></figure></li>
      <li>Second case<figure><img src="assets/second.png" width="100" height="40"></figure></li>
    </ul></article></body></html>"""
    (job_dir / "input" / "source.html").write_bytes(raw)

    document, _quality = parse_scholarly_html(job_dir, {
        "job_id": job_dir.name,
        "document_kind": "research_paper",
        "source_fingerprint": _fingerprint(raw),
    })

    assert [item["label"] for item in document["figures"]] == ["Image 1", "Image 2"]
    assert [(item["media"][0]["width"], item["media"][0]["height"])
            for item in document["figures"]] == [(100, 40), (100, 40)]
