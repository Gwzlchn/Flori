"""Ask 来源清单与引用校验测试。"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json

import pytest

from shared.ask_citations import (
    build_source_manifest,
    validate_ask_citations,
    validate_bound_ask_citations,
)


def _passages() -> list[dict]:
    return [
        {
            "job_id": "job_video",
            "title": "反向传播详解",
            "domain": "ml",
            "content_type": "video",
            "note_type": "smart",
            "body": "反向传播通过链式法则计算梯度。",
            "evidence": {
                "chunk_id": "job_video:smart:0", "section": "原理",
                "artifact_sha256": "a" * 64,
            },
        },
        {
            "job_id": "job_paper",
            "title": "优化算法",
            "domain": "ml",
            "content_type": "paper",
            "note_type": "smart",
            "body": "梯度下降使用梯度更新模型参数。",
            "artifact_sha256": "b" * 64,
            "evidence": {"chunk_id": "job_paper:smart:0", "section": "方法"},
        },
    ]


def _manifest(task_id: str = "at_1") -> dict:
    # 生产 helper 是 body hash 的真相源,fixture 不复制规范化算法。
    return build_source_manifest(task_id, "反向传播如何训练模型?", _passages())


def test_manifest_binds_task_and_source_identity() -> None:
    manifest = _manifest()
    assert manifest["kind"] == "ask_sources"
    assert manifest["task_id"] == "at_1"
    assert len(manifest["sources"]) == 2
    source = manifest["sources"][0]
    assert source["index"] == 1
    assert source["note_type"] == "smart"
    assert source["artifact_sha256"] == "a" * 64
    assert len(source["body_sha256"]) == 64
    expected_identity = {
        "job_id": source["job_id"], "note_type": source["note_type"],
        "artifact_sha256": source["artifact_sha256"],
        "body_sha256": source["body_sha256"],
    }
    expected_fingerprint = hashlib.sha256(json.dumps(
        expected_identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    assert source["source_fingerprint"] == expected_fingerprint
    assert len(manifest["manifest_sha256"]) == 64


def test_valid_citations_bind_each_claim_to_its_source() -> None:
    answer = (
        "反向传播通过链式法则计算梯度 [来源1]。\n"
        "梯度下降使用梯度更新模型参数 [来源2]。"
    )
    result = validate_ask_citations("at_1", answer, _manifest())
    assert result["status"] == "valid"
    assert result["checked"] == 2
    assert result["metrics"] == {
        "structural_precision": 1.0,
        "source_precision": 1.0,
        "claim_precision": 1.0,
        "coverage": 1.0,
    }
    assert all(item["status"] == "valid" for item in result["items"])


def test_supported_cited_markdown_heading_is_valid() -> None:
    result = validate_ask_citations(
        "at_1", "## 反向传播通过链式法则计算梯度 [来源1]", _manifest(),
    )
    assert result["status"] == "valid"
    assert result["metrics"]["coverage"] == 1.0
    assert result["items"][0]["claim"] == "反向传播通过链式法则计算梯度"


def test_unsupported_cited_markdown_heading_fails_closed() -> None:
    result = validate_ask_citations(
        "at_1", "## 模型会自动获得意识 [来源1]", _manifest(),
    )
    assert result["status"] == "invalid"
    assert "unsupported_claim" in result["items"][0]["errors"]


def test_uncited_factual_markdown_heading_cannot_be_valid() -> None:
    result = validate_ask_citations(
        "at_1", "## 模型会自动获得意识\n反向 [来源1]。", _manifest(),
    )
    assert result["status"] == "unverified"
    assert "uncited_claims" in result["errors"]
    assert result["metrics"]["coverage"] == 0.5


def test_empty_markdown_heading_does_not_create_a_claim() -> None:
    result = validate_ask_citations(
        "at_1", "##\n反向传播通过链式法则计算梯度 [来源1]。", _manifest(),
    )
    assert result["status"] == "valid"
    assert result["metrics"]["coverage"] == 1.0


@pytest.mark.parametrize(
    ("answer", "error"),
    [
        ("没有引用的回答。", "missing_citations"),
        ("未知来源 [来源3]。", "unknown_source_index"),
        ("## 反向传播通过链式法则计算梯度 [来源x]。", "malformed_citation"),
        ("模型会自动获得意识 [来源1]。", "unsupported_claim"),
    ],
)
def test_invalid_or_unsupported_citations_fail_closed(answer: str, error: str) -> None:
    result = validate_ask_citations("at_1", answer, _manifest())
    assert result["status"] == "invalid"
    assert error in result["errors"] or any(
        error in item["errors"] for item in result["items"]
    )


def test_cross_task_manifest_fails_closed() -> None:
    result = validate_ask_citations(
        "at_other", "## 反向传播通过链式法则计算梯度 [来源1]。", _manifest(),
    )
    assert result["status"] == "invalid"
    assert "manifest_task_mismatch" in result["errors"]
    assert result["checked"] == 0


@pytest.mark.parametrize("field", ["body", "body_sha256", "artifact_sha256", "source_fingerprint"])
def test_tampered_source_manifest_fails_closed(field: str) -> None:
    manifest = deepcopy(_manifest())
    manifest["sources"][0][field] = "tampered"
    result = validate_ask_citations(
        "at_1", "反向传播通过链式法则计算梯度 [来源1]。", manifest,
    )
    assert result["status"] == "invalid"
    assert "invalid_source_manifest" in result["errors"]


def test_manifest_rejects_unbound_or_oversized_inputs() -> None:
    with pytest.raises(ValueError, match="task_id"):
        build_source_manifest("", "问题", _passages())
    with pytest.raises(ValueError, match="最多"):
        build_source_manifest("at_many", "问题", _passages() * 11)
    broken = _passages()
    broken[0]["artifact_sha256"] = "not-a-hash"
    with pytest.raises(ValueError, match="artifact_sha256"):
        build_source_manifest("at_bad", "问题", broken)
    missing = _passages()
    missing[0]["evidence"].pop("artifact_sha256")
    with pytest.raises(ValueError, match="artifact_sha256"):
        build_source_manifest("at_missing", "问题", missing)


def test_result_manifest_must_equal_original_task_manifest() -> None:
    original = _manifest()
    replacement = build_source_manifest("at_1", "替换后的问题", [_passages()[1]])
    result = validate_bound_ask_citations(
        "at_1", "梯度下降使用梯度更新模型参数 [来源1]。", replacement, original,
    )
    assert result["status"] == "invalid"
    assert "source_manifest_mismatch" in result["errors"]


@pytest.mark.parametrize(
    ("result_manifest", "original_manifest", "error"),
    [
        (_manifest(), None, "source_manifest_unbound"),
        (None, _manifest(), "source_manifest_missing"),
    ],
)
def test_missing_manifest_binding_fails_closed(
    result_manifest: dict | None, original_manifest: dict | None, error: str,
) -> None:
    result = validate_bound_ask_citations(
        "at_1", "反向传播通过链式法则计算梯度 [来源1]。",
        result_manifest, original_manifest,
    )
    assert result["status"] == "invalid"
    assert error in result["errors"]
