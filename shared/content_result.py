"""便携备份工具统一校验并原子发布机器可读结果。"""

from __future__ import annotations

import json
import os
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ResultFileError(ValueError):
    """结果路径会污染便携仓库或包含可变链接时拒绝。"""


RESULT_ROOT_IDENTITY_ENV = "FLORI_RESULT_ROOT_IDENTITY"


def _absolute_without_symlinks(path: str | Path, *, label: str) -> Path:
    absolute = Path(os.path.abspath(os.fspath(path)))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ResultFileError(f"cannot inspect {label} path {current}: {exc}") from exc
        if stat.S_ISLNK(info.st_mode):
            raise ResultFileError(f"{label} path contains a symbolic link: {current}")
    resolved = absolute.resolve(strict=False)
    if resolved != absolute:
        raise ResultFileError(
            f"{label} path changes after canonical resolution: {absolute} -> {resolved}"
        )
    return resolved


@dataclass(frozen=True)
class ResultDestination:
    """已绑定全部数据边界的结果位置,每次发布前仍会重新验证。"""

    path: Path
    repository_root: Path
    protected_roots: tuple[Path, ...] = ()
    protected_files: tuple[Path, ...] = ()
    protected_directory_identities: frozenset[tuple[int, int]] = frozenset()

    @property
    def protected_trees(self) -> tuple[Path, ...]:
        return (self.repository_root, *self.protected_roots)

    def validate(self) -> None:
        path = _absolute_without_symlinks(self.path, label="result-file")
        for raw_root in self.protected_trees:
            root = _absolute_without_symlinks(raw_root, label="protected root")
            if path == root or root in path.parents:
                raise ResultFileError(
                    f"result-file must be outside protected root {root}: {path}"
                )
        if path.parent.is_dir():
            try:
                descriptor = os.open(
                    path.parent,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                )
                try:
                    if _opened_directory_is_protected(
                        descriptor,
                        self.protected_trees,
                        self.protected_directory_identities,
                    ):
                        raise ResultFileError(
                            "result-file parent aliases a protected tree: "
                            f"{path.parent}"
                        )
                finally:
                    os.close(descriptor)
            except ResultFileError:
                raise
            except OSError as exc:
                raise ResultFileError(
                    f"cannot compare result/protected root identity: {exc}"
                ) from exc
        for raw_file in self.protected_files:
            protected = _absolute_without_symlinks(raw_file, label="protected file")
            if path == protected:
                raise ResultFileError(
                    f"result-file must not replace protected file: {protected}"
                )
            try:
                if path.exists() and protected.exists() and os.path.samefile(path, protected):
                    raise ResultFileError(
                        f"result-file aliases protected file: {protected}"
                    )
            except OSError as exc:
                raise ResultFileError(
                    f"cannot compare result/protected file identity: {exc}"
                ) from exc


def prepare_result_destination(
    result_file: str | Path | None,
    repository_root: str | Path,
    *,
    protected_roots: tuple[str | Path, ...] = (),
    protected_files: tuple[str | Path, ...] = (),
) -> ResultDestination | None:
    if result_file is None or str(result_file) == "":
        return None
    repository = _absolute_without_symlinks(repository_root, label="repository")
    roots = tuple(
            _absolute_without_symlinks(path, label="protected root")
            for path in protected_roots
        )
    destination = ResultDestination(
        path=_absolute_without_symlinks(result_file, label="result-file"),
        repository_root=repository,
        protected_roots=roots,
        protected_files=tuple(
            _absolute_without_symlinks(path, label="protected file")
            for path in protected_files
        ),
        # 大数据根只遍历一次。后续 validate/publish 只查缓存并沿已打开目录的
        # 祖先链复验，避免每次结果写入都重扫视频和 MinIO 目录。
        protected_directory_identities=_directory_identity_index((repository, *roots)),
    )
    destination.validate()
    expected_identity = os.environ.get(RESULT_ROOT_IDENTITY_ENV)
    if expected_identity:
        try:
            info = destination.path.parent.stat(follow_symlinks=False)
        except OSError as exc:
            raise ResultFileError(
                f"cannot stat mounted result-file parent: {destination.path.parent}: {exc}"
            ) from exc
        actual_identity = f"{info.st_dev}:{info.st_ino}"
        if actual_identity != expected_identity:
            raise ResultFileError(
                "result-file parent changed before container validation: "
                f"{expected_identity} != {actual_identity}"
            )
    return destination


def ensure_output_roots_disjoint(
    output_roots: tuple[str | Path, ...],
    protected_roots: tuple[str | Path, ...],
) -> None:
    """仓库/工作目录不能嵌进或物理别名到本轮数据源和目标。"""
    outputs = tuple(
        _absolute_without_symlinks(path, label="output root")
        for path in output_roots if str(path)
    )
    protected = tuple(
        _absolute_without_symlinks(path, label="protected root")
        for path in protected_roots if str(path)
    )
    output_identities = _directory_identity_index(outputs)
    protected_identities = _directory_identity_index(protected)
    for output in outputs:
        for root in protected:
            if output == root or output in root.parents or root in output.parents:
                raise ResultFileError(
                    f"output root {output} overlaps protected data root {root}"
                )
            output_anchor = _nearest_existing_directory(output)
            root_anchor = _nearest_existing_directory(root)
            if output_anchor is None or root_anchor is None:
                continue
            output_info = output_anchor.stat(follow_symlinks=False)
            root_info = root_anchor.stat(follow_symlinks=False)
            if (
                (output_info.st_dev, output_info.st_ino) in protected_identities
                or (root_info.st_dev, root_info.st_ino) in output_identities
            ):
                raise ResultFileError(
                    f"output root {output} physically aliases protected data root {root}"
                )


def _nearest_existing_directory(path: Path) -> Path | None:
    """返回路径或最近现存目录,让未创建输出也能识别bind别名祖先。"""
    current = path
    while True:
        try:
            info = current.stat(follow_symlinks=False)
        except FileNotFoundError:
            parent = current.parent
            if parent == current:
                return None
            current = parent
            continue
        except OSError as exc:
            raise ResultFileError(f"cannot inspect output boundary {current}: {exc}") from exc
        return current if stat.S_ISDIR(info.st_mode) else None


def _directory_identity_index(roots: tuple[Path, ...]) -> frozenset[tuple[int, int]]:
    """每个现存目录树只遍历一次，缓存目录实体用于识别 bind alias。"""
    identities: set[tuple[int, int]] = set()
    for root in sorted(set(roots), key=lambda item: len(item.parts)):
        if not root.is_dir():
            continue
        try:
            root_info = root.stat(follow_symlinks=False)
            root_identity = (root_info.st_dev, root_info.st_ino)
            # 更短的祖先树已把该实体收进索引时不重复遍历。它同时覆盖词法嵌套
            # 和另一个路径bind到祖先树子目录两种情况。
            if root_identity in identities:
                continue
            for _path, directory_names, _files, directory_fd in os.fwalk(
                root, topdown=True, follow_symlinks=False,
            ):
                directory_names[:] = [
                    name for name in directory_names
                    if not stat.S_ISLNK(os.stat(
                        name, dir_fd=directory_fd, follow_symlinks=False,
                    ).st_mode)
                ]
                current = os.fstat(directory_fd)
                identities.add((current.st_dev, current.st_ino))
        except OSError as exc:
            raise ResultFileError(
                f"cannot build protected directory identity index: {root}: {exc}"
            ) from exc
    return frozenset(identities)


def _opened_directory_within_tree(directory_fd: int, tree_root: Path) -> bool:
    """从已打开目录句柄逐级 openat(".."),识别 bind/祖先替换后的真实包含关系。"""
    try:
        repository_identity = tree_root.stat(follow_symlinks=False)
    except FileNotFoundError:
        return False
    current_fd = os.dup(directory_fd)
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        while True:
            current = os.fstat(current_fd)
            if (current.st_dev, current.st_ino) == (
                repository_identity.st_dev, repository_identity.st_ino,
            ):
                return True
            parent_fd = os.open("..", directory_flags, dir_fd=current_fd)
            parent = os.fstat(parent_fd)
            if (parent.st_dev, parent.st_ino) == (current.st_dev, current.st_ino):
                os.close(parent_fd)
                return False
            os.close(current_fd)
            current_fd = parent_fd
    finally:
        os.close(current_fd)


def _opened_directory_aliases_tree(directory_fd: int, tree_root: Path) -> bool:
    """识别bind alias到受保护树任一子目录,不只比较根inode。"""
    if _opened_directory_within_tree(directory_fd, tree_root):
        return True
    try:
        opened = os.fstat(directory_fd)
        for _path, directory_names, _files, repository_fd in os.fwalk(
            tree_root, topdown=True, follow_symlinks=False,
        ):
            # 仓库契约不允许symlink目录；发现时不跟随并从遍历集合删除。
            directory_names[:] = [
                name for name in directory_names
                if not stat.S_ISLNK(os.stat(
                    name, dir_fd=repository_fd, follow_symlinks=False,
                ).st_mode)
            ]
            current = os.fstat(repository_fd)
            if (current.st_dev, current.st_ino) == (opened.st_dev, opened.st_ino):
                return True
    except OSError as exc:
        raise ResultFileError(f"cannot inspect protected directory identities: {exc}") from exc
    return False


def _opened_directory_is_protected(
    directory_fd: int,
    protected_roots: tuple[Path, ...],
    protected_directory_identities: frozenset[tuple[int, int]],
) -> bool:
    """用预建身份索引识别旧子目录别名，并用祖先链识别运行中移动。"""
    opened = os.fstat(directory_fd)
    if (opened.st_dev, opened.st_ino) in protected_directory_identities:
        return True
    return any(
        _opened_directory_within_tree(directory_fd, root)
        for root in protected_roots if root.exists()
    )


def _opened_directory_aliases_repository_tree(
    directory_fd: int, repository_root: Path,
) -> bool:
    """保留旧测试入口;实现已推广到任意受保护目录树。"""
    return _opened_directory_aliases_tree(directory_fd, repository_root)


def _open_or_create_directory_without_symlinks(
    path: Path,
    protected_roots: tuple[Path, ...],
    protected_directory_identities: frozenset[tuple[int, int]],
) -> int:
    """从根 dirfd 逐分量 mkdirat/openat,首次创建也不越过仓库边界。"""
    if not path.is_absolute():
        raise ResultFileError(f"result-file parent must be absolute: {path}")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    current_fd = os.open(path.anchor, flags)
    try:
        for part in path.parts[1:]:
            try:
                next_fd = os.open(part, flags, dir_fd=current_fd)
            except FileNotFoundError:
                if _opened_directory_is_protected(
                    current_fd, protected_roots, protected_directory_identities,
                ):
                    raise ResultFileError(
                        "refusing to create a result-file directory inside the "
                        "a protected data tree"
                    )
                try:
                    os.mkdir(part, mode=0o700, dir_fd=current_fd)
                    os.fsync(current_fd)
                except FileExistsError:
                    # 与并发创建者竞争时仍经下面的 O_NOFOLLOW openat 复验。
                    pass
                try:
                    next_fd = os.open(part, flags, dir_fd=current_fd)
                except OSError as exc:
                    raise ResultFileError(
                        "cannot open newly created result-file parent component "
                        f"without symlinks: {part}: {exc}"
                    ) from exc
            except OSError as exc:
                raise ResultFileError(
                    f"cannot open result-file parent component without symlinks: {part}: {exc}"
                ) from exc
            os.close(current_fd)
            current_fd = next_fd
            if _opened_directory_is_protected(
                current_fd, protected_roots, protected_directory_identities,
            ):
                raise ResultFileError(
                    "opened result-file parent is inside a protected data tree"
                )
        return current_fd
    except BaseException:
        os.close(current_fd)
        raise


def write_result_json(payload: dict[str, Any], destination: ResultDestination | None) -> str:
    """返回格式化 JSON,有目标时以同目录临时文件 + replace 原子发布。"""
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, default=str)
    if destination is None:
        return text
    destination.validate()
    path = destination.path
    protected_trees = destination.protected_trees
    directory_fd = _open_or_create_directory_without_symlinks(
        path.parent, protected_trees, destination.protected_directory_identities,
    )
    opened_identity = os.fstat(directory_fd)
    if _opened_directory_is_protected(
        directory_fd, protected_trees, destination.protected_directory_identities,
    ):
        os.close(directory_fd)
        raise ResultFileError(
            "opened result-file parent is inside a protected data tree"
        )
    current_identity = os.stat(path.parent, follow_symlinks=False)
    if (opened_identity.st_dev, opened_identity.st_ino) != (
        current_identity.st_dev, current_identity.st_ino,
    ):
        os.close(directory_fd)
        raise ResultFileError("result-file parent changed while it was being opened")
    temporary_name = f".{path.name}.{uuid.uuid4().hex}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    published_identity: tuple[int, int] | None = None
    try:
        descriptor = os.open(temporary_name, flags, 0o600, dir_fd=directory_fd)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary_info = os.stat(
            temporary_name, dir_fd=directory_fd, follow_symlinks=False,
        )
        if _opened_directory_is_protected(
            directory_fd, protected_trees, destination.protected_directory_identities,
        ):
            raise ResultFileError(
                "result-file parent moved into a protected data tree before publication"
            )
        os.replace(
            temporary_name,
            path.name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        published_identity = (temporary_info.st_dev, temporary_info.st_ino)
        os.fsync(directory_fd)
        # 攻击者即使在 openat 后替换父目录,也只能让写入落在旧目录句柄里。
        # 这里再比身份并报失败,绝不把“请求路径没收到结果”误报成成功。
        published_parent = os.stat(path.parent, follow_symlinks=False)
        unsafe_parent = (
            (opened_identity.st_dev, opened_identity.st_ino)
            != (published_parent.st_dev, published_parent.st_ino)
            or _opened_directory_is_protected(
                directory_fd,
                protected_trees,
                destination.protected_directory_identities,
            )
        )
        if unsafe_parent:
            try:
                published = os.stat(
                    path.name, dir_fd=directory_fd, follow_symlinks=False,
                )
                if (published.st_dev, published.st_ino) == published_identity:
                    os.unlink(path.name, dir_fd=directory_fd)
                    os.fsync(directory_fd)
            except FileNotFoundError:
                pass
            raise ResultFileError(
                "result-file parent changed or moved into a protected data tree "
                "during atomic publication"
            )
    except BaseException:
        try:
            os.unlink(temporary_name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        raise
    finally:
        os.close(directory_fd)
    return text


def emit_result(payload: dict[str, Any], destination: ResultDestination | None) -> None:
    print(write_result_json(payload, destination))
