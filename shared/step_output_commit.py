"""步骤输出提交的共享纯逻辑:输出展开校验、candidate 采集、staging 命名空间与诊断白名单。

设计稿 §2.5/§2.6:Worker 在子进程 rc=0 后据 pipelines outputs glob 展开精确输出,
经本模块校验后流式哈希、构造 final manifest,再走 Storage staging->promote->
read-back->manifest-last 协议。本模块无 IO 副作用(除只读 stat/哈希),Redis/网络
集成在 transport 与 storage 层。
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import mimetypes
import os
import stat as stat_module
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .step_manifest import (
    MANIFEST_FORMAT,
    MANIFEST_FORMAT_VERSION,
    OUTCOME_DONE,
    ManifestError,
    canonical_json_bytes,
    compute_input_digest,
    is_internal_namespace_path,
    manifest_relative_path,
    validate_input_fingerprints,
    validate_manifest,
    validate_output_path,
)
from .step_scope import JOB_SCOPE, part_id_from_scope

# staging 前缀/commit 记录文件名的单一来源在 storage(其内部路径构造与清理直接使用);
# 这里 re-export 供 worker/api 引用,避免双向依赖(storage 不 import 本模块)。
from .storage import (
    COMMIT_RECORD_FILENAME,
    EXECUTION_STAGING_PREFIX,
    is_credential_file,
)

# 单输出大小上限,对齐 runner 网关产物上限(超过无法经 gateway 往返)。
MAX_OUTPUT_FILE_BYTES = 10 * 1024 * 1024 * 1024

# candidate 采集文件(步骤子进程写,Worker 消费):独立于 .done,双写期互不影响。
CANDIDATE_FORMAT = "flori-step-candidate"
CANDIDATE_FORMAT_VERSION = 1
CANDIDATE_MAX_BYTES = 1024 * 1024


class StepOutputError(ValueError):
    """输出所有权/校验违规;Worker 据此把成功退出的步骤降级为失败,不发布 manifest。"""


class StaleCommitError(RuntimeError):
    """commit fence 拒绝当前执行(generation/exec/租约已换);不得上报 done。"""


def candidate_filename(step: str) -> str:
    return f".{step}.manifest-candidate.json"


def _assert_safe_segment(value: str, field: str) -> str:
    if (
        type(value) is not str
        or not value
        or len(value) > 200
        or "/" in value or "\\" in value or "\x00" in value
        or value in (".", "..")
    ):
        raise ValueError(f"{field}: unsafe path segment {value!r}")
    return value


def execution_staging_prefix(job_id: str, exec_id: str) -> str:
    """执行 staging 的相对前缀(相对存储根,非 job 根)。exec_id 含 ':',Linux/MinIO 合法。"""
    _assert_safe_segment(job_id, "job_id")
    _assert_safe_segment(exec_id, "exec_id")
    return f"{EXECUTION_STAGING_PREFIX}{job_id}/{exec_id}/"


@dataclass(frozen=True)
class StepOutput:
    """一个已校验的候选输出。path 相对 scope 根;job_rel 相对 job 根(即对象键去掉 job_id)。"""

    path: str
    job_rel: str
    size_bytes: int
    sha256: str
    media_type: str | None


def _scope_prefix(scope_key: str) -> str:
    part_id = part_id_from_scope(scope_key)
    return "" if part_id is None else f"parts/{part_id}/"


def _is_runtime_sidecar(rel: str) -> bool:
    """步骤生命周期 dotfile(.{step}.done/.meta/.error/.progress/.config/candidate)不作为业务输出。"""
    name = rel.rsplit("/", 1)[-1]
    return name.startswith(".")


def expand_step_outputs(
    work_dir: Path,
    outputs_globs: list[str],
    *,
    scope_key: str,
    exclude_paths: set[str] | None = None,
) -> list[str]:
    """按 outputs glob(fnmatch 语义,与前端分组/provenance 校验一致)展开精确输出相对路径。

    返回 scope 相对路径升序列表。exclude_paths 是 scope 相对路径(如 NAS 源
    input/source.mp4),命中即整体跳过(含 symlink)。其余违规 fail-closed:
    symlink/非普通文件/凭证文件/路径越界/超大小上限 全部抛 StepOutputError。
    """
    if not isinstance(outputs_globs, list) or not all(
        type(item) is str and item for item in outputs_globs
    ):
        raise StepOutputError("outputs: must be a list of non-empty glob strings")
    excluded = exclude_paths or set()
    scope_kind = "job" if scope_key == JOB_SCOPE else "part"
    matched: list[str] = []
    for path in sorted(work_dir.rglob("*")):
        try:
            rel = path.relative_to(work_dir).as_posix()
        except ValueError:
            continue
        if is_internal_namespace_path(rel):
            continue
        if scope_kind == "job" and rel.startswith("parts/"):
            continue  # Part 领地由 Part manifest 拥有,job 步 glob 不越界(§2.2)
        if not any(fnmatch.fnmatch(rel, pattern) for pattern in outputs_globs):
            continue
        if rel in excluded:
            continue
        # 凭证判定先于 sidecar 跳过:.credentials.json 也是 dotfile,不得被静默略过。
        if is_credential_file(rel):
            raise StepOutputError(f"output matches credential sidecar: {rel}")
        if _is_runtime_sidecar(rel):
            continue
        if path.is_symlink():
            raise StepOutputError(f"output is a symlink: {rel}")
        if path.is_dir():
            continue
        st = path.lstat()
        if not stat_module.S_ISREG(st.st_mode):
            raise StepOutputError(f"output is not a regular file: {rel}")
        try:
            validate_output_path(rel, scope_kind=scope_kind)
        except ManifestError as exc:
            raise StepOutputError(str(exc)) from exc
        if st.st_size > MAX_OUTPUT_FILE_BYTES:
            # 超限输出按 NO_PUSH 同款豁免(审查 P2-5):不进 manifest、不失败步骤
            # (gateway 产物上限本就无法往返);契约收敛留 Unit C,绝不把成功步骤打失败。
            continue
        matched.append(rel)
    return sorted(matched, key=lambda item: item.encode("utf-8"))


def hash_output_file(path: Path, *, chunk_size: int = 1024 * 1024) -> tuple[int, str]:
    """流式 SHA-256;返回 (size, sha256:{hex})。哈希期间文件被替换由 read-back 兜底。"""
    digest = hashlib.sha256()
    total = 0
    with open(path, "rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
            total += len(chunk)
    return total, f"sha256:{digest.hexdigest()}"


def collect_step_outputs(
    work_dir: Path,
    outputs_globs: list[str],
    *,
    scope_key: str,
    exclude_paths: set[str] | None = None,
    path_filter=None,
) -> list[StepOutput]:
    """展开 + 流式哈希,产出已按 UTF-8 path 升序的 StepOutput 列表。

    path_filter(job_rel)->bool 返回 False 的路径在哈希前剔除:供 gateway NO_PUSH
    大源文件豁免(中心无副本,manifest 不得声明为可校验输出;Unit C 统一契约)。
    """
    prefix = _scope_prefix(scope_key)
    result: list[StepOutput] = []
    for rel in expand_step_outputs(
        work_dir, outputs_globs, scope_key=scope_key, exclude_paths=exclude_paths,
    ):
        if path_filter is not None and not path_filter(f"{prefix}{rel}"):
            continue
        path = work_dir / rel
        size, sha = hash_output_file(path)
        media_type = mimetypes.guess_type(rel)[0]
        result.append(StepOutput(
            path=rel, job_rel=f"{prefix}{rel}", size_bytes=size,
            sha256=sha, media_type=media_type,
        ))
    return result


# candidate 采集文件:步骤子进程成功(执行或幂等跳过)时写,只含子进程独有的事实
# (input fingerprints);manifest 组装与摘要计算在 Worker 侧单点完成。


def build_candidate_record(
    step: str, input_fingerprints: dict[str, str], *, reused: bool = False,
) -> dict:
    """reused=True 表示子进程幂等跳过(未重新执行):Worker 据此在中心 manifest
    仍与当前 input/definition digest 相同时省去整套重发 IO(审查 P3-7)。"""
    return {
        "format": CANDIDATE_FORMAT,
        "format_version": CANDIDATE_FORMAT_VERSION,
        "step": step,
        "exec_id": os.environ.get("STEP_EXEC_ID", ""),
        "input_fingerprints": validate_input_fingerprints(input_fingerprints),
        "reused": bool(reused),
        "written_at": datetime.now(timezone.utc).isoformat(),
    }


def load_candidate_record(work_dir: Path, step: str) -> dict | None:
    """读取并校验 candidate 采集文件;缺失/损坏返回 None(dual 阶段保守跳过 manifest)。"""
    path = work_dir / candidate_filename(step)
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if len(raw) > CANDIDATE_MAX_BYTES:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if (
        type(data) is not dict
        or data.get("format") != CANDIDATE_FORMAT
        or data.get("format_version") != CANDIDATE_FORMAT_VERSION
        or data.get("step") != step
    ):
        return None
    try:
        validate_input_fingerprints(data.get("input_fingerprints"))
    except ManifestError:
        return None
    return data


def build_step_manifest(
    *,
    job_id: str,
    scope_key: str,
    step: str,
    part_index: int | None,
    exec_id: str,
    job_generation: int,
    attempt: int,
    started_at: str,
    committed_at: str,
    duration_sec: float,
    input_fingerprints: dict[str, str],
    definition_digest: str,
    outputs: list[StepOutput],
    producer: dict,
) -> tuple[dict, bytes, str]:
    """组装并校验 final manifest;返回 (manifest, canonical bytes, manifest digest)。

    manifest digest 即 commit fence 的 candidate_digest:token 与 manifest 字节内容
    一一绑定,promote/publish/done 全程携带同一身份。
    """
    part_id = part_id_from_scope(scope_key)
    scope_block: dict = {
        "kind": "job" if part_id is None else "part",
        "scope_key": scope_key,
        "part_id": part_id,
        "part_index": part_index if part_id is not None else None,
    }
    manifest = {
        "format": MANIFEST_FORMAT,
        "format_version": MANIFEST_FORMAT_VERSION,
        "job_id": job_id,
        "scope": scope_block,
        "step": step,
        "outcome": OUTCOME_DONE,
        "execution": {
            "exec_id": exec_id,
            "job_generation": job_generation,
            "attempt": attempt,
            "started_at": started_at,
            "committed_at": committed_at,
            "duration_sec": round(float(duration_sec), 3),
        },
        "compatibility": {
            "input_fingerprints": dict(input_fingerprints),
            "input_digest": compute_input_digest(dict(input_fingerprints)),
            "definition_digest": definition_digest,
        },
        "producer": producer,
        "outputs": [
            {
                "path": entry.path,
                "size_bytes": entry.size_bytes,
                "sha256": entry.sha256,
                "media_type": entry.media_type,
            }
            for entry in outputs
        ],
        "skip": None,
    }
    encoded = validate_manifest(manifest)
    digest = "sha256:" + hashlib.sha256(encoded).hexdigest()
    return manifest, encoded, digest


async def read_previous_manifest(storage, job_id: str, scope_key: str, step: str) -> dict | None:
    """读上一份已发布 manifest(算精确删除集用);缺失或损坏返回 None,损坏不阻塞新提交。"""
    rel = manifest_relative_path(scope_key, step)
    try:
        raw = await storage.read_file(job_id, rel)
    except Exception:
        return None
    if raw is None:
        return None
    try:
        data = json.loads(raw)
        validate_manifest(data)
    except (ValueError, ManifestError):
        return None
    return data


def stale_output_paths(previous_manifest: dict | None, new_outputs: list[StepOutput]) -> list[str]:
    """按旧 manifest 精确计算本次不再产生的旧输出(job 相对路径),promote 后删除(§2.6-6)。"""
    if previous_manifest is None:
        return []
    scope_key = previous_manifest["scope"]["scope_key"]
    prefix = _scope_prefix(scope_key)
    kept = {entry.job_rel for entry in new_outputs}
    stale = []
    for entry in previous_manifest["outputs"]:
        job_rel = f"{prefix}{entry['path']}"
        if job_rel not in kept:
            stale.append(job_rel)
    return stale


def build_commit_record(
    *,
    job_id: str,
    execution_step: str,
    exec_id: str,
    token: dict,
    manifest_digest: str,
    output_job_rels: list[str],
) -> bytes:
    """第一次 promote 前写入 staging namespace 的持久 commit 记录(§2.7 行 1/2 区分事实)。"""
    record = {
        "format": "flori-step-commit-record",
        "format_version": 1,
        "job_id": job_id,
        "execution_step": execution_step,
        "exec_id": exec_id,
        "token_id": token.get("token_id"),
        "job_generation": token.get("job_generation"),
        "candidate_digest": manifest_digest,
        "promote_started": True,
        "outputs": list(output_job_rels),
        "written_at": datetime.now(timezone.utc).isoformat(),
    }
    return canonical_json_bytes(record)


def diagnostics_globs(
    step: str, scope_key: str, *, audit_globs: list[str] | None = None,
) -> list[str]:
    """失败/超时路径允许回传中心的白名单(job 相对 fnmatch glob):
    AI 审计 namespace、运行日志、progress/meta/error 生命周期文件,
    外加 step 声明的 output_policy.audit_globs(scope 相对,配置即语义)。
    业务输出一律不推。"""
    prefix = _scope_prefix(scope_key)
    result = [
        f"{prefix}output/ai_logs/*",
        f"{prefix}logs/*",
        f"{prefix}.{step}.progress",
        f"{prefix}.{step}.meta.json",
        f"{prefix}.{step}.error.json",
    ]
    for pattern in audit_globs or []:
        if isinstance(pattern, str) and pattern:
            result.append(f"{prefix}{pattern}")
    return result
