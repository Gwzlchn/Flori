#!/usr/bin/env bash
# Flori 测试唯一入口 -- 所有 agent / 会话统一走此脚本,别再各写 `docker compose run ...`.
# 权威规约见 CLAUDE.md §测试规约.全容器内跑(宿主不装依赖).
#
# 用常驻热测试容器 flori-test-warm(docker-compose.test.yml 已挂源码 → 改代码即时生效),
# 消除每次 `run --rm` 的容器启停税.首次自动建镜像 + 起热容器.
#
# 用法:
#   scripts/test.sh -m <module> [-m <module2> …]   # 只跑相关模块(默认本地快测):tests/test_<module>*.py
#   scripts/test.sh --changed [-m <module>]        # 只跑受本次改动影响的用例(pytest-testmon,迭代秒级)
#   scripts/test.sh --all                          # 全量 + 覆盖率门 75%(对齐 CI)
#   scripts/test.sh --fe [vitest 参数…]            # 前端 vitest
#   scripts/test.sh --integration [pytest 参数…]   # 真 Redis/SQLite 多进程/real-docker
#   scripts/test.sh --external <场景|all>          # 显式公网 article/audio/rss/youtube
#   scripts/test.sh --wire                         # selected OpenAPI/TS 生成漂移门
#   scripts/test.sh -- <裸 pytest 参数…>           # 透传任意 pytest 参数(高级)
#   scripts/test.sh --rebuild                      # 改了 pyproject [test] 依赖后重建测试镜像
#   scripts/test.sh --down                         # 停/删热容器
#   scripts/test.sh                                # 打本帮助
#
# 标准 flags 已烤进脚本,勿在调用处另写:-p no:cacheprovider  -m 'not fuzz'  -n auto(仅 --all 加).
# --changed 例外:不传 -m 且保留 cacheprovider,避免 pytest-testmon 退化或缺少 --lf 状态;
# 改用 --ignore 排除 fuzz 文件.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"                       # 让 tests/test_*.py glob 相对 worktree 展开(与容器内 /app/tests 同路径)
COMPOSE="$REPO/docker-compose.test.yml"
FE_COMPOSE="$REPO/docker-compose.fe-test.yml"
WARM="${TEST_WARM_NAME:-flori-test-warm}"
IMAGE="flori-test:latest"

usage() { sed -n '2,20p' "$0" | sed 's/^#\{1,\} \{0,1\}//; s/^#$//'; exit "${1:-0}"; }

run_ci_shard() {
  kind="$1"
  group="$2"
  splits="$3"
  case "$group:$splits" in
    *[!0-9:]*|:*) echo "CI shard 参数必须是正整数" >&2; exit 2 ;;
  esac
  [ "$group" -ge 1 ] && [ "$group" -le "$splits" ] || {
    echo "CI shard 超出范围: $group/$splits" >&2
    exit 2
  }

  cov_dir="${CI_COVERAGE_DIR:-$REPO/covdata}"
  mkdir -p "$cov_dir"
  base=(pytest -p no:cacheprovider -m 'not fuzz' -n "${CI_XDIST_WORKERS:-4}")
  if [ "$kind" = "normal" ]; then
    exec docker compose -f "$COMPOSE" run --rm \
      -v "$cov_dir:/covdata" -e "COVERAGE_FILE=/covdata/.coverage.${kind}.${group}" \
      test python scripts/ci_test_shard.py \
        --group "$group" --splits "$splits" -- "${base[@]}" \
        --cov=shared --cov=api --cov=scheduler --cov=worker --cov=steps \
        --cov-branch --cov-report=
  else
    paths=(tests/steps tests/test_step_*.py tests/test_worker.py
           tests/test_canonical_evidence_e2e.py)
  fi
  exec docker compose -f "$COMPOSE" run --rm \
    -v "$cov_dir:/covdata" -e "COVERAGE_FILE=/covdata/.coverage.${kind}.${group}" \
    test "${base[@]}" "${paths[@]}" \
      --splitting-algorithm least_duration \
      --splits "$splits" --group "$group" \
      --cov=shared --cov=api --cov=scheduler --cov=worker --cov=steps \
      --cov-branch --cov-report=
}

ensure_warm() {
  if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    echo ">> 建测试镜像(首次)…" >&2
    docker compose -f "$COMPOSE" build test
  fi
  if [ -z "$(docker ps -q -f "name=^${WARM}$" 2>/dev/null)" ]; then
    docker rm -f "$WARM" >/dev/null 2>&1 || true          # 清已停的同名残留
    echo ">> 起热测试容器 $WARM(源码挂载,常驻;下次复用)…" >&2
    docker compose -f "$COMPOSE" run -d --name "$WARM" --entrypoint sh test -c 'sleep infinity' >/dev/null
  fi
}

# 参数解析
[ $# -eq 0 ] && usage 0
MODE="fast"; CHANGED=0; MODULES=(); RAW=()
while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help) usage 0 ;;
    --down) docker rm -f "$WARM" >/dev/null 2>&1 && echo ">> 已删热容器 $WARM" || echo ">> 无热容器"; exit 0 ;;
    --rebuild) docker rm -f "$WARM" >/dev/null 2>&1 || true; docker compose -f "$COMPOSE" build test; export INTEGRATION_FORCE_REBUILD=1; echo ">> 已重建测试镜像(改了 [test] 依赖后用)"; shift ;;
    --fe)
      shift
      [ $# -gt 0 ] || exec docker compose -f "$FE_COMPOSE" run --rm fe-test
      FE_ARGS=()
      for arg in "$@"; do
        case "$arg" in
          frontend/*) FE_ARGS+=("${arg#frontend/}") ;;
          *) FE_ARGS+=("$arg") ;;
        esac
      done
      exec docker compose -f "$FE_COMPOSE" run --rm fe-test \
        sh /repo-scripts/fe-test.sh "${FE_ARGS[@]}"
      ;;
    --integration) shift; exec "$REPO/scripts/run-integration.sh" core "$@" ;;
    --external) shift; exec "$REPO/scripts/run-integration.sh" external "$@" ;;
    --wire) shift; [ $# -eq 0 ] || usage 1; exec "$REPO/scripts/generate-frontend-wire.sh" --check ;;
    --ci-normal)
      shift
      [ $# -gt 0 ] || usage 1
      group="$1"
      shift
      run_ci_shard normal "$group" "${1:-${CI_NORMAL_SPLITS:-15}}"
      ;;
    --ci-worker)
      shift
      [ $# -gt 0 ] || usage 1
      group="$1"
      shift
      run_ci_shard worker "$group" "${1:-${CI_WORKER_SPLITS:-1}}"
      ;;
    --all)  MODE="all"; shift ;;
    --changed) CHANGED=1; shift ;;
    -m)     shift; [ $# -gt 0 ] || usage 1; MODULES+=("$1"); shift ;;
    --)     shift; RAW=("$@"); break ;;
    *)      echo "未知参数: $1" >&2; usage 1 ;;
  esac
done

# 组装 pytest 参数
if [ "$CHANGED" -eq 1 ]; then
  # testmon 与 -m 组合会主动退化为全量,no:cacheprovider 又会令其缺少 --lf 状态.
  # fuzz 用例仅此一文件,用路径排除保留增量选择.
  ARGS=(pytest --ignore=tests/integration --testmon --ignore=tests/test_openapi_fuzz.py)
elif [ "$MODE" = "all" ]; then
  ARGS=(pytest -p no:cacheprovider --ignore=tests/integration)
  # -n auto 只给全量:xdist 每 worker 要重导入整个 app,启动开销只有大用例集才摊得回;
  #   单模块 -m 约 100 用例时单进程更快,实测 -n auto 让 107 用例 pytest 3.4s -> 6.4s,负优化.
  ARGS+=(-m 'not fuzz' -n auto
         --cov=shared --cov=api --cov=scheduler --cov=worker --cov=steps
         --cov-branch --cov-report=term-missing --cov-fail-under=75)
else
  ARGS=(pytest -p no:cacheprovider --ignore=tests/integration)
  ARGS+=(-m 'not fuzz')
fi
# 单模块 -m / 透传:默认单进程(小集最快,不加 -n auto)
for mod in "${MODULES[@]}"; do
  ARGS+=(tests/test_"${mod}"*.py)      # host glob(cd $REPO)→ 展开成实文件,与容器 /app/tests 对齐
done
[ ${#RAW[@]} -gt 0 ] && ARGS+=("${RAW[@]}")

ensure_warm
echo ">> docker exec $WARM ${ARGS[*]}" >&2
exec docker exec "$WARM" "${ARGS[@]}"
