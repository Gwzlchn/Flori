#!/usr/bin/env bash
# gc-jobs.sh — 单机 LocalStorage 的 job 产物垃圾回收。
#
# 背景:LocalStorage.cleanup 是 no-op,源视频/音频/PDF 等大文件
# 永远堆在 jobs_dir(/data/jobs/<job_id>/input/source.*),磁盘只增不减。
# 本脚本通过一次性容器进卷,按年龄回收;默认只删大源媒体,保留笔记/图等产物。
#
# 安全设计:
#   - 默认 --dry-run:只算、只列、不删。要真删必须显式 --apply。
#   - 永不碰 DB 或非 job 数据:只在 /data/jobs/<job_id>/ 下动手。
#   - --what source 默认只删每个 job 的 input/source.*(大头),保留 notes/assets。
#
# 用法:
#   scripts/gc-jobs.sh                          # 干跑:列出 30 天前的源媒体
#   scripts/gc-jobs.sh --older-than 14 --apply  # 真删 14 天前的源媒体
#   scripts/gc-jobs.sh --what all --apply       # 删整 job 目录(谨慎)
#   scripts/gc-jobs.sh --min-free-gb 50 --apply # 仅当剩余空间 < 50G 才回收
#
# 选项:
#   --older-than DAYS   仅处理 mtime 早于 N 天的(默认 30)
#   --what {source,all} source=只删 input/source.*(默认);all=删整个 job 目录
#   --min-free-gb N     水位线:仅当 /data 剩余空间 < N GiB 才执行(默认不限)
#   --apply             真正删除(不加则 dry-run)
#   -h, --help
#
# 环境变量:
#   COMPOSE_PROJECT     compose 项目名(默认 flori),推断卷名用
#   FLORI_DATA_DIR      数据目录;留空=命名卷,填绝对路径=bind-mount 直接操作
#   FLORI_DATA_VOLUME   数据命名卷(默认 ${COMPOSE_PROJECT}_flori-data)

set -euo pipefail

COMPOSE_PROJECT="${COMPOSE_PROJECT:-flori}"
FLORI_DATA_DIR="${FLORI_DATA_DIR:-}"
FLORI_DATA_VOLUME="${FLORI_DATA_VOLUME:-${COMPOSE_PROJECT}_flori-data}"
ALPINE_IMAGE="${ALPINE_IMAGE:-alpine:3.20}"

OLDER_THAN=30
WHAT="source"
MIN_FREE_GB=""
APPLY=0

usage() {
  sed -n '2,29p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help)     usage 0 ;;
    --older-than)  OLDER_THAN="${2:?--older-than 需要天数}"; shift 2 ;;
    --what)        WHAT="${2:?--what 需要 source|all}"; shift 2 ;;
    --min-free-gb) MIN_FREE_GB="${2:?--min-free-gb 需要数字}"; shift 2 ;;
    --apply)       APPLY=1; shift ;;
    --dry-run)     APPLY=0; shift ;;
    -*) echo "未知选项: $1" >&2; usage 1 ;;
    *)  echo "多余参数: $1" >&2; usage 1 ;;
  esac
done

case "$WHAT" in
  source|all) ;;
  *) echo "错误: --what 只能是 source 或 all" >&2; exit 1 ;;
esac
case "$OLDER_THAN" in
  ''|*[!0-9]*) echo "错误: --older-than 必须是整数天数" >&2; exit 1 ;;
esac

command -v docker >/dev/null 2>&1 || { echo "错误: 找不到 docker" >&2; exit 1; }

# 解析数据源挂载
if [ -n "$FLORI_DATA_DIR" ]; then
  MOUNT_SRC="$FLORI_DATA_DIR"
  echo "==> 数据源: bind-mount $FLORI_DATA_DIR"
else
  MOUNT_SRC="$FLORI_DATA_VOLUME"
  echo "==> 数据源: 命名卷 $FLORI_DATA_VOLUME"
  docker volume inspect "$FLORI_DATA_VOLUME" >/dev/null 2>&1 \
    || { echo "错误: 命名卷 $FLORI_DATA_VOLUME 不存在(用 FLORI_DATA_VOLUME= 指定)" >&2; exit 1; }
fi

MODE="DRY-RUN(不删,加 --apply 才删)"
[ "$APPLY" -eq 1 ] && MODE="APPLY(真删)"
echo "==> 模式: $MODE  | 年龄: >${OLDER_THAN}天  | 范围: $WHAT  | 水位: ${MIN_FREE_GB:-无}GB"

# 在容器内执行回收逻辑
# 把参数透传进 alpine;容器内对 /data/jobs 操作(/data 由命名卷或 bind 挂载提供)。
# 所有判断/删除/统计都在容器里完成,宿主无需有 find/du。
docker run --rm \
  -e GC_OLDER_THAN="$OLDER_THAN" \
  -e GC_WHAT="$WHAT" \
  -e GC_APPLY="$APPLY" \
  -e GC_MIN_FREE_GB="${MIN_FREE_GB:-}" \
  -v "${MOUNT_SRC}:/data" \
  "$ALPINE_IMAGE" \
  sh <<'INNER'
set -eu

JOBS_DIR="/data/jobs"

if [ ! -d "$JOBS_DIR" ]; then
  echo "    jobs 目录不存在: $JOBS_DIR(无可回收)"
  exit 0
fi

# 水位线检查:仅当剩余空间低于阈值才继续。
if [ -n "${GC_MIN_FREE_GB:-}" ]; then
  # df -P 第 4 列是可用块(1K 块);转 GiB。
  AVAIL_KB="$(df -P /data | awk 'NR==2{print $4}')"
  AVAIL_GB=$(( AVAIL_KB / 1024 / 1024 ))
  echo "    /data 剩余: ${AVAIL_GB}GiB  阈值: ${GC_MIN_FREE_GB}GiB"
  if [ "$AVAIL_GB" -ge "$GC_MIN_FREE_GB" ]; then
    echo "    剩余空间高于阈值,跳过回收。"
    exit 0
  fi
  echo "    剩余低于阈值,继续回收。"
fi

# 计算 mtime 阈值用 find -mtime(天)。-mtime +N = 严格早于 N 天前。
MTIME_ARG="+$(( GC_OLDER_THAN - 1 ))"
[ "$GC_OLDER_THAN" -le 0 ] && MTIME_ARG="+0"

TOTAL_BYTES=0
TOTAL_COUNT=0

# 收集候选目标列表(每行一个路径)。
TARGETS="$(mktemp)"
trap 'rm -f "$TARGETS"' EXIT

if [ "$GC_WHAT" = "source" ]; then
  # 只删大源媒体:<job>/input/source.*(mp4/mp3/pdf/html 等),按文件年龄。
  # 严格限定在 jobs/*/input/ 下,绝不触及 db 或其它目录。
  find "$JOBS_DIR" -mindepth 3 -maxdepth 3 -type f \
       -path "$JOBS_DIR/*/input/source.*" -mtime "$MTIME_ARG" \
       > "$TARGETS" 2>/dev/null || true
else
  # 整 job 目录:按目录 mtime。只在 jobs 一级子目录。
  find "$JOBS_DIR" -mindepth 1 -maxdepth 1 -type d -mtime "$MTIME_ARG" \
       > "$TARGETS" 2>/dev/null || true
fi

if [ ! -s "$TARGETS" ]; then
  echo "    无符合条件的回收目标(>${GC_OLDER_THAN}天, $GC_WHAT)。"
  exit 0
fi

while IFS= read -r path; do
  [ -e "$path" ] || continue
  # du -s -k 取目录/文件大小(KiB),换成字节。
  KB="$(du -s -k "$path" 2>/dev/null | awk '{print $1}')"
  [ -n "$KB" ] || KB=0
  BYTES=$(( KB * 1024 ))
  TOTAL_BYTES=$(( TOTAL_BYTES + BYTES ))
  TOTAL_COUNT=$(( TOTAL_COUNT + 1 ))
  HUMAN="$(du -sh "$path" 2>/dev/null | awk '{print $1}')"
  if [ "$GC_APPLY" = "1" ]; then
    rm -rf -- "$path"
    echo "    删除  ${HUMAN:-?}  $path"
  else
    echo "    将删  ${HUMAN:-?}  $path"
  fi
done < "$TARGETS"

# 人类可读的总量(MiB/GiB)。
HUM_TOTAL="$(awk -v b="$TOTAL_BYTES" 'BEGIN{
  if (b>=1073741824) printf "%.2f GiB", b/1073741824;
  else if (b>=1048576) printf "%.2f MiB", b/1048576;
  else printf "%d B", b;
}')"

echo "    ----------------------------------------"
if [ "$GC_APPLY" = "1" ]; then
  echo "    已回收: ${TOTAL_COUNT} 项, ${HUM_TOTAL}"
else
  echo "    可回收: ${TOTAL_COUNT} 项, ${HUM_TOTAL}  (dry-run,未删;加 --apply 执行)"
fi
INNER

echo "==> 完成"
