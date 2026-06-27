# 多 stage 镜像拆分(P2 image-split):各后端服务只装自己需要的依赖/系统包,镜像各自精简。
#   common  : python + curl + pip 镜像源 + core 依赖 + shared/configs(所有 stage 共享底座)
#   scheduler: 仅 core(scheduler/ + tunnel_stats/) —— 无 ffmpeg/nodejs/claude/重 extras,最小
#   api     : +[api,mcp](api/ + mcp_server)—— Phase1 后 api 不调 claude,故无 ffmpeg/nodejs/claude
#   worker  : +ffmpeg+nodejs+claude-code + [steps,gpu,worker](steps/ worker/)—— 唯一跑 claude、唯一重镜像
#   full    : 全 extras + 全 COPY + ffmpeg/claude —— 给测试(--cov 要 import 全部模块);放最后 = 默认 build 目标
# 注:不用 `# syntax=...` 指令(会去 docker.io 拉 frontend 镜像,被 NAS 代理 reset);
#    --mount=type=cache 靠引擎内置 BuildKit frontend 即可(已实测 `docker compose build` 支持)。

# ── common:共享底座 ──
FROM python:3.11-slim AS common
# 默认 USTC 镜像源(国内构建快);海外 CI runner 传 --build-arg USE_USTC_MIRROR=0 用官方源。
ARG USE_USTC_MIRROR=1
RUN if [ "$USE_USTC_MIRROR" = "1" ]; then \
        sed -i 's|deb.debian.org|mirrors.ustc.edu.cn|g' /etc/apt/sources.list.d/debian.sources; \
    fi \
    && apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*
RUN if [ "$USE_USTC_MIRROR" = "1" ]; then \
        pip config set global.index-url https://mirrors.ustc.edu.cn/pypi/web/simple; \
    fi
WORKDIR /app
COPY pyproject.toml .
# core 依赖([project].dependencies)装在 common,子 stage 共享此层;各 stage 再 pip 加自己的 extras。
# pip 走 BuildKit cache mount(复用 wheel,版本 bump 冲层也秒级,不重下);故去掉 --no-cache-dir。
RUN --mount=type=cache,target=/root/.cache/pip pip install "."
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
COPY shared/ shared/
COPY configs/ configs/

# ── scheduler:仅 core(调度器 + 通联上报) ──
FROM common AS scheduler
COPY scheduler/ scheduler/
COPY tunnel_stats/ tunnel_stats/
ARG FLORI_BUILD_SHA=
ENV FLORI_BUILD_SHA=${FLORI_BUILD_SHA}

# ── api:+[api,mcp](api + mcp_server),无 claude/ffmpeg。/data/prompts seed(profiles 管理读它) ──
FROM common AS api
RUN --mount=type=cache,target=/root/.cache/pip pip install ".[api,mcp]"
COPY api/ api/
# prompts_dir 运行时 = /data/prompts(config.data_dir/'prompts');api 的 /api/profiles 读 profiles。
# 生产 /data 是命名卷,首建空卷时被 seed,之后持久化(rebuild 不覆盖卷内旧内容,需手动同步)。
COPY configs/prompts/ /data/prompts/
ARG FLORI_BUILD_SHA=
ENV FLORI_BUILD_SHA=${FLORI_BUILD_SHA}

# ── worker:重镜像 —— ffmpeg(steps 调 ffmpeg/ffprobe + PyAV 解码)+ nodejs/claude-code(claude-cli)
#    + [steps,gpu,worker] + cn_domains bake(放 COPY 源码前以利缓存)+ /data/prompts seed(AI 步读 profiles) ──
FROM common AS worker
ARG USE_USTC_MIRROR=1
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg nodejs npm \
    && rm -rf /var/lib/apt/lists/*
# Claude Code CLI:claude-cli provider(订阅出笔记、看帧图)需要 `claude` 在 PATH。npm 缓存走 cache mount。
RUN --mount=type=cache,target=/root/.npm \
    if [ "$USE_USTC_MIRROR" = "1" ]; then npm config set registry https://registry.npmmirror.com; fi \
    && npm install -g @anthropic-ai/claude-code
# net-zone CN 域名表:只用 curl、不依赖应用源码 → 放在 COPY 源码之前(cn-reorder,改源码不重新联网拉)。
# 【构建时从 GitHub 上游(felixonmars/dnsmasq-china-list)拉取,不自维护】→ /app/data/cn_domains.txt
# (运行时 shared.net_zone 只读不拉)。国内(=1)优先 gitee(~4s),jsdelivr/ghproxy 兜底;海外(=0)走 github raw。
RUN mkdir -p /app/data \
    && CN_RAW="https://raw.githubusercontent.com/felixonmars/dnsmasq-china-list/master/accelerated-domains.china.conf" \
    && CN_GITEE="https://gitee.com/felixonmars/dnsmasq-china-list/raw/master/accelerated-domains.china.conf" \
    && CN_JSD="https://cdn.jsdelivr.net/gh/felixonmars/dnsmasq-china-list@master/accelerated-domains.china.conf" \
    && CN_GHP="https://ghproxy.net/${CN_RAW}" \
    && if [ "$USE_USTC_MIRROR" = "1" ]; then ORDER="$CN_GITEE $CN_JSD $CN_GHP $CN_RAW"; else ORDER="$CN_RAW $CN_JSD"; fi \
    && for u in $ORDER; do curl -fsSL --retry 2 --max-time 90 "$u" -o /tmp/cn.conf && break || true; done; \
       sed -n 's#^server=/\([^/]*\)/.*#\1#p' /tmp/cn.conf 2>/dev/null | sort -u > /app/data/cn_domains.txt || true; \
       echo "cn_domains baked: $(wc -l < /app/data/cn_domains.txt 2>/dev/null || echo 0) domains"
# 注:net-zone 探针 URL(NET_PROBE_CN/NET_PROBE_GLOBAL)是部署/启动配置,不烤进镜像——由 compose worker env 注入。
RUN --mount=type=cache,target=/root/.cache/pip pip install ".[steps,gpu,worker]"
COPY steps/ steps/
COPY worker/ worker/
COPY configs/prompts/ /data/prompts/
ARG FLORI_BUILD_SHA=
ENV FLORI_BUILD_SHA=${FLORI_BUILD_SHA}

# ── full:全模块全依赖,仅给测试(docker-compose.test.yml --cov=shared,api,scheduler,worker,steps 需 import 全部)──
FROM common AS full
ARG USE_USTC_MIRROR=1
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg nodejs npm \
    && rm -rf /var/lib/apt/lists/*
RUN --mount=type=cache,target=/root/.npm \
    if [ "$USE_USTC_MIRROR" = "1" ]; then npm config set registry https://registry.npmmirror.com; fi \
    && npm install -g @anthropic-ai/claude-code
RUN mkdir -p /app/data \
    && CN_RAW="https://raw.githubusercontent.com/felixonmars/dnsmasq-china-list/master/accelerated-domains.china.conf" \
    && CN_GITEE="https://gitee.com/felixonmars/dnsmasq-china-list/raw/master/accelerated-domains.china.conf" \
    && CN_JSD="https://cdn.jsdelivr.net/gh/felixonmars/dnsmasq-china-list@master/accelerated-domains.china.conf" \
    && CN_GHP="https://ghproxy.net/${CN_RAW}" \
    && if [ "$USE_USTC_MIRROR" = "1" ]; then ORDER="$CN_GITEE $CN_JSD $CN_GHP $CN_RAW"; else ORDER="$CN_RAW $CN_JSD"; fi \
    && for u in $ORDER; do curl -fsSL --retry 2 --max-time 90 "$u" -o /tmp/cn.conf && break || true; done; \
       sed -n 's#^server=/\([^/]*\)/.*#\1#p' /tmp/cn.conf 2>/dev/null | sort -u > /app/data/cn_domains.txt || true; \
       echo "cn_domains baked: $(wc -l < /app/data/cn_domains.txt 2>/dev/null || echo 0) domains"
RUN --mount=type=cache,target=/root/.cache/pip pip install ".[steps,api,worker,gpu,mcp]"
COPY steps/ steps/
COPY api/ api/
COPY scheduler/ scheduler/
COPY worker/ worker/
COPY tunnel_stats/ tunnel_stats/
COPY configs/prompts/ /data/prompts/
ARG FLORI_BUILD_SHA=
ENV FLORI_BUILD_SHA=${FLORI_BUILD_SHA}
