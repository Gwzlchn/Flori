"""验证 CI 混合预分片完整、确定且会纳入动态 nodeid。"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from scripts.ci_test_shard import (
    build_hybrid_shards,
    collect_nodeids,
    file_duration_weights,
    heavy_test_files,
    load_durations,
    normal_test_files,
)


REPO = Path(__file__).parents[1]


def _write_test_tree(repo: Path, names: list[str]) -> None:
    tests = repo / "tests"
    tests.mkdir()
    for name in names:
        (tests / name).write_text("def test_value(): pass\n", encoding="utf-8")


def test_real_hybrid_shards_are_complete_disjoint_and_deterministic() -> None:
    files = normal_test_files(REPO)
    durations = load_durations(REPO / ".test_durations")
    weights = file_duration_weights(durations, files)
    heavy = heavy_test_files(weights, 14)
    collected = collect_nodeids(REPO, heavy)

    first, first_heavy = build_hybrid_shards(
        REPO, REPO / ".test_durations", 14, collected,
    )
    second, second_heavy = build_hybrid_shards(
        REPO, REPO / ".test_durations", 14, collected,
    )
    expected = {
        *(path for path in files if path not in heavy),
        *(nodeid for path in heavy for nodeid in collected[path]),
    }
    flattened = [item for group in first for item in group]

    assert first == second
    assert first_heavy == second_heavy == heavy
    assert {"tests/test_backup_restore.py", "tests/test_db_migrations.py"} <= set(heavy)
    assert set(flattened) == expected
    assert len(flattened) == len(expected)
    assert all(group for group in first)
    assert not set(heavy) & set(flattened)
    assert all(
        not item.startswith("tests/test_step_")
        and item not in {
            "tests/test_worker.py",
            "tests/test_canonical_evidence_e2e.py",
        }
        for item in flattened
    )


def test_unknown_new_file_and_dynamic_nodeid_are_included(tmp_path: Path) -> None:
    _write_test_tree(tmp_path, ["test_fast.py", "test_slow.py", "test_new.py"])
    durations = tmp_path / ".test_durations"
    durations.write_text(
        json.dumps({
            "tests/test_fast.py::test_value": 4.0,
            "tests/test_slow.py::test_old_a": 20.0,
            "tests/test_slow.py::test_old_b": 20.0,
        }),
        encoding="utf-8",
    )
    collected = {
        "tests/test_slow.py": [
            "tests/test_slow.py::test_old_a",
            "tests/test_slow.py::test_old_b",
            "tests/test_slow.py::test_new_parameter[value]",
        ],
    }

    groups, heavy = build_hybrid_shards(tmp_path, durations, 2, collected)

    flattened = [item for group in groups for item in group]
    assert heavy == ["tests/test_slow.py"]
    assert set(flattened) == {
        "tests/test_fast.py",
        "tests/test_new.py",
        "tests/test_slow.py::test_old_a",
        "tests/test_slow.py::test_old_b",
        "tests/test_slow.py::test_new_parameter[value]",
    }
    assert len(flattened) == len(set(flattened))
    assert "tests/test_slow.py" not in flattened


def test_invalid_duration_and_collection_sets_fail_closed(tmp_path: Path) -> None:
    _write_test_tree(tmp_path, ["test_one.py"])
    durations = tmp_path / ".test_durations"
    durations.write_text("[]", encoding="utf-8")

    with pytest.raises(ValueError, match="JSON object"):
        build_hybrid_shards(tmp_path, durations, 1, {})

    durations.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="超过普通测试文件数"):
        build_hybrid_shards(tmp_path, durations, 2, {})
    with pytest.raises(ValueError, match="collection 集合与计划不一致"):
        build_hybrid_shards(
            tmp_path,
            durations,
            1,
            {"tests/not-in-plan.py": ["tests/not-in-plan.py::test_value"]},
        )


def test_collection_failure_and_missing_nodeids_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 2, stdout="", stderr="collection exploded\n",
        ),
    )
    with pytest.raises(ValueError, match="collection 失败: collection exploded"):
        collect_nodeids(tmp_path, ["tests/test_slow.py"])

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, stdout="2 tests collected\n", stderr="",
        ),
    )
    with pytest.raises(ValueError, match="未 collection 到 nodeid"):
        collect_nodeids(tmp_path, ["tests/test_slow.py"])
