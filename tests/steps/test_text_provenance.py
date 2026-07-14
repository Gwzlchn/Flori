"""paper/article producer 的来源坐标与最终笔记绑定测试。"""

from __future__ import annotations

import hashlib
import json

import pytest

from shared.provenance import validate_provenance_manifest
from steps.article.provenance import (
    build_html_source_manifest,
    build_pdf_source_manifest,
    extract_note_markers,
    load_source_manifest,
    markdown_to_index_text,
    persist_note_provenance,
    publish_source_manifest,
)
from steps.article.step_04_smart_article import SmartArticleStep
from steps.paper.step_05_smart_paper import SmartPaperStep
from tests.steps.conftest import make_step_config


def _job(tmp_path, *, name="job"):
    job_dir = tmp_path / name
    for rel in ("input", "intermediate", "output", "assets", "logs"):
        (job_dir / rel).mkdir(parents=True, exist_ok=True)
    return job_dir


def test_html_source_manifest_binds_raw_sha_and_unique_locator(tmp_path):
    job_dir = _job(tmp_path)
    html = (
        "<html><body><article>"
        "<p>这是唯一且足够长的原始文章事实段落，用于验证精确来源定位。</p>"
        "<p>第二个唯一事实段落也足够长，用于验证多段来源不会共享坐标。</p>"
        "</article></body></html>"
    )
    source = job_dir / "input/source.html"
    source.write_text(html, encoding="utf-8")

    manifest = build_html_source_manifest(
        job_dir, pipeline="article", revision="https://example.test/a",
    )
    assert manifest is not None
    publish_source_manifest(job_dir, manifest)
    loaded = load_source_manifest(job_dir, pipeline="article")

    assert loaded is not None
    assert loaded["source_artifacts"][0]["sha256"] == hashlib.sha256(
        source.read_bytes()
    ).hexdigest()
    for segment in loaded["segments"]:
        locator = segment["locator"]
        assert locator["kind"] == "text"
        assert html.count(locator["exact"]) == 1
        assert html[segment["start"]:segment["end"]] == locator["exact"]
        assert segment["support_text"] == locator["exact"]


def test_pdf_source_manifest_uses_measured_pages_without_fake_bbox(tmp_path):
    job_dir = _job(tmp_path)
    source = job_dir / "input/source.pdf"
    source.write_bytes(b"%PDF-real-test-bytes")
    supports = ["first page evidence", None, "third page evidence"]
    (job_dir / "intermediate/pdf_page_support.json").write_text(json.dumps({
        "schema_version": 1,
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "pages": [
            {"page": index, "support_text": text}
            for index, text in enumerate(supports, 1)
        ],
    }), encoding="utf-8")

    manifest = build_pdf_source_manifest(
        job_dir,
        pipeline="paper",
        page_count=3,
        page_support_texts=supports,
    )

    assert manifest is not None
    assert manifest["source_artifacts"][0]["page_count"] == 3
    assert manifest["source_artifacts"][0]["sha256"] == hashlib.sha256(
        source.read_bytes()
    ).hexdigest()
    assert [item["locator"]["page"] for item in manifest["segments"]] == [1, 2, 3]
    assert all(item["locator"]["bbox"] is None for item in manifest["segments"])
    assert [item["support_text"] for item in manifest["segments"]] == [
        "first page evidence", None, "third page evidence",
    ]


def test_article_smart_note_removes_marker_and_persists_empty_mapping(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("DRY_RUN", "1")
    job_dir = _job(tmp_path)
    (job_dir / "input/source.html").write_text(
        "<article><p>文章里的唯一可靠事实足够长，可以生成可复验的来源坐标。</p></article>",
        encoding="utf-8",
    )
    manifest = build_html_source_manifest(job_dir, pipeline="article")
    assert manifest is not None
    publish_source_manifest(job_dir, manifest)
    (job_dir / "intermediate/sections.json").write_text(json.dumps({
        "title": "示例文章",
        "authors": [],
        "abstract": "",
        "sections": [{
            "level": 1,
            "title": "正文",
            "text": "文章里的唯一可靠事实足够长，可以生成可复验的来源坐标。",
            "children": [],
        }],
    }), encoding="utf-8")
    token = manifest["segments"][0]["segment_id"].removeprefix("seg_")
    step = SmartArticleStep(
        "04_smart_article",
        job_dir,
        make_step_config(tmp_path, step_name="04_smart_article", pool="ai"),
    )
    monkeypatch.setattr(
        step.ai,
        "call",
        lambda *_a, **_k: f"# 笔记\n\n文章可靠事实。 [[source:{token}]]",
    )

    result = step.execute()

    note_path = job_dir / result["note_file"]
    provenance = json.loads(
        (job_dir / "output/provenance/smart.json").read_text(encoding="utf-8")
    )
    assert "[[source:" not in note_path.read_text(encoding="utf-8")
    assert result["provenance_status"] == "written_empty"
    assert result["provenance_segments"] == 0
    assert provenance["note_sha256"] == hashlib.sha256(note_path.read_bytes()).hexdigest()
    assert provenance["segments"] == []
    validate_provenance_manifest(
        provenance,
        source_manifest=manifest,
        note_bytes=note_path.read_bytes(),
        normalized_body=markdown_to_index_text(note_path.read_text(encoding="utf-8")),
    )


def test_article_smart_exact_quote_persists_mapping(tmp_path, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "1")
    job_dir = _job(tmp_path)
    claim = "文章里的唯一可靠事实足够长，可以生成可复验的来源坐标。"
    (job_dir / "input/source.html").write_text(
        f"<article><p>{claim}</p></article>", encoding="utf-8",
    )
    manifest = build_html_source_manifest(job_dir, pipeline="article")
    assert manifest is not None
    publish_source_manifest(job_dir, manifest)
    (job_dir / "intermediate/sections.json").write_text(json.dumps({
        "title": "示例文章", "authors": [], "abstract": "",
        "sections": [{
            "level": 1, "title": "正文", "text": claim, "children": [],
        }],
    }), encoding="utf-8")
    token = manifest["segments"][0]["segment_id"].removeprefix("seg_")
    step = SmartArticleStep(
        "04_smart_article",
        job_dir,
        make_step_config(tmp_path, step_name="04_smart_article", pool="ai"),
    )
    monkeypatch.setattr(
        step.ai, "call", lambda *_a, **_k: f"# 笔记\n\n{claim} [[source:{token}]]",
    )

    result = step.execute()

    provenance = json.loads(
        (job_dir / "output/provenance/smart.json").read_text(encoding="utf-8")
    )
    assert result["provenance_status"] == "written"
    assert result["provenance_segments"] == 1
    assert provenance["segments"][0]["anchor"] == claim
    assert provenance["segments"][0]["verification_policy"] == "exact_quote_v1"


def test_translated_note_remains_empty_without_cross_language_attestation(tmp_path):
    job_dir = _job(tmp_path)
    claim = "A source sentence long enough to produce a deterministic HTML segment."
    (job_dir / "input/source.html").write_text(
        f"<article><p>{claim}</p></article>", encoding="utf-8",
    )
    manifest = build_html_source_manifest(job_dir, pipeline="article")
    assert manifest is not None
    publish_source_manifest(job_dir, manifest)
    (job_dir / "output/translated.md").write_text(
        "这是尚未经过跨语言证明的译文。", encoding="utf-8",
    )

    result = persist_note_provenance(
        job_dir,
        pipeline="article",
        note_type="translated",
        note_artifact="output/translated.md",
        candidates=[],
    )

    assert result == {"status": "written_empty", "segments": 0}


def test_marker_validation_rejects_unknown_duplicate_and_malformed(tmp_path):
    job_dir = _job(tmp_path)
    (job_dir / "input/source.html").write_text(
        "<p>唯一可靠事实段落足够长，用来生成严格来源坐标并检查恶意标记。</p>",
        encoding="utf-8",
    )
    manifest = build_html_source_manifest(job_dir, pipeline="article")
    assert manifest is not None
    token = manifest["segments"][0]["segment_id"].removeprefix("seg_")

    with pytest.raises(ValueError, match="unknown"):
        extract_note_markers("事实 [[source:" + "0" * 64 + "]]", manifest)
    with pytest.raises(ValueError, match="duplicate"):
        extract_note_markers(
            f"事实一 [[source:{token}]]\n事实二 [[source:{token}]]", manifest,
        )
    with pytest.raises(ValueError, match="malformed"):
        extract_note_markers("事实 [[source:broken", manifest)


def test_ambiguous_final_anchor_never_publishes_false_mapping(tmp_path):
    job_dir = _job(tmp_path)
    (job_dir / "input/source.html").write_text(
        "<p>唯一可靠事实段落足够长，用来生成严格来源坐标并检查歧义锚点。</p>",
        encoding="utf-8",
    )
    manifest = build_html_source_manifest(job_dir, pipeline="article")
    assert manifest is not None
    publish_source_manifest(job_dir, manifest)
    note_rel = "output/versions/notes_smart_test_model_20260101-000000.md"
    note_path = job_dir / note_rel
    note_path.parent.mkdir(parents=True, exist_ok=True)
    note_path.write_text("重复事实\n重复事实\n", encoding="utf-8")

    result = persist_note_provenance(
        job_dir,
        pipeline="article",
        note_type="smart",
        note_artifact=note_rel,
        candidates=[{
            "anchor": "重复事实",
            "prefix": "",
            "suffix": "",
            "section": "smart",
            "source_segment_ids": [manifest["segments"][0]["segment_id"]],
        }],
    )

    assert result["segments"] == 0
    assert result["status"] in {"no_reliable_mapping", "written_empty"}
    if (job_dir / "output/provenance/smart.json").exists():
        persisted = json.loads(
            (job_dir / "output/provenance/smart.json").read_text(encoding="utf-8")
        )
        assert persisted["segments"] == []


def test_pdf_direct_note_requires_page_support_for_exact_mapping(tmp_path, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "1")
    job_dir = _job(tmp_path)
    (job_dir / "input/source.pdf").write_bytes(b"%PDF-real-test-bytes")
    claim = "Second page contains an exact source fact."
    source_sha256 = hashlib.sha256(
        (job_dir / "input/source.pdf").read_bytes()
    ).hexdigest()
    (job_dir / "intermediate/pdf_page_support.json").write_text(json.dumps({
        "schema_version": 1,
        "source_sha256": source_sha256,
        "pages": [
            {"page": 1, "support_text": None},
            {"page": 2, "support_text": claim},
        ],
    }), encoding="utf-8")
    manifest = build_pdf_source_manifest(
        job_dir,
        pipeline="paper",
        page_count=2,
        page_support_texts=[None, claim],
    )
    assert manifest is not None
    publish_source_manifest(job_dir, manifest)
    (job_dir / "intermediate/sections.json").write_text(json.dumps({
        "title": "PDF Paper", "sections": [],
    }), encoding="utf-8")
    (job_dir / "intermediate/parsed.json").write_text(json.dumps({
        "source_kind": "pdf-only", "pages": 2,
    }), encoding="utf-8")
    token = manifest["segments"][1]["segment_id"].removeprefix("seg_")
    step = SmartPaperStep(
        "05_smart_paper",
        job_dir,
        make_step_config(
            tmp_path, step_name="05_smart_paper", pool="ai", pipeline="paper",
        ),
    )
    monkeypatch.setattr(
        step.ai,
        "call",
        lambda *_a, **_k: f"# PDF note\n\n{claim} [[source:{token}]]",
    )

    result = step.execute()

    assert result["source"] == "pdf-direct"
    assert result["provenance_status"] == "written"
    assert result["provenance_segments"] == 1
    note_path = job_dir / result["note_file"]
    assert "[[source:" not in note_path.read_text(encoding="utf-8")
    provenance = json.loads(
        (job_dir / "output/provenance/smart.json").read_text(encoding="utf-8")
    )
    assert provenance["segments"][0]["anchor"] == claim
    assert provenance["segments"][0]["verification_policy"] == "exact_quote_v1"
