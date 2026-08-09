#!/usr/bin/env bash
# 往 worker 家目录 seed CLI 订阅凭证(每 worker 独立副本,各自续期,无并发写冲突)。
#
# 目录布局与 worker/transport.py default_worker_id_file 保持一致。
# - ${FLORI_DATA_DIR}/workers/<worker名>/:该 worker 的家目录,容器内挂为 HOME。
# - worker.id:稳定身份缓存,由 worker 自管,旧平铺文件会自迁移。
# - .claude/.credentials.json:claude 工具 seed,权限 600。
# - .claude/projects/:CLI transcript 目录,用于全轨迹审计。
# - .claude.json:CLI settings 副本,有源文件才 seed。
# - .qoder/.auth/user(+machine_id)与 .qoder/installation_id:qoder 工具 seed,权限 600。
#   只 seed 认证必需文件;projects/logs/file-history 等含其它项目内容,绝不整目录照搬。
# - .codex/auth.json:codex 工具 seed,权限 600。宿主 config.toml 可能放宽沙箱,绝不复制。
#   其它工具的 worker 种子同样进各自 worker home,不按工具建顶层目录。
#
# 用法:
#   scripts/seed-worker-home.sh claude-1 claude-2              # 给指定 worker seed(默认 claude)
#   SEED_TOOLS=qoder scripts/seed-worker-home.sh qoder-1       # seed qoder 凭证
#   SEED_TOOLS=codex scripts/seed-worker-home.sh codex-1       # seed codex 凭证
#   SEED_TOOLS="claude qoder codex" scripts/seed-worker-home.sh w1
#   FORCE=1 scripts/seed-worker-home.sh claude-1               # 已存在也覆盖(默认幂等跳过)
# 环境:
#   FLORI_DATA_DIR   数据根(默认读仓库 .env;NAS=/volume2/DATA/flori)
#   SEED_TOOLS       要 seed 的工具集,空格分隔(默认 claude;可选 claude qoder codex)
#   SRC_CLAUDE_DIR   claude 凭证来源(默认 ~/.claude)
#   SRC_QODER_DIR    qoder 凭证来源(默认 ~/.qoder)
#   SRC_CODEX_DIR    codex 凭证来源(默认 ~/.codex)
# 安全:凭证只落 ${FLORI_DATA_DIR}(永不入 git);目录 700、凭证 600。
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
FLORI_DATA_DIR="${FLORI_DATA_DIR:-$(sed -n 's/^FLORI_DATA_DIR=//p' "$REPO/.env" 2>/dev/null | head -1)}"
SRC="${SRC_CLAUDE_DIR:-$HOME/.claude}"
SRC_QODER="${SRC_QODER_DIR:-$HOME/.qoder}"
SRC_CODEX="${SRC_CODEX_DIR:-$HOME/.codex}"
SEED_TOOLS="${SEED_TOOLS:-claude}"
FORCE="${FORCE:-0}"

[ -n "$FLORI_DATA_DIR" ] || { echo "FLORI_DATA_DIR 未设置(env 或 $REPO/.env)"; exit 1; }
[ $# -ge 1 ] || { echo "用法: scripts/seed-worker-home.sh <worker名…>(如 claude-1 qoder-1)"; exit 1; }

seed_claude=0; seed_qoder=0; seed_codex=0
for tool in $SEED_TOOLS; do
  case "$tool" in
    claude) seed_claude=1 ;;
    qoder)  seed_qoder=1 ;;
    codex)  seed_codex=1 ;;
    *) echo "未知 SEED_TOOLS 工具: $tool(支持 claude qoder codex)"; exit 1 ;;
  esac
done
[ "$seed_claude" = 0 ] || [ -f "$SRC/.credentials.json" ] || { echo "来源凭证不存在: $SRC/.credentials.json"; exit 1; }
[ "$seed_qoder" = 0 ] || [ -f "$SRC_QODER/.auth/user" ] || { echo "来源凭证不存在: $SRC_QODER/.auth/user"; exit 1; }
[ "$seed_codex" = 0 ] || [ -f "$SRC_CODEX/auth.json" ] || { echo "来源凭证不存在: $SRC_CODEX/auth.json"; exit 1; }

for name in "$@"; do
  home="$FLORI_DATA_DIR/workers/$name"
  # 旧平铺布局(该路径是 worker.id 文件):先迁移成目录,seed 先到也不丢 id。
  if [ -f "$home" ]; then
    wid=$(cat "$home"); rm -f "$home"; mkdir -p "$home"; printf '%s' "$wid" > "$home/worker.id"
    echo ">> $name: 旧平铺 id 文件已迁移到 $home/worker.id(id 不变)"
  fi
  mkdir -p "$home"
  chmod 700 "$home"

  if [ "$seed_claude" = 1 ]; then
    mkdir -p "$home/.claude"
    chmod 700 "$home/.claude"
    if [ -f "$home/.claude/.credentials.json" ] && [ "$FORCE" != "1" ]; then
      echo ">> $name: claude 凭证已存在,跳过(FORCE=1 覆盖)"
    else
      cp "$SRC/.credentials.json" "$home/.claude/.credentials.json"
      chmod 600 "$home/.claude/.credentials.json"
      echo ">> $name: claude 凭证已 seed 到 $home/.claude/.credentials.json"
    fi
    # CLI settings($HOME/.claude.json):有源才拷,幂等
    if [ -f "$HOME/.claude.json" ] && { [ ! -f "$home/.claude.json" ] || [ "$FORCE" = "1" ]; }; then
      cp "$HOME/.claude.json" "$home/.claude.json"
      echo ">> $name: claude settings 已 seed 到 $home/.claude.json"
    fi
  fi

  if [ "$seed_qoder" = 1 ]; then
    mkdir -p "$home/.qoder/.auth"
    chmod 700 "$home/.qoder" "$home/.qoder/.auth"
    if [ -f "$home/.qoder/.auth/user" ] && [ "$FORCE" != "1" ]; then
      echo ">> $name: qoder 凭证已存在,跳过(FORCE=1 覆盖)"
    else
      cp "$SRC_QODER/.auth/user" "$home/.qoder/.auth/user"
      chmod 600 "$home/.qoder/.auth/user"
      # 设备标识随凭证同步,缺失可能触发重新登录;有源才拷。
      for extra in .auth/machine_id installation_id; do
        if [ -f "$SRC_QODER/$extra" ]; then
          cp "$SRC_QODER/$extra" "$home/.qoder/$extra"
          chmod 600 "$home/.qoder/$extra"
        fi
      done
      echo ">> $name: qoder 凭证已 seed 到 $home/.qoder/.auth/user"
    fi
    # CLI settings:有源才拷,幂等;不拷 projects/logs 等运行数据。
    for cfg in settings.json state.json; do
      if [ -f "$SRC_QODER/$cfg" ] && { [ ! -f "$home/.qoder/$cfg" ] || [ "$FORCE" = "1" ]; }; then
        cp "$SRC_QODER/$cfg" "$home/.qoder/$cfg"
        echo ">> $name: qoder $cfg 已 seed"
      fi
    done
  fi

  if [ "$seed_codex" = 1 ]; then
    mkdir -p "$home/.codex"
    chmod 700 "$home/.codex"
    if [ -f "$home/.codex/auth.json" ] && [ "$FORCE" != "1" ]; then
      chmod 600 "$home/.codex/auth.json"
      echo ">> $name: codex 凭证已存在,跳过(FORCE=1 覆盖)"
    else
      cp "$SRC_CODEX/auth.json" "$home/.codex/auth.json"
      chmod 600 "$home/.codex/auth.json"
      echo ">> $name: codex 凭证已 seed 到 $home/.codex/auth.json"
    fi
  fi
done
echo ">> 完成。compose 里给该 worker 挂 \${FLORI_DATA_DIR}/workers/<名>:/home/worker + env HOME=/home/worker 即可。"
