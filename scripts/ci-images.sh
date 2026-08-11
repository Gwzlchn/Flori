#!/usr/bin/env bash
# 在一个 runner 内并行构建候选或提升 scheduler/api、五种 Worker 与 frontend 产品镜像。
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
declare -A CLI_VERSIONS=()
CLI_TOOLS_KEY=""
CLI_TOOLS_SOURCE_DIGEST=""
CLI_TOOLS_REF=""
CLI_TOOLS_DIGEST=""
CLI_TOOLS_AVAILABLE=false

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

resolve_cli_channels() {
  local output cli matches count version status
  if output=$(timeout --foreground --kill-after=30s 1800 \
      sh docker/install-cli-tools.sh resolve); then
    :
  else
    status=$?
    if [ "$status" -eq 124 ] || [ "$status" -eq 137 ]; then
      echo "官方 CLI channel 解析超过 1800 秒总预算" >&2
    else
      echo "官方 CLI channel 解析失败(exit=$status)" >&2
    fi
    return "$status"
  fi
  for cli in claude qoder codex; do
    matches=$(printf '%s\n' "$output" | awk -v cli="$cli" \
      '$0 ~ "^FLORI_CLI_CHANNEL " cli "=" { count += 1; value = substr($0, index($0, "=") + 1) } END { print count + 0, value }')
    count="${matches%% *}"
    version="${matches#* }"
    if [ "$count" != "1" ] || ! [[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+([-+][0-9A-Za-z.-]+)?$ ]]; then
      echo "$cli 官方 channel 版本缺失、重复或格式无效" >&2
      return 1
    fi
    CLI_VERSIONS[$cli]="$version"
    echo "FLORI_CLI_CHANNEL $cli=$version"
  done
  CLI_TOOLS_KEY=$(printf 'claude=%s\nqoder=%s\ncodex=%s\nsource=%s\n' \
    "${CLI_VERSIONS[claude]}" "${CLI_VERSIONS[qoder]}" "${CLI_VERSIONS[codex]}" \
    "$CLI_TOOLS_SOURCE_DIGEST" \
    | sha256sum | cut -d ' ' -f1)
  CLI_TOOLS_REF="ghcr.io/$OWNER_LC/flori-cli-tools:versions-$CLI_TOOLS_KEY"
}

resolve_cli_source_digest() {
  local stage_file="$RUN_TMP/cli-tools-docker-stage"
  local installer_digest stage_digest combined
  if ! awk '
    $0 == "FROM python:3.11-slim AS cli-tools-builder" { active = 1; starts += 1 }
    active { print }
    $0 == "FROM ${CLI_TOOLS_IMAGE} AS cli-tools-source" {
      active = 0
      ends += 1
    }
    END {
      if (active || starts != 1 || ends != 1) exit 2
    }
  ' docker/base.Dockerfile > "$stage_file"; then
    echo "无法唯一提取 cli-tools Docker stage 输入" >&2
    return 1
  fi
  installer_digest=$(sha256sum docker/install-cli-tools.sh | cut -d ' ' -f1)
  stage_digest=$(sha256sum "$stage_file" | cut -d ' ' -f1)
  combined=$(printf 'installer=%s\ndocker-stage=%s\n' \
    "$installer_digest" "$stage_digest" | sha256sum | cut -d ' ' -f1)
  [[ "$combined" =~ ^[0-9a-f]{64}$ ]] || {
    echo "cli-tools source digest 无效" >&2
    return 1
  }
  CLI_TOOLS_SOURCE_DIGEST="sha256:$combined"
  echo "FLORI_CLI_SOURCE $CLI_TOOLS_SOURCE_DIGEST"
}

validate_cli_tools_inspect() {
  local inspect_file="$1"
  python3 - "$inspect_file" \
    "${CLI_VERSIONS[claude]}" "${CLI_VERSIONS[qoder]}" "${CLI_VERSIONS[codex]}" \
    "$CLI_TOOLS_SOURCE_DIGEST" <<'PY'
import json
import re
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    data = json.load(handle)
digest = data.get("manifest", {}).get("digest", "")
if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
    raise SystemExit("cli-tools manifest digest 无效")
labels = data.get("image", {}).get("config", {}).get("Labels", {}) or {}
expected = {
    "io.flori.cli.claude": sys.argv[2],
    "io.flori.cli.qoder": sys.argv[3],
    "io.flori.cli.codex": sys.argv[4],
    "io.flori.cli.source": sys.argv[5],
}
if any(labels.get(key) != value for key, value in expected.items()):
    raise SystemExit("cli-tools 镜像标签与官方 channel/source 输入不一致")
print(digest)
PY
}

inspect_cli_tools() {
  local ref="$1"
  local inspect_file="$RUN_TMP/cli-tools-inspect.json"
  local error_file="$RUN_TMP/cli-tools-inspect.err"
  local attempt
  for attempt in 1 2 3; do
    if docker buildx imagetools inspect "$ref" --format '{{json .}}' \
        >"$inspect_file" 2>"$error_file"; then
      CLI_TOOLS_DIGEST=$(validate_cli_tools_inspect "$inspect_file") || return 1
      CLI_TOOLS_AVAILABLE=true
      return 0
    fi
    if grep -qiE 'not found|manifest unknown|does not exist' "$error_file"; then
      CLI_TOOLS_AVAILABLE=false
      return 0
    fi
    [ "$attempt" -eq 3 ] || sleep "$attempt"
  done
  cat "$error_file" >&2
  echo "无法判定 cli-tools 稳定镜像是否存在" >&2
  return 1
}

validate_cli_build_log() {
  local log="$1"
  local invalid=0
  local cli expected evidence count
  for cli in claude qoder codex; do
    expected="${CLI_VERSIONS[$cli]}"
    evidence=$(grep -Eo "FLORI_CLI_VERSION ${cli}=[0-9A-Za-z.+-]+" "$log" || true)
    count=$(printf '%s\n' "$evidence" | awk 'NF { count += 1 } END { print count + 0 }')
    if [ "$count" != "1" ] || [ "$evidence" != "FLORI_CLI_VERSION $cli=$expected" ]; then
      echo "cli-tools 的 $cli 实装版本证据缺失、重复或与 channel 不一致" >&2
      invalid=1
    fi
  done
  return "$invalid"
}

build_cli_tools() {
  local metadata="$RUN_TMP/cli-tools.metadata.json"
  local log="$RUN_TMP/cli-tools.log"
  if ! docker buildx build \
      --file docker/base.Dockerfile \
      --platform linux/amd64 \
      --target cli-tools \
      --no-cache-filter cli-tools-builder \
      --build-arg USE_USTC_MIRROR=0 \
      --build-arg "CLI_INSTALL_REFRESH=$CLI_TOOLS_KEY" \
      --build-arg "CLI_TOOLS_SOURCE_DIGEST=$CLI_TOOLS_SOURCE_DIGEST" \
      --build-arg "CLAUDE_CLI_VERSION=${CLI_VERSIONS[claude]}" \
      --build-arg "QODER_CLI_VERSION=${CLI_VERSIONS[qoder]}" \
      --build-arg "CODEX_CLI_VERSION=${CLI_VERSIONS[codex]}" \
      --cache-from "type=registry,ref=ghcr.io/$OWNER_LC/flori-worker-ai-qoder:buildcache" \
      --cache-from "type=registry,ref=ghcr.io/$OWNER_LC/flori-worker:buildcache" \
      --cache-to type=inline \
      --metadata-file "$metadata" \
      --provenance=false \
      --push \
      --tag "$CLI_TOOLS_REF" \
      . >"$log" 2>&1; then
    tail -n 240 "$log" >&2
    return 1
  fi
  validate_cli_build_log "$log" || { tail -n 240 "$log" >&2; return 1; }
  CLI_TOOLS_DIGEST=$(python3 - "$metadata" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    print(json.load(handle).get("containerimage.digest", ""))
PY
)
  if ! [[ "$CLI_TOOLS_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]]; then
    echo "cli-tools 未产出有效的 immutable digest" >&2
    return 1
  fi
  inspect_cli_tools "$CLI_TOOLS_REF@$CLI_TOOLS_DIGEST"
}

prepare_cli_tools() {
  [ "$BACKEND" = "true" ] || return 0
  [ "$MODE" = "candidate" ] || [ "$MODE" = "check" ] || return 0
  resolve_cli_source_digest
  resolve_cli_channels
  inspect_cli_tools "$CLI_TOOLS_REF"
  if [ "$CLI_TOOLS_AVAILABLE" != "true" ] && [ "$MODE" = "candidate" ]; then
    build_cli_tools
  fi
  if [ "$CLI_TOOLS_AVAILABLE" = "true" ]; then
    echo "FLORI_CLI_BASE $CLI_TOOLS_REF@$CLI_TOOLS_DIGEST"
  else
    echo "cli-tools 稳定镜像尚不存在;本次只读构建将实装并校验官方版本"
  fi
}

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
    if [[ "$image" == flori-worker-ai-* ]]; then
      command+=(
        --build-arg "CLI_INSTALL_REFRESH=$CLI_TOOLS_KEY"
        --build-arg "CLI_TOOLS_SOURCE_DIGEST=$CLI_TOOLS_SOURCE_DIGEST"
        --build-arg "CLAUDE_CLI_VERSION=${CLI_VERSIONS[claude]}"
        --build-arg "QODER_CLI_VERSION=${CLI_VERSIONS[qoder]}"
        --build-arg "CODEX_CLI_VERSION=${CLI_VERSIONS[codex]}"
      )
      if [ "$CLI_TOOLS_AVAILABLE" = "true" ]; then
        command+=(--build-arg "CLI_TOOLS_IMAGE=$CLI_TOOLS_REF@$CLI_TOOLS_DIGEST")
      fi
    fi
    if [[ "$image" == flori-worker-* ]]; then
      command+=(--cache-from "type=registry,ref=ghcr.io/$OWNER_LC/flori-worker:buildcache")
    fi
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

prepare_cli_tools
start_build flori-scheduler docker/base.Dockerfile . scheduler "$BACKEND"
start_build flori-api docker/base.Dockerfile . api "$BACKEND"
start_build flori-worker-cpu docker/base.Dockerfile . worker-cpu "$BACKEND"
start_build flori-worker-gpu docker/base.Dockerfile . worker-gpu "$BACKEND"
start_build flori-worker-ai-claude docker/base.Dockerfile . worker-ai-claude "$BACKEND"
start_build flori-worker-ai-qoder docker/base.Dockerfile . worker-ai-qoder "$BACKEND"
start_build flori-worker-ai-codex docker/base.Dockerfile . worker-ai-codex "$BACKEND"
start_build flori-frontend frontend/Dockerfile ./frontend "" "$FRONTEND"

failed=0
cli_log_validated=false
for index in "${!PIDS[@]}"; do
  pid="${PIDS[$index]}"
  image="${NAMES[$index]}"
  if wait "$pid"; then
    echo "== $image $MODE success =="
    if [[ "$image" == flori-worker-ai-* ]] \
        && { [ "$MODE" = "candidate" ] || [ "$MODE" = "check" ]; }; then
      marker_count=$(grep -Fc "FLORI_CLI_VERSION " "$RUN_TMP/$image.log" || true)
      if [ "$marker_count" != "0" ]; then
        if ! validate_cli_build_log "$RUN_TMP/$image.log"; then
          failed=1
        else
          cli_log_validated=true
        fi
      fi
    fi
  else
    echo "== $image $MODE failed ==" >&2
    failed=1
  fi
  tail -n 240 "$RUN_TMP/$image.log"
done

if [ "$BACKEND" = "true" ] \
    && { [ "$MODE" = "candidate" ] || [ "$MODE" = "check" ]; } \
    && [ "$CLI_TOOLS_AVAILABLE" != "true" ] \
    && [ "$cli_log_validated" != "true" ]; then
  validate_cli_build_log /dev/null || true
  echo "内部 cli-tools 构建未提供三种 CLI 的实装版本证据" >&2
  failed=1
fi

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
    if [ "$BACKEND" = "true" ]; then
      {
        printf 'claude\t%s\n' "${CLI_VERSIONS[claude]}"
        printf 'qoder\t%s\n' "${CLI_VERSIONS[qoder]}"
        printf 'codex\t%s\n' "${CLI_VERSIONS[codex]}"
        printf 'source\t%s\n' "$CLI_TOOLS_SOURCE_DIGEST"
        printf 'cli-tools\t%s@%s\n' "$CLI_TOOLS_REF" "$CLI_TOOLS_DIGEST"
      } > "$(dirname "$CI_IMAGE_DIGEST_FILE")/cli-tools.tsv"
    fi
  fi
fi
exit "$failed"
