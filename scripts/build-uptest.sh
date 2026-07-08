#!/usr/bin/env bash
# 本地构建拆分镜像并打 :uptest 标签,供 .local 活栈(IMAGE_TAG=uptest)使用 —— 不依赖 ghcr。
#
# 后端是三个 target(base.Dockerfile 的 scheduler/api/worker)+ 前端,各出一镜像:
#   flori-scheduler / flori-api / flori-worker / flori-frontend。
#
# 为什么用 `docker compose build` 而非裸 `docker build`:
#   base.Dockerfile 用 BuildKit `--mount=type=cache`(治 pip 重装)。NAS 未装 buildx CLI 插件,
#   裸 `docker build` 走 legacy builder 不识别 cache mount 会挂;`docker compose` 内置 buildkit 即支持。
#
# 冷构建复用 CI 已建层(registry buildcache):每个 service 的 build.cache_from 指向
#   ghcr.io/<owner>/flori-<stage>:buildcache(CI build-push 的 cache-to 已常驻产出)。换机/清缓存后
#   首建即从 ghcr 拉依赖层(pip/apt/CLI binary)而非重算;命中需先 `docker login ghcr.io`(包私有),
#   读不到则 BuildKit 优雅跳过(import 失败非致命),退化为本地层缓存。本地热重建仍秒级(本地层 + cache mount)。
#
# 与 CI 一致,本地也把构建上下文里的 pyproject version 抹成 0.0.0,真实运行版本通过
# FLORI_VERSION build-arg 注入。否则每次提交 bump 版本都会让 COPY pyproject.toml 层变化,
# 进而拖垮 worker 的 apt/CLI/pip 依赖缓存。
#
# 用法:
#   scripts/build-uptest.sh                 # 建全部 4 个
#   scripts/build-uptest.sh worker frontend # 只建指定(service 名:scheduler/api/worker/frontend)
# 环境:
#   IMAGE_OWNER      ghcr 归属(默认从 remote.origin.url 推断);TAG 固定 uptest(活栈约定)
#   USE_USTC_MIRROR  1=用 USTC 源(默认),CI/海外置 0
#   FLORI_BUILD_PROXY_HOST  把宿主 loopback 代理透给 build 容器时使用的宿主网关(默认 docker0 IP)
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
TAG="uptest"
USTC="${USE_USTC_MIRROR:-1}"
OWNER="${IMAGE_OWNER:-}"
if [ -z "$OWNER" ]; then
  ORIGIN_URL="$(git -C "$REPO" config --get remote.origin.url || true)"
  OWNER="$(printf '%s\n' "$ORIGIN_URL" | sed -nE 's#.*[:/]([^/:]+)/[^/]+(\.git)?$#\1#p' | head -1)"
fi
if [ -z "$OWNER" ]; then
  echo "IMAGE_OWNER 未设置,且无法从 remote.origin.url 推断 ghcr 归属" >&2
  exit 1
fi
OWNER="${OWNER,,}"
# 真实语义版本,注入镜像 ENV FLORI_VERSION。构建上下文用临时副本,不改宿主 pyproject。
VER="$(sed -n 's/^version = "\(.*\)"/\1/p' "${REPO}/pyproject.toml" | head -1)"
PROXY_HOST="${FLORI_BUILD_PROXY_HOST:-}"
if [ -z "$PROXY_HOST" ]; then
  PROXY_HOST="$(ip -4 addr show docker0 2>/dev/null | sed -n 's/.*inet \([0-9.]*\).*/\1/p' | head -1 || true)"
fi
normalize_proxy() {
  local value="$1"
  if [ -n "$PROXY_HOST" ]; then
    value="${value//:\/\/127.0.0.1:/:\/\/${PROXY_HOST}:}"
    value="${value//:\/\/localhost:/:\/\/${PROXY_HOST}:}"
  fi
  printf '%s' "$value"
}
build_args=()
for proxy_var in HTTP_PROXY HTTPS_PROXY ALL_PROXY NO_PROXY http_proxy https_proxy all_proxy no_proxy; do
  proxy_value="${!proxy_var:-}"
  if [ -n "$proxy_value" ]; then
    proxy_value="$(normalize_proxy "$proxy_value")"
    build_args+=(--build-arg "${proxy_var}=${proxy_value}")
  fi
done

work="$(mktemp -d)"; trap 'rm -rf "$work"' EXIT
ctx="${work}/context"
mkdir -p "$ctx"
rsync -a --delete --exclude-from="${REPO}/.dockerignore" "${REPO}/" "$ctx/"
sed -i 's/^version = .*/version = "0.0.0"/' "${ctx}/pyproject.toml"
# NAS/ACL 复制到 /tmp 后可能变成 0600,非 root worker 会读不了 /app 源码。
find "$ctx" -type d -exec chmod 755 {} +
find "$ctx" -type f -exec chmod 644 {} +
cat > "$work/build.yml" <<YAML
services:
  scheduler:
    build:
      context: ${ctx}
      dockerfile: docker/base.Dockerfile
      target: scheduler
      args: { USE_USTC_MIRROR: "${USTC}", FLORI_VERSION: "${VER}" }
      cache_from: [ "type=registry,ref=ghcr.io/${OWNER}/flori-scheduler:buildcache" ]
    image: ghcr.io/${OWNER}/flori-scheduler:${TAG}
  api:
    build:
      context: ${ctx}
      dockerfile: docker/base.Dockerfile
      target: api
      args: { USE_USTC_MIRROR: "${USTC}", FLORI_VERSION: "${VER}" }
      cache_from: [ "type=registry,ref=ghcr.io/${OWNER}/flori-api:buildcache" ]
    image: ghcr.io/${OWNER}/flori-api:${TAG}
  worker:
    build:
      context: ${ctx}
      dockerfile: docker/base.Dockerfile
      target: worker
      args: { USE_USTC_MIRROR: "${USTC}", FLORI_VERSION: "${VER}" }
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
docker compose -f "$work/build.yml" build "${build_args[@]}" "$@"

echo ">> 完成,本地镜像:"
docker images --format '  {{.Repository}}:{{.Tag}}\t{{.Size}}' \
  | grep -E "flori-(scheduler|api|worker|frontend):${TAG}" || true
cat <<'TIP'
>> 起/重建活栈(NAS):
   docker compose -f docker-compose.yml -f .local/docker-compose.uptest.yml --env-file .env \
     --profile distributed up -d --scale worker-cpu=0 --scale worker-ai=0
   (.env 须 IMAGE_TAG=uptest)
TIP
