#!/usr/bin/env bash
# 生成包含全部持久资产、逐文件校验和与 SQLite 一致性快照的灾备归档.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
COMPOSE_PROJECT="${COMPOSE_PROJECT:-flori}"
BACKUP_DIR="${BACKUP_DIR:-./backups}"
FLORI_DATA_DIR_WAS_SET="${FLORI_DATA_DIR+x}"
FLORI_DATA_VOLUME_WAS_SET="${FLORI_DATA_VOLUME+x}"
FLORI_DATA_DIR="${FLORI_DATA_DIR:-}"
FLORI_DATA_VOLUME="${FLORI_DATA_VOLUME:-${COMPOSE_PROJECT}_flori-data}"
FLORI_DATA_CONTAINER="${FLORI_DATA_CONTAINER-flori-api}"
REDIS_DATA_DIR_WAS_SET="${REDIS_DATA_DIR+x}"
REDIS_VOLUME_WAS_SET="${REDIS_VOLUME+x}"
REDIS_DATA_DIR="${REDIS_DATA_DIR:-}"
REDIS_VOLUME="${REDIS_VOLUME:-${COMPOSE_PROJECT}_redis-data}"
REDIS_CONTAINER="${REDIS_CONTAINER-flori-redis}"
MINIO_DATA_DIR_WAS_SET="${MINIO_DATA_DIR+x}"
MINIO_VOLUME_WAS_SET="${MINIO_VOLUME+x}"
MINIO_DATA_DIR="${MINIO_DATA_DIR:-}"
MINIO_VOLUME="${MINIO_VOLUME:-${COMPOSE_PROJECT}_minio-data}"
MINIO_CONTAINER="${MINIO_CONTAINER-flori-minio}"
MINIO_REQUIRED="${MINIO_REQUIRED:-auto}"
FLORI_CONFIG_DIR="${FLORI_CONFIG_DIR:-$REPO/configs}"
FLORI_SCHEMA_MANIFEST="${FLORI_SCHEMA_MANIFEST:-$REPO/shared/migrations/manifest.json}"
FLORI_DR_IMAGE="${FLORI_DR_IMAGE:-python:3.11-slim}"
FLORI_REDIS_IMAGE="${FLORI_REDIS_IMAGE:-redis:7-alpine}"
REDIS_MATERIALIZE_TIMEOUT="${REDIS_MATERIALIZE_TIMEOUT:-60}"
BACKUP_GENERATION="${BACKUP_GENERATION:-}"
BACKUP_RESULT_FILE="${BACKUP_RESULT_FILE:-}"
DATA_EXCLUDES=()
MINIO_EXCLUDES=()
TEMP_REDIS_VOLUME=""
TEMP_REDIS_CONTAINER=""

cleanup() {
  if [ -n "$TEMP_REDIS_CONTAINER" ]; then
    docker rm -f "$TEMP_REDIS_CONTAINER" >/dev/null 2>&1 || true
  fi
  if [ -n "$TEMP_REDIS_VOLUME" ]; then
    docker volume rm "$TEMP_REDIS_VOLUME" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

usage() {
  cat <<'EOF'
用法: scripts/backup.sh [备份目录] [--result-file <JSON>]
       [--data-exclude <相对子树>] [--minio-exclude <相对子树>]

快照内容:
  - /data 全部持久状态，其中 db/analyzer.db 通过 SQLite online backup 单独获取。
  - jobs、prompts/profiles、worker 持久状态与运行时 configs。
  - Redis RDB（运行中容器先强制 SAVE）或已停服的完整 Redis 卷。
  - MinIO 数据根（未启用时在 manifest 标记 not-configured）。
  - 无 secret 值的应用配置目录。

只有资产稳定性、SQLite integrity_check 和全部 sha256 通过才会原子发布
flori-backup-<generation>.tar.gz、.sha256 与机器可读 result JSON。任一步失败不覆盖
既有备份。

主要环境变量:
  FLORI_DATA_DIR / FLORI_DATA_VOLUME
  REDIS_DATA_DIR / REDIS_VOLUME / REDIS_CONTAINER
  MINIO_DATA_DIR / MINIO_VOLUME / MINIO_REQUIRED=auto|0|1
  FLORI_CONFIG_DIR / FLORI_SCHEMA_MANIFEST / FLORI_DR_IMAGE
  BACKUP_GENERATION / BACKUP_RESULT_FILE
  --data-exclude / --minio-exclude 可重复,分别作用于对应资产;排除项写入 manifest
  FLORI_REDIS_IMAGE / REDIS_MATERIALIZE_TIMEOUT
EOF
  exit "${1:-0}"
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    -h|--help) usage 0 ;;
    --result-file)
      [ "$#" -ge 2 ] || usage 1
      BACKUP_RESULT_FILE="$2"
      shift 2
      ;;
    --data-exclude)
      [ "$#" -ge 2 ] || usage 1
      DATA_EXCLUDES+=("$2")
      shift 2
      ;;
    --minio-exclude)
      [ "$#" -ge 2 ] || usage 1
      MINIO_EXCLUDES+=("$2")
      shift 2
      ;;
    -*) echo "未知选项: $1" >&2; usage 1 ;;
    *) BACKUP_DIR="$1"; shift ;;
  esac
done

command -v docker >/dev/null 2>&1 || { echo "错误: 找不到 docker" >&2; exit 1; }
[ -f "$SCRIPT_DIR/dr_snapshot.py" ] || { echo "错误: 缺少 scripts/dr_snapshot.py" >&2; exit 1; }
[ -f "$FLORI_SCHEMA_MANIFEST" ] || {
  echo "错误: 缺少 schema manifest: $FLORI_SCHEMA_MANIFEST" >&2
  exit 1
}
FLORI_SCHEMA_MANIFEST="$(cd "$(dirname "$FLORI_SCHEMA_MANIFEST")" && pwd)/$(basename "$FLORI_SCHEMA_MANIFEST")"
FLORI_SCHEMA_DIR="$(dirname "$FLORI_SCHEMA_MANIFEST")"
FLORI_SCHEMA_NAME="$(basename "$FLORI_SCHEMA_MANIFEST")"

discover_data_mount() {
  local container="$1" destination="$2" descriptor type name source
  docker inspect "$container" >/dev/null 2>&1 || return 1
  descriptor="$(docker inspect --format "{{range .Mounts}}{{if eq .Destination \"$destination\"}}{{.Type}}|{{.Name}}|{{.Source}}{{end}}{{end}}" "$container")"
  [ -n "$descriptor" ] || return 1
  IFS='|' read -r type name source <<EOF
$descriptor
EOF
  case "$type" in
    bind) printf 'bind|%s\n' "$source" ;;
    volume) printf 'volume|%s\n' "$name" ;;
    *) return 1 ;;
  esac
}

if [ -z "$FLORI_DATA_DIR_WAS_SET" ] && [ -z "$FLORI_DATA_VOLUME_WAS_SET" ]; then
  if mount_info="$(discover_data_mount "$FLORI_DATA_CONTAINER" /data)"; then
    case "$mount_info" in
      bind\|*) FLORI_DATA_DIR="${mount_info#bind|}" ;;
      volume\|*) FLORI_DATA_VOLUME="${mount_info#volume|}" ;;
    esac
  fi
fi
if [ -z "$REDIS_DATA_DIR_WAS_SET" ] && [ -z "$REDIS_VOLUME_WAS_SET" ]; then
  if mount_info="$(discover_data_mount "$REDIS_CONTAINER" /data)"; then
    case "$mount_info" in
      bind\|*) REDIS_DATA_DIR="${mount_info#bind|}" ;;
      volume\|*) REDIS_VOLUME="${mount_info#volume|}" ;;
    esac
  fi
fi
if [ -z "$MINIO_DATA_DIR_WAS_SET" ] && [ -z "$MINIO_VOLUME_WAS_SET" ]; then
  if mount_info="$(discover_data_mount "$MINIO_CONTAINER" /data)"; then
    case "$mount_info" in
      bind\|*) MINIO_DATA_DIR="${mount_info#bind|}" ;;
      volume\|*) MINIO_VOLUME="${mount_info#volume|}" ;;
    esac
  elif [ -n "$FLORI_DATA_DIR" ] && [ -d "${FLORI_DATA_DIR%/}/minio" ]; then
    # NAS 默认把 MinIO bind 嵌在数据根.容器已被删除时仍要识别并单独收录,
    # 不能退化成 data 内的一份未声明副本.
    MINIO_DATA_DIR="${FLORI_DATA_DIR%/}/minio"
  fi
fi

absolute_existing_dir() {
  local path="$1"
  [ -d "$path" ] || { echo "错误: 目录不存在: $path" >&2; exit 1; }
  (cd "$path" && pwd)
}

mkdir -p "$BACKUP_DIR"
BACKUP_DIR="$(cd "$BACKUP_DIR" && pwd)"

if [ -z "$BACKUP_GENERATION" ]; then
  BACKUP_GENERATION="$(date -u +%Y%m%dT%H%M%SZ)-$$"
fi
case "$BACKUP_GENERATION" in
  *[!A-Za-z0-9_.-]*|'') echo "错误: BACKUP_GENERATION 含非法字符" >&2; exit 1 ;;
esac
case "$REDIS_MATERIALIZE_TIMEOUT" in
  *[!0-9]*|'') echo "错误: REDIS_MATERIALIZE_TIMEOUT 必须是非负整数" >&2; exit 1 ;;
esac
ARCHIVE_NAME="flori-backup-${BACKUP_GENERATION}.tar.gz"
ARCHIVE="$BACKUP_DIR/$ARCHIVE_NAME"
[ ! -e "$ARCHIVE" ] || { echo "错误: 备份已存在，拒绝覆盖: $ARCHIVE" >&2; exit 1; }

if [ -z "$BACKUP_RESULT_FILE" ]; then
  BACKUP_RESULT_FILE="$ARCHIVE.result.json"
fi
mkdir -p "$(dirname "$BACKUP_RESULT_FILE")"
RESULT_DIR="$(cd "$(dirname "$BACKUP_RESULT_FILE")" && pwd)"
RESULT_NAME="$(basename "$BACKUP_RESULT_FILE")"

DOCKER_ARGS=(run --rm
  -v "$SCRIPT_DIR/dr_snapshot.py:/tool/dr_snapshot.py:ro"
  -v "$FLORI_SCHEMA_DIR:/tool/migrations:ro"
  -v "$BACKUP_DIR:/output"
  -v "$RESULT_DIR:/result")
CREATE_ARGS=(python /tool/dr_snapshot.py create
  --data /source-data
  --redis /source-redis
  --output "/output/$ARCHIVE_NAME"
  --generation "$BACKUP_GENERATION"
  --schema-manifest "/tool/migrations/$FLORI_SCHEMA_NAME"
  --result-file "/result/$RESULT_NAME"
  --owner-uid "$(id -u)"
  --owner-gid "$(id -g)")
for excluded in "${DATA_EXCLUDES[@]}"; do
  CREATE_ARGS+=(--data-exclude "$excluded")
done
for excluded in "${MINIO_EXCLUDES[@]}"; do
  CREATE_ARGS+=(--minio-exclude "$excluded")
done

if [ -n "$FLORI_DATA_DIR" ]; then
  FLORI_DATA_DIR="$(absolute_existing_dir "$FLORI_DATA_DIR")"
  DOCKER_ARGS+=(-v "$FLORI_DATA_DIR:/source-data:ro")
  DATA_LABEL="bind"
else
  docker volume inspect "$FLORI_DATA_VOLUME" >/dev/null 2>&1 || {
    echo "错误: 数据卷不存在: $FLORI_DATA_VOLUME" >&2
    exit 1
  }
  DOCKER_ARGS+=(-v "$FLORI_DATA_VOLUME:/source-data:ro")
  DATA_LABEL="volume"
fi

REDIS_MODE="offline-volume"
if [ -n "$REDIS_CONTAINER" ] && docker ps --format '{{.Names}}' | grep -Fqx "$REDIS_CONTAINER"; then
  echo "==> 强制 Redis 生成本代 RDB"
  docker exec "$REDIS_CONTAINER" redis-cli SAVE >/dev/null || {
    echo "错误: Redis SAVE 失败，拒绝生成不完整备份" >&2
    exit 1
  }
  REDIS_MODE="rdb"
fi

materialize_redis_aof() {
  local source_mount="$1" elapsed=0 info
  TEMP_REDIS_VOLUME="flori-dr-redis-${BACKUP_GENERATION}-$$"
  TEMP_REDIS_CONTAINER="flori-dr-redis-${BACKUP_GENERATION}-$$"
  docker volume create "$TEMP_REDIS_VOLUME" >/dev/null
  docker run --rm \
    -v "$source_mount:/source-redis:ro" \
    -v "$TEMP_REDIS_VOLUME:/target-redis" \
    "$FLORI_DR_IMAGE" \
    python -c 'import pathlib, shutil; source=pathlib.Path("/source-redis/dump.rdb"); target=pathlib.Path("/target-redis/dump.rdb"); source.is_file() or (_ for _ in ()).throw(RuntimeError("Redis SAVE 后未找到 dump.rdb")); shutil.copy2(source, target)'
  docker run -d --name "$TEMP_REDIS_CONTAINER" \
    -v "$TEMP_REDIS_VOLUME:/data" \
    "$FLORI_REDIS_IMAGE" redis-server --appendonly no >/dev/null
  while ! docker exec "$TEMP_REDIS_CONTAINER" redis-cli ping >/dev/null 2>&1; do
    [ "$elapsed" -lt "$REDIS_MATERIALIZE_TIMEOUT" ] || {
      echo "错误: Redis RDB 临时实例启动超时" >&2
      return 1
    }
    sleep 1
    elapsed=$((elapsed + 1))
  done
  docker exec "$TEMP_REDIS_CONTAINER" redis-cli CONFIG SET appendonly yes >/dev/null
  elapsed=0
  while :; do
    info="$(docker exec "$TEMP_REDIS_CONTAINER" redis-cli INFO persistence | tr -d '\r')"
    if printf '%s\n' "$info" | grep -Fqx 'aof_enabled:1' \
      && printf '%s\n' "$info" | grep -Fqx 'aof_rewrite_in_progress:0' \
      && printf '%s\n' "$info" | grep -Fqx 'aof_last_bgrewrite_status:ok'; then
      break
    fi
    [ "$elapsed" -lt "$REDIS_MATERIALIZE_TIMEOUT" ] || {
      echo "错误: Redis RDB 转换 AOF 超时" >&2
      return 1
    }
    sleep 1
    elapsed=$((elapsed + 1))
  done
  if ! docker exec "$TEMP_REDIS_CONTAINER" redis-cli SHUTDOWN NOSAVE >/dev/null 2>&1; then
    docker stop -t 10 "$TEMP_REDIS_CONTAINER" >/dev/null || return 1
  fi
  docker wait "$TEMP_REDIS_CONTAINER" >/dev/null
  docker rm "$TEMP_REDIS_CONTAINER" >/dev/null
  TEMP_REDIS_CONTAINER=""
  REDIS_MODE="materialized-rdb-aof"
}

if [ -n "$REDIS_DATA_DIR" ]; then
  REDIS_DATA_DIR="$(absolute_existing_dir "$REDIS_DATA_DIR")"
  REDIS_SOURCE_MOUNT="$REDIS_DATA_DIR"
else
  docker volume inspect "$REDIS_VOLUME" >/dev/null 2>&1 || {
    echo "错误: Redis 卷不存在: $REDIS_VOLUME" >&2
    exit 1
  }
  REDIS_SOURCE_MOUNT="$REDIS_VOLUME"
fi
if [ "$REDIS_MODE" = "rdb" ]; then
  echo "==> 将本代 Redis RDB 转换为生产可直接加载的 AOF"
  materialize_redis_aof "$REDIS_SOURCE_MOUNT"
  REDIS_SOURCE_MOUNT="$TEMP_REDIS_VOLUME"
fi
DOCKER_ARGS+=(-v "$REDIS_SOURCE_MOUNT:/source-redis:ro")
CREATE_ARGS+=(--redis-mode "$REDIS_MODE")

MINIO_INCLUDED=0
if [ -n "$MINIO_DATA_DIR" ]; then
  MINIO_DATA_DIR="$(absolute_existing_dir "$MINIO_DATA_DIR")"
  DOCKER_ARGS+=(-v "$MINIO_DATA_DIR:/source-minio:ro")
  CREATE_ARGS+=(--minio /source-minio)
  MINIO_INCLUDED=1
elif docker volume inspect "$MINIO_VOLUME" >/dev/null 2>&1; then
  DOCKER_ARGS+=(-v "$MINIO_VOLUME:/source-minio:ro")
  CREATE_ARGS+=(--minio /source-minio)
  MINIO_INCLUDED=1
elif [ "$MINIO_REQUIRED" = "1" ]; then
  echo "错误: MINIO_REQUIRED=1 但未找到 $MINIO_VOLUME" >&2
  exit 1
fi
if [ "$MINIO_REQUIRED" != "auto" ] && [ "$MINIO_REQUIRED" != "0" ] && [ "$MINIO_REQUIRED" != "1" ]; then
  echo "错误: MINIO_REQUIRED 只允许 auto、0 或 1" >&2
  exit 1
fi
if [ "$MINIO_INCLUDED" -eq 1 ] && [ -n "$FLORI_DATA_DIR" ] && [ -n "$MINIO_DATA_DIR" ]; then
  DATA_PREFIX="${FLORI_DATA_DIR%/}/"
  case "$MINIO_DATA_DIR" in
    "$DATA_PREFIX"*)
      MINIO_RELATIVE="${MINIO_DATA_DIR#"$DATA_PREFIX"}"
      CREATE_ARGS+=(--data-exclude "${MINIO_RELATIVE%%/*}")
      ;;
  esac
fi

if [ -n "$FLORI_CONFIG_DIR" ]; then
  FLORI_CONFIG_DIR="$(absolute_existing_dir "$FLORI_CONFIG_DIR")"
  DOCKER_ARGS+=(-v "$FLORI_CONFIG_DIR:/source-config:ro")
  CREATE_ARGS+=(--config /source-config)
fi

APP_VERSION="${FLORI_VERSION:-}"
if [ -z "$APP_VERSION" ] && [ -f "$REPO/pyproject.toml" ]; then
  APP_VERSION="$(sed -n 's/^version = "\([^"]*\)"/\1/p' "$REPO/pyproject.toml" | head -1)"
fi
CREATE_ARGS+=(--app-version "${APP_VERSION:-unknown}")

echo "==> Flori 完整灾备快照开始"
echo "    generation: $BACKUP_GENERATION"
echo "    data: $DATA_LABEL"
echo "    redis: $REDIS_MODE"
echo "    minio: $([ "$MINIO_INCLUDED" -eq 1 ] && echo included || echo not-configured)"
echo "    output: $ARCHIVE"

docker "${DOCKER_ARGS[@]}" "$FLORI_DR_IMAGE" "${CREATE_ARGS[@]}"

echo "==> 备份已原子发布"
echo "    archive: $ARCHIVE"
echo "    checksum: $ARCHIVE.sha256"
echo "    result: $BACKUP_RESULT_FILE"
echo "    restore: scripts/restore.sh '$ARCHIVE' --yes"
