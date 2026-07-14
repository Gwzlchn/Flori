"""投影概念的可复算来源佐证。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any

from api.services.evidence import resolve_canonical_evidence_batch
from shared.ai_gateway import AIGateway
from shared.config import AppConfig
from shared.db import (
    ConceptConflictError,
    ConceptNotFoundError,
    Database,
)
from shared.models import LLMRequest
from shared.prompt_resolver import PromptResolver
from shared.review_contract import verify_persisted_review
from shared.storage import (
    StorageBackend,
    read_verification_artifact_bounded,
)
from shared.structured_output import StructuredOutputParser


_SYNTHESIS_STEP = "concept_resynthesis"
_SYNTHESIS_ROUTE_STEP = "05_concepts"
_MAX_DEFINITION_CHARS = 100_000
_MAX_EVIDENCE_EXCERPT_BYTES = 8 * 1024
_MAX_SYNTHESIS_EVIDENCE = 50
_MAX_SYNTHESIS_INPUT_BYTES = 128 * 1024
_CONCEPT_DETAIL_OCCURRENCE_LIMIT = 100
_CONCEPT_DEFINITION_HISTORY_LIMIT = 100


class ConceptSynthesisConfigError(ValueError):
    """概念综合缺少受控 Prompt 或 AI route。"""


class ConceptSynthesisParseError(ValueError):
    """AI 返回不能形成唯一非空定义。"""


def _source_set_fingerprint(evidence_ids: list[str]) -> str:
    payload = json.dumps(
        sorted(set(evidence_ids)),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def concept_definition_projection(row: dict[str, Any]) -> dict[str, Any]:
    """定义历史去掉 DB 操作结果字段，并序列化时间。"""
    created_at = row.get("created_at")
    return {
        "definition_version_id": row["definition_version_id"],
        "domain": row["domain"],
        "term": row["term"],
        "version": row["version"],
        "definition": row.get("definition") or "",
        "source_evidence_ids": row.get("source_evidence_ids") or [],
        "source_set_fingerprint": row["source_set_fingerprint"],
        "strategy": row["strategy"],
        "provider": row.get("provider"),
        "model": row.get("model"),
        "prompt_hash": row.get("prompt_hash"),
        "input_hash": row.get("input_hash"),
        "supersedes_version_id": row.get("supersedes_version_id"),
        "actor": row["actor"],
        "created_at": (
            created_at.isoformat()
            if hasattr(created_at, "isoformat")
            else str(created_at)
        ),
    }


def _attestation_level(
    *, jobs: int, source_fingerprints: int, content_types: int,
) -> str:
    """三类独立性同时满足才升级，防止单 job 多段或同源副本冒充互证。"""
    if jobs == 0 or source_fingerprints == 0:
        return "none"
    if jobs >= 3 and source_fingerprints >= 3 and content_types >= 3:
        return "strong"
    if jobs >= 2 and source_fingerprints >= 2 and content_types >= 2:
        return "corroborated"
    return "supported"


def _concept_ai_route(config: AppConfig) -> dict[str, Any]:
    """概念综合复用 article 概念步的受控 provider/model route。"""
    pipeline = config.pipelines.get("article")
    steps = pipeline.get("steps") if isinstance(pipeline, dict) else None
    for step in steps if isinstance(steps, list) else []:
        if isinstance(step, dict) and step.get("name") == _SYNTHESIS_ROUTE_STEP:
            route = step.get("ai")
            if isinstance(route, dict) and route:
                return route
    raise ConceptSynthesisConfigError("concept synthesis AI route missing")


def _concept_prompt_resolver(config: AppConfig) -> PromptResolver:
    return PromptResolver(
        hot_dir=config.prompts_dir / "templates",
        image_dir=config.config_dir / "prompts" / "templates",
    )


def _definition_from_response(content: str) -> str:
    try:
        value = json.loads(StructuredOutputParser.extract_json(content))
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ConceptSynthesisParseError("concept synthesis response is not JSON") from exc
    if not isinstance(value, dict) or set(value) != {"definition"}:
        raise ConceptSynthesisParseError("concept synthesis response schema mismatch")
    definition = value.get("definition")
    if (
        not isinstance(definition, str)
        or not definition.strip()
        or len(definition) > _MAX_DEFINITION_CHARS
    ):
        raise ConceptSynthesisParseError("concept synthesis definition is invalid")
    return definition.strip()


def _synthesis_input_json(
    *,
    domain: str,
    term: str,
    current_definition: str,
    attestation: dict[str, Any],
) -> str:
    evidence = [
        {
            key: item.get(key)
            for key in (
                "evidence_id", "job_id", "content_type", "source_fingerprint",
                "note_type", "section", "excerpt", "locator",
            )
        }
        for item in attestation["included"]
    ]
    return json.dumps(
        {
            "schema_version": 1,
            "domain": domain,
            "term": term,
            "previous_definition": current_definition,
            "source_set_fingerprint": attestation["source_set_fingerprint"],
            "evidence": evidence,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


async def _review_reliability(
    db: Database,
    storage: StorageBackend,
    job_id: str,
) -> tuple[bool, str | None, tuple[str, str] | None]:
    job = await asyncio.to_thread(db.get_job, job_id)
    if job is None:
        return False, "job_missing", None
    try:
        raw = await read_verification_artifact_bounded(
            storage, job_id, "output/review.json",
        )
    except asyncio.CancelledError:
        raise
    except (OSError, ValueError):
        return False, "review_unreadable", None
    if raw is None:
        return False, "review_missing", None
    try:
        review = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return False, "review_invalid", None
    if not isinstance(review, dict):
        return False, "review_invalid", None

    async def read_file(rel_path: str) -> bytes | None:
        return await read_verification_artifact_bounded(
            storage, job_id, rel_path,
        )

    try:
        verified = await verify_persisted_review(
            review,
            job_id=job_id,
            pipeline=job.pipeline,
            read_file=read_file,
        )
    except asyncio.CancelledError:
        raise
    except (OSError, ValueError, TypeError):
        return False, "review_validation_failed", None
    if verified.get("review_reliable") is True:
        review_input = verified.get("review_input")
        sources = review_input.get("sources") if isinstance(review_input, dict) else None
        smart_sources = [
            source for source in (sources if isinstance(sources, list) else [])
            if isinstance(source, dict) and source.get("label") == "smart"
        ]
        if len(smart_sources) != 1:
            return False, "review_smart_source_missing", None
        smart = smart_sources[0]
        artifact, digest = smart.get("artifact"), smart.get("sha256")
        if not isinstance(artifact, str) or not isinstance(digest, str):
            return False, "review_smart_source_invalid", None
        return True, None, (artifact, digest)
    reasons = verified.get("reliability_reasons")
    if isinstance(reasons, list):
        reason = next(
            (item for item in reasons if isinstance(item, str) and item),
            None,
        )
        if reason:
            return False, f"review_unreliable:{reason}", None
    return False, "review_unreliable", None


async def project_concept_attestation(
    db: Database,
    storage: StorageBackend,
    domain: str,
    term: str,
) -> dict[str, Any]:
    """重验正规化 occurrence，并只让可靠评审中的有效证据进入佐证集。"""
    occurrences = await asyncio.to_thread(
        db.list_concept_occurrences,
        domain,
        term,
        include_invalid=True,
    )
    by_evidence: dict[str, dict[str, Any]] = {}
    for occurrence in occurrences:
        evidence_id = occurrence.get("evidence_id")
        job_id = occurrence.get("job_id")
        if isinstance(evidence_id, str) and isinstance(job_id, str):
            by_evidence.setdefault(evidence_id, occurrence)

    evidence_ids = sorted(by_evidence)
    resolved: dict[str, dict[str, Any]] = {}
    database_states: dict[str, dict[str, Any]] = {}
    for start in range(0, len(evidence_ids), 100):
        batch = evidence_ids[start:start + 100]
        database_states.update(await asyncio.to_thread(
            db.canonical_evidence_database_states, batch,
        ))
        try:
            projections = await resolve_canonical_evidence_batch(
                db, storage, batch,
            )
        except asyncio.CancelledError:
            raise
        except (OSError, ValueError, TypeError):
            projections = []
        for projection in projections:
            evidence_id = projection.get("evidence_id")
            if isinstance(evidence_id, str) and evidence_id in by_evidence:
                resolved[evidence_id] = projection

    review_cache: dict[
        str, tuple[bool, str | None, tuple[str, str] | None]
    ] = {}
    included: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for evidence_id in evidence_ids:
        occurrence = by_evidence[evidence_id]
        job_id = str(occurrence["job_id"])
        projection = resolved.get(evidence_id)
        status = projection.get("status") if projection else "missing"
        reason = projection.get("reason") if projection else "evidence_validation_failed"
        if projection is not None and projection.get("job_id") != job_id:
            status, reason = "stale", "evidence_job_mismatch"

        if status == "valid":
            if job_id not in review_cache:
                review_cache[job_id] = await _review_reliability(
                    db, storage, job_id,
                )
            reliable, review_reason, smart_source = review_cache[job_id]
            if not reliable:
                reason = review_reason
            else:
                identity = database_states.get(evidence_id)
                if identity is None:
                    reason = "evidence_identity_missing"
                elif smart_source != (
                    identity.get("note_path"),
                    f"sha256:{identity.get('note_sha256')}",
                ):
                    reason = "review_note_mismatch"
                else:
                    excerpt = occurrence.get("evidence_excerpt")
                    excerpt_hash = occurrence.get("chunk_body_sha256")
                    if not isinstance(excerpt, str) or not excerpt.strip():
                        reason = "evidence_excerpt_missing"
                    elif len(excerpt.encode("utf-8")) > _MAX_EVIDENCE_EXCERPT_BYTES:
                        reason = "evidence_excerpt_too_large"
                    elif (
                        not isinstance(excerpt_hash, str)
                        or hashlib.sha256(excerpt.encode("utf-8")).hexdigest()
                        != excerpt_hash
                    ):
                        reason = "evidence_excerpt_mismatch"

        base = {
            "evidence_id": evidence_id,
            "job_id": job_id,
            "content_type": occurrence.get("content_type") or "",
            "source_fingerprint": (
                projection.get("source_fingerprint")
                if projection is not None else occurrence.get("source_fingerprint")
            ),
        }
        if status == "valid" and reason is None:
            included.append({
                **base,
                "note_type": projection.get("note_type"),
                "chunk_id": projection.get("chunk_id"),
                "section": projection.get("section"),
                "excerpt": occurrence["evidence_excerpt"],
                "locator": projection.get("locator"),
                "link": projection.get("link"),
            })
        else:
            excluded.append({
                **base,
                "reason": reason or f"evidence_{status}",
                "locator": None,
                "link": None,
            })

    included.sort(key=lambda item: (item["evidence_id"], item["job_id"]))
    excluded.sort(key=lambda item: (item["evidence_id"], item["job_id"]))
    included_ids = [item["evidence_id"] for item in included]
    jobs = {item["job_id"] for item in included}
    source_fingerprints = {
        item["source_fingerprint"]
        for item in included
        if isinstance(item.get("source_fingerprint"), str)
        and item["source_fingerprint"]
    }
    content_types = {
        item["content_type"] for item in included if item["content_type"]
    }
    return {
        "domain": domain,
        "term": term,
        "level": _attestation_level(
            jobs=len(jobs),
            source_fingerprints=len(source_fingerprints),
            content_types=len(content_types),
        ),
        "evidence_count": len(set(included_ids)),
        "job_count": len(jobs),
        "source_fingerprint_count": len(source_fingerprints),
        "content_type_count": len(content_types),
        "source_set_fingerprint": _source_set_fingerprint(included_ids),
        "included": included,
        "excluded": excluded,
    }


async def project_concept_detail(
    db: Database,
    storage: StorageBackend,
    domain: str,
    term: str,
) -> dict[str, Any] | None:
    """REST 与 MCP 共用的概念详情，佐证始终现场重验。"""
    row = await asyncio.to_thread(db.get_glossary_term, domain, term)
    if row is None:
        return None
    all_occurrences = row.get("occurrences") or []
    occurrence_total = len(all_occurrences)
    occurrences = all_occurrences[:_CONCEPT_DETAIL_OCCURRENCE_LIMIT]
    titles = await asyncio.to_thread(
        db.get_job_titles,
        [
            item.get("job_id")
            for item in occurrences
            if isinstance(item, dict) and isinstance(item.get("job_id"), str)
        ],
    )
    row["occurrences"] = [
        {**item, "title": titles.get(item.get("job_id"))}
        for item in occurrences
        if isinstance(item, dict)
    ]
    current = await asyncio.to_thread(
        db.current_concept_definition, domain, term,
    )
    if current is None:
        raise ConceptConflictError("concept current version 不存在")
    history = await asyncio.to_thread(
        db.list_concept_definition_versions,
        domain,
        term,
        limit=_CONCEPT_DEFINITION_HISTORY_LIMIT,
    )
    history_total = await asyncio.to_thread(
        db.count_concept_definition_versions, domain, term,
    )
    attestation = await project_concept_attestation(db, storage, domain, term)
    created_at = row.get("created_at")
    updated_at = row.get("updated_at")
    return {
        "domain": row["domain"],
        "term": row["term"],
        "definition": row.get("definition") or "",
        "zh_name": row.get("zh_name") or "",
        "aliases": row.get("aliases") or [],
        "occurrences": row["occurrences"],
        "occurrence_total": occurrence_total,
        "occurrence_limit": _CONCEPT_DETAIL_OCCURRENCE_LIMIT,
        "related": row.get("related") or [],
        "status": row.get("status") or "accepted",
        "watched": bool(row.get("watched")),
        "is_topic": bool(row.get("is_topic")),
        "definition_locked": bool(row.get("definition_locked")),
        "current_definition_version_id": row.get("current_definition_version_id"),
        "lock_revision": int(row.get("lock_revision") or 0),
        "created_at": (
            created_at.isoformat()
            if hasattr(created_at, "isoformat") else (created_at or None)
        ),
        "updated_at": (
            updated_at.isoformat()
            if hasattr(updated_at, "isoformat") else (updated_at or None)
        ),
        "current_definition": concept_definition_projection(current),
        "definition_history": [concept_definition_projection(item) for item in history],
        "definition_history_total": history_total,
        "definition_history_limit": _CONCEPT_DEFINITION_HISTORY_LIMIT,
        "attestation": attestation,
    }


async def maybe_resynthesize_concept(
    db: Database,
    storage: StorageBackend,
    config: AppConfig,
    domain: str,
    term: str,
    *,
    expected_current_version_id: str,
    expected_lock_revision: int,
    actor: str,
    strategy: str,
    gateway: Any | None = None,
    prompt_resolver: PromptResolver | None = None,
) -> dict[str, Any]:
    """满足独立来源与新 source set 时综合，并用 current+lock revision 原子切换。"""
    row = await asyncio.to_thread(db.get_glossary_term, domain, term)
    if row is None:
        raise ConceptNotFoundError(f"concept not found: {domain}/{term}")
    if (
        row.get("current_definition_version_id") != expected_current_version_id
        or row.get("lock_revision") != expected_lock_revision
    ):
        raise ConceptConflictError("concept current version 或 lock revision 已变化")
    if row.get("status") == "rejected":
        raise ConceptConflictError("rejected concept 不接受定义版本")
    current = await asyncio.to_thread(
        db.current_concept_definition, domain, term,
    )
    if (
        current is None
        or current.get("definition_version_id") != expected_current_version_id
    ):
        raise ConceptConflictError("concept current version 或 lock revision 已变化")
    if row.get("definition_locked"):
        return {
            "created": False,
            "reason": "locked",
            "current": current,
            "attestation": None,
        }

    attestation = await project_concept_attestation(
        db, storage, domain, term,
    )
    if (
        attestation["job_count"] < 2
        or attestation["source_fingerprint_count"] < 2
    ):
        return {
            "created": False,
            "reason": "no_quorum",
            "current": current,
            "attestation": attestation,
        }
    if current.get("source_set_fingerprint") == attestation["source_set_fingerprint"]:
        return {
            "created": False,
            "reason": "source_set_unchanged",
            "current": current,
            "attestation": attestation,
        }
    if len(attestation["included"]) > _MAX_SYNTHESIS_EVIDENCE:
        return {
            "created": False,
            "reason": "input_too_large",
            "current": current,
            "attestation": attestation,
        }

    resolver = prompt_resolver or _concept_prompt_resolver(config)
    resolved_prompt = resolver.resolve(
        _SYNTHESIS_STEP,
        step_name=_SYNTHESIS_STEP,
        primary_template=_SYNTHESIS_STEP,
    )
    input_json = _synthesis_input_json(
        domain=domain,
        term=term,
        current_definition=current.get("definition") or "",
        attestation=attestation,
    )
    if len(input_json.encode("utf-8")) > _MAX_SYNTHESIS_INPUT_BYTES:
        return {
            "created": False,
            "reason": "input_too_large",
            "current": current,
            "attestation": attestation,
        }
    request = LLMRequest(
        messages=[{
            "role": "user",
            "content": f"{resolved_prompt.text}\n\n输入(JSON):\n{input_json}",
        }],
        max_tokens=2048,
        temperature=0,
        response_format="json",
    )
    ai_gateway = gateway or AIGateway(
        config.providers,
        {"steps": [{"name": _SYNTHESIS_STEP, "ai": _concept_ai_route(config)}]},
    )
    response = await ai_gateway.call(_SYNTHESIS_STEP, request)
    input_hash = hashlib.sha256(input_json.encode("utf-8")).hexdigest()
    refreshed_attestation = await project_concept_attestation(
        db, storage, domain, term,
    )
    refreshed_input = _synthesis_input_json(
        domain=domain,
        term=term,
        current_definition=current.get("definition") or "",
        attestation=refreshed_attestation,
    )
    original_ids = [item["evidence_id"] for item in attestation["included"]]
    refreshed_ids = [
        item["evidence_id"] for item in refreshed_attestation["included"]
    ]
    if (
        refreshed_ids != original_ids
        or refreshed_attestation["source_set_fingerprint"]
        != attestation["source_set_fingerprint"]
        or hashlib.sha256(refreshed_input.encode("utf-8")).hexdigest()
        != input_hash
    ):
        raise ConceptConflictError("concept evidence changed during synthesis")
    definition = _definition_from_response(response.content)
    prompt_hash = resolved_prompt.sha256.removeprefix("sha256:")
    version = await asyncio.to_thread(
        db.append_concept_definition_version,
        domain=domain,
        term=term,
        definition=definition,
        evidence_ids=[item["evidence_id"] for item in attestation["included"]],
        strategy=strategy,
        actor=actor,
        expected_current_version_id=expected_current_version_id,
        expected_lock_revision=expected_lock_revision,
        provider=response.provider,
        model=response.model,
        prompt_hash=prompt_hash,
        input_hash=input_hash,
    )
    return {
        "created": bool(version.get("created")),
        "reason": None if version.get("created") else "source_set_unchanged",
        "version": version,
        "attestation": attestation,
    }
