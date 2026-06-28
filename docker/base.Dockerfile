# 多 stage 镜像拆分(P2 image-split + 分层提速):各后端服务只装自己需要的依赖/系统包,镜像各自精简。
#   common  : python + curl + pip 镜像源 + core 依赖(【不含源码】)——所有 stage 共享底座
#   scheduler: 仅 core(scheduler/ + tunnel_stats/)—— 无 ffmpeg/nodejs/claude/重 extras,最小
#   api     : +[api,mcp](api/ + mcp_server)—— Phase1 后 api 不调 claude,无 ffmpeg/nodejs/claude
#   worker  : +ffmpeg+nodejs+claude-code + [steps,gpu,worker] —— 唯一跑 claude、唯一重镜像
#   test    : 全 pip extras + [test] 依赖,【无 ffmpeg/nodejs/claude 二进制、无 cn bake】—— 仅给测试
#             (pytest 全程 mock subprocess;opencv/whisper/PyAV 是自带 .so 的 wheel,import 不需系统 ffmpeg。
#              已审计:1570 用例对 ffmpeg/claude/node 天然安全,故省去 apt ffmpeg + npm claude-code → 更快更小。)
#
# ★分层铁律(buildcache 命中关键):每个 stage 的【源码 COPY 一律放在所有 apt/npm/pip 之后】。
#   改源码 → 只重算末尾 COPY 层,apt/npm/pip 依赖层恒命中 registry buildcache → CI 不再每次 push 冷建依赖。
#   (旧版把 COPY shared/ 放进 common,任何 shared/ 改动都让子 stage 的 FROM common 基底变 → 依赖层全废重建。)
#
# 注:不用 `# syntax=...` 指令(会去 docker.io 拉 frontend 镜像,被 NAS 代理 reset);
#    --mount=type=cache 靠引擎内置 BuildKit frontend 即可(已实测 `docker compose build` 支持)。

# ── common:共享底座(python + pip 源 + core 依赖,无源码)──
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
# 此处只有 pyproject(无源码)→ 装的是【纯依赖】(空包);模块由各 stage 末尾 COPY + 运行时 WORKDIR /app 提供。
RUN --mount=type=cache,target=/root/.cache/pip pip install "."
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# ── scheduler:仅 core(调度器 + 通联上报)──
FROM common AS scheduler
COPY shared/ shared/
COPY configs/ configs/
COPY scheduler/ scheduler/
COPY tunnel_stats/ tunnel_stats/
ARG FLORI_BUILD_SHA=
ENV FLORI_BUILD_SHA=${FLORI_BUILD_SHA}

# ── api:+[api,mcp](api + mcp_server),无 claude/ffmpeg。/data/prompts seed(profiles 管理读它)──
FROM common AS api
RUN --mount=type=cache,target=/root/.cache/pip pip install ".[api,mcp]"
COPY shared/ shared/
COPY configs/ configs/
COPY api/ api/
# prompts_dir 运行时 = /data/prompts(config.data_dir/'prompts');api 的 /api/profiles 读 profiles。
# 生产 /data 是命名卷,首建空卷时被 seed,之后持久化(rebuild 不覆盖卷内旧内容,需手动同步)。
COPY configs/prompts/ /data/prompts/
ARG FLORI_BUILD_SHA=
ENV FLORI_BUILD_SHA=${FLORI_BUILD_SHA}

# ── worker:重镜像 —— ffmpeg(steps 调 ffmpeg/ffprobe + PyAV 解码)+ nodejs/claude-code(claude-cli)
#    + [steps,gpu,worker] + cn_domains bake(net-zone CN 表)+ /data/prompts seed(AI 步读 profiles)──
FROM common AS worker
ARG USE_USTC_MIRROR=1
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg nodejs npm \
    && rm -rf /var/lib/apt/lists/*
# Claude Code CLI:claude-cli provider(订阅出笔记、看帧图)需要 `claude` 在 PATH。npm 缓存走 cache mount。
RUN --mount=type=cache,target=/root/.npm \
    if [ "$USE_USTC_MIRROR" = "1" ]; then npm config set registry https://registry.npmmirror.com; fi \
    && npm install -g @anthropic-ai/claude-code
RUN --mount=type=cache,target=/root/.cache/pip pip install ".[steps,gpu,worker]"
# net-zone CN 域名表:构建时从 GitHub 上游(felixonmars/dnsmasq-china-list)拉取,不自维护 → /app/data/cn_domains.txt
# (运行时 shared.net_zone 只读不拉)。只用 curl、不依赖应用源码 → 放在 COPY 源码之前(改源码不重新联网拉)。
# 国内(=1)优先 gitee(~4s),jsdelivr/ghproxy 兜底;海外(=0)走 github raw。
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
COPY shared/ shared/
COPY configs/ configs/
COPY steps/ steps/
COPY worker/ worker/
COPY configs/prompts/ /data/prompts/
ARG FLORI_BUILD_SHA=
ENV FLORI_BUILD_SHA=${FLORI_BUILD_SHA}

# ── test:精简测试镜像 —— 全 pip extras + [test] 依赖,仅给测试(docker-compose.test.yml
#    --cov=shared,api,scheduler,worker,steps 需 import 全部模块)。
#    【不装 apt ffmpeg/nodejs + npm claude-code、不烤 cn 表】:pytest -m 'not fuzz' 全程 mock 二进制
#      (已审计 1570 用例;net_zone 测试自带 monkeypatch 临时表),import 重媒体库走自带 .so 的 wheel。
#    [test] 依赖烤进镜像 → compose 跑时不再运行时 pip install(更快、版本随 pyproject 不漂移)。
FROM common AS test
# 测试需要两类系统依赖,【但不装 nodejs + npm claude-code(~900MB 大头 + 慢)】:
#   ① ffmpeg/ffprobe:少数 download/metadata 用例真调 ffprobe(假 mp4 读不出时长、代码能处理空结果),
#      非全 mock → 缺二进制会 FileNotFoundError。装 ffmpeg 给 ffprobe(顺带带齐 PyAV/解码库)。
#   ② opencv-python(cv2,经 scenedetect[opencv])import 需的 X/GL 共享库(libGL/libxcb/libSM 等)。
#   claude/node 则【确实不装】:已审计 1570+ 用例对 claude/node 全程 mock 或走 shutil.which 的 None 分支,无真执行。
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libgl1 libglib2.0-0 libsm6 libxext6 libxrender1 \
    && rm -rf /var/lib/apt/lists/*
RUN --mount=type=cache,target=/root/.cache/pip pip install ".[steps,api,worker,gpu,mcp,test]"
COPY shared/ shared/
COPY configs/ configs/
COPY steps/ steps/
COPY api/ api/
COPY scheduler/ scheduler/
COPY worker/ worker/
COPY tunnel_stats/ tunnel_stats/
COPY configs/prompts/ /data/prompts/
ARG FLORI_BUILD_SHA=
ENV FLORI_BUILD_SHA=${FLORI_BUILD_SHA}
