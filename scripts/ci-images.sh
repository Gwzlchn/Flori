#!/usr/bin/env bash
# 在一个 runner 内并行预热或发布四个产品镜像。
set -euo pipefail

MODE="${1:-}"
BACKEND="${2:-false}"
FRONTEND="${3:-false}"
case "$MODE" in
  warm|push) ;;
  *) echo "usage: scripts/ci-images.sh <warm|push> <backend:true|false> <frontend:true|false>" >&2; exit 2 ;;
esac
case "$BACKEND:$FRONTEND" in
  true:true|true:false|false:true|false:false) ;;
  *) echo "backend/frontend 必须是 true 或 false" >&2; exit 2 ;;
esac

: "${OWNER_LC:?OWNER_LC is required}"
: "${FLORI_VERSION:?FLORI_VERSION is required}"
: "${GITHUB_SHA:?GITHUB_SHA is required}"

RUN_TMP="${RUNNER_TEMP:-${TMPDIR:-/tmp}}/flori-ci-images-${MODE}-$$"
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
trap cleanup EXIT INT TERM

start_build() {
  image="$1"
  dockerfile="$2"
  context="$3"
  target="$4"
  want="$5"
  if [ "$want" != "true" ]; then
    echo "跳过 $image(无相关运行时变化)"
    return
  fi

  command=(
    docker buildx build
    --file "$dockerfile"
    --platform linux/amd64
    --build-arg USE_USTC_MIRROR=0
    --build-arg "FLORI_BUILD_SHA=$GITHUB_SHA"
    --build-arg "FLORI_VERSION=$FLORI_VERSION"
    --cache-from "type=registry,ref=ghcr.io/$OWNER_LC/$image:buildcache"
  )
  [ -z "$target" ] || command+=(--target "$target")
  if [ "$MODE" = "warm" ]; then
    if [ "${GITHUB_REF:-}" = "refs/heads/main" ]; then
      command+=(--cache-to "type=registry,ref=ghcr.io/$OWNER_LC/$image:buildcache,mode=max")
    fi
  else
    command+=(
      --push
      --tag "ghcr.io/$OWNER_LC/$image:latest"
      --tag "ghcr.io/$OWNER_LC/$image:sha-${GITHUB_SHA:0:7}"
    )
  fi
  command+=("$context")

  log="$RUN_TMP/$image.log"
  "${command[@]}" >"$log" 2>&1 &
  PIDS+=("$!")
  NAMES+=("$image")
}

start_build flori-scheduler docker/base.Dockerfile . scheduler "$BACKEND"
start_build flori-api docker/base.Dockerfile . api "$BACKEND"
start_build flori-worker docker/base.Dockerfile . worker "$BACKEND"
start_build flori-frontend frontend/Dockerfile ./frontend "" "$FRONTEND"

failed=0
for index in "${!PIDS[@]}"; do
  pid="${PIDS[$index]}"
  image="${NAMES[$index]}"
  if wait "$pid"; then
    echo "== $image $MODE success =="
  else
    echo "== $image $MODE failed ==" >&2
    failed=1
  fi
  tail -n 240 "$RUN_TMP/$image.log"
done
exit "$failed"
