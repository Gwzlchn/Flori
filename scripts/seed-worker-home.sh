#!/usr/bin/env bash
# 往 worker 家目录 seed claude 订阅凭证(每 worker 独立副本,各自续期,无并发写冲突)。
#
# 目录布局(与 worker/transport.py default_worker_id_file 一致):
#   ${FLORI_DATA_DIR}/workers/<worker名>/     ← 该 worker 的家目录(容器内挂为 HOME)
#     ├── worker.id                            ← 稳定身份缓存(worker 自管,含旧平铺文件自迁移)
#     ├── .claude/.credentials.json            ← 本脚本 seed(chmod 600);CLI transcript 也落
#     │                                          .claude/projects/…(纳管,全轨迹审计从这读)
#     └── .claude.json                         ← 本脚本 seed(CLI settings 副本)
#   未来其它工具的 worker 种子同样进各自 worker home,不按工具建顶层目录。
#
# 用法:
#   scripts/seed-worker-home.sh claude-1 claude-2      # 给指定 worker seed
#   FORCE=1 scripts/seed-worker-home.sh claude-1       # 已存在也覆盖(默认幂等跳过)
# 环境:
#   FLORI_DATA_DIR   数据根(默认读仓库 .env;NAS=/volume2/DATA/flori)
#   SRC_CLAUDE_DIR   凭证来源(默认 ~/.claude)
# 安全:凭证只落 ${FLORI_DATA_DIR}(永不入 git);目录 700、凭证 600。
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
FLORI_DATA_DIR="${FLORI_DATA_DIR:-$(sed -n 's/^FLORI_DATA_DIR=//p' "$REPO/.env" 2>/dev/null | head -1)}"
SRC="${SRC_CLAUDE_DIR:-$HOME/.claude}"
FORCE="${FORCE:-0}"

[ -n "$FLORI_DATA_DIR" ] || { echo "FLORI_DATA_DIR 未设置(env 或 $REPO/.env)"; exit 1; }
[ $# -ge 1 ] || { echo "用法: scripts/seed-worker-home.sh <worker名…>(如 claude-1 claude-2)"; exit 1; }
[ -f "$SRC/.credentials.json" ] || { echo "来源凭证不存在: $SRC/.credentials.json"; exit 1; }

for name in "$@"; do
  home="$FLORI_DATA_DIR/workers/$name"
  # 旧平铺布局(该路径是 worker.id 文件)→ 先迁移成目录(与 worker 启动自迁移同款,seed 先到也不丢 id)
  if [ -f "$home" ]; then
    wid=$(cat "$home"); rm -f "$home"; mkdir -p "$home"; printf '%s' "$wid" > "$home/worker.id"
    echo ">> $name: 旧平铺 id 文件已迁移 → $home/worker.id(id 不变)"
  fi
  mkdir -p "$home/.claude"
  chmod 700 "$home" "$home/.claude"
  if [ -f "$home/.claude/.credentials.json" ] && [ "$FORCE" != "1" ]; then
    echo ">> $name: 凭证已存在,跳过(FORCE=1 覆盖)"
  else
    cp "$SRC/.credentials.json" "$home/.claude/.credentials.json"
    chmod 600 "$home/.claude/.credentials.json"
    echo ">> $name: 凭证已 seed → $home/.claude/.credentials.json"
  fi
  # CLI settings($HOME/.claude.json):有源才拷,幂等
  if [ -f "$HOME/.claude.json" ] && { [ ! -f "$home/.claude.json" ] || [ "$FORCE" = "1" ]; }; then
    cp "$HOME/.claude.json" "$home/.claude.json"
    echo ">> $name: settings 已 seed → $home/.claude.json"
  fi
done
echo ">> 完成。compose 里给该 worker 挂 \${FLORI_DATA_DIR}/workers/<名>:/home/worker + env HOME=/home/worker 即可。"
