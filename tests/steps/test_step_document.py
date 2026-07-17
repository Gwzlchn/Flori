"""Document 结构、翻译、笔记和消费源的闭环测试。"""

from __future__ import annotations

import json
from pathlib import Path

from shared.document_contract import (
    DOCUMENT_SCHEMA_VERSION,
    TRANSLATION_SCHEMA_VERSION,
    validate_translation,
)
from shared.models import LLMResponse
from steps.document.step_02_parse import DocumentParseStep
from steps.document.step_03_structure import DocumentStructureStep
from steps.document.step_04_translate import DocumentTranslateStep
from steps.document.step_05_smart import DocumentSmartStep
from steps.document.step_07_concepts import DocumentConceptsStep
from steps.document.step_08_review import DocumentReviewStep
from steps.document.translation import (
    materialize_translation_segments,
    translation_batches,
    translation_units,
    validate_batch_response,
)
from tests.steps.conftest import make_job_dir, make_step_config


def _fixture(tmp_path):
    job = make_job_dir(
        tmp_path, "input", "intermediate", "output", "logs", "assets", name="jobs_document_fixture",
    )
    (job / "output" / "provenance").mkdir()
    (job / "output" / "provenance_candidates").mkdir()
    source = "<article><h1>Title</h1><p>Latency is 3 ms.</p><pre>x = 3</pre></article>"
    (job / "input" / "source.html").write_text(source, encoding="utf-8")
    fingerprint = "sha256:" + __import__("hashlib").sha256(source.encode()).hexdigest()

    def locator(exact: str):
        start = source.index(exact)
        return {
            "html": {
                "source_id": "html",
                "source_fingerprint": fingerprint,
                "dom_path": "article",
                "start": start,
                "end": start + len(exact),
                "exact": exact,
                "prefix": source[max(0, start - 8):start],
                "suffix": source[start + len(exact):start + len(exact) + 8],
            },
        }

    document = {
        "schema_version": DOCUMENT_SCHEMA_VERSION,
        "job_id": job.name,
        "content_type": "document",
        "document_kind": "research_paper",
        "classification": {"method": "source", "confidence": 1.0},
        "source_profile": "scholarly_html",
        "capabilities": ["html", "math", "bibliography", "embedded_media"],
        "primary_source_id": "html",
        "sources": [{
            "source_id": "html", "source_profile": "scholarly_html",
            "capabilities": ["html", "math", "bibliography", "embedded_media"],
            "fingerprint": fingerprint, "path": "input/source.html",
            "mime_type": "text/html", "immutable": True,
        }],
        "metadata": {
            "lang": "en",
            "titles": {"original": "Title", "zh": None},
            "authors": [{
                "author_id": "author_alice", "order": 0, "name": "Alice",
                "affiliation_ids": ["aff_lab"], "affiliations": ["Example Lab"],
                "emails": [], "notes": [], "note_refs": [],
                "equal_contribution": False,
            }],
            "affiliations": [{"affiliation_id": "aff_lab", "name": "Example Lab", "name_zh": None}],
            "author_notes": [], "abstract": "", "keywords": [], "license": "",
            "source_license": "", "rights_notices": [], "identifiers": {},
        },
        "blocks": [
            {
                "block_id": "S1",
                "parent_id": None,
                "order": 0,
                "kind": "title",
                "level": 1,
                "text": "Title",
                "locator": locator("Title"),
            },
            {
                "block_id": "S1.P1",
                "parent_id": "S1",
                "order": 1,
                "kind": "paragraph",
                "level": None,
                "text": "Latency is 3 ms.",
                "locator": locator("Latency is 3 ms."),
            },
            {
                "block_id": "S1.C1",
                "parent_id": "S1",
                "order": 2,
                "kind": "code",
                "level": None,
                "text": "x = 3",
                "locator": locator("x = 3"),
            },
        ],
        "references": [],
        "assets": [],
        "figures": [],
        "tables": [],
    }
    quality = {
        "schema_version": 1,
        "job_id": job.name,
        "status": "complete",
        "reasons": [],
        "metrics": {"source_block_count": 3, "registry_block_count": 3},
    }
    (job / "intermediate" / "document.json").write_text(
        json.dumps(document, ensure_ascii=False), encoding="utf-8",
    )
    (job / "intermediate" / "quality.json").write_text(
        json.dumps(quality, ensure_ascii=False), encoding="utf-8",
    )
    config = make_step_config(
        tmp_path, step_name="03_structure", pool="cpu", pipeline="document",
    )
    DocumentStructureStep("03_structure", job, config).execute()
    return job


def test_structure_publishes_locator_bound_source_index_without_original_md(tmp_path):
    job = _fixture(tmp_path)

    projection = job / "intermediate" / "document_index.md"
    assert projection.read_text(encoding="utf-8") == (
        "# Title\n\nLatency is 3 ms.\n\nx = 3\n"
    )
    provenance = json.loads(
        (job / "output" / "provenance" / "original.json").read_text(encoding="utf-8")
    )
    assert provenance["note_artifact"] == "intermediate/document_index.md"
    assert {item["verification_policy"] for item in provenance["segments"]} == {
        "direct_locator_v1"
    }
    assert {item["source_segment_ids"][0] for item in provenance["segments"]} == {
        "S1", "S1.P1", "S1.C1",
    }
    assert not (job / "output" / "original.md").exists()


def test_parse_step_dispatches_generic_html_and_publishes_document_contract(tmp_path):
    job = make_job_dir(
        tmp_path, "input", "intermediate", "output", "logs", "assets",
        name="jobs_document_parse",
    )
    source = (
        Path(__file__).parent / "document" / "fixtures" / "generic_article.html"
    ).read_bytes()
    (job / "input" / "source.html").write_bytes(source)
    (job / "job.json").write_text(json.dumps({
        "id": job.name,
        "content_type": "document",
        "pipeline": "document",
        "document_kind": "article",
        "source_profile": "generic_html",
        "url": "https://example.com/requested",
        "final_url": "https://example.com/final",
    }), encoding="utf-8")
    config = make_step_config(
        tmp_path, step_name="02_parse", pool="cpu", pipeline="document",
    )

    result = DocumentParseStep("02_parse", job, config).execute()

    document = json.loads(
        (job / "intermediate" / "document.json").read_text(encoding="utf-8")
    )
    assert result["source_profile"] == "generic_html"
    assert result["document_kind"] == "article"
    assert document["content_type"] == "document"
    assert document["blocks"]
    assert (job / "intermediate" / "quality.json").is_file()
    assert (job / "intermediate" / "needs_translation.json").is_file()


def test_translation_is_block_aligned_and_never_creates_original_markdown(tmp_path, monkeypatch):
    job = _fixture(tmp_path)
    config = make_step_config(
        tmp_path, step_name="04_translate", pool="ai", pipeline="document",
    )
    config["step"]["prompt_template"] = "04_translate_document"
    step = DocumentTranslateStep("04_translate", job, config)

    def call_json(_prompt, **_kwargs):
        step.ai.last_response = LLMResponse(
            content="{}", model="m", provider="anthropic", session_id="translate-session",
        )
        step.ai.last_provider = "anthropic"
        step.ai.last_model = "m"
        return {
            "segments": [
                {"id": "S1", "text": "标题"},
                {"id": "S1.P1", "text": "延迟为 3 ms。"},
            ],
        }, False

    monkeypatch.setattr(step.ai, "call_json", call_json)
    result = step.execute()

    translation = json.loads((job / "output" / "translation.json").read_text())
    assert result["status"] == "complete"
    assert translation["coverage"] == {
        "source_segments": 3,
        "translated_segments": 2,
        "passthrough_segments": 1,
    }
    assert translation["segments"][2]["text"] == "x = 3"
    assert translation["segments"][2]["transform_kind"] == "passthrough"
    rendered = (job / "output" / "translated.html").read_text(encoding="utf-8")
    assert rendered.index("<h1>标题</h1>") < rendered.index("<strong>英文标题：</strong>Title")
    assert 'data-source-segment="S1.P1"' in rendered
    assert not (job / "output" / "original.md").exists()


def test_translation_splits_oversized_block_into_contiguous_one_to_many_ranges(tmp_path):
    job = _fixture(tmp_path)
    document = json.loads(
        (job / "intermediate" / "document.json").read_text(encoding="utf-8")
    )
    source_text = ("Long sentence keeps 3 ms unchanged. " * 20).strip()
    document["blocks"][1]["text"] = source_text
    units = translation_units(document)
    batches = translation_batches(units, max_chars=80)
    fragments = [item for batch in batches for item in batch]
    oversized = [item for item in fragments if item["source_segment_id"] == "S1.P1"]

    assert len(oversized) > 1
    assert all(len(item["source_text"]) <= 80 for item in fragments)
    assert len({item["translation_request_id"] for item in oversized}) == len(oversized)
    assert [item["source_start"] for item in oversized] == [
        0, *[item["source_end"] for item in oversized[:-1]],
    ]
    assert "".join(item["source_text"] for item in oversized) == source_text

    translated: dict[str, str] = {}
    invocation_ids: dict[str, str] = {}
    for batch in batches:
        response = {"segments": [
            {"id": item["translation_request_id"], "text": item["source_text"]}
            for item in batch
        ]}
        translated.update(validate_batch_response(batch, response))
        invocation_ids.update({
            item["translation_request_id"]: "inv_fragment" for item in batch
        })
    segments = materialize_translation_segments(
        units, translated, invocation_ids, translated_fragments=fragments,
    )
    aligned = [item for item in segments if item["source_segment_ids"] == ["S1.P1"]]
    assert len(aligned) == len(oversized)
    assert {item["alignment_kind"] for item in aligned} == {"one_to_many"}
    assert "".join(item["source_ranges"][0]["exact"] for item in aligned) == source_text

    coverage = {
        "source_segments": len({
            source_id for item in segments for source_id in item["source_segment_ids"]
        }),
        "translated_segments": sum(
            item["transform_kind"] == "translated" for item in segments
        ),
        "passthrough_segments": sum(
            item["transform_kind"] == "passthrough" for item in segments
        ),
    }
    artifact = {
        "schema_version": TRANSLATION_SCHEMA_VERSION,
        "job_id": job.name,
        "source_fingerprint": document["sources"][0]["fingerprint"],
        "source_lang": "en",
        "target_lang": "zh",
        "status": "complete",
        "coverage": coverage,
        "segments": segments,
    }
    assert validate_translation(artifact, expected_job_id=job.name)["coverage"] == coverage


def test_smart_note_title_is_deterministic_and_uses_translation(tmp_path, monkeypatch):
    job = _fixture(tmp_path)
    translate_config = make_step_config(
        tmp_path, step_name="04_translate", pool="ai", pipeline="document",
    )
    translate_config["step"]["prompt_template"] = "04_translate_document"
    translate = DocumentTranslateStep("04_translate", job, translate_config)

    def call_json(_prompt, **_kwargs):
        translate.ai.last_response = LLMResponse(
            content="{}", model="m", provider="anthropic", session_id="translate-session",
        )
        translate.ai.last_provider = "anthropic"
        translate.ai.last_model = "m"
        return {"segments": [
            {"id": "S1", "text": "标题"},
            {"id": "S1.P1", "text": "延迟为 3 ms。"},
        ]}, False

    monkeypatch.setattr(translate.ai, "call_json", call_json)
    translate.execute()

    config = make_step_config(
        tmp_path, step_name="05_smart", pool="ai", pipeline="document",
    )
    config["step"]["prompt_template"] = "05_smart_document"
    step = DocumentSmartStep("05_smart", job, config)

    def call(_prompt, **_kwargs):
        step.ai.last_response = LLMResponse(
            content="", model="m", provider="anthropic", session_id="smart-session",
        )
        step.ai.last_provider = "anthropic"
        step.ai.last_model = "m"
        return "# 模型自拟标题\n\n## 核心\n\n延迟为 3 ms。[[source:S1.P1]]"

    monkeypatch.setattr(step.ai, "call", call)
    result = step.execute()
    note = (job / result["note_file"]).read_text(encoding="utf-8")
    assert note.splitlines()[0] == "# 标题 - 笔记"
    assert "模型自拟标题" not in note
    assert result["source"] == "translation"


def test_document_concepts_never_read_legacy_original_markdown(tmp_path):
    job = _fixture(tmp_path)
    (job / "output" / "original.md").write_text("LEGACY MUST NOT BE READ", encoding="utf-8")
    config = make_step_config(
        tmp_path, step_name="07_concepts", pool="ai", pipeline="document",
    )
    config["step"]["prompt_template"] = "05_concepts"
    step = DocumentConceptsStep("07_concepts", job, config)
    source = step._resolve_concept_source()
    assert source is not None
    assert source.kind == "document"
    assert "LEGACY" not in source.text


def test_document_review_uses_document_quality_and_translation_sources(tmp_path, monkeypatch):
    job = _fixture(tmp_path)
    versions = job / "output" / "versions"
    versions.mkdir(exist_ok=True)
    note = versions / "notes_smart_anthropic_claude-opus-4-8_20260717-000000.md"
    note.write_text("# 标题 - 笔记\n\n延迟为 3 ms。\n", encoding="utf-8")
    config = make_step_config(
        tmp_path, step_name="08_review", pool="ai", pipeline="document",
    )
    config["step"]["prompt_template"] = "08_review"
    step = DocumentReviewStep("08_review", job, config)
    scores = {
        "completeness": 5,
        "accuracy": 5,
        "structure": 4,
        "terminology": 4,
        "formula_integrity": 5,
        "visual_references": 4,
        "traceability": 5,
        "key_terms": [],
        "missing_concepts": [],
        "top3_improvements": ["补充边界", "强化来源", "保持术语"],
        "issues": [],
    }

    def call(_prompt, **_kwargs):
        content = json.dumps(scores, ensure_ascii=False)
        step.ai.last_response = LLMResponse(
            content=content,
            model="claude-opus-4-8",
            provider="anthropic",
            finish_reason="stop",
        )
        step.ai.last_provider = "anthropic"
        step.ai.last_model = "claude-opus-4-8"
        return content

    monkeypatch.setattr(step.ai, "call", call)
    result = step.execute()

    review = json.loads((job / "output" / "review.json").read_text(encoding="utf-8"))
    assert result["parse_failed"] is False
    assert review["score_keys"] == [
        "completeness", "accuracy", "structure", "terminology",
        "formula_integrity", "visual_references", "traceability",
    ]
    assert {item["label"] for item in review["review_input"]["sources"]} == {
        "smart", "document", "quality",
    }
