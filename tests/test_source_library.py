"""NAS 只读源库引用契约测试。"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from shared.source_library import (
    SourceIdentityMismatch,
    SourceLibrary,
    SourceReferenceError,
    build_source_ref,
    parse_source_ref,
    source_root_tag,
)


def _digest(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def test_source_ref_round_trips_chinese_relative_path():
    ref = build_source_ref("zg-library", "20250914-交易节奏/P01.mkv")

    assert ref == (
        "nas://zg-library/20250914-%E4%BA%A4%E6%98%93%E8%8A%82%E5%A5%8F/P01.mkv"
    )
    parsed = parse_source_ref(ref)
    assert parsed.root_id == "zg-library"
    assert parsed.relative_path == "20250914-交易节奏/P01.mkv"
    assert source_root_tag(parsed.root_id) == "source-root:zg-library"


@pytest.mark.parametrize(
    "relative_path",
    ["", "/etc/passwd", "../escape.mkv", "safe/../../escape.mkv", "a\\b.mkv", "a\x00b.mkv"],
)
def test_source_ref_rejects_non_canonical_or_escaping_paths(relative_path: str):
    with pytest.raises(SourceReferenceError):
        build_source_ref("zg-library", relative_path)


def test_source_library_verifies_full_identity_and_rejects_later_change(tmp_path: Path):
    root = tmp_path / "library"
    source = root / "20250914" / "P01.mkv"
    source.parent.mkdir(parents=True)
    data = b"original-video-bytes"
    source.write_bytes(data)
    library = SourceLibrary({"zg-library": root})
    ref = build_source_ref("zg-library", "20250914/P01.mkv")

    snapshot = library.verify(ref, _digest(data), len(data))

    assert snapshot.size_bytes == len(data)
    assert snapshot.digest == _digest(data)
    source.write_bytes(b"tampered-video-byte")
    with pytest.raises(SourceIdentityMismatch, match="source identity changed"):
        library.verify(ref, _digest(data), len(data))


@pytest.mark.parametrize("link_parent", [False, True])
def test_source_library_rejects_leaf_and_parent_symlinks(tmp_path: Path, link_parent: bool):
    root = tmp_path / "library"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    target = outside / "P01.mkv"
    target.write_bytes(b"video")
    if link_parent:
        (root / "linked").symlink_to(outside, target_is_directory=True)
        relative = "linked/P01.mkv"
    else:
        (root / "P01.mkv").symlink_to(target)
        relative = "P01.mkv"
    library = SourceLibrary({"zg-library": root})

    with pytest.raises(SourceReferenceError, match="source is unavailable"):
        library.verify(build_source_ref("zg-library", relative), _digest(b"video"), 5)


def test_source_library_rejects_hard_link_alias(tmp_path: Path):
    root = tmp_path / "library"
    root.mkdir()
    source = root / "P01.mkv"
    outside = tmp_path / "outside.mkv"
    outside.write_bytes(b"video")
    source.hardlink_to(outside)
    library = SourceLibrary({"zg-library": root})
    ref = build_source_ref("zg-library", "P01.mkv")

    with pytest.raises(SourceReferenceError, match="hard links"):
        library.verify(ref, _digest(b"video"), 5)
    assert library.status(ref, 5) == "invalid"


def test_source_library_materializes_only_a_temporary_symlink(tmp_path: Path):
    root = tmp_path / "library"
    root.mkdir()
    source = root / "P01.mkv"
    source.write_bytes(b"video")
    library = SourceLibrary({"zg-library": root})
    ref = build_source_ref("zg-library", "P01.mkv")
    work_dir = tmp_path / "work"

    link = library.materialize(ref, _digest(b"video"), 5, work_dir)

    assert link == work_dir / "input" / "source.mp4"
    assert link.is_symlink()
    assert link.resolve() == source
    library.dematerialize(link)
    assert not link.exists() and source.read_bytes() == b"video"


def test_source_status_distinguishes_available_changed_missing_and_unmounted(tmp_path: Path):
    root = tmp_path / "library"
    root.mkdir()
    source = root / "P01.mkv"
    source.write_bytes(b"video")
    ref = build_source_ref("zg-library", "P01.mkv")
    library = SourceLibrary({"zg-library": root})

    assert library.status(ref, 5) == "available"
    source.write_bytes(b"changed")
    assert library.status(ref, 5) == "changed"
    source.unlink()
    assert library.status(ref, 5) == "missing"
    assert SourceLibrary({}).status(ref, 5) == "unmounted"
