"""验证 CI 从最近完整成功运行选择累计变更基线."""

from scripts.ci_select_change_base import select_change_base


HEAD = "d" * 40
OLD = "a" * 40
RECENT = "c" * 40


def test_selects_nearest_successful_ancestor_regardless_of_api_order() -> None:
    ancestry = {RECENT, OLD}
    distances = {RECENT: 1, OLD: 3}

    assert select_change_base(
        HEAD,
        ["f" * 40, OLD, RECENT],
        is_ancestor=lambda candidate, _head: candidate in ancestry,
        distance=lambda candidate, _head: distances[candidate],
    ) == RECENT


def test_ignores_current_invalid_and_non_ancestor_candidates() -> None:
    assert select_change_base(
        HEAD,
        [HEAD, "not-a-sha", "f" * 40],
        is_ancestor=lambda _candidate, _head: False,
        distance=lambda _candidate, _head: None,
    ) is None


def test_normalizes_candidate_whitespace_and_case() -> None:
    upper = OLD.upper()

    assert select_change_base(
        HEAD,
        [f"  {upper}\n"],
        is_ancestor=lambda candidate, _head: candidate == OLD,
        distance=lambda _candidate, _head: 2,
    ) == OLD
