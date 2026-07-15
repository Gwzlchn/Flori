"""在 producer 与 concepts 之间批量独立核验语义证据候选。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from shared.errors import AIProviderError
from shared.note_text import markdown_to_index_text
from shared.provenance import (
    MAX_SEMANTIC_CANDIDATES,
    MAX_SEMANTIC_AI_LOG_BYTES,
    MAX_SEMANTIC_AI_LOG_RECORDS,
    SEMANTIC_ATTESTATION_POLICY,
    SEMANTIC_BATCH_COMMIT_PATH,
    build_provenance_candidate_manifest,
    build_provenance_manifest,
    build_semantic_attestation_prompt,
    build_semantic_batch_commit,
    canonical_json_bytes,
    materialize_semantic_attestations,
    semantic_attestation_batch_id,
    validate_provenance_candidate_manifest,
    validate_provenance_manifest,
    validate_semantic_batch_commit,
    validate_source_manifest,
    write_json_atomic,
    write_provenance_candidate_manifest,
    write_provenance_manifest,
)


SOURCE_MANIFEST_PATH = "intermediate/source_segments.json"
_NOTE_TYPES = ("smart", "translated")


def producer_invocation_id(ai) -> str | None:
    """只接受 provider 返回的真实 session/request id,本地猜测值不能成为证明身份。"""
    response = ai.last_response
    value = response.session_id if response is not None else None
    if type(value) is not str or not value.strip() or len(value) > 128:
        return None
    return value


def persist_semantic_candidates(
    job_dir: Path,
    *,
    pipeline: str,
    note_type: str,
    note_artifact: str,
    candidates: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """producer 始终覆盖 canonical sidecar,空结果也写 tombstone 清除远端旧值。"""
    target = job_dir / "output" / "provenance_candidates" / f"{note_type}.json"
    note_bytes = (job_dir / note_artifact).read_bytes()
    normalized_body = markdown_to_index_text(note_bytes.decode("utf-8"))
    source_path = job_dir / SOURCE_MANIFEST_PATH
    if not source_path.is_file():
        if candidates:
            raise ValueError("semantic candidates require a source manifest")
        tombstone = {
            "schema_version": 2,
            "status": "no_source",
            "job_id": job_dir.name,
            "note_type": note_type,
            "note_artifact": note_artifact,
            "note_sha256": _sha256(note_bytes),
            "source_manifest": SOURCE_MANIFEST_PATH,
            "source_manifest_sha256": None,
            "candidates": [],
        }
        write_json_atomic(
            target,
            tombstone,
            trusted_root=job_dir,
            validator=lambda value: validate_provenance_candidate_manifest(
                value,
                source_manifest=None,
                note_bytes=note_bytes,
                normalized_body=normalized_body,
            ),
        )
        return {"status": "no_source", "candidates": 0}

    source_data = source_path.read_bytes()
    source_manifest = _load_canonical(source_data, "source manifest")
    source_manifest = validate_source_manifest(source_manifest)
    if source_manifest["job_id"] != job_dir.name or source_manifest["pipeline"] != pipeline:
        raise ValueError("semantic candidate source identity is invalid")
    manifest = build_provenance_candidate_manifest(
        job_id=job_dir.name,
        note_type=note_type,
        note_artifact=note_artifact,
        note_bytes=note_bytes,
        normalized_body=normalized_body,
        source_manifest_path=SOURCE_MANIFEST_PATH,
        source_manifest=source_manifest,
        candidates=candidates,
    )
    write_provenance_candidate_manifest(
        target,
        manifest,
        trusted_root=job_dir,
        source_manifest=source_manifest,
        note_bytes=note_bytes,
        normalized_body=normalized_body,
    )
    return {"status": manifest["status"], "candidates": len(candidates)}


def semantic_attestation_input_hashes(job_dir: Path) -> dict[str, str]:
    """独立 attestation step 的幂等输入只绑定 producer 候选和 exact final。"""
    hashes: dict[str, str] = {}
    for note_type in _NOTE_TYPES:
        for kind, rel in (
            ("candidate", f"output/provenance_candidates/{note_type}.json"),
            ("final", f"output/provenance/{note_type}.json"),
        ):
            path = job_dir / rel
            if path.is_file():
                hashes[f"semantic_{note_type}_{kind}"] = _sha256(path.read_bytes())
    return hashes


def finalize_pending_semantic_provenance(
    job_dir: Path,
    *,
    pipeline: str,
    ai,
) -> dict[str, Any]:
    """一次批量调用准备全部 final,再以 commit-last 原子发布可信批次。"""
    source_path = job_dir / SOURCE_MANIFEST_PATH
    if not source_path.is_file():
        (job_dir / SEMANTIC_BATCH_COMMIT_PATH).unlink(missing_ok=True)
        return {"note_types": 0, "accepted": 0, "rejected": 0, "failed": 0, "calls": 0}
    source_data = source_path.read_bytes()
    source_manifest = validate_source_manifest(_load_canonical(source_data, "source manifest"))
    if source_manifest["job_id"] != job_dir.name or source_manifest["pipeline"] != pipeline:
        raise ValueError("semantic attestor source identity is invalid")

    loaded: list[dict[str, Any]] = []
    candidate_artifacts: list[dict[str, str]] = []
    for note_type in _NOTE_TYPES:
        candidate_path = job_dir / "output" / "provenance_candidates" / f"{note_type}.json"
        if not candidate_path.is_file():
            continue
        candidate_data = candidate_path.read_bytes()
        candidate_manifest = _load_canonical(candidate_data, "semantic candidates")
        if candidate_manifest.get("status") == "no_source":
            continue
        note_artifact = candidate_manifest.get("note_artifact")
        if type(note_artifact) is not str:
            raise ValueError("semantic candidate note artifact is invalid")
        note_bytes = (job_dir / note_artifact).read_bytes()
        normalized_body = markdown_to_index_text(note_bytes.decode("utf-8"))
        candidate_manifest = validate_provenance_candidate_manifest(
            candidate_manifest,
            source_manifest=source_manifest,
            note_bytes=note_bytes,
            normalized_body=normalized_body,
        )
        loaded.append({
            "note_type": note_type,
            "path": candidate_path,
            "data": candidate_data,
            "manifest": candidate_manifest,
            "note_bytes": note_bytes,
            "normalized_body": normalized_body,
        })
        candidate_artifacts.append({
            "note_type": note_type,
            "path": candidate_path.relative_to(job_dir).as_posix(),
            "sha256": _sha256(candidate_data),
        })

    if not loaded:
        (job_dir / SEMANTIC_BATCH_COMMIT_PATH).unlink(missing_ok=True)
        return {"note_types": 0, "accepted": 0, "rejected": 0, "failed": 0, "calls": 0}

    candidates = [
        candidate
        for item in loaded
        for candidate in item["manifest"]["candidates"]
    ]
    if len(candidates) > MAX_SEMANTIC_CANDIDATES:
        raise ValueError("semantic attestation batch candidates exceed limit")
    response_text: str | None = None
    response = None
    prompt: str | None = None
    ai_log_binding: dict[str, Any] | None = None
    calls = 0
    if candidates:
        prompt = build_semantic_attestation_prompt(
            [item["manifest"] for item in loaded], source_manifest,
        )
        try:
            response_text = ai.call(prompt, response_format="json", temperature=0)
            calls = 1
            response = ai.last_response
            invocation_id = producer_invocation_id(ai)
            if response is None or invocation_id is None:
                raise ValueError("semantic attestor invocation identity is unavailable")
            ai_log_binding = _capture_ai_log_binding(
                job_dir,
                step_name=ai.step_name,
                prompt=prompt,
                response_text=response_text,
                response=response,
            )
        except Exception as exc:
            ai.log.warning(
                "semantic_attestation_failed",
                error_class=type(exc).__name__,
                error=str(exc)[:300],
            )
            if isinstance(exc, AIProviderError):
                raise
            raise AIProviderError(f"semantic attestation failed: {exc}") from exc

    batch_id = semantic_attestation_batch_id(
        job_id=job_dir.name,
        pipeline=pipeline,
        attestor_component=ai.step_name,
        candidate_manifests=candidate_artifacts,
        ai_log=ai_log_binding,
    )
    all_candidate_ids = [item["candidate_id"] for item in candidates]
    accepted_total = 0
    rejected_total = 0
    pending: list[dict[str, Any]] = []
    for item in loaded:
        manifest = item["manifest"]
        accepted: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        if manifest["candidates"]:
            assert response_text is not None and response is not None
            assert prompt is not None and ai_log_binding is not None
            accepted, rejected = materialize_semantic_attestations(
                manifest,
                source_manifest,
                response_text=response_text,
                attestor_component=ai.step_name,
                attestor_invocation_id=response.session_id,
                attestor_provider=response.provider,
                attestor_model=response.model,
                attestor_prompt=prompt,
                ai_log_binding=ai_log_binding,
                batch_id=batch_id,
                response_candidate_ids=all_candidate_ids,
            )
        provenance_path = job_dir / "output" / "provenance" / f"{item['note_type']}.json"
        provenance = validate_provenance_manifest(
            _load_canonical(provenance_path.read_bytes(), "note provenance"),
            source_manifest=source_manifest,
            note_bytes=item["note_bytes"],
            normalized_body=item["normalized_body"],
        )
        if (
            provenance["job_id"] != job_dir.name
            or provenance["note_type"] != item["note_type"]
            or provenance["note_artifact"] != manifest["note_artifact"]
            or provenance["source_manifest"] != SOURCE_MANIFEST_PATH
        ):
            raise ValueError("semantic attestor provenance identity is invalid")
        exact = [
            mapping for mapping in provenance["segments"]
            if mapping.get("verification_policy") != SEMANTIC_ATTESTATION_POLICY
        ]
        final = build_provenance_manifest(
            job_id=job_dir.name,
            note_type=item["note_type"],
            note_artifact=manifest["note_artifact"],
            note_bytes=item["note_bytes"],
            normalized_body=item["normalized_body"],
            source_manifest_path=SOURCE_MANIFEST_PATH,
            source_manifest=source_manifest,
            segments=[*exact, *accepted],
        )
        pending.append({
            **item,
            "provenance_path": provenance_path,
            "final": final,
            "final_data": canonical_json_bytes(final),
        })
        accepted_total += len(accepted)
        rejected_total += len(rejected)

    provenance_artifacts = [{
        "note_type": item["note_type"],
        "path": item["provenance_path"].relative_to(job_dir).as_posix(),
        "sha256": _sha256(item["final_data"]),
    } for item in pending]
    commit = build_semantic_batch_commit(
        job_id=job_dir.name,
        pipeline=pipeline,
        batch_id=batch_id,
        attestor_component=ai.step_name,
        candidate_manifests=candidate_artifacts,
        provenance_manifests=provenance_artifacts,
        ai_log=ai_log_binding,
    )
    _publish_batch(job_dir, pending, commit, source_manifest)
    return {
        "note_types": len(pending),
        "accepted": accepted_total,
        "rejected": rejected_total,
        "failed": 0,
        "calls": calls,
        "batch_id": batch_id,
    }


def _capture_ai_log_binding(
    job_dir: Path,
    *,
    step_name: str,
    prompt: str,
    response_text: str,
    response,
) -> dict[str, Any]:
    rel = f"output/ai_logs/{step_name}.jsonl"
    path = job_dir / rel
    data = path.read_bytes()
    if len(data) > MAX_SEMANTIC_AI_LOG_BYTES:
        raise ValueError("semantic attestor ai_log exceeds size limit")
    lines = [line for line in data.splitlines() if line.strip()]
    if len(lines) > MAX_SEMANTIC_AI_LOG_RECORDS:
        raise ValueError("semantic attestor ai_log has too many records")
    records = []
    for line in lines:
        try:
            record = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise ValueError("semantic attestor ai_log is invalid JSONL") from exc
        if not isinstance(record, dict):
            raise ValueError("semantic attestor ai_log record is invalid")
        records.append(record)
    matches = [
        record for record in records
        if record.get("job_id") == job_dir.name
        and record.get("step") == step_name
        and record.get("session_id") == response.session_id
        and (record.get("prompt") or {}).get("rendered", {}).get("user") == prompt
        and (record.get("output") or {}).get("content") == response_text
        and record.get("ok") is True
    ]
    if len(matches) != 1:
        raise ValueError("semantic attestor ai_log record is not unique")
    record = matches[0]
    routing = record.get("routing") or {}
    if routing.get("provider") != response.provider or routing.get("model") != response.model:
        raise ValueError("semantic attestor ai_log routing changed")
    call_index = record.get("call_index")
    if type(call_index) is not int or call_index < 0:
        raise ValueError("semantic attestor ai_log call_index is invalid")
    try:
        parsed = json.loads(response_text)
        decisions = parsed["decisions"]
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise ValueError("semantic attestor response is invalid") from exc
    return {
        "path": rel,
        "call_index": call_index,
        "record_sha256": _sha256(canonical_json_bytes(record)),
        "session_id": response.session_id,
        "provider": response.provider,
        "model": response.model,
        "step": step_name,
        "job_id": job_dir.name,
        "prompt_user_sha256": _sha256(prompt.encode("utf-8")),
        "response_content_sha256": _sha256(response_text.encode("utf-8")),
        "response_decision_sha256": _sha256(canonical_json_bytes(decisions)),
    }


def _publish_batch(
    job_dir: Path,
    pending: Sequence[Mapping[str, Any]],
    commit: Mapping[str, Any],
    source_manifest: Mapping[str, Any],
) -> None:
    commit_path = job_dir / SEMANTIC_BATCH_COMMIT_PATH
    targets = [item["provenance_path"] for item in pending]
    backups = {path: path.read_bytes() if path.is_file() else None for path in [*targets, commit_path]}
    staging: list[tuple[Path, Path]] = []
    try:
        for item in pending:
            target = item["provenance_path"]
            staged = target.with_name(f".{target.name}.{commit['batch_id']}.staged")
            write_provenance_manifest(
                staged,
                item["final"],
                trusted_root=job_dir,
                source_manifest=source_manifest,
                note_bytes=item["note_bytes"],
                normalized_body=item["normalized_body"],
            )
            staging.append((staged, target))
        for staged, target in staging:
            staged.replace(target)
        write_json_atomic(
            commit_path,
            commit,
            trusted_root=job_dir,
            validator=validate_semantic_batch_commit,
        )
    except BaseException:
        for path, previous in backups.items():
            if previous is None:
                path.unlink(missing_ok=True)
            else:
                _write_bytes_atomic(path, previous)
        raise
    finally:
        for staged, _target in staging:
            staged.unlink(missing_ok=True)


def _write_bytes_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.rollback")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_bytes(data)
    tmp.replace(path)


def _load_canonical(data: bytes, field: str) -> dict[str, Any]:
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"{field} is not UTF-8 JSON") from exc
    if not isinstance(value, dict) or canonical_json_bytes(value) != data:
        raise ValueError(f"{field} is not canonical JSON")
    return value


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
