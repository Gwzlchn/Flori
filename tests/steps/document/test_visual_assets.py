"""验证 PDF 图表区域产物完整、失败降级且不丢 registry 项。"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from shared.document_contract import DOCUMENT_SCHEMA_VERSION
from steps.document.adapters import parse_scholarly_html
from steps.document.visual_assets import (
    BlankPdfRegionError,
    _download_remote_image,
    _render_region,
    _verified_image,
    materialize_html_visuals,
    materialize_pdf_visuals,
)


FINGERPRINT = "sha256:" + "b" * 64


def _locator(bbox: list[float]):
    return {"pdf": {
        "source_id": "pdf", "source_fingerprint": FINGERPRINT,
        "page": 1, "bboxes": [bbox],
    }}


def _document(job_id: str):
    metadata = {
        "titles": {"original": "PDF", "zh": None}, "authors": [],
        "author_notes": [], "rights_notices": [], "source_license": "",
        "affiliations": [], "abstract": "", "keywords": [], "lang": "en",
        "license": "", "identifiers": {},
    }
    return {
        "schema_version": DOCUMENT_SCHEMA_VERSION, "job_id": job_id, "content_type": "document",
        "document_kind": "whitepaper", "classification": {"method": "user", "confidence": 1.0},
        "source_profile": "digital_pdf", "capabilities": ["pdf", "text_layer", "page_bbox"],
        "primary_source_id": "pdf",
        "sources": [{
            "source_id": "pdf", "source_profile": "digital_pdf",
            "capabilities": ["pdf", "text_layer", "page_bbox"],
            "fingerprint": FINGERPRINT, "path": "input/source.pdf",
            "mime_type": "application/pdf", "immutable": True,
        }],
        "metadata": metadata,
        "blocks": [
            {"block_id": "blk_f", "parent_id": None, "order": 0, "kind": "figure", "text": "Figure 1", "locator": _locator([10, 20, 110, 120])},
            {"block_id": "blk_t", "parent_id": None, "order": 1, "kind": "table", "text": "Table 1", "locator": _locator([20, 130, 180, 260])},
        ],
        "assets": [], "references": [],
        "figures": [{
            "figure_id": "fig_1", "block_id": "blk_f", "label": "Figure 1", "caption": "",
            "order": 0, "source_locator": _locator([10, 20, 110, 120]),
            "media": [{"media_id": "panel_a", "artifact": None, "source_locator": _locator([10, 20, 110, 120])}],
            "extraction": {"status": "complete", "reasons": []},
        }],
        "tables": [{
            "table_id": "tbl_1", "block_id": "blk_t", "label": "Table 1", "caption": "",
            "order": 1, "source_locator": _locator([20, 130, 180, 260]), "cells": [],
            "representations": [{"kind": "source_crop", "artifact": None, "source_locator": _locator([20, 130, 180, 260])}],
            "footnotes": [], "extraction": {"status": "degraded", "reasons": ["structure_unavailable"]},
        }],
    }


def _quality(job_id: str):
    return {"schema_version": 1, "job_id": job_id, "status": "degraded", "reasons": ["table_structure_unavailable"], "metrics": {}}


def test_materializes_figure_panels_and_table_crop(tmp_path: Path, monkeypatch):
    job_dir = tmp_path / "job_pdf"
    (job_dir / "input").mkdir(parents=True)
    (job_dir / "input/source.pdf").write_bytes(b"pdf")

    def render(_source, destination, **_kwargs):
        destination.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (4, 3)).save(destination)

    monkeypatch.setattr("steps.document.visual_assets._render_region", render)
    document, quality = materialize_pdf_visuals(job_dir, _document(job_dir.name), _quality(job_dir.name))

    figure_artifact = document["figures"][0]["media"][0]["artifact"]
    table_artifact = document["tables"][0]["representations"][0]["artifact"]
    assert figure_artifact.startswith("assets/document/fig_1-panel_a-")
    assert table_artifact.startswith("assets/document/tbl_1-")
    with Image.open(job_dir / figure_artifact) as image:
        assert image.size == (4, 3)
    with Image.open(job_dir / table_artifact) as image:
        assert image.size == (4, 3)
    assert quality["metrics"]["visual_assets_rendered"] == 2


def test_render_failure_degrades_without_dropping_visual(tmp_path: Path, monkeypatch):
    job_dir = tmp_path / "job_pdf"
    (job_dir / "input").mkdir(parents=True)
    (job_dir / "input/source.pdf").write_bytes(b"pdf")
    monkeypatch.setattr(
        "steps.document.visual_assets._render_region",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("failed")),
    )

    document, quality = materialize_pdf_visuals(job_dir, _document(job_dir.name), _quality(job_dir.name))

    assert len(document["figures"]) == 1 and len(document["tables"]) == 1
    assert document["figures"][0]["extraction"]["status"] == "degraded"
    assert document["tables"][0]["representations"][0]["artifact"] is None
    assert quality["metrics"]["visual_asset_failures"] == 2
    assert "pdf_visual_render_incomplete" in quality["reasons"]


def test_source_blank_pdf_region_is_reported_as_source_fidelity_issue(
    tmp_path: Path,
    monkeypatch,
):
    job_dir = tmp_path / "job_pdf"
    (job_dir / "input").mkdir(parents=True)
    (job_dir / "input/source.pdf").write_bytes(b"pdf")
    document = _document(job_dir.name)
    document["tables"] = []
    monkeypatch.setattr(
        "steps.document.visual_assets._render_region",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            BlankPdfRegionError("blank source region"),
        ),
    )

    updated, report = materialize_pdf_visuals(
        job_dir, document, _quality(job_dir.name),
    )

    assert updated["figures"][0]["extraction"] == {
        "status": "degraded", "reasons": ["pdf_visual_source_blank"],
    }
    assert "pdf_visual_source_blank" in report["reasons"]
    assert "pdf_visual_render_incomplete" not in report["reasons"]
    assert report["metrics"]["visual_asset_failures"] == 1


def test_corrupt_existing_pdf_artifact_is_rerendered(tmp_path: Path, monkeypatch):
    job_dir = tmp_path / "job_pdf"
    (job_dir / "input").mkdir(parents=True)
    (job_dir / "input/source.pdf").write_bytes(b"pdf")
    document = _document(job_dir.name)
    document["tables"] = []
    artifact = "assets/document/fig_1-panel_a.png"
    (job_dir / artifact).parent.mkdir(parents=True)
    (job_dir / artifact).write_bytes(b"<html>not an image</html>")
    document["figures"][0]["media"][0]["artifact"] = artifact
    rendered = False

    def render(_source, destination, **_kwargs):
        nonlocal rendered
        rendered = True
        Image.new("RGB", (4, 3)).save(destination)

    monkeypatch.setattr("steps.document.visual_assets._render_region", render)

    updated, _report = materialize_pdf_visuals(job_dir, document, _quality(job_dir.name))

    assert rendered is True
    updated_artifact = updated["figures"][0]["media"][0]["artifact"]
    assert updated_artifact and updated_artifact != artifact
    with Image.open(job_dir / updated_artifact) as image:
        assert image.size == (4, 3)


def test_valid_legacy_pdf_artifact_is_rerendered_to_locator_addressed_path(
    tmp_path: Path,
    monkeypatch,
):
    job_dir = tmp_path / "job_pdf"
    (job_dir / "input").mkdir(parents=True)
    (job_dir / "input/source.pdf").write_bytes(b"pdf")
    document = _document(job_dir.name)
    document["tables"] = []
    legacy = "assets/document/fig_1-panel_a.png"
    (job_dir / legacy).parent.mkdir(parents=True)
    Image.new("RGB", (2, 2)).save(job_dir / legacy)
    document["figures"][0]["media"][0]["artifact"] = legacy
    rendered = False

    def render(_source, destination, **_kwargs):
        nonlocal rendered
        rendered = True
        Image.new("RGB", (4, 3)).save(destination)

    monkeypatch.setattr("steps.document.visual_assets._render_region", render)

    updated, _report = materialize_pdf_visuals(
        job_dir, document, _quality(job_dir.name),
    )

    artifact = updated["figures"][0]["media"][0]["artifact"]
    assert rendered is True
    assert artifact and artifact != legacy
    assert artifact.startswith("assets/document/fig_1-panel_a-")


def test_corrupt_existing_pdf_artifact_degrades_when_rerender_fails(
    tmp_path: Path,
    monkeypatch,
):
    job_dir = tmp_path / "job_pdf"
    (job_dir / "input").mkdir(parents=True)
    (job_dir / "input/source.pdf").write_bytes(b"pdf")
    document = _document(job_dir.name)
    document["tables"] = []
    artifact = "assets/document/fig_1-panel_a.png"
    (job_dir / artifact).parent.mkdir(parents=True)
    (job_dir / artifact).write_bytes(b"broken")
    document["figures"][0]["media"][0]["artifact"] = artifact
    monkeypatch.setattr(
        "steps.document.visual_assets._render_region",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("bad crop")),
    )

    updated, report = materialize_pdf_visuals(job_dir, document, _quality(job_dir.name))

    media = updated["figures"][0]["media"][0]
    assert media["artifact"] is None
    assert updated["figures"][0]["extraction"]["status"] == "degraded"
    assert report["metrics"]["visual_asset_failures"] == 1


def test_missing_or_invalid_pdf_locator_cannot_remain_complete(tmp_path: Path):
    job_dir = tmp_path / "job_pdf"
    (job_dir / "input").mkdir(parents=True)
    (job_dir / "input/source.pdf").write_bytes(b"pdf")
    document = _document(job_dir.name)
    document["tables"] = []
    document["figures"][0]["media"][0]["source_locator"] = _locator([10, 20, 10, 120])
    document["figures"][0]["source_locator"] = _locator([10, 20, 10, 120])

    updated, report = materialize_pdf_visuals(job_dir, document, _quality(job_dir.name))

    figure = updated["figures"][0]
    assert figure["media"][0]["artifact"] is None
    assert figure["extraction"] == {
        "status": "degraded", "reasons": ["pdf_visual_locator_unavailable"],
    }
    assert report["metrics"]["visual_asset_failures"] == 1


def test_pdf_media_locator_unions_all_bboxes_before_render(
    tmp_path: Path,
    monkeypatch,
):
    job_dir = tmp_path / "job_pdf"
    (job_dir / "input").mkdir(parents=True)
    (job_dir / "input/source.pdf").write_bytes(b"pdf")
    document = _document(job_dir.name)
    document["tables"] = []
    media = document["figures"][0]["media"][0]
    media["source_locator"] = {"pdf": {
        "source_id": "pdf", "source_fingerprint": FINGERPRINT,
        "page": 1,
        "bboxes": [
            [10, 20, 110, 120], [120, 20, 220, 120],
            [10, 130, 110, 230], [120, 130, 220, 230],
        ],
    }}
    rendered_bbox = None

    def render(_source, destination, **kwargs):
        nonlocal rendered_bbox
        rendered_bbox = kwargs["bbox"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (4, 3)).save(destination)

    monkeypatch.setattr("steps.document.visual_assets._render_region", render)

    updated, report = materialize_pdf_visuals(
        job_dir, document, _quality(job_dir.name),
    )

    assert rendered_bbox == [10.0, 20.0, 220.0, 230.0]
    assert updated["figures"][0]["media"][0]["artifact"]
    assert report["metrics"]["visual_asset_failures"] == 0


def test_empty_figure_media_cannot_remain_complete(tmp_path: Path):
    job_dir = tmp_path / "job_pdf"
    (job_dir / "input").mkdir(parents=True)
    (job_dir / "input/source.pdf").write_bytes(b"pdf")
    document = _document(job_dir.name)
    document["tables"] = []
    document["figures"][0]["media"] = []

    updated, report = materialize_pdf_visuals(
        job_dir, document, _quality(job_dir.name),
    )

    assert updated["figures"][0]["extraction"] == {
        "status": "degraded", "reasons": ["pdf_visual_media_missing"],
    }
    assert report["metrics"]["visual_asset_failures"] == 1
    assert "pdf_visual_render_incomplete" in report["reasons"]


def test_missing_html_panel_falls_back_to_figure_pdf_locator(
    tmp_path: Path,
    monkeypatch,
):
    job_dir = tmp_path / "job_pdf"
    (job_dir / "input").mkdir(parents=True)
    (job_dir / "input/source.pdf").write_bytes(b"pdf")
    document = _document(job_dir.name)
    document["tables"] = []
    figure = document["figures"][0]
    figure["caption"] = "Left: memory layout. Right: throughput."
    figure["media"][0]["source_locator"] = {
        "html": {"source_id": "html", "source_fingerprint": FINGERPRINT,
                 "dom_path": "/figure/img", "exact": None},
    }
    (job_dir / "assets").mkdir()
    Image.new("RGB", (3, 4)).save(job_dir / "assets/right.png")
    figure["media"].append({
        "media_id": "panel_b", "artifact": "assets/right.png",
        "source_locator": {
            "html": {"source_id": "html", "source_fingerprint": FINGERPRINT,
                     "dom_path": "/figure/img[2]", "exact": None},
        },
    })
    figure["extraction"] = {
        "status": "degraded", "reasons": ["html_visual_asset_incomplete"],
    }

    rendered_bbox = None

    def render(_source, destination, **kwargs):
        nonlocal rendered_bbox
        rendered_bbox = kwargs["bbox"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (4, 3)).save(destination)

    monkeypatch.setattr("steps.document.visual_assets._render_region", render)

    updated, report = materialize_pdf_visuals(
        job_dir, document,
        {**_quality(job_dir.name), "reasons": ["html_visual_asset_incomplete"]},
    )

    assert updated["figures"][0]["media"][0]["artifact"]
    assert updated["figures"][0]["media"][1]["artifact"] == "assets/right.png"
    assert rendered_bbox == [10.0, 20.0, 57.0, 120.0]
    assert updated["figures"][0]["extraction"] == {
        "status": "complete", "reasons": [],
    }
    assert "html_visual_asset_incomplete" not in report["reasons"]
    assert report["metrics"]["visual_asset_failures"] == 0


def test_labeled_html_panels_split_pdf_fallback_horizontally(
    tmp_path: Path,
    monkeypatch,
):
    job_dir = tmp_path / "job_pdf"
    (job_dir / "input").mkdir(parents=True)
    (job_dir / "input/source.pdf").write_bytes(b"pdf")
    document = _document(job_dir.name)
    document["tables"] = []
    figure = document["figures"][0]
    figure["source_locator"] = _locator([10, 20, 210, 120])
    figure["media"] = [
        {
            "media_id": "panel_a", "artifact": None, "role": "(a) ShareGPT",
            "width": 460, "height": 346,
            "source_locator": {"html": {"source_id": "html", "dom_path": "/a"}},
        },
        {
            "media_id": "panel_b", "artifact": None, "role": "(b) Alpaca",
            "width": 460, "height": 346,
            "source_locator": {"html": {"source_id": "html", "dom_path": "/b"}},
        },
    ]
    rendered: list[list[float]] = []

    def render(_source, destination, **kwargs):
        rendered.append(kwargs["bbox"])
        destination.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (4, 3)).save(destination)

    monkeypatch.setattr("steps.document.visual_assets._render_region", render)

    updated, _report = materialize_pdf_visuals(
        job_dir, document, _quality(job_dir.name),
    )

    assert rendered == [[10.0, 20.0, 110.0, 120.0], [110.0, 20.0, 210.0, 120.0]]
    assert all(item["artifact"] for item in updated["figures"][0]["media"])


def test_wide_html_rows_split_pdf_fallback_vertically(
    tmp_path: Path,
    monkeypatch,
):
    job_dir = tmp_path / "job_pdf"
    (job_dir / "input").mkdir(parents=True)
    (job_dir / "input/source.pdf").write_bytes(b"pdf")
    document = _document(job_dir.name)
    document["tables"] = []
    figure = document["figures"][0]
    figure["source_locator"] = _locator([10, 20, 210, 120])
    figure["media"] = [
        {
            "media_id": "row_a", "artifact": None, "role": "Refer to caption",
            "width": 390, "height": 75,
            "source_locator": {"html": {"source_id": "html", "dom_path": "/a"}},
        },
        {
            "media_id": "row_b", "artifact": None, "role": "Refer to caption",
            "width": 390, "height": 72,
            "source_locator": {"html": {"source_id": "html", "dom_path": "/b"}},
        },
    ]
    rendered: list[list[float]] = []

    def render(_source, destination, **kwargs):
        rendered.append(kwargs["bbox"])
        destination.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (4, 3)).save(destination)

    monkeypatch.setattr("steps.document.visual_assets._render_region", render)

    updated, _report = materialize_pdf_visuals(
        job_dir, document, _quality(job_dir.name),
    )

    assert rendered == [[10.0, 20.0, 210.0, 70.0], [10.0, 70.0, 210.0, 120.0]]
    assert all(item["artifact"] for item in updated["figures"][0]["media"])


def test_semantic_table_does_not_require_pdf_crop(tmp_path: Path):
    job_dir = tmp_path / "job_pdf"
    (job_dir / "input").mkdir(parents=True)
    (job_dir / "input/source.pdf").write_bytes(b"pdf")
    document = _document(job_dir.name)
    document["figures"] = []
    table = document["tables"][0]
    table["source_locator"] = _locator([20, 130, 180, 260])
    table["representations"] = []
    table["cells"] = [{
        "cell_id": "cell_1", "block_id": "blk_t", "row": 0, "col": 0,
        "rowspan": 1, "colspan": 1, "role": "data", "text": "98.4",
        "source_locator": table["source_locator"],
    }]
    table["extraction"] = {"status": "complete", "reasons": []}

    updated, report = materialize_pdf_visuals(
        job_dir, document, _quality(job_dir.name),
    )

    assert updated["tables"][0]["representations"] == []
    assert updated["tables"][0]["extraction"] == {
        "status": "complete", "reasons": [],
    }
    assert report["metrics"]["visual_asset_failures"] == 0


def test_pdftocairo_singlefile_output_keeps_temporary_suffix(tmp_path: Path, monkeypatch):
    source = tmp_path / "source.pdf"
    destination = tmp_path / "assets" / "figure.png"
    source.write_bytes(b"pdf")

    def fake_run(command, **_kwargs):
        output_root = Path(command[-1])
        image = Image.new("RGB", (2, 2), "white")
        image.putpixel((0, 0), (0, 0, 0))
        image.save(Path(f"{output_root}.png"))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr("steps.document.visual_assets.subprocess.run", fake_run)

    _render_region(source, destination, page=1, bbox=[10, 20, 110, 120])

    with Image.open(destination) as image:
        assert image.size == (2, 2)
    assert not list(destination.parent.glob(".*.tmp*"))


def test_pdftocairo_rejects_blank_crop(tmp_path: Path, monkeypatch):
    source = tmp_path / "source.pdf"
    destination = tmp_path / "assets" / "figure.png"
    source.write_bytes(b"pdf")

    def fake_run(command, **_kwargs):
        output_root = Path(command[-1])
        Image.new("RGB", (2, 2), "white").save(Path(f"{output_root}.png"))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr("steps.document.visual_assets.subprocess.run", fake_run)

    with pytest.raises(BlankPdfRegionError, match="blank image"):
        _render_region(source, destination, page=1, bbox=[10, 20, 110, 120])

    assert not destination.exists()
    assert not list(destination.parent.glob(".*.tmp*"))


@pytest.mark.parametrize(
    "bbox",
    ([0, 0, 20_000, 10], [0, 0, 5_000, 5_000]),
)
def test_pdftocairo_rejects_oversized_crop_before_subprocess(
    tmp_path: Path,
    monkeypatch,
    bbox: list[float],
):
    source = tmp_path / "source.pdf"
    destination = tmp_path / "assets" / "figure.png"
    source.write_bytes(b"pdf")
    called = False

    def fake_run(*_args, **_kwargs):
        nonlocal called
        called = True
        return subprocess.CompletedProcess([], 0)

    monkeypatch.setattr("steps.document.visual_assets.subprocess.run", fake_run)

    with pytest.raises(ValueError, match="pixel limit"):
        _render_region(source, destination, page=1, bbox=bbox)

    assert called is False
    assert not destination.parent.exists()


def test_pdftocairo_rejects_nonfinite_crop_before_subprocess(tmp_path: Path, monkeypatch):
    source = tmp_path / "source.pdf"
    destination = tmp_path / "assets" / "figure.png"
    source.write_bytes(b"pdf")
    called = False

    def fake_run(*_args, **_kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr("steps.document.visual_assets.subprocess.run", fake_run)

    with pytest.raises(ValueError, match="invalid coordinates"):
        _render_region(source, destination, page=1, bbox=[0, 0, float("inf"), 10])

    assert called is False
    assert not destination.parent.exists()


def test_real_pdftocairo_output_is_decodable(tmp_path: Path):
    source = Path("tests/fixtures/sample.pdf")
    destination = tmp_path / "sample-region.png"

    _render_region(source, destination, page=1, bbox=[50, 50, 350, 80])

    with Image.open(destination) as image:
        assert image.width > 0 and image.height > 0


def test_remote_html_figure_is_snapshotted_to_local_artifact(tmp_path: Path, monkeypatch):
    job_dir = tmp_path / "job_html_remote"
    (job_dir / "input").mkdir(parents=True)
    raw = b"""<html><body><article><h1>Paper</h1><p>Body.</p>
      <figure><img src="https://cdn.example.org/panel.png" alt="panel">
      <figcaption>Figure 1: Remote panel.</figcaption></figure>
      </article></body></html>"""
    (job_dir / "input/source.html").write_bytes(raw)
    fingerprint = "sha256:" + hashlib.sha256(raw).hexdigest()
    document, quality = parse_scholarly_html(job_dir, {
        "job_id": job_dir.name, "document_kind": "research_paper",
        "source_fingerprint": fingerprint,
    })

    def download(_url, destination_root):
        destination = destination_root.with_suffix(".png")
        destination.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (3, 4)).save(destination)
        return destination

    monkeypatch.setattr("steps.document.visual_assets._download_remote_image", download)

    updated, report = materialize_html_visuals(job_dir, document, quality)

    artifact = updated["figures"][0]["media"][0]["artifact"]
    assert artifact and (job_dir / artifact).is_file()
    assert updated["figures"][0]["extraction"]["status"] == "complete"
    assert report["metrics"]["html_visual_assets_localized"] == 1
    assert report["status"] == "complete"
    assert "html_asset_remote" not in report["reasons"]


def test_missing_html_asset_reuses_matching_extracted_pdf_raster(tmp_path: Path):
    job_dir = tmp_path / "job_html_pdf_raster"
    (job_dir / "input").mkdir(parents=True)
    raw = b"""<html><body><article><h1>Paper</h1><p>Body.</p>
      <figure><img src="assets/missing.png" width="100" height="50" alt="panel">
      <figcaption>Figure 1: Extracted panel.</figcaption></figure>
      </article></body></html>"""
    (job_dir / "input/source.html").write_bytes(raw)
    Image.new("RGB", (400, 200), "blue").save(job_dir / "input/source-3_1.png")
    document, quality = parse_scholarly_html(job_dir, {
        "job_id": job_dir.name, "document_kind": "research_paper",
        "source_fingerprint": "sha256:" + hashlib.sha256(raw).hexdigest(),
    })

    updated, report = materialize_html_visuals(job_dir, document, quality)

    media = updated["figures"][0]["media"][0]
    assert media["artifact"] == "input/source-3_1.png"
    assert (media["width"], media["height"]) == (400, 200)
    assert updated["figures"][0]["extraction"] == {
        "status": "complete", "reasons": [],
    }
    assert report["metrics"]["html_visual_assets_localized"] == 1
    assert "html_visual_asset_incomplete" not in report["reasons"]


def test_corrupt_local_html_figure_degrades_instead_of_claiming_complete(tmp_path: Path):
    job_dir = tmp_path / "job_html_corrupt_local"
    (job_dir / "input").mkdir(parents=True)
    (job_dir / "assets").mkdir()
    (job_dir / "assets/panel.png").write_bytes(b"<html>not an image</html>")
    raw = b"""<html><body><article><h1>Paper</h1><p>Body.</p>
      <figure><img src="assets/panel.png" alt="panel">
      <figcaption>Figure 1: Corrupt local panel.</figcaption></figure>
      </article></body></html>"""
    (job_dir / "input/source.html").write_bytes(raw)
    document, quality = parse_scholarly_html(job_dir, {
        "job_id": job_dir.name, "document_kind": "research_paper",
        "source_fingerprint": "sha256:" + hashlib.sha256(raw).hexdigest(),
    })

    updated, report = materialize_html_visuals(job_dir, document, quality)

    figure = updated["figures"][0]
    assert figure["media"][0]["artifact"] is None
    assert figure["extraction"]["status"] == "degraded"
    assert "html_visual_asset_incomplete" in figure["extraction"]["reasons"]
    assert report["status"] == "degraded"


def test_remote_html_snapshot_failure_degrades_without_dropping_media(
    tmp_path: Path,
    monkeypatch,
):
    job_dir = tmp_path / "job_html_remote_failed"
    (job_dir / "input").mkdir(parents=True)
    raw = b"""<html><body><article><h1>Paper</h1><p>Body.</p>
      <figure><img src="https://cdn.example.org/panel.png" alt="panel">
      <figcaption>Figure 1: Remote panel.</figcaption></figure>
      </article></body></html>"""
    (job_dir / "input/source.html").write_bytes(raw)
    document, quality = parse_scholarly_html(job_dir, {
        "job_id": job_dir.name, "document_kind": "research_paper",
        "source_fingerprint": "sha256:" + hashlib.sha256(raw).hexdigest(),
    })
    monkeypatch.setattr(
        "steps.document.visual_assets._download_remote_image",
        lambda *_args: (_ for _ in ()).throw(ValueError("pixel limit")),
    )

    updated, report = materialize_html_visuals(job_dir, document, quality)

    figure = updated["figures"][0]
    assert len(figure["media"]) == 1 and figure["media"][0]["artifact"] is None
    assert figure["extraction"]["status"] == "degraded"
    assert "html_visual_asset_incomplete" in figure["extraction"]["reasons"]
    assert report["status"] == "degraded"
    assert report["metrics"]["html_visual_asset_failures"] == 1


def test_remote_html_snapshot_rejects_private_network(tmp_path: Path):
    with pytest.raises(ValueError, match="not public"):
        _download_remote_image("http://127.0.0.1/panel.png", tmp_path / "panel")


def test_remote_snapshot_pins_public_ip_and_bypasses_proxy(tmp_path: Path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "steps.document.visual_assets.socket.getaddrinfo",
        lambda *_args, **_kwargs: [
            (2, 1, 6, "", ("93.184.216.34", 443)),
        ],
    )

    def fake_run(command, **_kwargs):
        calls.append(command)
        destination = Path(command[command.index("-o") + 1])
        Image.new("RGB", (2, 2)).save(destination, format="PNG")
        return subprocess.CompletedProcess(command, 0, stdout="200")

    monkeypatch.setattr("steps.document.visual_assets.subprocess.run", fake_run)

    destination = _download_remote_image(
        "https://cdn.example.org/panel.png", tmp_path / "panel",
    )

    assert destination.is_file()
    command = calls[0]
    assert command[command.index("--resolve") + 1] == (
        "cdn.example.org:443:93.184.216.34"
    )
    assert command[command.index("--noproxy") + 1] == "*"
    assert command[command.index("--max-redirs") + 1] == "0"
    assert "-L" not in command and "--location" not in command


def test_remote_snapshot_rejects_redirect_response(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "steps.document.visual_assets.socket.getaddrinfo",
        lambda *_args, **_kwargs: [(2, 1, 6, "", ("93.184.216.34", 443))],
    )

    def fake_run(command, **_kwargs):
        destination = Path(command[command.index("-o") + 1])
        Image.new("RGB", (2, 2)).save(destination, format="PNG")
        return subprocess.CompletedProcess(command, 0, stdout="302")

    monkeypatch.setattr("steps.document.visual_assets.subprocess.run", fake_run)

    with pytest.raises(ValueError, match="HTTP status"):
        _download_remote_image("https://cdn.example.org/moved.png", tmp_path / "panel")


def test_remote_snapshot_rejects_pillow_decompression_bomb(tmp_path: Path, monkeypatch):
    candidate = tmp_path / "bomb.png"
    candidate.write_bytes(b"compressed")
    monkeypatch.setattr(
        "steps.document.visual_assets.Image.open",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            Image.DecompressionBombError("too many pixels"),
        ),
    )

    with pytest.raises(ValueError, match="pixel limit"):
        _verified_image(candidate)


@pytest.mark.parametrize("size", [(20_001, 10), (10_001, 10_001)])
def test_remote_snapshot_enforces_dimension_and_total_pixel_limits(
    tmp_path: Path,
    monkeypatch,
    size: tuple[int, int],
):
    candidate = tmp_path / "oversized.png"
    candidate.write_bytes(b"image")

    class FakeImage:
        format = "PNG"
        width, height = size

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def verify(self):
            return None

    monkeypatch.setattr("steps.document.visual_assets.Image.open", lambda *_args: FakeImage())

    with pytest.raises(ValueError, match="pixel limit"):
        _verified_image(candidate)
