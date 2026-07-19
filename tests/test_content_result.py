"""便携工具 result-file 边界与原子发布测试。"""

from __future__ import annotations

import json
import os

import pytest

import shared.content_result as content_result
from shared.content_backup import main as backup_main
from shared.content_gc import main as gc_main
from shared.content_import import main as import_main
from shared.content_result import (
    ResultFileError,
    _opened_directory_aliases_repository_tree,
    ensure_output_roots_disjoint,
    prepare_result_destination,
    write_result_json,
)


def test_result_inside_repository_is_rejected(tmp_path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    with pytest.raises(ResultFileError, match="outside"):
        prepare_result_destination(repository / "result.json", repository)


def test_symlink_parent_into_repository_is_rejected(tmp_path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(repository, target_is_directory=True)
    with pytest.raises(ResultFileError, match="symbolic link"):
        prepare_result_destination(alias / "result.json", repository)


def test_wrapper_bound_parent_identity_must_match_mounted_directory(
    tmp_path, monkeypatch,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    result_parent = tmp_path / "results"
    result_parent.mkdir()
    monkeypatch.setenv("FLORI_RESULT_ROOT_IDENTITY", "1:2")
    with pytest.raises(ResultFileError, match="changed before container validation"):
        prepare_result_destination(result_parent / "result.json", repository)

    info = result_parent.stat()
    monkeypatch.setenv(
        "FLORI_RESULT_ROOT_IDENTITY", f"{info.st_dev}:{info.st_ino}",
    )
    assert prepare_result_destination(
        result_parent / "result.json", repository,
    ) is not None


def test_repository_subdirectory_entity_is_rejected_as_result_mount(tmp_path) -> None:
    repository = tmp_path / "repo"
    nested = repository / "nested" / "results"
    nested.mkdir(parents=True)
    descriptor = os.open(nested, os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert _opened_directory_aliases_repository_tree(descriptor, repository)
    finally:
        os.close(descriptor)


def test_atomic_publish_replaces_old_json_without_temp_residue(tmp_path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    result = tmp_path / "results" / "result.json"
    destination = prepare_result_destination(result, repository)
    assert destination is not None
    write_result_json({"ok": False, "value": "old"}, destination)
    write_result_json({"ok": True, "value": "new"}, destination)
    assert json.loads(result.read_text(encoding="utf-8")) == {
        "ok": True, "value": "new",
    }
    assert list(result.parent.glob(".result.json.*.tmp")) == []


def test_result_cannot_replace_database_or_enter_source_root(tmp_path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    data = tmp_path / "data"
    database = data / "db" / "analyzer.db"
    database.parent.mkdir(parents=True)
    database.write_bytes(b"sqlite-sentinel")
    with pytest.raises(ResultFileError, match="protected root|protected file"):
        prepare_result_destination(
            database, repository, protected_roots=(data,), protected_files=(database,),
        )
    with pytest.raises(ResultFileError, match="protected root"):
        prepare_result_destination(
            data / "jobs" / "result.json", repository, protected_roots=(data,),
        )
    assert database.read_bytes() == b"sqlite-sentinel"


def test_protected_tree_identity_index_is_built_once_per_destination(
    tmp_path, monkeypatch,
) -> None:
    repository = tmp_path / "repo"
    data = tmp_path / "data"
    (repository / "objects").mkdir(parents=True)
    (data / "jobs" / "nested").mkdir(parents=True)
    result = tmp_path / "results" / "result.json"
    result.parent.mkdir()
    real_fwalk = content_result.os.fwalk
    traversals = 0

    def counted_fwalk(*args, **kwargs):
        nonlocal traversals
        traversals += 1
        return real_fwalk(*args, **kwargs)

    monkeypatch.setattr(content_result.os, "fwalk", counted_fwalk)
    destination = prepare_result_destination(
        result, repository, protected_roots=(data, data / "jobs"),
    )
    assert destination is not None
    prepared_traversals = traversals
    assert prepared_traversals == 2
    write_result_json({"ok": True}, destination)
    write_result_json({"ok": True, "again": True}, destination)
    assert traversals == prepared_traversals


def test_repository_and_work_roots_must_be_outside_data_tree(tmp_path) -> None:
    data = tmp_path / "data"
    repository = data / "portable-repo"
    work = tmp_path / "work"
    repository.mkdir(parents=True)
    work.mkdir()
    with pytest.raises(ResultFileError, match="overlaps"):
        ensure_output_roots_disjoint((repository,), (data,))
    with pytest.raises(ResultFileError, match="overlaps"):
        ensure_output_roots_disjoint((work,), (tmp_path,))


def test_direct_import_rejects_result_that_is_target_database(tmp_path, capsys) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    database = tmp_path / "target.db"
    database.write_bytes(b"target-sentinel")
    code = import_main([
        "--repo", str(repository),
        "--db", str(database),
        "--jobs-dir", str(tmp_path / "jobs"),
        "--config-root", str(tmp_path / "prompts"),
        "--journal", str(tmp_path / "journal" / "import.sqlite3"),
        "--result-file", str(database),
    ])
    assert code == 2
    assert database.read_bytes() == b"target-sentinel"
    assert json.loads(capsys.readouterr().out)["ok"] is False


def test_parent_replacement_cannot_redirect_publication_into_repository(
    tmp_path, monkeypatch,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    result_parent = tmp_path / "results"
    result_parent.mkdir()
    result = result_parent / "result.json"
    destination = prepare_result_destination(result, repository)
    assert destination is not None
    real_replace = os.replace
    replaced = False

    def swap_parent_then_replace(src, dst, *, src_dir_fd=None, dst_dir_fd=None):
        nonlocal replaced
        if not replaced and src_dir_fd is not None:
            replaced = True
            displaced = tmp_path / "displaced-results"
            result_parent.rename(displaced)
            result_parent.symlink_to(repository, target_is_directory=True)
        return real_replace(
            src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd,
        )

    monkeypatch.setattr(os, "replace", swap_parent_then_replace)
    with pytest.raises(ResultFileError, match="parent changed"):
        write_result_json({"ok": True}, destination)
    assert not (repository / "result.json").exists()


def test_opened_result_directory_moved_into_repository_is_cleaned(
    tmp_path, monkeypatch,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    result_parent = tmp_path / "results"
    result_parent.mkdir()
    result = result_parent / "result.json"
    destination = prepare_result_destination(result, repository)
    assert destination is not None
    real_replace = os.replace
    moved = False

    def move_old_parent_into_repo_then_replace(
        src, dst, *, src_dir_fd=None, dst_dir_fd=None,
    ):
        nonlocal moved
        if not moved and src_dir_fd is not None:
            moved = True
            result_parent.rename(repository / "stolen-results")
            result_parent.mkdir()
        return real_replace(
            src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd,
        )

    monkeypatch.setattr(os, "replace", move_old_parent_into_repo_then_replace)
    with pytest.raises(ResultFileError, match="moved into"):
        write_result_json({"ok": True}, destination)
    assert not (repository / "stolen-results" / "result.json").exists()
    assert not result.exists()


def test_ancestor_replacement_before_dirfd_open_is_detected(
    tmp_path, monkeypatch,
) -> None:
    repository = tmp_path / "repo"
    (repository / "results").mkdir(parents=True)
    outside = tmp_path / "outside"
    result_parent = outside / "results"
    result_parent.mkdir(parents=True)
    result = result_parent / "result.json"
    destination = prepare_result_destination(result, repository)
    assert destination is not None
    real_open = os.open
    replaced = False

    def swap_ancestor_then_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal replaced
        if not replaced and dir_fd is not None and os.fspath(path) == outside.name:
            replaced = True
            outside.rename(tmp_path / "displaced-outside")
            outside.symlink_to(repository, target_is_directory=True)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", swap_ancestor_then_open)
    with pytest.raises(ResultFileError, match="without symlinks"):
        write_result_json({"ok": True}, destination)
    assert not (repository / "results" / "result.json").exists()


def test_missing_parent_symlink_race_cannot_create_inside_repository(
    tmp_path, monkeypatch,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    result = tmp_path / "raced-parent" / "result.json"
    destination = prepare_result_destination(result, repository)
    assert destination is not None
    real_mkdir = os.mkdir
    raced = False

    def preempt_mkdir(path, mode=0o777, *, dir_fd=None):
        nonlocal raced
        if not raced and path == "raced-parent" and dir_fd is not None:
            raced = True
            os.symlink(repository, path, target_is_directory=True, dir_fd=dir_fd)
        return real_mkdir(path, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "mkdir", preempt_mkdir)
    with pytest.raises(ResultFileError, match="without symlinks"):
        write_result_json({"ok": True}, destination)
    assert not (repository / "result.json").exists()


@pytest.mark.parametrize(
    ("main", "argv"),
    [
        (backup_main, ["verify"]),
        (gc_main, ["--mark"]),
        (import_main, ["--db", "/data/import/new.db"]),
    ],
)
def test_each_direct_cli_rejects_result_inside_repository(
    tmp_path, capsys, main, argv,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    result = repository / "result.json"
    code = main([*argv, "--repo", str(repository), "--result-file", str(result)])
    assert code == 2
    assert not result.exists()
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert "outside" in payload["error"]
