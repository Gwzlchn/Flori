#!/usr/bin/env bash
# backup.sh — 把 SQLite 库 + Redis 状态打包成带时间戳的 tar.gz 快照。
#
# 设计要点:
#   - 无需停服:通过一次性 alpine 容器以只读方式挂载命名卷,把数据拷出来。
#   - Redis 先尽力 `redis-cli SAVE` 落盘(容器不在则告警跳过),再拷 dump.rdb / appendonly。
#   - 数据目录既支持命名卷,也支持 bind-mount(MNEMO_DATA_DIR=绝对路径)。
#   - 幂等可重复:每次产出独立时间戳文件,不覆盖、不改动源卷。
#
# 用法:
#   scripts/backup.sh [备份目录]
#   BACKUP_DIR=/mnt/backups scripts/backup.sh
#
# 环境变量(均有默认值,通常不用动):
#   BACKUP_DIR        备份输出目录(默认 ./backups,可被第一个位置参数覆盖)
#   COMPOSE_PROJECT   compose 项目名,用于推断命名卷前缀(默认 ai-knowledge-base)
#   MNEMO_DATA_DIR    数据目录;留空=用命名卷,填绝对路径=bind-mount 直接打包该路径
#   MNEMO_DATA_VOLUME 数据命名卷名(默认 ${COMPOSE_PROJECT}_mnemo-data)
#   REDIS_VOLUME      Redis 命名卷名(默认 ${COMPOSE_PROJECT}_redis-data)
#   REDIS_CONTAINER   Redis 容器名(默认 mnemo-redis),用于触发 SAVE

set -euo pipefail

# ── 默认值 ──────────────────────────────────────────────
COMPOSE_PROJECT="${COMPOSE_PROJECT:-ai-knowledge-base}"
BACKUP_DIR="${BACKUP_DIR:-./backups}"
MNEMO_DATA_DIR="${MNEMO_DATA_DIR:-}"
MNEMO_DATA_VOLUME="${MNEMO_DATA_VOLUME:-${COMPOSE_PROJECT}_mnemo-data}"
REDIS_VOLUME="${REDIS_VOLUME:-${COMPOSE_PROJECT}_redis-data}"
REDIS_CONTAINER="${REDIS_CONTAINER:-mnemo-redis}"
ALPINE_IMAGE="${ALPINE_IMAGE:-alpine:3.20}"

usage() {
  sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help) usage 0 ;;
    -*) echo "未知选项: $1" >&2; usage 1 ;;
    *)  BACKUP_DIR="$1"; shift ;;
  esac
done

# ── 前置检查 ────────────────────────────────────────────
command -v docker >/dev/null 2>&1 || { echo "错误: 找不到 docker" >&2; exit 1; }

mkdir -p "$BACKUP_DIR"
# 解析成绝对路径,供 docker -v 挂载(docker 要求绝对路径)。
BACKUP_DIR="$(cd "$BACKUP_DIR" && pwd)"

TS="$(date +%Y%m%d-%H%M%S)"
STAGE="$(mktemp -d "${TMPDIR:-/tmp}/mnemo-backup.XXXXXX")"
# shellcheck disable=SC2064
trap "rm -rf '$STAGE'" EXIT

OUT="$BACKUP_DIR/mnemo-backup-$TS.tar.gz"

echo "==> Mnemo 备份开始 ($TS)"
echo "    输出: $OUT"

# 用一次性 alpine 容器以只读挂载源、读写挂载暂存目录,把 SUBDIR 下内容拷到暂存。
# 参数: <卷或路径> <源类型 volume|bind> <要拷的子目录(相对源,空=整卷)> <暂存子目录名>
copy_from_source() {
  local src="$1" kind="$2" subdir="$3" dest="$4"
  mkdir -p "$STAGE/$dest"
  local mount
  if [ "$kind" = "bind" ]; then
    mount="$src"
  else
    mount="$src"
  fi
  # alpine: 复制 /src/<subdir> 的内容到 /dst(用 cp -a 保留属性);源缺失则静默跳过。
  docker run --rm \
    -v "${mount}:/src:ro" \
    -v "$STAGE/$dest:/dst" \
    "$ALPINE_IMAGE" \
    sh -c "if [ -e \"/src/$subdir\" ]; then cp -a \"/src/$subdir/.\" /dst/ 2>/dev/null || cp -a \"/src/$subdir\" /dst/ 2>/dev/null || true; fi"
}

# ── 1. SQLite DB(/data/db) ─────────────────────────────
echo "==> 备份 SQLite 库 (db/)"
if [ -n "$MNEMO_DATA_DIR" ]; then
  echo "    数据源: bind-mount $MNEMO_DATA_DIR"
  copy_from_source "$MNEMO_DATA_DIR" bind "db" "db"
else
  echo "    数据源: 命名卷 $MNEMO_DATA_VOLUME"
  if ! docker volume inspect "$MNEMO_DATA_VOLUME" >/dev/null 2>&1; then
    echo "    警告: 命名卷 $MNEMO_DATA_VOLUME 不存在,跳过 DB(用 MNEMO_DATA_VOLUME= 指定正确卷名)" >&2
  else
    copy_from_source "$MNEMO_DATA_VOLUME" volume "db" "db"
  fi
fi

# ── 2. Redis(先 SAVE 再拷卷) ───────────────────────────
echo "==> 备份 Redis 状态"
if docker ps --format '{{.Names}}' | grep -qx "$REDIS_CONTAINER"; then
  echo "    触发 $REDIS_CONTAINER 落盘 (redis-cli SAVE)"
  if ! docker exec "$REDIS_CONTAINER" redis-cli SAVE >/dev/null 2>&1; then
    echo "    警告: redis-cli SAVE 失败,继续拷已有 dump(可能不是最新)" >&2
  fi
else
  echo "    警告: 容器 $REDIS_CONTAINER 未运行,跳过 SAVE,直接拷卷上已有 dump" >&2
fi

if ! docker volume inspect "$REDIS_VOLUME" >/dev/null 2>&1; then
  echo "    警告: Redis 卷 $REDIS_VOLUME 不存在,跳过(用 REDIS_VOLUME= 指定正确卷名)" >&2
else
  # Redis 卷整卷拷(含 dump.rdb 和 appendonly 目录/文件)。
  copy_from_source "$REDIS_VOLUME" volume "" "redis"
fi

# ── 3. 元信息 + 打包 ───────────────────────────────────
cat > "$STAGE/MANIFEST.txt" <<EOF
mnemo backup
created_at=$TS
data_source=${MNEMO_DATA_DIR:-volume:$MNEMO_DATA_VOLUME}
redis_volume=$REDIS_VOLUME
members: db/ redis/
EOF

echo "==> 打包"
# 在暂存目录内打包,避免把绝对路径写进 tar。
tar -czf "$OUT" -C "$STAGE" .

if [ ! -s "$OUT" ]; then
  echo "错误: 生成的备份文件为空" >&2
  exit 1
fi

SIZE="$(du -h "$OUT" | cut -f1)"
echo "==> 完成"
echo "    文件: $OUT"
echo "    大小: $SIZE"
echo "    恢复: scripts/restore.sh '$OUT' --yes"
