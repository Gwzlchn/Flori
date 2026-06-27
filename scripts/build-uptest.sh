#!/usr/bin/env bash
# 本地构建拆分镜像并打 :uptest 标签,供 .local 活栈(IMAGE_TAG=uptest)使用 —— 不依赖 ghcr。
#
# P2 镜像拆分后,后端是三个 target(base.Dockerfile 的 scheduler/api/worker)+ 前端,各出一镜像:
#   flori-scheduler / flori-api / flori-worker / flori-frontend。
#
# 为什么用 `docker compose build` 而非裸 `docker build`:
#   base.Dockerfile 用 BuildKit `--mount=type=cache`(治 pip/npm 重装)。NAS 未装 buildx CLI 插件,
#   裸 `docker build` 走 legacy builder 不识别 cache mount 会挂;`docker compose` 内置 buildkit 即支持。
#
# 冷构建复用 CI 已建层(registry buildcache):每个 service 的 build.cache_from 指向
#   ghcr.io/<owner>/flori-<stage>:buildcache(CI build-push 的 cache-to 已常驻产出)。换机/清缓存后
#   首建即从 ghcr 拉依赖层(pip/apt/npm)而非重算;命中需先 `docker login ghcr.io`(包私有),
#   读不到则 BuildKit 优雅跳过(import 失败非致命),退化为本地层缓存。本地热重建仍秒级(本地层 + cache mount)。
#
# 用法:
#   scripts/build-uptest.sh                 # 建全部 4 个
#   scripts/build-uptest.sh worker frontend # 只建指定(service 名:scheduler/api/worker/frontend)
# 环境:
#   IMAGE_OWNER      ghcr 归属(默认 gwzlchn);TAG 固定 uptest(活栈约定)
#   USE_USTC_MIRROR  1=用 USTC 源(默认),CI/海外置 0
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
OWNER="${IMAGE_OWNER:-gwzlchn}"
TAG="uptest"
USTC="${USE_USTC_MIRROR:-1}"

work="$(mktemp -d)"; trap 'rm -rf "$work"' EXIT
cat > "$work/build.yml" <<YAML
services:
  scheduler:
    build:
      context: ${REPO}
      dockerfile: docker/base.Dockerfile
      target: scheduler
      args: { USE_USTC_MIRROR: "${USTC}" }
      cache_from: [ "type=registry,ref=ghcr.io/${OWNER}/flori-scheduler:buildcache" ]
    image: ghcr.io/${OWNER}/flori-scheduler:${TAG}
  api:
    build:
      context: ${REPO}
      dockerfile: docker/base.Dockerfile
      target: api
      args: { USE_USTC_MIRROR: "${USTC}" }
      cache_from: [ "type=registry,ref=ghcr.io/${OWNER}/flori-api:buildcache" ]
    image: ghcr.io/${OWNER}/flori-api:${TAG}
  worker:
    build:
      context: ${REPO}
      dockerfile: docker/base.Dockerfile
      target: worker
      args: { USE_USTC_MIRROR: "${USTC}" }
      cache_from: [ "type=registry,ref=ghcr.io/${OWNER}/flori-worker:buildcache" ]
    image: ghcr.io/${OWNER}/flori-worker:${TAG}
  frontend:
    build:
      context: ${REPO}/frontend
      dockerfile: Dockerfile
      cache_from: [ "type=registry,ref=ghcr.io/${OWNER}/flori-frontend:buildcache" ]
    image: ghcr.io/${OWNER}/flori-frontend:${TAG}
YAML

echo ">> 构建拆分镜像 → :${TAG}(${*:-scheduler api worker frontend})"
docker compose -f "$work/build.yml" build "$@"

echo ">> 完成,本地镜像:"
docker images --format '  {{.Repository}}:{{.Tag}}\t{{.Size}}' \
  | grep -E "flori-(scheduler|api|worker|frontend):${TAG}" || true
cat <<'TIP'
>> 起/重建活栈(NAS):
   docker compose -f docker-compose.yml -f .local/docker-compose.uptest.yml --env-file .env \
     --profile distributed up -d --scale worker-cpu=0 --scale worker-ai=0
   (.env 须 IMAGE_TAG=uptest)
TIP
