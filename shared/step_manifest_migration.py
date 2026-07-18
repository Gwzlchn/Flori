"""存量 .done 到 manifest-v1 的一次性迁移工具(设计稿 §2.11 阶段 B/C/D)。

子命令:
- report(默认):按 DB v8 scope 遍历 done/skipped,逐步给出可签发/不一致清单,不写任何对象。
- backfill:report 判定通过的条目签发 producer.kind=legacy_done_backfill 的 manifest。
  旧 .done 缺 def_digest 时默认 legacy_definition_unverified 只报告;
  --accept-legacy-definition=current 且 input_hashes/输出全部校验过才以当前语义定义签发。
- verify:阶段 C 逐节点闭合校验(DB terminal 与 manifest 双向 + 全量 SHA 重验)。
- cleanup:阶段 D,只删 .{step}.done;本列车不在生产执行,须先通过 verify 与 exact DR。

fail-closed 原则:done 但 marker/输出不完整 → 不一致报告,不签发;失败步骤不扫描
部分输出;重复 backfill 对同 digest 是 no-op,已有不同 manifest 时不覆盖。
"""

from __future__ import annotations

import argparse
import asyncio
import fnmatch
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .config import AppConfig
from .db import Database
from .runner_ops import parse_style_tags
from .step_completion import read_valid_manifest, step_definition_digest_for
from .step_manifest import (
    MANIFEST_FORMAT,
    MANIFEST_FORMAT_VERSION,
    ManifestError,
    compute_input_digest,
    manifest_relative_path,
    validate_input_fingerprints,
    validate_manifest,
    validate_output_path,
)
from .step_scope import part_scope
from .storage import StorageBackend, sha256_file
from .version import FLORI_VERSION

_MAX_DONE_BYTES = 64 * 1024


@dataclass
class MigrationReport:
    scanned: int = 0
    eligible: int = 0
    issued: int = 0
    already_present: int = 0
    legacy_definition_unverified: int = 0
    inconsistent: list[dict] = field(default_factory=list)
    verified: int = 0
    verify_failures: list[dict] = field(default_factory=list)
    cleaned: int = 0

    def to_jsonable(self) -> dict:
        return {
            "scanned": self.scanned,
            "eligible": self.eligible,
            "issued": self.issued,
            "already_present": self.already_present,
            "legacy_definition_unverified": self.legacy_definition_unverified,
            "inconsistent": self.inconsistent,
            "verified": self.verified,
            "verify_failures": self.verify_failures,
            "cleaned": self.cleaned,
        }


def _legacy_exec_id(job_id: str, scope_key: str, step: str, marker: bytes) -> str:
    """确定性 legacy 执行身份:不伪造真实 Worker,重复 backfill 得到同一 exec_id。"""
    digest = hashlib.sha256(
        b"|".join([job_id.encode(), scope_key.encode(), step.encode(), marker])
    ).hexdigest()
    return f"legacy:{digest[:40]}"


def _scope_prefix(scope_key: str) -> str:
    from .step_scope import part_id_from_scope

    part_id = part_id_from_scope(scope_key)
    return f"parts/{part_id}/" if part_id else ""


async def _collect_owned_outputs(
    storage: StorageBackend, job_id: str, scope_key: str, outputs_globs: list[str],
) -> list[dict] | None:
    """按当前 output ownership 枚举实际文件并做 size/SHA-256 全量采集;任一失败回 None。"""
    prefix = _scope_prefix(scope_key)
    scope_kind = "job" if not prefix else "part"
    files = await storage.list_files(job_id)
    entries: list[dict] = []
    for rel in sorted(files, key=lambda item: item.encode("utf-8")):
        if prefix:
            if not rel.startswith(prefix):
                continue
            scoped = rel[len(prefix):]
        else:
            if rel.startswith("parts/"):
                continue
            scoped = rel
        if scoped.rsplit("/", 1)[-1].startswith("."):
            continue  # 生命周期 dotfile 不是业务输出
        if not any(fnmatch.fnmatch(scoped, pattern) for pattern in outputs_globs):
            continue
        try:
            validate_output_path(scoped, scope_kind=scope_kind)
        except ManifestError:
            return None
        size = await storage.file_size(job_id, rel)
        sha = await sha256_file(storage, job_id, rel)
        if size is None or sha is None:
            return None
        # 稳定双读:哈希后复核 size 未变,读中替换即判不稳定。
        if await storage.file_size(job_id, rel) != size:
            return None
        entries.append({
            "path": scoped, "size_bytes": size,
            "sha256": f"sha256:{sha}", "media_type": None,
        })
    return entries


async def _load_done_marker(
    storage: StorageBackend, job_id: str, scope_key: str, step: str,
) -> tuple[dict | None, bytes | None, str | None]:
    prefix = _scope_prefix(scope_key)
    raw = await storage.read_file(job_id, f"{prefix}.{step}.done")
    if raw is None:
        return None, None, "done_marker_missing"
    if len(raw) > _MAX_DONE_BYTES:
        return None, None, "done_marker_oversize"
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None, None, "done_marker_corrupt"
    if type(data) is not dict or data.get("step") != step:
        return None, None, "done_marker_step_mismatch"
    if type(data.get("input_hashes")) is not dict:
        return None, None, "done_marker_input_hashes_invalid"
    return data, raw, None


async def migrate_job_step(
    *,
    db: Database,
    storage: StorageBackend,
    config: AppConfig,
    job,
    scope_key: str,
    step_name: str,
    step_cfg: dict,
    status: str,
    apply: bool,
    accept_legacy_definition: str | None,
    report: MigrationReport,
) -> None:
    report.scanned += 1
    where = {"job_id": job.id, "scope": scope_key, "step": step_name}
    existing = await read_valid_manifest(storage, job.id, scope_key, step_name)
    if existing is not None:
        report.already_present += 1
        return
    if status == "skipped":
        # §2.11-B6:与 dag_planner/reconcile 同源判定重推导确定性 skip;
        # 通过者签发 legacy_skip_backfill,no_worker/不可重推导只进报告。
        reason = await _rederive_skip_reason(storage, job, scope_key, step_cfg)
        if reason is None:
            report.inconsistent.append({**where, "reason": "skip_not_rederivable"})
            return
        try:
            definition_digest = step_definition_digest_for(
                job.pipeline, step_cfg, config=config,
                domain=job.domain, style_tags=parse_style_tags(job.style_tags),
            )
        except Exception as exc:
            report.inconsistent.append(
                {**where, "reason": f"definition_digest_failed:{exc}"},
            )
            return
        part_index = None
        from .step_scope import part_id_from_scope as _pid

        if _pid(scope_key) is not None:
            part = next(
                (item for item in db.get_parts(job.id) if item.id == _pid(scope_key)),
                None,
            )
            if part is None:
                report.inconsistent.append({**where, "reason": "unknown_part"})
                return
            part_index = part.part_index
        from .step_completion import build_skipped_manifest

        manifest, encoded = build_skipped_manifest(
            job_id=job.id, scope_key=scope_key, step=step_name,
            part_index=part_index, job_generation=0, reason_code=reason,
            definition_digest=definition_digest, flori_version=FLORI_VERSION,
        )
        manifest["producer"]["kind"] = "legacy_skip_backfill"
        encoded = validate_manifest(manifest)
        report.eligible += 1
        if apply:
            await storage.write_file(
                job.id, manifest_relative_path(scope_key, step_name), encoded,
            )
            report.issued += 1
        return
    done, marker_bytes, error = await _load_done_marker(
        storage, job.id, scope_key, step_name,
    )
    if done is None:
        report.inconsistent.append({**where, "reason": error})
        return
    try:
        fingerprints = validate_input_fingerprints(done["input_hashes"])
    except ManifestError as exc:
        report.inconsistent.append({**where, "reason": f"input_hashes_invalid:{exc}"})
        return
    outputs_globs = [
        pattern for pattern in (step_cfg.get("outputs") or [])
        if isinstance(pattern, str)
    ]
    entries = await _collect_owned_outputs(storage, job.id, scope_key, outputs_globs)
    if entries is None:
        report.inconsistent.append({**where, "reason": "outputs_unstable_or_invalid"})
        return
    # 等价 should_run 门(审查 B1):def_digest 存在时必须匹配当前定义摘要
    # (def_digest_for 同一算法),漂移即拒签——绝不给旧定义产物披上当前定义的 manifest。
    from .step_base import def_digest_for

    stored_def = done.get("def_digest")
    if stored_def is not None:
        current_def = def_digest_for(step_cfg.get("version"), step_cfg.get("ai"))
        if stored_def != current_def:
            report.inconsistent.append({**where, "reason": "definition_drift"})
            return
    elif accept_legacy_definition != "current":
        # 缺 def_digest 的旧 marker 默认不冒充当前定义(§2.11-B5);
        # --accept-legacy-definition=current 只豁免这一种缺失情况。
        report.legacy_definition_unverified += 1
        report.inconsistent.append({**where, "reason": "legacy_definition_unverified"})
        return
    # 输入漂移辅助门:DB input_hash 缓存存在且与 marker 指纹摘要不符即拒
    # (该列无生产写入方时为空,门自然放行;独立重算 input_hashes 需步骤代码,工具不做)。
    db_step = next(
        (
            item for item in db.get_steps(job.id)
            if item.scope_key == scope_key and item.name == step_name
        ),
        None,
    )
    if db_step is not None and db_step.input_hash:
        from .step_manifest import canonical_digest

        if db_step.input_hash != canonical_digest(fingerprints):
            report.inconsistent.append({**where, "reason": "input_drift"})
            return
    try:
        definition_digest = step_definition_digest_for(
            job.pipeline, step_cfg, config=config,
            domain=job.domain, style_tags=parse_style_tags(job.style_tags),
        )
    except Exception as exc:
        report.inconsistent.append({**where, "reason": f"definition_digest_failed:{exc}"})
        return
    finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    part_index = None
    from .step_scope import part_id_from_scope

    if part_id_from_scope(scope_key) is not None:
        parts = {part.id: part for part in db.get_parts(job.id)}
        part = parts.get(part_id_from_scope(scope_key))
        if part is None:
            report.inconsistent.append({**where, "reason": "unknown_part"})
            return
        part_index = part.part_index
    manifest = {
        "format": MANIFEST_FORMAT,
        "format_version": MANIFEST_FORMAT_VERSION,
        "job_id": job.id,
        "scope": {
            "kind": "job" if part_index is None else "part",
            "scope_key": scope_key,
            "part_id": part_id_from_scope(scope_key),
            "part_index": part_index,
        },
        "step": step_name,
        "outcome": "done",
        "execution": {
            "exec_id": _legacy_exec_id(job.id, scope_key, step_name, marker_bytes),
            "job_generation": 0,
            "attempt": 1,
            "started_at": finished_at,
            "committed_at": finished_at,
            "duration_sec": 0.0,
        },
        "compatibility": {
            "input_fingerprints": fingerprints,
            "input_digest": compute_input_digest(fingerprints),
            "definition_digest": definition_digest,
        },
        "producer": {
            "flori_version": FLORI_VERSION,
            "build_sha": None,
            "worker_id": None,
            "runner": "backfill",
            "image": None,
            "image_digest": None,
            "tool_versions": {},
            "kind": "legacy_done_backfill",
        },
        "outputs": entries,
        "skip": None,
    }
    try:
        encoded = validate_manifest(manifest)
    except ManifestError as exc:
        report.inconsistent.append({**where, "reason": f"manifest_invalid:{exc}"})
        return
    report.eligible += 1
    if apply:
        await storage.write_file(
            job.id, manifest_relative_path(scope_key, step_name), encoded,
        )
        report.issued += 1


async def _rederive_skip_reason(
    storage: StorageBackend, job, scope_key: str, step_cfg: dict,
) -> str | None:
    """与 dag_planner/reconcile 同源的确定性 skip 重推导;不可重推导返回 None。"""
    flags = (job.meta or {}).get("flags") or {}
    if flags.get("mechanical_only") is True and step_cfg.get("pool") == "ai":
        return "mechanical_only"
    condition = step_cfg.get("condition")
    rules = step_cfg.get("rules")
    if not condition and not rules:
        return None
    prefix = _scope_prefix(scope_key)
    files = [
        rel[len(prefix):]
        for rel in await storage.list_files(job.id)
        if rel.startswith(prefix)
    ] if prefix else [
        rel for rel in await storage.list_files(job.id)
        if not rel.startswith("parts/")
    ]
    has_srt = any(fnmatch.fnmatch(item, "input/*.srt") for item in files)
    has_ass = any(fnmatch.fnmatch(item, "input/*.ass") for item in files)
    if condition:
        should_run = {
            "no_subtitle": not has_srt,
            "has_subtitle": has_srt,
            "has_danmaku": has_ass,
        }.get(condition, True)
        return None if should_run else "rule_false"
    # 声明式 rules(与 task_router._eval_rules 同语义:自上而下首条命中生效)。
    for rule in rules or []:
        if not isinstance(rule, dict):
            continue
        when = rule.get("when")
        when = "on" if when in (True, None) else ("skip" if when is False else str(when))
        if rule.get("exists") is not None:
            if any(fnmatch.fnmatch(item, rule["exists"]) for item in files):
                return None if when == "on" else "rule_false"
            continue
        if rule.get("if_flag") is not None:
            if flags.get(rule["if_flag"]) is True:
                return None if when == "on" else "rule_false"
            continue
        return None if when == "on" else "rule_false"
    return None  # 无命中默认运行 → skip 不可由规则重推导


async def _list_manifest_rels(storage: StorageBackend, job_id: str) -> list[str]:
    """物理枚举 .flori/steps/* manifest(list_files 隔离内部命名空间,须走后端内部)。"""
    rels: list[str] = []
    jobs_dir = getattr(storage, "jobs_dir", None)
    if jobs_dir is not None:
        root = jobs_dir / job_id
        if root.is_dir():
            for path in root.rglob("manifest.json"):
                rel = path.relative_to(root).as_posix()
                if "/.flori/steps/" in f"/{rel}":
                    rels.append(rel)
        return sorted(rels)
    client_getter = getattr(storage, "_client", None)
    bucket = getattr(storage, "_bucket", None)
    if callable(client_getter) and bucket:
        import asyncio as _asyncio

        def _scan() -> list[str]:
            out = []
            prefix = f"{job_id}/"
            for obj in client_getter().list_objects(bucket, prefix=prefix, recursive=True):
                rel = obj.object_name[len(prefix):]
                if ".flori/steps/" in rel and rel.endswith("/manifest.json"):
                    out.append(rel)
            return sorted(out)

        return await _asyncio.to_thread(_scan)
    return []


async def verify_job_step(
    *, storage: StorageBackend, job, scope_key: str, step_name: str,
    status: str, report: MigrationReport,
) -> None:
    """阶段 C:DB terminal 与 manifest 双向闭合 + 全量 SHA 重验。"""
    where = {"job_id": job.id, "scope": scope_key, "step": step_name}
    manifest = await read_valid_manifest(storage, job.id, scope_key, step_name)
    if manifest is None:
        if status in ("done", "skipped"):
            report.verify_failures.append({**where, "reason": "manifest_missing"})
        return
    if status not in ("done", "skipped"):
        report.verify_failures.append({**where, "reason": f"db_not_terminal:{status}"})
        return
    prefix = _scope_prefix(scope_key)
    for entry in manifest["outputs"]:
        rel = f"{prefix}{entry['path']}"
        size = await storage.file_size(job.id, rel)
        sha = await sha256_file(storage, job.id, rel)
        if size != entry["size_bytes"] or sha is None or f"sha256:{sha}" != entry["sha256"]:
            report.verify_failures.append({**where, "reason": f"output_mismatch:{entry['path']}"})
            return
    report.verified += 1


async def run_migration(
    *,
    db: Database,
    storage: StorageBackend,
    config: AppConfig,
    command: str = "report",
    accept_legacy_definition: str | None = None,
    job_ids: list[str] | None = None,
) -> MigrationReport:
    from .pipeline_scope import expand_pipeline_steps
    from .step_scope import execution_step_key

    report = MigrationReport()
    if job_ids:
        jobs = [db.get_job(job_id) for job_id in job_ids]
        jobs = [job for job in jobs if job is not None]
    else:
        _, jobs = db.list_jobs(limit=1_000_000, current_only=False)
    for job in jobs:
        steps_list = config.pipelines.get(job.pipeline, {}).get("steps", [])
        if not steps_list:
            continue
        parts = db.get_parts(job.id)
        expanded = expand_pipeline_steps(steps_list, parts)
        db_steps = {
            execution_step_key(item.scope_key, item.name): item
            for item in db.get_steps(job.id)
        }
        if command == "verify":
            # 双向闭合:遍历全部 expanded steps(含非终态),再对物理 manifest 清单
            # 找不属于任何 pipeline 节点的孤儿。
            expected_rels = set()
            for name, cfg in expanded.items():
                item = db_steps.get(name)
                status = (
                    item.status.value if item is not None and hasattr(item.status, "value")
                    else (str(item.status) if item is not None else "absent")
                )
                expected_rels.add(
                    manifest_relative_path(cfg["scope_key"], cfg["template_step"]),
                )
                await verify_job_step(
                    storage=storage, job=job, scope_key=cfg["scope_key"],
                    step_name=cfg["template_step"], status=status, report=report,
                )
            for rel in await _list_manifest_rels(storage, job.id):
                if rel not in expected_rels:
                    report.verify_failures.append({
                        "job_id": job.id, "manifest": rel,
                        "reason": "orphan_manifest_outside_pipeline",
                    })
            continue
        for name, cfg in expanded.items():
            item = db_steps.get(name)
            if item is None:
                continue
            status = item.status.value if hasattr(item.status, "value") else str(item.status)
            if status not in ("done", "skipped"):
                continue  # 失败/未跑步骤只保诊断,绝不据部分输出签发(§2.11-B8)
            scope_key = cfg["scope_key"]
            template_step = cfg["template_step"]
            if command == "cleanup":
                prefix = _scope_prefix(scope_key)
                marker = f"{prefix}.{template_step}.done"
                manifest = await read_valid_manifest(
                    storage, job.id, scope_key, template_step,
                )
                # 阶段 D 前置:仅当 manifest 已闭合才删 marker;不删 manifest/输出/审计。
                if manifest is not None and await storage.read_file(job.id, marker) is not None:
                    await storage.delete_file(job.id, marker)
                    report.cleaned += 1
            else:
                await migrate_job_step(
                    db=db, storage=storage, config=config, job=job,
                    scope_key=scope_key, step_name=template_step, step_cfg=cfg,
                    status=status, apply=(command == "backfill"),
                    accept_legacy_definition=accept_legacy_definition,
                    report=report,
                )
    return report


def main() -> int:
    from pathlib import Path
    import os

    from .config import load_config
    from .storage import create_storage

    parser = argparse.ArgumentParser(description=".done -> manifest-v1 迁移工具")
    parser.add_argument(
        "command", nargs="?", default="report",
        choices=["report", "backfill", "verify", "cleanup"],
    )
    parser.add_argument("--accept-legacy-definition", choices=["current"], default=None)
    parser.add_argument("--job", action="append", dest="jobs", default=None)
    args = parser.parse_args()

    config = load_config(
        config_dir=os.environ.get("CONFIG_DIR", "/data/configs"),
        data_dir=os.environ.get("DATA_DIR", "/data"),
    )
    db = Database(Path(config.db_path))
    storage = create_storage(config.jobs_dir)
    report = asyncio.run(run_migration(
        db=db, storage=storage, config=config,
        command=args.command,
        accept_legacy_definition=args.accept_legacy_definition,
        job_ids=args.jobs,
    ))
    print(json.dumps(report.to_jsonable(), ensure_ascii=False, indent=2))
    return 1 if (report.inconsistent and args.command == "backfill") else 0


if __name__ == "__main__":
    raise SystemExit(main())
