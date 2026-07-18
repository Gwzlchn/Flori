"""验证 PDF 页级定位、数字版图表提取和扫描件 fail-closed。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from shared.document_contract import validate_document, validate_quality
from steps.document.adapters import parse_pdf_document
from steps.document.adapters.scholarly_pdf import (
    _FIGURE_CAPTION,
    _TABLE_CAPTION,
    LayoutItem,
    PageLayout,
    ScholarlyPdfAdapter,
)
from steps.document.provenance import build_document_source_manifest


def _fingerprint(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


@pytest.fixture
def pdf_job(tmp_path: Path) -> tuple[Path, dict[str, str], bytes]:
    job_dir = tmp_path / "jobs_arxiv_2205.14135"
    (job_dir / "input").mkdir(parents=True)
    raw = b"%PDF-1.7\nimmutable scholarly fixture\n%%EOF\n"
    (job_dir / "input" / "source.pdf").write_bytes(raw)
    job = {
        "job_id": job_dir.name,
        "document_kind": "research_paper",
        "url": "https://arxiv.org/pdf/2205.14135",
        "source_fingerprint": _fingerprint(raw),
    }
    return job_dir, job, raw


def _digital_pages() -> list[PageLayout]:
    return [PageLayout(
        number=1,
        width=600.0,
        height=800.0,
        text_items=[
            LayoutItem("FlashAttention: Fast and Memory-Efficient Exact Attention", [70, 50, 530, 82]),
            LayoutItem(
                "We compute exact attention with IO awareness and preserve a digital text layer.",
                [70, 100, 530, 150],
            ),
            LayoutItem("Figure 1: The algorithm contains two panels.", [70, 330, 530, 355]),
            LayoutItem("Table 1: Training throughput.", [70, 500, 530, 525]),
            LayoutItem(
                "Code is available at https://example.org/flash and DOI 10.1234/FLASH.1.",
                [70, 700, 530, 730],
            ),
        ],
        image_bboxes=[[70, 170, 285, 315], [315, 170, 530, 315]],
    )]


def test_digital_pdf_keeps_page_bbox_figures_tables_and_references(
    monkeypatch: pytest.MonkeyPatch,
    pdf_job: tuple[Path, dict[str, str], bytes],
) -> None:
    job_dir, job, raw = pdf_job
    monkeypatch.setattr(ScholarlyPdfAdapter, "_pdf_info", lambda self: {
        "Pages": "1",
        "Title": "FlashAttention",
        "Author": "Tri Dao and Daniel Y. Fu",
    })
    monkeypatch.setattr(
        ScholarlyPdfAdapter,
        "_layout",
        lambda self: (_digital_pages(), "fixture_layout"),
    )
    before_paths = sorted(path.relative_to(job_dir) for path in job_dir.rglob("*"))

    document, quality = parse_pdf_document(job_dir, job)

    assert validate_document(document, expected_job_id=job["job_id"]) == document
    assert validate_quality(quality, expected_job_id=job["job_id"]) == quality
    assert (job_dir / "input" / "source.pdf").read_bytes() == raw
    assert sorted(path.relative_to(job_dir) for path in job_dir.rglob("*")) == before_paths
    assert document["source_profile"] == "digital_pdf"
    assert document["metadata"]["titles"]["original"] == "FlashAttention"
    assert [author["name"] for author in document["metadata"]["authors"]] == [
        "Tri Dao", "Daniel Y. Fu",
    ]
    assert all("pdf" in block["locator"] for block in document["blocks"])
    assert all(block["locator"]["pdf"]["page"] == 1 for block in document["blocks"])
    assert len(document["figures"]) == 1
    assert document["figures"][0]["extraction"]["status"] == "complete"
    assert len(document["figures"][0]["media"]) == 2
    assert len(document["assets"]) == 2
    assert len(document["tables"]) == 1
    assert document["tables"][0]["extraction"]["status"] == "degraded"
    assert document["tables"][0]["representations"][0]["kind"] == "source_crop"
    assert document["tables"][0]["source_locator"]["pdf"]["page"] == 1
    assert {reference["kind"] for reference in document["references"]} == {
        "external", "citation",
    }
    assert quality["status"] == "degraded"
    assert quality["reasons"] == ["pdf_table_structure_unavailable"]
    assert quality["metrics"]["layout_method"] == "fixture_layout"
    assert quality["metrics"]["figure_panel_count"] == 2


def test_pdf_sidecar_metadata_wins_over_container_metadata(
    monkeypatch: pytest.MonkeyPatch,
    pdf_job: tuple[Path, dict[str, str], bytes],
) -> None:
    job_dir, job, _ = pdf_job
    (job_dir / "input/metadata.json").write_text(json.dumps({
        "title": "Empirical Asset Pricing via Machine Learning",
        "author": "Shihao Gu; Bryan Kelly; Dacheng Xiu",
        "published_at": "2018-12-24",
        "sitename": "NBER",
        "source_url": "https://www.nber.org/papers/w25398",
    }), encoding="utf-8")
    monkeypatch.setattr(ScholarlyPdfAdapter, "_pdf_info", lambda self: {
        "Pages": "1", "Title": "NBER WORKING PAPER SERIES", "Author": "",
    })
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_layout", lambda self: (_digital_pages(), "fixture_layout"),
    )

    document, _quality = parse_pdf_document(job_dir, job)

    metadata = document["metadata"]
    assert metadata["titles"]["original"] == (
        "Empirical Asset Pricing via Machine Learning"
    )
    assert [author["name"] for author in metadata["authors"]] == [
        "Shihao Gu", "Bryan Kelly", "Dacheng Xiu",
    ]
    assert metadata["published_at"] == "2018-12-24"
    assert metadata["publisher"] == "NBER"
    assert metadata["identifiers"]["nber_working_paper"] == "w25398"


def test_pdf_cover_combines_multiline_title_authors_date_and_report_id(
    monkeypatch: pytest.MonkeyPatch,
    pdf_job: tuple[Path, dict[str, str], bytes],
) -> None:
    job_dir, job, _ = pdf_job
    page = PageLayout(1, 612, 792, text_items=[
        LayoutItem("Roofline: An Insightful Visual Performance Model for", [80, 95, 532, 112]),
        LayoutItem("Floating-Point Programs and Multicore Architectures", [79, 113, 532, 130]),
        LayoutItem("Samuel Webb Williams", [224, 257, 368, 270]),
        LayoutItem("Andrew Waterman", [224, 272, 340, 285]),
        LayoutItem("David A. Patterson", [224, 287, 341, 300]),
        LayoutItem("Electrical Engineering and Computer Sciences", [224, 473, 513, 486]),
        LayoutItem("Technical Report No. UCB/EECS-2008-134", [224, 536, 417, 545]),
        LayoutItem("October 17, 2008", [224, 578, 332, 591]),
    ])
    monkeypatch.setattr(ScholarlyPdfAdapter, "_pdf_info", lambda self: {"Pages": "16"})
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_layout", lambda self: ([page], "fixture_layout"),
    )

    document, _quality = parse_pdf_document(job_dir, job)

    metadata = document["metadata"]
    assert metadata["titles"]["original"] == (
        "Roofline: An Insightful Visual Performance Model for "
        "Floating-Point Programs and Multicore Architectures"
    )
    assert [author["name"] for author in metadata["authors"]] == [
        "Samuel Webb Williams", "Andrew Waterman", "David A. Patterson",
    ]
    assert metadata["published_at"] == "2008-10-17"
    assert metadata["identifiers"]["report_number"] == "UCB/EECS-2008-134"


def test_pdf_labeled_cover_uses_article_title_and_complete_author_lineup(
    monkeypatch: pytest.MonkeyPatch,
    pdf_job: tuple[Path, dict[str, str], bytes],
) -> None:
    job_dir, job, _ = pdf_job
    job = {
        **job,
        "url": "https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2326253",
    }
    page = PageLayout(1, 595, 842, text_items=[
        LayoutItem("Lawrence Berkeley National Laboratory", [50, 58, 407, 74]),
        LayoutItem("LBL Publications", [50, 78, 180, 92]),
        LayoutItem("Title", [50, 111, 80, 123]),
        LayoutItem("Backtest Overfitting in Financial Markets", [50, 127, 277, 141]),
        LayoutItem("Authors", [50, 202, 103, 214]),
        LayoutItem("Bailey, David H", [50, 219, 136, 233]),
        LayoutItem("Borwein, Jonathan M", [50, 235, 165, 249]),
        LayoutItem("Lopez de Prado, Marcos", [50, 252, 183, 266]),
        LayoutItem("et al.", [50, 269, 79, 283]),
        LayoutItem("Publication Date", [50, 298, 162, 310]),
        LayoutItem("2016-02-09", [50, 314, 114, 328]),
        LayoutItem("Backtest overfitting in financial markets", [128, 412, 468, 427]),
        LayoutItem("David H. Bailey", [123, 444, 205, 455]),
        LayoutItem("Jonathan M. Borwein", [241, 444, 352, 455]),
        LayoutItem("Amir Salehipour", [383, 444, 468, 455]),
        LayoutItem("Marcos Ló", [190, 464, 244, 475]),
        LayoutItem("pez de Prado", [238, 464, 311, 475]),
        LayoutItem("Qiji Zhu", [358, 464, 401, 475]),
        LayoutItem("February 9, 2016", [254, 488, 340, 499]),
        LayoutItem("Introduction", [96, 544, 185, 557]),
    ])
    monkeypatch.setattr(ScholarlyPdfAdapter, "_pdf_info", lambda self: {"Pages": "9"})
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_layout", lambda self: ([page], "fixture_layout"),
    )

    document, _quality = parse_pdf_document(job_dir, job)

    metadata = document["metadata"]
    assert metadata["titles"]["original"] == "Backtest Overfitting in Financial Markets"
    assert [author["name"] for author in metadata["authors"]] == [
        "David H. Bailey", "Jonathan M. Borwein", "Amir Salehipour",
        "Marcos López de Prado", "Qiji Zhu",
    ]
    assert metadata["published_at"] == "2016-02-09"
    assert metadata["identifiers"]["ssrn_id"] == "2326253"


def test_digital_pdf_restores_reliable_table_cells_with_bbox(
    monkeypatch: pytest.MonkeyPatch,
    pdf_job: tuple[Path, dict[str, str], bytes],
) -> None:
    job_dir, job, _ = pdf_job
    page = PageLayout(1, 600, 800, text_items=[
        LayoutItem("Paper title", [60, 40, 500, 70]),
        LayoutItem("Table 1: Benchmark results", [60, 200, 500, 220]),
        LayoutItem("Model", [70, 235, 180, 252]),
        LayoutItem("Accuracy", [260, 235, 390, 252]),
        LayoutItem("Baseline", [70, 265, 180, 282]),
        LayoutItem("91.2", [260, 265, 390, 282]),
        LayoutItem("Flori", [70, 295, 180, 312]),
        LayoutItem("98.4", [260, 295, 390, 312]),
    ])
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_pdf_info",
        lambda self: {"Pages": "1", "Title": "Paper title"},
    )
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_layout", lambda self: ([page], "fixture_layout"),
    )

    document, quality = parse_pdf_document(job_dir, job)

    table = document["tables"][0]
    assert table["extraction"]["status"] == "complete"
    assert [(cell["row"], cell["col"], cell["text"]) for cell in table["cells"]] == [
        (0, 0, "Model"), (0, 1, "Accuracy"),
        (1, 0, "Baseline"), (1, 1, "91.2"),
        (2, 0, "Flori"), (2, 1, "98.4"),
    ]
    assert all(cell["source_locator"]["pdf"]["bboxes"] for cell in table["cells"])
    assert quality["metrics"]["table_cell_count"] == 6
    assert "pdf_table_structure_unavailable" not in quality["reasons"]


def test_scanned_pdf_failed_ocr_is_explicitly_rejected(
    monkeypatch: pytest.MonkeyPatch,
    pdf_job: tuple[Path, dict[str, str], bytes],
) -> None:
    job_dir, job, _ = pdf_job
    monkeypatch.setattr(ScholarlyPdfAdapter, "_pdf_info", lambda self: {"Pages": "2"})
    monkeypatch.setattr(ScholarlyPdfAdapter, "_layout", lambda self: ([
        PageLayout(1, 600, 800), PageLayout(2, 600, 800),
    ], "fixture_scan"))
    monkeypatch.setattr(ScholarlyPdfAdapter, "_ocr_layout", lambda self: [])

    document, quality = parse_pdf_document(job_dir, job)

    assert document["source_profile"] == "scanned_pdf"
    assert document["capabilities"] == ["pdf", "ocr", "page_bbox"]
    assert document["blocks"] == []
    assert quality["status"] == "rejected"
    assert set(quality["reasons"]) >= {
        "scanned_pdf_source", "scanned_pdf_ocr_failed", "pdf_title_missing",
    }
    assert quality["metrics"]["scan_detected"] is True


def test_forced_scanned_pdf_with_partial_ocr_is_degraded_not_complete(
    monkeypatch: pytest.MonkeyPatch,
    pdf_job: tuple[Path, dict[str, str], bytes],
) -> None:
    job_dir, job, _ = pdf_job
    job = {**job, "source_profile": "scanned_pdf"}
    monkeypatch.setattr(ScholarlyPdfAdapter, "_pdf_info", lambda self: {
        "Pages": "1", "Title": "Scanned paper",
    })
    monkeypatch.setattr(ScholarlyPdfAdapter, "_layout", lambda self: ([PageLayout(
        1, 600, 800, [LayoutItem("OCR recovered title and one paragraph.", [20, 20, 400, 50])],
    )], "fixture_ocr"))

    document, quality = parse_pdf_document(job_dir, job)

    assert document["source_profile"] == "scanned_pdf"
    assert len(document["blocks"]) == 1
    assert quality["status"] == "degraded"
    assert quality["reasons"] == ["scanned_pdf_source"]


def test_scanned_pdf_ocr_publishes_page_bbox_and_confidence(
    monkeypatch: pytest.MonkeyPatch,
    pdf_job: tuple[Path, dict[str, str], bytes],
) -> None:
    job_dir, job, _ = pdf_job
    monkeypatch.setattr(ScholarlyPdfAdapter, "_pdf_info", lambda self: {"Pages": "1"})
    monkeypatch.setattr(
        ScholarlyPdfAdapter,
        "_layout",
        lambda self: ([PageLayout(1, 600, 800)], "fixture_scan"),
    )
    monkeypatch.setattr(
        ScholarlyPdfAdapter,
        "_ocr_layout",
        lambda self: [PageLayout(
            1, 600, 800,
            [LayoutItem("Recovered scanned paragraph", [20, 40, 400, 72], 0.97)],
        )],
    )

    document, quality = parse_pdf_document(job_dir, job)

    assert document["source_profile"] == "scanned_pdf"
    pdf_locator = document["blocks"][0]["locator"]["pdf"]
    assert pdf_locator["page"] == 1
    assert pdf_locator["bboxes"] == [[20, 40, 400, 72]]
    assert pdf_locator["source_id"] == "pdf"
    assert pdf_locator["source_fingerprint"].startswith("sha256:")
    assert document["blocks"][0]["ocr_confidence"] == 0.97
    assert quality["status"] == "degraded"
    assert set(quality["reasons"]) >= {
        "scanned_pdf_source", "scanned_pdf_ocr_applied", "pdf_title_inferred",
    }
    assert quality["metrics"]["ocr_confidence_min"] == 0.97


def test_low_confidence_ocr_cannot_publish_exact_quote_support(
    monkeypatch: pytest.MonkeyPatch,
    pdf_job: tuple[Path, dict[str, str], bytes],
) -> None:
    job_dir, job, _ = pdf_job
    monkeypatch.setattr(ScholarlyPdfAdapter, "_pdf_info", lambda self: {"Pages": "1"})
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_layout",
        lambda self: ([PageLayout(1, 600, 800)], "fixture_scan"),
    )
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_ocr_layout",
        lambda self: [PageLayout(
            1, 600, 800,
            [LayoutItem("Uncertain OCR text", [20, 40, 400, 72], 0.41)],
        )],
    )

    document, quality = parse_pdf_document(job_dir, job)
    manifest = build_document_source_manifest(job_dir, document)

    locator = document["blocks"][0]["locator"]["pdf"]
    assert locator["ocr_confidence"] == 0.41
    assert "scanned_pdf_ocr_low_confidence" in quality["reasons"]
    assert manifest["segments"][0]["support_text"] is None
    assert manifest["segments"][0]["support_artifact"] is None


def test_pdf_xml_parsers_preserve_coordinates() -> None:
    html_pages = ScholarlyPdfAdapter._parse_pdftohtml("""<pdf2xml>
      <page number="2" width="600" height="800">
        <text top="10" left="20" width="100" height="15">Hello <b>PDF</b></text>
        <image top="40" left="50" width="200" height="120" src="panel.png"/>
      </page>
    </pdf2xml>""")
    assert html_pages == [PageLayout(
        number=2,
        width=600.0,
        height=800.0,
        text_items=[LayoutItem("Hello PDF", [20.0, 10.0, 120.0, 25.0])],
        image_bboxes=[[50.0, 40.0, 250.0, 160.0]],
    )]

    text_pages = ScholarlyPdfAdapter._parse_pdftotext("""<doc><page width="600" height="800">
      <flow><block><line>
        <word xMin="20" yMin="10" xMax="60" yMax="25">Hello</word>
        <word xMin="65" yMin="10" xMax="95" yMax="25">PDF</word>
      </line></block></flow>
    </page></doc>""")
    assert text_pages[0].text_items == [LayoutItem("Hello PDF", [20.0, 10.0, 95.0, 25.0])]


def test_zero_area_text_bbox_falls_back_to_page_locator(
    monkeypatch: pytest.MonkeyPatch,
    pdf_job: tuple[Path, dict[str, str], bytes],
) -> None:
    job_dir, job, _ = pdf_job
    page = PageLayout(1, 600, 800, text_items=[
        LayoutItem("Paper title", [60, 40, 500, 70]),
        LayoutItem("M", [376, 222, 376, 232]),
    ])
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_pdf_info",
        lambda self: {"Pages": "1", "Title": "Paper title"},
    )
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_layout", lambda self: ([page], "fixture_layout"),
    )

    document, _quality = parse_pdf_document(job_dir, job)
    manifest = build_document_source_manifest(job_dir, document)

    glyph = next(block for block in document["blocks"] if block["text"] == "M")
    assert glyph["locator"]["pdf"]["bboxes"] == []
    segment = next(item for item in manifest["segments"] if item["segment_id"] == glyph["block_id"])
    assert segment["locator"] == {"kind": "pdf", "page": 1, "bbox": None}


def test_prose_figure_references_are_not_registered_as_figures(
    monkeypatch: pytest.MonkeyPatch,
    pdf_job: tuple[Path, dict[str, str], bytes],
) -> None:
    job_dir, job, _ = pdf_job
    page = PageLayout(1, 600, 800, text_items=[
        LayoutItem("Paper title", [60, 40, 500, 70]),
        LayoutItem("Figure 2 shows the measured roofline results.", [60, 100, 500, 120]),
        LayoutItem("Figure 1: Measured throughput.", [60, 400, 500, 425]),
    ], image_bboxes=[[60, 180, 500, 380]])
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_pdf_info",
        lambda self: {"Pages": "1", "Title": "Paper title"},
    )
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_layout", lambda self: ([page], "fixture_layout"),
    )

    document, _quality = parse_pdf_document(job_dir, job)

    assert [figure["label"] for figure in document["figures"]] == ["Figure 1"]
    assert any(
        block["kind"] == "paragraph" and block["text"].startswith("Figure 2 shows")
        for block in document["blocks"]
    )


def test_prose_table_references_are_not_registered_as_tables(
    monkeypatch: pytest.MonkeyPatch,
    pdf_job: tuple[Path, dict[str, str], bytes],
) -> None:
    job_dir, job, _ = pdf_job
    page = PageLayout(1, 600, 800, text_items=[
        LayoutItem("Paper title", [60, 40, 500, 70]),
        LayoutItem("Table 2 shows the measured throughput.", [60, 100, 500, 120]),
        LayoutItem("Table 1 | Benchmark results", [60, 400, 500, 425]),
    ])
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_pdf_info",
        lambda self: {"Pages": "1", "Title": "Paper title"},
    )
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_layout", lambda self: ([page], "fixture_layout"),
    )

    document, _quality = parse_pdf_document(job_dir, job)

    assert [table["label"] for table in document["tables"]] == ["Table 1"]
    assert any(
        block["kind"] == "paragraph" and block["text"].startswith("Table 2 shows")
        for block in document["blocks"]
    )


@pytest.mark.parametrize("separator", [":", ".", "-", "–", "—", "|"])
def test_visual_caption_labels_accept_common_separators(separator: str) -> None:
    assert _FIGURE_CAPTION.match(f"Figure A1 {separator} Result")
    assert _TABLE_CAPTION.match(f"Table 2 {separator} Result")


def test_multiline_figure_caption_uses_nearest_image_row(
    monkeypatch: pytest.MonkeyPatch,
    pdf_job: tuple[Path, dict[str, str], bytes],
) -> None:
    job_dir, job, _ = pdf_job
    page = PageLayout(1, 600, 800, text_items=[
        LayoutItem("Paper title", [60, 40, 500, 70]),
        LayoutItem("Figure 1: Roofline performance for floating-point", [60, 500, 500, 520]),
        LayoutItem("programs and multicore architectures.", [60, 522, 500, 542]),
    ], image_bboxes=[
        [60, 100, 500, 240],
        [60, 330, 275, 485], [285, 330, 500, 485],
    ])
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_pdf_info",
        lambda self: {"Pages": "1", "Title": "Paper title"},
    )
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_layout", lambda self: ([page], "fixture_layout"),
    )

    document, _quality = parse_pdf_document(job_dir, job)

    figure = document["figures"][0]
    assert figure["caption"] == (
        "Figure 1: Roofline performance for floating-point "
        "programs and multicore architectures."
    )
    assert len(figure["media"]) == 2
    assert [media["source_locator"]["pdf"]["bboxes"][0] for media in figure["media"]] == [
        [60, 330, 275, 485], [285, 330, 500, 485],
    ]
    assert not any(block["text"].startswith("programs and") for block in document["blocks"])


def test_vector_figure_with_top_caption_crops_until_note(
    monkeypatch: pytest.MonkeyPatch,
    pdf_job: tuple[Path, dict[str, str], bytes],
) -> None:
    job_dir, job, _ = pdf_job
    page = PageLayout(1, 600, 800, text_items=[
        LayoutItem("Paper title", [60, 40, 500, 65]),
        LayoutItem("Figure 1: Regression Tree Example", [180, 80, 420, 100]),
        LayoutItem("Category 1", [90, 230, 150, 245]),
        LayoutItem("Category 2", [350, 230, 410, 245]),
        LayoutItem("Note: The panels show equivalent models.", [60, 350, 500, 370]),
    ])
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_pdf_info",
        lambda self: {"Pages": "1", "Title": "Paper title"},
    )
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_layout", lambda self: ([page], "fixture_layout"),
    )

    document, _quality = parse_pdf_document(job_dir, job)

    bbox = document["figures"][0]["media"][0]["source_locator"]["pdf"]["bboxes"][0]
    assert bbox == [48.0, 100, 552.0, 350]


def test_vector_figure_below_right_column_uses_bounded_crop(
    monkeypatch: pytest.MonkeyPatch,
    pdf_job: tuple[Path, dict[str, str], bytes],
) -> None:
    job_dir, job, _ = pdf_job
    page = PageLayout(1, 600, 800, text_items=[
        LayoutItem("Paper title", [60, 40, 500, 65]),
        LayoutItem("Parameters", [340, 210, 410, 225]),
        LayoutItem("KV Cache", [430, 220, 490, 235]),
        LayoutItem("Figure 1: Memory layout and throughput.", [310, 316, 550, 336]),
    ])
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_pdf_info",
        lambda self: {"Pages": "1", "Title": "Paper title"},
    )
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_layout", lambda self: ([page], "fixture_layout"),
    )

    document, _quality = parse_pdf_document(job_dir, job)

    bbox = document["figures"][0]["media"][0]["source_locator"]["pdf"]["bboxes"][0]
    assert bbox == [310.0, 172.0, 552.0, 316]


def test_pdf_primary_layout_extracts_images_only_in_temporary_directory(
    monkeypatch: pytest.MonkeyPatch,
    pdf_job: tuple[Path, dict[str, str], bytes],
) -> None:
    job_dir, job, _ = pdf_job
    adapter = ScholarlyPdfAdapter(job_dir, job)
    calls: list[tuple[list[str], str | None]] = []

    def fake_run(
        command: list[str],
        timeout: int = 120,
        *,
        cwd: str | None = None,
    ) -> str:
        del timeout
        calls.append((command, cwd))
        assert cwd is not None and Path(cwd).is_dir()
        return """<pdf2xml><page number="1" width="600" height="800">
          <text top="10" left="20" width="100" height="15">Paper title</text>
          <image top="40" left="50" width="200" height="120" src="figure.png"/>
        </page></pdf2xml>"""

    monkeypatch.setattr(adapter, "_run", fake_run)

    pages, method = adapter._layout()

    assert method == "pdftohtml_xml"
    assert pages[0].image_bboxes == [[50.0, 40.0, 250.0, 160.0]]
    assert calls[0][0][0] == "pdftohtml"
    assert "-i" not in calls[0][0]
    assert calls[0][0][calls[0][0].index("-zoom") + 1] == "1"


def test_pdf_whitepaper_uses_same_kind_with_digital_profile(
    monkeypatch: pytest.MonkeyPatch,
    pdf_job: tuple[Path, dict[str, str], bytes],
) -> None:
    job_dir, job, _ = pdf_job
    job = {**job, "document_kind": "whitepaper"}
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_pdf_info",
        lambda self: {"Pages": "1", "Title": "A Systems Whitepaper"},
    )
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_layout", lambda self: (_digital_pages(), "fixture_layout"),
    )

    document, _quality = parse_pdf_document(job_dir, job)

    assert document["content_type"] == "document"
    assert document["document_kind"] == "whitepaper"
    assert document["source_profile"] == "digital_pdf"
    assert {"pdf", "text_layer", "page_bbox"} == set(document["capabilities"])


def test_pdf_rejects_source_fingerprint_mismatch(pdf_job: tuple[Path, dict[str, str], bytes]) -> None:
    job_dir, job, _ = pdf_job
    with pytest.raises(ValueError, match="fingerprint mismatch"):
        parse_pdf_document(job_dir, {**job, "source_fingerprint": "sha256:" + "0" * 64})
