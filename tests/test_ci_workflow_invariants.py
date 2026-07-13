"""验证 CI 取消,累计路径分类与发布门的结构不变量."""

from pathlib import Path

import yaml


WORKFLOW = Path(__file__).parents[1] / ".github/workflows/ci.yml"


def load_workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


def test_cancelable_jobs_use_scoped_job_concurrency() -> None:
    workflow = load_workflow()
    assert "concurrency" not in workflow

    jobs = workflow["jobs"]
    matrix_groups = {
        "unit-normal": "matrix.group",
        "unit-worker": "matrix.group",
        "build-images": "matrix.image",
    }
    for job_name, matrix_key in matrix_groups.items():
        concurrency = jobs[job_name]["concurrency"]
        assert concurrency["cancel-in-progress"] is True
        assert "github.ref" in concurrency["group"]
        assert matrix_key in concurrency["group"]

    for job_name in ("coverage-gate", "fe-test", "detect", "coverage-badge"):
        concurrency = jobs[job_name]["concurrency"]
        assert concurrency["cancel-in-progress"] is True
        assert "github.ref" in concurrency["group"]


def test_release_is_non_cancelable_and_coverage_gated() -> None:
    push = load_workflow()["jobs"]["push-images"]
    concurrency = push["concurrency"]

    assert concurrency["cancel-in-progress"] is False
    assert "github.ref" in concurrency["group"]
    assert "matrix.image" in concurrency["group"]
    assert set(push["needs"]) == {"coverage-gate", "fe-test", "detect", "build-images"}

    # GitHub concurrency 对同组只保留最新 pending;这里明确采用 latest-wins.
    # 累计基线负责把被替代运行的改动带入最新 HEAD.
    workflow_text = WORKFLOW.read_text()
    assert "可覆盖未启动的旧排队" in workflow_text


def test_detect_uses_last_successful_run_and_fail_safe_classification() -> None:
    detect = load_workflow()["jobs"]["detect"]
    assert detect["permissions"]["actions"] == "read"
    assert detect["outputs"] == {
        "backend": "${{ steps.classify.outputs.backend }}",
        "frontend": "${{ steps.classify.outputs.frontend }}",
    }

    steps = {step.get("id"): step for step in detect["steps"] if step.get("id")}
    baseline_run = steps["baseline"]["run"]
    assert "status=success" in baseline_run
    assert "ci_select_change_base.py" in baseline_run
    assert "force_all=true" in baseline_run

    filters = steps["f"]["with"]
    assert "steps.baseline.outputs.sha" in filters["base"]
    assert "docker:" in filters["filters"]

    classify_run = steps["classify"]["run"]
    assert "steps.baseline.outputs.force_all" in classify_run
    assert "backend=true" in classify_run
    assert "frontend=true" in classify_run
    assert "ci_pyproject_change.py" in classify_run
    assert "ci_docker_change.py" in classify_run
    assert '"$HEAD_SHA" docker' in classify_run
    assert '"$HEAD_SHA" frontend' in classify_run
