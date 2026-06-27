#!/usr/bin/env bash
# 把镜像从 NAS 直推到边缘机(如 ECS),绕开边缘直连镜像仓库过慢的问题。
#
# 背景:边缘机直连 ghcr 往往只有 KB/s 级,Watchtower 在边缘拉不动大镜像;而
# NAS↔边缘内网/直连通常 MB/s 级。故 NAS 先 pull,再 save|gzip 经 ssh 推到边缘
# docker load,最后在边缘 compose 重建——一条命令完成发布。
#
# 用法: scripts/push-to-edge.sh <frontend|worker|all|resume-watchtower>
#   worker/all 会【先暂停边缘 Watchtower】:worker 容器带 watchtower.enable=true,直传后会被旧 ghcr :latest
#     在下一次 120s 轮询回退覆盖(横跳)。即时部署窗口先暂停;CI 绿(ghcr=同代码)后 `resume-watchtower` 恢复跟随。
#   frontend 已 enable=false(不归 Watchtower 管),不暂停。详见 CLAUDE.md §开发/测试/交付节奏。
# 配置(环境变量,或写入 gitignored .env):
#   EDGE_HOST          边缘机 ssh 主机(IP 或域名)
#   EDGE_SSH_KEY       ssh 私钥路径(相对仓库根或绝对)
#   EDGE_COMPOSE_DIR   边缘 compose 目录(默认 /opt/flori-edge)
#   IMAGE_OWNER        镜像仓库前缀(默认 ghcr.io/gwzlchn)
set -euo pipefail
cd "$(dirname "$0")/.."

# 仅从 .env 取需要的键(不 source 整个 .env,避免含特殊字符的密钥值出错)。
_load() {
  local k="$1" v=""
  eval "v=\${$k:-}"
  if [ -z "$v" ] && [ -f .env ]; then
    v="$(grep -E "^${k}=" .env | head -1 | cut -d= -f2- | sed 's/[[:space:]]*#.*$//' | xargs 2>/dev/null || true)"
  fi
  printf '%s' "$v"
}

EDGE_HOST="$(_load EDGE_HOST)"
EDGE_SSH_KEY="$(_load EDGE_SSH_KEY)"
EDGE_COMPOSE_DIR="$(_load EDGE_COMPOSE_DIR)"; EDGE_COMPOSE_DIR="${EDGE_COMPOSE_DIR:-/opt/flori-edge}"
IMAGE_OWNER="$(_load IMAGE_OWNER)"; IMAGE_OWNER="${IMAGE_OWNER:-ghcr.io/gwzlchn}"
TARGET="${1:-}"

[ -n "$EDGE_HOST" ]    || { echo "✗ EDGE_HOST 未设(env 或 .env)"; exit 1; }
[ -n "$EDGE_SSH_KEY" ] || { echo "✗ EDGE_SSH_KEY 未设"; exit 1; }
[ -f "$EDGE_SSH_KEY" ] || { echo "✗ EDGE_SSH_KEY 文件不存在: $EDGE_SSH_KEY"; exit 1; }
[ -n "$TARGET" ]       || { echo "用法: $0 <frontend|worker|all|resume-watchtower>"; exit 1; }

SSH="ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 -i $EDGE_SSH_KEY root@$EDGE_HOST"

push_image() {  # $1=image ref
  echo ">> NAS pull $1"
  HTTPS_PROXY= HTTP_PROXY= ALL_PROXY= docker pull "$1" >/dev/null
  echo ">> 推送到边缘 (save|gzip|ssh load)…"
  docker save "$1" | gzip -1 | $SSH 'gunzip | docker load' | tail -1
}

recreate() {  # $1=compose 文件  $2..=服务名
  local file="$1"; shift
  echo ">> 边缘重建: docker compose -f $file up -d --force-recreate $*"
  $SSH "cd $EDGE_COMPOSE_DIR && docker compose -f $file up -d --force-recreate $*"
}

# 边缘 Watchtower 暂停/恢复:worker 走 SSH 直传(本地镜像),但 worker 容器带 watchtower.enable=true,
# 下次 120s 轮询会被 ghcr 旧 :latest 回退覆盖。即时部署窗口先暂停,CI 绿后 resume(见 CLAUDE.md §交付节奏)。
edge_watchtower() { $SSH "docker ps --format '{{.Names}}' | grep -i watchtower | head -1" 2>/dev/null || true; }
pause_watchtower() {
  local wt; wt="$(edge_watchtower)"
  if [ -n "$wt" ]; then
    echo ">> 暂停边缘 Watchtower ($wt):防其用旧 ghcr 回退覆盖本次直传的 worker"
    $SSH "docker pause $wt" >/dev/null 2>&1 || true
    echo "   ★ CI 绿(ghcr=同代码)后恢复跟随: scripts/push-to-edge.sh resume-watchtower"
  else
    echo ">> (边缘无 watchtower 容器,跳过暂停)"
  fi
}
resume_watchtower() {
  local wt; wt="$(edge_watchtower)"
  if [ -n "$wt" ]; then echo ">> 恢复边缘 Watchtower ($wt)"; $SSH "docker unpause $wt" >/dev/null 2>&1 || true
  else echo ">> (边缘无 watchtower 容器)"; fi
}

do_frontend() { push_image "$IMAGE_OWNER/flori-frontend:latest"; recreate docker-compose.yml frontend; }
do_worker()   { pause_watchtower; push_image "$IMAGE_OWNER/flori-worker:latest"; recreate worker.yml worker-cpu worker-ai; }

case "$TARGET" in
  frontend) do_frontend ;;
  worker)   do_worker ;;
  all)      do_frontend; do_worker ;;
  resume-watchtower) resume_watchtower; exit 0 ;;
  *) echo "✗ 未知目标: $TARGET (frontend|worker|all|resume-watchtower)"; exit 1 ;;
esac

echo ">> 完成。边缘当前镜像:"
$SSH "docker ps --format '{{.Names}} {{.Image}} {{.Status}}' | grep -iE 'frontend|worker' || true"
