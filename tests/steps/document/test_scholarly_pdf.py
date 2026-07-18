"""验证 PDF 页级定位、数字版图表提取和扫描件 fail-closed。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from shared.document_contract import validate_document, validate_quality
from steps.document.adapters import parse_pdf_document
from steps.document.adapters.scholarly_pdf import (
    _caption_text,
    _FIGURE_CAPTION,
    _TABLE_CAPTION,
    _normalize_date,
    LayoutItem,
    PageLayout,
    ScholarlyPdfAdapter,
)
from steps.document.layout_detector import (
    DocumentLayoutDetector,
    LayoutDetection,
    LayoutDetectorError,
)
from steps.document.provenance import build_document_source_manifest


def _fingerprint(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


@pytest.mark.parametrize(("raw", "expected"), [
    ("noted with a gray bar. Published May 20, 2014.", "2014-05-20"),
    ("2023-07", "2023-07"),
    ("2013", "2013"),
    ("not a publication date", ""),
])
def test_normalize_date_never_returns_prose(raw: str, expected: str) -> None:
    assert _normalize_date(raw) == expected


def test_cover_date_accepts_year_from_publication_footer() -> None:
    page = PageLayout(1, 600, 800, text_items=[
        LayoutItem("Published in the Proceedings of OSDI 2012", [40, 760, 300, 775]),
    ])

    assert ScholarlyPdfAdapter._cover_date([page]) == "2012"


def test_cover_date_expands_short_year_from_venue_footer() -> None:
    page = PageLayout(1, 600, 800, text_items=[
        LayoutItem(
            "USENIX Association OSDI ’04: 6th Symposium on Operating Systems",
            [40, 760, 500, 775],
        ),
    ])

    assert ScholarlyPdfAdapter._cover_date([page]) == "2004"


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
    assert len(document["figures"][0]["media"]) == 1
    assert document["figures"][0]["media"][0]["source_locator"]["pdf"]["bboxes"] == [
        [70, 170, 530, 315],
    ]
    assert len(document["assets"]) == 1
    assert len(document["tables"]) == 1
    assert document["tables"][0]["extraction"]["status"] == "degraded"
    assert document["tables"][0]["representations"] == []
    assert document["tables"][0]["source_locator"]["pdf"]["page"] == 1
    assert document["tables"][0]["source_locator"]["pdf"]["bboxes"] == []
    assert {reference["kind"] for reference in document["references"]} == {
        "external", "citation",
    }
    assert quality["status"] == "degraded"
    assert quality["reasons"] == [
        "pdf_table_crop_ambiguous", "pdf_table_structure_unavailable",
    ]
    assert quality["metrics"]["layout_method"] == "fixture_layout"
    assert quality["metrics"]["figure_panel_count"] == 1


def test_layout_detector_expands_figure_and_bounds_table_by_caption(
    monkeypatch: pytest.MonkeyPatch,
    pdf_job: tuple[Path, dict[str, str], bytes],
) -> None:
    job_dir, job, _raw = pdf_job

    class FakeDetector:
        model_identity = "sha256:fixture"

        @staticmethod
        def detect_pdf_page(
            _source: Path,
            *,
            page: int,
            page_width: float,
            page_height: float,
        ) -> list[LayoutDetection]:
            assert (page, page_width, page_height) == (1, 600, 800)
            return [
                LayoutDetection("figure", 0.97, (65, 95, 535, 315)),
                LayoutDetection("figure_caption", 0.94, (68, 329, 532, 356)),
                LayoutDetection("table_caption", 0.93, (68, 499, 532, 526)),
                LayoutDetection("table", 0.96, (72, 535, 528, 610)),
            ]

    monkeypatch.setattr(
        DocumentLayoutDetector,
        "from_env",
        classmethod(lambda cls: FakeDetector()),
    )
    monkeypatch.setattr(ScholarlyPdfAdapter, "_pdf_info", lambda self: {
        "Pages": "1", "Title": "FlashAttention", "Author": "Tri Dao",
    })
    monkeypatch.setattr(
        ScholarlyPdfAdapter,
        "_layout",
        lambda self: (_digital_pages(), "fixture_layout"),
    )

    document, quality = parse_pdf_document(job_dir, job)

    figure_box = document["figures"][0]["source_locator"]["pdf"]["bboxes"][0]
    table_box = document["tables"][0]["source_locator"]["pdf"]["bboxes"][0]
    assert figure_box == [63.0, 93.0, 537.0, 317.0]
    assert table_box == [70.0, 533.0, 530.0, 612.0]
    assert quality["metrics"]["layout_detector_enabled"] is True
    assert quality["metrics"]["layout_detector_pages"] == 1
    assert quality["metrics"]["layout_detector_figure_matches"] == 1
    assert quality["metrics"]["layout_detector_table_matches"] == 1
    assert quality["metrics"]["layout_detector_failures"] == 0


def test_layout_detector_failure_disables_model_and_keeps_geometry_fallback(
    monkeypatch: pytest.MonkeyPatch,
    pdf_job: tuple[Path, dict[str, str], bytes],
) -> None:
    job_dir, job, _raw = pdf_job

    class FailingDetector:
        model_identity = "sha256:fixture"
        calls = 0

        @classmethod
        def detect_pdf_page(cls, *_args, **_kwargs) -> list[LayoutDetection]:
            cls.calls += 1
            raise LayoutDetectorError("fixture inference failure")

    monkeypatch.setattr(
        DocumentLayoutDetector,
        "from_env",
        classmethod(lambda cls: FailingDetector()),
    )
    monkeypatch.setattr(ScholarlyPdfAdapter, "_pdf_info", lambda self: {
        "Pages": "1", "Title": "FlashAttention", "Author": "Tri Dao",
    })
    monkeypatch.setattr(
        ScholarlyPdfAdapter,
        "_layout",
        lambda self: (_digital_pages(), "fixture_layout"),
    )

    document, quality = parse_pdf_document(job_dir, job)

    assert FailingDetector.calls == 1
    assert document["figures"][0]["source_locator"]["pdf"]["bboxes"] == [
        [70, 170, 530, 315],
    ]
    assert quality["metrics"]["layout_detector_enabled"] is True
    assert quality["metrics"]["layout_detector_pages"] == 0
    assert quality["metrics"]["layout_detector_failures"] == 1
    assert "pdf_layout_detector_failed" in quality["reasons"]


def test_pdf_caption_keeps_more_than_sixteen_fragmented_text_items() -> None:
    items = [LayoutItem("Figure 1:", [10, 100, 70, 110])]
    words = [f"word{index}" for index in range(24)] + ["end."]
    for index, word in enumerate(words):
        row, column = divmod(index, 5)
        left = 10 + column * 42 if row else 74 + column * 42
        top = 100 + row * 11
        items.append(LayoutItem(word, [left, top, left + 38, top + 10]))
    items.append(LayoutItem("Body prose starts here.", [10, 170, 250, 182]))
    page = PageLayout(1, 600, 800, text_items=items)

    caption_items = ScholarlyPdfAdapter._figure_caption_items(page, 0)

    assert len(caption_items) == 26
    assert _caption_text([item for _index, item in caption_items]).endswith("end.")


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


@pytest.mark.parametrize(("title", "author_line", "container_author", "expected"), [
    (
        "MapReduce: Simplified Data Processing on Large Clusters",
        "Jeffrey Dean and Sanjay Ghemawat",
        "",
        ["Jeffrey Dean", "Sanjay Ghemawat"],
    ),
    (
        "In Search of an Understandable Consensus Algorithm",
        "Diego Ongaro and John Ousterhout",
        "ongardie",
        ["Diego Ongaro", "John Ousterhout"],
    ),
    (
        "Optimization of Conditional Value-at-Risk",
        "R. Tyrrell Rockafellar1 and Stanislav Uryasev2",
        "",
        ["R. Tyrrell Rockafellar", "Stanislav Uryasev"],
    ),
])
def test_pdf_cover_splits_authors_and_ignores_container_username(
    monkeypatch: pytest.MonkeyPatch,
    pdf_job: tuple[Path, dict[str, str], bytes],
    title: str,
    author_line: str,
    container_author: str,
    expected: list[str],
) -> None:
    job_dir, job, _ = pdf_job
    page = PageLayout(1, 612, 792, text_items=[
        LayoutItem(title, [60, 70, 550, 90]),
        LayoutItem(author_line, [120, 105, 490, 125]),
        LayoutItem("Google, Inc.", [200, 130, 350, 145]),
        LayoutItem("Abstract", [60, 180, 140, 195]),
        LayoutItem("Published May 20, 2014.", [60, 700, 250, 715]),
    ])
    monkeypatch.setattr(ScholarlyPdfAdapter, "_pdf_info", lambda self: {
        "Pages": "1", "Author": container_author,
    })
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_layout", lambda self: ([page], "fixture_layout"),
    )

    document, _quality = parse_pdf_document(job_dir, job)

    assert [item["name"] for item in document["metadata"]["authors"]] == expected
    assert document["metadata"]["published_at"] == "2014-05-20"


def test_pdf_cover_keeps_author_split_by_superscript_marker(
    monkeypatch: pytest.MonkeyPatch,
    pdf_job: tuple[Path, dict[str, str], bytes],
) -> None:
    job_dir, job, _ = pdf_job
    page = PageLayout(1, 612, 792, text_items=[
        LayoutItem("Optimization of Conditional Value-at-Risk", [80, 90, 530, 110]),
        LayoutItem("R. Tyrrell Rockafellar", [170, 150, 300, 168]),
        LayoutItem("1", [301, 145, 308, 155]),
        LayoutItem("and Stanislav Uryasev", [312, 150, 450, 168]),
        LayoutItem("2", [451, 145, 458, 155]),
        LayoutItem(
            "VaR as well. CVaR; also called Mean Excess Loss; Mean Shortfall; or Tail VaR",
            [55, 215, 555, 235],
        ),
        LayoutItem("September 5, 1999", [240, 500, 370, 518]),
    ])
    monkeypatch.setattr(ScholarlyPdfAdapter, "_pdf_info", lambda self: {"Pages": "1"})
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_layout", lambda self: ([page], "fixture_layout"),
    )

    document, _quality = parse_pdf_document(job_dir, job)

    assert [item["name"] for item in document["metadata"]["authors"]] == [
        "R. Tyrrell Rockafellar", "Stanislav Uryasev",
    ]


def test_pdf_cover_splits_multiline_comma_separated_author_list(
    monkeypatch: pytest.MonkeyPatch,
    pdf_job: tuple[Path, dict[str, str], bytes],
) -> None:
    job_dir, job, _ = pdf_job
    page = PageLayout(1, 612, 792, text_items=[
        LayoutItem("Spanner: Google’s Globally-Distributed Database", [150, 100, 460, 115]),
        LayoutItem(
            "James C. Corbett, Jeffrey Dean, Michael Epstein, Andrew Fikes, "
            "Christopher Frost, JJ Furman,",
            [75, 143, 535, 154],
        ),
        LayoutItem(
            "Sanjay Ghemawat, Andrey Gubarev, Christopher Heiser, "
            "Peter Hochschild, Wilson Hsieh,",
            [90, 157, 520, 168],
        ),
        LayoutItem("Google, Inc.", [275, 223, 336, 234]),
        LayoutItem("Abstract", [160, 253, 207, 265]),
    ])
    monkeypatch.setattr(ScholarlyPdfAdapter, "_pdf_info", lambda self: {"Pages": "1"})
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_layout", lambda self: ([page], "fixture_layout"),
    )

    document, _quality = parse_pdf_document(job_dir, job)

    assert [item["name"] for item in document["metadata"]["authors"]] == [
        "James C. Corbett", "Jeffrey Dean", "Michael Epstein", "Andrew Fikes",
        "Christopher Frost", "JJ Furman", "Sanjay Ghemawat", "Andrey Gubarev",
        "Christopher Heiser", "Peter Hochschild", "Wilson Hsieh",
    ]


def test_repository_cover_reorders_semicolon_authors_and_reads_year(
    monkeypatch: pytest.MonkeyPatch,
    pdf_job: tuple[Path, dict[str, str], bytes],
) -> None:
    job_dir, job, _ = pdf_job
    page = PageLayout(1, 595, 842, text_items=[
        LayoutItem("Value and Momentum Everywhere", [57, 167, 317, 182]),
        LayoutItem(
            "Asness, Clifford S.; Moskowitz, Tobias; Heje Pedersen, Lasse",
            [57, 205, 386, 216],
        ),
        LayoutItem("Document Version", [57, 270, 139, 279]),
        LayoutItem("Accepted author manuscript", [57, 280, 182, 289]),
        LayoutItem("Published in:", [57, 310, 114, 319]),
        LayoutItem("Journal of Finance", [57, 320, 139, 329]),
        LayoutItem("Publication date:", [57, 390, 131, 399]),
        LayoutItem("2013", [57, 400, 79, 409]),
    ])
    monkeypatch.setattr(ScholarlyPdfAdapter, "_pdf_info", lambda self: {"Pages": "1"})
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_layout", lambda self: ([page], "fixture_layout"),
    )

    document, _quality = parse_pdf_document(job_dir, job)

    assert [item["name"] for item in document["metadata"]["authors"]] == [
        "Clifford S. Asness", "Tobias Moskowitz", "Lasse Heje Pedersen",
    ]
    assert document["metadata"]["published_at"] == "2013"


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


def test_multiline_figure_caption_uses_complete_nearest_image_cluster(
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
    assert len(figure["media"]) == 1
    assert figure["media"][0]["source_locator"]["pdf"]["bboxes"] == [
        [60, 330, 500, 485],
    ]
    assert not any(block["text"].startswith("programs and") for block in document["blocks"])


def test_pdf_figure_keeps_all_rows_of_six_panel_grid(
    monkeypatch: pytest.MonkeyPatch,
    pdf_job: tuple[Path, dict[str, str], bytes],
) -> None:
    job_dir, job, _ = pdf_job
    page = PageLayout(1, 600, 800, text_items=[
        LayoutItem("Paper title", [60, 40, 500, 70]),
        LayoutItem("Figure 3: Six workload panels.", [60, 500, 500, 520]),
    ], image_bboxes=[
        [60, 150, 275, 250], [285, 150, 500, 250],
        [60, 260, 275, 360], [285, 260, 500, 360],
        [60, 370, 275, 485], [285, 370, 500, 485],
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
    assert len(figure["media"]) == 1
    assert figure["media"][0]["source_locator"]["pdf"]["bboxes"] == [
        [60, 150, 500, 485],
    ]


def test_pdf_figure_keeps_tall_composite_near_bottom_caption(
    monkeypatch: pytest.MonkeyPatch,
    pdf_job: tuple[Path, dict[str, str], bytes],
) -> None:
    job_dir, job, _ = pdf_job
    page = PageLayout(1, 612, 792, text_items=[
        LayoutItem("Paper title", [54, 40, 558, 65]),
        LayoutItem("Figure A1. Three vertically stacked panels.", [329, 654, 560, 674]),
    ], image_bboxes=[[348, 72, 540, 647]])
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_pdf_info",
        lambda self: {"Pages": "1", "Title": "Paper title"},
    )
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_layout", lambda self: ([page], "fixture_layout"),
    )

    document, _quality = parse_pdf_document(job_dir, job)

    figure = document["figures"][0]
    assert figure["extraction"]["status"] == "complete"
    assert figure["media"][0]["source_locator"]["pdf"]["bboxes"] == [
        [348, 72, 540, 647],
    ]


def test_pdf_figure_with_top_caption_combines_following_image_grid(
    monkeypatch: pytest.MonkeyPatch,
    pdf_job: tuple[Path, dict[str, str], bytes],
) -> None:
    job_dir, job, _ = pdf_job
    page = PageLayout(1, 600, 800, text_items=[
        LayoutItem("Paper title", [60, 40, 500, 65]),
        LayoutItem("Figure 1: Results by workload.", [60, 90, 500, 110]),
        LayoutItem("Note: Error bars show standard deviation.", [60, 370, 500, 390]),
    ], image_bboxes=[
        [60, 120, 275, 230], [285, 120, 500, 230],
        [60, 240, 275, 350], [285, 240, 500, 350],
    ])
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_pdf_info",
        lambda self: {"Pages": "1", "Title": "Paper title"},
    )
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_layout", lambda self: ([page], "fixture_layout"),
    )

    document, _quality = parse_pdf_document(job_dir, job)

    assert document["figures"][0]["media"][0]["source_locator"]["pdf"]["bboxes"] == [
        [60, 120, 500, 350],
    ]


def test_pdf_table_caption_above_uses_own_column_and_stops_before_section(
    monkeypatch: pytest.MonkeyPatch,
    pdf_job: tuple[Path, dict[str, str], bytes],
) -> None:
    job_dir, job, _ = pdf_job
    page = PageLayout(1, 600, 800, text_items=[
        LayoutItem("Paper title", [60, 40, 500, 65]),
        LayoutItem("Left-column prose must stay out.", [60, 180, 285, 195]),
        LayoutItem("Table 1: Accelerator results.", [330, 160, 550, 175]),
        LayoutItem("Model", [330, 190, 390, 205]),
        LayoutItem("TFLOPS", [450, 190, 520, 205]),
        LayoutItem("A100", [330, 215, 390, 230]),
        LayoutItem("312", [450, 215, 480, 230]),
        LayoutItem("6. Evaluation", [330, 260, 470, 278]),
        LayoutItem("Following body text.", [330, 285, 550, 300]),
    ])
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_pdf_info",
        lambda self: {"Pages": "1", "Title": "Paper title"},
    )
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_layout", lambda self: ([page], "fixture_layout"),
    )

    document, _quality = parse_pdf_document(job_dir, job)

    bbox = document["tables"][0]["source_locator"]["pdf"]["bboxes"][0]
    assert 300 <= bbox[0] <= 330
    assert 520 <= bbox[2] <= 570
    assert 175 <= bbox[1] <= 190
    assert 230 <= bbox[3] < 260


def test_pdf_table_caption_below_crops_upward_without_previous_prose(
    monkeypatch: pytest.MonkeyPatch,
    pdf_job: tuple[Path, dict[str, str], bytes],
) -> None:
    job_dir, job, _ = pdf_job
    page = PageLayout(1, 600, 800, text_items=[
        LayoutItem("Paper title", [60, 40, 500, 65]),
        LayoutItem("Previous body text.", [60, 120, 285, 135]),
        LayoutItem("Model", [60, 190, 120, 205]),
        LayoutItem("Accuracy", [190, 190, 260, 205]),
        LayoutItem("Baseline", [60, 215, 120, 230]),
        LayoutItem("91.2", [190, 215, 230, 230]),
        LayoutItem("Table 2: Accuracy results.", [60, 245, 280, 260]),
        LayoutItem("Following body text.", [60, 280, 285, 295]),
    ])
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_pdf_info",
        lambda self: {"Pages": "1", "Title": "Paper title"},
    )
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_layout", lambda self: ([page], "fixture_layout"),
    )

    document, _quality = parse_pdf_document(job_dir, job)

    bbox = document["tables"][0]["source_locator"]["pdf"]["bboxes"][0]
    assert 45 <= bbox[0] <= 60
    assert 260 <= bbox[2] < 300
    assert 135 < bbox[1] <= 190
    assert 230 <= bbox[3] <= 245


def test_pdf_full_width_table_stops_before_two_column_body(
    monkeypatch: pytest.MonkeyPatch,
    pdf_job: tuple[Path, dict[str, str], bytes],
) -> None:
    job_dir, job, _ = pdf_job
    text_items = [
        LayoutItem("Paper title", [60, 40, 500, 65]),
        LayoutItem("Table 4: Full-width benchmark.", [80, 80, 520, 95]),
    ]
    for row, top in enumerate((110, 130, 150)):
        for column, left in enumerate((60, 160, 260, 360, 460)):
            text_items.append(LayoutItem(
                f"r{row}c{column}", [left, top, left + 45, top + 12],
            ))
    text_items.extend([
        LayoutItem("Left-column body text starts here.", [60, 200, 285, 215]),
        LayoutItem("Right-column body text starts here.", [315, 200, 540, 215]),
    ])
    page = PageLayout(1, 600, 800, text_items=text_items)
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_pdf_info",
        lambda self: {"Pages": "1", "Title": "Paper title"},
    )
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_layout", lambda self: ([page], "fixture_layout"),
    )

    document, _quality = parse_pdf_document(job_dir, job)

    bbox = document["tables"][0]["source_locator"]["pdf"]["bboxes"][0]
    assert bbox[0] <= 60
    assert bbox[2] >= 505
    assert 162 <= bbox[3] < 200


def test_pdf_single_column_boxed_table_keeps_all_rows_until_body_gap(
    monkeypatch: pytest.MonkeyPatch,
    pdf_job: tuple[Path, dict[str, str], bytes],
) -> None:
    job_dir, job, _ = pdf_job
    page = PageLayout(1, 600, 800, text_items=[
        LayoutItem("Paper title", [60, 40, 500, 65]),
        LayoutItem("Table 3: Kernel optimizations.", [60, 100, 285, 115]),
        LayoutItem("Memory Affinity", [60, 125, 130, 137]),
        LayoutItem("Reduce remote accesses.", [131, 125, 285, 137]),
        LayoutItem("The other socket.", [60, 138, 180, 150]),
        LayoutItem("Software Prefetching", [60, 154, 155, 166]),
        LayoutItem("Use hardware hints.", [156, 154, 285, 166]),
        LayoutItem("Keep streams contiguous.", [60, 167, 250, 179]),
        LayoutItem("Compress Data Structures", [60, 184, 180, 196]),
        LayoutItem("Use smaller indices.", [181, 184, 285, 196]),
        LayoutItem("Final table detail.", [60, 197, 210, 209]),
        LayoutItem("Following body text starts here.", [60, 224, 285, 236]),
    ])
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_pdf_info",
        lambda self: {"Pages": "1", "Title": "Paper title"},
    )
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_layout", lambda self: ([page], "fixture_layout"),
    )

    document, _quality = parse_pdf_document(job_dir, job)

    table = document["tables"][0]
    bbox = table["source_locator"]["pdf"]["bboxes"][0]
    assert 121 <= bbox[1] <= 125
    assert 209 <= bbox[3] < 224
    assert table["cells"] == []


def test_pdf_table_with_rotated_header_keeps_normal_rows_until_section(
    monkeypatch: pytest.MonkeyPatch,
    pdf_job: tuple[Path, dict[str, str], bytes],
) -> None:
    job_dir, job, _ = pdf_job
    page = PageLayout(1, 600, 800, text_items=[
        LayoutItem("Paper title", [60, 40, 500, 65]),
        LayoutItem("Table 1: Multicore characteristics.", [320, 100, 550, 115]),
        LayoutItem("Intel", [390, 116, 390, 165]),
        LayoutItem("AMD", [520, 116, 520, 165]),
        LayoutItem("ISA", [320, 170, 350, 182]),
        LayoutItem("x86", [390, 170, 420, 182]),
        LayoutItem("x86", [500, 170, 530, 182]),
        LayoutItem("Threads", [320, 184, 370, 196]),
        LayoutItem("8", [390, 184, 400, 196]),
        LayoutItem("8", [510, 184, 520, 196]),
        LayoutItem("6.2 Evaluation", [320, 210, 470, 226]),
        LayoutItem("Following body.", [320, 230, 550, 242]),
    ])
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_pdf_info",
        lambda self: {"Pages": "1", "Title": "Paper title"},
    )
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_layout", lambda self: ([page], "fixture_layout"),
    )

    document, _quality = parse_pdf_document(job_dir, job)

    bbox = document["tables"][0]["source_locator"]["pdf"]["bboxes"][0]
    assert bbox[1] <= 116
    assert 196 <= bbox[3] < 210


def test_pdf_table_uses_nearby_horizontal_rules_when_text_layout_is_ambiguous(
    monkeypatch: pytest.MonkeyPatch,
    pdf_job: tuple[Path, dict[str, str], bytes],
) -> None:
    job_dir, job, _ = pdf_job
    page = PageLayout(1, 600, 800, text_items=[
        LayoutItem("Paper title", [60, 40, 500, 65]),
        LayoutItem("Table 5: Sparse benchmark results.", [60, 100, 500, 115]),
        LayoutItem("Model A 91.2 Model B 92.4", [90, 150, 470, 165]),
        LayoutItem("Following body text.", [60, 260, 500, 275]),
    ])
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_pdf_info",
        lambda self: {"Pages": "1", "Title": "Paper title"},
    )
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_layout", lambda self: ([page], "fixture_layout"),
    )
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_table_region",
        lambda self, page, caption, caption_indexes: ([], []),
    )
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_page_table_rules",
        lambda self, page: [(125, 80, 520), (220, 80, 520)],
    )

    document, quality = parse_pdf_document(job_dir, job)

    table = document["tables"][0]
    assert table["source_locator"]["pdf"]["bboxes"] == [[78, 124, 522, 221]]
    assert "pdf_table_crop_rule_fallback" in quality["reasons"]
    assert "pdf_table_crop_ambiguous" not in quality["reasons"]


def test_pdf_rule_table_expands_incomplete_text_crop(
    monkeypatch: pytest.MonkeyPatch,
    pdf_job: tuple[Path, dict[str, str], bytes],
) -> None:
    job_dir, job, _ = pdf_job
    page = PageLayout(1, 600, 800, text_items=[
        LayoutItem("Paper title", [60, 40, 500, 65]),
        LayoutItem("Operation", [120, 75, 220, 90]),
        LayoutItem("Mode", [300, 75, 380, 90]),
        LayoutItem("Read-write", [120, 100, 220, 115]),
        LayoutItem("leader", [300, 100, 380, 115]),
        LayoutItem("Read-only", [120, 125, 220, 140]),
        LayoutItem("lock-free", [300, 125, 380, 140]),
        LayoutItem("Table 2: Types of reads and writes.", [160, 165, 440, 180]),
    ])
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_pdf_info",
        lambda self: {"Pages": "1", "Title": "Paper title"},
    )
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_layout", lambda self: ([page], "fixture_layout"),
    )
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_table_region",
        lambda self, page, caption, caption_indexes: (
            [109, 109, 503, 153],
            [(5, page.text_items[5]), (6, page.text_items[6])],
        ),
    )
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_page_table_rules",
        lambda self, page: [(70, 107, 506), (155, 107, 506)],
    )

    document, _quality = parse_pdf_document(job_dir, job)

    assert document["tables"][0]["source_locator"]["pdf"]["bboxes"] == [
        [105, 69, 508, 156],
    ]


def test_pdf_rule_table_ignores_section_heading_in_other_column(
    monkeypatch: pytest.MonkeyPatch,
    pdf_job: tuple[Path, dict[str, str], bytes],
) -> None:
    job_dir, job, _ = pdf_job
    page = PageLayout(1, 600, 800, text_items=[
        LayoutItem("Paper title", [60, 40, 500, 65]),
        LayoutItem("6 Experience", [73, 423, 147, 431]),
        LayoutItem("Unique implementations 269", [340, 401, 497, 407]),
        LayoutItem("Table 1: Jobs run in August.", [337, 442, 519, 449]),
    ])
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_pdf_info",
        lambda self: {"Pages": "1", "Title": "Paper title"},
    )
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_layout", lambda self: ([page], "fixture_layout"),
    )
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_table_region",
        lambda self, page, caption, caption_indexes: ([], []),
    )
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_page_table_rules",
        lambda self, page: [(300, 333, 523), (420, 333, 523)],
    )

    document, _quality = parse_pdf_document(job_dir, job)

    assert document["tables"][0]["source_locator"]["pdf"]["bboxes"] == [
        [331, 299, 525, 421],
    ]


def test_pdf_rule_tables_do_not_cross_another_table_caption(
    monkeypatch: pytest.MonkeyPatch,
    pdf_job: tuple[Path, dict[str, str], bytes],
) -> None:
    job_dir, job, _ = pdf_job
    page = PageLayout(1, 600, 800, text_items=[
        LayoutItem("Paper title", [60, 40, 500, 65]),
        LayoutItem("Table 1: Mean return.", [220, 230, 380, 240]),
        LayoutItem("Table 2: Covariance matrix.", [210, 405, 390, 415]),
    ])
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_pdf_info",
        lambda self: {"Pages": "1", "Title": "Paper title"},
    )
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_layout", lambda self: ([page], "fixture_layout"),
    )
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_table_region",
        lambda self, page, caption, caption_indexes: ([], []),
    )
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_page_table_rules",
        lambda self, page: [
            (130, 230, 372), (205, 230, 372),
            (305, 175, 428), (381, 175, 428),
        ],
    )

    document, _quality = parse_pdf_document(job_dir, job)

    assert [
        table["source_locator"]["pdf"]["bboxes"][0]
        for table in document["tables"]
    ] == [[228, 129, 374, 206], [173, 304, 430, 382]]


def test_pdf_split_same_line_caption_keeps_full_width_table_column(
    monkeypatch: pytest.MonkeyPatch,
    pdf_job: tuple[Path, dict[str, str], bytes],
) -> None:
    job_dir, job, _ = pdf_job
    page = PageLayout(1, 600, 800, text_items=[
        LayoutItem("Paper title", [60, 40, 500, 65]),
        LayoutItem("Table 8:", [72, 403, 110, 413]),
        LayoutItem("Best hedge and mini-", [114, 403, 531, 413]),
        LayoutItem("mum CVaR and", [72, 422, 200, 432]),
        LayoutItem("source", [204, 422, 400, 432]),
        LayoutItem("fidelity.", [72, 441, 130, 451]),
    ])
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_pdf_info",
        lambda self: {"Pages": "1", "Title": "Paper title"},
    )
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_layout", lambda self: ([page], "fixture_layout"),
    )
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_table_region",
        lambda self, page, caption, caption_indexes: ([], []),
    )
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_page_table_rules",
        lambda self, page: [(523, 158, 445), (619, 158, 445)],
    )

    document, _quality = parse_pdf_document(job_dir, job)

    table = document["tables"][0]
    assert table["caption"] == (
        "Table 8: Best hedge and minimum CVaR and source fidelity."
    )
    assert table["source_locator"]["pdf"]["bboxes"] == [
        [156, 522, 447, 620],
    ]


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


def test_tiny_raster_icon_does_not_replace_full_vector_figure(
    monkeypatch: pytest.MonkeyPatch,
    pdf_job: tuple[Path, dict[str, str], bytes],
) -> None:
    job_dir, job, _ = pdf_job
    page = PageLayout(1, 600, 800, text_items=[
        LayoutItem("Paper title", [60, 40, 500, 65]),
        LayoutItem("Figure 4. System overview.", [60, 200, 280, 215]),
    ], image_bboxes=[[250, 160, 270, 180]])
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_pdf_info",
        lambda self: {"Pages": "1", "Title": "Paper title"},
    )
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_layout", lambda self: ([page], "fixture_layout"),
    )

    document, quality = parse_pdf_document(job_dir, job)

    figure = document["figures"][0]
    assert figure["media"][0]["source_locator"]["pdf"]["bboxes"] == [
        [48.0, 56.0, 290.0, 200],
    ]
    assert figure["extraction"]["status"] == "degraded"
    assert "pdf_figure_crop_heuristic" in quality["reasons"]


def test_sparse_raster_icons_do_not_truncate_full_vector_figure(
    monkeypatch: pytest.MonkeyPatch,
    pdf_job: tuple[Path, dict[str, str], bytes],
) -> None:
    job_dir, job, _ = pdf_job
    page = PageLayout(1, 600, 800, text_items=[
        LayoutItem("Paper title", [60, 40, 500, 65]),
        LayoutItem("Figure 2: Neural Networks", [180, 80, 420, 100]),
        LayoutItem("Output Layer", [75, 112, 118, 120]),
        LayoutItem("Hidden Layer", [301, 174, 346, 182]),
        LayoutItem("Note: The panels compare two architectures.", [60, 275, 500, 290]),
    ], image_bboxes=[
        [138, 163, 160, 186],
        [356, 139, 389, 163],
        [361, 194, 387, 216],
    ])
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_pdf_info",
        lambda self: {"Pages": "1", "Title": "Paper title"},
    )
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_layout", lambda self: ([page], "fixture_layout"),
    )

    document, quality = parse_pdf_document(job_dir, job)

    figure = document["figures"][0]
    assert figure["media"][0]["source_locator"]["pdf"]["bboxes"] == [
        [48.0, 100, 552.0, 275],
    ]
    assert figure["extraction"]["status"] == "degraded"
    assert "pdf_figure_crop_heuristic" in quality["reasons"]


def test_vector_caption_near_page_top_can_describe_figure_above(
    monkeypatch: pytest.MonkeyPatch,
    pdf_job: tuple[Path, dict[str, str], bytes],
) -> None:
    job_dir, job, _ = pdf_job
    page = PageLayout(1, 600, 800, text_items=[
        LayoutItem("Paper title", [60, 20, 500, 40]),
        LayoutItem("Block 0", [100, 80, 140, 88]),
        LayoutItem("Internal fragmentation", [240, 95, 340, 103]),
        LayoutItem("Request B", [440, 120, 490, 128]),
        LayoutItem("Figure 3. KV cache memory management.", [60, 145, 540, 160]),
        LayoutItem("3 Memory Challenges", [60, 180, 250, 195]),
        LayoutItem("Following body text spans the complete column width.", [60, 210, 540, 225]),
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
    assert bbox == [48.0, 0.0, 552.0, 145]


def test_two_column_vector_crop_excludes_adjacent_prose(
    monkeypatch: pytest.MonkeyPatch,
    pdf_job: tuple[Path, dict[str, str], bytes],
) -> None:
    job_dir, job, _ = pdf_job
    page = PageLayout(1, 600, 800, text_items=[
        LayoutItem("Paper title", [60, 40, 500, 65]),
        LayoutItem("Adjacent prose fills the right column before the float.", [310, 365, 550, 380]),
        LayoutItem("Pretrained Weights", [405, 410, 500, 425]),
        LayoutItem("B = 0", [455, 440, 500, 455]),
        LayoutItem("Figure 1: Our reparametrization.", [400, 500, 545, 515]),
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
    assert 370 < bbox[0] < 400
    assert 510 < bbox[2] < 530
    assert bbox[3] == 500


def test_left_column_vector_crop_keeps_visual_wider_than_caption(
    monkeypatch: pytest.MonkeyPatch,
    pdf_job: tuple[Path, dict[str, str], bytes],
) -> None:
    job_dir, job, _ = pdf_job
    page = PageLayout(1, 600, 800, text_items=[
        LayoutItem("Paper title", [60, 40, 500, 65]),
        LayoutItem("Logical blocks", [60, 100, 125, 110]),
        LayoutItem("Physical blocks", [235, 100, 290, 110]),
        LayoutItem("Figure 8. Parallel sampling.", [95, 210, 250, 225]),
    ])
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_pdf_info",
        lambda self: {"Pages": "1", "Title": "Paper title"},
    )
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_layout", lambda self: ([page], "fixture_layout"),
    )
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_page_visual_raster", lambda self, page: None,
    )

    document, _quality = parse_pdf_document(job_dir, job)

    bbox = document["figures"][0]["media"][0]["source_locator"]["pdf"]["bboxes"][0]
    assert bbox == [48.0, 66.0, 290.0, 210]


def test_heuristic_vector_crop_tightens_to_rasterized_ink(
    monkeypatch: pytest.MonkeyPatch,
    pdf_job: tuple[Path, dict[str, str], bytes],
) -> None:
    job_dir, job, _ = pdf_job
    page = PageLayout(1, 600, 800, text_items=[
        LayoutItem("Paper title", [60, 40, 500, 65]),
        LayoutItem("Block 0", [350, 100, 390, 110]),
        LayoutItem("Block 1", [470, 150, 510, 160]),
        LayoutItem("Figure 9. Beam search example.", [370, 190, 540, 205]),
        LayoutItem("Following body text fills the right column.", [310, 225, 550, 240]),
    ])
    raster = Image.new("L", (600, 800), 255)
    ImageDraw.Draw(raster).line((60, 45, 540, 45), fill=0, width=1)
    ImageDraw.Draw(raster).rectangle((335, 82, 525, 172), fill=0)
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_pdf_info",
        lambda self: {"Pages": "1", "Title": "Paper title"},
    )
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_layout", lambda self: ([page], "fixture_layout"),
    )
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_page_visual_raster", lambda self, page: raster,
    )

    document, _quality = parse_pdf_document(job_dir, job)

    bbox = document["figures"][0]["media"][0]["source_locator"]["pdf"]["bboxes"][0]
    assert bbox == [331.0, 78.0, 529.36, 177.0]


def test_vector_crop_skips_leading_prose_and_section_heading(
    monkeypatch: pytest.MonkeyPatch,
    pdf_job: tuple[Path, dict[str, str], bytes],
) -> None:
    job_dir, job, _ = pdf_job
    page = PageLayout(1, 600, 800, text_items=[
        LayoutItem(
            "Evaluation details are identical across all datasets and model sizes.",
            [60, 345, 540, 360],
        ),
        LayoutItem("4.2.1. Language modelling", [60, 380, 240, 392]),
        LayoutItem("Pile subset", [220, 535, 300, 548]),
        LayoutItem("Figure 5. Pile evaluation.", [60, 600, 540, 615]),
    ])
    raster = Image.new("L", (600, 800), 255)
    draw = ImageDraw.Draw(raster)
    draw.rectangle((60, 345, 540, 360), fill=0)
    draw.rectangle((60, 380, 240, 392), fill=0)
    draw.rectangle((120, 410, 500, 575), fill=0)
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_pdf_info",
        lambda self: {"Pages": "1", "Title": "Paper title"},
    )
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_layout", lambda self: ([page], "fixture_layout"),
    )
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_page_visual_raster", lambda self, page: raster,
    )

    document, _quality = parse_pdf_document(job_dir, job)

    bbox = document["figures"][0]["media"][0]["source_locator"]["pdf"]["bboxes"][0]
    assert bbox == [116.0, 406.0, 505.0, 580.0]


def test_vector_crop_starts_after_previous_figure_caption(
    monkeypatch: pytest.MonkeyPatch,
    pdf_job: tuple[Path, dict[str, str], bytes],
) -> None:
    job_dir, job, _ = pdf_job
    page = PageLayout(1, 600, 800, text_items=[
        LayoutItem("Paper title", [60, 40, 500, 65]),
        LayoutItem("Figure 12. Previous experiment.", [60, 110, 540, 125]),
        LayoutItem("ShareGPT", [100, 160, 160, 170]),
        LayoutItem("Alpaca", [400, 160, 450, 170]),
        LayoutItem("Figure 13. Average batched requests.", [60, 260, 540, 275]),
    ])
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_pdf_info",
        lambda self: {"Pages": "1", "Title": "Paper title"},
    )
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_layout", lambda self: ([page], "fixture_layout"),
    )
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_page_visual_raster", lambda self, page: None,
    )

    document, _quality = parse_pdf_document(job_dir, job)

    figure = next(item for item in document["figures"] if item["label"] == "Figure 13")
    bbox = figure["media"][0]["source_locator"]["pdf"]["bboxes"][0]
    assert bbox[1] == 125
    assert bbox[3] == 260


def test_vector_crop_starts_after_full_multiline_previous_caption(
    monkeypatch: pytest.MonkeyPatch,
    pdf_job: tuple[Path, dict[str, str], bytes],
) -> None:
    job_dir, job, _ = pdf_job
    page = PageLayout(1, 600, 800, text_items=[
        LayoutItem("Paper title", [60, 40, 500, 65]),
        LayoutItem("Figure 14.", [60, 110, 125, 120]),
        LayoutItem("Previous full-width experiment", [127, 110, 520, 120]),
        LayoutItem("Caption continuation across the page.", [60, 125, 520, 137]),
        LayoutItem("Parallel sampling", [360, 165, 460, 175]),
        LayoutItem("Figure 15. Memory saving.", [330, 260, 540, 275]),
    ])
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_pdf_info",
        lambda self: {"Pages": "1", "Title": "Paper title"},
    )
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_layout", lambda self: ([page], "fixture_layout"),
    )
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_page_visual_raster", lambda self, page: None,
    )

    document, _quality = parse_pdf_document(job_dir, job)

    figure = next(item for item in document["figures"] if item["label"] == "Figure 15")
    bbox = figure["media"][0]["source_locator"]["pdf"]["bboxes"][0]
    assert bbox[1] == 137
    assert bbox[3] == 260


def test_vector_crop_removes_prose_before_full_width_figure(
    monkeypatch: pytest.MonkeyPatch,
    pdf_job: tuple[Path, dict[str, str], bytes],
) -> None:
    job_dir, job, _ = pdf_job
    page = PageLayout(1, 600, 800, text_items=[
        LayoutItem("Paper title", [60, 40, 500, 65]),
        LayoutItem("A broad prose line before the plot fills this complete row.", [60, 210, 540, 225]),
        LayoutItem("A second prose line establishes a reliable body-text block.", [60, 230, 540, 245]),
        LayoutItem("axis", [100, 270, 100, 280]),
        LayoutItem("WikiSQL", [170, 285, 220, 295]),
        LayoutItem("MNLI-matched", [380, 285, 455, 295]),
        LayoutItem("Figure 2. Validation accuracy.", [80, 410, 520, 425]),
    ])
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_pdf_info",
        lambda self: {"Pages": "1", "Title": "Paper title"},
    )
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_layout", lambda self: ([page], "fixture_layout"),
    )
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_page_visual_raster", lambda self, page: None,
    )

    document, _quality = parse_pdf_document(job_dir, job)

    bbox = document["figures"][0]["media"][0]["source_locator"]["pdf"]["bboxes"][0]
    assert bbox[1] == 245
    assert bbox[3] == 410


def test_vector_caption_below_plot_ignores_tabular_content_after_caption(
    monkeypatch: pytest.MonkeyPatch,
    pdf_job: tuple[Path, dict[str, str], bytes],
) -> None:
    job_dir, job, _ = pdf_job
    page = PageLayout(1, 600, 800, text_items=[
        LayoutItem("Paper title", [60, 40, 500, 65]),
        LayoutItem("Delta Wq", [130, 130, 190, 140]),
        LayoutItem("Delta Wv", [360, 130, 420, 140]),
        LayoutItem("Figure 4. Subspace similarity.", [80, 300, 520, 315]),
        LayoutItem("The following discussion spans the complete page width.", [60, 345, 540, 360]),
        LayoutItem("Another discussion line precedes a table.", [60, 365, 540, 380]),
        LayoutItem("0.32", [150, 430, 180, 440]),
        LayoutItem("21.67", [300, 430, 340, 440]),
    ])
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_pdf_info",
        lambda self: {"Pages": "1", "Title": "Paper title"},
    )
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_layout", lambda self: ([page], "fixture_layout"),
    )
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_page_visual_raster", lambda self, page: None,
    )

    document, _quality = parse_pdf_document(job_dir, job)

    bbox = document["figures"][0]["media"][0]["source_locator"]["pdf"]["bboxes"][0]
    assert bbox[3] == 300
    assert bbox[1] < 130


def test_vector_crop_keeps_repeated_axes_and_panel_labels_in_multirow_chart(
    monkeypatch: pytest.MonkeyPatch,
    pdf_job: tuple[Path, dict[str, str], bytes],
) -> None:
    job_dir, job, _ = pdf_job
    page = PageLayout(1, 600, 800, text_items=[
        LayoutItem("Request rate (req/s) Request rate (req/s) Request rate (req/s)", [70, 125, 530, 135]),
        LayoutItem("(a) OPT-13B (b) OPT-66B (c) OPT-175B", [80, 145, 520, 155]),
        LayoutItem("Request rate (req/s) Request rate (req/s) Request rate (req/s)", [70, 215, 530, 225]),
        LayoutItem("(d) OPT-13B (e) OPT-66B (f) OPT-175B", [80, 235, 520, 245]),
        LayoutItem("Figure 12. Single sequence generation.", [70, 280, 530, 295]),
    ])
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_pdf_info",
        lambda self: {"Pages": "1", "Title": "Paper title"},
    )
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_layout", lambda self: ([page], "fixture_layout"),
    )
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_page_visual_raster", lambda self, page: None,
    )

    document, _quality = parse_pdf_document(job_dir, job)

    bbox = document["figures"][0]["media"][0]["source_locator"]["pdf"]["bboxes"][0]
    assert bbox == [48.0, 24.0, 552.0, 280]


def test_blank_region_above_caption_is_not_replaced_by_body_prose_below(
    monkeypatch: pytest.MonkeyPatch,
    pdf_job: tuple[Path, dict[str, str], bytes],
) -> None:
    job_dir, job, _ = pdf_job
    page = PageLayout(1, 600, 800, text_items=[
        LayoutItem("Figure 1. Distribution of portfolio losses.", [70, 300, 530, 315]),
        LayoutItem("The constraints could eliminate variables from this optimization problem.", [70, 340, 530, 355]),
        LayoutItem("This formulation simplifies notation and facilitates comparisons.", [70, 365, 530, 380]),
        LayoutItem("F(x, alpha) = alpha + expected shortfall", [160, 420, 440, 435]),
    ])
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_pdf_info",
        lambda self: {"Pages": "1", "Title": "Paper title"},
    )
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_layout", lambda self: ([page], "fixture_layout"),
    )
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_page_visual_raster", lambda self, page: None,
    )

    document, _quality = parse_pdf_document(job_dir, job)

    bbox = document["figures"][0]["media"][0]["source_locator"]["pdf"]["bboxes"][0]
    assert bbox == [48.0, 44.0, 552.0, 300]


def test_landscape_figure_uses_full_page_width_and_tall_candidate(
    monkeypatch: pytest.MonkeyPatch,
    pdf_job: tuple[Path, dict[str, str], bytes],
) -> None:
    job_dir, job, _ = pdf_job
    page = PageLayout(1, 800, 600, text_items=[
        LayoutItem("Pair 4, Kennecott and Uniroyal", [250, 95, 550, 110]),
        LayoutItem("1.30", [70, 150, 95, 160]),
        LayoutItem("120", [700, 470, 725, 480]),
        LayoutItem("Figure 1", [70, 520, 120, 532]),
    ])
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_pdf_info",
        lambda self: {"Pages": "1", "Title": "Paper title"},
    )
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_layout", lambda self: ([page], "fixture_layout"),
    )
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_page_visual_raster", lambda self, page: None,
    )

    document, _quality = parse_pdf_document(job_dir, job)

    bbox = document["figures"][0]["media"][0]["source_locator"]["pdf"]["bboxes"][0]
    assert bbox == [64.0, 70.0, 736.0, 516.0]


def test_tall_vector_figure_expands_when_ink_touches_candidate_top(
    monkeypatch: pytest.MonkeyPatch,
    pdf_job: tuple[Path, dict[str, str], bytes],
) -> None:
    job_dir, job, _ = pdf_job
    page = PageLayout(1, 600, 800, text_items=[
        LayoutItem("1x2@16", [145, 55, 200, 65]),
        LayoutItem("Inception@32", [130, 180, 215, 190]),
        LayoutItem("Figure 3. Model architecture schematic.", [60, 300, 290, 315]),
    ])
    raster = Image.new("L", (600, 800), 255)
    ImageDraw.Draw(raster).rectangle((100, 40, 250, 280), fill=0)
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_pdf_info",
        lambda self: {"Pages": "1", "Title": "Paper title"},
    )
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_layout", lambda self: ([page], "fixture_layout"),
    )
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_page_visual_raster", lambda self, page: raster,
    )

    document, _quality = parse_pdf_document(job_dir, job)

    bbox = document["figures"][0]["media"][0]["source_locator"]["pdf"]["bboxes"][0]
    assert bbox == [96.0, 36.0, 255.0, 285.0]


def test_vector_figure_does_not_expand_past_note_boundary(
    monkeypatch: pytest.MonkeyPatch,
    pdf_job: tuple[Path, dict[str, str], bytes],
) -> None:
    job_dir, job, _ = pdf_job
    page = PageLayout(1, 600, 800, text_items=[
        LayoutItem("Figure 6. Marginal association.", [100, 80, 500, 95]),
        LayoutItem("-1 -0.5 0 0.5 1", [100, 330, 500, 345]),
        LayoutItem("Note: Other covariates are fixed at median values.", [70, 365, 530, 375]),
        LayoutItem("Following body prose must remain outside the visual.", [70, 410, 530, 425]),
    ])
    raster = Image.new("L", (600, 800), 255)
    draw = ImageDraw.Draw(raster)
    draw.rectangle((80, 105, 520, 350), fill=0)
    draw.text((70, 365), "Note", fill=0)
    draw.rectangle((70, 410, 530, 425), fill=0)
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_pdf_info",
        lambda self: {"Pages": "1", "Title": "Paper title"},
    )
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_layout", lambda self: ([page], "fixture_layout"),
    )
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_page_visual_raster", lambda self, page: raster,
    )

    document, _quality = parse_pdf_document(job_dir, job)

    bbox = document["figures"][0]["media"][0]["source_locator"]["pdf"]["bboxes"][0]
    assert bbox[3] <= 365


def test_vector_figure_expands_incrementally_without_absorbing_previous_table(
    monkeypatch: pytest.MonkeyPatch,
    pdf_job: tuple[Path, dict[str, str], bytes],
) -> None:
    job_dir, job, _ = pdf_job
    page = PageLayout(1, 600, 800, text_items=[
        LayoutItem("Table IV", [120, 50, 220, 65]),
        LayoutItem("Accuracy Precision Recall", [60, 90, 290, 105]),
        LayoutItem("Down Stationary Up", [80, 175, 270, 190]),
        LayoutItem("Down Stationary Up", [80, 335, 270, 350]),
        LayoutItem("Figure 5. Confusion matrices.", [50, 376, 295, 391]),
    ])
    raster = Image.new("L", (600, 800), 255)
    draw = ImageDraw.Draw(raster)
    draw.rectangle((55, 45, 295, 150), fill=0)
    draw.rectangle((55, 170, 295, 360), fill=0)
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_pdf_info",
        lambda self: {"Pages": "1", "Title": "Paper title"},
    )
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_layout", lambda self: ([page], "fixture_layout"),
    )
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_page_visual_raster", lambda self, page: raster,
    )

    document, _quality = parse_pdf_document(job_dir, job)

    bbox = document["figures"][0]["media"][0]["source_locator"]["pdf"]["bboxes"][0]
    assert bbox == [51.0, 166.0, 290.0, 365.0]


def test_vector_crop_discards_isolated_running_header_band(
    monkeypatch: pytest.MonkeyPatch,
    pdf_job: tuple[Path, dict[str, str], bytes],
) -> None:
    job_dir, job, _ = pdf_job
    page = PageLayout(1, 600, 800, text_items=[
        LayoutItem("JOURNAL OF HIGH CLASS FILES", [60, 20, 300, 28]),
        LayoutItem("Stock", [240, 100, 280, 110]),
        LayoutItem("Figure 7. Normalised daily profits.", [60, 250, 540, 265]),
    ])
    raster = Image.new("L", (600, 800), 255)
    ImageDraw.Draw(raster).rectangle((60, 20, 540, 26), fill=0)
    ImageDraw.Draw(raster).rectangle((90, 80, 510, 225), fill=0)
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_pdf_info",
        lambda self: {"Pages": "1", "Title": "Paper title"},
    )
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_layout", lambda self: ([page], "fixture_layout"),
    )
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_page_visual_raster", lambda self, page: raster,
    )

    document, _quality = parse_pdf_document(job_dir, job)

    bbox = document["figures"][0]["media"][0]["source_locator"]["pdf"]["bboxes"][0]
    assert bbox == [86.0, 76.0, 515.0, 230.0]


def test_vector_crop_discards_isolated_footer_page_number(
    monkeypatch: pytest.MonkeyPatch,
    pdf_job: tuple[Path, dict[str, str], bytes],
) -> None:
    job_dir, job, _ = pdf_job
    page = PageLayout(1, 800, 600, text_items=[
        LayoutItem("Figure 1: Landscape experiment.", [100, 70, 700, 90]),
        LayoutItem("46", [392, 520, 408, 532]),
    ])
    raster = Image.new("L", (800, 600), 255)
    draw = ImageDraw.Draw(raster)
    draw.rectangle((100, 120, 700, 430), fill=0)
    draw.rectangle((392, 520, 408, 532), fill=0)
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_pdf_info",
        lambda self: {"Pages": "1", "Title": "Paper title"},
    )
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_layout", lambda self: ([page], "fixture_layout"),
    )
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_page_visual_raster", lambda self, page: raster,
    )

    document, _quality = parse_pdf_document(job_dir, job)

    bbox = document["figures"][0]["media"][0]["source_locator"]["pdf"]["bboxes"][0]
    assert bbox == [96.0, 116.0, 705.0, 435.0]


def test_vector_crop_does_not_treat_numeric_axis_ticks_as_section_boundaries(
    monkeypatch: pytest.MonkeyPatch,
    pdf_job: tuple[Path, dict[str, str], bytes],
) -> None:
    job_dir, job, _ = pdf_job
    page = PageLayout(1, 600, 800, text_items=[
        LayoutItem("Paper title", [60, 40, 500, 65]),
        LayoutItem("0.0", [75, 110, 88, 118]),
        LayoutItem("0.5", [75, 155, 88, 163]),
        LayoutItem("1.0", [75, 200, 88, 208]),
        LayoutItem("64 128 256", [170, 210, 230, 218]),
        LayoutItem("Request rate (req/s)", [120, 235, 220, 245]),
        LayoutItem("Figure 12. Serving latency.", [60, 260, 540, 275]),
    ])
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_pdf_info",
        lambda self: {"Pages": "1", "Title": "Paper title"},
    )
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_layout", lambda self: ([page], "fixture_layout"),
    )
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_page_visual_raster", lambda self, page: None,
    )

    document, _quality = parse_pdf_document(job_dir, job)

    bbox = document["figures"][0]["media"][0]["source_locator"]["pdf"]["bboxes"][0]
    assert bbox[1] <= 110
    assert bbox[3] == 260


def test_pdf_primary_layout_extracts_images_only_in_temporary_directory(
    monkeypatch: pytest.MonkeyPatch,
    pdf_job: tuple[Path, dict[str, str], bytes],
) -> None:
    job_dir, job, _ = pdf_job
    adapter = ScholarlyPdfAdapter(job_dir, job)
    calls: list[tuple[list[str], str | None]] = []
    input_files = set((job_dir / "input").iterdir())

    def fake_run(
        command: list[str],
        timeout: int = 120,
        *,
        cwd: str | None = None,
    ) -> str:
        del timeout
        calls.append((command, cwd))
        assert cwd is not None and Path(cwd).is_dir()
        layout_pdf = Path(command[-1])
        assert layout_pdf.parent == Path(cwd)
        assert layout_pdf != adapter.path
        assert layout_pdf.read_bytes() == adapter.path.read_bytes()
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
    assert set((job_dir / "input").iterdir()) == input_files


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


def test_duplicate_figure_number_keeps_descriptive_caption_not_bare_reference(
    monkeypatch: pytest.MonkeyPatch,
    pdf_job: tuple[Path, dict[str, str], bytes],
) -> None:
    job_dir, job, _ = pdf_job
    pages = [
        PageLayout(1, 600, 800, text_items=[
            LayoutItem("Paper title", [60, 40, 500, 65]),
            LayoutItem("Figure 4", [60, 300, 130, 315]),
        ]),
        PageLayout(2, 600, 800, text_items=[
            LayoutItem(
                "Figure 4: Liquidity risk beta statistics.", [60, 400, 500, 420],
            ),
        ], image_bboxes=[[80, 120, 500, 390]]),
    ]
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_pdf_info",
        lambda self: {"Pages": "2", "Title": "Paper title"},
    )
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_layout", lambda self: (pages, "fixture_layout"),
    )

    document, _quality = parse_pdf_document(job_dir, job)

    assert [(item["label"], item["caption"]) for item in document["figures"]] == [
        ("Figure 4", "Figure 4: Liquidity risk beta statistics."),
    ]
    assert document["figures"][0]["source_locator"]["pdf"]["page"] == 2


def test_split_figure_caption_beats_longer_fig_reference_at_line_start(
    monkeypatch: pytest.MonkeyPatch,
    pdf_job: tuple[Path, dict[str, str], bytes],
) -> None:
    job_dir, job, _ = pdf_job
    pages = [
        PageLayout(1, 600, 800, text_items=[
            LayoutItem("Paper title", [60, 40, 500, 65]),
            LayoutItem("Figure 10.", [54, 158, 98, 167]),
            LayoutItem("Shared prompt example.", [100, 158, 295, 167]),
        ]),
        PageLayout(2, 600, 800, text_items=[
            LayoutItem(
                "Fig. 10. For the model, we use a multilingual benchmark and "
                "several long examples.",
                [54, 365, 296, 410],
            ),
        ]),
    ]
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_pdf_info",
        lambda self: {"Pages": "2", "Title": "Paper title"},
    )
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_layout", lambda self: (pages, "fixture_layout"),
    )

    document, _quality = parse_pdf_document(job_dir, job)

    figure = document["figures"][0]
    assert figure["label"] == "Figure 10"
    assert figure["caption"] == "Figure 10. Shared prompt example."
    assert figure["source_locator"]["pdf"]["page"] == 1


def test_explicit_colon_caption_beats_bare_label_followed_by_body_prose(
    monkeypatch: pytest.MonkeyPatch,
    pdf_job: tuple[Path, dict[str, str], bytes],
) -> None:
    job_dir, job, _ = pdf_job
    pages = [
        PageLayout(1, 600, 800, text_items=[
            LayoutItem("Figure 4", [60, 480, 120, 495]),
            LayoutItem(
                "reports the statistics for the individual strategies and markets.",
                [60, 500, 500, 515],
            ),
        ]),
        PageLayout(2, 600, 800, text_items=[
            LayoutItem(
                "Figure 4: Liquidity Risk Beta t-statistics", [60, 320, 500, 335],
            ),
        ], image_bboxes=[[80, 80, 520, 310]]),
    ]
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_pdf_info",
        lambda self: {"Pages": "2", "Title": "Paper title"},
    )
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_layout", lambda self: (pages, "fixture_layout"),
    )

    document, _quality = parse_pdf_document(job_dir, job)

    figure = document["figures"][0]
    assert figure["caption"] == "Figure 4: Liquidity Risk Beta t-statistics"
    assert figure["source_locator"]["pdf"]["page"] == 2


def test_midline_figure_reference_is_not_treated_as_caption(
    monkeypatch: pytest.MonkeyPatch,
    pdf_job: tuple[Path, dict[str, str], bytes],
) -> None:
    job_dir, job, _ = pdf_job
    pages = [
        PageLayout(1, 600, 800, text_items=[
            LayoutItem("The majority of tasks are shown in", [60, 700, 250, 715]),
            LayoutItem("Figure 7", [254, 700, 305, 715]),
            LayoutItem("and improve over the baseline.", [309, 700, 500, 715]),
        ]),
        PageLayout(2, 600, 800, text_items=[
            LayoutItem("Figure 7. BIG-bench results.", [60, 320, 500, 335]),
        ], image_bboxes=[[80, 80, 520, 310]]),
    ]
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_pdf_info",
        lambda self: {"Pages": "2", "Title": "Paper title"},
    )
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_layout", lambda self: (pages, "fixture_layout"),
    )

    document, _quality = parse_pdf_document(job_dir, job)

    assert [(item["label"], item["caption"]) for item in document["figures"]] == [
        ("Figure 7", "Figure 7. BIG-bench results."),
    ]
    assert document["figures"][0]["source_locator"]["pdf"]["page"] == 2


def test_right_column_caption_can_share_row_with_left_column_body(
    monkeypatch: pytest.MonkeyPatch,
    pdf_job: tuple[Path, dict[str, str], bytes],
) -> None:
    job_dir, job, _ = pdf_job
    page = PageLayout(1, 600, 800, text_items=[
        LayoutItem("Body prose continues in the left column.", [60, 310, 380, 325]),
        LayoutItem("Figure 1: Our reparametrization.", [390, 310, 550, 325]),
    ], image_bboxes=[[350, 100, 550, 300]])
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_pdf_info",
        lambda self: {"Pages": "1", "Title": "Paper title"},
    )
    monkeypatch.setattr(
        ScholarlyPdfAdapter, "_layout", lambda self: ([page], "fixture_layout"),
    )

    document, _quality = parse_pdf_document(job_dir, job)

    assert [(item["label"], item["caption"]) for item in document["figures"]] == [
        ("Figure 1", "Figure 1: Our reparametrization."),
    ]


def test_short_prose_continuation_marks_figure_candidate_edge() -> None:
    page = PageLayout(1, 600, 800, text_items=[
        LayoutItem(
            "We compare inference latency across all adapter configurations and batches.",
            [60, 335, 540, 344],
        ),
        LayoutItem("baseline in Figure 5.", [60, 346, 170, 355]),
    ])

    assert ScholarlyPdfAdapter._figure_edge_has_prose(
        page, 355, "above", 48, 552,
    )
