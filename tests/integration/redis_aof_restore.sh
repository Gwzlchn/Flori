#!/usr/bin/env bash
# 验证生产 Redis AOF 与 MinIO 对象可从完整灾备恢复到空环境.
set -euo pipefail

REPO="${FLORI_INTEGRATION_DR_REPO:-$(cd "$(dirname "$0")/../.." && pwd)}"
HOST_REPO="${FLORI_INTEGRATION_DR_HOST_REPO:-$REPO}"
DR_IMAGE="${DOCKER_TEST_IMAGE:?DOCKER_TEST_IMAGE 未设置}"
APP_IMAGE="${FLORI_INTEGRATION_APP_IMAGE:?FLORI_INTEGRATION_APP_IMAGE 未设置}"
REDIS_IMAGE="${FLORI_INTEGRATION_REDIS_IMAGE:?FLORI_INTEGRATION_REDIS_IMAGE 未设置}"
MINIO_IMAGE="${FLORI_INTEGRATION_MINIO_IMAGE:?FLORI_INTEGRATION_MINIO_IMAGE 未设置}"
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
SOURCE_CONFIG="$ROOT/source-config"
TARGET_CONFIG="$ROOT/target-config"
BACKUPS="$ROOT/backups"
SOURCE_REDIS_VOLUME="${SUFFIX}-redis-source"
TARGET_REDIS_VOLUME="${SUFFIX}-redis-target"
SOURCE_MINIO_VOLUME="${SUFFIX}-minio-source"
TARGET_MINIO_VOLUME="${SUFFIX}-minio-target"
NETWORK="${SUFFIX}-dr"
SOURCE_CONTAINER="${SUFFIX}-redis-source"
TARGET_CONTAINER="${SUFFIX}-redis-target"
SOURCE_DB_CONTAINER="${SUFFIX}-db-source"
SOURCE_MINIO_CONTAINER="${SUFFIX}-minio-source"
TARGET_MINIO_CONTAINER="${SUFFIX}-minio-target"
GENERATION="integration-aof-$$"
ARCHIVE="$BACKUPS/flori-backup-${GENERATION}.tar.gz"
BACKUP_RESULT="$ROOT/backup-result.json"
RESTORE_RESULT="$ROOT/restore-result.json"
MINIO_ENV_FILE="$ROOT/minio.env"
MINIO_BUCKET="flori-integration"
MINIO_OBJECT="disaster-recovery/multipart-object.bin"
MINIO_METADATA_KEY="flori-dr-check"
MINIO_METADATA_VALUE="metadata-preserved"
MINIO_OBJECT_SIZE=$((6 * 1024 * 1024 + 257))
MINIO_PASSWORD_ENV="MINIO_ROOT_PASSWORD"

random_hex() {
  bytes="$1"
  od -An -N "$bytes" -tx1 /dev/urandom | tr -d ' \n'
}

MINIO_LOGIN="integration-$(random_hex 8)"
MINIO_PROOF="$(random_hex 24)"

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
    docker logs "$SOURCE_MINIO_CONTAINER" >&2 2>/dev/null || true
    docker logs "$TARGET_MINIO_CONTAINER" >&2 2>/dev/null || true
  fi
  docker rm -f \
    "$SOURCE_DB_CONTAINER" "$SOURCE_CONTAINER" "$TARGET_CONTAINER" \
    "$SOURCE_MINIO_CONTAINER" "$TARGET_MINIO_CONTAINER" \
    >/dev/null 2>&1 || true
  docker volume rm \
    "$SOURCE_REDIS_VOLUME" "$TARGET_REDIS_VOLUME" \
    "$SOURCE_MINIO_VOLUME" "$TARGET_MINIO_VOLUME" \
    >/dev/null 2>&1 || true
  docker network rm "$NETWORK" >/dev/null 2>&1 || true
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

wait_for_minio() {
  container="$1"
  docker run --rm -i \
    --network "$NETWORK" \
    -e "MINIO_ENDPOINT=$container:9000" \
    -e "FLORI_MINIO_LOGIN=$MINIO_LOGIN" \
    -e "FLORI_MINIO_PROOF=$MINIO_PROOF" \
    "$APP_IMAGE" python - <<'PY'
import os
import time

from minio import Minio

client = Minio(
    os.environ["MINIO_ENDPOINT"],
    access_key=os.environ["FLORI_MINIO_LOGIN"],
    secret_key=os.environ["FLORI_MINIO_PROOF"],
    secure=False,
)
last_error = None
for _ in range(80):
    try:
        client.list_buckets()
        break
    except Exception as exc:  # noqa: BLE001
        last_error = exc
        time.sleep(0.25)
else:
    raise SystemExit(f"MinIO 启动超时: {type(last_error).__name__}")
PY
}

create_minio_fixture() {
  docker run --rm -i \
    --network "$NETWORK" \
    -e "MINIO_ENDPOINT=$SOURCE_MINIO_CONTAINER:9000" \
    -e "FLORI_MINIO_LOGIN=$MINIO_LOGIN" \
    -e "FLORI_MINIO_PROOF=$MINIO_PROOF" \
    -e "MINIO_BUCKET=$MINIO_BUCKET" \
    -e "MINIO_OBJECT=$MINIO_OBJECT" \
    -e "MINIO_OBJECT_SIZE=$MINIO_OBJECT_SIZE" \
    -e "MINIO_METADATA_KEY=$MINIO_METADATA_KEY" \
    -e "MINIO_METADATA_VALUE=$MINIO_METADATA_VALUE" \
    -v "$ROOT:/evidence" \
    "$APP_IMAGE" python - <<'PY'
import hashlib
import io
import json
import os
from pathlib import Path

from minio import Minio


def fixture_bytes(size: int) -> bytes:
    pattern = b"flori-minio-disaster-recovery-integration\n"
    return (pattern * (size // len(pattern) + 1))[:size]


client = Minio(
    os.environ["MINIO_ENDPOINT"],
    access_key=os.environ["FLORI_MINIO_LOGIN"],
    secret_key=os.environ["FLORI_MINIO_PROOF"],
    secure=False,
)
bucket = os.environ["MINIO_BUCKET"]
object_name = os.environ["MINIO_OBJECT"]
size = int(os.environ["MINIO_OBJECT_SIZE"])
metadata_key = os.environ["MINIO_METADATA_KEY"]
metadata_value = os.environ["MINIO_METADATA_VALUE"]
payload = fixture_bytes(size)
client.make_bucket(bucket)
client.put_object(
    bucket,
    object_name,
    io.BytesIO(payload),
    length=len(payload),
    metadata={metadata_key: metadata_value},
    part_size=5 * 1024 * 1024,
)
stat = client.stat_object(bucket, object_name)
metadata = {str(key).lower(): str(value) for key, value in stat.metadata.items()}
assert stat.size == size
assert "-" in stat.etag
assert metadata[f"x-amz-meta-{metadata_key}"].strip() == metadata_value
Path("/evidence/source-object.json").write_text(
    json.dumps(
        {
            "bucket": bucket,
            "object": object_name,
            "size": size,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "etag": stat.etag,
            "metadata_key": metadata_key,
            "metadata_value": metadata_value,
        },
        sort_keys=True,
    ),
    encoding="utf-8",
)
PY
}

verify_restored_minio_fixture() {
  docker run --rm -i \
    --network "$NETWORK" \
    -e "MINIO_ENDPOINT=$TARGET_MINIO_CONTAINER:9000" \
    -e "FLORI_MINIO_LOGIN=$MINIO_LOGIN" \
    -e "FLORI_MINIO_PROOF=$MINIO_PROOF" \
    -v "$ROOT:/evidence:ro" \
    "$APP_IMAGE" python - <<'PY'
import hashlib
import json
import os
from pathlib import Path

from minio import Minio


def fixture_bytes(size: int) -> bytes:
    pattern = b"flori-minio-disaster-recovery-integration\n"
    return (pattern * (size // len(pattern) + 1))[:size]


expected = json.loads(Path("/evidence/source-object.json").read_text(encoding="utf-8"))
client = Minio(
    os.environ["MINIO_ENDPOINT"],
    access_key=os.environ["FLORI_MINIO_LOGIN"],
    secret_key=os.environ["FLORI_MINIO_PROOF"],
    secure=False,
)
response = client.get_object(expected["bucket"], expected["object"])
try:
    body = response.read()
finally:
    response.close()
    response.release_conn()
stat = client.stat_object(expected["bucket"], expected["object"])
metadata = {str(key).lower(): str(value) for key, value in stat.metadata.items()}
assert body == fixture_bytes(expected["size"])
assert len(body) == expected["size"] == stat.size
assert hashlib.sha256(body).hexdigest() == expected["sha256"]
assert stat.etag == expected["etag"]
key = f'x-amz-meta-{expected["metadata_key"]}'
assert metadata[key].strip() == expected["metadata_value"]
PY
}

verify_minio_backup_manifest() {
  docker run --rm -i \
    -v "$ROOT:/evidence:ro" \
    "$APP_IMAGE" python - <<'PY'
import json
from pathlib import Path

result = json.loads(Path("/evidence/backup-result.json").read_text(encoding="utf-8"))
expected = json.loads(Path("/evidence/source-object.json").read_text(encoding="utf-8"))
manifest = result["manifest"]
asset = manifest["assets"]["minio"]
assert asset["included"] is True
assert asset["capture_mode"] == "stable-filesystem-copy"
assert asset["file_count"] > 0
assert asset["total_bytes"] > 0
prefix = f"assets/minio/{expected['bucket']}/{expected['object']}"
assert any(
    path == prefix or path.startswith(prefix + "/")
    for path in manifest["files"]
)
PY
}

mkdir -p \
  "$SOURCE_DATA/db" "$SOURCE_DATA/jobs/job-integration" \
  "$SOURCE_DATA/prompts/profiles" "$TARGET_DATA" \
  "$SOURCE_CONFIG" "$TARGET_CONFIG" "$BACKUPS"
umask 077
{
  printf 'MINIO_ROOT_USER=%s\n' "$MINIO_LOGIN"
  printf '%s=%s\n' "$MINIO_PASSWORD_ENV" "$MINIO_PROOF"
} > "$MINIO_ENV_FILE"
printf '%s\n' '{"source":"integration"}' > "$SOURCE_DATA/jobs/job-integration/input.json"
printf '%s\n' 'role: integration' > "$SOURCE_DATA/prompts/profiles/integration.yaml"
printf '%s\n' 'pipelines: {}' > "$SOURCE_CONFIG/pipelines.yaml"
docker run -d --name "$SOURCE_DB_CONTAINER" \
  -v "$HOST_REPO/shared:/app/shared:ro" \
  -v "$HOST_REPO/configs:/app/configs:ro" \
  -v "$SOURCE_DATA:/fixture" \
  -w /app \
  "$APP_IMAGE" \
  python -u -c 'import signal; from shared.db import Database; from shared.models import Job; database=Database("/fixture/db/analyzer.db"); database.init_schema(); job=Job(id="job-integration", content_type="article", pipeline="article", title="Redis AOF restore", lineage_key="job-integration"); database.create_job(job); assert database.get_job(job.id).title == job.title; print("database-ready", flush=True); signal.pause()' \
  >/dev/null
wait_for_database

docker network create "$NETWORK" >/dev/null
docker volume create "$SOURCE_REDIS_VOLUME" >/dev/null
docker volume create "$TARGET_REDIS_VOLUME" >/dev/null
docker volume create "$SOURCE_MINIO_VOLUME" >/dev/null
docker run -d --name "$SOURCE_CONTAINER" \
  -v "$SOURCE_REDIS_VOLUME:/data" \
  "$REDIS_IMAGE" redis-server --appendonly yes --save "" >/dev/null
wait_for_redis "$SOURCE_CONTAINER"
docker exec "$SOURCE_CONTAINER" redis-cli SET flori:integration:aof restore-safe >/dev/null
docker exec "$SOURCE_CONTAINER" redis-cli XADD flori:lifecycle '*' \
  topic job_command payload '{"action":"new_job","job_id":"aof-pending"}' \
  emitted_at 1 schema 1 >/dev/null
docker exec "$SOURCE_CONTAINER" redis-cli XGROUP CREATE \
  flori:lifecycle flori:scheduler 0 >/dev/null
docker exec "$SOURCE_CONTAINER" redis-cli XREADGROUP GROUP \
  flori:scheduler scheduler-before-crash COUNT 1 STREAMS flori:lifecycle '>' >/dev/null

docker run -d --name "$SOURCE_MINIO_CONTAINER" \
  --network "$NETWORK" \
  --env-file "$MINIO_ENV_FILE" \
  -v "$SOURCE_MINIO_VOLUME:/data" \
  "$MINIO_IMAGE" server /data --console-address ":9001" >/dev/null
wait_for_minio "$SOURCE_MINIO_CONTAINER"
create_minio_fixture
docker stop -t 10 "$SOURCE_MINIO_CONTAINER" >/dev/null

BACKUP_DIR="$BACKUPS" \
FLORI_DATA_DIR="$SOURCE_DATA" \
REDIS_DATA_DIR= \
REDIS_VOLUME="$SOURCE_REDIS_VOLUME" \
REDIS_CONTAINER="$SOURCE_CONTAINER" \
MINIO_DATA_DIR= \
MINIO_VOLUME="$SOURCE_MINIO_VOLUME" \
MINIO_CONTAINER="$SOURCE_MINIO_CONTAINER" \
MINIO_REQUIRED=1 \
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
verify_minio_backup_manifest
docker rm -f "$SOURCE_DB_CONTAINER" >/dev/null
docker rm -f "$SOURCE_CONTAINER" >/dev/null
docker rm -f "$SOURCE_MINIO_CONTAINER" >/dev/null

if docker volume inspect "$TARGET_MINIO_VOLUME" >/dev/null 2>&1; then
  echo "MinIO 恢复目标卷在恢复前已存在" >&2
  exit 1
fi

FLORI_DATA_DIR="$TARGET_DATA" \
REDIS_DATA_DIR= \
REDIS_VOLUME="$TARGET_REDIS_VOLUME" \
REDIS_CONTAINER= \
MINIO_DATA_DIR= \
MINIO_VOLUME="$TARGET_MINIO_VOLUME" \
MINIO_CONTAINER= \
RESTORE_CONFIG_DIR="$TARGET_CONFIG" \
FLORI_DR_IMAGE="$DR_IMAGE" \
RESTORE_RESULT_FILE="$RESTORE_RESULT" \
  "$REPO/scripts/restore.sh" "$ARCHIVE" --yes --no-stop > "$ROOT/restore.log" 2>&1

grep -F '"atomic_switch": "ok"' "$RESTORE_RESULT" >/dev/null || {
  echo "恢复未通过原子切换校验" >&2
  exit 1
}
docker run -d --name "$TARGET_CONTAINER" \
  -v "$TARGET_REDIS_VOLUME:/data" \
  "$REDIS_IMAGE" redis-server --appendonly yes >/dev/null
wait_for_redis "$TARGET_CONTAINER"
value="$(docker exec "$TARGET_CONTAINER" redis-cli --raw GET flori:integration:aof)"
[ "$value" = "restore-safe" ] || {
  echo "生产 appendonly Redis 未加载恢复 key" >&2
  exit 1
}
docker exec "$TARGET_CONTAINER" redis-cli INFO persistence | tr -d '\r' | grep -Fqx 'aof_enabled:1'
stream_len="$(docker exec "$TARGET_CONTAINER" redis-cli --raw XLEN flori:lifecycle)"
[ "$stream_len" = "1" ] || {
  echo "生命周期 Stream 未从 AOF 恢复" >&2
  exit 1
}
pending="$(docker exec "$TARGET_CONTAINER" redis-cli --raw XPENDING flori:lifecycle flori:scheduler | head -n 1)"
[ "$pending" = "1" ] || {
  echo "生命周期 pending PEL 未从 AOF 恢复" >&2
  exit 1
}

docker run -d --name "$TARGET_MINIO_CONTAINER" \
  --network "$NETWORK" \
  --env-file "$MINIO_ENV_FILE" \
  -v "$TARGET_MINIO_VOLUME:/data" \
  "$MINIO_IMAGE" server /data --console-address ":9001" >/dev/null
wait_for_minio "$TARGET_MINIO_CONTAINER"
verify_restored_minio_fixture

docker run --rm \
  -v "$HOST_REPO/shared:/app/shared:ro" \
  -v "$HOST_REPO/configs:/app/configs:ro" \
  -v "$TARGET_DATA:/fixture" \
  -w /app \
  "$APP_IMAGE" \
  python -c 'from shared.db import Database; database=Database("/fixture/db/analyzer.db"); database.init_schema(); job=database.get_job("job-integration"); assert job is not None and job.title == "Redis AOF restore"; total, jobs=database.list_jobs(current_only=False, limit=10); assert total == 1 and [item.id for item in jobs] == ["job-integration"]; database.close()'

grep -Fqx '{"source":"integration"}' "$TARGET_DATA/jobs/job-integration/input.json"
grep -Fqx 'role: integration' "$TARGET_DATA/prompts/profiles/integration.yaml"
grep -Fqx 'pipelines: {}' "$TARGET_CONFIG/pipelines.yaml"

printf '%s\n' "Redis AOF and MinIO object restore integration: passed"
