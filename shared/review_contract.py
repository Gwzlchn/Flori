"""评审 v2 的严格解析、可靠性判定与 API 投影。"""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any, Awaitable, Callable

from .models import LLMResponse
from .storage import publish_content_addressed_path, read_path_bounded


REVIEW_SCHEMA_VERSION = 2
MAX_REVIEW_SOURCE_BYTES = 8 * 1024 * 1024
MAX_REVIEW_SOURCES = 14
MAX_REVIEW_SOURCE_AGGREGATE_BYTES = 8 * 1024 * 1024
ISSUE_TYPES = {"consistency", "missing_in_source", "missing_external", "traceability"}
ISSUE_SEVERITIES = {"info", "warning", "error"}
_SCORE_KEYS_BY_PIPELINE = {
    "video": ("completeness", "accuracy", "structure", "terminology", "visual_integration", "readability"),
    "paper": ("completeness", "accuracy", "structure", "terminology", "formula_integrity", "figure_references"),
    "article": ("completeness", "accuracy", "structure", "readability", "insight"),
    "audio": ("completeness", "accuracy", "structure", "terminology", "conciseness", "readability"),
}
_REVIEW_RESPONSE_KEYS = {
    "key_terms", "missing_concepts", "top3_improvements", "issues",
}
_REVIEW_FIXED_KEYS = {
    "schema_version", "score_keys", "overall", "key_terms", "missing_concepts",
    "top3_improvements", "issues", "review_reliable", "reliability_reasons",
    "review_input", "completion", "parse", "citation_validation", "review_coverage",
    "note_file", "provider", "model", "generated_at",
}
_RECORD_VALUE_KEYS = {"artifact", "sha256", "bytes", "chars", "truncated"}
_SOURCE_RECORD_KEYS = _RECORD_VALUE_KEYS | {"label"}
_ISSUE_BASE_KEYS = {
    "type", "severity", "dimension", "claim", "message", "evidence_status",
}
_ATTEMPT_BASE_KEYS = {"tier", "provider", "model", "ok"}
_CONTENT_ADDRESSED_SOURCE_RE = re.compile(
    r"output/review_sources/(?P<label>sections|transcript|figures)-(?P<digest>[0-9a-f]{64})\.md",
)


def sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def source_record(job_dir: Path, rel_path: str, *, label: str) -> tuple[str, dict[str, Any]]:
    """读取完整评审输入并记录精确摘要;超过防御上限时拒绝,绝不静默截断。"""
    path = job_dir / rel_path
    resolved = path.resolve()
    root = job_dir.resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError(f"review source escapes job dir: {rel_path}")
    try:
        data = read_path_bounded(
            path, MAX_REVIEW_SOURCE_BYTES, trusted_root=job_dir,
        )
    except OSError as exc:
        raise ValueError(f"review source cannot be read: {rel_path}") from exc
    if len(data) > MAX_REVIEW_SOURCE_BYTES:
        raise ValueError(f"review source exceeds {MAX_REVIEW_SOURCE_BYTES} bytes: {rel_path}")
    return source_record_from_data(data, rel_path, label=label)


def source_record_from_data(
    data: bytes, rel_path: str, *, label: str,
) -> tuple[str, dict[str, Any]]:
    """从已完成有界读取的数据生成记录,避免同一产物为摘要再次读盘。"""
    if not isinstance(data, bytes):
        raise ValueError("review source data must be bytes")
    if len(data) > MAX_REVIEW_SOURCE_BYTES:
        raise ValueError(f"review source exceeds {MAX_REVIEW_SOURCE_BYTES} bytes: {rel_path}")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"review source is not UTF-8: {rel_path}") from exc
    if not text.strip():
        raise ValueError(f"review source is empty: {rel_path}")
    return text, {
        "label": label,
        "artifact": rel_path,
        "sha256": sha256_bytes(data),
        "bytes": len(data),
        "chars": len(text),
        "truncated": False,
    }


def persist_review_source(job_dir: Path, text: str, *, label: str) -> tuple[str, dict[str, Any]]:
    """按内容地址持久化精确送评文本,使历史 review locator 可稳定复算。"""
    if re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", label) is None:
        raise ValueError("review source label is invalid")
    data = text.encode("utf-8")
    if not text.strip():
        raise ValueError("review source is empty")
    if len(data) > MAX_REVIEW_SOURCE_BYTES:
        raise ValueError(f"review source exceeds {MAX_REVIEW_SOURCE_BYTES} bytes")
    digest = hashlib.sha256(data).hexdigest()
    rel = f"output/review_sources/{label}-{digest}.md"
    path = job_dir / rel
    try:
        publish_content_addressed_path(path, data)
    except ValueError as exc:
        raise ValueError("review source content-address collision") from exc
    return source_record_from_data(data, rel, label=label)


def _terminal_status(
    provider: str, raw_reason: str | None, raw_error: bool | None,
) -> str:
    """仅从持久化的最小终态证明重算状态。"""
    raw_reason = raw_reason.strip() if type(raw_reason) is str else ""
    reason = raw_reason.lower()
    status = "unknown"
    if provider == "anthropic":
        if reason in {"end_turn", "stop_sequence", "stop"}:
            status = "complete"
        elif reason in {"max_tokens", "length"}:
            status = "truncated"
    elif provider == "openai" or provider in {"deepseek", "kimi", "local"}:
        if reason == "stop":
            status = "complete"
        elif reason in {"length", "max_tokens"}:
            status = "truncated"
        elif reason in {"content_filter", "error", "failed"}:
            status = "error"
    elif provider == "claude-cli":
        if type(raw_error) is not bool:
            return "unknown"
        if raw_error is True:
            status = "error"
        elif reason in {"success", "end_turn", "stop_sequence", "stop"}:
            status = "complete"
        elif any(mark in reason for mark in ("max_turn", "max_token", "context", "length")):
            status = "truncated"
        elif reason in {"error", "failed", "failure"}:
            status = "error"
    elif provider == "codex-cli":
        if type(raw_error) is not bool:
            return "unknown"
        if raw_error is True or reason in {"error", "failed", "turn.failed"}:
            status = "error"
        elif reason in {"turn.completed", "completed", "stop"}:
            status = "complete"
    elif provider == "dry-run" and reason in {"stop", "complete", "completed"}:
        status = "complete"
    return status


def completion_from_response(response: LLMResponse) -> dict[str, Any]:
    """持久化可重算的最小 provider 终态证明。"""
    provider = response.provider if type(response.provider) is str else ""
    raw_reason = (
        response.finish_reason.strip()
        if type(response.finish_reason) is str else ""
    )
    raw = response.raw if isinstance(response.raw, dict) else {}
    if provider == "claude-cli":
        value = raw.get("is_error")
        raw_error = value if type(value) is bool else None
    elif provider == "codex-cli":
        value = raw.get("errors")
        raw_error = bool(value) if type(value) is list else None
    else:
        raw_error = False
    status = _terminal_status(provider, raw_reason or None, raw_error)

    return {
        "schema_version": 2,
        "status": status,
        "raw_finish_reason": raw_reason or None,
        "raw_error": raw_error,
        "tier_used": response.tier_used,
        "attempts": response.attempts,
    }


def _strict_scores(obj: dict[str, Any], score_keys: list[str], errors: list[str]) -> dict[str, int]:
    scores: dict[str, int] = {}
    for key in score_keys:
        value = obj.get(key)
        if type(value) is not int or not 1 <= value <= 5:
            errors.append(f"{key} must be an integer from 1 to 5")
        else:
            scores[key] = value
    return scores


def _strict_string_list(value: Any, name: str, errors: list[str]) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(x, str) or not x.strip() for x in value):
        errors.append(f"{name} must be a list of non-empty strings")
        return []
    return value


def _strict_key_terms(value: Any, errors: list[str]) -> list[dict[str, str]]:
    if not isinstance(value, list):
        errors.append("key_terms must be a list")
        return []
    result = []
    for item in value:
        if not isinstance(item, dict):
            errors.append("key_terms item must be an object")
            continue
        if set(item) != {"term", "definition"}:
            errors.append("key_terms item fields are invalid")
        term, definition = item.get("term"), item.get("definition")
        if (
            not isinstance(term, str) or not term.strip()
            or not isinstance(definition, str) or not definition.strip()
        ):
            errors.append("key_terms item requires term and definition strings")
            continue
        result.append({"term": term.strip(), "definition": definition.strip()})
    return result


def _strict_issues(
    value: Any,
    score_keys: list[str],
    review_source_texts: dict[str, str],
    errors: list[str],
    *,
    persisted: bool = False,
) -> list[dict[str, Any]]:
    """校验结构化 issue；supported locator 必须命中本次评审的真实来源原文。"""
    if not isinstance(value, list):
        errors.append("issues must be a list")
        return []
    result = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            errors.append(f"issues[{index}] must be an object")
            continue
        issue_type = item.get("type")
        severity = item.get("severity")
        dimension = item.get("dimension")
        claim = item.get("claim")
        message = item.get("message")
        evidence_status = item.get("evidence_status")
        locator = item.get("locator")
        reason = item.get("reason")
        expected_fields = _ISSUE_BASE_KEYS
        if evidence_status == "supported":
            expected_fields = expected_fields | {"locator"}
        elif evidence_status == "insufficient":
            expected_fields = expected_fields | {"reason"}
        if set(item) != expected_fields:
            errors.append(f"issues[{index}].fields are invalid")
        if not isinstance(issue_type, str) or issue_type not in ISSUE_TYPES:
            errors.append(f"issues[{index}].type is invalid")
        if not isinstance(severity, str) or severity not in ISSUE_SEVERITIES:
            errors.append(f"issues[{index}].severity is invalid")
        if dimension not in score_keys:
            errors.append(f"issues[{index}].dimension is invalid")
        if not isinstance(claim, str) or not claim.strip():
            errors.append(f"issues[{index}].claim is required")
        if not isinstance(message, str) or not message.strip():
            errors.append(f"issues[{index}].message is required")
        if not isinstance(evidence_status, str) or evidence_status not in {"supported", "insufficient"}:
            errors.append(f"issues[{index}].evidence_status is invalid")
        canonical = {
            "type": issue_type,
            "severity": severity,
            "dimension": dimension,
            "claim": claim.strip() if isinstance(claim, str) else claim,
            "message": message.strip() if isinstance(message, str) else message,
            "evidence_status": evidence_status,
        }
        if evidence_status == "supported":
            if not isinstance(locator, dict):
                errors.append(f"issues[{index}].locator is required")
            else:
                expected_locator_fields = {"source", "quote", "offset"} if persisted else {"source", "quote"}
                if set(locator) != expected_locator_fields:
                    errors.append(f"issues[{index}].locator fields are invalid")
                source = locator.get("source")
                quote = locator.get("quote")
                source_text = review_source_texts.get(source) if isinstance(source, str) else None
                if source_text is None:
                    errors.append(f"issues[{index}].locator.source is invalid")
                if not isinstance(quote, str) or not quote.strip():
                    errors.append(f"issues[{index}].locator.quote is required")
                elif source_text is not None:
                    offset = source_text.find(quote)
                    if offset < 0:
                        errors.append(f"issues[{index}].locator.quote was not found")
                    else:
                        canonical["locator"] = {
                            "source": source, "quote": quote, "offset": offset,
                        }
        if evidence_status == "insufficient":
            if not isinstance(reason, str) or not reason.strip():
                errors.append(f"issues[{index}].reason is required")
            else:
                canonical["reason"] = reason.strip()
        result.append(canonical)
    return result


def _extract_object(raw: str) -> dict[str, Any] | None:
    match = re.search(r"\{[\s\S]*\}", raw or "")
    if not match:
        return None
    try:
        obj = json.loads(match.group(0))
    except (json.JSONDecodeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def parse_review(
    raw: str,
    score_keys: list[str],
    response: LLMResponse,
    *,
    review_input: dict[str, Any],
    review_source_texts: dict[str, str] | None = None,
    citation_validation: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], bool]:
    """严格解析 v2;提取/抢救只保留诊断,永远不能得到可靠评审。"""
    parse_errors: list[str] = []
    mode = "strict"
    try:
        obj = json.loads((raw or "").strip())
        if not isinstance(obj, dict):
            raise ValueError("root must be object")
    except (json.JSONDecodeError, ValueError):
        obj = _extract_object(raw)
        mode = "extracted" if obj is not None else "fallback"
    if obj is None:
        obj = {}
        parse_errors.append("response is not a JSON object")

    if not _is_json_value(obj):
        parse_errors.append("response contains non-JSON values")
    if set(obj) != set(score_keys) | _REVIEW_RESPONSE_KEYS:
        parse_errors.append("response fields do not match review schema")

    scores = _strict_scores(obj, score_keys, parse_errors)
    key_terms = _strict_key_terms(obj.get("key_terms"), parse_errors)
    missing = _strict_string_list(obj.get("missing_concepts"), "missing_concepts", parse_errors)
    top3 = _strict_string_list(obj.get("top3_improvements"), "top3_improvements", parse_errors)
    if len(top3) != 3:
        parse_errors.append("top3_improvements must contain exactly 3 items")
    issues = _strict_issues(
        obj.get("issues"), score_keys, review_source_texts or {}, parse_errors,
    )
    completion = completion_from_response(response)
    citation = citation_validation or {
        "status": "not_applicable", "checked": 0, "items": [],
    }
    citation_status = citation.get("status") if isinstance(citation, dict) else None
    schema_valid = not parse_errors and len(scores) == len(score_keys)
    reliable = (
        mode == "strict"
        and schema_valid
        and completion["status"] == "complete"
        and not review_input.get("truncated")
        and isinstance(citation_status, str)
        and citation_status in {"valid", "not_applicable"}
    )
    reasons = []
    if mode != "strict":
        reasons.append(f"parse_{mode}")
    if not schema_valid:
        reasons.append("schema_invalid")
    if completion["status"] != "complete":
        reasons.append(f"completion_{completion['status']}")
    if review_input.get("truncated"):
        reasons.append("input_truncated")
    if not isinstance(citation_status, str) or citation_status not in {"valid", "not_applicable"}:
        reasons.append(f"citation_{citation_status or 'unknown'}")

    result: dict[str, Any] = {
        "schema_version": REVIEW_SCHEMA_VERSION,
        "score_keys": list(score_keys),
        **scores,
        "overall": round(sum(scores.values()) / len(score_keys), 1) if len(scores) == len(score_keys) else None,
        "key_terms": key_terms,
        "missing_concepts": missing,
        "top3_improvements": top3,
        "issues": issues,
        "review_reliable": reliable,
        "reliability_reasons": reasons,
        "review_input": review_input,
        "completion": completion,
        "parse": {"mode": mode, "schema_valid": schema_valid, "errors": parse_errors},
        "citation_validation": citation,
    }
    if not reliable:
        result["raw_response"] = (raw or "")[:2000]
    return result, not schema_valid or mode != "strict"


def _safe_review_rel(rel: Any) -> bool:
    return (
        isinstance(rel, str)
        and rel.startswith("output/")
        and "\x00" not in rel
        and ".." not in Path(rel).parts
        and not Path(rel).is_absolute()
    )


def _same_json_value(left: Any, right: Any) -> bool:
    """按 JSON 类型递归比较,阻止 bool==int 之类 Python 宽松等值绕过。"""
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(
            _same_json_value(left[key], right[key]) for key in left
        )
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _same_json_value(a, b) for a, b in zip(left, right)
        )
    return left == right


def _is_json_value(value: Any) -> bool:
    """拒绝非 JSON 类型与 NaN/Infinity,避免嵌套对象绕过精确 schema。"""
    if value is None or type(value) in {bool, int, str}:
        return True
    if type(value) is float:
        return math.isfinite(value)
    if isinstance(value, list):
        return all(_is_json_value(item) for item in value)
    if isinstance(value, dict):
        return all(type(key) is str and _is_json_value(item) for key, item in value.items())
    return False


def _completion_is_strict(
    completion: Any,
    errors: list[str],
    *,
    review_provider: Any,
    review_model: Any,
) -> bool:
    if not isinstance(completion, dict) or set(completion) != {
        "schema_version", "status", "raw_finish_reason", "raw_error",
        "tier_used", "attempts",
    }:
        errors.append("completion_schema_invalid")
        return False
    valid = True
    if type(completion.get("schema_version")) is not int or completion.get("schema_version") != 2:
        errors.append("completion_schema_version_invalid")
        valid = False
    raw_error = completion.get("raw_error")
    if review_provider in {"claude-cli", "codex-cli"}:
        if raw_error is None:
            errors.append("completion_terminal_proof_missing")
            valid = False
        elif type(raw_error) is not bool:
            errors.append("completion_raw_error_invalid")
            valid = False
    elif type(raw_error) is not bool:
        errors.append("completion_raw_error_invalid")
        valid = False
    elif raw_error:
        errors.append("completion_raw_error_unexpected")
        valid = False
    recomputed = _terminal_status(
        review_provider if type(review_provider) is str else "",
        completion.get("raw_finish_reason"),
        raw_error if type(raw_error) is bool else None,
    )
    if completion.get("status") != recomputed:
        errors.append("completion_status_mismatch")
        valid = False
    if completion.get("status") != "complete":
        errors.append("completion_not_complete")
        valid = False
    raw_reason = completion.get("raw_finish_reason")
    if raw_reason is not None and (type(raw_reason) is not str or not raw_reason.strip()):
        errors.append("completion_finish_reason_invalid")
        valid = False
    tier_used = completion.get("tier_used")
    if (
        type(tier_used) is not str
        or tier_used not in {"primary", "fallback", "text_fallback"}
    ):
        errors.append("completion_tier_invalid")
        valid = False
    attempts = completion.get("attempts")
    if not isinstance(attempts, list) or not 1 <= len(attempts) <= 8:
        errors.append("completion_attempts_invalid")
        return False
    success_indexes: list[int] = []
    for index, attempt in enumerate(attempts):
        if not isinstance(attempt, dict):
            errors.append(f"completion_attempts_{index}_invalid")
            valid = False
            continue
        ok = attempt.get("ok")
        if ok is True:
            success_indexes.append(index)
        expected = _ATTEMPT_BASE_KEYS if ok is True else _ATTEMPT_BASE_KEYS | {"error_class", "error"}
        if ok is False and "transcript_path" in attempt:
            expected = expected | {"transcript_path"}
        if set(attempt) != expected:
            errors.append(f"completion_attempts_{index}_fields_invalid")
            valid = False
        if (
            type(attempt.get("tier")) is not str
            or attempt.get("tier") not in {"primary", "fallback", "text_fallback"}
            or type(attempt.get("provider")) is not str
            or not attempt.get("provider", "").strip()
            or type(attempt.get("model")) is not str
            or not attempt.get("model", "").strip()
            or type(ok) is not bool
        ):
            errors.append(f"completion_attempts_{index}_values_invalid")
            valid = False
        if ok is False and (
            type(attempt.get("error_class")) is not str
            or not attempt.get("error_class", "").strip()
            or type(attempt.get("error")) is not str
            or not attempt.get("error", "").strip()
        ):
            errors.append(f"completion_attempts_{index}_error_invalid")
            valid = False
        if "transcript_path" in attempt and (
            type(attempt.get("transcript_path")) is not str
            or not attempt.get("transcript_path", "").strip()
        ):
            errors.append(f"completion_attempts_{index}_transcript_invalid")
            valid = False
    if len(success_indexes) != 1:
        errors.append("completion_success_count_invalid")
        valid = False
    else:
        success_index = success_indexes[0]
        success = attempts[success_index]
        if success_index != len(attempts) - 1:
            errors.append("completion_success_not_last")
            valid = False
        if success.get("tier") != tier_used:
            errors.append("completion_tier_mismatch")
            valid = False
        if success.get("provider") != review_provider:
            errors.append("completion_provider_mismatch")
            valid = False
        if success.get("model") != review_model:
            errors.append("completion_model_mismatch")
            valid = False
    return valid


async def _read_record(
    record: Any,
    read_file: Callable[[str], Awaitable[bytes | None]],
    errors: list[str],
    prefix: str,
    *,
    expected_keys: set[str] = _SOURCE_RECORD_KEYS,
) -> str | None:
    if not isinstance(record, dict):
        errors.append(f"{prefix}_record_invalid")
        return None
    if set(record) != expected_keys:
        errors.append(f"{prefix}_fields_invalid")
    rel = record.get("artifact")
    if not _safe_review_rel(rel):
        errors.append(f"{prefix}_artifact_invalid")
        return None
    try:
        data = await read_file(rel)
    except (OSError, ValueError):
        data = None
    if data is None:
        errors.append(f"{prefix}_artifact_missing")
        return None
    if not isinstance(data, bytes):
        errors.append(f"{prefix}_artifact_type_invalid")
        return None
    if len(data) > MAX_REVIEW_SOURCE_BYTES:
        errors.append(f"{prefix}_too_large")
        return None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        errors.append(f"{prefix}_artifact_not_utf8")
        return None
    if not text.strip():
        errors.append(f"{prefix}_artifact_empty")
    if record.get("sha256") != sha256_bytes(data):
        errors.append(f"{prefix}_sha256_mismatch")
    if type(record.get("bytes")) is not int or record.get("bytes") != len(data):
        errors.append(f"{prefix}_bytes_mismatch")
    if type(record.get("chars")) is not int or record.get("chars") != len(text):
        errors.append(f"{prefix}_chars_mismatch")
    if record.get("truncated") is not False:
        errors.append(f"{prefix}_truncated")
    return text


def _preflight_review_sources(
    value: Any, errors: list[str],
) -> list[dict[str, Any]]:
    """在读取任何评审产物前限制 source 数量、形状与声明总字节。"""
    if not isinstance(value, list) or not value:
        errors.append("review_sources_invalid")
        return []
    if len(value) > MAX_REVIEW_SOURCES:
        errors.append("review_sources_too_many")
        return []
    records: list[dict[str, Any]] = []
    labels: set[str] = set()
    declared_total = 0
    for index, record in enumerate(value):
        if not isinstance(record, dict) or set(record) != _SOURCE_RECORD_KEYS:
            errors.append(f"review_source_{index}_fields_invalid")
            continue
        label = record.get("label")
        if type(label) is not str or re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", label) is None:
            errors.append(f"review_source_{index}_label_invalid")
            continue
        if label in labels:
            errors.append(f"review_source_{label}_duplicate")
            continue
        labels.add(label)
        if not _safe_review_rel(record.get("artifact")):
            errors.append(f"review_source_{label}_artifact_invalid")
            continue
        size = record.get("bytes")
        if type(size) is not int or size < 0:
            errors.append(f"review_source_{label}_bytes_invalid")
            continue
        if size > MAX_REVIEW_SOURCE_BYTES:
            errors.append(f"review_source_{label}_too_large")
            continue
        declared_total += size
        records.append(record)
    if declared_total > MAX_REVIEW_SOURCE_AGGREGATE_BYTES:
        errors.append("review_sources_declared_too_large")
    return records


def _downgrade_review(data: dict[str, Any], errors: list[str]) -> dict[str, Any]:
    data["review_reliable"] = False
    existing = data.get("reliability_reasons")
    reasons = [reason for reason in existing if type(reason) is str] if isinstance(existing, list) else []
    data["reliability_reasons"] = list(dict.fromkeys([*reasons, *errors]))
    return data


def _content_addressed_source_is_bound(record: dict[str, Any], label: str) -> bool:
    artifact = record.get("artifact")
    match = _CONTENT_ADDRESSED_SOURCE_RE.fullmatch(artifact) if isinstance(artifact, str) else None
    return bool(
        match
        and match.group("label") == label
        and record.get("sha256") == f"sha256:{match.group('digest')}"
    )


def paper_figures_review_text(figures: Any) -> str:
    """把 figures.json 投影为稳定、可内容寻址的评审事实文本。"""
    if not isinstance(figures, list) or any(not isinstance(item, dict) for item in figures):
        raise ValueError("paper figures must be a list of objects")
    projected = []
    for item in figures:
        ref = item.get("index") if item.get("index") is not None else item.get("id", "")
        caption = item.get("caption", "")
        filename = item.get("filename")
        if type(ref) not in {int, str} or not isinstance(caption, str):
            raise ValueError("paper figure fields are invalid")
        if filename is not None and not isinstance(filename, str):
            raise ValueError("paper figure filename is invalid")
        projected.append({
            "ref": ref, "caption": caption, "embeddable": bool(filename),
        })
    return json.dumps(
        projected, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )


async def _validate_pipeline_sources(
    pipeline: str | None,
    source_records: dict[str, dict],
    source_texts: dict[str, str],
    read_file: Callable[[str], Awaitable[bytes | None]],
    errors: list[str],
) -> None:
    """把 source label/path 绑定到真实 pipeline,任意 output 文件不能冒充送评来源。"""
    labels = set(source_records)
    if pipeline == "video":
        non_evidence = {label for label in labels if re.fullmatch(r"E[1-9]\d*", label) is None}
        if non_evidence != {"smart", "mechanical"}:
            errors.append("video_source_profile_mismatch")
        mechanical = source_records.get("mechanical")
        if not isinstance(mechanical, dict) or mechanical.get("artifact") != "output/notes_mechanical.md":
            errors.append("video_mechanical_source_invalid")
        return
    if pipeline == "audio":
        if labels != {"smart", "transcript"}:
            errors.append("audio_source_profile_mismatch")
        transcript = source_records.get("transcript")
        if not isinstance(transcript, dict) or not _content_addressed_source_is_bound(
            transcript, "transcript",
        ):
            errors.append("audio_transcript_source_invalid")
        return
    if pipeline not in {"article", "paper"}:
        return

    direct_paths = {
        "original": "output/original.md",
        "translated": "output/translated.md",
    }
    present_direct: set[str] = set()
    for label, rel in direct_paths.items():
        try:
            body = await read_file(rel)
        except (OSError, ValueError):
            body = None
        if body is not None:
            present_direct.add(label)
    paper_figures_present = False
    if pipeline == "paper":
        try:
            figures_raw = await read_file("intermediate/figures.json")
        except (OSError, ValueError):
            figures_raw = None
        if figures_raw is not None:
            paper_figures_present = True
            if not isinstance(figures_raw, bytes):
                errors.append("paper_figures_current_fact_invalid")
            elif len(figures_raw) > MAX_REVIEW_SOURCE_BYTES:
                errors.append("paper_figures_current_fact_too_large")
            else:
                try:
                    figures = json.loads(figures_raw.decode("utf-8"))
                    current_text = paper_figures_review_text(figures)
                except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
                    errors.append("paper_figures_current_fact_invalid")
                else:
                    if source_texts.get("figures") != current_text:
                        errors.append("paper_figures_source_mismatch")
            figures_record = source_records.get("figures")
            if not isinstance(figures_record, dict) or not _content_addressed_source_is_bound(
                figures_record, "figures",
            ):
                errors.append("paper_figures_source_invalid")

    expected = {"smart", *present_direct} if present_direct else {"smart", "sections"}
    if paper_figures_present:
        expected.add("figures")
    if labels != expected:
        errors.append(f"{pipeline}_source_profile_mismatch")
    for label in present_direct:
        record = source_records.get(label)
        if not isinstance(record, dict) or record.get("artifact") != direct_paths[label]:
            errors.append(f"{pipeline}_{label}_source_invalid")
    if not present_direct:
        sections = source_records.get("sections")
        if not isinstance(sections, dict) or not _content_addressed_source_is_bound(
            sections, "sections",
        ):
            errors.append(f"{pipeline}_sections_source_invalid")


async def verify_persisted_review(
    review: Any,
    *,
    job_id: str,
    pipeline: str | None,
    read_file: Callable[[str], Awaitable[bytes | None]],
) -> dict[str, Any]:
    """读时重算评审可靠性;自报 reliable 不能越过 schema/文件/locator/citation 门。"""
    data = dict(review) if isinstance(review, dict) else {}
    if type(data.get("schema_version")) is not int or data.get("schema_version") != REVIEW_SCHEMA_VERSION:
        data["review_reliable"] = False
        data["reliability_reasons"] = ["legacy_schema"]
        return data

    source_read_file = read_file
    snapshot: dict[str, Any] = {}
    failed_reads: set[str] = set()

    async def read_snapshot(rel: str) -> bytes | None:
        """一次重验只读取每个产物一次,后续消费者共享同一时点快照。"""
        if rel in failed_reads:
            raise OSError("review artifact snapshot read failed")
        if rel in snapshot:
            return snapshot[rel]
        try:
            value = await source_read_file(rel)
        except (OSError, ValueError):
            failed_reads.add(rel)
            raise
        snapshot[rel] = value
        return value

    read_file = read_snapshot
    errors: list[str] = []
    if not _is_json_value(data):
        errors.append("review_contains_non_json_value")
    expected_score_keys = _SCORE_KEYS_BY_PIPELINE.get(pipeline) if type(pipeline) is str else None
    if expected_score_keys is None:
        errors.append("review_pipeline_unknown")
    expected_top_fields = _REVIEW_FIXED_KEYS | set(expected_score_keys or ())
    if set(data) != expected_top_fields:
        errors.append("review_top_level_fields_invalid")
    score_keys = data.get("score_keys")
    keys_well_typed = (
        isinstance(score_keys, list)
        and bool(score_keys)
        and len(score_keys) <= 16
        and all(type(key) is str and re.fullmatch(r"[a-z][a-z0-9_]*", key) is not None
                for key in score_keys)
    )
    if not keys_well_typed or len(set(score_keys)) != len(score_keys):
        errors.append("score_keys_invalid")
        score_keys = []
    elif expected_score_keys is not None and tuple(score_keys) != expected_score_keys:
        errors.append("score_profile_mismatch")
        score_keys = []
    scores = _strict_scores(data, score_keys, errors)
    expected_overall = (
        round(sum(scores.values()) / len(score_keys), 1)
        if score_keys and len(scores) == len(score_keys) else None
    )
    overall = data.get("overall")
    if type(overall) not in {int, float} or overall != expected_overall:
        errors.append("overall_mismatch")
    normalized_terms = _strict_key_terms(data.get("key_terms"), errors)
    if not _same_json_value(normalized_terms, data.get("key_terms")):
        errors.append("key_terms_not_normalized")
    normalized_missing = _strict_string_list(
        data.get("missing_concepts"), "missing_concepts", errors,
    )
    if not _same_json_value(normalized_missing, data.get("missing_concepts")):
        errors.append("missing_concepts_not_normalized")
    top3 = _strict_string_list(data.get("top3_improvements"), "top3_improvements", errors)
    if len(top3) != 3:
        errors.append("top3_improvements must contain exactly 3 items")
    if not _same_json_value(top3, data.get("top3_improvements")):
        errors.append("top3_improvements_not_normalized")
    for field in ("provider", "model"):
        if type(data.get(field)) is not str or not data.get(field).strip():
            errors.append(f"{field}_invalid")
        elif data.get(field).strip().lower() == "unknown":
            errors.append(f"{field}_unknown")
    if (
        type(data.get("generated_at")) is not str
        or re.fullmatch(r"\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}", data.get("generated_at")) is None
    ):
        errors.append("generated_at_invalid")
    if "raw_response" in data:
        errors.append("raw_response_on_reliable_review")

    review_input = data.get("review_input")
    if not isinstance(review_input, dict):
        errors.append("review_input_invalid")
        review_input = {}
    review_input_keys = _RECORD_VALUE_KEYS | {"sources"}
    if pipeline == "video" and "evidence_manifest" in review_input:
        review_input_keys = review_input_keys | {"evidence_manifest"}
    source_preflight_errors: list[str] = []
    raw_sources = _preflight_review_sources(
        review_input.get("sources"), source_preflight_errors,
    )
    if source_preflight_errors:
        errors.extend(source_preflight_errors)
        return _downgrade_review(data, errors)
    prompt_error_count = len(errors)
    prompt_text = await _read_record(
        review_input, read_file, errors, "review_input", expected_keys=review_input_keys,
    )
    if prompt_text is None or len(errors) != prompt_error_count:
        return _downgrade_review(data, errors)
    source_texts: dict[str, str] = {}
    source_records: dict[str, dict] = {}
    actual_source_bytes = 0
    for record in raw_sources:
        label = record["label"]
        source_records[label] = record
        source_error_count = len(errors)
        text = await _read_record(record, read_file, errors, f"review_source_{label}")
        if text is not None:
            actual_source_bytes += len(text.encode("utf-8"))
            if actual_source_bytes > MAX_REVIEW_SOURCE_AGGREGATE_BYTES:
                errors.append("review_sources_actual_too_large")
                return _downgrade_review(data, errors)
        if text is None or len(errors) != source_error_count:
            if re.fullmatch(r"E[1-9]\d*", label):
                errors.append(f"evidence_source_record_mismatch:{label}")
            return _downgrade_review(data, errors)
        if text is not None:
            source_texts[label] = text
    if prompt_text is not None:
        for label, source_text in source_texts.items():
            if source_text not in prompt_text:
                errors.append(f"review_source_{label}_not_in_prompt")
    await _validate_pipeline_sources(
        pipeline, source_records, source_texts, read_file, errors,
    )
    smart_record = source_records.get("smart")
    note_file = data.get("note_file")
    note_file_valid = (
        type(note_file) is str
        and note_file.startswith("output/versions/notes_smart_")
        and note_file.endswith(".md")
        and _safe_review_rel(note_file)
    )
    if not note_file_valid:
        errors.append("note_file_invalid")
    if smart_record is None or note_file != smart_record.get("artifact"):
        errors.append("note_file_smart_source_mismatch")
    coverage = data.get("review_coverage")
    smart_text = source_texts.get("smart")
    expected_coverage = {
        "note_chars": len(smart_text or ""),
        "reviewed_chars": len(smart_text or ""),
        "truncated": False,
    }
    if smart_text is None or not _same_json_value(coverage, expected_coverage):
        errors.append("review_coverage_mismatch")
    normalized_issues = _strict_issues(
        data.get("issues"), score_keys, source_texts, errors, persisted=True,
    )
    if not _same_json_value(normalized_issues, data.get("issues")):
        errors.append("issue_locator_mismatch")

    completion = data.get("completion")
    _completion_is_strict(
        completion, errors,
        review_provider=data.get("provider"), review_model=data.get("model"),
    )
    parse = data.get("parse")
    if not _same_json_value(
        parse, {"mode": "strict", "schema_valid": True, "errors": []},
    ):
        errors.append("parse_not_strict")
    if review_input.get("truncated") is not False:
        errors.append("review_input_truncated")

    citation = data.get("citation_validation")
    is_video = pipeline == "video"
    if (
        not isinstance(citation, dict)
        or not isinstance(citation.get("status"), str)
        or citation.get("status") not in {"valid", "not_applicable"}
        or type(citation.get("checked")) is not int
        or not isinstance(citation.get("items"), list)
    ):
        errors.append("citation_not_valid")
    elif not is_video and not _same_json_value(
        citation, {"status": "not_applicable", "checked": 0, "items": []},
    ):
        errors.append("citation_unexpected_for_pipeline")
    if is_video:
        from .evidence_contract import (
            blocking_manifest_errors,
            validate_citations_with_reader,
            validate_manifest_with_reader,
        )

        try:
            evidence_data = await read_file("output/evidence.json")
        except (OSError, ValueError):
            evidence_data = None
            errors.append("evidence_manifest_unreadable")
        manifest = None
        manifest_present = evidence_data is not None
        if evidence_data is not None:
            try:
                manifest = json.loads(evidence_data)
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                errors.append("evidence_manifest_invalid")
            else:
                if not isinstance(manifest, dict):
                    errors.append("evidence_manifest_invalid")
            evidence_record = review_input.get("evidence_manifest")
            if isinstance(evidence_record, dict) and evidence_record.get("label") != "evidence_manifest":
                errors.append("evidence_manifest_label_invalid")
            await _read_record(evidence_record, read_file, errors, "evidence_manifest")
        elif review_input.get("evidence_manifest") is not None:
            errors.append("evidence_manifest_missing")
        valid_evidence: dict[str, dict] = {}
        if manifest_present:
            valid_evidence, manifest_errors = await validate_manifest_with_reader(
                job_id, manifest, read_file,
            )
            errors.extend(blocking_manifest_errors(manifest_errors))
            manifest_items = manifest.get("evidence") if isinstance(manifest, dict) else None
            claimed_eligible = {
                item.get("id")
                for item in (manifest_items if isinstance(manifest_items, list) else [])
                if isinstance(item, dict)
                and (item.get("eligible") is True or item.get("confidence") == "high")
                and isinstance(item.get("id"), str)
            }
            if claimed_eligible != set(valid_evidence):
                errors.append("evidence_manifest_trust_mismatch")
        expected_video_sources = {"smart", "mechanical", *valid_evidence}
        if set(source_records) != expected_video_sources:
            errors.append("evidence_sources_mismatch")
        for evidence_id, item in valid_evidence.items():
            record = source_records.get(evidence_id)
            if not isinstance(record, dict) or any(
                not _same_json_value(record.get(field), item.get(field))
                for field in ("artifact", "sha256", "bytes", "chars")
            ):
                errors.append(f"evidence_source_record_mismatch:{evidence_id}")
        recomputed = await validate_citations_with_reader(
            job_id, smart_text or "", manifest, read_file,
        )
        if not _same_json_value(citation, recomputed):
            errors.append("citation_revalidation_mismatch")

    if type(data.get("review_reliable")) is not bool or data.get("review_reliable") is not True:
        errors.append("stored_unreliable")
    if data.get("reliability_reasons") != []:
        errors.append("reliability_reasons_not_empty")
    if errors:
        return _downgrade_review(data, errors)
    return data


def project_review(review: Any) -> dict[str, Any]:
    """API 返回固定诊断 schema;不可靠内容不携带可点击产物定位。"""
    data = review if isinstance(review, dict) else {}
    schema_version = data.get("schema_version")
    is_v2 = type(schema_version) is int and schema_version == REVIEW_SCHEMA_VERSION
    reliable = is_v2 and data.get("review_reliable") is True
    state = "reliable" if reliable else ("unreliable" if is_v2 else "legacy_unverified")

    reasons = _project_string_list(data.get("reliability_reasons"))
    if not is_v2 and "legacy_schema" not in reasons:
        reasons.insert(0, "legacy_schema")
    score_keys = [
        key for key in _project_string_list(data.get("score_keys"))
        if key in _PROJECTED_SCORE_KEYS
    ] if reliable else []
    result: dict[str, Any] = {
        "schema_version": schema_version if type(schema_version) is int else None,
        "reliability_state": state,
        "review_reliable": reliable,
        "reliability_reasons": reasons,
        "score_keys": score_keys,
        "overall": _project_score(data.get("overall")) if reliable else None,
        "diagnostic_overall": None,
        "key_terms": _project_key_terms(data.get("key_terms")) if reliable else [],
        "missing_concepts": _project_string_list(data.get("missing_concepts")),
        "top3_improvements": _project_string_list(data.get("top3_improvements")),
        "issues": _project_issues(data.get("issues"), allow_locator=reliable),
        "review_input": _project_review_input(
            data.get("review_input"), allow_artifacts=reliable,
        ),
        "completion": _project_completion(data.get("completion")),
        "parse": _project_parse(data.get("parse")),
        "citation_validation": _project_citation(data.get("citation_validation")),
        "review_coverage": _project_coverage(data.get("review_coverage")),
        "note_file": (
            data.get("note_file")
            if reliable and _safe_review_rel(data.get("note_file")) else None
        ),
        "provider": _project_text(data.get("provider")),
        "model": _project_text(data.get("model")),
        "generated_at": _project_text(data.get("generated_at")),
    }
    for key in _PROJECTED_SCORE_KEYS:
        result[key] = (
            _project_score(data.get(key))
            if reliable and key in score_keys else None
        )
    return result


_PROJECTED_SCORE_KEYS = tuple(dict.fromkeys(
    key for keys in _SCORE_KEYS_BY_PIPELINE.values() for key in keys
))


def _project_text(value: Any) -> str | None:
    if type(value) is not str:
        return None
    value = value.strip()
    return value or None


def _project_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [text for item in value if (text := _project_text(item)) is not None]


def _project_score(value: Any) -> int | float | None:
    if type(value) not in {int, float} or not math.isfinite(value):
        return None
    return value if 1 <= value <= 5 else None


def _project_int(value: Any, *, minimum: int = 0) -> int | None:
    return value if type(value) is int and value >= minimum else None


def _project_key_terms(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    terms = []
    for item in value:
        if not isinstance(item, dict):
            continue
        term = _project_text(item.get("term"))
        definition = _project_text(item.get("definition"))
        if term is not None and definition is not None:
            terms.append({"term": term, "definition": definition})
    return terms


def _project_record(record: Any, *, allow_artifact: bool, include_label: bool) -> dict[str, Any]:
    item = record if isinstance(record, dict) else {}
    artifact = item.get("artifact")
    artifact = artifact if allow_artifact and _safe_review_rel(artifact) else None
    result = {
        "artifact": artifact,
        "sha256": _project_text(item.get("sha256")) if artifact is not None else None,
        "bytes": _project_int(item.get("bytes")) if artifact is not None else None,
        "chars": _project_int(item.get("chars")) if artifact is not None else None,
        "truncated": item.get("truncated") if type(item.get("truncated")) is bool else None,
    }
    if include_label:
        result["label"] = _project_text(item.get("label"))
    return result


def _project_review_input(value: Any, *, allow_artifacts: bool) -> dict[str, Any]:
    data = value if isinstance(value, dict) else {}
    result = _project_record(data, allow_artifact=allow_artifacts, include_label=False)
    raw_sources = data.get("sources")
    sources = raw_sources if isinstance(raw_sources, list) else []
    result["sources"] = [
        _project_record(item, allow_artifact=allow_artifacts, include_label=True)
        for item in sources if isinstance(item, dict)
    ]
    return result


def _project_issues(value: Any, *, allow_locator: bool) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    issues = []
    for raw in value:
        if not isinstance(raw, dict):
            continue
        locator = None
        candidate = raw.get("locator")
        if allow_locator and isinstance(candidate, dict):
            source = _project_text(candidate.get("source"))
            quote = _project_text(candidate.get("quote"))
            offset = _project_int(candidate.get("offset"))
            if source is not None and quote is not None and offset is not None:
                locator = {"source": source, "quote": quote, "offset": offset}
        evidence_status = _project_text(raw.get("evidence_status")) or "unverified"
        if not allow_locator or (evidence_status == "supported" and locator is None):
            evidence_status = "unverified"
        issues.append({
            "type": _project_text(raw.get("type")),
            "severity": _project_text(raw.get("severity")),
            "dimension": _project_text(raw.get("dimension")),
            "claim": _project_text(raw.get("claim")),
            "message": _project_text(raw.get("message")),
            "evidence_status": evidence_status,
            "reason": _project_text(raw.get("reason")),
            "locator": locator,
        })
    return issues


def _project_completion(value: Any) -> dict[str, Any]:
    data = value if isinstance(value, dict) else {}
    raw_attempts = data.get("attempts")
    attempts = raw_attempts if isinstance(raw_attempts, list) else []
    return {
        "status": _project_text(data.get("status")) or "unknown",
        "raw_finish_reason": _project_text(data.get("raw_finish_reason")),
        "tier_used": _project_text(data.get("tier_used")),
        "attempts": [{
            "tier": _project_text(item.get("tier")),
            "provider": _project_text(item.get("provider")),
            "model": _project_text(item.get("model")),
            "ok": item.get("ok") if type(item.get("ok")) is bool else None,
            "error_class": _project_text(item.get("error_class")),
            "error": _project_text(item.get("error")),
        } for item in attempts if isinstance(item, dict)],
    }


def _project_parse(value: Any) -> dict[str, Any]:
    data = value if isinstance(value, dict) else {}
    return {
        "mode": _project_text(data.get("mode")) or "unknown",
        "schema_valid": data.get("schema_valid") is True,
        "errors": _project_string_list(data.get("errors")),
    }


def _project_citation(value: Any) -> dict[str, Any]:
    data = value if isinstance(value, dict) else {}
    raw_items = data.get("items")
    items = raw_items if isinstance(raw_items, list) else []
    return {
        "status": _project_text(data.get("status")) or "unknown",
        "checked": _project_int(data.get("checked")) or 0,
        "items": [{
            "id": _project_text(item.get("id")),
            "offset": _project_int(item.get("offset")),
            "status": _project_text(item.get("status")) or "unknown",
            "errors": _project_string_list(item.get("errors")),
        } for item in items if isinstance(item, dict)],
        "manifest_errors": _project_string_list(data.get("manifest_errors")),
    }


def _project_coverage(value: Any) -> dict[str, Any]:
    data = value if isinstance(value, dict) else {}
    return {
        "note_chars": _project_int(data.get("note_chars")),
        "reviewed_chars": _project_int(data.get("reviewed_chars")),
        "truncated": data.get("truncated") if type(data.get("truncated")) is bool else None,
    }
