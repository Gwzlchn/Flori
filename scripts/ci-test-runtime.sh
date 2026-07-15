#!/usr/bin/env bash
# 为 main 复用或构建无源码测试运行时,输出不可变 digest 供测试 runner 使用.
set -euo pipefail

MODE="${1:-build}"
: "${OWNER_LC:?OWNER_LC is required}"
[ "${GITHUB_REF:-}" = "refs/heads/main" ] || {
  echo "测试运行时仅允许在 main 发布" >&2
  exit 2
}
case "$MODE" in
  probe|build|tag|pull) ;;
  *) echo "未知测试运行时模式: $MODE" >&2; exit 2 ;;
esac

# 版本值不会改变依赖集合. 输入摘要让普通源码提交直接复用既有 runtime tag.
RUNTIME_KEY="$({
  sha256sum docker/base.Dockerfile
  sed 's/^version = .*/version = "0.0.0"/' pyproject.toml | sha256sum
} | sha256sum | cut -c1-32)"

runtime_name() {
  case "$1" in
    normal) printf 'flori-test' ;;
    worker) printf 'flori-test-worker' ;;
    *) echo "未知测试运行时类型: $1" >&2; return 2 ;;
  esac
}

runtime_tag() {
  printf 'ghcr.io/%s/%s:runtime-%s' "$OWNER_LC" "$1" "$RUNTIME_KEY"
}

if [ "$MODE" = "tag" ]; then
  [ $# -eq 2 ] || { echo "用法: $0 tag normal|worker" >&2; exit 2; }
  runtime_tag "$(runtime_name "$2")"
  exit 0
fi

if [ "$MODE" = "pull" ]; then
  [ $# -eq 3 ] || { echo "用法: $0 pull normal|worker local-tag" >&2; exit 2; }
  name="$(runtime_name "$2")"
  tag="$(runtime_tag "$name")"
  attempts="${CI_RUNTIME_PULL_ATTEMPTS:-36}"
  delay="${CI_RUNTIME_PULL_DELAY:-5}"
  [[ "$attempts" =~ ^[1-9][0-9]*$ ]] || { echo "CI_RUNTIME_PULL_ATTEMPTS 必须是正整数" >&2; exit 2; }
  [[ "$delay" =~ ^[0-9]+$ ]] || { echo "CI_RUNTIME_PULL_DELAY 必须是非负整数" >&2; exit 2; }

  pulled=false
  for ((attempt = 1; attempt <= attempts; attempt++)); do
    if docker pull "$tag"; then
      pulled=true
      break
    fi
    if [ "$attempt" -lt "$attempts" ]; then
      echo "共享测试运行时尚未就绪: $name ($attempt/$attempts)" >&2
      sleep "$delay"
    fi
  done
  [ "$pulled" = true ] || {
    echo "等待共享测试运行时超时: $tag" >&2
    exit 1
  }

  repo="ghcr.io/$OWNER_LC/$name"
  digest_ref=""
  while IFS= read -r candidate; do
    case "$candidate" in
      "$repo"@sha256:*)
        digest="${candidate#"$repo"@}"
        if [[ "$digest" =~ ^sha256:[0-9a-f]{64}$ ]]; then
          digest_ref="$repo@$digest"
          break
        fi
        ;;
    esac
  done < <(docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "$tag")
  [ -n "$digest_ref" ] || {
    echo "共享测试运行时缺少有效 RepoDigest: $tag" >&2
    exit 1
  }
  docker tag "$digest_ref" "$3"
  echo "共享测试运行时固定到: $digest_ref"
  exit 0
fi

: "${GITHUB_OUTPUT:?GITHUB_OUTPUT is required}"
RUN_TMP="${RUNNER_TEMP:-${TMPDIR:-/tmp}}/flori-ci-test-runtime-$$"
mkdir -p "$RUN_TMP"
PIDS=()
NAMES=()

cleanup() {
  status=$?
  trap - EXIT INT TERM
  for pid in "${PIDS[@]}"; do
    kill "$pid" >/dev/null 2>&1 || true
  done
  for pid in "${PIDS[@]}"; do
    wait "$pid" >/dev/null 2>&1 || true
  done
  rm -rf -- "$RUN_TMP"
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

inspect_runtime() {
  name="$1"
  tag="$(runtime_tag "$name")"
  manifest="$(docker buildx imagetools inspect "$tag" --format '{{json .Manifest}}' 2>/dev/null)" || return 1
  digest="$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("digest", ""))' <<<"$manifest")"
  [[ "$digest" =~ ^sha256:[0-9a-f]{64}$ ]] || return 1
  printf 'ghcr.io/%s/%s@%s' "$OWNER_LC" "$name" "$digest"
}

normal_ref="$(inspect_runtime flori-test || true)"
worker_ref="$(inspect_runtime flori-test-worker || true)"
if [ -n "$normal_ref" ] && [ -n "$worker_ref" ]; then
  printf 'ready=true\nnormal=%s\nworker=%s\n' \
    "$normal_ref" "$worker_ref" >> "$GITHUB_OUTPUT"
  echo "共享测试运行时命中: $RUNTIME_KEY"
  exit 0
fi

if [ "$MODE" = "probe" ]; then
  printf 'ready=false\n' >> "$GITHUB_OUTPUT"
  echo "共享测试运行时未命中: $RUNTIME_KEY"
  exit 0
fi

start_runtime() {
  name="$1"
  target="$2"
  cache="$3"
  shift 3
  metadata="$RUN_TMP/$name.metadata.json"
  tag="$(runtime_tag "$name")"
  cache_from=(--cache-from "type=registry,ref=ghcr.io/$OWNER_LC/$cache:buildcache")
  for extra_cache in "$@"; do
    cache_from+=(--cache-from "type=registry,ref=ghcr.io/$OWNER_LC/$extra_cache:buildcache")
  done
  docker buildx build \
    --file docker/base.Dockerfile \
    --target "$target" \
    --platform linux/amd64 \
    --build-arg USE_USTC_MIRROR=0 \
    "${cache_from[@]}" \
    --cache-to "type=registry,ref=ghcr.io/$OWNER_LC/$cache:buildcache,mode=max" \
    --metadata-file "$metadata" \
    --push --tag "$tag" . >"$RUN_TMP/$name.log" 2>&1 &
  PIDS+=("$!")
  NAMES+=("$name")
}

[ -n "$normal_ref" ] || start_runtime flori-test test-runtime flori-test
[ -n "$worker_ref" ] || start_runtime \
  flori-test-worker test-worker-runtime flori-test-worker flori-test

failed=0
for index in "${!PIDS[@]}"; do
  pid="${PIDS[$index]}"
  name="${NAMES[$index]}"
  if wait "$pid"; then
    echo "== $name build success =="
  else
    echo "== $name build failed ==" >&2
    failed=1
  fi
  tail -n 240 "$RUN_TMP/$name.log"
done
[ "$failed" -eq 0 ] || exit "$failed"

normal_ref="$(inspect_runtime flori-test || true)"
worker_ref="$(inspect_runtime flori-test-worker || true)"
if [ -z "$normal_ref" ] || [ -z "$worker_ref" ]; then
  echo "测试运行时构建后未产出有效的 immutable digest" >&2
  exit 1
fi
printf 'ready=true\nnormal=%s\nworker=%s\n' \
  "$normal_ref" "$worker_ref" >> "$GITHUB_OUTPUT"
