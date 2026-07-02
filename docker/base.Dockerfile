# 多 stage 镜像拆分:各后端服务只装自己需要的依赖/系统包,镜像各自精简。
#   common  : python + curl + pip 镜像源 + core 依赖(不含源码)——所有 stage 共享底座
#   scheduler: 仅 core(scheduler/ + tunnel_stats/)—— 无 ffmpeg/nodejs/claude/重 extras,最小
#   api     : +[api,mcp](api/ + mcp_server)—— api 不调 claude,无 ffmpeg/nodejs/claude
#   worker  : +ffmpeg+nodejs+claude-code + [steps,gpu,worker] —— 唯一跑 claude、唯一重镜像
#   test    : 全 pip extras + [test] 依赖,无 ffmpeg/nodejs/claude 二进制、无 cn bake —— 仅给测试。
#             pytest 全程 mock subprocess;opencv/whisper/PyAV 是自带 .so 的 wheel,import 不需系统 ffmpeg。
#             用例对 ffmpeg/claude/node 天然安全,故省去 apt ffmpeg + npm claude-code,镜像更快更小。
#
# 分层铁律(buildcache 命中关键):每个 stage 的源码 COPY 一律放在所有 apt/npm/pip 之后。
#   改源码只重算末尾 COPY 层,apt/npm/pip 依赖层恒命中 registry buildcache,CI 不必每次 push 冷建依赖。
#   源码 COPY(含 shared/)也不能放进 common:那会让子 stage 的 FROM 基底随源码变,依赖层全废重建。
#
# 版本解耦(buildcache 命中关键之二):每次提交 bump pyproject [project].version 会让 `COPY pyproject.toml` 层
#   随之变,下游 pip 依赖层全废冷建。故 CI/build 在构建前把上下文里的 pyproject version 抹成占位 0.0.0(见
#   ci.yml / build-uptest.sh),COPY pyproject 层跨提交稳定,pip 缓存命中。真实语义版本经 build-arg FLORI_VERSION
#   注入(各 stage ENV FLORI_VERSION);shared/version.py 用此 env 覆盖,不读已安装包版本,故显示仍准。
#
# 注:不用 `# syntax=...` 指令(会去 docker.io 拉 frontend 镜像,被 NAS 代理 reset);
#    --mount=type=cache 靠引擎内置 BuildKit frontend 即可(已实测 `docker compose build` 支持)。

# common:共享底座(python + pip 源 + core 依赖,无源码)
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
# 此处只有 pyproject、无源码,装的是纯依赖(空包);模块由各 stage 末尾 COPY + 运行时 WORKDIR /app 提供。
RUN --mount=type=cache,target=/root/.cache/pip pip install "."
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# scheduler:仅 core(调度器 + 通联上报)
FROM common AS scheduler
COPY shared/ shared/
COPY configs/ configs/
COPY scheduler/ scheduler/
COPY tunnel_stats/ tunnel_stats/
ARG FLORI_BUILD_SHA=
ENV FLORI_BUILD_SHA=${FLORI_BUILD_SHA}
ARG FLORI_VERSION=
ENV FLORI_VERSION=${FLORI_VERSION}

# api:+[api,mcp](api + mcp_server),无 claude/ffmpeg。/data/prompts seed(profiles 管理读它)
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
ARG FLORI_VERSION=
ENV FLORI_VERSION=${FLORI_VERSION}

# worker:重镜像 —— ffmpeg(steps 调 ffmpeg/ffprobe + PyAV 解码)+ nodejs/claude-code(claude-cli)
#    + [steps,gpu,worker] + cn_domains bake(net-zone CN 表)+ /data/prompts seed(AI 步读 profiles)
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
ARG FLORI_VERSION=
ENV FLORI_VERSION=${FLORI_VERSION}

# test(普通):纯逻辑单测镜像 —— 仅 [api,worker,mcp,test],无 ffmpeg / 无 [steps] 媒体库(opencv/pymupdf/skimage 等)。
#    跑非 step 测试,即 scheduler/api/shared/db/redis 等绝大多数:app+tests 无任何顶层 import 重库(全惰性 + mock),
#    collection 与运行都不需要。与部署镜像同理拆普通与 worker 两档:普通镜像轻(~350MB,build/load 秒级)。
FROM common AS test
RUN --mount=type=cache,target=/root/.cache/pip pip install ".[api,worker,mcp,test]"
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
ARG FLORI_VERSION=
ENV FLORI_VERSION=${FLORI_VERSION}

# test-worker(重):跑 step/worker 测试(tests/steps/ + tests/test_step_*.py + test_worker.py,真 import
#    opencv/pymupdf/scikit-image/trafilatura/imagehash)。复用现有 [steps] extras,不含 [gpu](测试全 mock 不需)。
#    必须 FROM common 而非 FROM test:否则 test 的源码 COPY 夹在依赖层中间,一改源码就冷重建下面的 [steps] 层
#    (装 opencv/scikit-image 约 100s)。故 apt + 全 pip 装在前、源码 COPY 放最后,改源码只重末层 COPY,
#    apt/[steps] 层恒命中 buildcache。
FROM common AS test-worker
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libgl1 libglib2.0-0 libsm6 libxext6 libxrender1 \
    && rm -rf /var/lib/apt/lists/*
RUN --mount=type=cache,target=/root/.cache/pip pip install ".[api,worker,mcp,test,steps]"
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
ARG FLORI_VERSION=
ENV FLORI_VERSION=${FLORI_VERSION}
