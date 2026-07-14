#!/usr/bin/env bash
# 验证运行中 Redis 备份可被生产 appendonly 启动命令直接加载.
set -euo pipefail

REPO="${FLORI_INTEGRATION_DR_REPO:-$(cd "$(dirname "$0")/../.." && pwd)}"
DR_IMAGE="${DOCKER_TEST_IMAGE:?DOCKER_TEST_IMAGE 未设置}"
APP_IMAGE="${FLORI_INTEGRATION_APP_IMAGE:?FLORI_INTEGRATION_APP_IMAGE 未设置}"
REDIS_IMAGE="${FLORI_INTEGRATION_REDIS_IMAGE:?FLORI_INTEGRATION_REDIS_IMAGE 未设置}"
HOST_TMP="${INTEGRATION_HOST_TMP:?INTEGRATION_HOST_TMP 未设置}"

for required in scripts/backup.sh scripts/restore.sh scripts/dr_snapshot.py; do
  [ -f "$REPO/$required" ] || {
    echo "灾备集成缺少 $required" >&2
    exit 1
  }
done

case "${TEST_WARM_NAME:-flori-test}" in
  *[!A-Za-z0-9_.-]*) echo "TEST_WARM_NAME 含非法字符" >&2; exit 1 ;;
esac
SUFFIX="${TEST_WARM_NAME:-flori-test}-$$"
ROOT="$HOST_TMP/redis-aof-restore"
SOURCE_DATA="$ROOT/source-data"
TARGET_DATA="$ROOT/target-data"
SOURCE_MINIO="$ROOT/source-minio-disabled"
TARGET_MINIO="$ROOT/target-minio-disabled"
SOURCE_CONFIG="$ROOT/source-config"
TARGET_CONFIG="$ROOT/target-config"
BACKUPS="$ROOT/backups"
SOURCE_VOLUME="${SUFFIX}-redis-source"
TARGET_VOLUME="${SUFFIX}-redis-target"
SOURCE_CONTAINER="${SUFFIX}-redis-source"
TARGET_CONTAINER="${SUFFIX}-redis-target"
SOURCE_DB_CONTAINER="${SUFFIX}-db-source"
GENERATION="integration-aof-$$"
ARCHIVE="$BACKUPS/flori-backup-${GENERATION}.tar.gz"
BACKUP_RESULT="$ROOT/backup-result.json"
RESTORE_RESULT="$ROOT/restore-result.json"

cleanup() {
  status=$?
  trap - EXIT
  if [ "$status" -ne 0 ]; then
    for log in "$ROOT/backup.log" "$ROOT/restore.log"; do
      [ -f "$log" ] && { echo "==> $(basename "$log")" >&2; tail -200 "$log" >&2; }
    done
    docker logs "$SOURCE_DB_CONTAINER" >&2 2>/dev/null || true
    docker logs "$SOURCE_CONTAINER" >&2 2>/dev/null || true
    docker logs "$TARGET_CONTAINER" >&2 2>/dev/null || true
  fi
  docker rm -f \
    "$SOURCE_DB_CONTAINER" "$SOURCE_CONTAINER" "$TARGET_CONTAINER" \
    >/dev/null 2>&1 || true
  docker volume rm "$SOURCE_VOLUME" "$TARGET_VOLUME" >/dev/null 2>&1 || true
  exit "$status"
}
trap cleanup EXIT

wait_for_redis() {
  container="$1"
  for _ in {1..40}; do
    docker exec "$container" redis-cli ping >/dev/null 2>&1 && return 0
    sleep 0.25
  done
  echo "Redis 启动超时: $container" >&2
  return 1
}

wait_for_database() {
  for _ in {1..40}; do
    docker logs "$SOURCE_DB_CONTAINER" 2>&1 | grep -Fqx 'database-ready' && return 0
    sleep 0.25
  done
  echo "Database 启动超时: $SOURCE_DB_CONTAINER" >&2
  return 1
}

mkdir -p \
  "$SOURCE_DATA/db" "$SOURCE_DATA/jobs/job-integration" \
  "$SOURCE_DATA/prompts/profiles" "$TARGET_DATA" \
  "$SOURCE_MINIO" "$TARGET_MINIO" "$SOURCE_CONFIG" "$TARGET_CONFIG" "$BACKUPS"
printf '%s\n' '{"source":"integration"}' > "$SOURCE_DATA/jobs/job-integration/input.json"
printf '%s\n' 'role: integration' > "$SOURCE_DATA/prompts/profiles/integration.yaml"
printf '%s\n' 'pipelines: {}' > "$SOURCE_CONFIG/pipelines.yaml"
docker run -d --name "$SOURCE_DB_CONTAINER" \
  -v "$REPO/shared:/app/shared:ro" \
  -v "$SOURCE_DATA:/fixture" \
  -w /app \
  "$APP_IMAGE" \
  python -u -c 'import signal; from shared.db import Database; from shared.models import Job; database=Database("/fixture/db/analyzer.db"); database.init_schema(); job=Job(id="job-integration", content_type="article", pipeline="article", title="Redis AOF restore", lineage_key="job-integration"); database.create_job(job); assert database.get_job(job.id).title == job.title; print("database-ready", flush=True); signal.pause()' \
  >/dev/null
wait_for_database

docker volume create "$SOURCE_VOLUME" >/dev/null
docker volume create "$TARGET_VOLUME" >/dev/null
docker run -d --name "$SOURCE_CONTAINER" \
  -v "$SOURCE_VOLUME:/data" \
  "$REDIS_IMAGE" redis-server --appendonly yes --save "" >/dev/null
wait_for_redis "$SOURCE_CONTAINER"
docker exec "$SOURCE_CONTAINER" redis-cli SET flori:integration:aof restore-safe >/dev/null

BACKUP_DIR="$BACKUPS" \
FLORI_DATA_DIR="$SOURCE_DATA" \
REDIS_DATA_DIR= \
REDIS_VOLUME="$SOURCE_VOLUME" \
REDIS_CONTAINER="$SOURCE_CONTAINER" \
MINIO_DATA_DIR= \
MINIO_REQUIRED=0 \
FLORI_CONFIG_DIR="$SOURCE_CONFIG" \
FLORI_DR_IMAGE="$DR_IMAGE" \
FLORI_REDIS_IMAGE="$REDIS_IMAGE" \
REDIS_MATERIALIZE_TIMEOUT=30 \
BACKUP_GENERATION="$GENERATION" \
BACKUP_RESULT_FILE="$BACKUP_RESULT" \
  "$REPO/scripts/backup.sh" "$BACKUPS" > "$ROOT/backup.log" 2>&1

grep -F '"capture_mode": "materialized-rdb-aof"' "$BACKUP_RESULT" >/dev/null || {
  echo "备份未标记 materialized-rdb-aof" >&2
  exit 1
}
docker rm -f "$SOURCE_DB_CONTAINER" >/dev/null
docker rm -f "$SOURCE_CONTAINER" >/dev/null

FLORI_DATA_DIR="$TARGET_DATA" \
REDIS_DATA_DIR= \
REDIS_VOLUME="$TARGET_VOLUME" \
REDIS_CONTAINER= \
MINIO_DATA_DIR= \
RESTORE_CONFIG_DIR="$TARGET_CONFIG" \
FLORI_DR_IMAGE="$DR_IMAGE" \
RESTORE_RESULT_FILE="$RESTORE_RESULT" \
  "$REPO/scripts/restore.sh" "$ARCHIVE" --yes --no-stop > "$ROOT/restore.log" 2>&1

grep -F '"atomic_switch": "ok"' "$RESTORE_RESULT" >/dev/null || {
  echo "恢复未通过原子切换校验" >&2
  exit 1
}
docker run -d --name "$TARGET_CONTAINER" \
  -v "$TARGET_VOLUME:/data" \
  "$REDIS_IMAGE" redis-server --appendonly yes >/dev/null
wait_for_redis "$TARGET_CONTAINER"
value="$(docker exec "$TARGET_CONTAINER" redis-cli --raw GET flori:integration:aof)"
[ "$value" = "restore-safe" ] || {
  echo "生产 appendonly Redis 未加载恢复 key" >&2
  exit 1
}
docker exec "$TARGET_CONTAINER" redis-cli INFO persistence | tr -d '\r' | grep -Fqx 'aof_enabled:1'

docker run --rm \
  -v "$REPO/shared:/app/shared:ro" \
  -v "$TARGET_DATA:/fixture" \
  -w /app \
  "$APP_IMAGE" \
  python -c 'from shared.db import Database; database=Database("/fixture/db/analyzer.db"); database.init_schema(); job=database.get_job("job-integration"); assert job is not None and job.title == "Redis AOF restore"; total, jobs=database.list_jobs(current_only=False, limit=10); assert total == 1 and [item.id for item in jobs] == ["job-integration"]; database.close()'

printf '%s\n' "Redis AOF restore integration: passed"
