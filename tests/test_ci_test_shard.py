"""验证 CI 文件级预分片完整、确定且会纳入未知新文件。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.ci_test_shard import build_file_shards, normal_test_files


REPO = Path(__file__).parents[1]


def _write_test_tree(repo: Path, names: list[str]) -> None:
    tests = repo / "tests"
    tests.mkdir()
    for name in names:
        (tests / name).write_text("def test_value(): pass\n", encoding="utf-8")


def test_real_normal_shards_are_complete_disjoint_and_deterministic() -> None:
    first = build_file_shards(REPO, REPO / ".test_durations", 14)
    second = build_file_shards(REPO, REPO / ".test_durations", 14)
    expected = normal_test_files(REPO)

    assert first == second
    assert sorted(path for group in first for path in group) == expected
    assert len({path for group in first for path in group}) == len(expected)
    assert all(group for group in first)
    assert all(
        not path.startswith("tests/test_step_")
        and path not in {
            "tests/test_worker.py",
            "tests/test_canonical_evidence_e2e.py",
        }
        for group in first
        for path in group
    )


def test_unknown_new_file_is_included_with_conservative_fallback(tmp_path: Path) -> None:
    _write_test_tree(tmp_path, ["test_fast.py", "test_slow.py", "test_new.py"])
    durations = tmp_path / ".test_durations"
    durations.write_text(
        json.dumps({
            "tests/test_fast.py::test_value": 1.0,
            "tests/test_slow.py::test_value": 9.0,
        }),
        encoding="utf-8",
    )

    groups = build_file_shards(tmp_path, durations, 2)

    flattened = [path for group in groups for path in group]
    assert sorted(flattened) == [
        "tests/test_fast.py",
        "tests/test_new.py",
        "tests/test_slow.py",
    ]
    assert flattened.count("tests/test_new.py") == 1


def test_invalid_duration_file_and_excessive_split_fail_closed(
    tmp_path: Path,
) -> None:
    _write_test_tree(tmp_path, ["test_one.py"])
    durations = tmp_path / ".test_durations"
    durations.write_text("[]", encoding="utf-8")

    with pytest.raises(ValueError, match="JSON object"):
        build_file_shards(tmp_path, durations, 1)

    durations.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="超过普通测试文件数"):
        build_file_shards(tmp_path, durations, 2)
