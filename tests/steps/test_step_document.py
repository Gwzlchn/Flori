"""Document 结构、翻译、笔记和消费源的闭环测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from shared.document_contract import (
    DOCUMENT_SCHEMA_VERSION,
    TRANSLATION_SCHEMA_VERSION,
    validate_translation,
)
from shared.errors import InputInvalidError, ProcessingError
from shared.models import LLMResponse
from shared.review_contract import (
    MAX_DOCUMENT_REVIEW_PACK_BYTES,
    MAX_DOCUMENT_REVIEW_PROMPT_BYTES,
)
from steps.document.step_02_parse import DocumentParseStep
from steps.document.step_03_structure import DocumentStructureStep
from steps.document.step_04_translate import DocumentTranslateStep
from steps.document.step_05_smart import (
    MAX_DOCUMENT_SMART_PROMPT_BYTES,
    DocumentSmartStep,
)
from steps.document.step_07_concepts import DocumentConceptsStep
from steps.document.step_08_review import DocumentReviewStep
from steps.document.step_09_publish import DocumentPublishStep
from steps.document.translation import (
    materialize_translation_segments,
    protected_tokens,
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


def test_translation_protected_tokens_do_not_absorb_ascii_word_prefixes():
    source = (
        "ZeRO-3 by 70% for 175 and 530 billion parameters takes 3 months "
        "on 100 servers; latency is 3 ms, memory is 80 GB, and speed is 2x."
    )

    tokens = protected_tokens(source)

    assert tokens == ["-3", "70%", "175", "530", "3", "100", "3 ms", "80 GB", "2x"]
    assert all(not token.endswith((" ", "\t", "\n")) for token in tokens)
    assert validate_batch_response(
        [{"translation_request_id": "segment", "protected_tokens": tokens}],
        {"segments": [{
            "id": "segment",
            "text": (
                "ZeRO-3 提升 70%，覆盖 175 与 530 十亿参数，需要 3 个月、"
                "100 台服务器；延迟 3 ms，内存 80 GB，速度 2x。"
            ),
        }]},
    ) == {"segment": (
        "ZeRO-3 提升 70%，覆盖 175 与 530 十亿参数，需要 3 个月、"
        "100 台服务器；延迟 3 ms，内存 80 GB，速度 2x。"
    )}


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("3 months on 100 servers", ["3", "100"]),
        ("3 m2, 100 s_worker, and 5 msfoo", ["3", "2", "100", "5"]),
        ("3 m, 100 s, 3 ms, 80 GB, and 2x", ["3 m", "100 s", "3 ms", "80 GB", "2x"]),
        ("a 2x4 grid", ["2x", "4"]),
    ],
)
def test_translation_protected_tokens_enforce_numeric_and_unit_boundaries(source, expected):
    assert protected_tokens(source) == expected


@pytest.mark.parametrize(
    ("tokens", "translated"),
    [
        (["3", "100"], "需要 30 个月和 1000 台服务器。"),
        (["3 m"], "长度为 3 models。"),
        (["3 m", "100 s", "3 ms", "80 GB", "2x"], "长度 3 米，耗时 100 秒，延迟 3 毫秒，内存 80 GB，速度 2x。"),
    ],
)
def test_translation_rejects_numeric_or_unit_prefix_substitutions(tokens, translated):
    with pytest.raises(ValueError, match="translation changed protected token"):
        validate_batch_response(
            [{"translation_request_id": "segment", "protected_tokens": tokens}],
            {"segments": [{"id": "segment", "text": translated}]},
        )


def test_translation_batch_split_does_not_cut_real_unit():
    source = "abcdefgh 80 GB tail"
    batches = translation_batches([{
        "source_segment_id": "S1",
        "translated_segment_id": "T1",
        "parent_id": None,
        "order": 0,
        "kind": "paragraph",
        "source_text": source,
        "protected_tokens": protected_tokens(source),
        "transform_kind": "translated",
    }], max_chars=10)

    fragments = [item["source_text"] for batch in batches for item in batch]
    assert "".join(fragments) == source
    assert any("80 GB" in fragment for fragment in fragments)


def test_translation_numeric_validation_allows_punctuation_but_not_number_extension():
    batch = [{"translation_request_id": "segment", "protected_tokens": ["3"]}]

    assert validate_batch_response(
        batch,
        {"segments": [{"id": "segment", "text": "包含 A,3 项。"}]},
    ) == {"segment": "包含 A,3 项。"}
    for text in ("值为 13。", "值为 -3。", "值为 .3。", "值为 1,3。", "值为 3.0。"):
        with pytest.raises(ValueError, match="translation changed protected token"):
            validate_batch_response(
                batch,
                {"segments": [{"id": "segment", "text": text}]},
            )


@pytest.mark.parametrize("text", ["range 1-3", "latency 3-5 ms", "date 2026-08-10"])
def test_translation_numeric_validation_preserves_signed_ranges_and_dates(text):
    tokens = protected_tokens(text)

    assert validate_batch_response(
        [{"translation_request_id": "segment", "protected_tokens": tokens}],
        {"segments": [{"id": "segment", "text": text}]},
    ) == {"segment": text}


@pytest.mark.parametrize(
    ("source", "translated"),
    [
        ("range 1-3", "range 1-30"),
        ("latency 3-5 ms", "latency 3-50 ms"),
        ("date 2026-08-10", "date 2026-080-10"),
    ],
)
def test_translation_numeric_validation_rejects_signed_range_extension(source, translated):
    with pytest.raises(ValueError, match="translation changed protected token"):
        validate_batch_response(
            [{
                "translation_request_id": "segment",
                "protected_tokens": protected_tokens(source),
            }],
            {"segments": [{"id": "segment", "text": translated}]},
        )


def test_document_translation_retry_includes_exact_validation_feedback(tmp_path, monkeypatch):
    job = _fixture(tmp_path)
    config = make_step_config(
        tmp_path, step_name="04_translate", pool="ai", pipeline="document",
    )
    config["step"]["prompt_template"] = "04_translate_document"
    step = DocumentTranslateStep("04_translate", job, config)
    batch = [{
        "translation_request_id": "segment",
        "kind": "paragraph",
        "source_text": "a model with 175 billion parameters",
        "protected_tokens": ["175"],
    }]
    prompts: list[str] = []

    def call_json(prompt, **_kwargs):
        prompts.append(prompt)
        text = (
            "一个具有 1750 亿参数的模型"
            if len(prompts) == 1
            else "一个具有 175 billion 参数的模型"
        )
        return {"segments": [{"id": "segment", "text": text}]}, False

    monkeypatch.setattr(step.ai, "call_json", call_json)

    assert step._translate_batch(batch) == {
        "segment": "一个具有 175 billion 参数的模型",
    }
    assert len(prompts) == 2
    assert "validation_error" not in prompts[0]
    assert "translation changed protected token for segment: 175" in prompts[1]
    assert "不得通过单位、数量级或数制换算改变数字" in prompts[1]


def test_smart_note_uses_original_document_even_when_translation_exists(
    tmp_path, monkeypatch,
):
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

    prompts = []

    def call(prompt, **_kwargs):
        prompts.append(prompt)
        step.ai.last_response = LLMResponse(
            content="", model="m", provider="anthropic", session_id="smart-session",
        )
        step.ai.last_provider = "anthropic"
        step.ai.last_model = "m"
        return "# 模型自拟标题\n\n## 核心\n\n延迟为 3 ms。[[source:S1.P1]]"

    monkeypatch.setattr(step.ai, "call", call)
    result = step.execute()
    note = (job / result["note_file"]).read_text(encoding="utf-8")
    assert note.splitlines()[0] == "# Title - 笔记"
    assert "模型自拟标题" not in note
    assert result["source"] == "document"
    assert "Latency is 3 ms." in prompts[0]
    assert "延迟为 3 ms。" not in prompts[0]
    assert "translation" not in step.step_input_hashes()
    assert (job / "output/provenance_exact/smart.json").is_file()
    assert not (job / "output/provenance/smart.json").exists()


def test_smart_note_binds_and_discloses_degraded_source_quality(tmp_path, monkeypatch):
    job = _fixture(tmp_path)
    config = make_step_config(
        tmp_path, step_name="05_smart", pool="ai", pipeline="document",
    )
    config["step"]["prompt_template"] = "05_smart_document"
    step = DocumentSmartStep("05_smart", job, config)
    complete_hash = step.step_input_hashes()["quality"]
    quality = {
        "schema_version": 1,
        "job_id": job.name,
        "status": "degraded",
        "reasons": ["pdf_crosswalk_partial"],
        "metrics": {
            "pdf_source_quality": "degraded",
            "pdf_crosswalk_blocks": 54,
            "pdf_crosswalk_visuals": 19,
            "pdf_crosswalk_ambiguous": 287,
            "pdf_layout_detector_failures": 1,
        },
    }
    (job / "intermediate" / "quality.json").write_text(
        json.dumps(quality, ensure_ascii=False), encoding="utf-8",
    )
    prompts: list[str] = []

    def call(prompt, **_kwargs):
        prompts.append(prompt)
        step.ai.last_response = LLMResponse(
            content="", model="m", provider="anthropic", session_id="smart-session",
        )
        step.ai.last_provider = "anthropic"
        step.ai.last_model = "m"
        return "## 核心\n\n延迟为 3 ms。[[source:S1.P1]]"

    monkeypatch.setattr(step.ai, "call", call)

    result = step.execute()
    note = (job / result["note_file"]).read_text(encoding="utf-8")
    assert step.step_input_hashes()["quality"] != complete_hash
    assert '"status":"degraded"' in prompts[0]
    assert '"pdf_crosswalk_ambiguous":287' in prompts[0]
    assert '"pdf_crosswalk_blocks":54' in prompts[0]
    assert '"pdf_crosswalk_visuals":19' in prompts[0]
    assert "来源内部未决矛盾" in prompts[0]
    assert "> 来源质量提示：结构化解析状态为 degraded" in note
    assert "pdf_crosswalk_partial" in note
    assert "pdf_crosswalk_ambiguous=287" in note
    assert "pdf_crosswalk_blocks=54" in note
    assert "pdf_crosswalk_visuals=19" in note
    assert result["quality_status"] == "degraded"
    assert result["quality_disclosed"] is True


def test_smart_note_rejects_rejected_source_quality(tmp_path, monkeypatch):
    job = _fixture(tmp_path)
    quality = {
        "schema_version": 1,
        "job_id": job.name,
        "status": "rejected",
        "reasons": ["pdf_layout_unavailable"],
        "metrics": {},
    }
    (job / "intermediate" / "quality.json").write_text(
        json.dumps(quality), encoding="utf-8",
    )
    config = make_step_config(
        tmp_path, step_name="05_smart", pool="ai", pipeline="document",
    )
    config["step"]["prompt_template"] = "05_smart_document"
    step = DocumentSmartStep("05_smart", job, config)
    calls = 0

    def call(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("rejected quality must not call AI")

    monkeypatch.setattr(step.ai, "call", call)

    with pytest.raises(InputInvalidError, match="rejects rejected source quality"):
        step.execute()
    assert calls == 0
    assert not (job / "output" / "provenance_exact" / "smart.json").exists()
    assert not (job / "output" / "provenance_candidates" / "smart.json").exists()
    assert not list((job / "output" / "versions").glob("notes_smart_*"))


@pytest.mark.parametrize(("blocks", "visuals"), [(54, 19), (54, 0), (0, 19), (0, 0)])
def test_smart_quality_crosswalk_dimensions_stay_independent(blocks, visuals):
    quality = {
        "status": "degraded",
        "reasons": ["pdf_crosswalk_partial"],
        "metrics": {
            "pdf_crosswalk_blocks": blocks,
            "pdf_crosswalk_visuals": visuals,
        },
    }

    prompt_quality = json.loads(DocumentSmartStep._quality_prompt_block(quality))
    notice = DocumentSmartStep._quality_notice(quality)
    assert prompt_quality["metrics"] == {
        "pdf_crosswalk_blocks": blocks,
        "pdf_crosswalk_visuals": visuals,
    }
    assert f"pdf_crosswalk_blocks={blocks}" in notice
    assert f"pdf_crosswalk_visuals={visuals}" in notice


def test_smart_note_rejects_oversized_quality_before_json_parse(tmp_path, monkeypatch):
    job = _fixture(tmp_path)
    (job / "intermediate" / "quality.json").write_bytes(b" " * (64 * 1024 + 1))
    config = make_step_config(
        tmp_path, step_name="05_smart", pool="ai", pipeline="document",
    )
    config["step"]["prompt_template"] = "05_smart_document"
    step = DocumentSmartStep("05_smart", job, config)
    monkeypatch.setattr(
        step.ai, "call",
        lambda *_args, **_kwargs: pytest.fail("oversized quality must not call AI"),
    )

    with pytest.raises(InputInvalidError, match="quality exceeds byte limit"):
        step.execute()


def test_smart_note_rejects_oversized_prompt_before_ai(tmp_path, monkeypatch):
    job = _fixture(tmp_path)
    config = make_step_config(
        tmp_path, step_name="05_smart", pool="ai", pipeline="document",
    )
    config["step"]["prompt_template"] = "05_smart_document"
    step = DocumentSmartStep("05_smart", job, config)
    monkeypatch.setattr(
        step, "_build_prompt",
        lambda *_args, **_kwargs: "x" * (MAX_DOCUMENT_SMART_PROMPT_BYTES + 1),
    )
    monkeypatch.setattr(
        step.ai, "call",
        lambda *_args, **_kwargs: pytest.fail("oversized prompt must not call AI"),
    )

    with pytest.raises(InputInvalidError, match="smart prompt exceeds byte limit"):
        step.execute()


def test_smart_note_rechecks_prompt_limit_before_marker_retry(tmp_path, monkeypatch):
    job = _fixture(tmp_path)
    config = make_step_config(
        tmp_path, step_name="05_smart", pool="ai", pipeline="document",
    )
    config["step"]["prompt_template"] = "05_smart_document"
    step = DocumentSmartStep("05_smart", job, config)
    monkeypatch.setattr(
        step, "_build_prompt",
        lambda *_args, **_kwargs: "x" * MAX_DOCUMENT_SMART_PROMPT_BYTES,
    )
    calls = 0

    def call(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return "无效来源。[[source:UNKNOWN]]"

    monkeypatch.setattr(step.ai, "call", call)

    with pytest.raises(InputInvalidError, match="smart prompt exceeds byte limit"):
        step.execute()
    assert calls == 1
    assert not (job / "output" / "provenance_exact" / "smart.json").exists()
    assert not (job / "output" / "provenance_candidates" / "smart.json").exists()


def test_smart_note_accepts_safe_legacy_ocr_quality_after_restore(tmp_path, monkeypatch):
    job = _fixture(tmp_path)
    quality = {
        "schema_version": 1,
        "job_id": job.name,
        "status": "degraded",
        "reasons": ["scanned_pdf_ocr_error:RuntimeError", "scanned_pdf_ocr_failed"],
        "metrics": {"layout_detector_model": "doclayout_yolo.onnx"},
    }
    (job / "intermediate" / "quality.json").write_text(
        json.dumps(quality), encoding="utf-8",
    )
    config = make_step_config(
        tmp_path, step_name="05_smart", pool="ai", pipeline="document",
    )
    config["step"]["prompt_template"] = "05_smart_document"
    step = DocumentSmartStep("05_smart", job, config)
    prompts: list[str] = []

    def call(prompt, **_kwargs):
        prompts.append(prompt)
        step.ai.last_response = LLMResponse(
            content="", model="m", provider="anthropic", session_id="smart-session",
        )
        step.ai.last_provider = "anthropic"
        step.ai.last_model = "m"
        return "## 核心\n\n延迟为 3 ms。[[source:S1.P1]]"

    monkeypatch.setattr(step.ai, "call", call)
    result = step.execute()
    note = (job / result["note_file"]).read_text(encoding="utf-8")
    assert "scanned_pdf_ocr_failed" in prompts[0]
    assert "scanned_pdf_ocr_error" not in prompts[0]
    assert "doclayout_yolo.onnx" not in prompts[0]
    assert note.count("scanned_pdf_ocr_failed") == 1


def test_smart_note_drops_duplicate_marker_binding_without_retry(tmp_path, monkeypatch):
    job = _fixture(tmp_path)
    config = make_step_config(
        tmp_path, step_name="05_smart", pool="ai", pipeline="document",
    )
    config["step"]["prompt_template"] = "05_smart_document"
    step = DocumentSmartStep("05_smart", job, config)
    prompts: list[str] = []

    def call(prompt, **_kwargs):
        prompts.append(prompt)
        step.ai.last_response = LLMResponse(
            content="", model="m", provider="anthropic", session_id="smart-session",
        )
        step.ai.last_provider = "anthropic"
        step.ai.last_model = "m"
        return (
            "## 核心\n\n延迟为 3 ms。[[source:S1.P1]]\n\n"
            "## 证据\n\n来源再次报告延迟。[[source:S1.P1]]"
        )

    monkeypatch.setattr(step.ai, "call", call)

    result = step.execute()
    note = (job / result["note_file"]).read_text(encoding="utf-8")
    assert "延迟为 3 ms" in note
    assert "来源再次报告延迟" in note
    assert "[[source:" not in note
    assert result["semantic_candidates"] == 1
    assert len(prompts) == 1
    assert "validation_error" not in prompts[0]


def test_smart_note_duplicate_downgrade_does_not_hide_unknown_marker(
    tmp_path, monkeypatch,
):
    job = _fixture(tmp_path)
    config = make_step_config(
        tmp_path, step_name="05_smart", pool="ai", pipeline="document",
    )
    config["step"]["prompt_template"] = "05_smart_document"
    step = DocumentSmartStep("05_smart", job, config)
    calls = 0

    def call(_prompt, **_kwargs):
        nonlocal calls
        calls += 1
        step.ai.last_response = LLMResponse(
            content="", model="m", provider="anthropic", session_id="smart-session",
        )
        return (
            "## 核心\n\n延迟为 3 ms。[[source:S1.P1]]\n\n"
            "## 证据\n\n来源再次报告延迟。[[source:S1.P1]]\n\n"
            "## 未知\n\n无效来源。[[source:UNKNOWN]]"
        )

    monkeypatch.setattr(step.ai, "call", call)

    with pytest.raises(ValueError, match="unknown source marker"):
        step.execute()
    assert calls == 2
    assert not (job / "output" / "provenance" / "smart.json").exists()
    assert not (job / "output" / "provenance_candidates" / "smart.json").exists()


def test_document_concepts_never_read_legacy_original_markdown(tmp_path):
    job = _fixture(tmp_path)
    (job / "output" / "original.md").write_text("LEGACY MUST NOT BE READ", encoding="utf-8")
    versions = job / "output" / "versions"
    versions.mkdir(exist_ok=True)
    (versions / "notes_smart_qoder-cli_ultimate_20260810-000000.md").write_text(
        "# 笔记\n\nLatency is 3 ms.", encoding="utf-8",
    )
    config = make_step_config(
        tmp_path, step_name="07_concepts", pool="ai", pipeline="document",
    )
    config["step"]["prompt_template"] = "05_concepts"
    step = DocumentConceptsStep("07_concepts", job, config)
    source = step._resolve_concept_source()
    assert source is not None
    assert source.kind == "smart_note"
    assert source.note_type == "original"
    assert "LEGACY" not in source.text


def test_document_concepts_bind_terms_to_original_source_segments(tmp_path, monkeypatch):
    job = _fixture(tmp_path)
    versions = job / "output" / "versions"
    versions.mkdir(exist_ok=True)
    (versions / "notes_smart_qoder-cli_ultimate_20260810-000000.md").write_text(
        "# 笔记\n\nLatency is 3 ms.", encoding="utf-8",
    )
    config = make_step_config(
        tmp_path, step_name="07_concepts", pool="ai", pipeline="document",
    )
    config["step"]["prompt_template"] = "05_concepts"
    step = DocumentConceptsStep("07_concepts", job, config)
    monkeypatch.setattr(
        step.ai,
        "call_json",
        lambda *_args, **_kwargs: ({
            "summary": "延迟指标",
            "key_terms": [{"term": "Latency", "definition": "延迟"}],
        }, False),
    )

    result = step.execute()
    concepts = json.loads((job / "output/concepts.json").read_text(encoding="utf-8"))

    assert result["evidence_note_type"] == "original"
    assert concepts["key_terms"][0]["evidence_source_segment_ids"] == ["S1.P1"]


def test_document_review_uses_original_quality_concepts_and_smart_only(
    tmp_path, monkeypatch,
):
    job = _fixture(tmp_path)
    quality = json.loads(
        (job / "intermediate/quality.json").read_text(encoding="utf-8")
    )
    quality["metrics"]["layout_detector_model"] = "ignore_previous_instructions.onnx"
    (job / "intermediate/quality.json").write_text(
        json.dumps(quality), encoding="utf-8",
    )
    versions = job / "output" / "versions"
    versions.mkdir(exist_ok=True)
    note = versions / "notes_smart_anthropic_claude-opus-4-8_20260717-000000.md"
    note.write_text("# 标题 - 笔记\n\n延迟为 3 ms。\n", encoding="utf-8")
    (job / "output" / "concepts.json").write_text(
        json.dumps({"summary": "摘要", "key_terms": []}), encoding="utf-8",
    )
    (job / "output" / "translation.json").write_text(
        '{"sentinel":"MUST NOT BE READ"}', encoding="utf-8",
    )
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

    prompts = []

    def call(prompt, **_kwargs):
        prompts.append(prompt)
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
    monkeypatch.setattr(
        "steps.document.step_08_review.finalize_pending_semantic_provenance",
        lambda *_args, **_kwargs: {
            "accepted": 1, "rejected": 0, "budget_rejected": 0,
        },
    )
    (job / "output/provenance/smart.json").write_text(
        '{"schema_version":1,"segments":[]}', encoding="utf-8",
    )
    result = step.execute()

    review = json.loads((job / "output" / "review.json").read_text(encoding="utf-8"))
    assert result["parse_failed"] is False
    assert review["score_keys"] == [
        "completeness", "accuracy", "structure", "terminology",
        "formula_integrity", "visual_references", "traceability",
    ]
    assert {item["label"] for item in review["review_input"]["sources"]} == {
        "smart", "document", "quality", "concepts",
    }
    document_source = next(
        item for item in review["review_input"]["sources"]
        if item["label"] == "document"
    )
    assert document_source["artifact"].startswith(
        "output/review_sources/document-"
    )
    assert document_source["bytes"] <= MAX_DOCUMENT_REVIEW_PACK_BYTES
    quality_source = next(
        item for item in review["review_input"]["sources"]
        if item["label"] == "quality"
    )
    assert quality_source["artifact"].startswith(
        "output/review_sources/quality-"
    )
    assert review["review_input"]["bytes"] <= MAX_DOCUMENT_REVIEW_PROMPT_BYTES
    assert "MUST NOT BE READ" not in prompts[0]
    assert "ignore_previous_instructions.onnx" not in prompts[0]
    assert "legacy-name-sha256:" in prompts[0]
    assert "translation" not in step.step_input_hashes()


def test_document_review_rejects_unreliable_result_after_strict_retry(
    tmp_path, monkeypatch,
):
    job = _fixture(tmp_path)
    versions = job / "output/versions"
    versions.mkdir(exist_ok=True)
    (versions / "notes_smart_qoder-cli_ultimate_20260810-000000.md").write_text(
        "# 标题 - 笔记\n\n延迟为 3 ms。", encoding="utf-8",
    )
    (job / "output/concepts.json").write_text(
        '{"summary":"摘要","key_terms":[]}', encoding="utf-8",
    )
    config = make_step_config(
        tmp_path, step_name="08_review", pool="ai", pipeline="document",
    )
    config["step"]["prompt_template"] = "08_review"
    step = DocumentReviewStep("08_review", job, config)
    calls = []

    def call(prompt, **_kwargs):
        calls.append(prompt)
        step.ai.last_response = LLMResponse(
            content='{"type":"result","subtype":"error_during_execution"}',
            model="ultimate",
            provider="qoder-cli",
            finish_reason="error_during_execution",
        )
        step.ai.last_provider = "qoder-cli"
        step.ai.last_model = "ultimate"
        return step.ai.last_response.content

    monkeypatch.setattr(step.ai, "call", call)
    monkeypatch.setattr(
        "steps.document.step_08_review.finalize_pending_semantic_provenance",
        lambda *_args, **_kwargs: {
            "accepted": 1, "rejected": 0, "budget_rejected": 0,
        },
    )
    (job / "output/provenance/smart.json").write_text(
        '{"schema_version":1,"segments":[]}', encoding="utf-8",
    )

    with pytest.raises(ProcessingError, match="document review is unreliable"):
        step.execute()

    assert len(calls) == 2
    diagnostic = json.loads((job / "output/review.json").read_text(encoding="utf-8"))
    assert diagnostic["review_reliable"] is False


def test_document_review_downgrades_unbound_quote_only_after_strict_retry(
    tmp_path, monkeypatch,
):
    job = _fixture(tmp_path)
    versions = job / "output/versions"
    versions.mkdir(exist_ok=True)
    (versions / "notes_smart_qoder-cli_ultimate_20260810-000000.md").write_text(
        "# 标题 - 笔记\n\n延迟为 3 ms。", encoding="utf-8",
    )
    (job / "output/concepts.json").write_text(
        '{"summary":"摘要","key_terms":[]}', encoding="utf-8",
    )
    config = make_step_config(
        tmp_path, step_name="08_review", pool="ai", pipeline="document",
    )
    config["step"]["prompt_template"] = "08_review"
    step = DocumentReviewStep("08_review", job, config)
    score_keys = [
        "completeness", "accuracy", "structure", "terminology",
        "formula_integrity", "visual_references", "traceability",
    ]
    response = json.dumps({
        **{key: 5 for key in score_keys},
        "key_terms": [], "missing_concepts": [],
        "top3_improvements": ["a", "b", "c"],
        "issues": [{
            "type": "traceability", "severity": "error", "dimension": "accuracy",
            "claim": "延迟", "message": "模型改写了引用",
            "evidence_status": "supported",
            "locator": {"source": "smart", "quote": "延迟约为 3 ms。"},
        }],
    }, ensure_ascii=False)
    calls = []

    def call(prompt, **_kwargs):
        calls.append(prompt)
        step.ai.last_response = LLMResponse(
            content=response, model="ultimate", provider="qoder-cli",
            finish_reason="success", raw={"is_error": False},
        )
        step.ai.last_provider = "qoder-cli"
        step.ai.last_model = "ultimate"
        return response

    monkeypatch.setattr(step.ai, "call", call)
    monkeypatch.setattr(
        "steps.document.step_08_review.finalize_pending_semantic_provenance",
        lambda *_args, **_kwargs: {
            "accepted": 1, "rejected": 0, "budget_rejected": 0,
        },
    )
    (job / "output/provenance/smart.json").write_text(
        '{"schema_version":1,"segments":[]}', encoding="utf-8",
    )

    result = step.execute()
    review = json.loads((job / "output/review.json").read_text(encoding="utf-8"))

    assert len(calls) == 2
    assert result["parse_failed"] is False
    assert review["review_reliable"] is True
    assert review["issues"] == [{
        "type": "traceability", "severity": "error", "dimension": "accuracy",
        "claim": "延迟", "message": "模型改写了引用",
        "evidence_status": "insufficient",
        "reason": "model-provided quote did not match bound source exactly",
    }]


def test_document_review_fingerprint_binds_semantic_source_protocol_and_owned_state(
    tmp_path, monkeypatch,
):
    job = _fixture(tmp_path)
    versions = job / "output/versions"
    versions.mkdir(exist_ok=True)
    (versions / "notes_smart_qoder-cli_ultimate_20260810-000000.md").write_text(
        "# 笔记\n\n延迟为 3 ms。", encoding="utf-8",
    )
    (job / "output/concepts.json").write_text(
        '{"summary":"摘要","key_terms":[]}', encoding="utf-8",
    )
    (job / "output/provenance_exact").mkdir(exist_ok=True)
    (job / "output/provenance_exact/smart.json").write_text("{}", encoding="utf-8")
    (job / "output/provenance_candidates/smart.json").write_text("{}", encoding="utf-8")
    (job / "output/provenance/smart.json").write_text("{}", encoding="utf-8")
    (job / "output/provenance/semantic_batch.json").write_text("{}", encoding="utf-8")
    (job / "output/ai_logs").mkdir(exist_ok=True)
    (job / "output/ai_logs/08_review.jsonl").write_text("{}\n", encoding="utf-8")
    config = make_step_config(
        tmp_path, step_name="08_review", pool="ai", pipeline="document",
    )
    config["step"]["prompt_template"] = "08_review"
    step = DocumentReviewStep("08_review", job, config)
    template_digests = {
        "08_review": "review-v1",
        "semantic_attestation": "semantic-v1",
    }
    monkeypatch.setattr(
        step.ai, "template_hash", lambda name: template_digests[name],
    )

    before = step.step_input_hashes()
    assert {
        "source_manifest", "semantic_attestation_template",
        "semantic_batch_commit", "semantic_ai_log", "semantic_smart_final",
        "semantic_smart_published",
    } <= set(before)

    source_path = job / "intermediate/source_segments.json"
    source_path.write_bytes(source_path.read_bytes() + b"\n")
    assert step.step_input_hashes()["source_manifest"] != before["source_manifest"]

    template_digests["semantic_attestation"] = "semantic-v2"
    assert (
        step.step_input_hashes()["semantic_attestation_template"]
        != before["semantic_attestation_template"]
    )
    (job / "output/provenance/smart.json").write_text(
        '{"changed":true}', encoding="utf-8",
    )
    assert (
        step.step_input_hashes()["semantic_smart_published"]
        != before["semantic_smart_published"]
    )


def _publication_fixture(tmp_path):
    job = _fixture(tmp_path)
    versions = job / "output" / "versions"
    versions.mkdir(exist_ok=True)
    note = versions / "notes_smart_qoder-cli_ultimate_20260810-000000.md"
    note.write_text("# 笔记\n\nLatency is 3 ms.", encoding="utf-8")
    (job / "output/concepts.json").write_text("{}", encoding="utf-8")
    (job / "output/review.json").write_text("{}", encoding="utf-8")
    (job / "output/provenance/smart.json").write_text("{}", encoding="utf-8")
    return job, str(note.relative_to(job))


def test_document_publish_writes_bound_manifest_after_reliable_review(
    tmp_path, monkeypatch,
):
    job, note_file = _publication_fixture(tmp_path)
    config = make_step_config(
        tmp_path, step_name="09_publish", pool="cpu", pipeline="document",
    )
    step = DocumentPublishStep("09_publish", job, config)

    async def verified(*_args, **_kwargs):
        return {
            "review_reliable": True,
            "score_keys": ["accuracy", "traceability", "structure"],
            "overall": 4.0,
            "accuracy": 4,
            "traceability": 4,
            "structure": 3,
            "issues": [],
            "note_file": note_file,
        }

    monkeypatch.setattr("steps.document.step_09_publish.verify_persisted_review", verified)
    verified_chain = [{"evidence_id": "ce_test"}]

    async def verify_chain(**_kwargs):
        return verified_chain

    monkeypatch.setattr(
        "steps.document.step_09_publish.build_canonical_evidence_records_with_reader",
        verify_chain,
    )
    result = step.execute()
    publication = json.loads((job / "output/publication.json").read_text(encoding="utf-8"))

    assert result["published"] is True
    assert result["canonical_evidence"] == 1
    assert publication["status"] == "published"
    assert {item["path"] for item in publication["artifacts"]} >= {
        "output/review.json", "output/concepts.json", note_file,
    }


def test_document_publish_source_hash_uses_canonical_reader_format(tmp_path):
    job, _note_file = _publication_fixture(tmp_path)
    config = make_step_config(
        tmp_path, step_name="09_publish", pool="cpu", pipeline="document",
    )
    step = DocumentPublishStep("09_publish", job, config)
    source = (job / "input/source.html").read_bytes()

    actual = __import__("asyncio").run(step._sha256_evidence_file("input/source.html"))

    assert actual == __import__("hashlib").sha256(source).hexdigest()
    assert not actual.startswith("sha256:")


def test_document_publish_rejects_unreliable_review_without_output(
    tmp_path, monkeypatch,
):
    job, note_file = _publication_fixture(tmp_path)
    config = make_step_config(
        tmp_path, step_name="09_publish", pool="cpu", pipeline="document",
    )
    step = DocumentPublishStep("09_publish", job, config)

    async def rejected(*_args, **_kwargs):
        return {
            "review_reliable": False,
            "score_keys": ["accuracy", "traceability"],
            "overall": 4.0,
            "accuracy": 4,
            "traceability": 4,
            "issues": [],
            "note_file": note_file,
        }

    monkeypatch.setattr("steps.document.step_09_publish.verify_persisted_review", rejected)
    with pytest.raises(InputInvalidError, match="review_unreliable"):
        step.execute()
    assert not (job / "output/publication.json").exists()


def test_document_publish_rejects_invalid_semantic_chain_without_output(
    tmp_path, monkeypatch,
):
    job, note_file = _publication_fixture(tmp_path)
    config = make_step_config(
        tmp_path, step_name="09_publish", pool="cpu", pipeline="document",
    )
    step = DocumentPublishStep("09_publish", job, config)

    async def verified(*_args, **_kwargs):
        return {
            "review_reliable": True,
            "score_keys": ["accuracy", "traceability"],
            "overall": 4.0,
            "accuracy": 4,
            "traceability": 4,
            "issues": [],
            "note_file": note_file,
        }

    async def invalid_chain(**_kwargs):
        raise ValueError("semantic batch provenance is incomplete")

    monkeypatch.setattr("steps.document.step_09_publish.verify_persisted_review", verified)
    monkeypatch.setattr(
        "steps.document.step_09_publish.build_canonical_evidence_records_with_reader",
        invalid_chain,
    )
    with pytest.raises(ValueError, match="semantic batch provenance is incomplete"):
        step.execute()
    assert not (job / "output/publication.json").exists()
