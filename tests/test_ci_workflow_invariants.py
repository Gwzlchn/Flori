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

    for job_name in ("coverage-gate", "fe-test", "integration", "detect", "coverage-badge"):
        concurrency = jobs[job_name]["concurrency"]
        assert concurrency["cancel-in-progress"] is True
        assert "github.ref" in concurrency["group"]


def test_release_is_non_cancelable_and_coverage_gated() -> None:
    push = load_workflow()["jobs"]["push-images"]
    concurrency = push["concurrency"]

    assert concurrency["cancel-in-progress"] is False
    assert "github.ref" in concurrency["group"]
    assert "matrix.image" in concurrency["group"]
    assert set(push["needs"]) == {
        "coverage-gate", "fe-test", "integration", "detect", "build-images",
    }

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


def test_unit_shards_and_real_integration_use_the_single_test_entrypoint() -> None:
    jobs = load_workflow()["jobs"]
    normal_job = jobs["unit-normal"]
    normal = next(
        step for step in normal_job["steps"]
        if step.get("name", "").startswith("Unit tests")
    )["run"]
    worker_job = jobs["unit-worker"]
    worker = next(
        step for step in worker_job["steps"]
        if step.get("name", "").startswith("Unit tests")
    )["run"]
    integration = jobs["integration"]
    integration_run = next(
        step for step in integration["steps"]
        if step.get("name") == "Real dependency integration gate"
    )["run"]

    assert "bash scripts/test.sh --ci-normal" in normal
    normal_splits = int(normal_job["env"]["CI_NORMAL_SPLITS"])
    assert normal_job["strategy"]["matrix"]["group"] == list(
        range(1, normal_splits + 1),
    )
    assert normal_job["env"]["CI_XDIST_WORKERS"] == "4"
    assert worker_job["env"]["CI_XDIST_WORKERS"] == "4"
    worker_splits = int(worker_job["env"]["CI_WORKER_SPLITS"])
    assert worker_job["strategy"]["matrix"]["group"] == list(
        range(1, worker_splits + 1),
    )
    assert '"$CI_NORMAL_SPLITS"' in normal
    assert "bash scripts/test.sh --ci-worker" in worker
    assert '"$CI_WORKER_SPLITS"' in worker
    assert "docker compose" not in normal + worker
    assert integration["timeout-minutes"] <= 3
    assert integration_run == "bash scripts/test.sh --integration"
    assert integration["env"]["TEST_WARM_NAME"].startswith("flori-ci-")


def test_integration_entrypoint_runs_production_redis_and_minio_restore_drill() -> None:
    entrypoint = WORKFLOW.parents[2] / "scripts" / "run-integration.sh"
    drill = WORKFLOW.parents[2] / "tests" / "integration" / "redis_aof_restore.sh"

    assert drill.is_file()
    entrypoint_text = entrypoint.read_text()
    drill_text = drill.read_text()
    assert '"$REPO/tests/integration/redis_aof_restore.sh"' in entrypoint_text
    assert "minio/minio@sha256:" in entrypoint_text
    assert 'for image in "$DOCKER_TEST_IMAGE" "$FLORI_INTEGRATION_MINIO_IMAGE"' in entrypoint_text
    assert 'docker pull "$image" &' in entrypoint_text
    assert '"$REPO/tests/integration/redis_aof_restore.sh" >"$drill_log" 2>&1 &' in entrypoint_text
    assert 'wait "$DRILL_PID"' in entrypoint_text
    assert 'return "$pytest_status"' in entrypoint_text
    assert 'MINIO_IMAGE="${FLORI_INTEGRATION_MINIO_IMAGE:?' in drill_text
    assert 'MINIO_REQUIRED=1' in drill_text
    assert 'MINIO_VOLUME="$SOURCE_MINIO_VOLUME"' in drill_text
    assert 'MINIO_VOLUME="$TARGET_MINIO_VOLUME"' in drill_text
    assert "put_object(" in drill_text
    assert "stat_object(" in drill_text
    assert "source-minio-disabled" not in drill_text

    workflow = load_workflow()
    integration = workflow["jobs"]["integration"]
    gate = workflow["jobs"]["coverage-gate"]
    run_step = next(
        step for step in integration["steps"]
        if step.get("name") == "Real dependency integration gate"
    )
    assert run_step["env"]["CI_COVERAGE"] == "1"
    assert set(gate["needs"]) == {"unit-normal", "unit-worker", "integration"}


def test_retrieval_quality_artifact_is_sha_bound_and_fail_closed() -> None:
    workflow = load_workflow()
    integration = workflow["jobs"]["integration"]
    run_step = next(
        step for step in integration["steps"]
        if step.get("name") == "Real dependency integration gate"
    )
    assert run_step["env"]["RETRIEVAL_QUALITY_MAIN_SHA"] == "${{ github.sha }}"

    upload = next(
        step for step in integration["steps"]
        if step.get("name") == "Upload retrieval quality decision"
    )
    assert upload["if"] == "always()"
    assert upload["with"]["if-no-files-found"] == "error"
    assert upload["with"]["path"].endswith("/retrieval-quality.json")

    root = WORKFLOW.parents[2]
    entrypoint = (root / "scripts" / "run-integration.sh").read_text()
    assert "git -C \"$REPO\" rev-parse HEAD" in entrypoint
    assert "export INTEGRATION_HOST_TMP INTEGRATION_ARTIFACT_DIR RETRIEVAL_QUALITY_MAIN_SHA" in entrypoint


def test_all_coverage_parts_fail_closed_before_combine() -> None:
    jobs = load_workflow()["jobs"]
    upload_jobs = (jobs["unit-normal"], jobs["unit-worker"], jobs["integration"])
    coverage_uploads = [
        step
        for job in upload_jobs
        for step in job["steps"]
        if step.get("uses", "").startswith("actions/upload-artifact@")
        and str(step.get("with", {}).get("name", "")).startswith("cov-")
    ]
    assert len(coverage_uploads) == 3
    assert all(
        step["with"].get("if-no-files-found") == "error"
        for step in coverage_uploads
    )

    gate = jobs["coverage-gate"]
    assertion_step = next(
        step for step in gate["steps"]
        if step.get("name") == "Assert all coverage parts are present and non-empty"
    )
    assertion = assertion_step["run"]
    normal_splits = int(assertion_step["env"]["NORMAL_SPLITS"])
    worker_splits = int(assertion_step["env"]["WORKER_SPLITS"])
    expected = {
        *(f"covdata/.coverage.normal.{group}" for group in range(1, normal_splits + 1)),
        *(f"covdata/.coverage.worker.{group}" for group in range(1, worker_splits + 1)),
        "covdata/.coverage.integration",
    }
    assert normal_splits == int(jobs["unit-normal"]["env"]["CI_NORMAL_SPLITS"])
    assert worker_splits == int(jobs["unit-worker"]["env"]["CI_WORKER_SPLITS"])
    assert len(expected) == normal_splits + worker_splits + 1
    assert "seq 1 \"$NORMAL_SPLITS\"" in assertion
    assert "seq 1 \"$WORKER_SPLITS\"" in assertion
    assert 'required+=("covdata/.coverage.normal.$group")' in assertion
    assert 'required+=("covdata/.coverage.worker.$group")' in assertion
    assert "covdata/.coverage.integration" in assertion
    assert '[ ! -s "$part" ]' in assertion
    assert "exit 1" in assertion


def test_external_workflow_rejects_whitespace_only_urls() -> None:
    external_path = WORKFLOW.with_name("external.yml")
    external = yaml.safe_load(external_path.read_text())
    validation = next(
        step for step in external["jobs"]["external-content"]["steps"]
        if step.get("name") == "Validate selected public URL variables"
    )["run"]
    assert 'value="${!var:-}"' in validation
    assert '${value//[[:space:]]/}' in validation
    assert '[ "$missing" -eq 0 ]' in validation


def test_external_workflow_requires_explicit_urls_and_single_entrypoint() -> None:
    external_path = WORKFLOW.with_name("external.yml")
    external = yaml.safe_load(external_path.read_text())
    job = external["jobs"]["external-content"]
    run = next(
        step for step in job["steps"]
        if step.get("name") == "Run selected external validation"
    )["run"]

    assert run.startswith("bash scripts/test.sh --external")
    for kind in ("ARTICLE", "AUDIO", "RSS", "YOUTUBE"):
        assert f"FLORI_EXTERNAL_{kind}_URL" in job["env"]
