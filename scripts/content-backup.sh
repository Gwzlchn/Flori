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
#       [--full-rehash] [--work-dir <dir>] [--result-file <json>]
#   scripts/content-backup.sh --repo <dir> --verify [--result-file <json>]
#
# 首次必做:--full-rehash 建立密钥扫描基线。文本 blob 的明文密钥扫描只在真正读
# 字节时发生,增量路径会跳过已在仓库的 blob,所以扫描上线前收进去的字节从没被扫
# 过,日常增量也不会补扫。新仓库与扫描规则变更后各跑一次:
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

IMAGE="${FLORI_CONTENT_BACKUP_IMAGE:-flori:${IMAGE_TAG:-uptest}}"
DATA_VOLUME="${FLORI_DATA_VOLUME:-flori-data}"
CONTAINER_DATA="/data"

# 早退也必须留下机器可读结果:自动化按 --result-file 判生死,shell 直接 exit
# 会让调用方读到上一次的陈旧 JSON 或什么都读不到。参数错(usage)同样受这条约束,
# 所以两条早退路径共用它。
write_failure_result() {
  [ -n "$RESULT_FILE" ] || return 0
  mkdir -p "$(dirname "$RESULT_FILE")"
  printf '{\n  "error": "%s",\n  "mode": "%s",\n  "ok": false\n}\n' \
    "$(printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g')" "backup" > "$RESULT_FILE"
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
for _index in $(seq 1 $#); do
  if [ "${!_index}" = "--result-file" ]; then
    _next=$((_index + 1))
    [ "$_next" -le $# ] && RESULT_FILE="${!_next}"
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
VERIFY=0
FULL_REHASH=0
ALLOW_UNKNOWN=0
JOB_ARGS=()

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
    --allow-unknown) ALLOW_UNKNOWN=1; shift ;;
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
if [ -n "$RESULT_FILE" ]; then
  mkdir -p "$(dirname "$RESULT_FILE")"
  RESULT_DIR="$(cd "$(dirname "$RESULT_FILE")" && pwd)"
  DOCKER_ARGS+=(-v "$RESULT_DIR:/result")
  RESULT_MOUNT=(--result-file "/result/$(basename "$RESULT_FILE")")
fi

if [ "$VERIFY" -eq 1 ]; then
  CMD+=(verify --repo /content-repo "${RESULT_MOUNT[@]}")
  exec docker "${DOCKER_ARGS[@]}" "$IMAGE" "${CMD[@]}"
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

CMD_WORK=()
if [ -n "$WORK_DIR" ]; then
  mkdir -p "$WORK_DIR"
  WORK_DIR="$(cd "$WORK_DIR" && pwd)"
  DOCKER_ARGS+=(-v "$WORK_DIR:/work")
  CMD_WORK=(--work-dir /work)
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
  "${RESULT_MOUNT[@]}" "${CMD_WORK[@]}" "${CMD_ALLOW[@]}")
[ "$ALLOW_UNKNOWN" -eq 0 ] || CMD+=(--allow-unknown)
[ "$FULL_REHASH" -eq 0 ] || CMD+=(--full-rehash)
[ ${#JOB_ARGS[@]} -eq 0 ] || CMD+=("${JOB_ARGS[@]}")

exec docker "${DOCKER_ARGS[@]}" "$IMAGE" "${CMD[@]}"
