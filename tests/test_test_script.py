"""验证容器化测试入口不会静默退化增量选择."""

from __future__ import annotations

import ast
import os
import stat
import subprocess
import tomllib
from collections.abc import Iterable
from pathlib import Path


REPO = Path(__file__).parents[1]


def test_repository_shell_entrypoints_are_executable() -> None:
    """新 checkout 必须保留脚本执行位;CI 用 bash 启动测试以输出精确失败."""
    non_executable = [
        path.relative_to(REPO).as_posix()
        for path in sorted((REPO / "scripts").glob("*.sh"))
        if not path.stat().st_mode & stat.S_IXUSR
    ]

    assert non_executable == []


def _numeric_value(node: ast.AST) -> float | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) \
            and not isinstance(node.value, bool):
        return float(node.value)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _numeric_value(node.operand)
        if value is not None:
            return value if isinstance(node.op, ast.UAdd) else -value
    if isinstance(node, ast.BinOp):
        left = _numeric_value(node.left)
        right = _numeric_value(node.right)
        if left is None or right is None:
            return None
        try:
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if isinstance(node.op, ast.Div):
                return left / right
        except ZeroDivisionError:
            return None
    return None


def _contains_fifty_milliseconds(nodes: Iterable[ast.AST]) -> bool:
    return any(_numeric_value(node) == 0.05 for root in nodes for node in ast.walk(root))


def _fixed_sleep_lines(source: str, filename: str = "<source>") -> list[int]:
    tree = ast.parse(source, filename=filename)
    module_aliases = {"asyncio", "time"}
    sleep_aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for name in node.names:
                if name.name in {"asyncio", "time"}:
                    module_aliases.add(name.asname or name.name)
        elif isinstance(node, ast.ImportFrom) and node.module in {"asyncio", "time"}:
            for name in node.names:
                if name.name == "sleep":
                    sleep_aliases.add(name.asname or name.name)

    # 覆盖 `real_sleep = asyncio.sleep` 及 alias 再赋值;这是旧守卫漏掉的路径.
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            value = node.value
            is_sleep = (
                isinstance(value, ast.Attribute)
                and value.attr == "sleep"
                and isinstance(value.value, ast.Name)
                and value.value.id in module_aliases
            ) or (isinstance(value, ast.Name) and value.id in sleep_aliases)
            for target in targets:
                if is_sleep and isinstance(target, ast.Name) and target.id not in sleep_aliases:
                    sleep_aliases.add(target.id)
                    changed = True

    offenders: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        is_sleep = (
            isinstance(func, ast.Attribute)
            and func.attr == "sleep"
        ) or (isinstance(func, ast.Name) and func.id in sleep_aliases)
        if is_sleep and _contains_fifty_milliseconds([node.args[0]]):
            offenders.append(node.lineno)
    return sorted(set(offenders))


def test_sleep_guard_detects_alias_and_conditional_expression() -> None:
    source = """
import asyncio as aio
real_sleep = aio.sleep
async def wait(secs):
    await real_sleep(5 / 100 if secs == 10 else secs)
"""
    assert _fixed_sleep_lines(source) == [5]


def test_test_tree_has_no_fixed_fifty_millisecond_sleep() -> None:
    """异步测试用可观测屏障等待,禁止用 50ms 猜调度时序."""
    offenders = [
        f"{path.relative_to(REPO)}:{line}"
        for path in (REPO / "tests").rglob("*.py")
        for line in _fixed_sleep_lines(
            path.read_text(encoding="utf-8"), filename=str(path),
        )
    ]

    assert offenders == []


def test_pytest_markers_have_one_authoritative_source() -> None:
    config = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    markers = config["tool"]["pytest"]["ini_options"]["markers"]
    names = {marker.partition(":")[0] for marker in markers}

    assert {"fuzz", "integration", "external"} <= names
    integration_conftest = (REPO / "tests/integration/conftest.py").read_text(
        encoding="utf-8",
    )
    assert "addinivalue_line" not in integration_conftest
    assert "pytest_configure" not in integration_conftest


def _fake_docker_environment(tmp_path: Path) -> dict[str, str]:
    fake_docker = tmp_path / "docker"
    fake_docker.write_text(
        """#!/bin/sh
if [ "$1" = "image" ] && [ "$2" = "inspect" ]; then
  exit 0
fi
if [ "$1" = "ps" ]; then
  printf '%s\\n' fake-running-container
  exit 0
fi
printf '%s\\n' "$*"
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    environment = os.environ.copy()
    environment["PATH"] = f"{tmp_path}:{environment['PATH']}"
    environment["TEST_WARM_NAME"] = "flori-test-script-contract"
    environment["CI_COVERAGE_DIR"] = str(tmp_path / "coverage")
    return environment


def _external_docker_environment(
    tmp_path: Path,
) -> tuple[dict[str, str], Path, Path, Path]:
    fake_docker = tmp_path / "docker"
    state = tmp_path / "unique-image-present"
    state.touch()
    log = tmp_path / "docker.log"
    unique_image = "flori-contract-external:latest"
    fake_docker.write_text(
        """#!/bin/sh
printf '%s\n' "$*" >> "$FAKE_DOCKER_LOG"
if [ "$1" = "image" ] && [ "$2" = "inspect" ]; then
  [ "$3" = "$FAKE_UNIQUE_IMAGE" ] && [ -f "$FAKE_IMAGE_STATE" ] && exit 0
  exit 1
fi
if [ "$1" = "image" ] && [ "$2" = "rm" ]; then
  rm -f "$FAKE_IMAGE_STATE"
  exit 0
fi
if [ "$1" = "compose" ]; then
  case " $* " in
    *" build external "*) : > "$FAKE_IMAGE_STATE" ;;
  esac
  exit 0
fi
exit 0
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    tmp_root = tmp_path / "tmp"
    tmp_root.mkdir()
    artifacts = tmp_path / "outside-artifacts"
    environment = os.environ.copy()
    environment.update({
        "PATH": f"{tmp_path}:{environment['PATH']}",
        "TEST_WARM_NAME": "flori-test-script-external",
        "INTEGRATION_EXTERNAL_IMAGE": unique_image,
        "INTEGRATION_ARTIFACT_DIR": str(artifacts),
        "FLORI_EXTERNAL_ARTICLE_URL": "https://example.com/public-article",
        "TMPDIR": str(tmp_root),
        "FAKE_DOCKER_LOG": str(log),
        "FAKE_IMAGE_STATE": str(state),
        "FAKE_UNIQUE_IMAGE": unique_image,
    })
    return environment, state, log, artifacts


def test_external_blank_selected_url_fails_before_pytest(tmp_path: Path) -> None:
    environment = _fake_docker_environment(tmp_path)
    environment["FLORI_EXTERNAL_ARTICLE_URL"] = " \t "
    environment["TMPDIR"] = str(tmp_path)

    completed = subprocess.run(
        ["bash", str(REPO / "scripts/test.sh"), "--external", "article"],
        cwd=REPO,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 2
    assert "FLORI_EXTERNAL_ARTICLE_URL 未配置" in completed.stderr
    assert "pytest" not in completed.stdout
    external_test = REPO / "tests/integration/test_external_content.py"
    assert "pytest.skip" not in external_test.read_text(encoding="utf-8")


def test_frontend_arguments_remain_vitest_arguments(tmp_path: Path) -> None:
    environment = _fake_docker_environment(tmp_path)

    completed = subprocess.run(
        [
            "bash",
            str(REPO / "scripts/test.sh"),
            "--fe",
            "frontend/src/components/settings/BiliLogin.test.ts",
            "-t",
            "confirmed",
        ],
        cwd=REPO,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "run --rm fe-test sh /repo-scripts/fe-test.sh" in completed.stdout
    assert "/repo-scripts/fe-test.sh src/components/settings/BiliLogin.test.ts -t confirmed" in completed.stdout
    assert "/repo-scripts/fe-test.sh frontend/src/components/settings/BiliLogin.test.ts" not in completed.stdout


def test_external_rebuild_replaces_and_cleans_only_unique_image(tmp_path: Path) -> None:
    environment, state, log, artifacts = _external_docker_environment(tmp_path)

    completed = subprocess.run(
        [
            "bash", str(REPO / "scripts/test.sh"),
            "--rebuild", "--external", "article",
        ],
        cwd=REPO,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    calls = log.read_text(encoding="utf-8").splitlines()
    removals = [line for line in calls if line.startswith("image rm ")]
    assert removals == [
        "image rm -f flori-contract-external:latest",
        "image rm flori-contract-external:latest",
    ]
    assert not state.exists()
    assert artifacts.is_dir()
    assert list((tmp_path / "tmp").glob("flori-integration.*")) == []


def test_external_reuses_but_does_not_remove_preexisting_unique_image(
    tmp_path: Path,
) -> None:
    environment, state, log, _artifacts = _external_docker_environment(tmp_path)

    completed = subprocess.run(
        ["bash", str(REPO / "scripts/test.sh"), "--external", "article"],
        cwd=REPO,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert state.exists()
    assert not any(
        line.startswith("image rm ")
        for line in log.read_text(encoding="utf-8").splitlines()
    )


def test_changed_keeps_testmon_active_without_marker_filter(tmp_path: Path) -> None:
    environment = _fake_docker_environment(tmp_path)

    completed = subprocess.run(
        ["bash", str(REPO / "scripts" / "test.sh"), "--changed"],
        cwd=REPO,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "--testmon" in completed.stdout
    assert "--ignore=tests/test_openapi_fuzz.py" in completed.stdout
    assert "no:cacheprovider" not in completed.stdout
    assert " -m " not in f" {completed.stdout} "


def test_ci_normal_uses_explicit_split_count_and_rejects_overflow(tmp_path: Path) -> None:
    environment = _fake_docker_environment(tmp_path)

    completed = subprocess.run(
        ["bash", str(REPO / "scripts" / "test.sh"), "--ci-normal", "14"],
        cwd=REPO,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "--splitting-algorithm least_duration" in completed.stdout
    assert "--ignore=tests/test_canonical_evidence_e2e.py" in completed.stdout
    assert "--splits 14 --group 14" in completed.stdout
    assert "-n 2" in completed.stdout
    assert ".coverage.normal.14" in completed.stdout

    worker = subprocess.run(
        ["bash", str(REPO / "scripts" / "test.sh"), "--ci-worker", "2", "2"],
        cwd=REPO,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert worker.returncode == 0, worker.stderr
    assert "tests/test_canonical_evidence_e2e.py" in worker.stdout
    assert "--splitting-algorithm least_duration" in worker.stdout
    assert "--splits 2 --group 2" in worker.stdout
    assert "-n 4" in worker.stdout
    assert ".coverage.worker.2" in worker.stdout

    overflow = subprocess.run(
        ["bash", str(REPO / "scripts" / "test.sh"), "--ci-normal", "15", "14"],
        cwd=REPO,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert overflow.returncode == 2
    assert "CI shard 超出范围: 15/14" in overflow.stderr


def _fake_frontend_environment(tmp_path: Path) -> tuple[dict[str, str], Path]:
    log = tmp_path / "frontend-tools.log"
    for command in ("npm", "npx", "cmp"):
        executable = tmp_path / command
        executable.write_text(
            """#!/bin/sh
printf '%s %s\n' \"$(basename \"$0\")\" \"$*\" >> \"$FAKE_FRONTEND_LOG\"
if [ -n \"${FAIL_MATCH:-}\" ]; then
  case \"$*\" in *\"$FAIL_MATCH\"*) exit 9 ;; esac
fi
exit 0
""",
            encoding="utf-8",
        )
        executable.chmod(0o755)
    environment = os.environ.copy()
    environment.update({
        "PATH": f"{tmp_path}:{environment['PATH']}",
        "FAKE_FRONTEND_LOG": str(log),
        "FE_INSTALL_MODE": "ci",
    })
    return environment, log


def test_frontend_gate_uses_ci_install_and_runs_all_gates(tmp_path: Path) -> None:
    environment, log = _fake_frontend_environment(tmp_path)

    completed = subprocess.run(
        ["sh", str(REPO / "scripts" / "fe-test.sh"), "src/App.test.ts"],
        cwd=REPO / "frontend",
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    calls = log.read_text(encoding="utf-8").splitlines()
    assert "npm ci --prefer-offline --no-audit --no-fund" in calls
    assert "npx vue-tsc --noEmit" in calls
    assert "npm run typecheck:test" in calls
    assert any(call.startswith("npx openapi-typescript ") for call in calls)
    assert any(call.startswith("cmp -s ") for call in calls)
    assert "npx vitest run --coverage src/App.test.ts" in calls


def test_frontend_gate_waits_all_static_checks_and_fails_closed(tmp_path: Path) -> None:
    environment, log = _fake_frontend_environment(tmp_path)
    environment["FAIL_MATCH"] = "vue-tsc"

    completed = subprocess.run(
        ["sh", str(REPO / "scripts" / "fe-test.sh")],
        cwd=REPO / "frontend",
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 1
    calls = log.read_text(encoding="utf-8").splitlines()
    assert any(call.startswith("npx openapi-typescript ") for call in calls)
    assert "npm run typecheck:test" in calls
    assert not any("vitest run" in call for call in calls)
    assert "前端静态门失败" in completed.stderr


def test_ci_image_runner_launches_all_selected_builds_and_propagates_failure(
    tmp_path: Path,
) -> None:
    fake_docker = tmp_path / "docker"
    log = tmp_path / "docker.log"
    fake_docker.write_text(
        """#!/bin/sh
printf '%s\n' "$*" >> "$FAKE_DOCKER_LOG"
case "$*" in
  *"flori-api:buildcache"*) exit 9 ;;
esac
exit 0
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    environment = os.environ.copy()
    environment.update({
        "PATH": f"{tmp_path}:{environment['PATH']}",
        "FAKE_DOCKER_LOG": str(log),
        "RUNNER_TEMP": str(tmp_path),
        "OWNER_LC": "gwzlchn",
        "FLORI_VERSION": "9.9.9",
        "GITHUB_SHA": "1234567890abcdef",
        "GITHUB_REF": "refs/heads/main",
    })

    completed = subprocess.run(
        ["bash", str(REPO / "scripts/ci-images.sh"), "warm", "true", "true"],
        cwd=REPO,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 1
    calls = log.read_text(encoding="utf-8").splitlines()
    assert len(calls) == 4
    assert sum("--file docker/base.Dockerfile" in call for call in calls) == 3
    assert sum("--file frontend/Dockerfile" in call for call in calls) == 1
    assert "flori-api warm failed" in completed.stderr
    assert list(tmp_path.glob("flori-ci-images-warm-*")) == []


def test_ci_image_push_uses_latest_and_short_sha_tags(tmp_path: Path) -> None:
    fake_docker = tmp_path / "docker"
    log = tmp_path / "docker.log"
    fake_docker.write_text(
        """#!/bin/sh
printf '%s\n' "$*" >> "$FAKE_DOCKER_LOG"
exit 0
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    environment = os.environ.copy()
    environment.update({
        "PATH": f"{tmp_path}:{environment['PATH']}",
        "FAKE_DOCKER_LOG": str(log),
        "RUNNER_TEMP": str(tmp_path),
        "OWNER_LC": "gwzlchn",
        "FLORI_VERSION": "9.9.9",
        "GITHUB_SHA": "1234567890abcdef",
        "GITHUB_REF": "refs/heads/main",
    })

    completed = subprocess.run(
        ["bash", str(REPO / "scripts/ci-images.sh"), "push", "true", "false"],
        cwd=REPO,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    calls = log.read_text(encoding="utf-8").splitlines()
    assert len(calls) == 3
    assert all("--push" in call for call in calls)
    assert all(":latest" in call and ":sha-1234567" in call for call in calls)
