#!/usr/bin/env bash
# 在一个 runner 内并行构建候选或提升四个产品镜像.
set -euo pipefail

MODE="${1:-}"
BACKEND="${2:-false}"
FRONTEND="${3:-false}"
case "$MODE" in
  check|candidate|promote) ;;
  *) echo "usage: scripts/ci-images.sh <check|candidate|promote> <backend:true|false> <frontend:true|false>" >&2; exit 2 ;;
esac
case "$BACKEND:$FRONTEND" in
  true:true|true:false|false:true|false:false) ;;
  *) echo "backend/frontend 必须是 true 或 false" >&2; exit 2 ;;
esac

: "${OWNER_LC:?OWNER_LC is required}"
: "${GITHUB_SHA:?GITHUB_SHA is required}"
[[ "$GITHUB_SHA" =~ ^[0-9a-f]{40}$ ]] || {
  echo "GITHUB_SHA 必须是 40 位小写十六进制" >&2
  exit 2
}
if [ "$MODE" != "check" ]; then
  : "${CI_IMAGE_DIGEST_FILE:?CI_IMAGE_DIGEST_FILE is required}"
fi
if [ "$MODE" = "candidate" ] || [ "$MODE" = "check" ]; then
  : "${FLORI_VERSION:?FLORI_VERSION is required}"
fi
if [ "$MODE" = "candidate" ] || [ "$MODE" = "promote" ]; then
  [ "${GITHUB_REF:-}" = "refs/heads/main" ] || {
    echo "$MODE 仅允许在 main 执行" >&2
    exit 2
  }
fi

RUN_TMP="${RUNNER_TEMP:-${TMPDIR:-/tmp}}/flori-ci-images-${MODE}-$$"
mkdir -p "$RUN_TMP"
PIDS=()
NAMES=()
METADATA=()

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

candidate_digest() {
  image="$1"
  matches=$(awk -F '\t' -v image="$image" '$1 == image { count += 1; digest = $2 } END { print count + 0, digest }' "$CI_IMAGE_DIGEST_FILE")
  count="${matches%% *}"
  digest="${matches#* }"
  if [ "$count" != "1" ] || ! [[ "$digest" =~ ^sha256:[0-9a-f]{64}$ ]]; then
    echo "$image 的候选 digest 缺失、重复或格式无效" >&2
    return 1
  fi
  printf '%s' "$digest"
}

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

  candidate="ghcr.io/$OWNER_LC/$image:candidate-$GITHUB_SHA"
  if [ "$MODE" = "candidate" ] || [ "$MODE" = "check" ]; then
    metadata="$RUN_TMP/$image.metadata.json"
    command=(
      docker buildx build
      --file "$dockerfile"
      --platform linux/amd64
      --build-arg USE_USTC_MIRROR=0
      --build-arg "FLORI_BUILD_SHA=$GITHUB_SHA"
      --build-arg "FLORI_VERSION=$FLORI_VERSION"
      --cache-from "type=registry,ref=ghcr.io/$OWNER_LC/$image:buildcache"
    )
    if [ "$MODE" = "candidate" ]; then
      command+=(
        --cache-to "type=registry,ref=ghcr.io/$OWNER_LC/$image:buildcache,mode=max"
        --metadata-file "$metadata"
        --push
        --tag "$candidate"
      )
    fi
    [ -z "$target" ] || command+=(--target "$target")
    command+=("$context")
  else
    digest=$(candidate_digest "$image") || return 1
    command=(
      docker buildx imagetools create
      --tag "ghcr.io/$OWNER_LC/$image:latest"
      --tag "ghcr.io/$OWNER_LC/$image:sha-${GITHUB_SHA:0:7}"
      "ghcr.io/$OWNER_LC/$image@$digest"
    )
  fi

  log="$RUN_TMP/$image.log"
  if [ "$MODE" = "promote" ]; then
    (
      for attempt in 1 2 3; do
        if "${command[@]}"; then
          exit 0
        fi
        echo "$image promote attempt $attempt failed" >&2
        [ "$attempt" -eq 3 ] || sleep "$attempt"
      done
      exit 1
    ) >"$log" 2>&1 &
  else
    "${command[@]}" >"$log" 2>&1 &
  fi
  PIDS+=("$!")
  NAMES+=("$image")
  [ "$MODE" != "candidate" ] || METADATA+=("$metadata")
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

if [ "$MODE" = "candidate" ] && [ "$failed" -eq 0 ]; then
  manifest="$RUN_TMP/candidate-digests.tsv"
  : > "$manifest"
  for index in "${!NAMES[@]}"; do
    image="${NAMES[$index]}"
    metadata="${METADATA[$index]}"
    digest=$(python3 - "$metadata" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    print(json.load(handle).get("containerimage.digest", ""))
PY
)
    if ! [[ "$digest" =~ ^sha256:[0-9a-f]{64}$ ]]; then
      echo "$image 未产出有效的 immutable digest" >&2
      failed=1
      continue
    fi
    printf '%s\t%s\n' "$image" "$digest" >> "$manifest"
  done
  if [ "$failed" -eq 0 ]; then
    mkdir -p "$(dirname "$CI_IMAGE_DIGEST_FILE")"
    mv "$manifest" "$CI_IMAGE_DIGEST_FILE"
  fi
fi
exit "$failed"
