"""验证 PDF 图表区域产物完整、失败降级且不丢 registry 项。"""

from __future__ import annotations

from pathlib import Path

from shared.document_contract import DOCUMENT_SCHEMA_VERSION
from steps.document.visual_assets import materialize_pdf_visuals


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
        destination.write_bytes(b"png")

    monkeypatch.setattr("steps.document.visual_assets._render_region", render)
    document, quality = materialize_pdf_visuals(job_dir, _document(job_dir.name), _quality(job_dir.name))

    figure_artifact = document["figures"][0]["media"][0]["artifact"]
    table_artifact = document["tables"][0]["representations"][0]["artifact"]
    assert figure_artifact == "assets/document/fig_1-panel_a.png"
    assert table_artifact == "assets/document/tbl_1.png"
    assert (job_dir / figure_artifact).read_bytes() == b"png"
    assert (job_dir / table_artifact).read_bytes() == b"png"
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
