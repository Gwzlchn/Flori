#!/usr/bin/env bash
# restore.sh — 从 backup.sh 产出的 tar.gz 恢复 SQLite 库 + Redis 状态。
#
# 危险操作:会覆盖数据卷里的 db/ 与 Redis 数据。默认要求显式确认。
#
# 设计要点:
#   - 先校验 tar 内含预期成员(db/ 或 redis/),不合格直接退出,绝不动卷。
#   - 必须 --yes 或交互确认才执行(默认安全:只打印计划,不落地)。
#   - 恢复前建议停掉 api/scheduler/worker(脚本会尝试 docker compose stop,失败不致命);
#     恢复后由用户自己 `docker compose up -d`(脚本只打印提示,不自动起)。
#   - 通过一次性 alpine 容器把数据写回命名卷;bind-mount 模式直接写目录。
#
# 用法:
#   scripts/restore.sh <备份文件.tar.gz> [--yes] [--no-stop]
#   scripts/restore.sh ./backups/mnemo-backup-20260620-101500.tar.gz --yes
#
# 选项:
#   --yes        跳过交互确认(无人值守时用)
#   --no-stop    不尝试 docker compose stop(默认会尝试停 api/scheduler/worker)
#
# 环境变量(同 backup.sh):
#   COMPOSE_PROJECT / MNEMO_DATA_DIR / MNEMO_DATA_VOLUME / REDIS_VOLUME

set -euo pipefail

COMPOSE_PROJECT="${COMPOSE_PROJECT:-ai-knowledge-base}"
MNEMO_DATA_DIR="${MNEMO_DATA_DIR:-}"
MNEMO_DATA_VOLUME="${MNEMO_DATA_VOLUME:-${COMPOSE_PROJECT}_mnemo-data}"
REDIS_VOLUME="${REDIS_VOLUME:-${COMPOSE_PROJECT}_redis-data}"
ALPINE_IMAGE="${ALPINE_IMAGE:-alpine:3.20}"

ARCHIVE=""
ASSUME_YES=0
DO_STOP=1

usage() {
  sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help) usage 0 ;;
    --yes|-y)  ASSUME_YES=1; shift ;;
    --no-stop) DO_STOP=0; shift ;;
    -*) echo "未知选项: $1" >&2; usage 1 ;;
    *)
      if [ -z "$ARCHIVE" ]; then ARCHIVE="$1"; else echo "多余参数: $1" >&2; usage 1; fi
      shift ;;
  esac
done

command -v docker >/dev/null 2>&1 || { echo "错误: 找不到 docker" >&2; exit 1; }

[ -n "$ARCHIVE" ] || { echo "错误: 需要提供备份文件路径" >&2; usage 1; }
[ -f "$ARCHIVE" ] || { echo "错误: 备份文件不存在: $ARCHIVE" >&2; exit 1; }
ARCHIVE="$(cd "$(dirname "$ARCHIVE")" && pwd)/$(basename "$ARCHIVE")"

# ── 1. 校验 tar 成员 ───────────────────────────────────
echo "==> 校验备份内容: $ARCHIVE"
MEMBERS="$(tar -tzf "$ARCHIVE" 2>/dev/null || true)"
[ -n "$MEMBERS" ] || { echo "错误: 无法读取 tar(损坏或非 gzip)" >&2; exit 1; }

HAS_DB=0; HAS_REDIS=0
echo "$MEMBERS" | grep -qE '(^|/)db/' && HAS_DB=1
echo "$MEMBERS" | grep -qE '(^|/)redis/' && HAS_REDIS=1

if [ "$HAS_DB" -eq 0 ] && [ "$HAS_REDIS" -eq 0 ]; then
  echo "错误: tar 内未找到 db/ 或 redis/ 成员,这不是 backup.sh 产出的快照,拒绝恢复" >&2
  exit 1
fi
echo "    含 db/    : $([ "$HAS_DB" -eq 1 ] && echo 是 || echo 否)"
echo "    含 redis/ : $([ "$HAS_REDIS" -eq 1 ] && echo 是 || echo 否)"

# ── 2. 确认(默认安全) ─────────────────────────────────
echo ""
echo "!! 即将覆盖以下目标(原数据将丢失):"
if [ -n "$MNEMO_DATA_DIR" ]; then
  echo "   - 数据: bind-mount $MNEMO_DATA_DIR/db"
else
  echo "   - 数据: 命名卷 $MNEMO_DATA_VOLUME (db/)"
fi
echo "   - Redis: 命名卷 $REDIS_VOLUME"
echo ""

if [ "$ASSUME_YES" -ne 1 ]; then
  if [ ! -t 0 ]; then
    echo "错误: 非交互环境且未传 --yes,已中止(默认安全,不自动覆盖)" >&2
    exit 1
  fi
  printf "确认恢复并覆盖? 输入大写 YES 继续: "
  read -r ans
  [ "$ans" = "YES" ] || { echo "已取消。"; exit 0; }
fi

# ── 3. 尽力停服(避免恢复时被写) ───────────────────────
if [ "$DO_STOP" -eq 1 ]; then
  echo "==> 尝试停掉 api/scheduler/worker(避免恢复中被写)"
  if docker compose stop api scheduler worker-cpu worker-ai >/dev/null 2>&1; then
    echo "    已停。"
  else
    echo "    警告: docker compose stop 失败(可能服务名不同/不在此目录),继续恢复" >&2
    echo "    建议先手动停掉写数据的服务再重试。" >&2
  fi
fi

# ── 4. 解包到暂存 ──────────────────────────────────────
STAGE="$(mktemp -d "${TMPDIR:-/tmp}/mnemo-restore.XXXXXX")"
# shellcheck disable=SC2064
trap "rm -rf '$STAGE'" EXIT
tar -xzf "$ARCHIVE" -C "$STAGE"

# 把暂存子目录内容写回目标(命名卷或 bind 目录)。
# 参数: <暂存子目录> <目标卷或路径> <kind volume|bind> <卷内目标子目录(空=卷根)>
restore_into() {
  local stage_sub="$1" target="$2" kind="$3" dst_sub="$4"
  [ -d "$STAGE/$stage_sub" ] || return 0
  if [ "$kind" = "bind" ]; then
    mkdir -p "$target/$dst_sub"
    cp -a "$STAGE/$stage_sub/." "$target/$dst_sub/"
    return 0
  fi
  # 命名卷:不存在则创建(恢复到新机的场景)。
  docker volume inspect "$target" >/dev/null 2>&1 || docker volume create "$target" >/dev/null
  docker run --rm \
    -v "$target:/dst" \
    -v "$STAGE/$stage_sub:/src:ro" \
    "$ALPINE_IMAGE" \
    sh -c "mkdir -p \"/dst/$dst_sub\" && cp -a /src/. \"/dst/$dst_sub/\""
}

# ── 5. 恢复 DB ─────────────────────────────────────────
if [ "$HAS_DB" -eq 1 ]; then
  echo "==> 恢复 SQLite 库 (db/)"
  if [ -n "$MNEMO_DATA_DIR" ]; then
    restore_into db "$MNEMO_DATA_DIR" bind "db"
  else
    restore_into db "$MNEMO_DATA_VOLUME" volume "db"
  fi
fi

# ── 6. 恢复 Redis ──────────────────────────────────────
if [ "$HAS_REDIS" -eq 1 ]; then
  echo "==> 恢复 Redis 状态"
  restore_into redis "$REDIS_VOLUME" volume ""
fi

echo "==> 恢复完成"
echo ""
echo "下一步(请手动执行,脚本不自动起服务):"
echo "    docker compose up -d"
echo "(Redis 启动会从 dump.rdb / appendonly 加载;DB 已就位。)"
