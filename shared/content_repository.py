"""便携内容仓库纯逻辑:CAS blob、record、snapshot、refs、receipts、锁、GC mark 与 scrub。

对应设计稿 05 号 §2.2/§2.3/§2.8/§2.14。本模块只依赖本地文件系统与
shared.content_policy / shared.step_manifest,不触碰 DB、Redis、MinIO;
在线备份编排(DB 视图、manifest 枚举、M1/M2 重读)由 content_backup 单元实现。

不变量:
- blob key = 文件字节 SHA-256;record/snapshot digest = canonical body SHA-256。
- 一切写入先落 tmp/,校验通过后 create-if-absent 发布;同 digest 已存在必须
  重新核对,字节不同视为仓库损坏,永不覆盖。
- snapshot 不含时间/主机字段,同一逻辑状态得到同一 digest;时间与统计进 receipt。
- refs 原子替换且最后更新;任何校验失败都不得动 ref。
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import socket
import time
import stat as stat_module
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Iterator, Mapping

import structlog

from .content_policy import (
    MAX_RECEIPT_CANONICAL_BYTES,
    MAX_RECORD_CANONICAL_BYTES,
    MAX_SNAPSHOT_CANONICAL_BYTES,
    PolicyError,
    RECORD_KINDS,
    ensure_regular_file,
    load_bounded_json,
    record_blob_refs,
    scan_json_for_secrets,
    validate_audit_text,
    validate_record,
)
# 时间戳/digest 形态的单一来源在 step_manifest(同 content_policy 的引用理由)。
from .step_manifest import (
    MANIFEST_FORMAT,
    MANIFEST_FORMAT_VERSION,
    ManifestError,
    _validate_utc_timestamp,
    canonical_json_bytes,
    validate_digest,
    validate_job_id,
)
from .step_scope import _SEGMENT_RE


REPOSITORY_FORMAT = "flori-portable-repository/v1"
LEGACY_SNAPSHOT_FORMAT = "flori-portable-snapshot/v1"
SNAPSHOT_FORMAT = "flori-portable-snapshot/v2"
SOURCE_MANIFEST_FORMAT = f"{MANIFEST_FORMAT}/v{MANIFEST_FORMAT_VERSION}"

# snapshot.records 的分组契约(§2.3):组名固定,每组只允许承载列出的 kind。
SNAPSHOT_RECORD_GROUPS: Mapping[str, frozenset[str]] = {
    "jobs": frozenset({"job_core", "job_user_state", "job_relation"}),
    "parts": frozenset({"part_core"}),
    "step_results": frozenset({"step_result"}),
    "failures": frozenset({"failure_event"}),
    "business_ledgers": frozenset({
        "collection", "ingested_item", "glossary", "definition_version",
        "prompt_override", "prompt_override_version", "study", "ai_usage",
        "ai_task_log", "user_config", "legacy_archive",
    }),
}

_LEGACY_SNAPSHOT_TOP_KEYS = frozenset({
    "format", "repository_format", "source", "selector", "records", "blob_refs",
    "relations_digest", "policy",
})
_SNAPSHOT_TOP_KEYS = _LEGACY_SNAPSHOT_TOP_KEYS | {"completeness"}
_SNAPSHOT_SOURCE_KEYS = frozenset({"app_version", "db_user_version", "manifest_format"})
# selector 进入 snapshot identity:局部快照与全量快照即使记录集合恰好相同也是
# 两个不同事实(前者不代表"系统全貌"),digest 必须可区分。
_SNAPSHOT_SELECTOR_KEYS = frozenset({"partial", "job_ids"})
# policy 是硬门(§2.3):值不对不是"配置不同",是契约违规。这两个恒为定值。
_SNAPSHOT_POLICY_FIXED = {
    "successful_artifacts_only": True,
    "runtime_state_included": False,
}
# secrets_included 不恒为 false:操作者用 --allow-secret-blob-file 放行过的字节,
# 谁也证明不了它不含密钥,把 false 硬编码进去等于让快照替人担保。改为与
# secret_scan_exceptions 双向一致 —— 有放行就必须承认,无放行才准断言 false,
# 且放行清单进 snapshot digest,事后改不动。
_SNAPSHOT_POLICY_KEYS = frozenset(
    set(_SNAPSHOT_POLICY_FIXED) | {"secrets_included", "secret_scan_exceptions"}
)
_SNAPSHOT_COMPLETENESS_KEYS = frozenset({
    "terminal_steps", "manifests_seen", "manifests_missing", "manifests_excluded",
    "ai_config_complete", "user_config_complete", "secret_scan_complete",
    "media_self_contained", "external_media_roots", "portable_ready",
    "readiness_reasons",
})

_RECEIPT_REQUIRED_KEYS = frozenset({"run_id", "observed_at", "outcome"})
_RECEIPT_OPTIONAL_KEYS = frozenset({
    "snapshot_digest", "hit_existing_snapshot", "stats", "source_instance", "error",
    "request_digest",
})
_RECEIPT_OUTCOMES = frozenset({"success", "failed", "in_progress"})

_HEX2_RE = re.compile(r"^[0-9a-f]{2}$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_REF_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,100}$")
# receipt 文件名前缀是零填充 epoch 微秒:字典序即真实时序,GC 的"最近 N 条"
# 保留窗口依赖这一点;不能用显示串过滤(小数秒/+00:00 会破坏排序)。
_RECEIPT_ID_RE = re.compile(r"^[0-9]{20}-[0-9a-f]{8}$")

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

_log = structlog.get_logger(__name__)


def validate_ref_name(name: object) -> str:
    """ref 名校验;编排层在取锁前先用它把非法名挡掉,不必先占锁再失败。"""
    if type(name) is not str or not _REF_NAME_RE.fullmatch(name):
        raise RepositoryError(f"ref name {name!r} is invalid")
    return name


def _receipt_time_prefix(observed_at: str) -> str:
    """已验证的 RFC3339 UTC 时间戳 -> 20 位零填充 epoch 微秒;整数运算免浮点丢精度。"""
    delta = datetime.fromisoformat(observed_at) - _EPOCH
    micros = (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds
    if micros < 0:
        raise RepositoryError("receipt.observed_at: must not be before 1970-01-01")
    return f"{micros:020d}"

_TOP_ENTRIES = frozenset({
    "repository.json", "blobs", "records", "snapshots", "refs", "receipts",
    "tmp", "locks",
})
_CHUNK_SIZE = 1024 * 1024
_WRITE_LOCK_NAME = "write.lock"


class RepositoryError(ValueError):
    """仓库操作违规或不一致;fail-closed。"""


class RepositoryCorruptionError(RepositoryError):
    """已落盘内容与其 digest/契约不符;禁止用同 digest 覆盖修复(§2.14.6)。"""


class RepositoryLockError(RepositoryError):
    """backup/GC 写锁冲突或释放者身份不符。"""


@dataclass(frozen=True)
class BlobPut:
    digest: str
    size_bytes: int
    created: bool


@dataclass(frozen=True)
class RecordPut:
    kind: str
    digest: str
    created: bool


@dataclass(frozen=True)
class SnapshotPut:
    digest: str
    created: bool


@dataclass(frozen=True)
class GCPlan:
    """mark 阶段产物:可达集合与 dry-run 待删清单(§2.14.3);本单元不做 sweep。"""
    reachable_snapshots: tuple[str, ...]
    reachable_records: tuple[tuple[str, str], ...]
    reachable_blobs: tuple[str, ...]
    unreachable_snapshots: tuple[str, ...]
    unreachable_records: tuple[tuple[str, str], ...]
    unreachable_blobs: tuple[str, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class ScrubIssue:
    kind: str
    path: str
    detail: str


@dataclass(frozen=True)
class ScrubReport:
    checked_blobs: int
    checked_records: int
    checked_snapshots: int
    checked_refs: int
    checked_receipts: int
    issues: tuple[ScrubIssue, ...]

    @property
    def ok(self) -> bool:
        return not self.issues


def _digest_hex(digest: object, field: str) -> str:
    try:
        validate_digest(digest, field)
    except ManifestError as exc:
        raise RepositoryError(str(exc)) from exc
    return digest.split(":", 1)[1]  # type: ignore[union-attr]


def _hash_stream(read) -> tuple[str, int]:
    hasher = hashlib.sha256()
    total = 0
    while True:
        chunk = read(_CHUNK_SIZE)
        if not chunk:
            break
        hasher.update(chunk)
        total += len(chunk)
    return "sha256:" + hasher.hexdigest(), total


def _hash_file(path: Path) -> tuple[str, int]:
    with open(path, "rb") as handle:
        return _hash_stream(handle.read)


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _absolute_path_without_symlinks(path: Path) -> Path:
    """规范化仓库路径并拒绝任一已存在祖先分量的 symlink。"""
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        try:
            value = os.lstat(current)
        except FileNotFoundError:
            break
        if stat_module.S_ISLNK(value.st_mode):
            raise RepositoryError(
                f"repository path contains a symlink component: {current}"
            )
    if absolute.resolve(strict=False) != absolute:
        raise RepositoryError(f"repository path resolution drifted: {absolute}")
    return absolute


def _ensure_directory_durable(path: Path) -> None:
    """创建目录链并同步每一级父目录,避免断电后只剩引用不见对象目录。"""
    missing: list[Path] = []
    current = path
    while not current.exists():
        missing.append(current)
        if current.parent == current:
            break
        current = current.parent
    if current.is_symlink() or not current.is_dir():
        raise RepositoryCorruptionError(f"repository directory {current} is unsafe")
    for directory in reversed(missing):
        try:
            os.mkdir(directory)
        except FileExistsError:
            pass
        if directory.is_symlink() or not directory.is_dir():
            raise RepositoryCorruptionError(f"repository directory {directory} is unsafe")
        _fsync_dir(directory.parent)


class ContentRepository:
    """flori-portable-repository/v1 的本地目录实现。

    读操作(get/iter/gc_mark/scrub)无锁并发安全:已发布对象不可变。
    写操作(put_*/set_ref/write_receipt/clean_tmp)按 §2.7 应在 backup/GC
    写锁内进行;本层不强制,由编排层(content_backup/GC 入口)持锁调用。
    """

    def __init__(self, root: Path):
        self.root = Path(root)
        # 本实例内已全量验证(含闭包)的 snapshot digest:set_ref 复用该结论,
        # 免于紧跟 put_snapshot 的重复重读。仅进程内信任通道,不跨进程持久。
        self._verified_snapshots: set[str] = set()


    @classmethod
    def create(cls, root: Path) -> "ContentRepository":
        """初始化空仓库;目标目录已存在且非空则拒绝,不做任何覆盖或迁移。"""
        root = _absolute_path_without_symlinks(Path(root))
        if root.exists() or root.is_symlink():
            if root.is_symlink() or not root.is_dir() or any(root.iterdir()):
                raise RepositoryError(f"repository root {root} exists and is not empty")
        _ensure_directory_durable(root)
        for name in ("blobs/sha256", "records", "snapshots", "refs", "receipts", "tmp", "locks"):
            _ensure_directory_durable(root / name)
        marker = canonical_json_bytes({"format": REPOSITORY_FORMAT})
        tmp = root / "tmp" / f"repository-{secrets.token_hex(8)}.json"
        tmp.write_bytes(marker)
        os.replace(tmp, root / "repository.json")
        _fsync_dir(root)
        return cls(root)

    @classmethod
    def open(cls, root: Path) -> "ContentRepository":
        """打开既有仓库并验 format:未知版本拒绝打开,升级须显式迁移(§2.2 规则 7)。"""
        root = _absolute_path_without_symlinks(Path(root))
        if root.is_symlink() or not root.is_dir():
            raise RepositoryError(f"{root} is not a safe portable content repository")
        marker_path = root / "repository.json"
        if marker_path.is_symlink() or not marker_path.is_file():
            raise RepositoryError(f"{root} is not a portable content repository")
        try:
            marker = load_bounded_json(
                marker_path.read_bytes(), "repository.json", max_bytes=4096,
            )
        except PolicyError as exc:
            raise RepositoryCorruptionError(f"repository.json: {exc}") from exc
        if type(marker) is not dict or set(marker) != {"format"}:
            raise RepositoryCorruptionError("repository.json: unexpected structure")
        if marker["format"] != REPOSITORY_FORMAT:
            raise RepositoryError(
                f"unsupported repository format {marker['format']!r}; "
                f"this build only reads {REPOSITORY_FORMAT}"
            )
        for name in ("blobs", "blobs/sha256", "records", "snapshots", "refs", "receipts", "tmp", "locks"):
            directory = root / name
            if directory.is_symlink() or not directory.is_dir():
                raise RepositoryCorruptionError(
                    f"repository directory {name!r} is missing or unsafe"
                )
        return cls(root)


    def _blob_dir(self) -> Path:
        return self.root / "blobs" / "sha256"

    def blob_path(self, digest: str) -> Path:
        hexdigest = _digest_hex(digest, "blob digest")
        return self._blob_dir() / hexdigest[:2] / hexdigest

    def _record_path(self, kind: str, digest: str) -> Path:
        if kind not in RECORD_KINDS:
            raise RepositoryError(f"record kind {kind!r} is not defined")
        hexdigest = _digest_hex(digest, "record digest")
        return self.root / "records" / kind / f"{hexdigest}.json"

    def _snapshot_path(self, digest: str) -> Path:
        hexdigest = _digest_hex(digest, "snapshot digest")
        return self.root / "snapshots" / f"{hexdigest}.json"

    def _tmp_file(self, hint: str) -> Path:
        return self.root / "tmp" / f"{hint}-{secrets.token_hex(8)}"


    def _publish(self, tmp: Path, final: Path, digest: str, what: str) -> bool:
        """tmp -> create-if-absent 发布;返回是否新建。

        用 os.link 而非 rename:link 在目标已存在时失败,天然 create-if-absent,
        并发同 digest 写入也不会互相覆盖。已存在路径必须重新核对(§2.2 规则 5)。
        """
        _ensure_directory_durable(final.parent)
        try:
            os.link(tmp, final)
        except FileExistsError:
            os.unlink(tmp)
            self._verify_existing(final, digest, what)
            return False
        os.unlink(tmp)
        _fsync_dir(final.parent)
        return True

    def _verify_existing(self, final: Path, digest: str, what: str) -> None:
        try:
            ensure_regular_file(final, what)
        except PolicyError as exc:
            raise RepositoryCorruptionError(str(exc)) from exc
        actual, _ = _hash_file(final)
        if actual != digest:
            raise RepositoryCorruptionError(
                f"{what}: existing object {final.name} does not match digest {digest}; "
                "refusing to overwrite"
            )

    def _write_tmp_bytes(self, data: bytes, hint: str) -> Path:
        tmp = self._tmp_file(hint)
        with open(tmp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        return tmp


    def put_blob_bytes(self, data: bytes) -> BlobPut:
        if type(data) is not bytes:
            raise RepositoryError("blob: data must be bytes")
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        final = self.blob_path(digest)
        tmp = self._write_tmp_bytes(data, "blob")
        created = self._publish(tmp, final, digest, "blob")
        return BlobPut(digest=digest, size_bytes=len(data), created=created)

    def put_blob_file(self, source: Path) -> BlobPut:
        """流式收纳文件字节:边读边 hash 到 tmp,防大视频占内存;来源须为普通文件。"""
        source = Path(source)
        try:
            st = ensure_regular_file(source, "blob source")
        except PolicyError as exc:
            raise RepositoryError(str(exc)) from exc
        tmp = self._tmp_file("blob")
        hasher = hashlib.sha256()
        total = 0
        with open(source, "rb") as reader, open(tmp, "wb") as writer:
            while True:
                chunk = reader.read(_CHUNK_SIZE)
                if not chunk:
                    break
                hasher.update(chunk)
                total += len(chunk)
                writer.write(chunk)
            writer.flush()
            os.fsync(writer.fileno())
        # 读到的字节数与 lstat 不一致 = 来源在收纳期间被改写,fail-closed(§2.7.5)。
        if total != st.st_size:
            os.unlink(tmp)
            raise RepositoryError(
                f"blob source: size changed during ingest ({st.st_size} -> {total})"
            )
        digest = "sha256:" + hasher.hexdigest()
        created = self._publish(tmp, self.blob_path(digest), digest, "blob")
        return BlobPut(digest=digest, size_bytes=total, created=created)

    def adopt_blob_file(self, source: Path) -> BlobPut:
        """收编已位于本仓库 tmp/ 的文件为 blob:原地 hash 后直接 link,省第二遍拷贝。

        source 必须在 tmp_dir 内(同文件系统才能 link,且避免误吞仓库外文件)。
        成功或撞已有 digest 都会消费掉 source;校验失败时保留 source 交调用方处置。
        """
        source = Path(source)
        if source.parent.resolve() != self.tmp_dir.resolve():
            raise RepositoryError(
                f"adopt_blob_file: source must live in {self.tmp_dir}, got {source}"
            )
        try:
            st = ensure_regular_file(source, "blob source")
        except PolicyError as exc:
            raise RepositoryError(str(exc)) from exc
        digest, total = _hash_file(source)
        if total != st.st_size:
            raise RepositoryError(
                f"blob source: size changed during adopt ({st.st_size} -> {total})"
            )
        created = self._publish(source, self.blob_path(digest), digest, "blob")
        return BlobPut(digest=digest, size_bytes=total, created=created)

    @property
    def tmp_dir(self) -> Path:
        """仓库 tmp/ 目录;调用方把大对象直接 spool 到这里再 adopt_blob_file。"""
        return self.root / "tmp"

    def has_blob(self, digest: str) -> bool:
        return self.blob_path(digest).is_file()

    def read_blob(self, digest: str, *, verify: bool = True) -> bytes:
        """整读并核验;仅限小对象(记录/日志级),大媒体用 copy_blob_to 或 open_blob_stream。"""
        path = self.blob_path(digest)
        if not path.is_file():
            raise RepositoryError(f"blob {digest} not found")
        data = path.read_bytes()
        if verify and "sha256:" + hashlib.sha256(data).hexdigest() != digest:
            raise RepositoryCorruptionError(f"blob {digest}: content hash mismatch")
        return data

    def open_blob_stream(self, digest: str) -> BinaryIO:
        """打开只读字节流;不做完整性校验(边读边验用 copy_blob_to,事后验用 verify_blob)。"""
        path = self.blob_path(digest)
        if not path.is_file():
            raise RepositoryError(f"blob {digest} not found")
        return open(path, "rb")

    def copy_blob_to(self, digest: str, dest: Path) -> int:
        """流式复制到 dest 并边流边验 SHA;失配即删除半成品目标并报损坏。

        内存占用有界(分块),适合大视频恢复路径;dest 不做原子发布,
        staging->promote 语义由 import 编排层负责。
        """
        source = self.blob_path(digest)
        if not source.is_file():
            raise RepositoryError(f"blob {digest} not found")
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        hasher = hashlib.sha256()
        total = 0
        with open(source, "rb") as reader, open(dest, "wb") as writer:
            while True:
                chunk = reader.read(_CHUNK_SIZE)
                if not chunk:
                    break
                hasher.update(chunk)
                total += len(chunk)
                writer.write(chunk)
            writer.flush()
            os.fsync(writer.fileno())
        if "sha256:" + hasher.hexdigest() != digest:
            os.unlink(dest)
            raise RepositoryCorruptionError(f"blob {digest}: content hash mismatch")
        return total

    def verify_blob(self, digest: str) -> int:
        """流式重算并核对;返回字节数,失配抛损坏错误。"""
        path = self.blob_path(digest)
        if not path.is_file():
            raise RepositoryError(f"blob {digest} not found")
        actual, size = _hash_file(path)
        if actual != digest:
            raise RepositoryCorruptionError(f"blob {digest}: content hash mismatch")
        return size

    def iter_blobs(self) -> Iterator[str]:
        base = self._blob_dir()
        if not base.is_dir():
            return
        for prefix in sorted(entry.name for entry in os.scandir(base) if entry.is_dir(follow_symlinks=False)):
            if not _HEX2_RE.fullmatch(prefix):
                continue
            for entry in sorted(os.scandir(base / prefix), key=lambda item: item.name):
                if entry.is_file(follow_symlinks=False) and _HEX64_RE.fullmatch(entry.name):
                    yield f"sha256:{entry.name}"


    def put_record(self, kind: str, body: object) -> RecordPut:
        """策略校验(allowlist + secret scan)通过后按 canonical 字节发布 record。"""
        encoded = validate_record(kind, body)
        digest = "sha256:" + hashlib.sha256(encoded).hexdigest()
        final = self._record_path(kind, digest)
        tmp = self._write_tmp_bytes(encoded, f"record-{kind}")
        created = self._publish(tmp, final, digest, f"record {kind}")
        return RecordPut(kind=kind, digest=digest, created=created)

    def has_record(self, kind: str, digest: str) -> bool:
        return self._record_path(kind, digest).is_file()

    def get_record(self, kind: str, digest: str) -> dict:
        """读回并全量复验:canonical 字节、digest、策略;任何失配即仓库损坏。"""
        path = self._record_path(kind, digest)
        if not path.is_file():
            raise RepositoryError(f"record {kind}/{digest} not found")
        raw = path.read_bytes()
        try:
            body = load_bounded_json(raw, f"record {kind}", max_bytes=MAX_RECORD_CANONICAL_BYTES)
            encoded = validate_record(kind, body)
        except PolicyError as exc:
            raise RepositoryCorruptionError(f"record {kind}/{digest}: {exc}") from exc
        if encoded != raw:
            raise RepositoryCorruptionError(
                f"record {kind}/{digest}: stored bytes are not canonical"
            )
        if "sha256:" + hashlib.sha256(raw).hexdigest() != digest:
            raise RepositoryCorruptionError(f"record {kind}/{digest}: digest mismatch")
        assert type(body) is dict
        return body

    def iter_records(self, kind: str | None = None) -> Iterator[tuple[str, str]]:
        kinds = [kind] if kind is not None else sorted(RECORD_KINDS)
        for current in kinds:
            if current not in RECORD_KINDS:
                raise RepositoryError(f"record kind {current!r} is not defined")
            base = self.root / "records" / current
            if not base.is_dir():
                continue
            for entry in sorted(os.scandir(base), key=lambda item: item.name):
                name = entry.name
                if entry.is_file(follow_symlinks=False) and name.endswith(".json") \
                        and _HEX64_RE.fullmatch(name[:-5]):
                    yield current, f"sha256:{name[:-5]}"

    def _find_record_kind(self, digest: str, kinds: frozenset[str]) -> str | None:
        for kind in sorted(kinds):
            if self.has_record(kind, digest):
                return kind
        return None

    def _get_record_cached(
        self, kind: str, digest: str, cache: dict[tuple[str, str], dict] | None,
    ) -> dict:
        """单次操作(gc_mark/scrub/put_snapshot)内的已验证 record 缓存。

        对象不可变,操作内重复重读只是浪费;缓存不跨调用持有,
        不承担跨进程一致性。
        """
        key = (kind, digest)
        if cache is not None and key in cache:
            return cache[key]
        body = self.get_record(kind, digest)
        if cache is not None:
            cache[key] = body
        return body


    def _validate_snapshot_body(self, body: object) -> bytes:
        """无 FS 的 snapshot 契约校验:键集合、格式、排序、policy 硬门、尺寸。

        精确键集合就是确定性门:observed_at/host 之类字段一旦出现即拒,
        保证同一逻辑状态永远得到同一 digest(§2.2 规则 3)。
        """
        if type(body) is not dict:
            raise RepositoryError("snapshot: must be an object")
        snapshot_format = body.get("format")
        if snapshot_format not in {LEGACY_SNAPSHOT_FORMAT, SNAPSHOT_FORMAT}:
            raise RepositoryError(
                "snapshot.format: must be one of "
                f"{[LEGACY_SNAPSHOT_FORMAT, SNAPSHOT_FORMAT]!r}"
            )
        expected_top_keys = (
            _LEGACY_SNAPSHOT_TOP_KEYS
            if snapshot_format == LEGACY_SNAPSHOT_FORMAT else _SNAPSHOT_TOP_KEYS
        )
        if set(body) != expected_top_keys:
            raise RepositoryError(
                f"snapshot: keys must be exactly {sorted(expected_top_keys)}"
            )
        if body["repository_format"] != REPOSITORY_FORMAT:
            raise RepositoryError(
                f"snapshot.repository_format: must be {REPOSITORY_FORMAT!r}"
            )
        source = body["source"]
        if type(source) is not dict or set(source) != _SNAPSHOT_SOURCE_KEYS:
            raise RepositoryError(
                f"snapshot.source: keys must be exactly {sorted(_SNAPSHOT_SOURCE_KEYS)}"
            )
        if type(source["app_version"]) is not str or not source["app_version"]:
            raise RepositoryError("snapshot.source.app_version: must be a non-empty str")
        if type(source["db_user_version"]) is not int or source["db_user_version"] < 0:
            raise RepositoryError("snapshot.source.db_user_version: must be int >= 0")
        if source["manifest_format"] != SOURCE_MANIFEST_FORMAT:
            raise RepositoryError(
                f"snapshot.source.manifest_format: must be {SOURCE_MANIFEST_FORMAT!r}"
            )
        selector = body["selector"]
        if type(selector) is not dict or set(selector) != _SNAPSHOT_SELECTOR_KEYS:
            raise RepositoryError(
                f"snapshot.selector: keys must be exactly {sorted(_SNAPSHOT_SELECTOR_KEYS)}"
            )
        if type(selector["partial"]) is not bool:
            raise RepositoryError("snapshot.selector.partial: must be bool")
        job_ids = selector["job_ids"]
        if type(job_ids) is not list:
            raise RepositoryError("snapshot.selector.job_ids: must be an array")
        for index, job_id in enumerate(job_ids):
            try:
                validate_job_id(job_id)
            except ManifestError as exc:
                raise RepositoryError(f"snapshot.selector.job_ids[{index}]: {exc}") from exc
        if job_ids != sorted(set(job_ids)):
            raise RepositoryError(
                "snapshot.selector.job_ids: must be sorted and unique"
            )
        # 全量快照不得携带选择集;局部快照必须列出它代表的 Job。
        if selector["partial"] != bool(job_ids):
            raise RepositoryError(
                "snapshot.selector: partial must be true exactly when job_ids is non-empty"
            )
        records = body["records"]
        if type(records) is not dict or set(records) != set(SNAPSHOT_RECORD_GROUPS):
            raise RepositoryError(
                f"snapshot.records: groups must be exactly {sorted(SNAPSHOT_RECORD_GROUPS)}"
            )
        for group, refs in records.items():
            self._validate_sorted_digests(refs, f"snapshot.records.{group}")
        self._validate_sorted_digests(body["blob_refs"], "snapshot.blob_refs")
        try:
            validate_digest(body["relations_digest"], "snapshot.relations_digest")
        except ManifestError as exc:
            raise RepositoryError(str(exc)) from exc
        policy_block = body["policy"]
        if type(policy_block) is not dict or set(policy_block) != _SNAPSHOT_POLICY_KEYS:
            raise RepositoryError(
                f"snapshot.policy: keys must be exactly {sorted(_SNAPSHOT_POLICY_KEYS)}"
            )
        # 逐键验 bool 类型:dict 相等会把 1 == True 放行,canonical 字节却不同。
        for key, expected in _SNAPSHOT_POLICY_FIXED.items():
            if type(policy_block[key]) is not bool or policy_block[key] is not expected:
                raise RepositoryError(f"snapshot.policy: {key} must be {expected}")
        if type(policy_block["secrets_included"]) is not bool:
            raise RepositoryError("snapshot.policy: secrets_included must be a bool")
        exceptions = policy_block["secret_scan_exceptions"]
        self._validate_sorted_strings(exceptions, "snapshot.policy.secret_scan_exceptions")
        # 双向一致:放行了却仍断言 false,正是这条门要挡的那种快照。
        if policy_block["secrets_included"] is not bool(exceptions):
            raise RepositoryError(
                "snapshot.policy: secrets_included must be true exactly when "
                "secret_scan_exceptions is non-empty"
            )
        if snapshot_format == SNAPSHOT_FORMAT:
            completeness = body["completeness"]
            if type(completeness) is not dict \
                    or set(completeness) != _SNAPSHOT_COMPLETENESS_KEYS:
                raise RepositoryError(
                    "snapshot.completeness: keys must be exactly "
                    f"{sorted(_SNAPSHOT_COMPLETENESS_KEYS)}"
                )
            for key in (
                "terminal_steps", "manifests_seen", "manifests_missing",
                "manifests_excluded",
            ):
                if type(completeness[key]) is not int or completeness[key] < 0:
                    raise RepositoryError(
                        f"snapshot.completeness.{key}: must be int >= 0"
                    )
            for key in (
                "ai_config_complete", "user_config_complete", "secret_scan_complete",
                "media_self_contained", "portable_ready",
            ):
                if type(completeness[key]) is not bool:
                    raise RepositoryError(f"snapshot.completeness.{key}: must be bool")
            self._validate_sorted_strings(
                completeness["external_media_roots"],
                "snapshot.completeness.external_media_roots",
            )
            self._validate_sorted_strings(
                completeness["readiness_reasons"],
                "snapshot.completeness.readiness_reasons",
            )
            if completeness["media_self_contained"] is bool(
                completeness["external_media_roots"]
            ):
                raise RepositoryError(
                    "snapshot.completeness: external_media_roots must be empty exactly "
                    "when media_self_contained is true"
                )
            expected_reasons: set[str] = set()
            if completeness["manifests_missing"]:
                expected_reasons.add("missing_step_manifests")
            if completeness["manifests_excluded"]:
                expected_reasons.add("excluded_step_manifests")
            if not completeness["ai_config_complete"]:
                expected_reasons.add("ai_config_incomplete")
            if not completeness["user_config_complete"]:
                expected_reasons.add("user_config_incomplete")
            if not completeness["secret_scan_complete"]:
                expected_reasons.add("secret_scan_incomplete")
            if policy_block["secret_scan_exceptions"]:
                expected_reasons.add("secret_scan_exceptions")
            if not completeness["media_self_contained"]:
                expected_reasons.add("external_media_dependencies")
            # unknown omission 没有计数键,因为 v2 completeness 键集已经固定。
            # 声明该诊断原因只能把 ready 收紧为 false,不能伪造可恢复性。
            if "unknown_artifacts_omitted" in completeness["readiness_reasons"]:
                expected_reasons.add("unknown_artifacts_omitted")
            if completeness["readiness_reasons"] != sorted(expected_reasons):
                raise RepositoryError(
                    "snapshot.completeness.readiness_reasons do not match flags/counts"
                )
            if completeness["portable_ready"] is not (not expected_reasons):
                raise RepositoryError(
                    "snapshot.completeness.portable_ready must be true exactly when "
                    "readiness_reasons is empty"
                )
        try:
            # policy 的契约键名含 "secrets_included",会撞 secret-name 扫描;键集已被
            # 上面钉死,藏不进 payload,故排除之。但放行清单是操作者自由输入,
            # 单独送扫,不能跟着键名一起豁免。
            scan_json_for_secrets(
                {
                    key: value for key, value in body.items()
                    if key not in {"policy", "completeness"}
                },
                "snapshot",
            )
            scan_json_for_secrets(exceptions, "snapshot.policy.secret_scan_exceptions")
            if snapshot_format == SNAPSHOT_FORMAT:
                # completeness 键集固定且含 secret_scan_complete；只扫其中自由字符串值。
                scan_json_for_secrets(
                    completeness["external_media_roots"],
                    "snapshot.completeness.external_media_roots",
                )
                scan_json_for_secrets(
                    completeness["readiness_reasons"],
                    "snapshot.completeness.readiness_reasons",
                )
            encoded = canonical_json_bytes(body)
        except (PolicyError, ManifestError) as exc:
            raise RepositoryError(f"snapshot: {exc}") from exc
        if len(encoded) > MAX_SNAPSHOT_CANONICAL_BYTES:
            raise RepositoryError(
                f"snapshot: canonical size {len(encoded)} exceeds {MAX_SNAPSHOT_CANONICAL_BYTES}"
            )
        return encoded

    @staticmethod
    def _validate_sorted_digests(refs: object, field: str) -> None:
        if type(refs) is not list:
            raise RepositoryError(f"{field}: must be an array")
        previous = None
        for index, ref in enumerate(refs):
            try:
                validate_digest(ref, f"{field}[{index}]")
            except ManifestError as exc:
                raise RepositoryError(str(exc)) from exc
            if previous is not None and ref <= previous:
                raise RepositoryError(f"{field}: digests must be strictly ascending")
            previous = ref

    @staticmethod
    def _validate_sorted_strings(values: object, field: str) -> None:
        """严格升序去重的字符串数组;顺序进 digest,同一集合必须只有一种字节形态。"""
        if type(values) is not list:
            raise RepositoryError(f"{field}: must be an array")
        previous = None
        for index, item in enumerate(values):
            if type(item) is not str or not item:
                raise RepositoryError(f"{field}[{index}]: must be a non-empty string")
            if previous is not None and item <= previous:
                raise RepositoryError(f"{field}: entries must be strictly ascending")
            previous = item

    def _check_snapshot_closure(
        self, body: Mapping, cache: dict[tuple[str, str], dict] | None = None,
    ) -> None:
        """引用可达性闭包(§2.7.8):record 必须存在于其分组允许的 kind 中,
        blob_refs 必须与全部 record 声明的 blob 并集严格相等,failure_event 的
        审计引用必须落在 business_ledgers 组内。

        相等而非包含:blob_refs 多列会让 GC 永久保活未被任何 record 佐证的
        字节,少列则损坏引用;两个方向都是契约违规。record->record 引用同理:
        悬空的 ai_usage/ai_task_log ref 会让审计链断裂或被 GC 清扫。
        """
        referenced_blobs: set[str] = set()
        failure_records: list[tuple[str, dict]] = []
        relation_records: list[tuple[str, dict]] = []
        present: dict[str, set[str]] = {group: set() for group in SNAPSHOT_RECORD_GROUPS}
        for group, refs in body["records"].items():
            kinds = SNAPSHOT_RECORD_GROUPS[group]
            for ref in refs:
                kind = self._find_record_kind(ref, kinds)
                if kind is None:
                    raise RepositoryError(
                        f"snapshot.records.{group}: record {ref} not found in repository"
                    )
                record = self._get_record_cached(kind, ref, cache)
                referenced_blobs.update(record_blob_refs(kind, record))
                present[group].add(ref)
                if kind == "failure_event":
                    failure_records.append((ref, record))
                elif kind == "job_relation":
                    relation_records.append((ref, record))
        ledger_refs = present["business_ledgers"]
        for digest, record in failure_records:
            for field, ref_kind in (
                ("ai_usage_refs", "ai_usage"), ("ai_task_log_refs", "ai_task_log"),
            ):
                for ref in record.get(field) or ():
                    if ref not in ledger_refs or not self.has_record(ref_kind, ref):
                        raise RepositoryError(
                            f"snapshot.failures: failure_event {digest} {field} -> {ref} "
                            f"must be a {ref_kind} record listed in business_ledgers"
                        )
        # job_relation 是 Job 维度的引用索引:每条边都必须落在对应分组内,
        # 否则 P3 按 Job diff 时会解出悬空引用(同 failure_event 审计边的理由)。
        for digest, record in relation_records:
            edges: list[tuple[str, str, str]] = [
                ("core", record["core"], "jobs"),
                *(("parts", ref, "parts") for ref in record["parts"]),
                *(("step_results", ref, "step_results")
                  for ref in record["step_results"].values()),
                *(("failures", ref, "failures") for ref in record["failures"]),
            ]
            if "user_state" in record:
                edges.append(("user_state", record["user_state"], "jobs"))
            for field, ref, group in edges:
                if ref not in present[group]:
                    raise RepositoryError(
                        f"snapshot.jobs: job_relation {digest} {field} -> {ref} "
                        f"is not listed in records.{group}"
                    )
        declared = set(body["blob_refs"])
        if declared != referenced_blobs:
            extra = sorted(declared - referenced_blobs)
            missing = sorted(referenced_blobs - declared)
            raise RepositoryError(
                f"snapshot.blob_refs: must equal record-referenced blobs "
                f"(extra={extra[:3]}, missing={missing[:3]})"
            )
        for digest in sorted(declared):
            if not self.has_blob(digest):
                raise RepositoryError(f"snapshot.blob_refs: blob {digest} not found")

    def put_snapshot(
        self, body: object, *, record_cache: dict[tuple[str, str], dict] | None = None,
    ) -> SnapshotPut:
        """闭包验证通过后发布确定性 snapshot;引用缺失即整体拒绝,不落半成品。

        record_cache 供刚写完这批 record 的调用方(备份编排)复用已验证 body,
        免整份快照重读;缓存内容必须来自本进程刚校验过的写入。
        """
        if type(body) is not dict or body.get("format") != SNAPSHOT_FORMAT:
            raise RepositoryError(
                f"new snapshots must use format {SNAPSHOT_FORMAT!r}"
            )
        encoded = self._validate_snapshot_body(body)
        assert type(body) is dict
        self._check_snapshot_closure(body, cache=record_cache if record_cache is not None else {})
        digest = "sha256:" + hashlib.sha256(encoded).hexdigest()
        tmp = self._write_tmp_bytes(encoded, "snapshot")
        created = self._publish(tmp, self._snapshot_path(digest), digest, "snapshot")
        self._verified_snapshots.add(digest)
        return SnapshotPut(digest=digest, created=created)

    def has_snapshot(self, digest: str) -> bool:
        return self._snapshot_path(digest).is_file()

    def get_snapshot(
        self,
        digest: str,
        *,
        verify_closure: bool = True,
        _record_cache: dict[tuple[str, str], dict] | None = None,
    ) -> dict:
        """读回并全量复验 snapshot;_record_cache 供 gc_mark/scrub 在单次操作内共享。"""
        path = self._snapshot_path(digest)
        if not path.is_file():
            raise RepositoryError(f"snapshot {digest} not found")
        raw = path.read_bytes()
        try:
            body = load_bounded_json(raw, "snapshot", max_bytes=MAX_SNAPSHOT_CANONICAL_BYTES)
            encoded = self._validate_snapshot_body(body)
        except (PolicyError, RepositoryError) as exc:
            raise RepositoryCorruptionError(f"snapshot {digest}: {exc}") from exc
        if encoded != raw:
            raise RepositoryCorruptionError(f"snapshot {digest}: stored bytes are not canonical")
        if "sha256:" + hashlib.sha256(raw).hexdigest() != digest:
            raise RepositoryCorruptionError(f"snapshot {digest}: digest mismatch")
        if verify_closure:
            try:
                self._check_snapshot_closure(body, cache=_record_cache)
            except RepositoryError as exc:
                raise RepositoryCorruptionError(f"snapshot {digest}: {exc}") from exc
            self._verified_snapshots.add(digest)
        assert type(body) is dict
        if body["format"] == LEGACY_SNAPSHOT_FORMAT:
            body = dict(body)
            body["completeness"] = {
                "terminal_steps": 0,
                "manifests_seen": 0,
                "manifests_missing": 0,
                "manifests_excluded": 0,
                "ai_config_complete": False,
                "user_config_complete": False,
                "secret_scan_complete": False,
                "media_self_contained": False,
                "external_media_roots": ["legacy-unknown"],
                "portable_ready": False,
                "readiness_reasons": ["legacy_snapshot_without_completeness"],
            }
        return body

    def iter_snapshots(self) -> Iterator[str]:
        base = self.root / "snapshots"
        if not base.is_dir():
            return
        for entry in sorted(os.scandir(base), key=lambda item: item.name):
            name = entry.name
            if entry.is_file(follow_symlinks=False) and name.endswith(".json") \
                    and _HEX64_RE.fullmatch(name[:-5]):
                yield f"sha256:{name[:-5]}"

    def snapshot_digest(self, body: object) -> str:
        """不落盘计算 snapshot digest;供备份编排判断"是否命中既有 snapshot"。"""
        encoded = self._validate_snapshot_body(body)
        return "sha256:" + hashlib.sha256(encoded).hexdigest()


    def _ref_path(self, name: str) -> Path:
        return self.root / "refs" / validate_ref_name(name)

    def set_ref(self, name: str, snapshot_digest: str) -> None:
        """指向已验证 snapshot 的原子替换;目标不完整则 ref 保持旧值(§2.2 规则 6)。

        本实例刚 put/get 全量验证过的 snapshot 走进程内信任通道,只查存在性,
        不重复闭包重读;跨进程(新 open 的实例)仍然全量验证。
        """
        path = self._ref_path(name)
        if snapshot_digest in self._verified_snapshots:
            if not self.has_snapshot(snapshot_digest):
                raise RepositoryError(f"snapshot {snapshot_digest} not found")
        else:
            # 全量验证(含闭包)后才允许指向:ref 是保留根,指向半成品等于发布半成品。
            self.get_snapshot(snapshot_digest)
        tmp = self._write_tmp_bytes(snapshot_digest.encode("ascii") + b"\n", f"ref-{name}")
        os.replace(tmp, path)
        _fsync_dir(path.parent)

    def get_ref(self, name: str) -> str:
        path = self._ref_path(name)
        if not path.is_file():
            raise RepositoryError(f"ref {name!r} not found")
        value = path.read_bytes().decode("ascii", errors="strict").strip()
        try:
            validate_digest(value, f"ref {name}")
        except ManifestError as exc:
            raise RepositoryCorruptionError(str(exc)) from exc
        return value

    def list_refs(self) -> dict[str, str]:
        base = self.root / "refs"
        result: dict[str, str] = {}
        if not base.is_dir():
            return result
        for entry in sorted(os.scandir(base), key=lambda item: item.name):
            if entry.is_file(follow_symlinks=False) and _REF_NAME_RE.fullmatch(entry.name):
                result[entry.name] = self.get_ref(entry.name)
        return result

    def delete_ref(self, name: str) -> None:
        path = self._ref_path(name)
        if not path.is_file():
            raise RepositoryError(f"ref {name!r} not found")
        os.unlink(path)
        _fsync_dir(path.parent)


    def _validate_receipt(self, body: object, *, check_snapshot: bool) -> bytes:
        """receipt 契约校验;snapshot 存在性只在写入时强制,GC 后旧 receipt 仍可读。"""
        if type(body) is not dict:
            raise RepositoryError("receipt: must be an object")
        missing = sorted(_RECEIPT_REQUIRED_KEYS - set(body))
        unknown = sorted(set(body) - _RECEIPT_REQUIRED_KEYS - _RECEIPT_OPTIONAL_KEYS)
        if missing or unknown:
            raise RepositoryError(f"receipt: missing={missing} unknown={unknown}")
        if type(body["run_id"]) is not str or not _SEGMENT_RE.fullmatch(body["run_id"]):
            raise RepositoryError("receipt.run_id: invalid identifier")
        try:
            _validate_utc_timestamp(body["observed_at"], "receipt.observed_at")
        except ManifestError as exc:
            raise RepositoryError(str(exc)) from exc
        outcome = body["outcome"]
        if outcome not in _RECEIPT_OUTCOMES:
            raise RepositoryError(f"receipt.outcome: must be one of {sorted(_RECEIPT_OUTCOMES)}")
        snapshot_digest = body.get("snapshot_digest")
        if snapshot_digest is not None:
            try:
                validate_digest(snapshot_digest, "receipt.snapshot_digest")
            except ManifestError as exc:
                raise RepositoryError(str(exc)) from exc
        if outcome == "success":
            if snapshot_digest is None:
                raise RepositoryError("receipt: success requires snapshot_digest")
            if check_snapshot and not self.has_snapshot(snapshot_digest):
                raise RepositoryError(
                    f"receipt.snapshot_digest: snapshot {snapshot_digest} not found"
                )
        request_digest = body.get("request_digest")
        if request_digest is not None:
            try:
                validate_digest(request_digest, "receipt.request_digest")
            except ManifestError as exc:
                raise RepositoryError(str(exc)) from exc
        if "hit_existing_snapshot" in body and type(body["hit_existing_snapshot"]) is not bool:
            raise RepositoryError("receipt.hit_existing_snapshot: must be bool")
        if "stats" in body and type(body["stats"]) is not dict:
            raise RepositoryError("receipt.stats: must be an object")
        if "source_instance" in body and type(body["source_instance"]) is not str:
            raise RepositoryError("receipt.source_instance: must be str")
        if "error" in body and body["error"] is not None:
            try:
                validate_audit_text(body["error"], "receipt.error")
            except PolicyError as exc:
                raise RepositoryError(str(exc)) from exc
        try:
            scan_json_for_secrets(body, "receipt")
            encoded = canonical_json_bytes(body)
        except (PolicyError, ManifestError) as exc:
            raise RepositoryError(f"receipt: {exc}") from exc
        if len(encoded) > MAX_RECEIPT_CANONICAL_BYTES:
            raise RepositoryError("receipt: canonical size exceeds limit")
        return encoded

    def write_receipt(self, body: object) -> str:
        """追加一条执行 receipt;时间/主机/统计只进这里,不进 snapshot(§2.2 规则 4)。"""
        encoded = self._validate_receipt(body, check_snapshot=True)
        assert type(body) is dict
        receipt_id = f"{_receipt_time_prefix(body['observed_at'])}-{secrets.token_hex(4)}"
        final = self.root / "receipts" / f"{receipt_id}.json"
        tmp = self._write_tmp_bytes(encoded, "receipt")
        if not self._publish(tmp, final, "sha256:" + hashlib.sha256(encoded).hexdigest(), "receipt"):
            raise RepositoryError(f"receipt {receipt_id} already exists")
        return receipt_id

    def read_receipt(self, receipt_id: str) -> dict:
        if type(receipt_id) is not str or not _RECEIPT_ID_RE.fullmatch(receipt_id):
            raise RepositoryError(f"receipt id {receipt_id!r} is invalid")
        path = self.root / "receipts" / f"{receipt_id}.json"
        if not path.is_file():
            raise RepositoryError(f"receipt {receipt_id} not found")
        raw = path.read_bytes()
        try:
            body = load_bounded_json(raw, "receipt", max_bytes=MAX_RECEIPT_CANONICAL_BYTES)
            encoded = self._validate_receipt(body, check_snapshot=False)
        except (PolicyError, RepositoryError) as exc:
            raise RepositoryCorruptionError(f"receipt {receipt_id}: {exc}") from exc
        if encoded != raw:
            raise RepositoryCorruptionError(f"receipt {receipt_id}: stored bytes are not canonical")
        assert type(body) is dict
        return body

    def list_receipts(self) -> tuple[str, ...]:
        base = self.root / "receipts"
        if not base.is_dir():
            return ()
        return tuple(sorted(
            entry.name[:-5]
            for entry in os.scandir(base)
            if entry.is_file(follow_symlinks=False) and entry.name.endswith(".json")
            and _RECEIPT_ID_RE.fullmatch(entry.name[:-5])
        ))

    def find_receipts(self, run_id: str | None = None) -> list[tuple[str, dict]]:
        """按 receipt_id 升序返回 (id, body);run_id 过滤用于 BACKUP_RUN_ID 幂等判定。"""
        result = []
        for receipt_id in self.list_receipts():
            body = self.read_receipt(receipt_id)
            if run_id is None or body["run_id"] == run_id:
                result.append((receipt_id, body))
        return result


    def _lock_path(self) -> Path:
        return self.root / "locks" / _WRITE_LOCK_NAME

    def _release_write_lock(self, path: Path, payload: bytes) -> None:
        try:
            current = path.read_bytes()
        except FileNotFoundError:
            raise RepositoryLockError("write lock disappeared while held") from None
        if current != payload:
            raise RepositoryLockError("write lock was taken over by another holder")
        os.unlink(path)

    @contextmanager
    def write_lock(self, owner: str):
        """backup/GC 互斥写锁:O_EXCL 抢占,释放前核对自己的 token。

        无 TTL/心跳(那是编排层的事);acquired_at/host 只供人工与编排判断
        持有者死活,本层绝不自动抢锁,进程崩溃遗留的锁由运维确认后
        break_write_lock 显式清除。with 体内的原始异常优先于释放失败:
        释放异常降级为告警日志,不得吞掉业务错误。
        """
        if type(owner) is not str or not owner or len(owner) > 200:
            raise RepositoryLockError("lock owner must be a non-empty str")
        token = secrets.token_hex(16)
        payload = canonical_json_bytes({
            "owner": owner,
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "acquired_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "token": token,
        })
        path = self._lock_path()
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            holder = self.write_lock_holder()
            raise RepositoryLockError(
                f"write lock is held by {holder.get('owner') if holder else 'unknown'!r}"
            ) from None
        try:
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            yield token
        except BaseException:
            try:
                self._release_write_lock(path, payload)
            except RepositoryLockError as release_exc:
                _log.warning(
                    "write_lock_release_failed_during_unwind",
                    repository=str(self.root), owner=owner, error=str(release_exc),
                )
            raise
        self._release_write_lock(path, payload)

    def write_lock_holder(self) -> dict | None:
        path = self._lock_path()
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return None
        try:
            body = load_bounded_json(raw, "write lock", max_bytes=4096)
        except PolicyError:
            return {"owner": None}
        return body if type(body) is dict else {"owner": None}

    def break_write_lock(self) -> None:
        """显式破锁:仅供确认持有者已死后的人工恢复路径。"""
        try:
            os.unlink(self._lock_path())
        except FileNotFoundError:
            raise RepositoryLockError("write lock is not held") from None

    def clean_tmp(self) -> int:
        """回收 tmp/ 残留(§2.8.6)。调用方必须持写锁,否则会删掉并发写入的中间文件。"""
        base = self.root / "tmp"
        removed = 0
        if base.is_symlink() or not base.is_dir():
            raise RepositoryCorruptionError("repository tmp directory is missing or unsafe")
        for entry in os.scandir(base):
            if entry.is_dir(follow_symlinks=False):
                raise RepositoryCorruptionError(
                    f"repository tmp contains unexpected directory {entry.name!r}"
                )
            os.unlink(entry.path)
            removed += 1
        if removed:
            _fsync_dir(base)
        return removed


    def gc_mark(self, *, receipt_root_limit: int | None = None) -> GCPlan:
        """mark 阶段(§2.14.3):refs + 保留 receipts 可达 snapshot -> records -> blobs。

        refs 指向缺失 snapshot 是致命损坏;receipt 指向缺失 snapshot 记入
        warnings(旧 snapshot 可能已被此前 GC 合法清除)。本方法只读,不删除;
        sweep 与 grace period 由后续 GC 执行单元实现。
        """
        roots: set[str] = set()
        warnings: list[str] = []
        for name, digest in self.list_refs().items():
            if not self.has_snapshot(digest):
                raise RepositoryCorruptionError(f"ref {name!r} points to missing snapshot {digest}")
            roots.add(digest)
        # 一次备份会写 in_progress + 终态两条 receipt,直接按条数截窗口等于
        # 把 --keep-receipts N 砍成大约 N/2 次备份。先滤掉不带 snapshot 的,
        # 再取最近 N 条,窗口语义才是"最近 N 次有结果的备份"。
        anchored = [
            (receipt_id, body)
            for receipt_id, body in (
                (item, self.read_receipt(item)) for item in self.list_receipts()
            )
            if body.get("snapshot_digest") is not None
        ]
        if receipt_root_limit is not None:
            if receipt_root_limit < 0:
                raise RepositoryError("receipt_root_limit must be >= 0")
            anchored = anchored[len(anchored) - receipt_root_limit:] \
                if receipt_root_limit else []
        for receipt_id, body in anchored:
            digest = body["snapshot_digest"]
            if not self.has_snapshot(digest):
                warnings.append(f"receipt {receipt_id} references missing snapshot {digest}")
                continue
            roots.add(digest)

        reachable_records: set[tuple[str, str]] = set()
        reachable_blobs: set[str] = set()
        record_cache: dict[tuple[str, str], dict] = {}
        for digest in sorted(roots):
            body = self.get_snapshot(digest, _record_cache=record_cache)
            for group, refs in body["records"].items():
                kinds = SNAPSHOT_RECORD_GROUPS[group]
                for ref in refs:
                    kind = self._find_record_kind(ref, kinds)
                    if kind is None:  # get_snapshot 闭包已验证,走到这说明并发删除
                        raise RepositoryCorruptionError(
                            f"snapshot {digest}: record {ref} vanished during mark"
                        )
                    reachable_records.add((kind, ref))
            reachable_blobs.update(body["blob_refs"])

        all_snapshots = set(self.iter_snapshots())
        all_records = set(self.iter_records())
        all_blobs = set(self.iter_blobs())
        return GCPlan(
            reachable_snapshots=tuple(sorted(roots)),
            reachable_records=tuple(sorted(reachable_records)),
            reachable_blobs=tuple(sorted(reachable_blobs)),
            unreachable_snapshots=tuple(sorted(all_snapshots - roots)),
            unreachable_records=tuple(sorted(all_records - reachable_records)),
            unreachable_blobs=tuple(sorted(all_blobs - reachable_blobs)),
            warnings=tuple(warnings),
        )

    def gc_sweep(
        self,
        plan: GCPlan,
        *,
        grace_seconds: int = 604_800,
        dry_run: bool = True,
        now: float | None = None,
    ) -> dict:
        """sweep 阶段(§2.14.3):删除 mark 判定不可达且已过 grace period 的对象。

        与 mark 分离并强制 grace period:刚被写入、尚未被任何 snapshot 引用的
        对象可能正属于一次进行中的备份,立刻清扫会删掉活数据。dry_run 与实删
        走同一段判定与同一份清单,保证"预演结果 = 实际结果"。
        调用方必须持 write_lock(与 backup 互斥);import 只读仓库不受影响。
        """
        if grace_seconds < 0:
            raise RepositoryError("grace_seconds must be >= 0")
        moment = time.time() if now is None else now
        deleted: dict[str, list[str]] = {"blobs": [], "records": [], "snapshots": []}
        retained_young: list[str] = []

        def _old_enough(path: Path, label: str) -> bool:
            try:
                mtime = path.stat().st_mtime
            except FileNotFoundError:
                return False
            if moment - mtime < grace_seconds:
                retained_young.append(label)
                return False
            return True

        # grace 必须按引用组判定,不能逐对象判。一次备份先写 record/blob、
        # 后写 snapshot,若该备份整体变成不可达而清扫又落在 grace 边界上,
        # 逐对象判会留下 snapshot 却删掉它引用的 record/blob —— GC 自己造出损坏。
        # 因此先找出"因 grace 被留下的不可达 snapshot",把它们引用的东西
        # 全部当作本次的额外保活根扣除。
        pinned_records: set[tuple[str, str]] = set()
        pinned_blobs: set[str] = set()
        for digest in plan.unreachable_snapshots:
            if _old_enough(self._snapshot_path(digest), f"snapshot:{digest}"):
                deleted["snapshots"].append(digest)
                continue
            try:
                body = self.get_snapshot(digest, verify_closure=False)
            except RepositoryError:
                continue
            pinned_blobs.update(body["blob_refs"])
            for group, refs in body["records"].items():
                kinds = SNAPSHOT_RECORD_GROUPS[group]
                for ref in refs:
                    kind = self._find_record_kind(ref, kinds)
                    if kind is not None:
                        pinned_records.add((kind, ref))

        for digest in plan.unreachable_blobs:
            if digest in pinned_blobs:
                retained_young.append(f"blob:{digest}(pinned by young snapshot)")
                continue
            if _old_enough(self.blob_path(digest), f"blob:{digest}"):
                deleted["blobs"].append(digest)
        for kind, digest in plan.unreachable_records:
            if (kind, digest) in pinned_records:
                retained_young.append(f"record:{kind}/{digest}(pinned by young snapshot)")
                continue
            if _old_enough(self._record_path(kind, digest), f"record:{kind}/{digest}"):
                deleted["records"].append(f"{kind}/{digest}")

        if not dry_run:
            # 删除顺序 snapshot -> record -> blob:任一步中断都不会留下
            # "snapshot 还在但它引用的 record 没了"的悬空引用。
            for digest in deleted["snapshots"]:
                self._snapshot_path(digest).unlink(missing_ok=True)
            for item in deleted["records"]:
                kind, _, digest_hex = item.partition("/")
                self._record_path(kind, digest_hex).unlink(missing_ok=True)
            for digest in deleted["blobs"]:
                self.blob_path(digest).unlink(missing_ok=True)
            # 实删分支必然持写锁,顺手回收上次中断留下的 tmp 残留(§2.8-6)。
            tmp_removed = self.clean_tmp()
        else:
            tmp_removed = 0
        return {
            "dry_run": dry_run,
            "deleted": deleted,
            "tmp_removed": tmp_removed,
            "counts": {key: len(value) for key, value in deleted.items()},
            "retained_within_grace": sorted(retained_young),
            "grace_seconds": grace_seconds,
        }


    def scrub(self) -> ScrubReport:
        """全量完整性校验:重算全部 blob/record/snapshot 摘要,核对 refs/receipts,
        揪出杂散文件与 symlink。只读,发现损坏也不修复(§2.14.5/6)。"""
        issues: list[ScrubIssue] = []
        counts = {"blobs": 0, "records": 0, "snapshots": 0, "refs": 0, "receipts": 0}
        # records 段验证过的 body 缓存给 snapshots 段的闭包检查复用(单次操作内)。
        record_cache: dict[tuple[str, str], dict] = {}

        def issue(kind: str, path: Path, detail: str) -> None:
            issues.append(ScrubIssue(kind, str(path.relative_to(self.root)), detail))

        for entry in os.scandir(self.root):
            if entry.name not in _TOP_ENTRIES:
                issue("stray_file", Path(entry.path), "unexpected top-level entry")
            elif entry.is_symlink():
                issue("symlink", Path(entry.path), "symlink in repository tree")

        def dir_ok(relative: str) -> bool:
            path = self.root / relative
            if path.is_dir() and not path.is_symlink():
                return True
            issue("missing_dir", path, "required directory is missing or not a directory")
            return False

        if dir_ok("blobs/sha256"):
            self._scrub_blobs(issue, counts)
        if dir_ok("records"):
            self._scrub_records(issue, counts, record_cache)
        if dir_ok("snapshots"):
            self._scrub_snapshots(issue, counts, record_cache)
        if dir_ok("refs"):
            self._scrub_refs(issue, counts)
        if dir_ok("receipts"):
            self._scrub_receipts(issue, counts)
        if dir_ok("tmp"):
            for entry in os.scandir(self.root / "tmp"):
                issue("tmp_leftover", Path(entry.path), "leftover temporary file")
        if dir_ok("locks"):
            for entry in os.scandir(self.root / "locks"):
                if entry.name != _WRITE_LOCK_NAME:
                    issue("stray_file", Path(entry.path), "unexpected lock entry")
        return ScrubReport(
            checked_blobs=counts["blobs"],
            checked_records=counts["records"],
            checked_snapshots=counts["snapshots"],
            checked_refs=counts["refs"],
            checked_receipts=counts["receipts"],
            issues=tuple(issues),
        )

    def _scrub_entry_is_clean_file(self, entry, issue) -> bool:
        path = Path(entry.path)
        if entry.is_symlink():
            issue("symlink", path, "symlink in repository tree")
            return False
        mode = entry.stat(follow_symlinks=False).st_mode
        if not stat_module.S_ISREG(mode):
            issue("irregular_file", path, "not a regular file")
            return False
        return True

    def _scrub_blobs(self, issue, counts) -> None:
        base = self._blob_dir()
        for prefix_entry in os.scandir(base):
            prefix_path = Path(prefix_entry.path)
            if prefix_entry.is_symlink() or not prefix_entry.is_dir(follow_symlinks=False):
                issue("stray_file", prefix_path, "unexpected entry under blobs/sha256")
                continue
            if not _HEX2_RE.fullmatch(prefix_entry.name):
                issue("stray_file", prefix_path, "invalid blob prefix directory")
                continue
            for entry in os.scandir(prefix_path):
                path = Path(entry.path)
                if not self._scrub_entry_is_clean_file(entry, issue):
                    continue
                if not _HEX64_RE.fullmatch(entry.name) or entry.name[:2] != prefix_entry.name:
                    issue("stray_file", path, "invalid blob file name")
                    continue
                counts["blobs"] += 1
                actual, _ = _hash_file(path)
                if actual != f"sha256:{entry.name}":
                    issue("blob_corrupt", path, f"content hashes to {actual}")

    def _scrub_records(self, issue, counts, record_cache) -> None:
        base = self.root / "records"
        for kind_entry in os.scandir(base):
            kind_path = Path(kind_entry.path)
            if kind_entry.is_symlink() or not kind_entry.is_dir(follow_symlinks=False) \
                    or kind_entry.name not in RECORD_KINDS:
                issue("stray_file", kind_path, "unexpected entry under records/")
                continue
            for entry in os.scandir(kind_path):
                path = Path(entry.path)
                if not self._scrub_entry_is_clean_file(entry, issue):
                    continue
                name = entry.name
                if not name.endswith(".json") or not _HEX64_RE.fullmatch(name[:-5]):
                    issue("stray_file", path, "invalid record file name")
                    continue
                counts["records"] += 1
                try:
                    self._get_record_cached(
                        kind_entry.name, f"sha256:{name[:-5]}", record_cache,
                    )
                except RepositoryError as exc:
                    issue("record_corrupt", path, str(exc))

    def _scrub_snapshots(self, issue, counts, record_cache) -> None:
        base = self.root / "snapshots"
        for entry in os.scandir(base):
            path = Path(entry.path)
            if not self._scrub_entry_is_clean_file(entry, issue):
                continue
            name = entry.name
            if not name.endswith(".json") or not _HEX64_RE.fullmatch(name[:-5]):
                issue("stray_file", path, "invalid snapshot file name")
                continue
            counts["snapshots"] += 1
            try:
                self.get_snapshot(f"sha256:{name[:-5]}", _record_cache=record_cache)
            except RepositoryError as exc:
                issue("snapshot_corrupt", path, str(exc))

    def _scrub_refs(self, issue, counts) -> None:
        base = self.root / "refs"
        for entry in os.scandir(base):
            path = Path(entry.path)
            if not self._scrub_entry_is_clean_file(entry, issue):
                continue
            if not _REF_NAME_RE.fullmatch(entry.name):
                issue("stray_file", path, "invalid ref name")
                continue
            counts["refs"] += 1
            try:
                digest = self.get_ref(entry.name)
            except RepositoryError as exc:
                issue("broken_ref", path, str(exc))
                continue
            if not self.has_snapshot(digest):
                issue("broken_ref", path, f"target snapshot {digest} not found")

    def _scrub_receipts(self, issue, counts) -> None:
        base = self.root / "receipts"
        for entry in os.scandir(base):
            path = Path(entry.path)
            if not self._scrub_entry_is_clean_file(entry, issue):
                continue
            name = entry.name
            if not name.endswith(".json") or not _RECEIPT_ID_RE.fullmatch(name[:-5]):
                issue("stray_file", path, "invalid receipt file name")
                continue
            counts["receipts"] += 1
            try:
                self.read_receipt(name[:-5])
            except RepositoryError as exc:
                issue("receipt_corrupt", path, str(exc))
