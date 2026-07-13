"""验证 Mutation scorer 不把 pytest 基础设施错误冒充为 killed."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import mutation_score
from scripts.mutation_score import (
    MutationCounts,
    MutationOutcome,
    TargetResult,
    classify_pytest_exit_code,
    measure_mutants,
    score_mutants,
)


@pytest.mark.parametrize(
    ("returncode", "expected"),
    [
        (0, MutationOutcome.SURVIVED),
        (1, MutationOutcome.KILLED),
        (2, MutationOutcome.INFRA_ERROR),
        (3, MutationOutcome.INFRA_ERROR),
        (4, MutationOutcome.INFRA_ERROR),
        (5, MutationOutcome.INFRA_ERROR),
        (124, MutationOutcome.INFRA_ERROR),
        (-9, MutationOutcome.INFRA_ERROR),
    ],
)
def test_classify_pytest_exit_code(returncode: int, expected: MutationOutcome) -> None:
    assert classify_pytest_exit_code(returncode) is expected


def test_score_mutants_tracks_three_outcomes() -> None:
    returncodes = iter([0, 1, 2])

    counts = score_mutants(["a", "b", "c"], run_mutant=lambda _mid: next(returncodes))

    assert (counts.killed, counts.survived, counts.infra_error) == (1, 1, 1)
    assert counts.valid is False


def test_score_mutants_treats_launch_error_as_infra() -> None:
    def fail(_mid: str) -> int:
        raise OSError("runner unavailable")

    counts = score_mutants(["a"], run_mutant=fail)

    assert counts.infra_error == 1


def test_score_mutants_treats_timeout_as_infra() -> None:
    def timeout(_mid: str) -> int:
        raise mutation_score.subprocess.TimeoutExpired("pytest", 300)

    counts = score_mutants(["a"], run_mutant=timeout)

    assert counts.infra_error == 1


@pytest.mark.parametrize("baseline_returncode", [1, 2, 5])
def test_failed_clean_baseline_blocks_mutant_loop(baseline_returncode: int) -> None:
    called: list[str] = []

    counts, detail = measure_mutants(
        ["a"],
        run_baseline=lambda: baseline_returncode,
        run_mutant=lambda mid: called.append(mid) or 0,
    )

    assert counts.infra_error == 1
    assert called == []
    assert detail and "clean baseline failed" in detail


def test_empty_mutant_set_is_infrastructure_error() -> None:
    counts, detail = measure_mutants(
        [],
        run_baseline=lambda: 0,
        run_mutant=lambda _mid: 0,
    )

    assert counts.infra_error == 1
    assert detail == "no mutants generated"


def test_unknown_target_fails_without_zero_score(capsys) -> None:
    assert mutation_score.main(["mutation_score.py", "does-not-exist"]) == 2
    assert "unknown mutation target" in capsys.readouterr().err


def test_infrastructure_error_report_is_not_persistable(capsys) -> None:
    mutation_score._print_report(
        [TargetResult("shared.db", MutationCounts(infra_error=1), "baseline failed")]
    )

    output = capsys.readouterr().out
    assert "infra-error" in output
    assert "<!--MUTATION-CSV-->" not in output


def test_valid_report_keeps_existing_csv_shape(capsys) -> None:
    mutation_score._print_report(
        [TargetResult("shared.db", MutationCounts(killed=2, survived=1))]
    )

    output = capsys.readouterr().out
    assert "<!--MUTATION-CSV-->" in output
    assert "shared.db,2,1" in output


def test_generation_error_restores_pyproject(tmp_path, monkeypatch) -> None:
    original = "pytest_add_cli_args_test_selection = ['tests/']\n"
    (tmp_path / "pyproject.toml").write_text(original)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        mutation_score.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("mutmut missing")),
    )

    with pytest.raises(OSError, match="mutmut missing"):
        mutation_score._generate_mutants("shared.db")

    assert (tmp_path / "pyproject.toml").read_text() == original


def test_enumerates_top_level_class_and_async_mutants(tmp_path, monkeypatch) -> None:
    module = tmp_path / "mutants/shared/db.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        """
def x_top__mutmut_1():
    pass

async def x_async_top__mutmut_2():
    pass

class Database:
    def xǁDatabaseǁsave__mutmut_3(self):
        pass

    async def xǁDatabaseǁload__mutmut_4(self):
        pass
"""
    )
    monkeypatch.chdir(tmp_path)

    assert mutation_score._enumerate_mutants("shared.db") == [
        "shared.db.x_top__mutmut_1",
        "shared.db.x_async_top__mutmut_2",
        "shared.db.xǁDatabaseǁsave__mutmut_3",
        "shared.db.xǁDatabaseǁload__mutmut_4",
    ]


def test_generation_nonzero_is_infrastructure_error_and_restores_pyproject(
    tmp_path, monkeypatch
) -> None:
    original = "pytest_add_cli_args_test_selection = ['tests/']\n"
    (tmp_path / "pyproject.toml").write_text(original)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        mutation_score.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=2),
    )

    with pytest.raises(RuntimeError, match="exit code 2"):
        mutation_score._generate_mutants("shared.db")

    assert (tmp_path / "pyproject.toml").read_text() == original


def test_pytest_runner_has_bounded_timeout(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(*_args, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(mutation_score.subprocess, "run", fake_run)

    assert mutation_score._run_pytest(["tests/test_db.py"], "mutant-id") == 0
    assert captured["timeout"] == mutation_score.MUTANT_TIMEOUT_SECONDS


def test_main_returns_two_and_omits_csv_when_setup_fails(monkeypatch, capsys) -> None:
    def fail_generation(_prefix: str) -> None:
        raise RuntimeError("partial mutant generation")

    monkeypatch.setattr(mutation_score, "_generate_mutants", fail_generation)

    assert mutation_score.main(["mutation_score.py", "db"]) == 2
    output = capsys.readouterr().out
    assert "infra-error" in output
    assert "<!--MUTATION-CSV-->" not in output


def test_workflow_propagates_scorer_failure_through_tee() -> None:
    workflow = Path(__file__).parents[1] / ".github/workflows/mutation.yml"
    content = workflow.read_text()

    assert "set -o pipefail" in content
    assert "score_status=${PIPESTATUS[0]}" in content
    assert content.index("GITHUB_STEP_SUMMARY") < content.index('exit "$score_status"')
