#!/usr/bin/env bash
# 回滚后端镜像到指定标签,重建服务。
# CI 每次提交都打 :latest + :<git-sha>;跟 :latest 的自动滚动出问题时,固定到一个
# 已知良好的 sha 即可(审计 #24:此前无版本固定/回滚路径)。
#
# 用法:
#   scripts/rollback.sh <image-tag|git-sha> [service ...]
# 例:
#   scripts/rollback.sh 76e8705            # 回滚 api/scheduler/worker 到该提交镜像
#   scripts/rollback.sh 76e8705 api        # 只回滚 api
# 环境:
#   IMAGE_OWNER    ghcr 归属(默认 gwzlchn)
#   COMPOSE_FILES  传给 docker compose 的 -f 列表(默认 "-f docker-compose.yml";
#                  有 .local 覆盖时设如 "-f docker-compose.yml -f .local/docker-compose.uptest.yml")
set -euo pipefail

usage() { sed -n '2,17p' "$0"; exit "${1:-0}"; }
if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then usage 0; fi

TAG="${1:?需要镜像标签/sha;见 --help}"; shift || true
OWNER="${IMAGE_OWNER:-gwzlchn}"
IMG="ghcr.io/${OWNER}/mnemo:${TAG}"
read -r -a CF <<< "${COMPOSE_FILES:--f docker-compose.yml}"
SERVICES=("$@")
if [ "${#SERVICES[@]}" -eq 0 ]; then SERVICES=(api scheduler worker-cpu worker-ai); fi

echo ">> 拉取 ${IMG}"
docker pull "${IMG}"
echo ">> 以 IMAGE_TAG=${TAG} 重建: ${SERVICES[*]}"
IMAGE_TAG="${TAG}" docker compose "${CF[@]}" up -d "${SERVICES[@]}"
echo ">> 完成。容器现固定在不可变标签 :${TAG},watchtower 不会再把它滚到 :latest。"
echo ">> 恢复自动更新(回到 :latest): docker compose ${CF[*]} up -d ${SERVICES[*]}"
