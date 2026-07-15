#!/usr/bin/env bash
# 真实依赖测试编排. 只由 scripts/test.sh 调用.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
MODE="${1:-core}"
shift || true
SCENARIO="${1:-all}"

raw_name="${TEST_WARM_NAME:-flori-test-warm}"
safe_name="$(printf '%s' "$raw_name" | tr '[:upper:]' '[:lower:]' | tr -cs 'a-z0-9_-' '-')"
safe_name="${safe_name#-}"
safe_name="${safe_name%-}"
PROJECT="${safe_name:-flori-test}-integration"
COMPOSE=(docker compose -p "$PROJECT" -f "$REPO/docker-compose.integration-test.yml")
CREATED_IMAGES=()
DRILL_PID=""
PULL_PIDS=()

INTEGRATION_HOST_TMP="$(mktemp -d "${TMPDIR:-/tmp}/flori-integration.XXXXXX")"
INTEGRATION_ARTIFACT_DIR="${INTEGRATION_ARTIFACT_DIR:-$INTEGRATION_HOST_TMP/artifacts}"
if [ "$MODE" = "core" ]; then
  RETRIEVAL_QUALITY_MAIN_SHA="${RETRIEVAL_QUALITY_MAIN_SHA:-$(git -C "$REPO" rev-parse HEAD)}"
else
  # external 不生成检索质量工件；占位只满足 Compose 变量校验，避免要求测试镜像安装 Git。
  RETRIEVAL_QUALITY_MAIN_SHA="${RETRIEVAL_QUALITY_MAIN_SHA:-external-not-applicable}"
fi
mkdir -p "$INTEGRATION_ARTIFACT_DIR"
export INTEGRATION_HOST_TMP INTEGRATION_ARTIFACT_DIR RETRIEVAL_QUALITY_MAIN_SHA
export DOCKER_TEST_IMAGE="${DOCKER_TEST_IMAGE:-python:3.11-slim@sha256:9a7765b36773a37061455b332f18e265e7f58f6fea9c419a550d2a8b0e9db834}"
export FLORI_INTEGRATION_MINIO_IMAGE="${FLORI_INTEGRATION_MINIO_IMAGE:-minio/minio@sha256:14cea493d9a34af32f524e538b8346cf79f3321eff8e708c1e2960462bd8936e}"
export INTEGRATION_TEST_IMAGE="${INTEGRATION_TEST_IMAGE:-${safe_name}-integration:latest}"
export INTEGRATION_EXTERNAL_IMAGE="${INTEGRATION_EXTERNAL_IMAGE:-${safe_name}-external:latest}"
export INTEGRATION_HTTP_PROXY="${FLORI_EXTERNAL_HTTP_PROXY:-${HTTP_PROXY:-}}"
export INTEGRATION_HTTPS_PROXY="${FLORI_EXTERNAL_HTTPS_PROXY:-${HTTPS_PROXY:-}}"
export INTEGRATION_ALL_PROXY="${FLORI_EXTERNAL_ALL_PROXY:-${ALL_PROXY:-}}"
export INTEGRATION_NO_PROXY="${FLORI_EXTERNAL_NO_PROXY:-${NO_PROXY:-}}"

cleanup() {
  status=$?
  trap - EXIT
  if [ -n "$DRILL_PID" ]; then
    kill "$DRILL_PID" >/dev/null 2>&1 || true
    wait "$DRILL_PID" >/dev/null 2>&1 || true
  fi
  for pull_pid in "${PULL_PIDS[@]}"; do
    kill "$pull_pid" >/dev/null 2>&1 || true
    wait "$pull_pid" >/dev/null 2>&1 || true
  done
  if [ "$status" -ne 0 ]; then
    "${COMPOSE[@]}" logs --no-color --tail 200 || true
  fi
  "${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
  for image in "${CREATED_IMAGES[@]}"; do
    docker image rm "$image" >/dev/null 2>&1 || true
  done
  # artifact 可定向到 runner.temp 等外部目录;本次 mktemp 仍必须无条件回收.
  rm -rf -- "$INTEGRATION_HOST_TMP"
  exit "$status"
}
trap cleanup EXIT

ensure_image() {
  image="$1"
  service="$2"
  if [ "${INTEGRATION_FORCE_REBUILD:-0}" = "1" ] \
      && docker image inspect "$image" >/dev/null 2>&1; then
    # unique tag 归本次 TEST_WARM_NAME 所有;--rebuild 不得复用同名旧镜像.
    docker image rm -f "$image" >/dev/null
  fi
  docker image inspect "$image" >/dev/null 2>&1 && return 0
  if [ "$service" = "test" ] \
      && docker image inspect flori-test:latest >/dev/null 2>&1; then
    docker tag flori-test:latest "$image"
    CREATED_IMAGES+=("$image")
    return 0
  fi
  if [ "$service" = "external" ] \
      && docker image inspect flori-test:latest >/dev/null 2>&1 \
      && docker run --rm --entrypoint sh flori-test:latest -c \
        'command -v ffprobe >/dev/null && python3 -c "import trafilatura"'; then
    docker tag flori-test:latest "$image"
    CREATED_IMAGES+=("$image")
    return 0
  fi
  for attempt in 1 2 3; do
    if "${COMPOSE[@]}" build "$service"; then
      CREATED_IMAGES+=("$image")
      return 0
    fi
    echo "构建 $service 失败,重试 $attempt/3" >&2
  done
  return 1
}

run_core() {
  partition="${INTEGRATION_PARTITION:-all}"
  case "$partition" in
    all)
      redis_database=15
      pytest_paths=(tests/integration)
      coverage_file="$INTEGRATION_ARTIFACT_DIR/.coverage.integration"
      junit_file="$INTEGRATION_ARTIFACT_DIR/junit-core.xml"
      run_drill=1
      ;;
    data)
      redis_database=14
      pytest_paths=(
        tests/integration/test_database_migration_recovery.py
        tests/integration/test_dr_compatibility.py
        tests/integration/test_retrieval_quality.py
        tests/integration/test_sqlite_concurrency.py
      )
      coverage_file="$INTEGRATION_ARTIFACT_DIR/.coverage.integration.data"
      junit_file="$INTEGRATION_ARTIFACT_DIR/junit-core-data.xml"
      run_drill=1
      ;;
    services)
      redis_database=15
      pytest_paths=(
        tests/integration/test_pipeline_search_closure.py
        tests/integration/test_real_docker.py
        tests/integration/test_real_redis.py
        tests/integration/test_runner_gateway_e2e.py
        tests/integration/test_study_suggestion_worker_e2e.py
      )
      coverage_file="$INTEGRATION_ARTIFACT_DIR/.coverage.integration.services"
      junit_file="$INTEGRATION_ARTIFACT_DIR/junit-core-services.xml"
      run_drill=0
      ;;
    *) echo "未知 integration 分组: $partition" >&2; return 2 ;;
  esac
  export INTEGRATION_REDIS_URL="redis://redis:6379/$redis_database"
  ensure_image "$INTEGRATION_TEST_IMAGE" test
  PULL_PIDS=()
  for image in "$DOCKER_TEST_IMAGE" "$FLORI_INTEGRATION_MINIO_IMAGE"; do
    if ! docker image inspect "$image" >/dev/null 2>&1; then
      docker pull "$image" &
      PULL_PIDS+=("$!")
    fi
  done
  for pull_pid in "${PULL_PIDS[@]}"; do
    wait "$pull_pid"
  done
  PULL_PIDS=()
  "${COMPOSE[@]}" up -d --wait --wait-timeout 30 redis
  drill_log="$INTEGRATION_HOST_TMP/redis-aof-restore.log"
  if [ "$run_drill" = "1" ]; then
    redis_container="$("${COMPOSE[@]}" ps -q redis)"
    FLORI_INTEGRATION_APP_IMAGE="$INTEGRATION_TEST_IMAGE" \
      FLORI_INTEGRATION_REDIS_IMAGE="$(docker inspect --format '{{.Config.Image}}' "$redis_container")" \
      "$REPO/tests/integration/redis_aof_restore.sh" >"$drill_log" 2>&1 &
    DRILL_PID="$!"
  fi
  run_options=(run --rm)
  coverage_args=()
  if [ "${CI_COVERAGE:-0}" = "1" ]; then
    run_options+=(-e "COVERAGE_FILE=$coverage_file")
    coverage_args+=(
      --cov=shared --cov=api --cov=scheduler --cov=worker --cov=steps
      --cov-branch --cov-report=
    )
  fi
  set +e
  "${COMPOSE[@]}" "${run_options[@]}" test \
    pytest -p no:cacheprovider -m 'integration and not external' \
      "${pytest_paths[@]}" --basetemp="/tmp/flori-pytest-$partition" \
      --junitxml="$junit_file" \
      "${coverage_args[@]}" "$@"
  pytest_status=$?
  drill_status=0
  if [ -n "$DRILL_PID" ]; then
    wait "$DRILL_PID"
    drill_status=$?
    DRILL_PID=""
  fi
  set -e
  [ ! -f "$drill_log" ] || cat "$drill_log"
  if [ "$drill_status" -ne 0 ]; then
    return "$drill_status"
  fi
  return "$pytest_status"
}

external_env_name() {
  case "$1" in
    article) printf '%s' FLORI_EXTERNAL_ARTICLE_URL ;;
    audio) printf '%s' FLORI_EXTERNAL_AUDIO_URL ;;
    rss) printf '%s' FLORI_EXTERNAL_RSS_URL ;;
    youtube) printf '%s' FLORI_EXTERNAL_YOUTUBE_URL ;;
    *) return 1 ;;
  esac
}

run_external() {
  case "$SCENARIO" in
    all) scenarios=(article audio rss youtube); selector='test_external_' ;;
    article|audio|rss|youtube) scenarios=("$SCENARIO"); selector="test_external_${SCENARIO}" ;;
    *) echo "未知 external 场景: $SCENARIO" >&2; return 2 ;;
  esac

  missing=0
  configured=0
  for scenario in "${scenarios[@]}"; do
    env_name="$(external_env_name "$scenario")"
    value="${!env_name:-}"
    if [ -z "${value//[[:space:]]/}" ]; then
      echo "SKIPPED $scenario: $env_name 未配置" >&2
      missing=1
    else
      configured=$((configured + 1))
    fi
  done

  if [ "$configured" -eq 0 ]; then
    echo "外网验证没有可执行场景,不能记为通过" >&2
    return 2
  fi

  ensure_image "$INTEGRATION_EXTERNAL_IMAGE" external
  set +e
  "${COMPOSE[@]}" run --rm external \
    pytest -p no:cacheprovider -m external tests/integration/test_external_content.py \
      -k "$selector" -rs --junitxml="$INTEGRATION_ARTIFACT_DIR/junit-external.xml"
  test_status=$?
  set -e
  if [ "$test_status" -ne 0 ]; then
    return "$test_status"
  fi
  if [ "$missing" -ne 0 ]; then
    echo "外网验证未完整执行,不能记为通过" >&2
    return 2
  fi
}

case "$MODE" in
  core) run_core "$@" ;;
  external) run_external ;;
  *) echo "未知 integration 模式: $MODE" >&2; exit 2 ;;
esac
