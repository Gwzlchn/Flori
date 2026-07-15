"""验证 CI 覆盖率预热屏障的 fail-closed 语义."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from scripts.ci_wait_coverage import (
    CoverageWaitError,
    Snapshot,
    evaluate_snapshot,
    expected_producers,
    wait_until_ready,
)


NORMAL_SPLITS = 15
WORKER_SPLITS = 1


def _job(name: str, conclusion: str = "success") -> dict[str, object]:
    return {"name": name, "status": "completed", "conclusion": conclusion}


def _complete_snapshot() -> Snapshot:
    return Snapshot(
        jobs=[_job(name) for name in expected_producers(NORMAL_SPLITS, WORKER_SPLITS)],
    )


def test_runtime_phase_waits_for_current_attempt_group_one_success() -> None:
    ready, message = evaluate_snapshot(
        Snapshot(jobs=[]),
        "runtime",
        NORMAL_SPLITS,
        WORKER_SPLITS,
    )
    assert ready is False
    assert "等待 unit-normal" in message

    ready, message = evaluate_snapshot(
        Snapshot(jobs=[_job("unit-normal (1)")]),
        "runtime",
        NORMAL_SPLITS,
        WORKER_SPLITS,
    )
    assert ready is True
    assert "已成功" in message


@pytest.mark.parametrize(
    "conclusion",
    ["failure", "cancelled", "timed_out", "action_required", "skipped"],
)
def test_runtime_phase_fails_when_group_one_is_terminal(
    conclusion: str,
) -> None:
    with pytest.raises(CoverageWaitError, match="unit-normal"):
        evaluate_snapshot(
            Snapshot(jobs=[_job("unit-normal (1)", conclusion)]),
            "runtime",
            NORMAL_SPLITS,
            WORKER_SPLITS,
        )


def test_all_phase_requires_exact_current_attempt_producer_union() -> None:
    producers = expected_producers(NORMAL_SPLITS, WORKER_SPLITS)
    assert len(producers) == NORMAL_SPLITS + WORKER_SPLITS + 2

    ready, message = evaluate_snapshot(
        _complete_snapshot(),
        "all",
        NORMAL_SPLITS,
        WORKER_SPLITS,
    )
    assert ready is True
    assert "18 个覆盖率生产 job" in message


@pytest.mark.parametrize(
    "conclusion",
    ["failure", "cancelled", "timed_out", "action_required", "skipped"],
)
def test_all_phase_rejects_any_unsuccessful_producer(conclusion: str) -> None:
    snapshot = _complete_snapshot()
    snapshot.jobs[0] = _job(str(snapshot.jobs[0]["name"]), conclusion)
    with pytest.raises(CoverageWaitError, match="未成功"):
        evaluate_snapshot(
            snapshot,
            "all",
            NORMAL_SPLITS,
            WORKER_SPLITS,
        )


def test_all_phase_waits_for_missing_job() -> None:
    snapshot = _complete_snapshot()
    snapshot.jobs.pop()
    ready, message = evaluate_snapshot(
        snapshot,
        "all",
        NORMAL_SPLITS,
        WORKER_SPLITS,
    )
    assert ready is False
    assert "1 个覆盖率生产 job" in message


def test_wait_retries_transient_api_errors_then_succeeds() -> None:
    outcomes: Iterator[Snapshot | CoverageWaitError] = iter([
        CoverageWaitError("GitHub Actions API 请求失败: reset"),
        _complete_snapshot(),
    ])

    def fetch() -> Snapshot:
        outcome = next(outcomes)
        if isinstance(outcome, CoverageWaitError):
            raise outcome
        return outcome

    ticks = iter([0.0, 0.0, 0.1, 0.1])
    result = wait_until_ready(
        fetch,
        "all",
        NORMAL_SPLITS,
        WORKER_SPLITS,
        timeout_seconds=2,
        interval_seconds=0,
        monotonic=lambda: next(ticks),
        sleep=lambda _seconds: None,
    )
    assert "已成功" in result


def test_wait_fails_closed_after_repeated_api_errors() -> None:
    def fetch() -> Snapshot:
        raise CoverageWaitError("GitHub Actions API 请求失败: unavailable")

    with pytest.raises(CoverageWaitError, match="连续失败 3 次"):
        wait_until_ready(
            fetch,
            "all",
            NORMAL_SPLITS,
            WORKER_SPLITS,
            timeout_seconds=5,
            interval_seconds=0,
            monotonic=lambda: 0,
            sleep=lambda _seconds: None,
        )


def test_wait_timeout_is_fail_closed() -> None:
    ticks = iter([0.0, 0.0, 2.0])
    with pytest.raises(CoverageWaitError, match="超时"):
        wait_until_ready(
            lambda: Snapshot(jobs=[]),
            "all",
            NORMAL_SPLITS,
            WORKER_SPLITS,
            timeout_seconds=1,
            interval_seconds=0,
            monotonic=lambda: next(ticks),
            sleep=lambda _seconds: None,
        )
