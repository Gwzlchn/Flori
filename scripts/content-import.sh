#!/usr/bin/env bash
# Flori 便携内容导入入口(portable content repository -> 空库重建,设计稿 05 号 §2.9/§2.10)。
# 两种模式:--target empty 把便携仓库物化到全新 SQLite + 对象存储;
# --target merge 把快照里的 Job 补进已有库(按 §2.9 七条规则分类,冲突单元零修改)。
# 两者的状态都由当前 pipeline 重投影。它不是灾备回滚——回滚仍用 scripts/restore.sh。
#
# 全容器内运行(宿主不装 Python 依赖),复用 flori 应用镜像里的 shared/ 包。
# 路径按容器内视图传参:数据卷统一挂到 /data(与 docker-compose.yml 一致)。
#
# 用法:
#   scripts/content-import.sh --repo <dir> --db <目标库> [--snapshot latest|sha256:…]
#       [--data-dir <dir>] [--jobs-dir <dir>] [--object-bucket <桶>] [--into-live]
#       [--journal <file>] [--target-generation <id>] [--target empty|merge]
#       [--config-root </data/...>] [--source-root <id>=<宿主目录>]
#       [--apply-user-state] [--allow-partial] [--allow-incomplete-portable-snapshot]
#       [--result-file <json>]
#   scripts/content-import.sh --repo <dir> --db <目标库> --plan       # 只出计划
#   scripts/content-import.sh --repo <dir> --db <目标库> --verify-only # 只跑全链校验
#
# `--db/--jobs-dir/--config-root/--journal` 是容器内 `/data` 视图;
# `--repo/--result-file/--source-root` 是宿主路径,由脚本显式挂载。
# 隔离与放行看目标身份,不看开关:目标解析到线上库 /data/db/analyzer.db、线上产物根
# /data/jobs,或(设了 MINIO_URL 时)生产桶,都算写线上面,必须显式 --into-live,
# 并通过 API/scheduler/本地 worker 共享锁已释放 + FLORI_REMOTE_WORKERS_QUIESCED=1 +
# FLORI_DR_RECEIPT 指向够新的 exact DR result JSON(内容会被解析校验)三道门。
# 对象存储模式下 jobs-dir 不构成隔离:隔离必须靠 --object-bucket <与生产桶不同的桶>。
# 普通导入不会自动 enqueue,恢复后需显式 resume/resubmit(见 docs/08-deployment.md §8.2)。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
source "$SCRIPT_DIR/lib/content-result.sh"

IMAGE="${FLORI_CONTENT_IMPORT_IMAGE:-flori:${IMAGE_TAG:-uptest}}"
DATA_VOLUME="${FLORI_DATA_VOLUME:-flori-data}"
CONTAINER_DATA="/data"
LIVE_DB_PATH="${FLORI_LIVE_DB_PATH:-/data/db/analyzer.db}"
LIVE_JOBS_DIR="${FLORI_LIVE_JOBS_DIR:-/data/jobs}"
LIVE_CONFIG_ROOT="${FLORI_LIVE_CONFIG_ROOT:-/data/prompts}"

# 早退也必须留下机器可读结果:自动化按 --result-file 判生死,shell 直接 exit
# 会让调用方读到上一次的陈旧 JSON 或什么都读不到。参数错(usage)同样受这条约束,
# 所以两条早退路径共用它。
write_failure_result() {
  content_write_failure_result "import" "$1"
}

# 只打印文件头的注释块;早期版本写死行号,把 set -euo pipefail 一起当帮助printf 出来,
# 同时把最后一行说明截断掉。
usage() {
  awk 'NR > 1 { if ($0 !~ /^#/) exit; sub(/^#[[:space:]]?/, ""); print }' "$0"
  local code="${1:-0}"
  [ "$code" -eq 0 ] || write_failure_result "参数错误:用法见 --help"
  exit "$code"
}

fail() {
  local code="$1"; shift
  local message="$*"
  echo "错误: $message" >&2
  write_failure_result "$message"
  exit "$code"
}

# --result-file 先于主解析扫一遍:参数错发生在解析途中,等轮到它才赋值就太晚了。
RESULT_FILE=""
PRESCAN_REPO=""
PRESCAN_DATA_DIR="${FLORI_DATA_DIR:-}"
PRESCAN_SOURCE_ROOTS=()
PRESCAN_PROTECTED_FILES=()
[ -z "${FLORI_DR_RECEIPT:-}" ] || PRESCAN_PROTECTED_FILES+=("$FLORI_DR_RECEIPT")
for _index in $(seq 1 $#); do
  if [ "${!_index}" = "--result-file" ]; then
    _next=$((_index + 1))
    [ "$_next" -le $# ] && RESULT_FILE="${!_next}"
  fi
  if [ "${!_index}" = "--repo" ]; then
    _next=$((_index + 1))
    [ "$_next" -le $# ] && PRESCAN_REPO="${!_next}"
  fi
  if [ "${!_index}" = "--data-dir" ]; then
    _next=$((_index + 1)); [ "$_next" -le $# ] && PRESCAN_DATA_DIR="${!_next}"
  fi
  if [ "${!_index}" = "--source-root" ]; then
    _next=$((_index + 1))
    if [ "$_next" -le $# ]; then
      _value="${!_next}"
      [ "${_value#*=}" = "$_value" ] || PRESCAN_SOURCE_ROOTS+=("${_value#*=}")
    fi
  fi
done
CONTENT_RESULT_PROTECTED_ROOTS=(
  "$PRESCAN_DATA_DIR" "${PRESCAN_SOURCE_ROOTS[@]}"
)
if ! content_validate_result_boundary "$PRESCAN_REPO" "$RESULT_FILE"; then
  unsafe_result="$RESULT_FILE"; RESULT_FILE=""
  fail 2 "--result-file 必须在便携仓库之外且路径不能含符号链接: $unsafe_result"
fi
PRESCAN_TARGET_ROOTS=()
[ -z "$PRESCAN_DATA_DIR" ] || PRESCAN_TARGET_ROOTS+=("$PRESCAN_DATA_DIR")
[ ${#PRESCAN_SOURCE_ROOTS[@]} -eq 0 ] || PRESCAN_TARGET_ROOTS+=("${PRESCAN_SOURCE_ROOTS[@]}")
for protected in "${PRESCAN_TARGET_ROOTS[@]}"; do
  if [ -n "$RESULT_FILE" ] && content_paths_overlap "$protected" "$RESULT_FILE"; then
    unsafe_result="$RESULT_FILE"; RESULT_FILE=""
    fail 2 "--result-file 不得位于数据或来源目标根内: $unsafe_result"
  fi
  if [ -n "$PRESCAN_REPO" ] && content_paths_overlap "$protected" "$PRESCAN_REPO"; then
    fail 2 "数据或来源目标根不得与便携仓库重叠: $PRESCAN_REPO"
  fi
done
for protected in "${PRESCAN_PROTECTED_FILES[@]}"; do
  if [ -n "$RESULT_FILE" ] && content_paths_overlap "$protected" "$RESULT_FILE"; then
    unsafe_result="$RESULT_FILE"; RESULT_FILE=""
    fail 2 "--result-file 不得覆盖exact DR回执: $unsafe_result"
  fi
done

REPO_DIR=""
DATA_DIR="${FLORI_DATA_DIR:-}"
DB_PATH=""
JOBS_DIR=""
CONFIG_ROOT=""
OBJECT_BUCKET=""
JOURNAL="/data/content-import/journal.sqlite3"
INTO_LIVE=0
SNAPSHOT="latest"
GENERATION=""
PLAN=0
VERIFY_ONLY=0
ALLOW_PARTIAL=0
TARGET_MODE="empty"
APPLY_USER_STATE=0
SKIP_INDEX=0
ALLOW_INCOMPLETE_PORTABLE=0
SOURCE_ROOT_SPECS=()

while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help) usage 0 ;;
    --repo) shift; [ $# -gt 0 ] || usage 2; REPO_DIR="$1"; shift ;;
    --db) shift; [ $# -gt 0 ] || usage 2; DB_PATH="$1"; shift ;;
    --data-dir) shift; [ $# -gt 0 ] || usage 2; DATA_DIR="$1"; shift ;;
    --jobs-dir) shift; [ $# -gt 0 ] || usage 2; JOBS_DIR="$1"; shift ;;
    --config-root) shift; [ $# -gt 0 ] || usage 2; CONFIG_ROOT="$1"; shift ;;
    --source-root) shift; [ $# -gt 0 ] || usage 2; SOURCE_ROOT_SPECS+=("$1"); shift ;;
    --object-bucket) shift; [ $# -gt 0 ] || usage 2; OBJECT_BUCKET="$1"; shift ;;
    --into-live) INTO_LIVE=1; shift ;;
    --journal) shift; [ $# -gt 0 ] || usage 2; JOURNAL="$1"; shift ;;
    --snapshot) shift; [ $# -gt 0 ] || usage 2; SNAPSHOT="$1"; shift ;;
    --target-generation) shift; [ $# -gt 0 ] || usage 2; GENERATION="$1"; shift ;;
    --result-file) shift; [ $# -gt 0 ] || usage 2; RESULT_FILE="$1"; shift ;;
    --plan) PLAN=1; shift ;;
    --verify-only) VERIFY_ONLY=1; shift ;;
    --allow-partial) ALLOW_PARTIAL=1; shift ;;
    --target) shift; [ $# -gt 0 ] || usage 2; TARGET_MODE="$1"; shift ;;
    --apply-user-state) APPLY_USER_STATE=1; shift ;;
    --skip-index-rebuild) SKIP_INDEX=1; shift ;;
    --allow-incomplete-portable-snapshot) ALLOW_INCOMPLETE_PORTABLE=1; shift ;;
    *) echo "未知参数: $1" >&2; usage 2 ;;
  esac
done

[ -n "$REPO_DIR" ] || fail 2 "必须提供 --repo"
[ -n "$DB_PATH" ] || fail 2 "必须提供 --db(目标库容器内路径)"
[ -d "$REPO_DIR" ] || fail 1 "仓库目录不存在: $REPO_DIR"
REPO_DIR="$(cd "$REPO_DIR" && pwd)"

if [ -n "$GENERATION" ]; then
  case "$GENERATION" in
    *[!A-Za-z0-9_.-]*) fail 2 "target-generation 只允许 [A-Za-z0-9_.-]" ;;
  esac
fi
if [ -n "$OBJECT_BUCKET" ]; then
  case "$OBJECT_BUCKET" in
    *[!a-z0-9.-]*|'') fail 2 "object-bucket 只允许 [a-z0-9.-]" ;;
  esac
fi

# 仓库只读挂载:导入绝不写便携仓库。
DOCKER_ARGS=(run --rm -v "$REPO_DIR:/content-repo:ro")

if [ -n "$DATA_DIR" ]; then
  [ -d "$DATA_DIR" ] || fail 1 "数据目录不存在: $DATA_DIR"
  DATA_DIR="$(cd "$DATA_DIR" && pwd)"
  DOCKER_ARGS+=(-v "$DATA_DIR:$CONTAINER_DATA")
else
  docker volume inspect "$DATA_VOLUME" >/dev/null 2>&1 || \
    fail 1 "数据卷不存在: $DATA_VOLUME(或设 FLORI_DATA_DIR)"
  DOCKER_ARGS+=(-v "$DATA_VOLUME:$CONTAINER_DATA")
fi

RESULT_ARGS=()
RESULT_ROOT_IDENTITY=""
if [ -n "$RESULT_FILE" ]; then
  [ -d "$(dirname "$RESULT_FILE")" ] || fail 2 \
    "result-file父目录必须由操作者预先创建: $(dirname "$RESULT_FILE")"
  RESULT_DIR="$(cd "$(dirname "$RESULT_FILE")" && pwd)"
  RESULT_ROOT_IDENTITY="$(stat -Lc '%d:%i' -- "$RESULT_DIR")"
  DOCKER_ARGS+=(-v "$RESULT_DIR:/result")
  DOCKER_ARGS+=(-e "FLORI_RESULT_ROOT_IDENTITY=$RESULT_ROOT_IDENTITY")
  RESULT_ARGS=(--result-file "/result/$(basename "$RESULT_FILE")")
fi

for env_name in MINIO_URL MINIO_ACCESS_KEY MINIO_SECRET_KEY MINIO_BUCKET MINIO_SECURE \
                FLORI_LIVE_DB_PATH FLORI_LIVE_JOBS_DIR FLORI_LIVE_CONFIG_ROOT \
                FLORI_LIVE_DATA_ROOT FLORI_DEPLOYMENT_ID \
                FLORI_DR_MAX_AGE_SEC FLORI_REMOTE_WORKERS_QUIESCED \
                FLORI_MAINTENANCE_LOCK_DIR FLORI_ACCEPT_INCOMPLETE_PORTABLE; do
  if [ -n "${!env_name:-}" ]; then
    DOCKER_ARGS+=(-e "$env_name=${!env_name}")
  fi
done
if [ -n "${MINIO_URL:-}" ]; then
  DOCKER_ARGS+=(--network "${FLORI_NETWORK:-flori_default}")
fi

# 默认写隔离 staging;写线上产物根必须显式 --into-live 并通过前置检查。
if [ -z "$JOBS_DIR" ]; then
  if [ "$INTO_LIVE" -eq 1 ]; then
    JOBS_DIR="$CONTAINER_DATA/jobs"
  else
    JOBS_DIR="$CONTAINER_DATA/import-staging/jobs"
  fi
fi
if [ -z "$CONFIG_ROOT" ]; then
  if [ "$INTO_LIVE" -eq 1 ]; then
    CONFIG_ROOT="$LIVE_CONFIG_ROOT"
  else
    CONFIG_ROOT="$CONTAINER_DATA/import-staging/prompts"
  fi
fi
case "$CONFIG_ROOT" in
  /data|/data/*) ;;
  *) fail 2 "--config-root 必须是容器 /data 下的持久路径: $CONFIG_ROOT" ;;
esac

# source root 参数是宿主目标目录。每个 root 单独 rw 挂载,容器内只见稳定 root ID,
# 不把个人绝对路径写进 snapshot 或导入请求的逻辑身份。
SOURCE_ROOT_ARGS=()
SOURCE_ROOT_IDENTITY_ARGS=()
SOURCE_HOST_ROOTS=()
SOURCE_HOST_IDENTITIES=()
SOURCE_HOST_IDS=()
declare -A SOURCE_ROOT_IDS=()
for spec in "${SOURCE_ROOT_SPECS[@]}"; do
  root_id="${spec%%=*}"
  host_root="${spec#*=}"
  if [ "$root_id" = "$spec" ] || [ -z "$root_id" ] || [ -z "$host_root" ]; then
    fail 2 "--source-root 格式必须是 root_id=宿主目标目录"
  fi
  case "$root_id" in
    [a-z0-9]*) ;;
    *) fail 2 "source root id 必须以小写字母或数字开头" ;;
  esac
  case "$root_id" in
    *[!a-z0-9_-]*) fail 2 "source root id 只允许 [a-z0-9_-]" ;;
  esac
  [ "${#root_id}" -le 63 ] || fail 2 "source root id 最长 63 个字符"
  [ -z "${SOURCE_ROOT_IDS[$root_id]:-}" ] || fail 2 "source root id 重复: $root_id"
  SOURCE_ROOT_IDS[$root_id]=1
  case "$host_root" in
    *:*) fail 2 "source root 宿主路径不能包含冒号: $host_root" ;;
  esac
  if content_path_has_symlink "$host_root"; then
    fail 2 "source root 路径不能包含符号链接: $host_root"
  fi
  [ -d "$host_root" ] || fail 2 "source root 必须由操作者预先创建为可信目录: $host_root"
  host_root="$(realpath -m -- "$host_root")"
  if content_paths_overlap "$REPO_DIR" "$host_root"; then
    fail 2 "source root 不得与便携仓库重叠: $host_root"
  fi
  host_identity="$(stat -Lc '%d:%i' -- "$host_root")"
  mount_token="$(printf '%s|%s' "$host_root" "$host_identity" | sha256sum | cut -c1-16)"
  container_root="/source-targets/$root_id-$mount_token"
  DOCKER_ARGS+=(-v "$host_root:$container_root:rw")
  SOURCE_ROOT_ARGS+=(--source-root "$root_id=$container_root")
  SOURCE_ROOT_IDENTITY_ARGS+=(--source-root-identity "$root_id=$host_identity")
  SOURCE_HOST_ROOTS+=("$host_root")
  SOURCE_HOST_IDENTITIES+=("$host_identity")
  SOURCE_HOST_IDS+=("$root_id")
done

SOURCE_POSTCHECK_ERROR=""
verify_source_root_hosts() {
  local index host_root expected_identity current_identity root_id
  for index in "${!SOURCE_HOST_ROOTS[@]}"; do
    host_root="${SOURCE_HOST_ROOTS[$index]}"
    expected_identity="${SOURCE_HOST_IDENTITIES[$index]}"
    root_id="${SOURCE_HOST_IDS[$index]}"
    if content_path_has_symlink "$host_root" || [ ! -d "$host_root" ]; then
      SOURCE_POSTCHECK_ERROR="source root $root_id 的宿主路径在导入期间消失、变类型或含符号链接: $host_root"
      return 1
    fi
    current_identity="$(stat -Lc '%d:%i' -- "$host_root" 2>/dev/null || true)"
    if [ "$current_identity" != "$expected_identity" ]; then
      SOURCE_POSTCHECK_ERROR="source root $root_id 的宿主路径实体在导入期间变化: $expected_identity != $current_identity"
      return 1
    fi
    if content_paths_overlap "$REPO_DIR" "$host_root"; then
      SOURCE_POSTCHECK_ERROR="source root $root_id 的宿主路径在导入期间与便携仓库重叠: $host_root"
      return 1
    fi
  done
  return 0
}

# 把关依据是目标的实际身份,不是 --into-live 有没有传。旧实现只让 INTO_LIVE
# 挑默认 jobs-dir,于是显式 --db /data/db/analyzer.db --jobs-dir /data/jobs
# 同时写两个线上面却既不查 worker 也不要 DR receipt。
strip_slash() { printf '%s' "${1%/}"; }
TARGETS_LIVE=0
LIVE_WHAT=""
if [ "$(strip_slash "$DB_PATH")" = "$(strip_slash "$LIVE_DB_PATH")" ]; then
  TARGETS_LIVE=1; LIVE_WHAT="数据库 $DB_PATH"
fi
if [ -n "${MINIO_URL:-}" ]; then
  PROD_BUCKET="${MINIO_BUCKET:-flori}"
  if [ -z "$OBJECT_BUCKET" ] || [ "$OBJECT_BUCKET" = "$PROD_BUCKET" ]; then
    TARGETS_LIVE=1; LIVE_WHAT="${LIVE_WHAT:+$LIVE_WHAT, }生产桶 $PROD_BUCKET"
  fi
else
  case "$(strip_slash "$JOBS_DIR")" in
    "$(strip_slash "$LIVE_JOBS_DIR")"|"$(strip_slash "$LIVE_JOBS_DIR")"/*)
      TARGETS_LIVE=1; LIVE_WHAT="${LIVE_WHAT:+$LIVE_WHAT, }产物根 $JOBS_DIR" ;;
  esac
fi
if [ "$(strip_slash "$CONFIG_ROOT")" = "$(strip_slash "$LIVE_CONFIG_ROOT")" ]; then
  TARGETS_LIVE=1; LIVE_WHAT="${LIVE_WHAT:+$LIVE_WHAT, }配置根 $CONFIG_ROOT"
fi

DR_ARGS=()
# --plan/--verify-only 只读,不过写入门:恢复流程第 1 步就是对着线上库出计划。
if [ "$TARGETS_LIVE" -eq 1 ] && [ "$PLAN" -eq 0 ] && [ "$VERIFY_ONLY" -eq 0 ]; then
  if [ "$INTO_LIVE" -eq 0 ]; then
    fail 2 "目标解析为线上面($LIVE_WHAT),写它必须显式 --into-live;隔离导入请指定非线上 --db/--jobs-dir,对象存储模式下用 --object-bucket <隔离桶>"
  fi
  # 本机 API/scheduler/worker 由 Python 侧按实际 DB/产物 namespace 的共享锁判定。
  # shell 不再猜容器名,也不在检查与真正写入之间留下 TOCTOU 窗口。
  # 跨机 worker 看不到本机 flock,仍需人工确认。
  if [ "${FLORI_REMOTE_WORKERS_QUIESCED:-}" != "1" ]; then
    fail 1 "docker ps 只覆盖本机容器;确认远程 worker 也已停后设 FLORI_REMOTE_WORKERS_QUIESCED=1"
  fi
  dr_receipt="${FLORI_DR_RECEIPT:-}"
  if [ -z "$dr_receipt" ] || [ ! -f "$dr_receipt" ]; then
    fail 1 "--into-live 需要 FLORI_DR_RECEIPT 指向最近一次 exact DR 的 result JSON"
  fi
  DR_DIR="$(cd "$(dirname "$dr_receipt")" && pwd)"
  DOCKER_ARGS+=(
    -v "$DR_DIR:/dr-receipt:ro"
    -v "$SCRIPT_DIR/dr_snapshot.py:/tool/dr_snapshot.py:ro"
    -v "$REPO/shared/migrations:/tool/migrations:ro"
    -e "FLORI_DR_VALIDATOR=/tool/dr_snapshot.py"
    -e "FLORI_SCHEMA_MANIFEST=/tool/migrations/manifest.json"
  )
  # 容器内会校验 receipt、真实 archive、sidecar、完整 SHA 与归档内部全链。
  DR_ARGS=(--dr-receipt "/dr-receipt/$(basename "$dr_receipt")")
fi

# journal 不得落在目标库目录内:阶段5 丢弃目标库会连崩溃证据一起删。
case "$JOURNAL" in
  /data/*) ;;
  *) fail 2 "--journal 必须在容器 /data 下(否则 --rm 退出即蒸发): $JOURNAL" ;;
esac
db_dir="$(dirname "$DB_PATH")"
case "$JOURNAL" in
  "$db_dir"/*) fail 2 "--journal 不能放在目标库目录 $db_dir 内" ;;
esac

CMD=(python -m shared.content_import
  --repo /content-repo
  --db "$DB_PATH"
  --jobs-dir "$JOBS_DIR"
  --config-root "$CONFIG_ROOT"
  --snapshot "$SNAPSHOT"
  "${RESULT_ARGS[@]}")
CMD+=(--journal "$JOURNAL")
[ -z "$GENERATION" ] || CMD+=(--target-generation "$GENERATION")
[ -z "$OBJECT_BUCKET" ] || CMD+=(--object-bucket "$OBJECT_BUCKET")
[ "$PLAN" -eq 0 ] || CMD+=(--plan)
[ "$VERIFY_ONLY" -eq 0 ] || CMD+=(--verify-only)
[ "$ALLOW_PARTIAL" -eq 0 ] || CMD+=(--allow-partial)
CMD+=(--target "$TARGET_MODE")
[ "$APPLY_USER_STATE" -eq 0 ] || CMD+=(--apply-user-state)
[ "$SKIP_INDEX" -eq 0 ] || CMD+=(--skip-index-rebuild)
[ "$ALLOW_INCOMPLETE_PORTABLE" -eq 0 ] || CMD+=(--allow-incomplete-portable-snapshot)
[ ${#SOURCE_ROOT_ARGS[@]} -eq 0 ] || CMD+=("${SOURCE_ROOT_ARGS[@]}")
[ ${#SOURCE_ROOT_IDENTITY_ARGS[@]} -eq 0 ] || CMD+=("${SOURCE_ROOT_IDENTITY_ARGS[@]}")
[ "$INTO_LIVE" -eq 0 ] || CMD+=(--into-live)
[ ${#DR_ARGS[@]} -eq 0 ] || CMD+=("${DR_ARGS[@]}")

RESULT_GUARD_ROOTS=()
[ -z "$DATA_DIR" ] || RESULT_GUARD_ROOTS+=("$DATA_DIR")
[ ${#SOURCE_HOST_ROOTS[@]} -eq 0 ] || RESULT_GUARD_ROOTS+=("${SOURCE_HOST_ROOTS[@]}")
CONTENT_RESULT_PROTECTED_ROOTS=("${RESULT_GUARD_ROOTS[@]}")

if content_run_with_result_guard \
    "$REPO_DIR" "$RESULT_FILE" "$RESULT_ROOT_IDENTITY" \
    "${#RESULT_GUARD_ROOTS[@]}" "${RESULT_GUARD_ROOTS[@]}" \
    docker "${DOCKER_ARGS[@]}" "$IMAGE" "${CMD[@]}"; then
  status=0
else
  status=$?
fi
if [ "$status" -eq 0 ] && ! verify_source_root_hosts; then
  fail 1 "$SOURCE_POSTCHECK_ERROR;容器固定的旧挂载可能含可幂等续跑残留,本轮不得视为成功"
fi
exit "$status"
