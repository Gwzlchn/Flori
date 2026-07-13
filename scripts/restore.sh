#!/usr/bin/env bash
# 在完整校验后以两阶段、可回滚切换恢复 Flori 灾备快照.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
COMPOSE_PROJECT="${COMPOSE_PROJECT:-flori}"
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
RESTORE_CONFIG_DIR="${RESTORE_CONFIG_DIR:-}"
FLORI_MAX_DB_USER_VERSION="${FLORI_MAX_DB_USER_VERSION:-}"
FLORI_DR_IMAGE="${FLORI_DR_IMAGE:-python:3.11-slim}"
RESTORE_RESULT_FILE="${RESTORE_RESULT_FILE:-}"

ARCHIVE=""
ASSUME_YES=0
DO_STOP=1
DO_RESTART=0
CHECK_ONLY=0

usage() {
  cat <<'EOF'
用法: scripts/restore.sh <备份.tar.gz> [--yes] [--no-stop] [--restart]
       scripts/restore.sh <备份.tar.gz> --check

选项:
  --yes                 跳过交互确认。
  --no-stop             仅隔离空环境演练使用；不检测或停止持有目标卷的容器。
  --restart             恢复成功后重启本脚本停止的容器；默认保持停止以便人工验收。
  --check               只校验归档、校验和、SQLite 完整性与兼容门，不修改目标。
  --result-file <JSON>  写出机器可读的 RTO/校验/资产结果。

恢复协议:
  1. 只读解包并校验成员、sha256、SQLite integrity_check 和版本兼容性。
  2. 默认停止所有持有数据/Redis/MinIO 目标的容器；任一容器停止失败即中止。
  3. 每个目标先写隐藏暂存代，全部准备完成才切换。
  4. 后续目标切换失败时回滚已切换目标；下次恢复会先回收中断暂存。

RESTORE_CONFIG_DIR 为空时，配置资产仍校验但不覆盖镜像管理的 /app/configs。
空环境恢复应显式设置 RESTORE_CONFIG_DIR。
EOF
  exit "${1:-0}"
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    -h|--help) usage 0 ;;
    --yes|-y) ASSUME_YES=1; shift ;;
    --no-stop) DO_STOP=0; shift ;;
    --restart) DO_RESTART=1; shift ;;
    --check) CHECK_ONLY=1; shift ;;
    --result-file)
      [ "$#" -ge 2 ] || usage 1
      RESTORE_RESULT_FILE="$2"
      shift 2
      ;;
    -*) echo "未知选项: $1" >&2; usage 1 ;;
    *)
      [ -z "$ARCHIVE" ] || { echo "多余参数: $1" >&2; usage 1; }
      ARCHIVE="$1"
      shift
      ;;
  esac
done

command -v docker >/dev/null 2>&1 || { echo "错误: 找不到 docker" >&2; exit 1; }
[ -f "$SCRIPT_DIR/dr_snapshot.py" ] || { echo "错误: 缺少 scripts/dr_snapshot.py" >&2; exit 1; }

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
  fi
fi

[ -n "$ARCHIVE" ] || { echo "错误: 需要提供备份归档" >&2; usage 1; }
[ -f "$ARCHIVE" ] || { echo "错误: 备份归档不存在: $ARCHIVE" >&2; exit 1; }
ARCHIVE_DIR="$(cd "$(dirname "$ARCHIVE")" && pwd)"
ARCHIVE_NAME="$(basename "$ARCHIVE")"
ARCHIVE="$ARCHIVE_DIR/$ARCHIVE_NAME"

VALIDATE_ARGS=(python /tool/dr_snapshot.py validate --archive "/archive/$ARCHIVE_NAME")
if [ -n "$FLORI_MAX_DB_USER_VERSION" ]; then
  case "$FLORI_MAX_DB_USER_VERSION" in
    *[!0-9]*|'') echo "错误: FLORI_MAX_DB_USER_VERSION 必须是非负整数" >&2; exit 1 ;;
  esac
  VALIDATE_ARGS+=(--max-db-user-version "$FLORI_MAX_DB_USER_VERSION")
fi

echo "==> 只读校验快照: $ARCHIVE"
VALIDATION_JSON="$(docker run --rm \
  -v "$SCRIPT_DIR/dr_snapshot.py:/tool/dr_snapshot.py:ro" \
  -v "$ARCHIVE_DIR:/archive:ro" \
  "$FLORI_DR_IMAGE" "${VALIDATE_ARGS[@]}")"
echo "$VALIDATION_JSON"

if [ "$CHECK_ONLY" -eq 1 ]; then
  echo "==> 快照校验通过，未修改任何目标"
  exit 0
fi

MINIO_INCLUDED=0
CONFIG_INCLUDED=0
echo "$VALIDATION_JSON" | grep -Eq '"minio": \{[^}]*"included": true' && MINIO_INCLUDED=1 || true
echo "$VALIDATION_JSON" | grep -Eq '"config": \{[^}]*"included": true' && CONFIG_INCLUDED=1 || true

echo ""
echo "!! 即将停止写入者并原子替换持久状态:"
echo "   - data: ${FLORI_DATA_DIR:-volume:$FLORI_DATA_VOLUME}"
echo "   - redis: ${REDIS_DATA_DIR:-volume:$REDIS_VOLUME}"
if [ "$MINIO_INCLUDED" -eq 1 ]; then
  echo "   - minio: ${MINIO_DATA_DIR:-volume:$MINIO_VOLUME}"
fi
if [ "$CONFIG_INCLUDED" -eq 1 ]; then
  echo "   - config: ${RESTORE_CONFIG_DIR:-只校验，不覆盖镜像配置}"
fi
echo ""

if [ "$ASSUME_YES" -ne 1 ]; then
  if [ ! -t 0 ]; then
    echo "错误: 非交互环境且未传 --yes，拒绝恢复" >&2
    exit 1
  fi
  printf '确认恢复并覆盖? 输入大写 YES 继续: '
  read -r answer
  [ "$answer" = "YES" ] || { echo "已取消。"; exit 0; }
fi

prepare_bind_target() {
  local path="$1"
  mkdir -p "$path"
  (cd "$path" && pwd)
}

DOCKER_ARGS=(run --rm
  -v "$SCRIPT_DIR/dr_snapshot.py:/tool/dr_snapshot.py:ro"
  -v "$ARCHIVE_DIR:/archive:ro")
RESTORE_ARGS=(python /tool/dr_snapshot.py restore
  --archive "/archive/$ARCHIVE_NAME"
  --data-target /target-data
  --redis-target /target-redis
  --owner-uid "$(id -u)"
  --owner-gid "$(id -g)")
TARGET_SOURCES=()

if [ -n "$FLORI_DATA_DIR" ]; then
  FLORI_DATA_DIR="$(prepare_bind_target "$FLORI_DATA_DIR")"
  DOCKER_ARGS+=(-v "$FLORI_DATA_DIR:/target-data")
  TARGET_SOURCES+=("$FLORI_DATA_DIR")
else
  docker volume inspect "$FLORI_DATA_VOLUME" >/dev/null 2>&1 || docker volume create "$FLORI_DATA_VOLUME" >/dev/null
  DOCKER_ARGS+=(-v "$FLORI_DATA_VOLUME:/target-data")
  TARGET_SOURCES+=("$FLORI_DATA_VOLUME")
fi

if [ -n "$REDIS_DATA_DIR" ]; then
  REDIS_DATA_DIR="$(prepare_bind_target "$REDIS_DATA_DIR")"
  DOCKER_ARGS+=(-v "$REDIS_DATA_DIR:/target-redis")
  TARGET_SOURCES+=("$REDIS_DATA_DIR")
else
  docker volume inspect "$REDIS_VOLUME" >/dev/null 2>&1 || docker volume create "$REDIS_VOLUME" >/dev/null
  DOCKER_ARGS+=(-v "$REDIS_VOLUME:/target-redis")
  TARGET_SOURCES+=("$REDIS_VOLUME")
fi

if [ "$MINIO_INCLUDED" -eq 1 ]; then
  if [ -n "$MINIO_DATA_DIR" ]; then
    MINIO_DATA_DIR="$(prepare_bind_target "$MINIO_DATA_DIR")"
    DOCKER_ARGS+=(-v "$MINIO_DATA_DIR:/target-minio")
    TARGET_SOURCES+=("$MINIO_DATA_DIR")
  else
    docker volume inspect "$MINIO_VOLUME" >/dev/null 2>&1 || docker volume create "$MINIO_VOLUME" >/dev/null
    DOCKER_ARGS+=(-v "$MINIO_VOLUME:/target-minio")
    TARGET_SOURCES+=("$MINIO_VOLUME")
  fi
  RESTORE_ARGS+=(--minio-target /target-minio)
fi

if [ "$CONFIG_INCLUDED" -eq 1 ] && [ -n "$RESTORE_CONFIG_DIR" ]; then
  RESTORE_CONFIG_DIR="$(prepare_bind_target "$RESTORE_CONFIG_DIR")"
  DOCKER_ARGS+=(-v "$RESTORE_CONFIG_DIR:/target-config")
  RESTORE_ARGS+=(--config-target /target-config)
  TARGET_SOURCES+=("$RESTORE_CONFIG_DIR")
fi

if [ -z "$RESTORE_RESULT_FILE" ]; then
  RESTORE_RESULT_FILE="$ARCHIVE.restore-result.json"
fi
mkdir -p "$(dirname "$RESTORE_RESULT_FILE")"
RESULT_DIR="$(cd "$(dirname "$RESTORE_RESULT_FILE")" && pwd)"
RESULT_NAME="$(basename "$RESTORE_RESULT_FILE")"
DOCKER_ARGS+=(-v "$RESULT_DIR:/result")
RESTORE_ARGS+=(--result-file "/result/$RESULT_NAME")
if [ -n "$FLORI_MAX_DB_USER_VERSION" ]; then
  RESTORE_ARGS+=(--max-db-user-version "$FLORI_MAX_DB_USER_VERSION")
fi

STOPPED_IDS=()
STOPPED_NAMES=()
declare -A TARGET_HOLDERS=()
for source in "${TARGET_SOURCES[@]}"; do
  while IFS= read -r container_id; do
    [ -n "$container_id" ] || continue
    TARGET_HOLDERS["$container_id"]=1
  done < <(docker ps -q --filter "volume=$source")
done
if [ "$DO_STOP" -eq 1 ]; then
  if [ "${#TARGET_HOLDERS[@]}" -gt 0 ]; then
    echo "==> 停止所有持有恢复目标的容器"
    for container_id in "${!TARGET_HOLDERS[@]}"; do
      name="$(docker inspect --format '{{.Name}}' "$container_id")"
      name="${name#/}"
      docker stop "$container_id" >/dev/null || {
        echo "错误: 无法停止容器 $name，恢复未开始" >&2
        exit 1
      }
      STOPPED_IDS+=("$container_id")
      STOPPED_NAMES+=("$name")
      echo "    stopped: $name"
    done
  fi
elif [ "${#TARGET_HOLDERS[@]}" -gt 0 ]; then
  echo "错误: --no-stop 不能绕过停写门；仍有运行容器持有恢复目标" >&2
  exit 1
fi
for source in "${TARGET_SOURCES[@]}"; do
  if [ -n "$(docker ps -q --filter "volume=$source")" ]; then
    echo "错误: 仍有运行容器持有目标 $source，恢复未开始" >&2
    exit 1
  fi
done

echo "==> 预置、二次校验并两阶段切换"
if ! docker "${DOCKER_ARGS[@]}" "$FLORI_DR_IMAGE" "${RESTORE_ARGS[@]}"; then
  echo "错误: 恢复失败；脚本已要求回滚所有已切换目标，容器保持停止" >&2
  exit 1
fi

if [ "$DO_RESTART" -eq 1 ] && [ "${#STOPPED_IDS[@]}" -gt 0 ]; then
  echo "==> 重启恢复前运行的容器"
  docker start "${STOPPED_IDS[@]}" >/dev/null
fi

echo "==> 恢复完成"
echo "    result: $RESTORE_RESULT_FILE"
if [ "$DO_RESTART" -eq 0 ] && [ "${#STOPPED_NAMES[@]}" -gt 0 ]; then
  echo "    已停容器仍保持停止: ${STOPPED_NAMES[*]}"
  echo "    验收后显式执行 docker start ${STOPPED_NAMES[*]}"
fi
