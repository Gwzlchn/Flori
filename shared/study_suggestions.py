"""学习建议的指纹,输出校验和冲突语义."""

from __future__ import annotations

import hashlib
import base64
import json
import os
import re
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .prompt_resolver import PromptResolver, ResolvedPrompt
from .study import MAX_SQLITE_INTEGER


SUGGESTION_CARD_TYPES = frozenset({"basic", "cloze", "qa"})
SUGGESTION_ACTIONS = frozenset({"edit", "accept", "reject"})
MAX_BATCH_ITEMS = 100
MAX_GENERATED_CARDS = 50
MAX_EVIDENCE_PER_SUGGESTION = 8
MAX_FRONT_LENGTH = 20_000
MAX_BACK_LENGTH = 100_000
MAX_EXPLANATION_LENGTH = 100_000
MAX_QUOTE_LENGTH = 20_000
MAX_KNOWLEDGE_KEY_LENGTH = 512
StudySuggestionFaultInjector = Callable[[str], None]


class StudySuggestionNotFoundError(LookupError):
    """学习建议批次,候选或关联输入不存在."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class StudySuggestionConflictError(RuntimeError):
    """学习建议的状态,revision,幂等键或证据冲突."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def prefixed_sha256(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def study_suggestion_prompt_snapshot(resolved: ResolvedPrompt) -> dict[str, object]:
    """把解析所得原始字节和来源元数据固化为可持久 JSON."""
    snapshot: dict[str, object] = {
        "name": resolved.name,
        "content_b64": base64.b64encode(resolved.raw).decode("ascii"),
        "bytes": len(resolved.raw),
        "sha256": resolved.sha256,
        "source": resolved.source,
        "version": resolved.version,
    }
    validate_study_suggestion_prompt_snapshot(snapshot)
    return snapshot


def resolve_study_suggestion_prompt(
    *, hot_dir: Path | None = None, image_dir: Path | None = None,
) -> dict[str, object]:
    """创建批次时解析一次 Prompt;重试和执行只消费持久快照."""
    data_dir = Path(os.environ.get("DATA_DIR", "/data"))
    config_dir = Path(os.environ.get("CONFIG_DIR", "/app/configs"))
    resolver = PromptResolver(
        hot_dir=hot_dir or data_dir / "prompts" / "templates",
        image_dir=image_dir or config_dir / "prompts" / "templates",
    )
    return study_suggestion_prompt_snapshot(
        resolver.resolve(
            "study_suggestions",
            step_name="study_suggestions",
            primary_template="study_suggestions",
        )
    )


def validate_study_suggestion_prompt_snapshot(value: object) -> bytes:
    if not isinstance(value, Mapping) or set(value) != {
        "name", "content_b64", "bytes", "sha256", "source", "version",
    }:
        raise ValueError("prompt_snapshot 字段集不匹配")
    if value.get("name") != "study_suggestions":
        raise ValueError("prompt_snapshot name 不匹配")
    encoded = value.get("content_b64")
    if not isinstance(encoded, str) or not encoded:
        raise ValueError("prompt_snapshot content_b64 非法")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise ValueError("prompt_snapshot content_b64 非法") from exc
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("prompt_snapshot 不是 UTF-8") from exc
    if not raw or type(value.get("bytes")) is not int or value["bytes"] != len(raw):
        raise ValueError("prompt_snapshot bytes 不匹配")
    if value.get("sha256") != prefixed_sha256(raw):
        raise ValueError("prompt_snapshot sha256 不匹配")
    if value.get("source") not in {"override", "hot", "image"}:
        raise ValueError("prompt_snapshot source 非法")
    version = value.get("version")
    if version is not None and (type(version) is not int or not 1 <= version < (1 << 63)):
        raise ValueError("prompt_snapshot version 非法")
    return raw


def study_suggestion_generator_fingerprint(snapshot: Mapping[str, object]) -> str:
    validate_study_suggestion_prompt_snapshot(snapshot)
    payload = {
        "name": "study-suggestions",
        "schema_version": 1,
        "parser_version": 1,
        "prompt_sha256": snapshot["sha256"],
    }
    return prefixed_sha256(canonical_json(payload).encode("utf-8"))


def validate_study_suggestion_task_payload(value: Mapping[str, object]) -> None:
    """校验跨 Redis/Worker 的批次身份和 canonical payload 指纹."""
    if value.get("kind") != "ai" or value.get("step") != "study_suggestions":
        raise ValueError("study suggestion task kind/step 非法")
    for field in ("task_id", "batch_id", "generator_fingerprint", "input_fingerprint"):
        require_identifier(value.get(field), field)
    require_revision(value.get("attempt"), "attempt")
    require_revision(value.get("revision"), "revision")
    prompt = value.get("prompt_snapshot")
    if not isinstance(prompt, Mapping):
        raise ValueError("study suggestion task 缺少 prompt_snapshot")
    if value.get("generator_fingerprint") != study_suggestion_generator_fingerprint(prompt):
        raise ValueError("study suggestion task generator_fingerprint 不匹配")
    recorded = value.get("task_payload_sha256")
    unsigned = dict(value)
    unsigned.pop("task_payload_sha256", None)
    for runtime_field in (
        "state", "claim_id", "worker_id", "lease_until", "lease_seconds",
        "score", "exec_id", "requeue_count",
    ):
        unsigned.pop(runtime_field, None)
    if recorded != prefixed_sha256(canonical_json(unsigned).encode("utf-8")):
        raise ValueError("study suggestion task payload hash 不匹配")


def payload_fingerprint(value: object) -> str:
    return sha256_text(canonical_json(value))


def normalized_fingerprint_text(value: str) -> str:
    """指纹归一化只折叠 Unicode,大小写和空白,不做语义改写."""
    normalized = unicodedata.normalize("NFKC", value).casefold().strip()
    return re.sub(r"\s+", " ", normalized)


def knowledge_fingerprint(domain: str, knowledge_key: str) -> str:
    return payload_fingerprint(
        {
            "domain": normalized_fingerprint_text(domain),
            "knowledge_key": normalized_fingerprint_text(knowledge_key),
        }
    )


def content_fingerprint(
    *,
    domain: str,
    card_type: str,
    front: str,
    back: str,
    explanation: str,
) -> str:
    return payload_fingerprint(
        {
            "domain": normalized_fingerprint_text(domain),
            "card_type": card_type,
            "front": normalized_fingerprint_text(front),
            "back": normalized_fingerprint_text(back),
            "explanation": normalized_fingerprint_text(explanation),
        }
    )


def require_identifier(value: object, field: str, *, max_length: int = 256) -> str:
    normalized = value.strip() if isinstance(value, str) else ""
    if not normalized or len(normalized) > max_length:
        raise ValueError(f"{field} 必须是 1..{max_length} 字符的非空字符串")
    return normalized


def require_request_id(value: object) -> str:
    return require_identifier(value, "request_id", max_length=128)


INTERNAL_REQUEST_ID_PREFIXES = (
    "study-lifecycle:",
    "identity-transition:",
)


def require_external_request_id(value: object) -> str:
    """校验客户端幂等键,保留内部状态账本的命名空间."""
    normalized = require_request_id(value)
    if normalized.startswith(INTERNAL_REQUEST_ID_PREFIXES):
        raise ValueError("request_id 使用了内部保留前缀")
    return normalized


def require_revision(value: object, field: str = "expected_revision") -> int:
    if type(value) is not int or not 1 <= value <= MAX_SQLITE_INTEGER:
        raise ValueError(f"{field} 必须是 SQLite 64 位正整数")
    return value


def require_plain_int(
    value: object,
    field: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{field} 必须是 {minimum}..{maximum} 的整数")
    return value


def validate_card_content(
    *,
    card_type: object,
    front: object,
    back: object,
    explanation: object = "",
) -> tuple[str, str, str, str]:
    if not isinstance(card_type, str) or card_type not in SUGGESTION_CARD_TYPES:
        raise ValueError("card_type 必须是 basic/cloze/qa")
    if not isinstance(front, str) or not front.strip() or len(front) > MAX_FRONT_LENGTH:
        raise ValueError(f"front 必须是 1..{MAX_FRONT_LENGTH} 字符")
    if not isinstance(back, str) or not back.strip() or len(back) > MAX_BACK_LENGTH:
        raise ValueError(f"back 必须是 1..{MAX_BACK_LENGTH} 字符")
    if not isinstance(explanation, str) or len(explanation) > MAX_EXPLANATION_LENGTH:
        raise ValueError(f"explanation 不得超过 {MAX_EXPLANATION_LENGTH} 字符")
    return card_type, front.strip(), back.strip(), explanation.strip()


def validate_operation_items(items: object) -> list[dict[str, Any]]:
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_BATCH_ITEMS:
        raise ValueError(f"items 必须包含 1..{MAX_BATCH_ITEMS} 项")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(items):
        if not isinstance(raw, Mapping):
            raise ValueError(f"items[{index}] 必须是对象")
        if any(not isinstance(key, str) for key in raw):
            raise ValueError(f"items[{index}] 字段名必须是字符串")
        allowed = {"suggestion_id", "expected_revision", "action", "patch", "reason"}
        extras = set(raw) - allowed
        if extras:
            raise ValueError(f"items[{index}] 包含未知字段: {sorted(extras)}")
        suggestion_id = require_identifier(raw.get("suggestion_id"), "suggestion_id")
        if suggestion_id in seen:
            raise ValueError("items 中 suggestion_id 不得重复")
        seen.add(suggestion_id)
        revision = require_revision(raw.get("expected_revision"))
        action = raw.get("action")
        if not isinstance(action, str) or action not in SUGGESTION_ACTIONS:
            raise ValueError("action 必须是 edit/accept/reject")
        raw_patch = raw.get("patch", {})
        if not isinstance(raw_patch, Mapping):
            raise ValueError("patch 必须是对象")
        if any(not isinstance(key, str) for key in raw_patch):
            raise ValueError("patch 字段名必须是字符串")
        patch = dict(raw_patch)
        patch_extras = set(patch) - {
            "card_type",
            "front",
            "back",
            "explanation",
            "concept_term",
        }
        if patch_extras:
            raise ValueError(f"patch 包含不可编辑字段: {sorted(patch_extras)}")
        if action == "edit" and not patch:
            raise ValueError("edit 必须提供非空 patch")
        if action == "reject" and patch:
            raise ValueError("reject 不接受 patch")
        reason = raw.get("reason")
        if reason is not None and (
            not isinstance(reason, str) or len(reason.strip()) > 2_000
        ):
            raise ValueError("reason 必须是不超过 2000 字符的字符串")
        if action == "reject" and (
            not isinstance(reason, str) or not reason.strip()
        ):
            raise ValueError("reject 必须提供非空 reason")
        if action != "reject" and reason is not None:
            raise ValueError("edit/accept 不接受 reason")
        normalized.append(
            {
                "suggestion_id": suggestion_id,
                "expected_revision": revision,
                "action": action,
                "patch": patch,
                "reason": reason.strip() if isinstance(reason, str) else None,
            }
        )
    return normalized


def parse_ai_suggestions(
    raw: object,
    *,
    max_cards: int,
    evidence_ids: set[str],
    concept_input_ids: set[str],
) -> list[dict[str, Any]]:
    """严格解析 AI 候选.任一行失效都拒绝整批."""
    if not isinstance(raw, Mapping):
        raise ValueError("AI 输出根必须是对象")
    if any(not isinstance(key, str) for key in raw):
        raise ValueError("AI 输出根字段名必须是字符串")
    if set(raw) != {"schema_version", "suggestions"}:
        raise ValueError("AI 输出根对象必须且只能包含 schema_version/suggestions")
    if type(raw.get("schema_version")) is not int or raw.get("schema_version") != 1:
        raise ValueError("AI 输出 schema_version 必须为 1")
    suggestions = raw.get("suggestions")
    if not isinstance(suggestions, list) or not 1 <= len(suggestions) <= max_cards:
        raise ValueError(f"AI suggestions 必须包含 1..{max_cards} 项")
    parsed: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    for index, item in enumerate(suggestions):
        if not isinstance(item, Mapping):
            raise ValueError(f"suggestions[{index}] 必须是对象")
        if any(not isinstance(key, str) for key in item):
            raise ValueError(f"suggestions[{index}] 字段名必须是字符串")
        expected = {
            "knowledge_key",
            "concept_input_id",
            "card_type",
            "front",
            "back",
            "explanation",
            "evidence",
        }
        if set(item) != expected:
            raise ValueError(f"suggestions[{index}] 字段集不匹配")
        knowledge_key = require_identifier(
            item.get("knowledge_key"),
            f"suggestions[{index}].knowledge_key",
            max_length=MAX_KNOWLEDGE_KEY_LENGTH,
        )
        normalized_key = normalized_fingerprint_text(knowledge_key)
        if normalized_key in seen_keys:
            raise ValueError("AI 输出 knowledge_key 重复")
        seen_keys.add(normalized_key)
        concept_input_id = item.get("concept_input_id")
        if concept_input_id is not None:
            concept_input_id = require_identifier(
                concept_input_id, f"suggestions[{index}].concept_input_id"
            )
            if concept_input_id not in concept_input_ids:
                raise ValueError("AI 输出引用了未知 concept_input_id")
        card_type, front, back, explanation = validate_card_content(
            card_type=item.get("card_type"),
            front=item.get("front"),
            back=item.get("back"),
            explanation=item.get("explanation"),
        )
        refs = item.get("evidence")
        if not isinstance(refs, list) or not 1 <= len(refs) <= MAX_EVIDENCE_PER_SUGGESTION:
            raise ValueError(
                f"suggestions[{index}].evidence 必须包含 1..{MAX_EVIDENCE_PER_SUGGESTION} 项"
            )
        normalized_refs: list[dict[str, str]] = []
        seen_evidence: set[str] = set()
        for ref_index, ref in enumerate(refs):
            if not isinstance(ref, Mapping):
                raise ValueError("evidence 引用必须是对象")
            if any(not isinstance(key, str) for key in ref):
                raise ValueError("evidence 引用字段名必须是字符串")
            if set(ref) != {"evidence_id", "quote"}:
                raise ValueError("evidence 引用必须且只能包含 evidence_id/quote")
            evidence_id = require_identifier(
                ref.get("evidence_id"),
                f"suggestions[{index}].evidence[{ref_index}].evidence_id",
            )
            if evidence_id not in evidence_ids:
                raise ValueError("AI 输出引用了未知 evidence_id")
            if evidence_id in seen_evidence:
                raise ValueError("同一候选不得重复引用 evidence_id")
            seen_evidence.add(evidence_id)
            quote = ref.get("quote")
            if (
                not isinstance(quote, str)
                or not quote.strip()
                or len(quote) > MAX_QUOTE_LENGTH
            ):
                raise ValueError(f"quote 必须是 1..{MAX_QUOTE_LENGTH} 字符")
            normalized_refs.append({"evidence_id": evidence_id, "quote": quote})
        parsed.append(
            {
                "knowledge_key": knowledge_key,
                "concept_input_id": concept_input_id,
                "card_type": card_type,
                "front": front,
                "back": back,
                "explanation": explanation,
                "evidence": normalized_refs,
            }
        )
    return parsed


def operation_payload(
    *,
    request_id: str,
    batch_id: str,
    items: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    return {
        "request_id": request_id,
        "batch_id": batch_id,
        "items": [dict(item) for item in items],
    }
