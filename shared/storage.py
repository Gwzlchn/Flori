"""StorageBackend:统一文件访问接口。

LocalStorage:数据在本机,pull/push 为 no-op(work_dir 即真实 job 目录)。
RemoteStorage:对象存储(MinIO/S3),让任意机器都能当 worker——
  pull 把该 job 现有产物下载到本机临时 work_dir,步骤照常读写本地路径,
  push 把本步新增/改动的文件回传对象存储(只增量上传、不删,避免并行分支互相覆盖)。
"""

from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import hmac
import io
import json
import os
import queue
import re
import shutil
import stat
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterable, AsyncIterator, Callable, Protocol

from shared.errors import WorkerAuthRejected
from shared.runner_ops import current_task_lease
from shared.step_manifest import (
    ManifestError,
    is_internal_namespace_path,
    validate_manifest,
)


# B站登录态等敏感凭证的本地侧载文件:只供同机(LocalStorage)下载步本地读取,
# 绝不入中心对象存储、绝不经 runner 网关下发给远端 worker(见 RemoteStorage / api/routes/runner.py)。
CREDENTIAL_REL = "input/.credentials.json"
INITIALIZATION_MARKER_REL = ".flori-initializing.json"
_STAGING_PREFIX = ".flori-upload/"
_GLOBAL_STAGING_PREFIX = ".flori-staging/"
_GLOBAL_INITIALIZATION_PREFIX = ".flori-initializing/"
_STAGING_LIFECYCLE_RULE_ID = "flori-staging-recovery"
_MINIO_STREAM_PART_SIZE = 5 * 1024 * 1024
# S3/MinIO 单次 copy_object 上限;超过必须 compose_object 分段服务端拷贝。
_MINIO_COPY_LIMIT = 5 * 1024 * 1024 * 1024
_STREAM_EOF = object()

# 执行 staging 命名空间(设计稿 §2.6):对象键 .flori/staging/{job_id}/{exec_id}/{job_rel}。
# 处于 .flori 内部命名空间,永不作为业务产物 push/pull/list/clone(见 _is_internal_file)。
EXECUTION_STAGING_PREFIX = ".flori/staging/"
# commit 记录:第一次 promote 前写入执行 staging 根,含 promote_started,
# 供 §2.7 恢复决策表(RecoveryFacts.promote_started)观察半提交窗口。
COMMIT_RECORD_FILENAME = ".commit.json"

_SAFE_SEGMENT_RE = re.compile(r"^[^/\\\x00]{1,200}$")


def _assert_staging_segment(value: str, field: str) -> str:
    """staging 路径段校验:exec_id 含 ':'(Linux/MinIO 合法),仍禁分隔符/NUL/穿越段。"""
    if (
        type(value) is not str
        or value in (".", "..")
        or not _SAFE_SEGMENT_RE.fullmatch(value)
    ):
        raise ValueError(f"{field}: unsafe staging segment {value!r}")
    return value


class ArtifactTooLarge(ValueError):
    pass


class StepCommitFenceRejected(RuntimeError):
    """commit token 校验失败(过期/换代/被轮换);中止提交,不发布 manifest,不上报 done。"""


class StepCommitIntegrityError(ValueError):
    """read-back 或 manifest digest 与 token 绑定不符;提交中止,不发布 manifest。"""


@dataclass(frozen=True)
class _StreamFailure:
    error: BaseException


class _AsyncToSyncStream:
    """以有界队列把 ASGI async chunks 接到 MinIO 同步 multipart reader。"""

    def __init__(self, max_chunks: int = 2):
        self._queue: queue.Queue = queue.Queue(maxsize=max_chunks)
        self._buffer = bytearray()
        self._stopped = threading.Event()
        self._eof = False

    def _put(self, item: object) -> None:
        while not self._stopped.is_set():
            try:
                self._queue.put(item, timeout=0.1)
                return
            except queue.Full:
                continue
        raise OSError("object upload stream stopped")

    async def put(self, chunk: bytes) -> None:
        await asyncio.to_thread(self._put, chunk)

    async def finish(self) -> None:
        await asyncio.to_thread(self._put, _STREAM_EOF)

    def _fail(self, error: BaseException) -> None:
        if self._stopped.is_set():
            return
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
        try:
            self._queue.put_nowait(_StreamFailure(error))
        except queue.Full:
            pass

    async def fail(self, error: BaseException) -> None:
        await asyncio.to_thread(self._fail, error)

    def abort(self, error: BaseException) -> None:
        self._fail(error)

    def stop(self) -> None:
        self._stopped.set()

    def read(self, size: int = -1) -> bytes:
        if size == 0:
            return b""
        if size < 0:
            size = _MINIO_STREAM_PART_SIZE
        while len(self._buffer) < size and not self._eof:
            item = self._queue.get()
            if item is _STREAM_EOF:
                self._eof = True
                break
            if isinstance(item, _StreamFailure):
                raise item.error
            if not isinstance(item, bytes):
                raise TypeError("artifact stream yielded non-bytes")
            self._buffer.extend(item)
        data = bytes(self._buffer[:size])
        del self._buffer[:size]
        return data


@dataclass(frozen=True)
class StorageObjectVersion:
    """可用于重验 memo 的可信对象版本;token 必须随对象替换而变化。"""

    namespace: str
    size: int
    token: str


def read_path_bounded(
    path: Path,
    max_bytes: int,
    *,
    chunk_size: int = 256 * 1024,
    trusted_root: Path | None = None,
) -> bytes:
    """从同一文件描述符最多读取 limit+1 字节,并拒绝目录逃逸与读中替换。"""
    if type(max_bytes) is not int or max_bytes < 0:
        raise ValueError("max_bytes must be a non-negative integer")
    if type(chunk_size) is not int or chunk_size <= 0:
        raise ValueError("chunk_size must be a positive integer")
    if trusted_root is None:
        fd = _open_path_nofollow(path)
        reopen = lambda: _open_path_nofollow(path)
    else:
        fd = _open_path_beneath(path, trusted_root)
        reopen = lambda: _open_path_beneath(path, trusted_root)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise OSError("artifact is not a regular file")
        data = bytearray()
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(fd, min(chunk_size, remaining))
            if not chunk:
                break
            data.extend(chunk)
            remaining -= len(chunk)

        after = os.fstat(fd)
        try:
            check_fd = reopen()
        except OSError as exc:
            raise OSError("artifact changed while reading") from exc
        try:
            current = os.fstat(check_fd)
        finally:
            os.close(check_fd)
        if not stat.S_ISREG(current.st_mode):
            raise OSError("artifact changed while reading")
        if (after.st_dev, after.st_ino) != (current.st_dev, current.st_ino):
            raise OSError("artifact changed while reading")

        def _snapshot(value):
            return (
                value.st_dev,
                value.st_ino,
                value.st_size,
                value.st_mtime_ns,
                value.st_ctime_ns,
            )

        if _snapshot(before) != _snapshot(after):
            raise OSError("artifact changed while reading")
        if len(data) <= max_bytes and len(data) != after.st_size:
            raise OSError("artifact changed while reading")
        return bytes(data)
    finally:
        os.close(fd)


def _open_path_nofollow(path: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    return os.open(path, flags)


def _open_path_beneath(path: Path, trusted_root: Path) -> int:
    """用 openat 逐段拒绝符号链接,使父目录竞态也不能逃出可信根。"""
    root_input = Path(os.path.abspath(trusted_root))
    candidate = Path(os.path.abspath(path))
    try:
        rel = candidate.relative_to(root_input)
    except ValueError as exc:
        raise OSError("artifact escapes trusted root") from exc
    if not rel.parts or any(part in {"", ".", ".."} for part in rel.parts):
        raise OSError("artifact path is invalid")

    root = root_input.resolve(strict=True)
    dir_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    current = os.open(root, dir_flags)
    try:
        for part in rel.parts[:-1]:
            next_fd = os.open(part, dir_flags, dir_fd=current)
            os.close(current)
            current = next_fd
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        return os.open(rel.parts[-1], flags, dir_fd=current)
    finally:
        os.close(current)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_path_atomic(path: Path, data: bytes) -> None:
    """在目标目录完成 fsync 后原子替换,失败不暴露半写文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, staging_name = tempfile.mkstemp(
        prefix=f".{path.name}.flori-part-", dir=path.parent,
    )
    staging = Path(staging_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(staging, path)
        _fsync_directory(path.parent)
    finally:
        staging.unlink(missing_ok=True)


def publish_content_addressed_path(path: Path, data: bytes) -> None:
    """原子发布内容寻址文件;已存在时只接受逐字节相同内容。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, staging_name = tempfile.mkstemp(
        prefix=f".{path.name}.flori-part-", dir=path.parent,
    )
    staging = Path(staging_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(staging, path, follow_symlinks=False)
        except FileExistsError:
            try:
                existing = read_path_bounded(path, len(data), trusted_root=path.parent)
            except OSError as exc:
                raise ValueError("content-addressed artifact is unsafe") from exc
            if existing != data:
                raise ValueError("content-addressed artifact collision")
        else:
            _fsync_directory(path.parent)
    finally:
        staging.unlink(missing_ok=True)


async def read_file_bounded(
    storage: "StorageBackend",
    job_id: str,
    rel_path: str,
    max_bytes: int,
    *,
    chunk_size: int = 256 * 1024,
) -> bytes | None:
    """用 size + range stream 有界读取;超限返回 limit+1 哨兵供上层降级。"""
    if type(max_bytes) is not int or max_bytes < 0:
        raise ValueError("max_bytes must be a non-negative integer")
    size = await storage.file_size(job_id, rel_path)
    if size is not None:
        if type(size) is not int or size < 0:
            raise ValueError("artifact size metadata is invalid")
        if size > max_bytes:
            return b"\0" * (max_bytes + 1)
    stream = await storage.open_stream(
        job_id, rel_path, length=max_bytes + 1,
        chunk_size=min(max(chunk_size, 1), max_bytes + 1 or 1),
    )
    if stream is None:
        return None
    data = bytearray()
    try:
        async for chunk in stream:
            if not isinstance(chunk, bytes):
                raise ValueError("artifact stream yielded non-bytes")
            remaining = max_bytes + 1 - len(data)
            if remaining <= 0:
                break
            data.extend(chunk[:remaining])
            if len(data) > max_bytes:
                break
        return bytes(data)
    finally:
        close = getattr(stream, "aclose", None)
        if callable(close):
            await close()


async def sha256_file(
    storage: "StorageBackend",
    job_id: str,
    rel_path: str,
    *,
    chunk_size: int = 1024 * 1024,
) -> str | None:
    """流式计算对象 SHA-256；不会把媒体或 PDF 整体读入内存。"""
    stream = await storage.open_stream(job_id, rel_path, chunk_size=chunk_size)
    if stream is None:
        return None
    digest = hashlib.sha256()
    try:
        async for chunk in stream:
            if not isinstance(chunk, bytes):
                raise ValueError("artifact stream yielded non-bytes")
            digest.update(chunk)
    finally:
        close = getattr(stream, "aclose", None)
        if callable(close):
            await close()
    return digest.hexdigest()


def verification_artifact_limit(rel_path: str) -> int:
    """返回评审/取证重验路径的单一字节上限。"""
    from shared.evidence_contract import MAX_EVIDENCE_BYTES, MAX_MECHANICAL_EVIDENCE_BYTES
    from shared.review_contract import MAX_REVIEW_SOURCE_BYTES

    if rel_path.startswith("output/evidence/evidence-") or rel_path == "output/evidence.json":
        return MAX_EVIDENCE_BYTES
    if rel_path == "output/notes_mechanical.md":
        return MAX_MECHANICAL_EVIDENCE_BYTES
    return MAX_REVIEW_SOURCE_BYTES


async def read_verification_artifact_bounded(
    storage: "StorageBackend", job_id: str, rel_path: str,
) -> bytes | None:
    """有界读取重验输入;存储故障统一成 verifier 可降级的 OSError。"""
    try:
        return await read_file_bounded(
            storage, job_id, rel_path, verification_artifact_limit(rel_path),
        )
    except asyncio.CancelledError:
        raise
    except (OSError, ValueError):
        raise
    except Exception as exc:
        raise OSError("verification artifact read failed") from exc


def is_credential_file(rel: str) -> bool:
    """是否为敏感凭证侧载文件(按 basename 判,跨平台)。"""
    return rel.replace("\\", "/").rsplit("/", 1)[-1] == ".credentials.json"


def execution_artifact_allowed(
    execution_step: str, rel_path: str, *, write: bool,
) -> bool:
    """按执行scope限制远端worker可见和可发布的对象前缀。"""
    from shared.step_scope import parse_execution_step, part_id_from_scope

    normalized = rel_path.replace("\\", "/")
    scope_key, _ = parse_execution_step(execution_step)
    part_id = part_id_from_scope(scope_key)
    if part_id is not None:
        return normalized.startswith(f"parts/{part_id}/")
    return not (write and normalized.startswith("parts/"))


def _is_staging_file(rel: str) -> bool:
    return rel.replace("\\", "/").startswith(_STAGING_PREFIX)


def _is_internal_file(rel: str) -> bool:
    # .flori 内部命名空间(manifest/staging)不作为业务产物往返:push/pull/list/clone 一律隔离。
    normalized = rel.replace("\\", "/")
    return (
        normalized == INITIALIZATION_MARKER_REL
        or _is_staging_file(normalized)
        or is_internal_namespace_path(normalized)
    )


def _canonical_manifest_bytes(manifest: dict, token: dict) -> bytes:
    """校验 final manifest 并核对与 commit token 的 candidate 绑定;两处(worker/中心)同套防线。"""
    try:
        encoded = validate_manifest(manifest)
    except ManifestError as exc:
        raise StepCommitIntegrityError(f"manifest invalid: {exc}") from exc
    digest = "sha256:" + hashlib.sha256(encoded).hexdigest()
    if digest != token.get("candidate_digest"):
        raise StepCommitIntegrityError(
            "manifest digest does not match commit token candidate_digest"
        )
    return encoded


def _guard_commit_path(execution_step: str, rel: str, kind: str) -> None:
    """promote/删除环的 scope 边界纵深防御:越权路径即围栏拒绝(审查 P1)。"""
    if (
        type(rel) is not str
        or not rel
        or _is_internal_file(rel)
        or is_credential_file(rel)
        or not execution_artifact_allowed(execution_step, rel, write=True)
    ):
        raise StepCommitFenceRejected(
            f"{kind} path outside execution scope: {rel!r}"
        )


def _verify_manifest_binding(
    manifest: dict, *, job_id: str, execution_step: str, exec_id: str,
    outputs: list[dict],
) -> list[dict]:
    """manifest 发布前交叉校验(审查 P2-3):身份等于租约执行身份,声明输出集与实际
    已 promote 集在 path/size/sha256 一一相等;返回声明集(job 相对),read-back 以此为准。"""
    from shared.step_scope import parse_execution_step, part_id_from_scope

    scope_key, template_step = parse_execution_step(execution_step)
    if manifest.get("job_id") != job_id:
        raise StepCommitIntegrityError("manifest job_id does not match execution")
    if manifest["scope"]["scope_key"] != scope_key:
        raise StepCommitIntegrityError("manifest scope does not match execution")
    if manifest["step"] != template_step:
        raise StepCommitIntegrityError("manifest step does not match execution")
    if manifest["execution"]["exec_id"] != exec_id:
        raise StepCommitIntegrityError("manifest exec_id does not match execution")
    part_id = part_id_from_scope(scope_key)
    prefix = f"parts/{part_id}/" if part_id else ""
    declared = [
        {
            "path": f"{prefix}{entry['path']}",
            "size_bytes": entry["size_bytes"],
            "sha256": entry["sha256"],
        }
        for entry in manifest["outputs"]
    ]
    declared_set = {
        (entry["path"], entry["size_bytes"], entry["sha256"]) for entry in declared
    }
    provided_set = {
        (entry.get("path"), entry.get("size_bytes"), entry.get("sha256"))
        for entry in outputs
    }
    if declared_set != provided_set or len(declared) != len(outputs):
        raise StepCommitIntegrityError(
            "manifest outputs do not match committed output set"
        )
    return declared


async def _verify_commit_token(verify_token, phase: str = "") -> None:
    if not await verify_token(phase):
        raise StepCommitFenceRejected("commit token is no longer valid")


def _is_s3_not_found(error: BaseException) -> bool:
    return (
        getattr(error, "code", None) in {
            "NoSuchKey", "NoSuchObject", "NoSuchVersion",
        }
        or getattr(getattr(error, "response", None), "status", None) == 404
    )


def _raise_gateway_auth(resp, endpoint: str) -> None:
    status = getattr(resp, "status_code", None)
    if status in (401, 403, 429):
        raise WorkerAuthRejected(status_code=status, endpoint=endpoint)


def _parse_minio_version(info: dict) -> str | None:
    """从 MinioAdmin.info() 的 JSON 取服务端版本。
    实测响应无顶层 version,版本在 servers[].version(形如 RELEASE.xxx 或 2025-09-07T16:13:09Z)。
    优先顶层(兼容未来/其他实现),否则取首个带 version 的 server。取不到返回 None。"""
    if not isinstance(info, dict):
        return None
    top = info.get("version")
    if isinstance(top, str) and top:
        return top
    for srv in info.get("servers") or []:
        if isinstance(srv, dict):
            v = srv.get("version")
            if isinstance(v, str) and v:
                return v
    return None


class StorageBackend(Protocol):
    async def pull(self, job_id: str, step: str) -> Path: ...
    # only_globs:失败/超时路径的诊断白名单(fnmatch,job 相对);None=全推(成功路径既有语义)。
    async def push(
        self, job_id: str, step: str, work_dir: Path, *,
        exclude_paths: set[str] | None = None,
        only_globs: list[str] | None = None,
    ) -> None: ...
    async def cleanup(self, job_id: str, step: str, work_dir: Path) -> None: ...
    # 步骤产物提交协议(设计稿 §2.6):candidate 先进执行 staging namespace,
    # commit token 保护下 promote 到 canonical,read-back 通过后 manifest 最后原子发布。
    # source=None 表示中心侧按 canonical 现状复制(stage_from_canonical 语义)。
    async def stage_step_output(
        self, job_id: str, exec_id: str, rel_path: str, source: Path | None, *,
        size_bytes: int, sha256: str,
    ) -> None: ...
    # 中心侧服务:canonical 对象存在且 size 匹配时服务端复制进 staging;返回是否成功。
    async def stage_from_canonical(
        self, job_id: str, exec_id: str, rel_path: str, *, size_bytes: int,
    ) -> bool: ...
    # 中心侧服务:gateway staging 上传落盘(分块校验后进入执行 staging namespace)。
    async def write_execution_staging_stream(
        self, job_id: str, exec_id: str, rel_path: str, chunks: AsyncIterable[bytes], *,
        expected_size: int | None = None, expected_sha256: str | None = None,
        max_bytes: int | None = None,
    ) -> dict: ...
    # 九步协议的 6/7 步:commit 记录先落 staging(promote_started 持久证据)→ 每个输出
    # promote 前后 verify_token → read-back(size/sha256)→ 按旧 manifest 精确删旧输出 →
    # manifest 最后原子发布。任一步失败抛错且不发布 manifest。
    async def commit_step_outputs(
        self, job_id: str, execution_step: str, exec_id: str, *,
        outputs: list[dict], manifest: dict, manifest_rel: str,
        stale_paths: list[str], token: dict, commit_record: bytes,
        verify_token,
    ) -> None: ...
    async def cleanup_execution_staging(self, job_id: str, exec_id: str) -> None: ...
    # staging TTL 清理:active_exec_ids 保护在途执行,stale_before_epoch 之前的孤儿清除。
    async def cleanup_stale_execution_staging(
        self, *, active_exec_ids: set[str], stale_before_epoch: float,
    ) -> int: ...
    # 删 job 时清掉该 job 的全部产物:LocalStorage 删 job 目录、RemoteStorage 删 {job_id}/ 前缀对象。
    # 幂等(无产物即 no-op),避免 MinIO/分布式部署删 job 后中心存储留孤儿产物。
    async def delete(
        self, job_id: str, *, defer_if_busy: bool = False,
    ) -> None: ...
    # 把 src job 的全部产物 + .done 复制到 dst job,供 fork 重建播种新快照,只重跑分叉步及下游。
    # 排除凭证侧载文件;Remote 任一对象失败即整体失败,不得发布不完整快照;Gateway 不支持。
    async def clone(self, src_job_id: str, dst_job_id: str) -> None: ...
    # 删单个产物(scheduler rerun 清中心 .done 用):幂等,文件不存在即 no-op。
    # 只删本地 jobs_dir 的 .done 在 MinIO 部署下是 no-op → worker pull 回旧 .done 指纹命中跳过,
    # rerun/「重跑该步」整体失效——中心存储必须同步删。Gateway 不支持(中心产物变更在 API/中心侧)。
    async def delete_file(self, job_id: str, rel_path: str) -> None: ...
    # 供 api 按需取单个产物(笔记/日志等);找不到返回 None。
    async def read_file(self, job_id: str, rel_path: str) -> bytes | None: ...
    # 大制品走分块读取;返回 None 表示不存在.调用方负责完整消费或关闭生成器.
    async def open_stream(
        self, job_id: str, rel_path: str, *, start: int = 0,
        length: int | None = None, chunk_size: int = 1024 * 1024,
    ) -> AsyncIterator[bytes] | None: ...
    # 供 api 写入 job 初始文件(job.json、上传源文件等),worker 才能 pull 到。
    async def write_file(self, job_id: str, rel_path: str, data: bytes) -> None: ...
    # 分块写暂存文件/对象,校验完成后原子替换目标;失败不得暴露半制品.
    async def write_stream(
        self, job_id: str, rel_path: str, chunks: AsyncIterable[bytes], *,
        expected_size: int | None = None, expected_sha256: str | None = None,
        max_bytes: int | None = None, staging_token: str | None = None,
    ) -> dict: ...
    # API 初始化恢复:枚举 marker,并按保守时间界限清全局 staging。
    async def list_initialization_markers(self) -> list[str]: ...
    async def cleanup_stale_staging(
        self, *, active_tokens: set[tuple[str, str]],
        protected_job_ids: set[str], stale_before_epoch: float,
    ) -> int: ...
    # API shutdown 在有界时间内等待后台 multipart finalizer 与其 delete barrier。
    async def wait_for_finalizers(self) -> None: ...
    # 列出某 job 的全部产物相对路径(供 gateway 产物清单端点 / GatewayStorage.pull 用)。
    async def list_files(self, job_id: str) -> list[str]: ...
    # 列产物相对路径 → 字节大小(供 /artifacts 透出每步/每 job 产物体积)。一次列举拿全,
    # 不对每文件逐个 stat(本地 rglob 自带 st_size、MinIO list_objects 自带 obj.size)。
    async def list_file_sizes(self, job_id: str) -> dict[str, int]: ...
    # 供 api range 流式播放视频/音频:取文件大小 + 读指定字节区间。找不到返回 None。
    async def file_size(self, job_id: str, rel_path: str) -> int | None: ...
    # 只供内容重验 memo 判定对象是否仍是同一版本。无可信版本或不存在时返回 None。
    async def object_version(
        self, job_id: str, rel_path: str,
    ) -> StorageObjectVersion | None: ...
    async def read_range(self, job_id: str, rel_path: str, start: int, length: int) -> bytes | None: ...
    # 健康探活(供 /api/status 的 minio 组件):返回 {status, mode, bucket, ...};不抛(异常由调用方包超时)。
    async def health(self) -> dict: ...
    # 容量统计(对象数 + 总字节):RemoteStorage 全量 list 求和很贵,故 api 侧带缓存+后台刷新,
    # 绝不同步阻塞 /api/status。不支持(本地不强求)返回 None。
    async def capacity(self) -> dict | None: ...


class LocalStorage:
    """本地部署:数据就在本机,pull/push 默认 no-op(work_dir 即 canonical job 目录)。

    FLORI_LOCAL_ATTEMPT_ISOLATION=1 时启用隔离 attempt(设计稿 §2.6):pull 复制
    committed 视图到独立工作目录(真实复制,不以可写 hardlink 暴露 canonical),
    push 只回写快照外新增/改动的文件。默认关闭,保持既有语义(双写保守序)。
    """

    def __init__(self, jobs_dir: Path):
        self.jobs_dir = jobs_dir
        self._isolate = os.environ.get(
            "FLORI_LOCAL_ATTEMPT_ISOLATION", "",
        ) not in ("", "0", "false")
        # 隔离模式:pull 时记录快照(relpath -> (size, mtime)),push 只回写增量。
        self._snapshots: dict[str, dict[str, tuple[int, float]]] = {}

    def _safe_path(self, job_id: str, rel_path: str = "") -> Path:
        # 兜底防穿越:job_id 不得逃出 jobs_dir、rel 不得逃出其 job 目录,
        # 挡持 token 者经 job_id/rel 里的 ".." 读写中心数据。
        # 空字节(null byte)会让 pathlib.resolve() / os 抛 ValueError(裸传即 500),在此与穿越一并拦成 ValueError。
        if "\x00" in job_id or "\x00" in rel_path:
            raise ValueError("null byte in path")
        root = self.jobs_dir.resolve()
        job_root = (root / job_id).resolve()
        if job_root != root and root not in job_root.parents:
            raise ValueError("path escapes jobs_dir")
        path = (job_root / rel_path).resolve()
        if path != job_root and job_root not in path.parents:
            raise ValueError("path escapes job dir")
        return path

    def _staging_path(self, job_id: str, token: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", token):
            raise ValueError("invalid staging token")
        self._safe_path(job_id)
        root = self.jobs_dir.resolve()
        staging_root = (root / _GLOBAL_STAGING_PREFIX / job_id).resolve()
        if root not in staging_root.parents:
            raise ValueError("staging path escapes jobs_dir")
        return staging_root / token

    def _initialization_path(self, job_id: str) -> Path:
        self._safe_path(job_id)
        root = self.jobs_dir.resolve()
        return root / _GLOBAL_INITIALIZATION_PREFIX / job_id / "marker.json"

    def _data_path(self, job_id: str, rel_path: str) -> Path:
        if rel_path == INITIALIZATION_MARKER_REL:
            return self._initialization_path(job_id)
        return self._safe_path(job_id, rel_path)

    async def pull(self, job_id: str, step: str) -> Path:
        canonical = self._safe_path(job_id)
        if not self._isolate:
            return canonical
        return await asyncio.to_thread(self._pull_isolated_sync, job_id, step, canonical)

    def _attempts_root(self) -> Path:
        return self.jobs_dir / ".flori" / "attempts"

    def _pull_isolated_sync(self, job_id: str, step: str, canonical: Path) -> Path:
        scope_token = hashlib.sha256(step.encode()).hexdigest()[:16]
        work_dir = self._attempts_root() / job_id / scope_token / uuid.uuid4().hex / "root"
        work_dir.mkdir(parents=True, exist_ok=True)
        snapshot: dict[str, tuple[int, float]] = {}
        if canonical.is_dir():
            for path in canonical.rglob("*"):
                if path.is_symlink() or not path.is_file():
                    continue
                rel = path.relative_to(canonical).as_posix()
                if _is_internal_file(rel):
                    continue
                dest = work_dir / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                # 真实复制而非 hardlink:hardlink 共享 inode,步骤原地写会击穿 canonical。
                shutil.copy2(path, dest)
                st = dest.stat()
                snapshot[rel] = (st.st_size, st.st_mtime)
        self._snapshots[str(work_dir)] = snapshot
        return work_dir

    async def push(
        self, job_id: str, step: str, work_dir: Path, *,
        exclude_paths: set[str] | None = None,
        only_globs: list[str] | None = None,
    ) -> None:
        if not self._isolate:
            return
        canonical = self._safe_path(job_id)
        if Path(work_dir).resolve() == canonical.resolve():
            return
        await asyncio.to_thread(
            self._push_isolated_sync, job_id, step, Path(work_dir),
            exclude_paths or set(), only_globs,
        )

    def _push_isolated_sync(
        self, job_id: str, step: str, work_dir: Path,
        exclude_paths: set[str], only_globs: list[str] | None,
    ) -> None:
        snapshot = self._snapshots.get(str(work_dir), {})
        for path in work_dir.rglob("*"):
            if path.is_symlink() or not path.is_file():
                continue
            rel = path.relative_to(work_dir).as_posix()
            if (
                rel in exclude_paths
                or is_credential_file(rel)
                or _is_internal_file(rel)
                or not execution_artifact_allowed(step, rel, write=True)
            ):
                continue
            if only_globs is not None and not any(
                fnmatch.fnmatch(rel, pattern) for pattern in only_globs
            ):
                continue
            st = path.stat()
            prev = snapshot.get(rel)
            if prev is not None and prev == (st.st_size, st.st_mtime):
                continue
            target = self._safe_path(job_id, rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(f".{target.name}.flori-part-{uuid.uuid4().hex}")
            try:
                shutil.copy2(path, tmp)
                os.replace(tmp, target)
            finally:
                tmp.unlink(missing_ok=True)

    async def cleanup(self, job_id: str, step: str, work_dir: Path) -> None:
        if not self._isolate:
            return
        self._snapshots.pop(str(work_dir), None)

        def _cleanup_attempt() -> None:
            attempt_dir = Path(work_dir).parent
            if self._attempts_root() not in attempt_dir.parents:
                return
            shutil.rmtree(attempt_dir, ignore_errors=True)
            for parent in (attempt_dir.parent, attempt_dir.parent.parent):
                try:
                    parent.rmdir()
                except OSError:
                    pass

        await asyncio.to_thread(_cleanup_attempt)

    async def wait_for_finalizers(self) -> None:
        pass

    # 步骤产物提交协议(§2.6)

    def _execution_staging_dir(self, job_id: str, exec_id: str) -> Path:
        self._safe_path(job_id)
        _assert_staging_segment(job_id, "job_id")
        _assert_staging_segment(exec_id, "exec_id")
        return self.jobs_dir / ".flori" / "staging" / job_id / exec_id

    @staticmethod
    def _staged_rel(root: Path, rel: str) -> Path:
        if (
            type(rel) is not str or not rel or rel.startswith("/")
            or "\x00" in rel or "\\" in rel
            or any(seg in ("", ".", "..") for seg in rel.split("/"))
        ):
            raise ValueError(f"invalid staging rel path: {rel!r}")
        return root / rel

    @staticmethod
    def _link_or_copy_replace(source: Path, target: Path) -> None:
        """staging<->canonical 之间的原子放置:优先同盘硬链接(零拷贝),失败退真实复制。"""
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.flori-promote-{uuid.uuid4().hex}")
        try:
            try:
                os.link(source, tmp, follow_symlinks=False)
            except OSError:
                shutil.copy2(source, tmp)
            os.replace(tmp, target)
            _fsync_directory(target.parent)
        finally:
            tmp.unlink(missing_ok=True)

    @staticmethod
    def _hash_file_sync(path: Path) -> tuple[int, str]:
        digest = hashlib.sha256()
        total = 0
        with open(path, "rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
                total += len(chunk)
        return total, f"sha256:{digest.hexdigest()}"

    async def stage_step_output(
        self, job_id: str, exec_id: str, rel_path: str, source: Path | None, *,
        size_bytes: int, sha256: str,
    ) -> None:
        if source is None:
            if not await self.stage_from_canonical(
                job_id, exec_id, rel_path, size_bytes=size_bytes,
            ):
                raise OSError(f"canonical object unavailable for staging: {rel_path}")
            return
        root = self._execution_staging_dir(job_id, exec_id)
        dest = self._staged_rel(root, rel_path)
        await asyncio.to_thread(self._link_or_copy_replace, Path(source), dest)

    async def stage_from_canonical(
        self, job_id: str, exec_id: str, rel_path: str, *, size_bytes: int,
    ) -> bool:
        def _copy() -> bool:
            try:
                src = self._safe_path(job_id, rel_path)
            except ValueError:
                return False
            try:
                st = src.lstat()
            except FileNotFoundError:
                return False
            if not stat.S_ISREG(st.st_mode) or st.st_size != size_bytes:
                return False
            dest = self._staged_rel(
                self._execution_staging_dir(job_id, exec_id), rel_path,
            )
            self._link_or_copy_replace(src, dest)
            return True

        return await asyncio.to_thread(_copy)

    async def write_execution_staging_stream(
        self, job_id: str, exec_id: str, rel_path: str, chunks: AsyncIterable[bytes], *,
        expected_size: int | None = None, expected_sha256: str | None = None,
        max_bytes: int | None = None,
    ) -> dict:
        target = self._staged_rel(
            self._execution_staging_dir(job_id, exec_id), rel_path,
        )
        await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.flori-part-{uuid.uuid4().hex}")
        digest = hashlib.sha256()
        total = 0
        fp = await asyncio.to_thread(open, tmp, "wb")
        try:
            async for chunk in chunks:
                if not isinstance(chunk, bytes):
                    raise TypeError("staging stream yielded non-bytes")
                if not chunk:
                    continue
                total += len(chunk)
                if max_bytes is not None and total > max_bytes:
                    raise ArtifactTooLarge(f"staged output exceeds {max_bytes} bytes")
                digest.update(chunk)
                await asyncio.to_thread(fp.write, chunk)
            await asyncio.to_thread(fp.flush)
            await asyncio.to_thread(os.fsync, fp.fileno())
            await asyncio.to_thread(fp.close)
            fp = None
            actual = digest.hexdigest()
            if expected_size is not None and total != expected_size:
                raise ValueError("staged output size mismatch")
            if expected_sha256 and not hmac.compare_digest(
                actual, expected_sha256.lower().removeprefix("sha256:"),
            ):
                raise ValueError("staged output checksum mismatch")
            await asyncio.to_thread(os.replace, tmp, target)
            await asyncio.to_thread(_fsync_directory, target.parent)
            return {"size": total, "sha256": actual}
        finally:
            if fp is not None:
                await asyncio.to_thread(fp.close)
            await asyncio.to_thread(tmp.unlink, True)

    async def commit_step_outputs(
        self, job_id: str, execution_step: str, exec_id: str, *,
        outputs: list[dict], manifest: dict, manifest_rel: str,
        stale_paths: list[str], token: dict, commit_record: bytes,
        verify_token,
    ) -> None:
        manifest_bytes = _canonical_manifest_bytes(manifest, token)
        # 发布前交叉校验(P2-3):身份=租约执行身份,声明集=实际提交集;read-back 以声明集为准。
        declared = _verify_manifest_binding(
            manifest, job_id=job_id, execution_step=execution_step,
            exec_id=exec_id, outputs=outputs,
        )
        kept = {out.get("path") for out in outputs}
        # 越权路径(跨步/跨 Part/内部命名空间/凭证)在任何副作用前整体拒绝(P1,零删除零 promote)。
        for out in outputs:
            _guard_commit_path(execution_step, out.get("path"), "output")
        for rel in stale_paths:
            if rel not in kept:
                _guard_commit_path(execution_step, rel, "stale")
        staging_root = self._execution_staging_dir(job_id, exec_id)
        # commit 记录先于第一次 promote 落盘:promote_started 持久证据(§2.7 行 1/2)。
        await asyncio.to_thread(
            write_path_atomic, staging_root / COMMIT_RECORD_FILENAME, commit_record,
        )
        for out in outputs:
            rel = out["path"]
            src = self._staged_rel(staging_root, rel)
            if not src.is_file():
                raise StepCommitIntegrityError(f"staged output missing: {rel}")
            await _verify_commit_token(verify_token)
            target = self._safe_path(job_id, rel)
            await asyncio.to_thread(self._link_or_copy_replace, src, target)
            await _verify_commit_token(verify_token)
        for out in declared:
            target = self._safe_path(job_id, out["path"])
            try:
                size, sha = await asyncio.to_thread(self._hash_file_sync, target)
            except OSError as exc:
                raise StepCommitIntegrityError(
                    f"read-back failed: {out['path']}"
                ) from exc
            if size != out["size_bytes"] or sha != out["sha256"]:
                raise StepCommitIntegrityError(
                    f"read-back mismatch: {out['path']}"
                )
        for rel in stale_paths:
            if rel in kept:
                continue
            path = self._safe_path(job_id, rel)
            await asyncio.to_thread(path.unlink, True)
        await _verify_commit_token(verify_token)
        # manifest 最后原子发布:temp+fsync+os.replace+fsync(parent)(write_path_atomic)。
        await asyncio.to_thread(
            write_path_atomic, self._safe_path(job_id, manifest_rel), manifest_bytes,
        )
        await _verify_commit_token(verify_token, "manifest_published")

    async def cleanup_execution_staging(self, job_id: str, exec_id: str) -> None:
        root = self._execution_staging_dir(job_id, exec_id)

        def _cleanup() -> None:
            shutil.rmtree(root, ignore_errors=True)
            for parent in (root.parent, root.parent.parent):
                try:
                    parent.rmdir()
                except OSError:
                    pass

        await asyncio.to_thread(_cleanup)

    async def cleanup_stale_execution_staging(
        self, *, active_exec_ids: set[str], stale_before_epoch: float,
    ) -> int:
        root = self.jobs_dir / ".flori" / "staging"

        def _cleanup() -> int:
            removed = 0
            if not root.is_dir():
                return 0
            for job_dir in root.iterdir():
                if not job_dir.is_dir():
                    continue
                for exec_dir in job_dir.iterdir():
                    if not exec_dir.is_dir() or exec_dir.name in active_exec_ids:
                        continue
                    try:
                        modified = exec_dir.stat().st_mtime
                    except FileNotFoundError:
                        continue
                    if modified >= stale_before_epoch:
                        continue
                    shutil.rmtree(exec_dir, ignore_errors=True)
                    removed += 1
                try:
                    job_dir.rmdir()
                except OSError:
                    pass
            return removed

        return await asyncio.to_thread(_cleanup)

    async def delete(
        self, job_id: str, *, defer_if_busy: bool = False,
    ) -> None:
        root = self._safe_path(job_id)
        staging = self._staging_path(job_id, "placeholder").parent
        initializing = self._initialization_path(job_id).parent
        # 执行 staging 与隔离 attempt 同属该 job 的运行时残留,删 job 一并清。
        execution_staging = self.jobs_dir / ".flori" / "staging" / job_id
        attempts = self._attempts_root() / job_id

        def _delete_tree(path: Path) -> None:
            try:
                shutil.rmtree(path)
            except FileNotFoundError:
                pass

        await asyncio.to_thread(_delete_tree, root)
        await asyncio.to_thread(_delete_tree, staging)
        await asyncio.to_thread(_delete_tree, initializing)
        await asyncio.to_thread(_delete_tree, execution_staging)
        await asyncio.to_thread(_delete_tree, attempts)
        await asyncio.to_thread(self._remove_empty_staging_parents, staging)
        await asyncio.to_thread(self._remove_empty_staging_parents, initializing)
        await asyncio.to_thread(self._remove_empty_staging_parents, execution_staging)
        await asyncio.to_thread(self._remove_empty_staging_parents, attempts)

    def _remove_empty_staging_parents(self, staging_job_dir: Path) -> None:
        for path in (staging_job_dir, staging_job_dir.parent):
            try:
                path.rmdir()
            except OSError:
                pass

    async def clone(self, src_job_id: str, dst_job_id: str) -> None:
        # 整目录复制(含 .done dotfile,供 fork 播种);排除凭证侧载文件与 .flori 内部命名空间
        # (manifest 绑定 job_id 身份,不得字节复制到新 job,设计稿 §2.10)。源不存在=no-op。
        src = self._safe_path(src_job_id)
        dst = self._safe_path(dst_job_id)

        def _ignore(directory: str, names: list[str]) -> list[str]:
            base = str(src)
            out = []
            for n in names:
                rel = os.path.relpath(os.path.join(directory, n), base).replace("\\", "/")
                if (
                    is_credential_file(rel)
                    or is_internal_namespace_path(rel)
                    or Path(directory, n).is_symlink()
                ):
                    out.append(n)
            return out

        def _copy() -> None:
            if not src.is_dir():
                return
            shutil.copytree(src, dst, dirs_exist_ok=True, ignore=_ignore)

        await asyncio.to_thread(_copy)

    async def delete_file(self, job_id: str, rel_path: str) -> None:
        path = self._data_path(job_id, rel_path)
        await asyncio.to_thread(path.unlink, True)
        if rel_path == INITIALIZATION_MARKER_REL:
            await asyncio.to_thread(self._remove_empty_staging_parents, path.parent)

    async def read_file(self, job_id: str, rel_path: str) -> bytes | None:
        path = self._data_path(job_id, rel_path)
        if not path.is_file():
            return None
        return await asyncio.to_thread(path.read_bytes)

    async def open_stream(
        self, job_id: str, rel_path: str, *, start: int = 0,
        length: int | None = None, chunk_size: int = 1024 * 1024,
    ) -> AsyncIterator[bytes] | None:
        path = self._safe_path(job_id, rel_path)
        if not path.is_file():
            return None

        async def _chunks() -> AsyncIterator[bytes]:
            fp = await asyncio.to_thread(open, path, "rb")
            remaining = length
            try:
                await asyncio.to_thread(fp.seek, start)
                while remaining is None or remaining > 0:
                    want = chunk_size if remaining is None else min(chunk_size, remaining)
                    chunk = await asyncio.to_thread(fp.read, want)
                    if not chunk:
                        break
                    if remaining is not None:
                        remaining -= len(chunk)
                    yield chunk
            finally:
                await asyncio.to_thread(fp.close)

        return _chunks()

    async def file_size(self, job_id: str, rel_path: str) -> int | None:
        path = self._safe_path(job_id, rel_path)
        return path.stat().st_size if path.is_file() else None

    async def object_version(
        self, job_id: str, rel_path: str,
    ) -> StorageObjectVersion | None:
        path = self._safe_path(job_id, rel_path)
        try:
            value = path.stat()
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(value.st_mode):
            return None
        return StorageObjectVersion(
            namespace=f"local:{self.jobs_dir.resolve()}",
            size=value.st_size,
            token=(
                f"{value.st_dev}:{value.st_ino}:{value.st_mtime_ns}:"
                f"{value.st_ctime_ns}"
            ),
        )

    async def read_range(self, job_id: str, rel_path: str, start: int, length: int) -> bytes | None:
        path = self._safe_path(job_id, rel_path)
        if not path.is_file():
            return None

        def _read() -> bytes:
            with open(path, "rb") as f:
                f.seek(start)
                return f.read(length)

        return await asyncio.to_thread(_read)

    async def write_file(self, job_id: str, rel_path: str, data: bytes) -> None:
        path = self._data_path(job_id, rel_path)
        await asyncio.to_thread(write_path_atomic, path, data)

    async def write_stream(
        self, job_id: str, rel_path: str, chunks: AsyncIterable[bytes], *,
        expected_size: int | None = None, expected_sha256: str | None = None,
        max_bytes: int | None = None, staging_token: str | None = None,
    ) -> dict:
        target = self._safe_path(job_id, rel_path)
        staging = self._staging_path(job_id, staging_token or uuid.uuid4().hex)
        await asyncio.to_thread(staging.parent.mkdir, parents=True, exist_ok=True)
        digest = hashlib.sha256()
        total = 0
        fp = await asyncio.to_thread(open, staging, "wb")
        try:
            async for chunk in chunks:
                if not isinstance(chunk, bytes):
                    raise TypeError("artifact stream yielded non-bytes")
                if not chunk:
                    continue
                total += len(chunk)
                if max_bytes is not None and total > max_bytes:
                    raise ArtifactTooLarge(f"artifact exceeds {max_bytes} bytes")
                digest.update(chunk)
                await asyncio.to_thread(fp.write, chunk)
            await asyncio.to_thread(fp.flush)
            await asyncio.to_thread(os.fsync, fp.fileno())
            await asyncio.to_thread(fp.close)
            fp = None
            actual = digest.hexdigest()
            if expected_size is not None and total != expected_size:
                raise ValueError("artifact size mismatch")
            if expected_sha256 and not hmac.compare_digest(actual, expected_sha256.lower()):
                raise ValueError("artifact checksum mismatch")
            await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(os.replace, staging, target)
            await asyncio.to_thread(_fsync_directory, target.parent)
            return {"size": total, "sha256": actual}
        finally:
            if fp is not None:
                await asyncio.to_thread(fp.close)
            await asyncio.to_thread(staging.unlink, True)
            await asyncio.to_thread(self._remove_empty_staging_parents, staging.parent)

    async def list_initialization_markers(self) -> list[str]:
        def _list() -> list[str]:
            root = self.jobs_dir / _GLOBAL_INITIALIZATION_PREFIX
            if not root.is_dir():
                return []
            return sorted(
                child.name
                for child in root.iterdir()
                if child.is_dir()
                and (child / "marker.json").is_file()
            )

        return await asyncio.to_thread(_list)

    async def cleanup_stale_staging(
        self, *, active_tokens: set[tuple[str, str]],
        protected_job_ids: set[str], stale_before_epoch: float,
    ) -> int:
        root = self.jobs_dir / _GLOBAL_STAGING_PREFIX

        def _cleanup() -> int:
            removed = 0
            if not root.is_dir():
                return 0
            for job_dir in root.iterdir():
                if not job_dir.is_dir() or job_dir.name in protected_job_ids:
                    continue
                for path in job_dir.iterdir():
                    token = path.name.split(".", 1)[0]
                    if (job_dir.name, token) in active_tokens:
                        continue
                    try:
                        modified = path.stat().st_mtime
                    except FileNotFoundError:
                        continue
                    if modified >= stale_before_epoch:
                        continue
                    if path.is_file():
                        path.unlink(missing_ok=True)
                        removed += 1
                try:
                    job_dir.rmdir()
                except OSError:
                    pass
            return removed

        return await asyncio.to_thread(_cleanup)

    async def list_files(self, job_id: str) -> list[str]:
        return await asyncio.to_thread(self._list_files_sync, job_id)

    def _list_files_sync(self, job_id: str) -> list[str]:
        root = self._safe_path(job_id)
        if not root.is_dir():
            return []
        # 只收文件,相对 job 目录,统一用 "/" 分隔(跨平台/与对象键对齐)。
        return [
            p.relative_to(root).as_posix()
            for p in root.rglob("*")
            if (
                not p.is_symlink()
                and p.is_file()
                and not _is_internal_file(p.relative_to(root).as_posix())
            )
        ]

    async def list_file_sizes(self, job_id: str) -> dict[str, int]:
        return await asyncio.to_thread(self._list_file_sizes_sync, job_id)

    def _list_file_sizes_sync(self, job_id: str) -> dict[str, int]:
        root = self._safe_path(job_id)
        if not root.is_dir():
            return {}
        # rglob 已遍历到每个文件,顺手 stat().st_size,无需二次列举。
        return {
            p.relative_to(root).as_posix(): p.stat().st_size
            for p in root.rglob("*")
            if (
                not p.is_symlink()
                and p.is_file()
                and not _is_internal_file(p.relative_to(root).as_posix())
            )
        }

    async def health(self) -> dict:
        # 本地盘:无独立对象存储组件,前端按 mode=local 显"本地存储"灰点(unknown,非 down)。
        return {
            "status": "unknown", "mode": "local", "bucket": None,
            "version": None, "detail": "本地盘", "probe_ms": None,
        }

    async def capacity(self) -> dict | None:
        # 本地盘容量(os.walk 求和);to_thread 防阻塞。jobs_dir 不存在 → 零。
        return await asyncio.to_thread(self._capacity_sync)

    def _capacity_sync(self) -> dict:
        objects = 0
        total = 0
        root = self.jobs_dir
        if root.is_dir():
            for dirpath, _dirs, files in os.walk(root):
                for name in files:
                    path = os.path.join(dirpath, name)
                    if os.path.islink(path):
                        continue
                    try:
                        total += os.path.getsize(path)
                        objects += 1
                    except OSError:
                        continue
        return {"objects": objects, "bytes": total}


class RemoteStorage:
    """对象存储后端:worker 在任意机器拉取/回传 job 产物。

    对象键 = ``{job_id}/{相对路径}``。pull 下载整个 job 前缀到本机临时目录,
    push 只上传相对 pull 快照新增或改动的文件(不删除),因此同一 job 的并行
    步骤各自只写自己的产物,互不覆盖。
    """

    def __init__(
        self,
        endpoint: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        secure: bool,
        tmp_root: Path,
    ):
        self._endpoint = endpoint
        self._access_key = access_key
        self._secret_key = secret_key
        self._bucket = bucket
        self._secure = secure
        self._tmp_root = tmp_root
        # pull 时记录每个 work_dir 的文件快照(relpath -> (size, mtime)),供 push 算增量。
        self._snapshots: dict[str, dict[str, tuple[int, float]]] = {}
        self._client_obj = None
        self._readiness_client_obj = None
        self._readiness_io_timeout_sec: float | None = None
        self._readiness_http_client = None
        self._finalizer_tasks: set[asyncio.Task] = set()
        self._finalizers_by_job: dict[str, set[asyncio.Task]] = {}
        self._active_writers: dict[str, set[asyncio.Task]] = {}
        self._job_locks: dict[str, asyncio.Lock] = {}
        self._delete_requested: set[str] = set()
        self._delete_tasks: dict[str, asyncio.Task] = {}

    def _client(self):
        # 延迟连接:构造时不导入 minio、不连服务器(便于选型与单测),首次用到才建。
        if self._client_obj is None:
            from minio import Minio

            c = Minio(
                self._endpoint, access_key=self._access_key,
                secret_key=self._secret_key, secure=self._secure,
            )
            if not c.bucket_exists(self._bucket):
                c.make_bucket(self._bucket)
            self._tmp_root.mkdir(parents=True, exist_ok=True)
            self._client_obj = c
        return self._client_obj

    @staticmethod
    def _object_key(job_id: str, rel_path: str) -> str:
        if rel_path == INITIALIZATION_MARKER_REL:
            return f"{_GLOBAL_INITIALIZATION_PREFIX}{job_id}/marker.json"
        return f"{job_id}/{rel_path}"

    def _readiness_client(self, timeout_sec: float):
        """创建 SDK 层有界的专用客户端,不复用业务客户端的长 I/O 配置."""
        # canary 包含 region + put + delete + 版本;单次 connect/read 取总预算五分之一,
        # 使外层 asyncio 超时前底层线程通常已自行返回,避免黑洞网络累积残留线程.
        io_timeout = max(0.1, min(0.75, timeout_sec / 5))
        if (
            self._readiness_client_obj is not None
            and self._readiness_io_timeout_sec == io_timeout
        ):
            return self._readiness_client_obj

        from minio import Minio
        http_client = self._bounded_http_client(io_timeout)
        self._readiness_client_obj = Minio(
            self._endpoint,
            access_key=self._access_key,
            secret_key=self._secret_key,
            secure=self._secure,
            http_client=http_client,
        )
        self._readiness_io_timeout_sec = io_timeout
        self._readiness_http_client = http_client
        return self._readiness_client_obj

    @staticmethod
    def _bounded_http_client(timeout_sec: float):
        from urllib3 import PoolManager, Retry, Timeout

        return PoolManager(
            timeout=Timeout(connect=timeout_sec, read=timeout_sec),
            retries=Retry(total=0, connect=0, read=0, redirect=0, status=0),
        )

    async def pull(self, job_id: str, step: str) -> Path:
        return await asyncio.to_thread(self._pull_sync, job_id, step)

    def _pull_sync(self, job_id: str, step: str) -> Path:
        scope_token = hashlib.sha256(step.encode()).hexdigest()[:16]
        work_dir = self._tmp_root / job_id / scope_token / uuid.uuid4().hex / "root"
        work_dir.mkdir(parents=True, exist_ok=True)
        snapshot: dict[str, tuple[int, float]] = {}
        prefix = f"{job_id}/"
        for obj in self._client().list_objects(self._bucket, prefix=prefix, recursive=True):
            rel = obj.object_name[len(prefix):]
            if (
                not rel
                or _is_internal_file(rel)
                or not execution_artifact_allowed(step, rel, write=False)
            ):
                continue
            dest = work_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            self._client().fget_object(self._bucket, obj.object_name, str(dest))
            st = dest.stat()
            snapshot[rel] = (st.st_size, st.st_mtime)
        self._snapshots[str(work_dir)] = snapshot
        return work_dir

    async def push(
        self, job_id: str, step: str, work_dir: Path, *,
        exclude_paths: set[str] | None = None,
        only_globs: list[str] | None = None,
    ) -> None:
        await asyncio.to_thread(
            self._push_sync, job_id, step, work_dir, exclude_paths or set(),
            only_globs,
        )

    def _push_sync(
        self, job_id: str, step: str, work_dir: Path,
        exclude_paths: set[str],
        only_globs: list[str] | None = None,
    ) -> None:
        snapshot = self._snapshots.get(str(work_dir), {})
        for path in work_dir.rglob("*"):
            if path.is_symlink() or not path.is_file():
                continue
            rel = path.relative_to(work_dir).as_posix()
            if (
                rel in exclude_paths
                or is_credential_file(rel)
                or _is_internal_file(rel)
                or not execution_artifact_allowed(step, rel, write=True)
            ):
                continue  # 敏感凭证永不上行中心存储
            if only_globs is not None and not any(
                fnmatch.fnmatch(rel, pattern) for pattern in only_globs
            ):
                continue  # 失败路径诊断白名单:业务输出不上行(设计稿 §2.5-5)
            st = path.stat()
            prev = snapshot.get(rel)
            if prev is not None and prev == (st.st_size, st.st_mtime):
                continue  # 未改动,跳过
            self._client().fput_object(self._bucket, f"{job_id}/{rel}", str(path))

    async def cleanup(self, job_id: str, step: str, work_dir: Path) -> None:
        await asyncio.to_thread(self._cleanup_sync, work_dir)

    def _cleanup_sync(self, work_dir: Path) -> None:
        self._snapshots.pop(str(work_dir), None)
        attempt_dir = work_dir.parent
        shutil.rmtree(attempt_dir, ignore_errors=True)
        for parent in (attempt_dir.parent, attempt_dir.parent.parent):
            try:
                parent.rmdir()
            except OSError:
                pass

    async def delete(
        self, job_id: str, *, defer_if_busy: bool = False,
    ) -> None:
        async with self._job_lock(job_id):
            self._delete_requested.add(job_id)
            task = self._maybe_start_delete(job_id)
        if defer_if_busy and task is None:
            return
        while task is None:
            pending: set[asyncio.Task] = set(
                self._active_writers.get(job_id, set())
            )
            pending.update(self._finalizers_by_job.get(job_id, set()))
            if not pending:
                task = self._maybe_start_delete(job_id)
                break
            await asyncio.gather(
                *(asyncio.shield(item) for item in pending),
                return_exceptions=True,
            )
            task = self._maybe_start_delete(job_id)
        if task is not None:
            await asyncio.shield(task)

    def _job_lock(self, job_id: str) -> asyncio.Lock:
        lock = self._job_locks.get(job_id)
        if lock is None:
            lock = asyncio.Lock()
            self._job_locks[job_id] = lock
        return lock

    async def _register_writer(self, job_id: str) -> None:
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("stream writer must run in an asyncio task")
        async with self._job_lock(job_id):
            if job_id in self._delete_requested:
                raise OSError("job deletion is in progress")
            self._active_writers.setdefault(job_id, set()).add(task)

    def _unregister_writer(self, job_id: str) -> None:
        task = asyncio.current_task()
        writers = self._active_writers.get(job_id)
        if writers is not None:
            writers.discard(task)
            if not writers:
                self._active_writers.pop(job_id, None)
        self._maybe_start_delete(job_id)
        self._cleanup_job_barrier_state(job_id)

    def _cleanup_job_barrier_state(self, job_id: str) -> None:
        if (
            not self._active_writers.get(job_id)
            and not self._finalizers_by_job.get(job_id)
            and job_id not in self._delete_requested
            and job_id not in self._delete_tasks
        ):
            self._job_locks.pop(job_id, None)

    async def _ensure_publish_allowed(self, job_id: str) -> None:
        async with self._job_lock(job_id):
            if job_id in self._delete_requested:
                raise OSError("job deletion is in progress")

    async def _start_publish_operation(
        self,
        job_id: str,
        operations: dict[str, asyncio.Task],
        phase: str,
        callback: Callable,
        *args,
    ) -> asyncio.Task:
        """在 delete barrier 锁内先登记同步操作，再把 Task 交还调用方。"""
        async with self._job_lock(job_id):
            if job_id in self._delete_requested:
                raise OSError("job deletion is in progress")
            task = asyncio.create_task(asyncio.to_thread(callback, *args))
            operations[phase] = task
            return task

    def _maybe_start_delete(self, job_id: str) -> asyncio.Task | None:
        if job_id not in self._delete_requested:
            return None
        existing = self._delete_tasks.get(job_id)
        if existing is not None:
            return existing
        if self._active_writers.get(job_id):
            return None
        if self._finalizers_by_job.get(job_id):
            return None

        task = asyncio.create_task(asyncio.to_thread(self._delete_sync, job_id))
        self._delete_tasks[job_id] = task

        def _done(done: asyncio.Task) -> None:
            if self._delete_tasks.get(job_id) is done:
                self._delete_tasks.pop(job_id, None)
            try:
                done.result()
            except BaseException as exc:
                import structlog
                structlog.get_logger().error(
                    "storage_delete_barrier_failed",
                    job_id=job_id,
                    error=type(exc).__name__,
                    detail=str(exc),
                )
            else:
                self._delete_requested.discard(job_id)
                if (
                    not self._active_writers.get(job_id)
                    and not self._finalizers_by_job.get(job_id)
                ):
                    self._job_locks.pop(job_id, None)

        task.add_done_callback(_done)
        return task

    def _delete_sync(self, job_id: str) -> None:
        from minio.deleteobjects import DeleteObject

        client = self._client()
        prefixes = (
            f"{job_id}/",
            f"{_GLOBAL_STAGING_PREFIX}{job_id}/",
            f"{_GLOBAL_INITIALIZATION_PREFIX}{job_id}/",
            f"{EXECUTION_STAGING_PREFIX}{job_id}/",
        )
        objs = []
        for prefix in prefixes:
            objs.extend(
                DeleteObject(o.object_name)
                for o in client.list_objects(
                    self._bucket, prefix=prefix, recursive=True,
                )
            )
        if objs:
            # remove_objects 惰性返回错误迭代器,必须消费(list)才真正发起删除。
            errors = list(client.remove_objects(self._bucket, objs))
            if errors:
                details = [
                    f"{getattr(error, 'code', type(error).__name__)}:"
                    f"{getattr(error, 'object_name', '')}"
                    for error in errors
                ]
                raise OSError(
                    f"storage delete partial for {job_id}: {'; '.join(details)}"
                )
        # 顺带清掉本机为该 job 留存的临时工作目录与快照(幂等)。
        work_dir = self._tmp_root / job_id
        self._snapshots.pop(str(work_dir), None)
        shutil.rmtree(work_dir, ignore_errors=True)

    async def clone(self, src_job_id: str, dst_job_id: str) -> None:
        await asyncio.to_thread(self._clone_sync, src_job_id, dst_job_id)

    def _clone_sync(self, src_job_id: str, dst_job_id: str) -> None:
        # 服务端 copy_object 逐对象复制 {src}/ → {dst}/(零下载);中心本无凭证(write_file 已跳过)。
        from minio.commonconfig import CopySource

        client = self._client()
        src_prefix = f"{src_job_id}/"
        failures: list[str] = []
        for o in client.list_objects(self._bucket, prefix=src_prefix, recursive=True):
            rel = o.object_name[len(src_prefix):]
            if is_credential_file(rel) or _is_internal_file(rel):
                continue
            try:
                client.copy_object(
                    self._bucket, f"{dst_job_id}/{rel}",
                    CopySource(self._bucket, o.object_name),
                )
            except Exception as e:
                failures.append(f"{o.object_name}: {e}")
        if failures:
            raise OSError(
                f"storage clone incomplete for {src_job_id} -> {dst_job_id}: "
                + "; ".join(failures)
            )

    async def write_file(self, job_id: str, rel_path: str, data: bytes) -> None:
        if is_credential_file(rel_path):
            return  # 敏感凭证不入中心对象存储(防下发到远端 worker);仅 LocalStorage 本机持有
        await asyncio.to_thread(self._write_file_sync, job_id, rel_path, data)

    async def open_stream(
        self, job_id: str, rel_path: str, *, start: int = 0,
        length: int | None = None, chunk_size: int = 1024 * 1024,
    ) -> AsyncIterator[bytes] | None:
        from minio.error import S3Error

        try:
            await asyncio.to_thread(
                self._client().stat_object, self._bucket, f"{job_id}/{rel_path}",
            )
        except S3Error as exc:
            if _is_s3_not_found(exc):
                return None
            raise

        async def _chunks() -> AsyncIterator[bytes]:
            resp = None
            try:
                kwargs = {"offset": start}
                if length is not None:
                    kwargs["length"] = length
                resp = await asyncio.to_thread(
                    self._client().get_object,
                    self._bucket,
                    f"{job_id}/{rel_path}",
                    **kwargs,
                )
                remaining = length
                while remaining is None or remaining > 0:
                    want = chunk_size if remaining is None else min(chunk_size, remaining)
                    chunk = await asyncio.to_thread(resp.read, want)
                    if not chunk:
                        break
                    if remaining is not None:
                        remaining -= len(chunk)
                    yield chunk
            finally:
                if resp is not None:
                    await asyncio.to_thread(resp.close)
                    await asyncio.to_thread(resp.release_conn)

        return _chunks()

    async def write_stream(
        self, job_id: str, rel_path: str, chunks: AsyncIterable[bytes], *,
        expected_size: int | None = None, expected_sha256: str | None = None,
        max_bytes: int | None = None, staging_token: str | None = None,
    ) -> dict:
        try:
            await self._register_writer(job_id)
            return await self._write_stream_registered(
                job_id,
                rel_path,
                chunks,
                expected_size=expected_size,
                expected_sha256=expected_sha256,
                max_bytes=max_bytes,
                staging_token=staging_token,
            )
        finally:
            task = asyncio.current_task()
            if (
                task is not None
                and task in self._active_writers.get(job_id, set())
            ):
                self._unregister_writer(job_id)

    async def _write_stream_registered(
        self, job_id: str, rel_path: str, chunks: AsyncIterable[bytes], *,
        expected_size: int | None = None, expected_sha256: str | None = None,
        max_bytes: int | None = None, staging_token: str | None = None,
    ) -> dict:
        token = staging_token or uuid.uuid4().hex
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", token):
            raise ValueError("invalid staging token")
        staging_key = f"{_GLOBAL_STAGING_PREFIX}{job_id}/{token}"
        backup_key = f"{staging_key}.backup"
        final_key = f"{job_id}/{rel_path}"
        digest = hashlib.sha256()
        total = 0
        bridge = _AsyncToSyncStream()
        operation_tasks: dict[str, asyncio.Task] = {}
        upload_task: asyncio.Task | None = None
        backup_task: asyncio.Task | None = None
        publish_task: asyncio.Task | None = None
        staging_cleanup_task: asyncio.Task | None = None
        had_final = False
        deferred_cleanup = False
        publish_committed = False

        def _upload() -> None:
            try:
                self._client().put_object(
                    self._bucket,
                    staging_key,
                    bridge,
                    length=-1,
                    part_size=_MINIO_STREAM_PART_SIZE,
                )
            finally:
                bridge.stop()

        try:
            try:
                upload_task = await self._start_publish_operation(
                    job_id, operation_tasks, "upload", _upload,
                )
                async for chunk in chunks:
                    if not isinstance(chunk, bytes):
                        raise TypeError("artifact stream yielded non-bytes")
                    if not chunk:
                        continue
                    total += len(chunk)
                    if max_bytes is not None and total > max_bytes:
                        raise ArtifactTooLarge(f"artifact exceeds {max_bytes} bytes")
                    digest.update(chunk)
                    try:
                        await bridge.put(chunk)
                    except OSError:
                        await asyncio.shield(upload_task)
                        raise
                try:
                    await bridge.finish()
                except OSError:
                    await asyncio.shield(upload_task)
                    raise
                await asyncio.shield(upload_task)
            except BaseException as exc:
                upload_task = operation_tasks.get("upload")
                if upload_task is not None and not upload_task.done():
                    stream_error = (
                        OSError("artifact stream cancelled")
                        if isinstance(exc, asyncio.CancelledError)
                        else exc
                    )
                    bridge.abort(stream_error)
                if isinstance(exc, asyncio.CancelledError):
                    self._schedule_cancelled_finalizer(
                        job_id,
                        final_key,
                        staging_key,
                        backup_key,
                        list(operation_tasks.values()),
                        backup_task=operation_tasks.get("backup"),
                        publish_task=operation_tasks.get("publish"),
                        had_final=had_final,
                    )
                    deferred_cleanup = True
                    raise
                if upload_task is not None:
                    try:
                        await asyncio.shield(upload_task)
                    except BaseException:
                        pass
                raise

            await self._ensure_publish_allowed(job_id)

            actual = digest.hexdigest()
            if expected_size is not None and total != expected_size:
                raise ValueError("artifact size mismatch")
            if expected_sha256 and not hmac.compare_digest(actual, expected_sha256.lower()):
                raise ValueError("artifact checksum mismatch")

            had_final = await asyncio.to_thread(self._object_exists_sync, final_key)
            await self._ensure_publish_allowed(job_id)
            if had_final:
                try:
                    backup_task = await self._start_publish_operation(
                        job_id,
                        operation_tasks,
                        "backup",
                        self._copy_object_sync,
                        final_key,
                        backup_key,
                    )
                    await asyncio.shield(backup_task)
                    await self._ensure_publish_allowed(job_id)
                except asyncio.CancelledError:
                    self._schedule_cancelled_finalizer(
                        job_id, final_key, staging_key, backup_key,
                        list(operation_tasks.values()),
                        backup_task=operation_tasks.get("backup"),
                        publish_task=None, had_final=True,
                    )
                    deferred_cleanup = True
                    raise

            try:
                publish_task = await self._start_publish_operation(
                    job_id,
                    operation_tasks,
                    "publish",
                    self._copy_object_sync,
                    staging_key,
                    final_key,
                )
                await asyncio.shield(publish_task)
                await self._ensure_publish_allowed(job_id)
            except asyncio.CancelledError:
                self._schedule_cancelled_finalizer(
                    job_id, final_key, staging_key, backup_key,
                    list(operation_tasks.values()),
                    backup_task=operation_tasks.get("backup"),
                    publish_task=operation_tasks.get("publish"),
                    had_final=had_final,
                )
                deferred_cleanup = True
                raise
            publish_committed = True
            return {"size": total, "sha256": actual}
        finally:
            bridge.stop()
            if not deferred_cleanup:
                staging_cleanup_task = asyncio.create_task(
                    self._remove_staging_keys(staging_key),
                )
                self._track_finalizer_task(job_id, staging_cleanup_task)
                try:
                    staging_cleanup_succeeded = await asyncio.shield(
                        staging_cleanup_task,
                    )
                except asyncio.CancelledError:
                    staging_cleanup_succeeded = (
                        publish_committed
                        and self._cleanup_task_succeeded(staging_cleanup_task)
                    )
                    if not staging_cleanup_succeeded:
                        self._schedule_cancelled_finalizer(
                            job_id, final_key, staging_key, backup_key,
                            list(operation_tasks.values()),
                            backup_task=operation_tasks.get("backup"),
                            publish_task=operation_tasks.get("publish"),
                            had_final=had_final,
                            staging_cleanup_task=staging_cleanup_task,
                        )
                        deferred_cleanup = True
                        raise
                if not staging_cleanup_succeeded:
                    self._schedule_cancelled_finalizer(
                        job_id, final_key, staging_key, backup_key,
                        list(operation_tasks.values()),
                        backup_task=operation_tasks.get("backup"),
                        publish_task=operation_tasks.get("publish"),
                        had_final=had_final,
                        staging_cleanup_task=staging_cleanup_task,
                    )
                    deferred_cleanup = True
                    if publish_committed:
                        raise OSError("storage staging cleanup failed")
                elif had_final:
                    cleanup_task = asyncio.create_task(
                        self._remove_staging_keys(backup_key),
                    )
                    self._track_finalizer_task(job_id, cleanup_task)
                    if publish_committed:
                        # staging 删除后即为提交点。backup 只由强引用任务异步收口，
                        # request task 不再留下可交付取消的 await。
                        pass
                    else:
                        await asyncio.shield(cleanup_task)

    def _copy_object_sync(self, source_key: str, target_key: str) -> None:
        from minio.commonconfig import CopySource

        self._client().copy_object(
            self._bucket,
            target_key,
            CopySource(self._bucket, source_key),
        )

    def _object_exists_sync(self, object_key: str) -> bool:
        from minio.error import S3Error

        try:
            value = self._client().stat_object(self._bucket, object_key)
        except S3Error as exc:
            if _is_s3_not_found(exc):
                return False
            raise
        return isinstance(getattr(value, "size", None), int)

    async def _remove_staging_keys(self, *keys: str) -> bool:
        succeeded = True
        for key in keys:
            try:
                await asyncio.to_thread(
                    self._client().remove_object, self._bucket, key,
                )
            except Exception as exc:
                import structlog
                structlog.get_logger().error(
                    "storage_staging_cleanup_failed", object_key=key,
                    error=str(exc),
                )
                succeeded = False
        return succeeded

    @staticmethod
    def _cleanup_task_succeeded(task: asyncio.Task) -> bool:
        """仅把已完成、未取消且明确返回 True 的 cleanup Task 视为提交点。"""
        if not task.done() or task.cancelled():
            return False
        try:
            return task.result() is True
        except BaseException:
            return False

    def _schedule_cancelled_finalizer(
        self,
        job_id: str,
        final_key: str,
        staging_key: str,
        backup_key: str,
        operation_tasks: list[asyncio.Task],
        *,
        backup_task: asyncio.Task | None,
        publish_task: asyncio.Task | None,
        had_final: bool,
        staging_cleanup_task: asyncio.Task | None = None,
    ) -> None:
        task = asyncio.create_task(self._finalize_cancelled_upload(
            job_id,
            final_key,
            staging_key,
            backup_key,
            list(operation_tasks),
            backup_task=backup_task,
            publish_task=publish_task,
            had_final=had_final,
            staging_cleanup_task=staging_cleanup_task,
        ))
        self._track_finalizer_task(job_id, task)

    def _track_finalizer_task(
        self, job_id: str, task: asyncio.Task,
    ) -> None:
        """强引用 job 收口任务，使 delete barrier 与 lifespan 能等待它。"""
        self._finalizer_tasks.add(task)
        self._finalizers_by_job.setdefault(job_id, set()).add(task)

        def _done(done: asyncio.Task) -> None:
            self._finalizer_tasks.discard(done)
            per_job = self._finalizers_by_job.get(job_id)
            if per_job is not None:
                per_job.discard(done)
                if not per_job:
                    self._finalizers_by_job.pop(job_id, None)
            try:
                done.result()
            except BaseException as exc:
                import structlog
                structlog.get_logger().error(
                    "storage_finalizer_unexpected_failure",
                    job_id=job_id,
                    error=type(exc).__name__,
                    detail=str(exc),
                )
            self._maybe_start_delete(job_id)
            self._cleanup_job_barrier_state(job_id)

        task.add_done_callback(_done)

    async def _finalize_cancelled_upload(
        self,
        job_id: str,
        final_key: str,
        staging_key: str,
        backup_key: str,
        operation_tasks: list[asyncio.Task],
        *,
        backup_task: asyncio.Task | None,
        publish_task: asyncio.Task | None,
        had_final: bool,
        staging_cleanup_task: asyncio.Task | None = None,
    ) -> None:
        import structlog

        task_errors: list[str] = []
        staging_cleanup_succeeded = False
        if staging_cleanup_task is not None:
            try:
                await asyncio.shield(staging_cleanup_task)
            except BaseException as exc:
                task_errors.append(f"cleanup {type(exc).__name__}: {exc}")
            staging_cleanup_succeeded = self._cleanup_task_succeeded(
                staging_cleanup_task,
            )
            if not staging_cleanup_succeeded:
                task_errors.append("cleanup did not remove staging")
        for task in operation_tasks:
            try:
                await asyncio.shield(task)
            except BaseException as exc:
                task_errors.append(f"{type(exc).__name__}: {exc}")

        backup_ready = backup_task is not None and not backup_task.cancelled()
        if backup_ready:
            try:
                backup_task.result()
            except BaseException:
                backup_ready = False
        publish_succeeded = publish_task is not None and not publish_task.cancelled()
        if publish_succeeded:
            try:
                publish_task.result()
            except BaseException:
                publish_succeeded = False

        rollback_task: asyncio.Task | None = None
        if publish_succeeded:
            async with self._job_lock(job_id):
                if job_id not in self._delete_requested:
                    if had_final and backup_ready:
                        rollback_task = asyncio.create_task(asyncio.to_thread(
                            self._copy_object_sync, backup_key, final_key,
                        ))
                    elif not had_final:
                        rollback_task = asyncio.create_task(asyncio.to_thread(
                            self._client().remove_object,
                            self._bucket,
                            final_key,
                        ))
        if rollback_task is not None:
            try:
                await asyncio.shield(rollback_task)
            except Exception as exc:
                task_errors.append(f"rollback {type(exc).__name__}: {exc}")

        keys = [] if staging_cleanup_succeeded else [staging_key]
        if had_final:
            keys.append(backup_key)
        if keys:
            await self._remove_staging_keys(*keys)

        if task_errors:
            structlog.get_logger().warning(
                "storage_cancelled_upload_finalized",
                job_id=job_id,
                errors=task_errors,
            )

    async def wait_for_finalizers(self) -> None:
        while True:
            tasks = set(self._finalizer_tasks)
            for writers in self._active_writers.values():
                tasks.update(writers)
            tasks.update(
                task for task in self._delete_tasks.values() if not task.done()
            )
            if not tasks:
                return
            await asyncio.gather(
                *(asyncio.shield(task) for task in tasks),
                return_exceptions=True,
            )

    async def list_initialization_markers(self) -> list[str]:
        return await asyncio.to_thread(self._list_initialization_markers_sync)

    def _list_initialization_markers_sync(self) -> list[str]:
        prefix = _GLOBAL_INITIALIZATION_PREFIX
        suffix = "/marker.json"
        jobs: list[str] = []
        for obj in self._client().list_objects(
            self._bucket, prefix=prefix, recursive=True,
        ):
            name = getattr(obj, "object_name", "")
            if (
                not isinstance(name, str)
                or not name.startswith(prefix)
                or not name.endswith(suffix)
            ):
                continue
            job_id = name[len(prefix):-len(suffix)]
            if job_id and "/" not in job_id and not job_id.startswith("."):
                jobs.append(job_id)
        return sorted(set(jobs))

    async def cleanup_stale_staging(
        self, *, active_tokens: set[tuple[str, str]],
        protected_job_ids: set[str], stale_before_epoch: float,
    ) -> int:
        return await asyncio.to_thread(
            self._cleanup_stale_staging_sync,
            active_tokens,
            protected_job_ids,
            stale_before_epoch,
        )

    def _cleanup_stale_staging_sync(
        self,
        active_tokens: set[tuple[str, str]],
        protected_job_ids: set[str],
        stale_before_epoch: float,
    ) -> int:
        """删除已完成的陈旧暂存对象;未完成 multipart 由 bucket lifecycle 回收。"""
        self._ensure_staging_lifecycle_sync()
        client = self._client()
        candidates: list[str] = []
        errors: list[str] = []
        for obj in client.list_objects(
            self._bucket, prefix=_GLOBAL_STAGING_PREFIX, recursive=True,
        ):
            name = getattr(obj, "object_name", "")
            match = re.fullmatch(
                rf"{re.escape(_GLOBAL_STAGING_PREFIX)}([^/]+)/"
                r"([A-Za-z0-9_-]{1,128})(?:\.backup)?",
                name,
            )
            if match is None:
                errors.append(f"invalid staging object:{name}")
                continue
            job_id, token = match.groups()
            if job_id in protected_job_ids or (job_id, token) in active_tokens:
                continue
            modified = getattr(obj, "last_modified", None)
            if not isinstance(modified, datetime):
                errors.append(f"missing last_modified:{name}")
                continue
            if modified.tzinfo is None:
                modified = modified.replace(tzinfo=timezone.utc)
            if modified.timestamp() < stale_before_epoch:
                candidates.append(name)

        removed = 0
        for name in candidates:
            try:
                client.remove_object(self._bucket, name)
            except Exception as exc:
                errors.append(f"remove {name}:{type(exc).__name__}:{exc}")
            else:
                removed += 1
        if errors:
            raise OSError("staging cleanup partial: " + "; ".join(errors))
        return removed

    # 需要 lifecycle 兜底回收的 staging 前缀:上传暂存 + 执行 staging namespace
    # (崩溃遗留的 candidate/commit 记录经 1 天 Expiration 自动回收,语义与上传暂存一致)。
    _STAGING_LIFECYCLE_RULES = (
        (_STAGING_LIFECYCLE_RULE_ID, _GLOBAL_STAGING_PREFIX),
        ("flori-exec-staging-recovery", EXECUTION_STAGING_PREFIX),
    )

    def _ensure_staging_lifecycle_sync(self) -> None:
        """使用 MinIO 公共 lifecycle API 回收进程崩溃留下的 multipart 与孤儿 staging。"""
        from minio.commonconfig import ENABLED, Filter
        from minio.lifecycleconfig import (
            AbortIncompleteMultipartUpload,
            Expiration,
            LifecycleConfig,
            Rule,
        )

        client = self._client()
        try:
            current = client.get_bucket_lifecycle(self._bucket)
        except Exception as exc:
            if getattr(exc, "code", None) != "NoSuchLifecycleConfiguration":
                raise
            current = None
        rules = list(getattr(current, "rules", None) or [])

        def _rule_ok(rule, prefix: str) -> bool:
            return (
                getattr(rule, "status", None) == ENABLED
                and getattr(getattr(rule, "rule_filter", None), "prefix", None)
                == prefix
                and getattr(
                    getattr(rule, "abort_incomplete_multipart_upload", None),
                    "days_after_initiation",
                    None,
                ) == 1
                and getattr(getattr(rule, "expiration", None), "days", None) == 1
            )

        by_id = {getattr(rule, "rule_id", None): rule for rule in rules}
        if all(
            rule_id in by_id and _rule_ok(by_id[rule_id], prefix)
            for rule_id, prefix in self._STAGING_LIFECYCLE_RULES
        ):
            return
        managed_ids = {rule_id for rule_id, _ in self._STAGING_LIFECYCLE_RULES}
        rules = [
            rule for rule in rules
            if getattr(rule, "rule_id", None) not in managed_ids
        ]
        for rule_id, prefix in self._STAGING_LIFECYCLE_RULES:
            rules.append(Rule(
                ENABLED,
                Filter(prefix=prefix),
                rule_id=rule_id,
                abort_incomplete_multipart_upload=AbortIncompleteMultipartUpload(
                    days_after_initiation=1,
                ),
                expiration=Expiration(days=1),
            ))
        client.set_bucket_lifecycle(self._bucket, LifecycleConfig(rules))

    def _write_file_sync(self, job_id: str, rel_path: str, data: bytes) -> None:
        import io

        self._client().put_object(
            self._bucket, self._object_key(job_id, rel_path),
            io.BytesIO(data), length=len(data),
        )

    async def delete_file(self, job_id: str, rel_path: str) -> None:
        def _rm() -> None:
            from minio.error import S3Error
            try:
                self._client().remove_object(
                    self._bucket, self._object_key(job_id, rel_path),
                )
            except S3Error as exc:
                if not _is_s3_not_found(exc):
                    raise
        await asyncio.to_thread(_rm)

    async def read_file(self, job_id: str, rel_path: str) -> bytes | None:
        return await asyncio.to_thread(self._read_file_sync, job_id, rel_path)

    def _read_file_sync(self, job_id: str, rel_path: str) -> bytes | None:
        from minio.error import S3Error

        resp = None
        try:
            resp = self._client().get_object(
                self._bucket, self._object_key(job_id, rel_path),
            )
            return resp.read()
        except S3Error as exc:
            if _is_s3_not_found(exc):
                return None
            raise
        finally:
            if resp is not None:
                resp.close()
                resp.release_conn()

    async def file_size(self, job_id: str, rel_path: str) -> int | None:
        def _stat() -> int | None:
            from minio.error import S3Error
            try:
                return self._client().stat_object(self._bucket, f"{job_id}/{rel_path}").size
            except S3Error as exc:
                if _is_s3_not_found(exc):
                    return None
                raise
        return await asyncio.to_thread(_stat)

    async def object_version(
        self, job_id: str, rel_path: str,
    ) -> StorageObjectVersion | None:
        def _stat() -> StorageObjectVersion | None:
            from minio.error import S3Error
            try:
                value = self._client().stat_object(
                    self._bucket, f"{job_id}/{rel_path}",
                )
            except S3Error as exc:
                if _is_s3_not_found(exc):
                    return None
                raise
            size = getattr(value, "size", None)
            etag = getattr(value, "etag", None)
            version_id = getattr(value, "version_id", None)
            last_modified = getattr(value, "last_modified", None)
            if type(size) is not int or size < 0 or not isinstance(etag, str) or not etag:
                return None
            return StorageObjectVersion(
                namespace=(
                    f"minio:{'https' if self._secure else 'http'}://"
                    f"{self._endpoint}/{self._bucket}"
                ),
                size=size,
                token=f"{etag}:{version_id or ''}:{last_modified or ''}",
            )

        return await asyncio.to_thread(_stat)

    async def read_range(self, job_id: str, rel_path: str, start: int, length: int) -> bytes | None:
        def _read() -> bytes | None:
            from minio.error import S3Error
            resp = None
            try:
                resp = self._client().get_object(
                    self._bucket, f"{job_id}/{rel_path}", offset=start, length=length,
                )
                return resp.read()
            except S3Error as exc:
                if _is_s3_not_found(exc):
                    return None
                raise
            finally:
                if resp is not None:
                    resp.close()
                    resp.release_conn()
        return await asyncio.to_thread(_read)

    async def list_files(self, job_id: str) -> list[str]:
        return await asyncio.to_thread(self._list_files_sync, job_id)

    def _list_files_sync(self, job_id: str) -> list[str]:
        prefix = f"{job_id}/"
        out: list[str] = []
        for obj in self._client().list_objects(self._bucket, prefix=prefix, recursive=True):
            rel = obj.object_name[len(prefix):]
            if rel and not _is_internal_file(rel):  # 跳过前缀本身/目录占位
                out.append(rel)
        return out

    async def list_file_sizes(self, job_id: str) -> dict[str, int]:
        return await asyncio.to_thread(self._list_file_sizes_sync, job_id)

    def _list_file_sizes_sync(self, job_id: str) -> dict[str, int]:
        prefix = f"{job_id}/"
        out: dict[str, int] = {}
        # list_objects 自带 obj.size,无需逐对象 stat_object。
        for obj in self._client().list_objects(self._bucket, prefix=prefix, recursive=True):
            rel = obj.object_name[len(prefix):]
            if rel and not _is_internal_file(rel):
                out[rel] = obj.size or 0
        return out

    # 步骤产物提交协议(§2.6)

    @staticmethod
    def _execution_staging_key(job_id: str, exec_id: str, rel: str = "") -> str:
        _assert_staging_segment(job_id, "job_id")
        _assert_staging_segment(exec_id, "exec_id")
        if rel:
            if (
                rel.startswith("/") or "\x00" in rel or "\\" in rel
                or any(seg in ("", ".", "..") for seg in rel.split("/"))
            ):
                raise ValueError(f"invalid staging rel path: {rel!r}")
        return f"{EXECUTION_STAGING_PREFIX}{job_id}/{exec_id}/{rel}"

    def _server_side_copy_sync(
        self, source_key: str, target_key: str, size: int | None,
    ) -> None:
        # copy_object 单次上限 5GiB;更大用 compose_object 分段服务端拷贝,仍不经本机下行。
        from minio.commonconfig import ComposeSource, CopySource

        if size is not None and size >= _MINIO_COPY_LIMIT:
            self._client().compose_object(
                self._bucket, target_key,
                [ComposeSource(self._bucket, source_key)],
            )
        else:
            self._client().copy_object(
                self._bucket, target_key, CopySource(self._bucket, source_key),
            )

    def _hash_object_sync(self, object_key: str) -> tuple[int, str]:
        digest = hashlib.sha256()
        total = 0
        resp = None
        try:
            resp = self._client().get_object(self._bucket, object_key)
            while chunk := resp.read(1024 * 1024):
                digest.update(chunk)
                total += len(chunk)
        finally:
            if resp is not None:
                resp.close()
                resp.release_conn()
        return total, f"sha256:{digest.hexdigest()}"

    async def stage_step_output(
        self, job_id: str, exec_id: str, rel_path: str, source: Path | None, *,
        size_bytes: int, sha256: str,
    ) -> None:
        # push 成功路径已把输出上传到 canonical:优先服务端复制进 staging,免二次上行。
        # read-back 在 promote 后对 canonical 全量重验 sha,复制来源不影响完整性结论。
        if await self.stage_from_canonical(
            job_id, exec_id, rel_path, size_bytes=size_bytes,
        ):
            return
        if source is None:
            raise OSError(f"canonical object unavailable for staging: {rel_path}")
        key = self._execution_staging_key(job_id, exec_id, rel_path)
        await asyncio.to_thread(
            self._client().fput_object, self._bucket, key, str(source),
        )

    async def stage_from_canonical(
        self, job_id: str, exec_id: str, rel_path: str, *, size_bytes: int,
    ) -> bool:
        def _copy() -> bool:
            from minio.error import S3Error

            canonical = f"{job_id}/{rel_path}"
            try:
                st = self._client().stat_object(self._bucket, canonical)
            except S3Error as exc:
                if _is_s3_not_found(exc):
                    return False
                raise
            if st.size != size_bytes:
                return False
            key = self._execution_staging_key(job_id, exec_id, rel_path)
            self._server_side_copy_sync(canonical, key, st.size)
            return True

        return await asyncio.to_thread(_copy)

    async def write_execution_staging_stream(
        self, job_id: str, exec_id: str, rel_path: str, chunks: AsyncIterable[bytes], *,
        expected_size: int | None = None, expected_sha256: str | None = None,
        max_bytes: int | None = None,
    ) -> dict:
        key = self._execution_staging_key(job_id, exec_id, rel_path)
        self._tmp_root.mkdir(parents=True, exist_ok=True)
        tmp = self._tmp_root / f".flori-stagebuf-{uuid.uuid4().hex}"
        digest = hashlib.sha256()
        total = 0
        fp = await asyncio.to_thread(open, tmp, "wb")
        try:
            async for chunk in chunks:
                if not isinstance(chunk, bytes):
                    raise TypeError("staging stream yielded non-bytes")
                if not chunk:
                    continue
                total += len(chunk)
                if max_bytes is not None and total > max_bytes:
                    raise ArtifactTooLarge(f"staged output exceeds {max_bytes} bytes")
                digest.update(chunk)
                await asyncio.to_thread(fp.write, chunk)
            await asyncio.to_thread(fp.close)
            fp = None
            actual = digest.hexdigest()
            if expected_size is not None and total != expected_size:
                raise ValueError("staged output size mismatch")
            if expected_sha256 and not hmac.compare_digest(
                actual, expected_sha256.lower().removeprefix("sha256:"),
            ):
                raise ValueError("staged output checksum mismatch")
            await asyncio.to_thread(
                self._client().fput_object, self._bucket, key, str(tmp),
            )
            return {"size": total, "sha256": actual}
        finally:
            if fp is not None:
                await asyncio.to_thread(fp.close)
            await asyncio.to_thread(tmp.unlink, True)

    async def commit_step_outputs(
        self, job_id: str, execution_step: str, exec_id: str, *,
        outputs: list[dict], manifest: dict, manifest_rel: str,
        stale_paths: list[str], token: dict, commit_record: bytes,
        verify_token,
    ) -> None:
        from minio.error import S3Error

        manifest_bytes = _canonical_manifest_bytes(manifest, token)
        # 发布前交叉校验(P2-3)+ 越权路径整体拒绝(P1,任何副作用之前)。
        declared = _verify_manifest_binding(
            manifest, job_id=job_id, execution_step=execution_step,
            exec_id=exec_id, outputs=outputs,
        )
        kept = {out.get("path") for out in outputs}
        for out in outputs:
            _guard_commit_path(execution_step, out.get("path"), "output")
        for rel in stale_paths:
            if rel not in kept:
                _guard_commit_path(execution_step, rel, "stale")
        record_key = self._execution_staging_key(
            job_id, exec_id, COMMIT_RECORD_FILENAME,
        )
        # commit 记录先落 staging namespace(promote_started 持久证据,§2.7 行 1/2)。
        await asyncio.to_thread(
            self._client().put_object, self._bucket, record_key,
            io.BytesIO(commit_record), len(commit_record),
        )
        for out in outputs:
            rel = out["path"]
            staged_key = self._execution_staging_key(job_id, exec_id, rel)
            await _verify_commit_token(verify_token)
            try:
                await asyncio.to_thread(
                    self._server_side_copy_sync, staged_key,
                    f"{job_id}/{rel}", out.get("size_bytes"),
                )
            except S3Error as exc:
                if _is_s3_not_found(exc):
                    raise StepCommitIntegrityError(
                        f"staged output missing: {rel}"
                    ) from exc
                raise
            await _verify_commit_token(verify_token)
        for out in declared:
            try:
                size, sha = await asyncio.to_thread(
                    self._hash_object_sync, f"{job_id}/{out['path']}",
                )
            except S3Error as exc:
                raise StepCommitIntegrityError(
                    f"read-back failed: {out['path']}"
                ) from exc
            if size != out["size_bytes"] or sha != out["sha256"]:
                raise StepCommitIntegrityError(f"read-back mismatch: {out['path']}")
        for rel in stale_paths:
            if rel in kept:
                continue
            await self.delete_file(job_id, rel)
        await _verify_commit_token(verify_token)
        # manifest 最后发布:单对象 PUT 即原子可见性边界。
        await asyncio.to_thread(
            self._client().put_object, self._bucket, f"{job_id}/{manifest_rel}",
            io.BytesIO(manifest_bytes), len(manifest_bytes),
        )
        await _verify_commit_token(verify_token, "manifest_published")

    async def cleanup_execution_staging(self, job_id: str, exec_id: str) -> None:
        prefix = self._execution_staging_key(job_id, exec_id)

        def _cleanup() -> None:
            from minio.deleteobjects import DeleteObject

            client = self._client()
            objs = [
                DeleteObject(o.object_name)
                for o in client.list_objects(
                    self._bucket, prefix=prefix, recursive=True,
                )
            ]
            if objs:
                list(client.remove_objects(self._bucket, objs))

        await asyncio.to_thread(_cleanup)

    async def cleanup_stale_execution_staging(
        self, *, active_exec_ids: set[str], stale_before_epoch: float,
    ) -> int:
        def _cleanup() -> int:
            client = self._client()
            removed = 0
            for obj in client.list_objects(
                self._bucket, prefix=EXECUTION_STAGING_PREFIX, recursive=True,
            ):
                name = getattr(obj, "object_name", "")
                remainder = name[len(EXECUTION_STAGING_PREFIX):]
                parts = remainder.split("/", 2)
                if len(parts) < 3:
                    continue
                _job, exec_id, _rest = parts
                if exec_id in active_exec_ids:
                    continue
                modified = getattr(obj, "last_modified", None)
                if not isinstance(modified, datetime):
                    continue
                if modified.tzinfo is None:
                    modified = modified.replace(tzinfo=timezone.utc)
                if modified.timestamp() >= stale_before_epoch:
                    continue
                try:
                    client.remove_object(self._bucket, name)
                    removed += 1
                except Exception:
                    continue
            return removed

        return await asyncio.to_thread(_cleanup)

    async def health(self) -> dict:
        # bucket_exists 是 HEAD bucket(O(1)),勿用 list_objects(全量扫)。minio SDK 同步 → to_thread。
        # 容量统计(对象数/总字节)MinIO 无聚合 API,全量 list 才能求和 → 不在探活里做。
        return await asyncio.to_thread(self._health_sync)

    async def readiness_probe(self, timeout_sec: float = 3) -> dict:
        """写入并删除短生命周期 canary,证明 bucket 不只是可读."""
        return await asyncio.to_thread(self._readiness_probe_sync, timeout_sec)

    def _readiness_probe_sync(self, timeout_sec: float) -> dict:
        t0 = time.perf_counter()
        client = self._readiness_client(timeout_sec)
        payload = b"flori-readiness"
        key = f".flori-readiness/{uuid.uuid4().hex}.canary"
        uploaded = False
        try:
            client.put_object(
                self._bucket,
                key,
                io.BytesIO(payload),
                len(payload),
                content_type="application/octet-stream",
            )
            uploaded = True
            client.remove_object(self._bucket, key)
            uploaded = False
        finally:
            # put 成功但 delete 失败时再次尽力清理,同时保留首次异常让 readiness
            # fail-closed,避免静默堆积探针对象或把只写不可删误判为健康.
            if uploaded:
                try:
                    client.remove_object(self._bucket, key)
                except Exception:
                    pass
        probe_ms = round((time.perf_counter() - t0) * 1000, 1)
        # 版本采集复用同一短 I/O 预算且失败只回 None,不把可写 bucket 误判 down.
        # 首次成功后实例缓存版本,后续 canary 不再访问管理 API.
        version = self._server_version_sync(timeout_sec=self._readiness_io_timeout_sec or 0.5)
        return {
            "status": "up", "mode": "remote", "version": version,
            "bucket": self._bucket, "bucket_exists": True, "probe_ms": probe_ms,
            "detail": None,
        }

    def _health_sync(self) -> dict:
        t0 = time.perf_counter()
        exists = self._client().bucket_exists(self._bucket)
        probe_ms = round((time.perf_counter() - t0) * 1000, 1)
        return {
            "status": "up" if exists else "degraded",
            "mode": "remote", "version": self._server_version_sync(),
            "bucket": self._bucket, "bucket_exists": exists, "probe_ms": probe_ms,
            "detail": None if exists else f"bucket {self._bucket} 不存在",
        }

    def _server_version_sync(self, timeout_sec: float = 2) -> str | None:
        # 经 MinIO 管理 API(MinioAdmin.info)取服务端版本。版本近乎静态:首次成功即缓存到实例,
        # 后续 health 直接复用,避免每次新建 MinioAdmin/调 info() 的管理 API RTT 反复挤占
        # health 的 3s 探活预算,拖超时致 minio 误报 down。失败回 None 且不缓存,下次再试。
        cached = getattr(self, "_server_version", None)
        if cached:
            return cached
        try:
            from minio import MinioAdmin
            from minio.credentials import StaticProvider

            adm = MinioAdmin(
                endpoint=self._endpoint,
                credentials=StaticProvider(self._access_key, self._secret_key),
                secure=self._secure,
                http_client=self._bounded_http_client(timeout_sec),
            )
            info = json.loads(adm.info())
            self._server_version = _parse_minio_version(info)
            return self._server_version
        except Exception:
            return None

    async def capacity(self) -> dict | None:
        # 全量遍历 bucket 求对象数 + 总字节(MinIO 无聚合 API)。贵! 故 api 侧带缓存+后台定时
        # 刷新,绝不在 /api/status 同步调。同步 minio 调用包 to_thread,不阻塞事件循环。
        return await asyncio.to_thread(self._capacity_sync)

    def _capacity_sync(self) -> dict:
        objects = 0
        total = 0
        for obj in self._client().list_objects(self._bucket, recursive=True):
            total += obj.size or 0
            objects += 1
        return {"objects": objects, "bytes": total}


class GatewayStorage:
    """gateway-PROXY 产物后端:纯出站 HTTPS,产物经 API 中转(worker 永不直连 minio)。

    pull 拉清单+逐个产物到本机临时 work_dir(并记快照),push 只回传相对快照
    新增/改动的文件(语义与 RemoteStorage 一致),read/write/list 直接打 API 端点。
    pull/push 与 open/write_stream 分块传输;中心端校验完成后才原子发布上传对象.

    远端 worker 经慢链路(出站 HTTPS)连中心存储时,两个可选项把大源文件挡在链路外:
      - STORAGE_WORKDIR_REUSE=1:job 目录跨步骤复用(按 job_id 命名),pull 跳过本机
        已存在的文件、cleanup 不逐步 rmtree,改由 pull 时按 TTL GC 兄弟目录。
        于是 01_download 下载的 source.mp4 留在本机,后续 03/04/02 步直接读本地,不重拉。
      - STORAGE_NO_PUSH_GLOBS=input/source.mp4,...:匹配的文件不回传中心存储,只留本机。
        大源文件(视频/音频)因此永不上行慢链路;帧图/字幕/OCR 等小产物照常回传供 AI 步消费。
    二者默认关闭(空),不改变既有部署语义;远端重算 worker 才在 docker run 里开。
    NOTE:开了 NO_PUSH,依赖该文件的步骤必须落在持有它的同一 worker(中心存储无副本)。
    """

    def __init__(
        self,
        base_url: str,
        token_getter: Callable[[], str],
        work_dir: Path,
    ):
        self._base_url = base_url.rstrip("/")
        self._token_getter = token_getter
        self._work_root = work_dir
        # pull 时记录每个 work_dir 的文件快照(relpath -> (size, mtime)),供 push 算增量。
        self._snapshots: dict[str, dict[str, tuple[int, float]]] = {}
        self._client_obj = None
        # 跨步骤复用 job 目录(留住大源文件,免重拉);关时逐步 rmtree。
        self._reuse = os.environ.get("STORAGE_WORKDIR_REUSE", "") not in ("", "0", "false")
        # 复用模式下,pull 时回收超过 TTL 未活动的兄弟 job 目录(默认 2h),给磁盘兜底。
        self._gc_ttl = int(os.environ.get("STORAGE_WORKDIR_GC_TTL_SEC", "7200"))
        # 不回传中心存储的文件 glob(相对 work_dir,fnmatch);默认空=全推。
        self._no_push = [
            g.strip() for g in os.environ.get("STORAGE_NO_PUSH_GLOBS", "").split(",") if g.strip()
        ]

    def _client(self):
        # 延迟建 httpx.AsyncClient:构造不连接(便于选型/单测),首次用到才建。
        if self._client_obj is None:
            import httpx

            from shared.net import gateway_tls_verify

            self._client_obj = httpx.AsyncClient(
                base_url=self._base_url, timeout=60, verify=gateway_tls_verify(),
            )
        return self._client_obj

    def _auth(self, job_id: str | None = None) -> dict:
        headers = {"Authorization": f"Bearer {self._token_getter()}"}
        lease = current_task_lease()
        if lease is not None:
            if job_id is not None and lease.job_id != job_id:
                raise RuntimeError("gateway artifact request does not match current task lease")
            headers.update({
                "X-Flori-Lease-Job": lease.job_id,
                "X-Flori-Lease-Step": lease.step,
                "X-Flori-Lease-Exec": lease.exec_id,
            })
        return headers

    async def pull(self, job_id: str, step: str) -> Path:
        if self._reuse:
            await asyncio.to_thread(self._gc_stale, job_id)
        from shared.step_scope import parse_execution_step
        scope_key, _ = parse_execution_step(step)
        scope_token = hashlib.sha256(scope_key.encode()).hexdigest()[:16]
        if self._reuse:
            work_dir = (
                self._work_root / job_id
                if scope_key == "job"
                else self._work_root / job_id / scope_token / "root"
            )
        else:
            work_dir = self._work_root / job_id / scope_token / uuid.uuid4().hex / "root"
        work_dir.mkdir(parents=True, exist_ok=True)
        rels = await self.list_files(job_id)
        for rel in rels:
            if not execution_artifact_allowed(step, rel, write=False):
                continue
            dest = work_dir / rel
            # 复用模式:本机已有同名文件就不重拉(留住的 source.mp4 不走慢链路下行)。
            if self._reuse and dest.is_file():
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            staging = dest.with_name(f".{dest.name}.flori-part-{uuid.uuid4().hex}")
            # 流式下载到磁盘:大产物(未配 NO_PUSH 的源文件)不整体载入内存。
            try:
                async with self._client().stream(
                    "GET", f"/api/runner/jobs/{job_id}/artifacts/{rel}",
                    headers=self._auth(job_id),
                ) as resp:
                    _raise_gateway_auth(resp, f"/api/runner/jobs/{job_id}/artifacts/{rel}")
                    resp.raise_for_status()
                    with open(staging, "wb") as f:
                        async for chunk in resp.aiter_bytes(65536):
                            f.write(chunk)
                        f.flush()
                        os.fsync(f.fileno())
                os.replace(staging, dest)
            finally:
                staging.unlink(missing_ok=True)
        # 快照覆盖 work_dir 全部本机文件(含复用留下的),push 才能据此跳过未改动的文件。
        snapshot: dict[str, tuple[int, float]] = {}
        for path in work_dir.rglob("*"):
            if not path.is_symlink() and path.is_file():
                st = path.stat()
                snapshot[path.relative_to(work_dir).as_posix()] = (st.st_size, st.st_mtime)
        self._snapshots[str(work_dir)] = snapshot
        if self._reuse:
            await asyncio.to_thread(os.utime, work_dir, None)  # 标记活动时间,供 GC 判活
        return work_dir

    def _gc_stale(self, current_job_id: str) -> None:
        """复用模式回收:删超过 TTL 未活动的兄弟 job 目录,给磁盘兜底。失败不致命。"""
        if not self._work_root.exists():
            return
        cutoff = time.time() - self._gc_ttl
        for child in self._work_root.iterdir():
            if child.name == current_job_id or not child.is_dir():
                continue
            try:
                if child.stat().st_mtime < cutoff:
                    shutil.rmtree(child, ignore_errors=True)
            except OSError:
                continue

    async def push(
        self, job_id: str, step: str, work_dir: Path, *,
        exclude_paths: set[str] | None = None,
        only_globs: list[str] | None = None,
    ) -> None:
        snapshot = self._snapshots.get(str(work_dir), {})
        excluded = exclude_paths or set()
        for path in work_dir.rglob("*"):
            if path.is_symlink() or not path.is_file():
                continue
            rel = path.relative_to(work_dir).as_posix()
            if (
                rel in excluded
                or is_credential_file(rel)
                or _is_internal_file(rel)
                or self._is_no_push(step, rel)
                or not execution_artifact_allowed(step, rel, write=True)
            ):
                continue  # 敏感凭证 / 大源文件等:不回传中心(凭证绝不上行;源文件配 NO_PUSH glob)
            if only_globs is not None and not any(
                fnmatch.fnmatch(rel, pattern) for pattern in only_globs
            ):
                continue  # 失败路径诊断白名单:业务输出不上行(设计稿 §2.5-5)
            st = path.stat()
            prev = snapshot.get(rel)
            if prev is not None and prev == (st.st_size, st.st_mtime):
                continue  # 未改动,跳过
            # 流式分块上传 + 独立长超时:视频等大产物几百 MB,经公网网关(如边缘反代)
            # 60s 全局超时必挂(实测新裸金属 worker 首批下载 push 超时,httpx 异常 str 还为空);
            # 整文件 read_bytes 也会顶满内存。read 超时按块计,900s 兜住慢链路整体传输。
            import httpx

            async def _chunks(fp=path):
                with open(fp, "rb") as f:
                    while chunk := f.read(1 << 20):
                        yield chunk

            def _sha256() -> str:
                digest = hashlib.sha256()
                with open(path, "rb") as f:
                    while chunk := f.read(1 << 20):
                        digest.update(chunk)
                return digest.hexdigest()

            checksum = await asyncio.to_thread(_sha256)
            headers = self._auth(job_id)
            headers.update({
                "Content-Length": str(st.st_size),
                "X-Content-SHA256": checksum,
            })

            resp = await self._client().put(
                f"/api/runner/jobs/{job_id}/artifacts/{rel}",
                headers=headers, content=_chunks(),
                timeout=httpx.Timeout(900, connect=15),
            )
            _raise_gateway_auth(resp, f"/api/runner/jobs/{job_id}/artifacts/{rel}")
            resp.raise_for_status()

    def _is_no_push(self, step: str, rel: str) -> bool:
        from shared.step_scope import parse_execution_step, part_id_from_scope

        scope_key, _ = parse_execution_step(step)
        part_id = part_id_from_scope(scope_key)
        candidate = rel
        if part_id is not None:
            prefix = f"parts/{part_id}/"
            if rel.startswith(prefix):
                candidate = rel[len(prefix):]
        return any(fnmatch.fnmatch(candidate, pattern) for pattern in self._no_push)

    async def cleanup(self, job_id: str, step: str, work_dir: Path) -> None:
        self._snapshots.pop(str(work_dir), None)
        # 复用模式留住 job 目录(同 job 后续步直接读本地),由 pull 时 TTL GC 回收。
        if self._reuse:
            return
        def _cleanup_attempt() -> None:
            attempt_dir = work_dir.parent
            shutil.rmtree(attempt_dir, ignore_errors=True)
            for parent in (attempt_dir.parent, attempt_dir.parent.parent):
                try:
                    parent.rmdir()
                except OSError:
                    pass

        await asyncio.to_thread(_cleanup_attempt)

    async def delete(
        self, job_id: str, *, defer_if_busy: bool = False,
    ) -> None:
        # worker 侧 gateway 不负责删中心产物(那是 API/中心存储的职责,删 job 在 API 端走 Local/Remote);
        # 这里仅清掉本机为该 job 留存的(复用)工作目录与快照,保证幂等。
        work_dir = self._work_root / job_id
        for snapshot in list(self._snapshots):
            if Path(snapshot) == work_dir or work_dir in Path(snapshot).parents:
                self._snapshots.pop(snapshot, None)
        await asyncio.to_thread(shutil.rmtree, work_dir, ignore_errors=True)

    async def clone(self, src_job_id: str, dst_job_id: str) -> None:
        # worker 侧 gateway 不负责中心产物复制(fork 重建在 API/中心存储侧走 Local/Remote)。
        raise NotImplementedError("GatewayStorage.clone: fork rebuild runs on API/central storage (Local/Remote)")

    async def delete_file(self, job_id: str, rel_path: str) -> None:
        # 中心产物变更是 API/中心侧职责(与 clone 同例);worker 侧不需要也不允许删中心 .done。
        raise NotImplementedError("GatewayStorage.delete_file: central artifact mutation runs on API/central storage")

    async def read_file(self, job_id: str, rel_path: str) -> bytes | None:
        resp = await self._client().get(
            f"/api/runner/jobs/{job_id}/artifacts/{rel_path}", headers=self._auth(job_id),
        )
        _raise_gateway_auth(resp, f"/api/runner/jobs/{job_id}/artifacts/{rel_path}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.content

    async def open_stream(
        self, job_id: str, rel_path: str, *, start: int = 0,
        length: int | None = None, chunk_size: int = 1024 * 1024,
    ) -> AsyncIterator[bytes] | None:
        headers = self._auth(job_id)
        if length is not None:
            headers["Range"] = f"bytes={start}-{start + length - 1}"
        elif start:
            headers["Range"] = f"bytes={start}-"

        async def _chunks() -> AsyncIterator[bytes]:
            async with self._client().stream(
                "GET", f"/api/runner/jobs/{job_id}/artifacts/{rel_path}", headers=headers,
            ) as resp:
                _raise_gateway_auth(resp, f"/api/runner/jobs/{job_id}/artifacts/{rel_path}")
                resp.raise_for_status()
                async for chunk in resp.aiter_bytes(chunk_size):
                    yield chunk

        return _chunks()

    async def write_file(self, job_id: str, rel_path: str, data: bytes) -> None:
        headers = self._auth(job_id)
        headers.update({
            "Content-Length": str(len(data)),
            "X-Content-SHA256": hashlib.sha256(data).hexdigest(),
        })
        resp = await self._client().put(
            f"/api/runner/jobs/{job_id}/artifacts/{rel_path}",
            headers=headers, content=data,
        )
        _raise_gateway_auth(resp, f"/api/runner/jobs/{job_id}/artifacts/{rel_path}")
        resp.raise_for_status()

    async def write_stream(
        self, job_id: str, rel_path: str, chunks: AsyncIterable[bytes], *,
        expected_size: int | None = None, expected_sha256: str | None = None,
        max_bytes: int | None = None, staging_token: str | None = None,
    ) -> dict:
        headers = self._auth(job_id)
        if expected_size is not None:
            headers["Content-Length"] = str(expected_size)
        if expected_sha256:
            headers["X-Content-SHA256"] = expected_sha256
        resp = await self._client().put(
            f"/api/runner/jobs/{job_id}/artifacts/{rel_path}",
            headers=headers,
            content=chunks,
        )
        _raise_gateway_auth(resp, f"/api/runner/jobs/{job_id}/artifacts/{rel_path}")
        resp.raise_for_status()
        data = resp.json()
        return {"size": int(data.get("size") or expected_size or 0),
                "sha256": data.get("sha256") or expected_sha256}

    async def list_initialization_markers(self) -> list[str]:
        return []

    async def cleanup_stale_staging(
        self, *, active_tokens: set[tuple[str, str]],
        protected_job_ids: set[str], stale_before_epoch: float,
    ) -> int:
        return 0

    async def wait_for_finalizers(self) -> None:
        pass

    # 步骤产物提交协议(§2.6):staging/promote/manifest 全部由中心侧执行,
    # worker 只经 runner 端点提交意图与字节;exec 身份由任务租约头携带,不进 URL。

    async def stage_step_output(
        self, job_id: str, exec_id: str, rel_path: str, source: Path | None, *,
        size_bytes: int, sha256: str,
    ) -> None:
        import httpx

        endpoint = f"/api/runner/jobs/{job_id}/staging/copy"
        resp = await self._client().post(
            endpoint, headers=self._auth(job_id),
            json={"path": rel_path, "size_bytes": size_bytes, "sha256": sha256},
        )
        _raise_gateway_auth(resp, endpoint)
        resp.raise_for_status()
        if resp.json().get("staged"):
            return  # push 已把该输出上传 canonical,中心服务端复制进 staging,免二次过慢链路
        if source is None:
            raise OSError(f"canonical object unavailable for staging: {rel_path}")

        async def _chunks(fp=Path(source)):
            with open(fp, "rb") as handle:
                while chunk := handle.read(1 << 20):
                    yield chunk

        headers = self._auth(job_id)
        headers.update({
            "Content-Length": str(size_bytes),
            "X-Content-SHA256": sha256.removeprefix("sha256:"),
        })
        put_endpoint = f"/api/runner/jobs/{job_id}/staging/{rel_path}"
        resp = await self._client().put(
            put_endpoint, headers=headers, content=_chunks(),
            timeout=httpx.Timeout(900, connect=15),
        )
        _raise_gateway_auth(resp, put_endpoint)
        resp.raise_for_status()

    async def stage_from_canonical(
        self, job_id: str, exec_id: str, rel_path: str, *, size_bytes: int,
    ) -> bool:
        # 中心侧能力;worker 侧经 stage_step_output 的 copy 请求间接使用。
        return False

    async def write_execution_staging_stream(
        self, job_id: str, exec_id: str, rel_path: str, chunks: AsyncIterable[bytes], *,
        expected_size: int | None = None, expected_sha256: str | None = None,
        max_bytes: int | None = None,
    ) -> dict:
        raise NotImplementedError(
            "GatewayStorage.write_execution_staging_stream: staging persistence runs on central storage"
        )

    async def commit_step_outputs(
        self, job_id: str, execution_step: str, exec_id: str, *,
        outputs: list[dict], manifest: dict, manifest_rel: str,
        stale_paths: list[str], token: dict, commit_record: bytes,
        verify_token,
    ) -> None:
        import httpx

        # 早停:中心会权威复验 token,这里先廉价拒绝明显陈旧的执行。
        await _verify_commit_token(verify_token)
        endpoint = f"/api/runner/jobs/{job_id}/steps/{execution_step}/commit"
        resp = await self._client().post(
            endpoint, headers=self._auth(job_id),
            json={
                "token": token,
                "outputs": outputs,
                "manifest": manifest,
                "manifest_rel": manifest_rel,
                "stale_paths": stale_paths,
            },
            timeout=httpx.Timeout(900, connect=15),
        )
        _raise_gateway_auth(resp, endpoint)
        if resp.status_code == 409:
            raise StepCommitFenceRejected("central commit fence rejected the token")
        if resp.status_code == 422:
            raise StepCommitIntegrityError(resp.text[:300])
        resp.raise_for_status()

    async def cleanup_execution_staging(self, job_id: str, exec_id: str) -> None:
        import httpx

        endpoint = f"/api/runner/jobs/{job_id}/staging"
        try:
            resp = await self._client().delete(
                endpoint, headers=self._auth(job_id),
            )
            _raise_gateway_auth(resp, endpoint)
            resp.raise_for_status()
        except WorkerAuthRejected:
            raise
        except httpx.HTTPError:
            pass  # staging 清理是 best-effort;孤儿由中心 TTL 清理兜底

    async def cleanup_stale_execution_staging(
        self, *, active_exec_ids: set[str], stale_before_epoch: float,
    ) -> int:
        return 0

    async def list_files(self, job_id: str) -> list[str]:
        resp = await self._client().get(
            f"/api/runner/jobs/{job_id}/artifacts", headers=self._auth(job_id),
        )
        _raise_gateway_auth(resp, f"/api/runner/jobs/{job_id}/artifacts")
        resp.raise_for_status()
        return resp.json().get("files", [])

    # gateway 仅供 worker 拉/回传产物,前端 /artifacts 体积透出走 API 端 Local/Remote;此处不参与 → 返回空。
    async def list_file_sizes(self, job_id: str) -> dict[str, int]:
        return {}

    # 只取 Range 元数据,验证路径不得为求大小先下载完整产物。
    async def file_size(self, job_id: str, rel_path: str) -> int | None:
        headers = self._auth(job_id)
        headers["Range"] = "bytes=0-0"
        endpoint = f"/api/runner/jobs/{job_id}/artifacts/{rel_path}"
        async with self._client().stream("GET", endpoint, headers=headers) as resp:
            _raise_gateway_auth(resp, endpoint)
            if resp.status_code == 404:
                return None
            content_range = resp.headers.get("Content-Range", "")
            if resp.status_code == 416:
                match = re.fullmatch(r"bytes \*/(\d+)", content_range.strip())
                if match and int(match.group(1)) == 0:
                    return 0
                resp.raise_for_status()
            if resp.status_code == 206:
                match = re.fullmatch(r"bytes \d+-\d+/(\d+)", content_range.strip())
                if not match:
                    raise ValueError("gateway artifact size metadata is invalid")
                return int(match.group(1))
            if resp.status_code == 200:
                value = resp.headers.get("Content-Length", "")
                if not re.fullmatch(r"\d+", value.strip()):
                    raise ValueError("gateway artifact size metadata is invalid")
                return int(value)
            resp.raise_for_status()
            raise ValueError("gateway artifact size metadata is invalid")

    async def object_version(
        self, job_id: str, rel_path: str,
    ) -> StorageObjectVersion | None:
        # Runner artifact 响应没有可信 ETag/Last-Modified,不能用 size 代替对象版本。
        return None

    async def read_range(self, job_id: str, rel_path: str, start: int, length: int) -> bytes | None:
        data = await self.read_file(job_id, rel_path)
        return data[start:start + length] if data is not None else None

    async def health(self) -> dict:
        # worker 侧网关存储,不参与 /api/status 的 minio 探活(那查的是 API 自己的中心存储)。
        # 仅满足 Protocol,标 unknown(gateway 中转)。
        return {"status": "unknown", "mode": "gateway", "bucket": None,
                "version": None, "detail": "gateway proxy", "probe_ms": None}

    async def capacity(self) -> dict | None:
        # worker 侧网关存储不查中心容量(那是 API 端 Remote/Local 的职责);仅满足 Protocol。
        return None

    async def close(self) -> None:
        if self._client_obj is not None:
            await self._client_obj.aclose()
            self._client_obj = None


def create_storage(jobs_dir: Path) -> StorageBackend:
    """设了 MINIO_URL 用对象存储(分布式 worker),否则本地。"""
    endpoint = os.environ.get("MINIO_URL")
    if endpoint:
        return RemoteStorage(
            endpoint=endpoint,
            access_key=os.environ.get("MINIO_ACCESS_KEY", ""),
            secret_key=os.environ.get("MINIO_SECRET_KEY", ""),
            bucket=os.environ.get("MINIO_BUCKET", "flori"),
            secure=os.environ.get("MINIO_SECURE", "0") == "1",
            tmp_root=Path(os.environ.get("WORK_DIR", "/tmp/flori-work")),
        )
    return LocalStorage(jobs_dir)
