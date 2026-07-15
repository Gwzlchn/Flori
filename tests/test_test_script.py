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
        ["bash", str(REPO / "scripts" / "test.sh"), "--ci-normal", "15"],
        cwd=REPO,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "test python scripts/ci_test_shard.py --group 15 --splits 15 -- pytest" in completed.stdout
    assert "--splitting-algorithm least_duration" not in completed.stdout
    assert "-n 4" in completed.stdout
    assert "--dist load --maxschedchunk=1" in completed.stdout
    assert ".coverage.normal.15" in completed.stdout

    worker = subprocess.run(
        ["bash", str(REPO / "scripts" / "test.sh"), "--ci-worker", "1", "1"],
        cwd=REPO,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert worker.returncode == 0, worker.stderr
    assert "tests/test_canonical_evidence_e2e.py" in worker.stdout
    assert "--splitting-algorithm least_duration" in worker.stdout
    assert "--maxschedchunk" not in worker.stdout
    assert "--splits 1 --group 1" in worker.stdout
    assert "-n 4" in worker.stdout
    assert ".coverage.worker.1" in worker.stdout

    overflow = subprocess.run(
        ["bash", str(REPO / "scripts" / "test.sh"), "--ci-normal", "16", "15"],
        cwd=REPO,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert overflow.returncode == 2
    assert "CI shard 超出范围: 16/15" in overflow.stderr


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


def _fake_test_runtime_environment(
    tmp_path: Path,
) -> tuple[dict[str, str], Path, Path]:
    fake_docker = tmp_path / "docker"
    log = tmp_path / "docker.log"
    github_output = tmp_path / "github-output"
    fake_docker.write_text(
        """#!/bin/sh
printf '%s\n' "$*" >> "$FAKE_DOCKER_LOG"
if [ "$1" = "pull" ]; then
  count=0
  [ ! -f "$FAKE_PULL_COUNT" ] || count=$(cat "$FAKE_PULL_COUNT")
  count=$((count + 1))
  printf '%s\n' "$count" > "$FAKE_PULL_COUNT"
  [ "$count" -ge "${FAKE_PULL_READY_AFTER:-1}" ]
  exit $?
fi
if [ "$1 $2" = "image inspect" ]; then
  [ "${FAKE_INVALID_REPO_DIGEST:-0}" != 1 ] || {
    printf 'ghcr.io/example-owner/flori-test@sha256:bad\n'
    exit 0
  }
  last=""
  for argument in "$@"; do last="$argument"; done
  case "$last" in
    *flori-test-worker*)
      printf 'ghcr.io/example-owner/flori-test-worker@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\n'
      ;;
    *)
      printf 'ghcr.io/example-owner/flori-test@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n'
      ;;
  esac
  exit 0
fi
if [ "$1" = "tag" ]; then
  exit 0
fi
if [ "$1 $2 $3" = "buildx imagetools inspect" ]; then
  case "$4" in
    *flori-test-worker*)
      [ -f "$FAKE_WORKER_READY" ] || exit 1
      printf '{"digest":"sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}\n'
      ;;
    *flori-test*)
      [ -f "$FAKE_NORMAL_READY" ] || exit 1
      printf '{"digest":"sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}\n'
      ;;
    *) exit 1 ;;
  esac
  exit 0
fi
target=""
metadata=""
previous=""
for argument in "$@"; do
  case "$previous" in
    --target) target="$argument" ;;
    --metadata-file) metadata="$argument" ;;
  esac
  previous="$argument"
done
[ "$target" != "${FAIL_TARGET:-}" ] || exit 9
if [ "$target" = "test-worker-runtime" ]; then
  printf '{"containerimage.digest":"sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}\n' > "$metadata"
  : > "$FAKE_WORKER_READY"
else
  printf '{"containerimage.digest":"sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}\n' > "$metadata"
  : > "$FAKE_NORMAL_READY"
fi
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    environment = os.environ.copy()
    environment.update({
        "PATH": f"{tmp_path}:{environment['PATH']}",
        "FAKE_DOCKER_LOG": str(log),
        "FAKE_NORMAL_READY": str(tmp_path / "normal-ready"),
        "FAKE_WORKER_READY": str(tmp_path / "worker-ready"),
        "FAKE_PULL_COUNT": str(tmp_path / "pull-count"),
        "RUNNER_TEMP": str(tmp_path),
        "OWNER_LC": "example-owner",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_OUTPUT": str(github_output),
    })
    return environment, log, github_output


def test_ci_test_runtime_builds_once_and_outputs_immutable_refs(tmp_path: Path) -> None:
    environment, log, github_output = _fake_test_runtime_environment(
        tmp_path,
    )

    completed = subprocess.run(
        ["bash", str(REPO / "scripts/ci-test-runtime.sh")],
        cwd=REPO,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    calls = log.read_text(encoding="utf-8").splitlines()
    assert len(calls) == 6
    assert sum("--target test-runtime" in call for call in calls) == 1
    assert sum("--target test-worker-runtime" in call for call in calls) == 1
    assert sum("--push" in call for call in calls) == 2
    worker_build = next(call for call in calls if "--target test-worker-runtime" in call)
    assert "flori-test-worker:buildcache" in worker_build
    assert "flori-test:buildcache" in worker_build
    assert not any("FLORI_VERSION=" in call for call in calls)
    outputs = github_output.read_text(encoding="utf-8").splitlines()
    assert outputs == [
        "ready=true",
        "normal=ghcr.io/example-owner/flori-test@sha256:" + "a" * 64,
        "worker=ghcr.io/example-owner/flori-test-worker@sha256:" + "b" * 64,
    ]


def test_ci_test_runtime_probe_reuses_content_keyed_immutable_refs(
    tmp_path: Path,
) -> None:
    environment, log, github_output = _fake_test_runtime_environment(tmp_path)
    Path(environment["FAKE_NORMAL_READY"]).touch()
    Path(environment["FAKE_WORKER_READY"]).touch()

    completed = subprocess.run(
        ["bash", str(REPO / "scripts/ci-test-runtime.sh"), "probe"],
        cwd=REPO,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    calls = log.read_text(encoding="utf-8").splitlines()
    assert len(calls) == 2
    assert all("buildx imagetools inspect" in call for call in calls)
    assert not any("buildx build" in call for call in calls)
    outputs = github_output.read_text(encoding="utf-8").splitlines()
    assert outputs == [
        "ready=true",
        "normal=ghcr.io/example-owner/flori-test@sha256:" + "a" * 64,
        "worker=ghcr.io/example-owner/flori-test-worker@sha256:" + "b" * 64,
    ]


def test_ci_test_runtime_probe_reports_cache_miss_without_building(
    tmp_path: Path,
) -> None:
    environment, log, github_output = _fake_test_runtime_environment(tmp_path)

    completed = subprocess.run(
        ["bash", str(REPO / "scripts/ci-test-runtime.sh"), "probe"],
        cwd=REPO,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "未命中" in completed.stdout
    assert github_output.read_text(encoding="utf-8").splitlines() == [
        "ready=false",
    ]
    assert not any(
        "buildx build" in call
        for call in log.read_text(encoding="utf-8").splitlines()
    )


def test_ci_test_runtime_propagates_parallel_build_failure(tmp_path: Path) -> None:
    environment, _log, github_output = _fake_test_runtime_environment(
        tmp_path,
    )
    environment["FAIL_TARGET"] = "test-worker-runtime"

    completed = subprocess.run(
        ["bash", str(REPO / "scripts/ci-test-runtime.sh")],
        cwd=REPO,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 1
    assert "flori-test-worker build failed" in completed.stderr
    assert not github_output.exists()


def test_ci_test_runtime_is_main_only(tmp_path: Path) -> None:
    environment, _log, _github_output = _fake_test_runtime_environment(
        tmp_path,
    )
    environment["GITHUB_REF"] = "refs/pull/9/merge"

    completed = subprocess.run(
        ["bash", str(REPO / "scripts/ci-test-runtime.sh")],
        cwd=REPO,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 2
    assert "测试运行时仅允许在 main 发布" in completed.stderr


def test_ci_test_runtime_tag_does_not_require_github_output(tmp_path: Path) -> None:
    environment, _log, _github_output = _fake_test_runtime_environment(tmp_path)
    environment.pop("GITHUB_OUTPUT")

    completed = subprocess.run(
        ["bash", str(REPO / "scripts/ci-test-runtime.sh"), "tag", "normal"],
        cwd=REPO,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.startswith(
        "ghcr.io/example-owner/flori-test:runtime-",
    )


def test_ci_test_runtime_pull_retries_then_tags_immutable_digest(
    tmp_path: Path,
) -> None:
    environment, log, _github_output = _fake_test_runtime_environment(tmp_path)
    environment.update({
        "FAKE_PULL_READY_AFTER": "3",
        "CI_RUNTIME_PULL_ATTEMPTS": "4",
        "CI_RUNTIME_PULL_DELAY": "0",
    })

    completed = subprocess.run(
        [
            "bash", str(REPO / "scripts/ci-test-runtime.sh"),
            "pull", "normal", "flori-test:latest",
        ],
        cwd=REPO,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    calls = log.read_text(encoding="utf-8").splitlines()
    assert sum(call.startswith("pull ") for call in calls) == 3
    assert any(call.startswith("image inspect --format ") for call in calls)
    assert calls[-1] == (
        "tag ghcr.io/example-owner/flori-test@sha256:"
        + "a" * 64
        + " flori-test:latest"
    )
    assert "固定到" in completed.stdout


def test_ci_test_runtime_pull_rejects_invalid_repo_digest(tmp_path: Path) -> None:
    environment, log, _github_output = _fake_test_runtime_environment(tmp_path)
    environment["FAKE_INVALID_REPO_DIGEST"] = "1"
    environment["CI_RUNTIME_PULL_DELAY"] = "0"

    completed = subprocess.run(
        [
            "bash", str(REPO / "scripts/ci-test-runtime.sh"),
            "pull", "normal", "flori-test:latest",
        ],
        cwd=REPO,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 1
    assert "缺少有效 RepoDigest" in completed.stderr
    assert not any(
        call.startswith("tag ")
        for call in log.read_text(encoding="utf-8").splitlines()
    )


def test_ci_image_runner_launches_all_selected_builds_and_propagates_failure(
    tmp_path: Path,
) -> None:
    fake_docker = tmp_path / "docker"
    log = tmp_path / "docker.log"
    fake_docker.write_text(
        """#!/bin/sh
printf '%s\n' "$*" >> "$FAKE_DOCKER_LOG"
metadata=""
previous=""
for argument in "$@"; do
  if [ "$previous" = "--metadata-file" ]; then metadata="$argument"; fi
  previous="$argument"
done
case "$*" in
  *"flori-api:buildcache"*) exit 9 ;;
esac
[ -z "$metadata" ] || printf '{"containerimage.digest":"sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}\n' > "$metadata"
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
        "GITHUB_SHA": "1234567890abcdef1234567890abcdef12345678",
        "GITHUB_REF": "refs/heads/main",
        "CI_IMAGE_DIGEST_FILE": str(tmp_path / "candidate-digests.tsv"),
    })

    completed = subprocess.run(
        ["bash", str(REPO / "scripts/ci-images.sh"), "candidate", "true", "true"],
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
    assert all(
        "--push" in call
        and ":candidate-1234567890abcdef1234567890abcdef12345678" in call
        for call in calls
    )
    assert "flori-api candidate failed" in completed.stderr
    assert not (tmp_path / "candidate-digests.tsv").exists()
    assert list(tmp_path.glob("flori-ci-images-candidate-*")) == []


def test_ci_image_candidate_writes_immutable_digest_manifest(tmp_path: Path) -> None:
    fake_docker = tmp_path / "docker"
    fake_docker.write_text(
        """#!/bin/sh
metadata=""
previous=""
for argument in "$@"; do
  if [ "$previous" = "--metadata-file" ]; then metadata="$argument"; fi
  previous="$argument"
done
printf '{"containerimage.digest":"sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}\n' > "$metadata"
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    digest_file = tmp_path / "artifacts" / "candidate-digests.tsv"
    environment = os.environ.copy()
    environment.update({
        "PATH": f"{tmp_path}:{environment['PATH']}",
        "RUNNER_TEMP": str(tmp_path),
        "OWNER_LC": "gwzlchn",
        "FLORI_VERSION": "9.9.9",
        "GITHUB_SHA": "1234567890abcdef1234567890abcdef12345678",
        "GITHUB_REF": "refs/heads/main",
        "CI_IMAGE_DIGEST_FILE": str(digest_file),
    })

    completed = subprocess.run(
        ["bash", str(REPO / "scripts/ci-images.sh"), "candidate", "true", "false"],
        cwd=REPO,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    lines = digest_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    assert {line.split("\t", 1)[0] for line in lines} == {
        "flori-scheduler", "flori-api", "flori-worker",
    }
    assert all(line.endswith("sha256:" + "b" * 64) for line in lines)


def test_ci_image_check_builds_products_without_push(tmp_path: Path) -> None:
    fake_docker = tmp_path / "docker"
    log = tmp_path / "docker.log"
    fake_docker.write_text(
        """#!/bin/sh
printf '%s\n' "$*" >> "$FAKE_DOCKER_LOG"
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
        "GITHUB_SHA": "1234567890abcdef1234567890abcdef12345678",
        "GITHUB_REF": "refs/pull/7/merge",
    })

    completed = subprocess.run(
        ["bash", str(REPO / "scripts/ci-images.sh"), "check", "true", "true"],
        cwd=REPO,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    calls = log.read_text(encoding="utf-8").splitlines()
    assert len(calls) == 4
    assert all("--cache-from" in call for call in calls)
    assert not any("--push" in call or "--cache-to" in call for call in calls)
    assert not any("--metadata-file" in call or "--tag" in call for call in calls)


def test_ci_image_promote_uses_exact_candidate_and_release_tags(tmp_path: Path) -> None:
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
    digest_file = tmp_path / "candidate-digests.tsv"
    digest_file.write_text(
        """flori-scheduler\tsha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc
flori-api\tsha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd
flori-worker\tsha256:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee
""",
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment.update({
        "PATH": f"{tmp_path}:{environment['PATH']}",
        "FAKE_DOCKER_LOG": str(log),
        "RUNNER_TEMP": str(tmp_path),
        "OWNER_LC": "gwzlchn",
        "FLORI_VERSION": "9.9.9",
        "GITHUB_SHA": "1234567890abcdef1234567890abcdef12345678",
        "GITHUB_REF": "refs/heads/main",
        "CI_IMAGE_DIGEST_FILE": str(digest_file),
    })

    completed = subprocess.run(
        ["bash", str(REPO / "scripts/ci-images.sh"), "promote", "true", "false"],
        cwd=REPO,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    calls = log.read_text(encoding="utf-8").splitlines()
    assert len(calls) == 3
    assert all(call.startswith("buildx imagetools create ") for call in calls)
    assert all(":latest" in call and ":sha-1234567" in call for call in calls)
    assert all("@sha256:" in call for call in calls)
    assert not any(":candidate-" in call for call in calls)


def test_ci_image_promote_retries_and_propagates_failure(tmp_path: Path) -> None:
    fake_docker = tmp_path / "docker"
    log = tmp_path / "docker.log"
    fake_docker.write_text(
        """#!/bin/sh
printf '%s\n' "$*" >> "$FAKE_DOCKER_LOG"
case "$*" in
  *"flori-api:latest"*) exit 7 ;;
esac
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    digest_file = tmp_path / "candidate-digests.tsv"
    digest_file.write_text(
        """flori-scheduler\tsha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc
flori-api\tsha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd
flori-worker\tsha256:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee
""",
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment.update({
        "PATH": f"{tmp_path}:{environment['PATH']}",
        "FAKE_DOCKER_LOG": str(log),
        "RUNNER_TEMP": str(tmp_path),
        "OWNER_LC": "gwzlchn",
        "GITHUB_SHA": "1234567890abcdef1234567890abcdef12345678",
        "GITHUB_REF": "refs/heads/main",
        "CI_IMAGE_DIGEST_FILE": str(digest_file),
    })

    completed = subprocess.run(
        ["bash", str(REPO / "scripts/ci-images.sh"), "promote", "true", "false"],
        cwd=REPO,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 1
    calls = log.read_text(encoding="utf-8").splitlines()
    assert sum("flori-api:latest" in call for call in calls) == 3
    assert "flori-api promote failed" in completed.stderr


def test_ci_image_candidate_is_main_only(tmp_path: Path) -> None:
    environment = os.environ.copy()
    environment.update({
        "OWNER_LC": "gwzlchn",
        "FLORI_VERSION": "9.9.9",
        "GITHUB_SHA": "1234567890abcdef1234567890abcdef12345678",
        "GITHUB_REF": "refs/pull/7/merge",
        "RUNNER_TEMP": str(tmp_path),
        "CI_IMAGE_DIGEST_FILE": str(tmp_path / "candidate-digests.tsv"),
    })

    completed = subprocess.run(
        ["bash", str(REPO / "scripts/ci-images.sh"), "candidate", "true", "true"],
        cwd=REPO,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 2
    assert "candidate 仅允许在 main 执行" in completed.stderr


def test_ci_image_promote_is_main_only(tmp_path: Path) -> None:
    environment = os.environ.copy()
    environment.update({
        "OWNER_LC": "gwzlchn",
        "GITHUB_SHA": "1234567890abcdef1234567890abcdef12345678",
        "GITHUB_REF": "refs/pull/7/merge",
        "RUNNER_TEMP": str(tmp_path),
        "CI_IMAGE_DIGEST_FILE": str(tmp_path / "candidate-digests.tsv"),
    })

    completed = subprocess.run(
        ["bash", str(REPO / "scripts/ci-images.sh"), "promote", "true", "true"],
        cwd=REPO,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 2
    assert "promote 仅允许在 main 执行" in completed.stderr
