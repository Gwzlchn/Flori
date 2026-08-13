"""验证论文章节卡的可恢复 AI 审计断点。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Mapping

from shared.step_artifacts import file_hash


CHECKPOINT_FORMAT = "flori-document-chapter-checkpoint"
CHECKPOINT_VERSION = 1
MAX_STAGE_ATTEMPTS = 3
_RETRY_INSTRUCTION = (
    "\n\n上一次输出未通过确定性结构与证据闭包校验。"
    "重新生成完整 JSON，不要返回补丁或解释。校验反馈="
)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _text_digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def build_stage_retry_prompt(base_prompt: str, validation_error: str) -> str:
    """从基础 prompt 和本地校验错误重建下一次调用；执行与恢复必须同源。"""
    feedback = _canonical_json({"validation_error": validation_error})
    return base_prompt + _RETRY_INSTRUCTION + feedback


def _route_identity(selection: Mapping[str, Any]) -> dict[str, Any] | None:
    tiers = selection.get("tiers")
    if not isinstance(tiers, list) or len(tiers) != 1:
        return None
    tier = tiers[0]
    if not isinstance(tier, dict):
        return None
    provider = tier.get("provider")
    model = tier.get("model")
    effort = tier.get("reasoning_effort")
    if not isinstance(provider, str) or not provider:
        return None
    if not isinstance(model, str) or not model:
        return None
    if effort is not None and (not isinstance(effort, str) or not effort):
        return None
    return {
        "provider": provider,
        "model": model,
        "reasoning_effort": effort,
    }


def build_chapter_input_identity(
    *,
    job_dir: Path,
    package: Mapping[str, Any],
    prompt: str,
    schema: Mapping[str, Any],
    images: list[Path],
    template: Any,
    selection: Mapping[str, Any],
) -> dict[str, Any] | None:
    """冻结一次章节调用的全部可复核输入；形状不完整时不产生断点。"""
    package_id = package.get("package_id")
    route = _route_identity(selection)
    if not isinstance(package_id, str) or not package_id or route is None:
        return None
    image_records = []
    try:
        root = job_dir.resolve()
        for image in images:
            resolved = image.resolve(strict=True)
            relative = resolved.relative_to(root).as_posix()
            image_records.append({
                "path": relative,
                "bytes": resolved.stat().st_size,
                "sha256": file_hash(resolved),
            })
    except (OSError, ValueError):
        return None
    template_identity = {
        "name": getattr(template, "name", None),
        "source": getattr(template, "source", None),
        "sha256": getattr(template, "sha256", None),
        "version": getattr(template, "version", None),
    }
    if not all(
        isinstance(template_identity[key], str) and template_identity[key]
        for key in ("name", "source", "sha256")
    ):
        return None
    return {
        "job_id": job_dir.name,
        "step": "05_smart",
        "package_id": package_id,
        "package_sha256": _digest(package),
        "prompt_sha256": _text_digest(prompt),
        "schema_sha256": _digest(schema),
        "images": image_records,
        "template": template_identity,
        "routing": route,
    }


def _normalized_audit_images(
    job_dir: Path, records: Any, expected: Any,
) -> list[dict[str, Any]] | None:
    if not isinstance(records, list) or not isinstance(expected, list):
        return None
    if len(records) != len(expected):
        return None
    normalized = []
    try:
        root = job_dir.resolve()
        for record, expected_record in zip(records, expected, strict=True):
            if not isinstance(record, dict):
                return None
            if not isinstance(expected_record, dict):
                return None
            path = record.get("path")
            size = record.get("bytes")
            digest = record.get("hash")
            if not isinstance(path, str) or type(size) is not int or not isinstance(digest, str):
                return None
            candidate = Path(path)
            if candidate.is_absolute():
                resolved = candidate.resolve()
                try:
                    relative = resolved.relative_to(root)
                except ValueError:
                    expected_relative = Path(str(expected_record.get("path", "")))
                    if not expected_relative.parts:
                        return None
                    if tuple(resolved.parts[-len(expected_relative.parts):]) != (
                        expected_relative.parts
                    ):
                        return None
                    relative = expected_relative
            else:
                relative = candidate
            if relative.is_absolute() or ".." in relative.parts or not relative.parts:
                return None
            normalized.append({
                "path": relative.as_posix(),
                "bytes": size,
                "sha256": "sha256:" + digest.removeprefix("sha256:"),
            })
    except (OSError, ValueError):
        return None
    return normalized


def _audit_identity(record: Mapping[str, Any]) -> dict[str, Any] | None:
    exec_id = record.get("exec_id")
    stage = record.get("audit_stage")
    session_id = record.get("session_id")
    if not isinstance(exec_id, str) or not exec_id:
        return None
    if not isinstance(stage, str) or not stage:
        return None
    if not isinstance(session_id, str) or not session_id:
        return None
    return {
        "exec_id": exec_id,
        "audit_stage": stage,
        "session_id": session_id,
    }


def _record_matches_static_input(
    record: Mapping[str, Any], identity: Mapping[str, Any], job_dir: Path,
) -> bool:
    if record.get("phase") != "final" or record.get("ok") is not True:
        return False
    if record.get("job_id") != identity.get("job_id"):
        return False
    if record.get("step") != identity.get("step"):
        return False
    package_id = identity.get("package_id")
    if record.get("audit_stage") != f"01-chapter-{package_id}":
        return False
    prompt = record.get("prompt")
    if not isinstance(prompt, dict):
        return False
    rendered = prompt.get("rendered")
    template = prompt.get("template")
    if not isinstance(rendered, dict) or not isinstance(template, dict):
        return False
    expected_template = identity.get("template")
    if not isinstance(expected_template, dict):
        return False
    if any(template.get(key) != expected_template.get(key) for key in expected_template):
        return False
    expected_images = identity.get("images")
    if _normalized_audit_images(
        job_dir, prompt.get("images"), expected_images,
    ) != expected_images:
        return False
    routing = record.get("routing")
    expected_routing = identity.get("routing")
    if not isinstance(routing, dict) or not isinstance(expected_routing, dict):
        return False
    return all(routing.get(key) == value for key, value in expected_routing.items())


def _record_prompt(record: Mapping[str, Any]) -> str | None:
    prompt = record.get("prompt")
    rendered = prompt.get("rendered") if isinstance(prompt, dict) else None
    user = rendered.get("user") if isinstance(rendered, dict) else None
    return user if isinstance(user, str) else None


def build_chapter_checkpoint(
    *, record: Mapping[str, Any], identity: Mapping[str, Any],
    result: Mapping[str, Any], job_dir: Path,
) -> dict[str, Any] | None:
    """只为已完成且通过结构验证的 AI 审计记录签出章节断点。"""
    audit = _audit_identity(record)
    output = record.get("output")
    processed = record.get("output_processed")
    actual_prompt = _record_prompt(record)
    if audit is None or not _record_matches_static_input(record, identity, job_dir):
        return None
    if not isinstance(processed, dict) or processed.get("contract") != "valid":
        return None
    if actual_prompt is None:
        return None
    if not isinstance(output, dict) or not isinstance(output.get("content"), str):
        return None
    body = {
        "format": CHECKPOINT_FORMAT,
        "version": CHECKPOINT_VERSION,
        "input": dict(identity),
        "audit": audit,
        "attempt_prompt_sha256": _text_digest(actual_prompt),
        "output_sha256": _text_digest(output["content"]),
        "result_sha256": _digest(result),
    }
    return {**body, "digest": _digest(body)}


def _checkpoint_matches_result(
    *,
    record: Mapping[str, Any],
    identity: Mapping[str, Any],
    actual_prompt: str,
    raw: str,
    result: Mapping[str, Any],
) -> bool:
    checkpoint = record.get("chapter_checkpoint")
    if not isinstance(checkpoint, dict):
        return False
    if set(checkpoint) != {
        "format", "version", "input", "audit", "attempt_prompt_sha256",
        "output_sha256", "result_sha256", "digest",
    }:
        return False
    if checkpoint.get("format") != CHECKPOINT_FORMAT:
        return False
    if checkpoint.get("version") != CHECKPOINT_VERSION:
        return False
    if checkpoint.get("input") != identity:
        return False
    audit = _audit_identity(record)
    if audit is None or checkpoint.get("audit") != audit:
        return False
    body = {key: checkpoint[key] for key in checkpoint if key != "digest"}
    if checkpoint.get("digest") != _digest(body):
        return False
    if checkpoint.get("attempt_prompt_sha256") != _text_digest(actual_prompt):
        return False
    if checkpoint.get("output_sha256") != _text_digest(raw):
        return False
    if checkpoint.get("result_sha256") != _digest(result):
        return False
    return True


def _attempt_chain_key(record: Mapping[str, Any]) -> tuple[str, int] | None:
    exec_id = record.get("exec_id")
    if not isinstance(exec_id, str) or ":" not in exec_id:
        return None
    prefix, raw_index = exec_id.rsplit(":", 1)
    try:
        index = int(raw_index)
    except ValueError:
        return None
    if not prefix or not 0 <= index < MAX_STAGE_ATTEMPTS:
        return None
    return prefix, index


def _ordered_attempt_chains(
    records: list[Mapping[str, Any]], stage: str,
) -> list[list[Mapping[str, Any]]]:
    chains: dict[str, list[tuple[int, Mapping[str, Any]]]] = {}
    for record in records:
        if record.get("audit_stage") != stage:
            continue
        key = _attempt_chain_key(record)
        if key is None:
            continue
        prefix, index = key
        chains.setdefault(prefix, []).append((index, record))
    valid = []
    for attempts in chains.values():
        indices = [index for index, _record in attempts]
        if indices == list(range(len(indices))) and len(indices) <= MAX_STAGE_ATTEMPTS:
            valid.append([record for _index, record in attempts])
    return valid


def restore_chapter_attempts(
    *,
    records: list[Mapping[str, Any]],
    identity: Mapping[str, Any],
    base_prompt: str,
    schema: Mapping[str, Any],
    validator: Callable[[dict[str, Any]], dict[str, Any]],
    parser: Callable[[str, Mapping[str, Any]], dict[str, Any]],
    job_dir: Path,
) -> dict[str, Any] | None:
    """按真实尝试链重建反馈 prompt；链不完整、乱序或任何输入漂移都拒绝。"""
    if identity.get("prompt_sha256") != _text_digest(base_prompt):
        return None
    stage = f"01-chapter-{identity.get('package_id')}"
    for chain in reversed(_ordered_attempt_chains(records, stage)):
        expected_prompt = base_prompt
        for position, record in enumerate(chain):
            if _audit_identity(record) is None:
                break
            if not _record_matches_static_input(record, identity, job_dir):
                break
            if _record_prompt(record) != expected_prompt:
                break
            output = record.get("output")
            raw = output.get("content") if isinstance(output, dict) else None
            processed = record.get("output_processed")
            if not isinstance(raw, str) or not isinstance(processed, dict):
                break
            if processed.get("attempt") != position + 1:
                break
            try:
                result = validator(parser(raw, schema))
            except (KeyError, TypeError, ValueError) as exc:
                if processed.get("contract") != "invalid":
                    break
                if position + 1 == len(chain):
                    break
                expected_prompt = build_stage_retry_prompt(base_prompt, str(exc))
                continue
            if processed.get("contract") != "valid" or position + 1 != len(chain):
                break
            if "chapter_checkpoint" in record:
                if not _checkpoint_matches_result(
                    record=record,
                    identity=identity,
                    actual_prompt=expected_prompt,
                    raw=raw,
                    result=result,
                ):
                    break
            return result
    return None


def restore_chapter_checkpoint(
    *,
    record: Mapping[str, Any],
    identity: Mapping[str, Any],
    schema: Mapping[str, Any],
    validator: Callable[[dict[str, Any]], dict[str, Any]],
    parser: Callable[[str, Mapping[str, Any]], dict[str, Any]],
    job_dir: Path,
) -> dict[str, Any] | None:
    """保留单次成功记录的局部接口；多次尝试必须走完整链恢复。"""
    if "chapter_checkpoint" not in record:
        return None
    base_prompt = _record_prompt(record)
    if base_prompt is None:
        return None
    return restore_chapter_attempts(
        records=[record], identity=identity, base_prompt=base_prompt,
        schema=schema, validator=validator, parser=parser, job_dir=job_dir,
    )


def restore_legacy_chapter_record(
    *,
    record: Mapping[str, Any],
    identity: Mapping[str, Any],
    schema: Mapping[str, Any],
    validator: Callable[[dict[str, Any]], dict[str, Any]],
    parser: Callable[[str, Mapping[str, Any]], dict[str, Any]],
    job_dir: Path,
) -> dict[str, Any] | None:
    """保留单次 legacy 成功记录的局部接口；缺失身份仍保守重跑。"""
    if "chapter_checkpoint" in record:
        return None
    base_prompt = _record_prompt(record)
    if base_prompt is None:
        return None
    return restore_chapter_attempts(
        records=[record], identity=identity, base_prompt=base_prompt,
        schema=schema, validator=validator, parser=parser, job_dir=job_dir,
    )
