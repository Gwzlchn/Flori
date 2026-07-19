#!/usr/bin/env bash
# Flori 便携内容仓库 GC 入口(设计稿 05 号 §2.14)。
# 三个子命令:
#   --mark    只算可达集合与待清扫清单(只读)
#   --sweep   经过 grace period 后真删;默认仍是 dry-run,加 --apply 才动手
#   --scrub   全量重算 blob/record/snapshot 摘要,核对 refs 与 receipts
#   --break-lock 打印持锁者后强制破锁(仅在确认持有者已死时用)
#
# GC 与 backup 互斥(同一把仓库写锁);import 只读仓库,不受 GC 阻塞。
# 保留集合 = refs(latest + 每月锚点 + 手工 named refs)+ 最近 N 条 receipts 引用。
# 全容器内运行(宿主不装 Python 依赖)。
#
# 用法:
#   scripts/content-gc.sh --repo <dir> --mark [--keep-receipts N] [--result-file <json>]
#   scripts/content-gc.sh --repo <dir> --sweep [--apply] [--grace-days N] [--allow-no-anchor]
#   scripts/content-gc.sh --repo <dir> --scrub [--result-file <json>]
#   scripts/content-gc.sh --repo <dir> --break-lock
set -euo pipefail

IMAGE="${FLORI_CONTENT_GC_IMAGE:-flori:${IMAGE_TAG:-uptest}}"

# 早退也必须留下机器可读结果:自动化按 --result-file 判生死,shell 直接 exit
# 会让调用方读到上一次的陈旧 JSON 或什么都读不到。参数错(usage)同样受这条约束,
# 所以两条早退路径共用它。
write_failure_result() {
  [ -n "$RESULT_FILE" ] || return 0
  mkdir -p "$(dirname "$RESULT_FILE")"
  printf '{\n  "error": "%s",\n  "ok": false\n}\n' \
    "$(printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g')" > "$RESULT_FILE"
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

# --result-file 先于主解析扫一遍:参数错发生在解析途中,等轮到它才赋值就太晚了。
RESULT_FILE=""
for _index in $(seq 1 $#); do
  if [ "${!_index}" = "--result-file" ]; then
    _next=$((_index + 1))
    [ "$_next" -le $# ] && RESULT_FILE="${!_next}"
  fi
done

REPO_DIR=""
MODE=""
KEEP_RECEIPTS=""
GRACE_DAYS=""
APPLY=0
ALLOW_NO_ANCHOR=0

while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help) usage 0 ;;
    --repo) shift; [ $# -gt 0 ] || usage 2; REPO_DIR="$1"; shift ;;
    --mark) MODE="mark"; shift ;;
    --sweep) MODE="sweep"; shift ;;
    --scrub) MODE="scrub"; shift ;;
    --break-lock) MODE="break-lock"; shift ;;
    --apply) APPLY=1; shift ;;
    --allow-no-anchor) ALLOW_NO_ANCHOR=1; shift ;;
    --keep-receipts) shift; [ $# -gt 0 ] || usage 2; KEEP_RECEIPTS="$1"; shift ;;
    --grace-days) shift; [ $# -gt 0 ] || usage 2; GRACE_DAYS="$1"; shift ;;
    --result-file) shift; [ $# -gt 0 ] || usage 2; RESULT_FILE="$1"; shift ;;
    *) echo "未知参数: $1" >&2; usage 2 ;;
  esac
done

[ -n "$REPO_DIR" ] || fail 2 "必须提供 --repo"
# 与 shared.content_gc 对齐:缺子命令是参数错,退出码 2。
[ -n "$MODE" ] || fail 2 "必须选一个子命令 --mark/--sweep/--scrub/--break-lock"
[ -d "$REPO_DIR" ] || fail 1 "仓库目录不存在: $REPO_DIR"
REPO_DIR="$(cd "$REPO_DIR" && pwd)"

for value in "$KEEP_RECEIPTS" "$GRACE_DAYS"; do
  case "$value" in
    ""|*[!0-9]*) [ -z "$value" ] || fail 2 "数值参数必须是非负整数" ;;
  esac
done

# mark/scrub/sweep 预演都只读;真删与破锁要写,必须可写挂载。
if { [ "$MODE" = "sweep" ] && [ "$APPLY" -eq 1 ]; } || [ "$MODE" = "break-lock" ]; then
  MOUNT="$REPO_DIR:/content-repo"
else
  MOUNT="$REPO_DIR:/content-repo:ro"
fi

DOCKER_ARGS=(run --rm -v "$MOUNT")
RESULT_ARGS=()
if [ -n "$RESULT_FILE" ]; then
  mkdir -p "$(dirname "$RESULT_FILE")"
  RESULT_DIR="$(cd "$(dirname "$RESULT_FILE")" && pwd)"
  DOCKER_ARGS+=(-v "$RESULT_DIR:/result")
  RESULT_ARGS=(--result-file "/result/$(basename "$RESULT_FILE")")
fi

CMD=(python -m shared.content_gc --repo /content-repo "--$MODE" "${RESULT_ARGS[@]}")
[ -z "$KEEP_RECEIPTS" ] || CMD+=(--keep-receipts "$KEEP_RECEIPTS")
[ -z "$GRACE_DAYS" ] || CMD+=(--grace-days "$GRACE_DAYS")
[ "$APPLY" -eq 0 ] || CMD+=(--apply)
[ "$ALLOW_NO_ANCHOR" -eq 0 ] || CMD+=(--allow-no-anchor)

exec docker "${DOCKER_ARGS[@]}" "$IMAGE" "${CMD[@]}"
