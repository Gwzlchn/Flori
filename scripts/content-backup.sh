#!/usr/bin/env bash
# Flori 便携内容备份入口(portable content repository,设计稿 05 号 §2.1B)。
# 与 scripts/backup.sh(exact DR)互补:本仓库只收业务事实/审计/有效产物,
# 按内容摘要去重,可反复增量;不含凭据、队列与运行态,不能用于整机回滚。
#
# 全容器内运行(宿主不装 Python 依赖),复用 flori 应用镜像里的 shared/ 包。
# 路径按容器内视图传参:数据卷统一挂到 /data(与 docker-compose.yml 一致),
# 因此默认库是 /data/db/analyzer.db、默认产物根是 /data/jobs。
#
# 用法:
#   scripts/content-backup.sh --repo <dir> [--data-dir <dir>] [--ref <名字>]
#       [--job <id>]... [--run-id <id>] [--allow-unknown-file <清单>]
#       [--allow-secret-blob-file <清单>]
#       [--user-config-dir <host-dir>]
#       [--vendor-media --source-root <root_id>=<host-dir>]...
#       [--full-rehash] [--work-dir <dir>] [--result-file <json>]
#   scripts/content-backup.sh --repo <dir> --verify [--result-file <json>]
#
# 日常增量会完整重读并扫描所有文本输出;已有文本不会因CAS命中而漏过新扫描规则。
# --full-rehash 只用于把二进制CAS也逐字节重读,执行全介质位腐蚀/摘要审计。它可能
# 重读大量视频,不应当作密钥扫描基线或每次增量的前置条件:
#   scripts/content-backup.sh --repo <dir> --full-rehash --result-file <json>
#
# 环境变量:
#   FLORI_DATA_DIR             宿主数据根;缺省用命名卷 ${FLORI_DATA_VOLUME}
#   FLORI_DATA_VOLUME          数据卷名(默认 flori-data)
#   FLORI_CONTENT_BACKUP_IMAGE 运行镜像(默认 flori:${IMAGE_TAG:-uptest})
#   FLORI_NETWORK              MINIO_URL 非空时接入的 docker 网络(默认 flori_default)
#   BACKUP_RUN_ID              --run-id 缺省值(§2.8-4 幂等键)
#   MINIO_URL/MINIO_*          设置时容器内走对象存储读(透传,本地 jobs 目录不再必需)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shell 早退发生在容器启动前,必须自己可靠地产生 JSON。
source "$SCRIPT_DIR/lib/content-result.sh"

IMAGE="${FLORI_CONTENT_BACKUP_IMAGE:-flori:${IMAGE_TAG:-uptest}}"
DATA_VOLUME="${FLORI_DATA_VOLUME:-flori-data}"
CONTAINER_DATA="/data"

# 早退也必须留下机器可读结果:自动化按 --result-file 判生死,shell 直接 exit
# 会让调用方读到上一次的陈旧 JSON 或什么都读不到。参数错(usage)同样受这条约束,
# 所以两条早退路径共用它。
write_failure_result() {
  content_write_failure_result "backup" "$1"
}

# 只打印文件头的注释块;写死行号会把 set -euo pipefail / IMAGE= 当帮助printf 出来。
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

# --result-file 先于主解析扫一遍:参数错发生在解析途中,等轮到它才赋值就太晚了,
# usage 早退会写不出结果 JSON(取决于调用方把它排在第几个)。
RESULT_FILE=""
PRESCAN_REPO=""
PRESCAN_DATA_DIR="${FLORI_DATA_DIR:-}"
PRESCAN_WORK_DIR=""
PRESCAN_USER_CONFIG_DIR=""
PRESCAN_SOURCE_ROOTS=()
PRESCAN_PROTECTED_FILES=()
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
  if [ "${!_index}" = "--work-dir" ]; then
    _next=$((_index + 1)); [ "$_next" -le $# ] && PRESCAN_WORK_DIR="${!_next}"
  fi
  if [ "${!_index}" = "--user-config-dir" ]; then
    _next=$((_index + 1)); [ "$_next" -le $# ] && PRESCAN_USER_CONFIG_DIR="${!_next}"
  fi
  if [ "${!_index}" = "--source-root" ]; then
    _next=$((_index + 1))
    if [ "$_next" -le $# ]; then
      _value="${!_next}"
      [ "${_value#*=}" = "$_value" ] || PRESCAN_SOURCE_ROOTS+=("${_value#*=}")
    fi
  fi
  if [ "${!_index}" = "--allow-unknown-file" ] \
      || [ "${!_index}" = "--allow-secret-blob-file" ]; then
    _next=$((_index + 1)); [ "$_next" -le $# ] && PRESCAN_PROTECTED_FILES+=("${!_next}")
  fi
done
CONTENT_RESULT_PROTECTED_ROOTS=(
  "$PRESCAN_DATA_DIR" "$PRESCAN_WORK_DIR" "$PRESCAN_USER_CONFIG_DIR"
  "${PRESCAN_SOURCE_ROOTS[@]}"
)
if ! content_validate_result_boundary "$PRESCAN_REPO" "$RESULT_FILE"; then
  unsafe_result="$RESULT_FILE"; RESULT_FILE=""
  fail 2 "--result-file 必须在便携仓库之外且路径不能含符号链接: $unsafe_result"
fi
PRESCAN_INPUT_ROOTS=()
[ -z "$PRESCAN_DATA_DIR" ] || PRESCAN_INPUT_ROOTS+=("$PRESCAN_DATA_DIR")
[ -z "$PRESCAN_USER_CONFIG_DIR" ] || PRESCAN_INPUT_ROOTS+=("$PRESCAN_USER_CONFIG_DIR")
[ ${#PRESCAN_SOURCE_ROOTS[@]} -eq 0 ] || PRESCAN_INPUT_ROOTS+=("${PRESCAN_SOURCE_ROOTS[@]}")
for protected in "${PRESCAN_INPUT_ROOTS[@]}"; do
  if [ -n "$RESULT_FILE" ] && content_paths_overlap "$protected" "$RESULT_FILE"; then
    unsafe_result="$RESULT_FILE"; RESULT_FILE=""
    fail 2 "--result-file 不得位于数据、配置或来源根内: $unsafe_result"
  fi
  if [ -n "$PRESCAN_REPO" ] && content_paths_overlap "$protected" "$PRESCAN_REPO"; then
    fail 2 "数据、配置或来源根不得与便携仓库重叠: $PRESCAN_REPO"
  fi
  if [ -n "$PRESCAN_WORK_DIR" ] && content_paths_overlap "$protected" "$PRESCAN_WORK_DIR"; then
    fail 2 "--work-dir 不得与数据、配置或来源根重叠: $PRESCAN_WORK_DIR"
  fi
done
if [ -n "$PRESCAN_WORK_DIR" ] && [ -n "$PRESCAN_REPO" ] \
    && content_paths_overlap "$PRESCAN_REPO" "$PRESCAN_WORK_DIR"; then
  fail 2 "--work-dir 不得位于便携仓库内或包含便携仓库: $PRESCAN_WORK_DIR"
fi
for protected in "${PRESCAN_PROTECTED_FILES[@]}"; do
  if [ -n "$RESULT_FILE" ] && content_paths_overlap "$protected" "$RESULT_FILE"; then
    unsafe_result="$RESULT_FILE"; RESULT_FILE=""
    fail 2 "--result-file 不得覆盖本轮输入清单: $unsafe_result"
  fi
done

REPO_DIR=""
DATA_DIR="${FLORI_DATA_DIR:-}"
DB_PATH=""
JOBS_DIR=""
REF="latest"
RUN_ID="${BACKUP_RUN_ID:-}"
ALLOW_UNKNOWN_FILE=""
SECRET_BLOB_FILE=""
WORK_DIR=""
USER_CONFIG_DIR=""
VERIFY=0
FULL_REHASH=0
ALLOW_UNKNOWN=0
VENDOR_MEDIA=0
JOB_ARGS=()
SOURCE_ROOT_SPECS=()

while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help) usage 0 ;;
    --repo) shift; [ $# -gt 0 ] || usage 2; REPO_DIR="$1"; shift ;;
    --data-dir) shift; [ $# -gt 0 ] || usage 2; DATA_DIR="$1"; shift ;;
    --db) shift; [ $# -gt 0 ] || usage 2; DB_PATH="$1"; shift ;;
    --jobs-dir) shift; [ $# -gt 0 ] || usage 2; JOBS_DIR="$1"; shift ;;
    --ref) shift; [ $# -gt 0 ] || usage 2; REF="$1"; shift ;;
    --job) shift; [ $# -gt 0 ] || usage 2; JOB_ARGS+=(--job "$1"); shift ;;
    --run-id) shift; [ $# -gt 0 ] || usage 2; RUN_ID="$1"; shift ;;
    --result-file) shift; [ $# -gt 0 ] || usage 2; RESULT_FILE="$1"; shift ;;
    --allow-unknown-file) shift; [ $# -gt 0 ] || usage 2; ALLOW_UNKNOWN_FILE="$1"; shift ;;
    --allow-secret-blob-file) shift; [ $# -gt 0 ] || usage 2; SECRET_BLOB_FILE="$1"; shift ;;
    --work-dir) shift; [ $# -gt 0 ] || usage 2; WORK_DIR="$1"; shift ;;
    --user-config-dir) shift; [ $# -gt 0 ] || usage 2; USER_CONFIG_DIR="$1"; shift ;;
    --allow-unknown) ALLOW_UNKNOWN=1; shift ;;
    --vendor-media) VENDOR_MEDIA=1; shift ;;
    --source-root) shift; [ $# -gt 0 ] || usage 2; SOURCE_ROOT_SPECS+=("$1"); shift ;;
    --full-rehash) FULL_REHASH=1; shift ;;
    --verify) VERIFY=1; shift ;;
    *) echo "未知参数: $1" >&2; usage 2 ;;
  esac
done

[ -n "$REPO_DIR" ] || fail 2 "必须提供 --repo"
mkdir -p "$REPO_DIR"
REPO_DIR="$(cd "$REPO_DIR" && pwd)"

DOCKER_ARGS=(run --rm -v "$REPO_DIR:/content-repo")
CMD=(python -m shared.content_backup)

RESULT_MOUNT=()
RESULT_ROOT_IDENTITY=""
if [ -n "$RESULT_FILE" ]; then
  [ -d "$(dirname "$RESULT_FILE")" ] || fail 2 \
    "result-file父目录必须由操作者预先创建: $(dirname "$RESULT_FILE")"
  RESULT_DIR="$(cd "$(dirname "$RESULT_FILE")" && pwd)"
  RESULT_ROOT_IDENTITY="$(stat -Lc '%d:%i' -- "$RESULT_DIR")"
  DOCKER_ARGS+=(-v "$RESULT_DIR:/result")
  DOCKER_ARGS+=(-e "FLORI_RESULT_ROOT_IDENTITY=$RESULT_ROOT_IDENTITY")
  RESULT_MOUNT=(--result-file "/result/$(basename "$RESULT_FILE")")
fi

if [ "$VERIFY" -eq 1 ]; then
  CMD+=(verify --repo /content-repo "${RESULT_MOUNT[@]}")
  RESULT_GUARD_ROOTS=()
  if content_run_with_result_guard \
      "$REPO_DIR" "$RESULT_FILE" "$RESULT_ROOT_IDENTITY" \
      "${#RESULT_GUARD_ROOTS[@]}" "${RESULT_GUARD_ROOTS[@]}" \
      docker "${DOCKER_ARGS[@]}" "$IMAGE" "${CMD[@]}"; then
    exit 0
  else
    status=$?
    exit "$status"
  fi
fi

# 局部快照不得覆盖 latest(容器内还有一道同样的门,这里早失败省一次启动)。
if [ ${#JOB_ARGS[@]} -gt 0 ] && [ "$REF" = "latest" ]; then
  fail 2 "--job 是局部快照,必须显式 --ref <名字>,不能覆盖 latest"
fi

if [ -z "$RUN_ID" ]; then
  RUN_ID="run-$(date -u +%Y%m%dT%H%M%SZ)-$$"
fi
case "$RUN_ID" in
  *[!A-Za-z0-9_-]*|'') fail 2 "run-id 只允许 [A-Za-z0-9_-]" ;;
esac
case "$REF" in
  *[!A-Za-z0-9._-]*|'') fail 2 "ref 名只允许 [A-Za-z0-9._-]" ;;
esac

# 数据源挂载:与 docker-compose.yml 一致,整个数据根挂到容器 /data。
# 库/产物是否存在由容器内 preflight 判断,宿主只校验挂载源本身。
# SQLite online backup 的读连接在 WAL 模式下要能访问 -shm/-wal,故不用 :ro。
if [ -n "$DATA_DIR" ]; then
  [ -d "$DATA_DIR" ] || fail 1 "数据目录不存在: $DATA_DIR"
  DATA_DIR="$(cd "$DATA_DIR" && pwd)"
  DOCKER_ARGS+=(-v "$DATA_DIR:$CONTAINER_DATA")
else
  docker volume inspect "$DATA_VOLUME" >/dev/null 2>&1 || \
    fail 1 "数据卷不存在: $DATA_VOLUME(或设 FLORI_DATA_DIR)"
  DOCKER_ARGS+=(-v "$DATA_VOLUME:$CONTAINER_DATA")
fi

for env_name in MINIO_URL MINIO_ACCESS_KEY MINIO_SECRET_KEY MINIO_BUCKET MINIO_SECURE; do
  if [ -n "${!env_name:-}" ]; then
    DOCKER_ARGS+=(-e "$env_name=${!env_name}")
  fi
done
# 走对象存储时必须能解析到 minio 服务名,否则容器内 DNS 失败。
if [ -n "${MINIO_URL:-}" ]; then
  DOCKER_ARGS+=(--network "${FLORI_NETWORK:-flori_default}")
fi

# 外部 NAS 不假设位于 /data。vendor 模式按 root_id 显式只读挂载,并把容器内
# 映射交给 SourceLibrary；未配置映射会在 Python 校验阶段 fail-closed。
SOURCE_ROOTS_JSON="{}"
SOURCE_HOST_ROOTS=()
if [ ${#SOURCE_ROOT_SPECS[@]} -gt 0 ]; then
  SOURCE_ROOTS_JSON="{"
  SOURCE_SEPARATOR=""
  for spec in "${SOURCE_ROOT_SPECS[@]}"; do
    root_id="${spec%%=*}"
    host_root="${spec#*=}"
    [ "$root_id" != "$spec" ] && [ -n "$host_root" ] || \
      fail 2 "--source-root 必须是 <root_id>=<host-dir>"
    case "$root_id" in
      *[!a-z0-9_-]*|'') fail 2 "source root id 只允许 [a-z0-9_-]" ;;
    esac
    case "${root_id:0:1}" in
      [a-z0-9]) ;;
      *) fail 2 "source root id 必须以字母或数字开头" ;;
    esac
    [ "${#root_id}" -le 63 ] || fail 2 "source root id 最长 63 个字符"
    case "$host_root" in
      *'"'*|*'\\'*) fail 2 "source root 路径不能包含双引号或反斜杠" ;;
    esac
    [ -d "$host_root" ] || fail 1 "source root 不存在: $host_root"
    host_root="$(cd "$host_root" && pwd)"
    SOURCE_HOST_ROOTS+=("$host_root")
    container_root="/source-roots/$root_id"
    DOCKER_ARGS+=(-v "$host_root:$container_root:ro")
    SOURCE_ROOTS_JSON+="$SOURCE_SEPARATOR\"$root_id\":\"$container_root\""
    SOURCE_SEPARATOR=","
  done
  SOURCE_ROOTS_JSON+="}"
  DOCKER_ARGS+=(-e "FLORI_SOURCE_ROOTS_JSON=$SOURCE_ROOTS_JSON")
fi

CMD_WORK=()
if [ -n "$WORK_DIR" ]; then
  mkdir -p "$WORK_DIR"
  WORK_DIR="$(cd "$WORK_DIR" && pwd)"
  DOCKER_ARGS+=(-v "$WORK_DIR:/work")
  CMD_WORK=(--work-dir /work)
fi

CMD_USER_CONFIG=()
if [ -n "$USER_CONFIG_DIR" ]; then
  [ -d "$USER_CONFIG_DIR" ] || fail 1 "用户配置目录不存在: $USER_CONFIG_DIR"
  USER_CONFIG_DIR="$(cd "$USER_CONFIG_DIR" && pwd)"
  DOCKER_ARGS+=(-v "$USER_CONFIG_DIR:/user-config:ro")
  CMD_USER_CONFIG=(
    --user-config-dir /user-config
    --user-config-source-id "$USER_CONFIG_DIR"
  )
fi

CMD_ALLOW=()
if [ -n "$SECRET_BLOB_FILE" ]; then
  [ -f "$SECRET_BLOB_FILE" ] || fail 1 "密钥例外清单不存在: $SECRET_BLOB_FILE"
  SECRET_DIR="$(cd "$(dirname "$SECRET_BLOB_FILE")" && pwd)"
  DOCKER_ARGS+=(-v "$SECRET_DIR:/secret-allowlist:ro")
  CMD_ALLOW+=(--allow-secret-blob-file "/secret-allowlist/$(basename "$SECRET_BLOB_FILE")")
fi
if [ -n "$ALLOW_UNKNOWN_FILE" ]; then
  [ -f "$ALLOW_UNKNOWN_FILE" ] || fail 1 "例外清单不存在: $ALLOW_UNKNOWN_FILE"
  ALLOW_DIR="$(cd "$(dirname "$ALLOW_UNKNOWN_FILE")" && pwd)"
  DOCKER_ARGS+=(-v "$ALLOW_DIR:/allowlist:ro")
  CMD_ALLOW+=(--allow-unknown-file "/allowlist/$(basename "$ALLOW_UNKNOWN_FILE")")
fi

CMD+=(backup
  --repo /content-repo
  --db "${DB_PATH:-$CONTAINER_DATA/db/analyzer.db}"
  --jobs-dir "${JOBS_DIR:-$CONTAINER_DATA/jobs}"
  --ref "$REF"
  --run-id "$RUN_ID"
  "${RESULT_MOUNT[@]}" "${CMD_WORK[@]}" "${CMD_USER_CONFIG[@]}" "${CMD_ALLOW[@]}")
[ "$ALLOW_UNKNOWN" -eq 0 ] || CMD+=(--allow-unknown)
[ "$FULL_REHASH" -eq 0 ] || CMD+=(--full-rehash)
[ "$VENDOR_MEDIA" -eq 0 ] || CMD+=(--vendor-media)
[ ${#JOB_ARGS[@]} -eq 0 ] || CMD+=("${JOB_ARGS[@]}")

RESULT_GUARD_ROOTS=()
[ -z "$DATA_DIR" ] || RESULT_GUARD_ROOTS+=("$DATA_DIR")
[ -z "$WORK_DIR" ] || RESULT_GUARD_ROOTS+=("$WORK_DIR")
[ -z "$USER_CONFIG_DIR" ] || RESULT_GUARD_ROOTS+=("$USER_CONFIG_DIR")
[ ${#SOURCE_HOST_ROOTS[@]} -eq 0 ] || RESULT_GUARD_ROOTS+=("${SOURCE_HOST_ROOTS[@]}")
CONTENT_RESULT_PROTECTED_ROOTS=("${RESULT_GUARD_ROOTS[@]}")

if content_run_with_result_guard \
    "$REPO_DIR" "$RESULT_FILE" "$RESULT_ROOT_IDENTITY" \
    "${#RESULT_GUARD_ROOTS[@]}" "${RESULT_GUARD_ROOTS[@]}" \
    docker "${DOCKER_ARGS[@]}" "$IMAGE" "${CMD[@]}"; then
  exit 0
else
  status=$?
  exit "$status"
fi
