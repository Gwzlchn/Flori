"""受控 NAS 只读源库引用、身份验证与临时物化。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import quote, unquote_to_bytes, urlsplit


_ROOT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
_SHA256_RE = re.compile(r"^(?:sha256:)?([0-9a-f]{64})$")
_VIDEO_SUFFIXES = frozenset({
    ".avi", ".m4v", ".mkv", ".mov", ".mp4", ".mpeg", ".mpg", ".ts", ".webm",
})
_READ_CHUNK_SIZE = 4 * 1024 * 1024
# 只有直接读原媒体的Part步骤绑NAS根;其他Part/Job步只消费已回传的小产物。
SOURCE_MEDIA_STEPS = frozenset({
    "01_download", "02_whisper", "03_scene", "04_frames", "08_punctuate",
})


class SourceReferenceError(ValueError):
    """源引用格式、根配置或安全解析失败。"""


class SourceIdentityMismatch(SourceReferenceError):
    """当前源字节与 Job 固定的不可变身份不同。"""


@dataclass(frozen=True)
class SourceReference:
    root_id: str
    relative_path: str


@dataclass(frozen=True)
class SourceSnapshot:
    reference: SourceReference
    digest: str
    size_bytes: int
    device: int
    inode: int
    mtime_ns: int
    ctime_ns: int

    @property
    def fingerprint(self) -> tuple[int, int, int, int, int]:
        return (
            self.device, self.inode, self.size_bytes, self.mtime_ns, self.ctime_ns,
        )


def _validate_root_id(root_id: str) -> str:
    if not isinstance(root_id, str) or not _ROOT_ID_RE.fullmatch(root_id):
        raise SourceReferenceError("invalid source root id")
    return root_id


def _canonical_relative_path(relative_path: str) -> str:
    if not isinstance(relative_path, str) or not relative_path or "\x00" in relative_path:
        raise SourceReferenceError("invalid source relative path")
    if "\\" in relative_path or relative_path.startswith("/") or "//" in relative_path:
        raise SourceReferenceError("invalid source relative path")
    path = PurePosixPath(relative_path)
    if (
        not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.as_posix() != relative_path
    ):
        raise SourceReferenceError("invalid source relative path")
    if path.suffix.lower() not in _VIDEO_SUFFIXES:
        raise SourceReferenceError("unsupported source video extension")
    return path.as_posix()


def normalize_source_digest(value: str) -> str:
    match = _SHA256_RE.fullmatch(value if isinstance(value, str) else "")
    if match is None:
        raise SourceReferenceError("invalid source sha256")
    return f"sha256:{match.group(1)}"


def build_source_ref(root_id: str, relative_path: str) -> str:
    root = _validate_root_id(root_id)
    relative = _canonical_relative_path(relative_path)
    return f"nas://{root}/{quote(relative, safe='/-._~')}"


def parse_source_ref(value: str) -> SourceReference:
    if not isinstance(value, str) or not value:
        raise SourceReferenceError("invalid source reference")
    parsed = urlsplit(value)
    try:
        parsed_port = parsed.port
        parsed_host = parsed.hostname
    except ValueError as exc:
        raise SourceReferenceError("invalid source reference") from exc
    if (
        parsed.scheme != "nas"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed_port is not None
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith("/")
    ):
        raise SourceReferenceError("invalid source reference")
    root_id = _validate_root_id(parsed_host or "")
    try:
        relative = unquote_to_bytes(parsed.path[1:]).decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise SourceReferenceError("invalid source reference encoding") from exc
    relative = _canonical_relative_path(relative)
    reference = SourceReference(root_id=root_id, relative_path=relative)
    if build_source_ref(root_id, relative) != value:
        raise SourceReferenceError("source reference is not canonical")
    return reference


def source_root_tag(root_id: str) -> str:
    return f"source-root:{_validate_root_id(root_id)}"


def source_roots_from_env(
    raw: str | None = None, *, env_name: str = "FLORI_SOURCE_ROOTS_JSON",
) -> dict[str, Path]:
    text = os.environ.get(env_name, "") if raw is None else raw
    if not text.strip():
        if os.environ.get("FLORI_SOURCE_LIBRARY_ENABLED", "0").lower() not in {
            "1", "true", "yes",
        }:
            return {}
        root_id = _validate_root_id(
            os.environ.get("FLORI_SOURCE_LIBRARY_ROOT_ID", "library"),
        )
        if env_name == "FLORI_SOURCE_ROOTS_JSON":
            value = os.environ.get(
                "FLORI_SOURCE_LIBRARY_CONTAINER_DIR", "/sources/library",
            )
        else:
            value = os.environ.get("FLORI_SOURCE_LIBRARY_HOST_DIR", "")
            if not value:
                return {}
        path = Path(value)
        if not path.is_absolute():
            raise SourceReferenceError(f"invalid {env_name}")
        return {root_id: path}
    try:
        value = json.loads(text)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise SourceReferenceError(f"invalid {env_name}") from exc
    if not isinstance(value, dict):
        raise SourceReferenceError(f"invalid {env_name}")
    roots: dict[str, Path] = {}
    for raw_id, raw_path in value.items():
        root_id = _validate_root_id(raw_id)
        if not isinstance(raw_path, str) or not raw_path or "\x00" in raw_path:
            raise SourceReferenceError(f"invalid {env_name}")
        path = Path(raw_path)
        if not path.is_absolute():
            raise SourceReferenceError(f"invalid {env_name}")
        roots[root_id] = path
    return roots


def configured_source_root_tags() -> set[str]:
    """只为当前进程可安全打开的root生成Worker能力tag。"""
    try:
        library = SourceLibrary.from_env()
    except SourceReferenceError:
        return set()
    return {
        source_root_tag(root_id)
        for root_id in library.roots
        if library.root_available(root_id)
    }


class SourceLibrary:
    """源根只读解析器;cache仅在文件系统指纹未变时复用full hash。"""

    def __init__(self, roots: dict[str, Path | str]):
        self.roots: dict[str, Path] = {}
        for raw_id, raw_path in roots.items():
            root_id = _validate_root_id(raw_id)
            path = Path(raw_path)
            if not path.is_absolute():
                raise SourceReferenceError("source root path must be absolute")
            self.roots[root_id] = path
        self._verified: dict[tuple[str, str, int], SourceSnapshot] = {}

    @classmethod
    def from_env(cls) -> SourceLibrary:
        return cls(source_roots_from_env())

    def root_available(self, root_id: str) -> bool:
        root = self.roots.get(_validate_root_id(root_id))
        if root is None:
            return False
        try:
            fd = self._open_root(root)
        except OSError:
            return False
        os.close(fd)
        return True

    def verify(
        self, source_ref: str, expected_digest: str, expected_size: int,
    ) -> SourceSnapshot:
        reference = parse_source_ref(source_ref)
        digest = normalize_source_digest(expected_digest)
        if type(expected_size) is not int or expected_size < 1:
            raise SourceReferenceError("invalid source size")
        root = self.roots.get(reference.root_id)
        if root is None:
            raise SourceReferenceError("source root is not configured")
        try:
            fd = self._open_relative(root, reference.relative_path)
        except OSError as exc:
            raise SourceReferenceError("source is unavailable") from exc
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode):
                raise SourceReferenceError("source is not a regular file")
            if before.st_nlink != 1:
                raise SourceReferenceError("source hard links are not allowed")
            if before.st_size != expected_size:
                raise SourceIdentityMismatch("source identity changed")
            fingerprint = self._fingerprint(before)
            key = (source_ref, digest, expected_size)
            cached = self._verified.get(key)
            if cached is not None and cached.fingerprint == fingerprint:
                try:
                    check_fd = self._open_relative(root, reference.relative_path)
                except OSError as exc:
                    raise SourceIdentityMismatch("source identity changed") from exc
                try:
                    current = os.fstat(check_fd)
                finally:
                    os.close(check_fd)
                if self._fingerprint(current) != fingerprint:
                    raise SourceIdentityMismatch("source identity changed")
                return cached

            actual = hashlib.sha256()
            while chunk := os.read(fd, _READ_CHUNK_SIZE):
                actual.update(chunk)
            after = os.fstat(fd)
            try:
                check_fd = self._open_relative(root, reference.relative_path)
            except OSError as exc:
                raise SourceIdentityMismatch("source identity changed") from exc
            try:
                current = os.fstat(check_fd)
            finally:
                os.close(check_fd)
            if self._fingerprint(before) != self._fingerprint(after):
                raise SourceIdentityMismatch("source identity changed")
            if self._fingerprint(after) != self._fingerprint(current):
                raise SourceIdentityMismatch("source identity changed")
            actual_digest = f"sha256:{actual.hexdigest()}"
            if actual_digest != digest:
                raise SourceIdentityMismatch("source identity changed")
            snapshot = SourceSnapshot(
                reference=reference,
                digest=actual_digest,
                size_bytes=after.st_size,
                device=after.st_dev,
                inode=after.st_ino,
                mtime_ns=after.st_mtime_ns,
                ctime_ns=after.st_ctime_ns,
            )
            self._verified[key] = snapshot
            return snapshot
        finally:
            os.close(fd)

    def status(self, source_ref: str, expected_size: int) -> str:
        """详情页只做快速可用性检查;full digest在准入与执行前验证。"""
        try:
            reference = parse_source_ref(source_ref)
        except SourceReferenceError:
            return "invalid"
        root = self.roots.get(reference.root_id)
        if root is None:
            return "unmounted"
        try:
            fd = self._open_relative(root, reference.relative_path)
        except OSError:
            return "missing"
        try:
            value = os.fstat(fd)
            if not stat.S_ISREG(value.st_mode):
                return "missing"
            if value.st_nlink != 1:
                return "invalid"
            return "available" if value.st_size == expected_size else "changed"
        finally:
            os.close(fd)

    def materialize(
        self,
        source_ref: str,
        expected_digest: str,
        expected_size: int,
        work_dir: Path,
    ) -> Path:
        snapshot = self.verify(source_ref, expected_digest, expected_size)
        root = self.roots[snapshot.reference.root_id]
        target = root / Path(*PurePosixPath(snapshot.reference.relative_path).parts)
        input_dir = work_dir / "input"
        input_dir.mkdir(parents=True, exist_ok=True)
        link = input_dir / "source.mp4"
        if link.is_symlink():
            link.unlink()
        elif link.exists():
            raise SourceReferenceError("source materialization target already exists")
        link.symlink_to(target)
        return link

    @staticmethod
    def dematerialize(link: Path | None) -> None:
        if link is None:
            return
        try:
            if link.is_symlink():
                link.unlink()
        except OSError:
            pass

    @staticmethod
    def _fingerprint(value: os.stat_result) -> tuple[int, int, int, int, int]:
        return (
            value.st_dev, value.st_ino, value.st_size,
            value.st_mtime_ns, value.st_ctime_ns,
        )

    @staticmethod
    def _open_root(root: Path) -> int:
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        return os.open(root, flags)

    @classmethod
    def _open_relative(cls, root: Path, relative_path: str) -> int:
        parts = PurePosixPath(_canonical_relative_path(relative_path)).parts
        current = cls._open_root(root)
        try:
            dir_flags = (
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            for part in parts[:-1]:
                next_fd = os.open(part, dir_flags, dir_fd=current)
                os.close(current)
                current = next_fd
            file_flags = (
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            return os.open(parts[-1], file_flags, dir_fd=current)
        finally:
            os.close(current)
