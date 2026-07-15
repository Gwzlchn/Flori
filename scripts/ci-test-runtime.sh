#!/usr/bin/env bash
# 为 main 复用或构建无源码测试运行时,输出不可变 digest 供测试 runner 使用.
set -euo pipefail

MODE="${1:-build}"
: "${OWNER_LC:?OWNER_LC is required}"
: "${GITHUB_OUTPUT:?GITHUB_OUTPUT is required}"
[ "${GITHUB_REF:-}" = "refs/heads/main" ] || {
  echo "测试运行时仅允许在 main 发布" >&2
  exit 2
}
case "$MODE" in
  probe|build) ;;
  *) echo "未知测试运行时模式: $MODE" >&2; exit 2 ;;
esac

# 版本值不会改变依赖集合. 输入摘要让普通源码提交直接复用既有 runtime tag.
RUNTIME_KEY="$({
  sha256sum docker/base.Dockerfile
  sed 's/^version = .*/version = "0.0.0"/' pyproject.toml | sha256sum
} | sha256sum | cut -c1-32)"
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

runtime_tag() {
  printf 'ghcr.io/%s/%s:runtime-%s' "$OWNER_LC" "$1" "$RUNTIME_KEY"
}

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
