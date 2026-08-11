# 多 stage 镜像拆分:各后端服务只装自己需要的依赖/系统包,镜像各自精简。
#   common  : python + curl + pip 镜像源 + core 依赖(不含源码)——所有 stage 共享底座
#   scheduler: core + scheduler惰性服务边界——无 routes/ffmpeg/CLI/重 extras
#   api     : +[api,mcp](api/ + mcp_server)—— api 不调 claude,无 ffmpeg/claude
#   worker-compute: +ffmpeg/poppler/Deno/布局模型 + [steps,gpu,worker],供 cpu/gpu
#   worker-ai-* : core + [worker] + 单个 concrete CLI,不携带媒体与论文计算依赖
#   test    : 全 pip extras + [test] 依赖,无 ffmpeg/claude 二进制、无 cn bake —— 仅给测试。
#             pytest 全程 mock subprocess;opencv/whisper/PyAV 是自带 .so 的 wheel,import 不需系统 ffmpeg。
#             用例对 ffmpeg/claude 天然安全,故省去 apt ffmpeg + claude-code binary,镜像更快更小。
#
# 分层铁律(buildcache 命中关键):每个 stage 的源码 COPY 一律放在所有 apt/pip/CLI binary 之后。
#   改源码只重算末尾 COPY 层,apt/pip/CLI binary 依赖层恒命中 registry buildcache,CI 不必每次 push 冷建依赖。
#   源码 COPY(含 shared/)也不能放进 common:那会让子 stage 的 FROM 基底随源码变,依赖层全废重建。
#
# 版本解耦(buildcache 命中关键之二):发布交付 bump pyproject [project].version 会让 `COPY pyproject.toml` 层
#   随之变,下游 pip 依赖层全废冷建。故 CI/build 在构建前把上下文里的 pyproject version 抹成占位 0.0.0(见
#   ci.yml / build-uptest.sh),COPY pyproject 层跨提交稳定,pip 缓存命中。真实语义版本经 build-arg FLORI_VERSION
#   注入(各 stage ENV FLORI_VERSION);shared/version.py 用此 env 覆盖,不读已安装包版本,故显示仍准。
#
# 注:不用 `# syntax=...` 指令(会去 docker.io 拉 frontend 镜像,被 NAS 代理 reset);
#    --mount=type=cache 靠引擎内置 BuildKit frontend 即可(已实测 `docker compose build` 支持)。

ARG CLI_TOOLS_IMAGE=cli-tools

# common:共享底座(python + pip 源 + core 依赖,无源码)
FROM python:3.11-slim AS common
# 默认 USTC 镜像源(国内构建快);海外 CI runner 传 --build-arg USE_USTC_MIRROR=0 用官方源。
ARG USE_USTC_MIRROR=1
RUN if [ "$USE_USTC_MIRROR" = "1" ]; then \
        sed -i 's|deb.debian.org|mirrors.ustc.edu.cn|g' /etc/apt/sources.list.d/debian.sources; \
    fi \
    && apt-get -o Acquire::Retries=5 update \
    && apt-get -o Acquire::Retries=5 install -y --no-install-recommends curl \
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

# 三种 CLI 独立成稳定工具 stage。CI 先解析官方 channel 版本,版本组合不变时复用该 stage digest。
# builder 只负责联网安装;最终 cli-tools 不包含临时 HOME、下载缓存或应用依赖。
FROM python:3.11-slim AS cli-tools-builder
RUN apt-get -o Acquire::Retries=5 update \
    && apt-get -o Acquire::Retries=5 install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*
ARG CLI_INSTALL_REFRESH=manual
ARG CLAUDE_CLI_VERSION=
ARG QODER_CLI_VERSION=
ARG CODEX_CLI_VERSION=
ARG INSTALL_CLAUDE_CODE=1
ARG INSTALL_QODER_CLI=1
ARG INSTALL_CODEX_CLI=1
COPY docker/install-cli-tools.sh /tmp/install-cli-tools.sh
RUN echo "CLI install key: ${CLI_INSTALL_REFRESH}" \
    && CLAUDE_CLI_VERSION="${CLAUDE_CLI_VERSION}" \
       QODER_CLI_VERSION="${QODER_CLI_VERSION}" \
       CODEX_CLI_VERSION="${CODEX_CLI_VERSION}" \
       INSTALL_CLAUDE_CODE="${INSTALL_CLAUDE_CODE}" \
       INSTALL_QODER_CLI="${INSTALL_QODER_CLI}" \
       INSTALL_CODEX_CLI="${INSTALL_CODEX_CLI}" \
       timeout 1800 sh /tmp/install-cli-tools.sh \
    && rm -f /tmp/install-cli-tools.sh \
    && mkdir -p /opt/flori-cli-root/usr/local/bin \
        /opt/flori-cli-root/usr/local/lib/flori-cli \
        /opt/flori-cli-root/usr/local/lib/codex \
        /opt/flori-cli-root/etc/flori \
    && for path in claude qodercli codex codex-code-mode-host; do \
         if [ -e "/usr/local/bin/$path" ] || [ -L "/usr/local/bin/$path" ]; then \
           cp -a "/usr/local/bin/$path" /opt/flori-cli-root/usr/local/bin/; \
         fi; \
       done \
    && if [ -d /usr/local/lib/flori-cli ]; then \
         cp -a /usr/local/lib/flori-cli/. /opt/flori-cli-root/usr/local/lib/flori-cli/; \
       fi \
    && if [ -d /usr/local/lib/codex ]; then \
         cp -a /usr/local/lib/codex/. /opt/flori-cli-root/usr/local/lib/codex/; \
       fi \
    && if [ -d /etc/flori ]; then \
         cp -a /etc/flori/. /opt/flori-cli-root/etc/flori/; \
       fi

FROM debian:bookworm-slim AS cli-tools
ARG CLI_TOOLS_SOURCE_DIGEST=
ARG CLAUDE_CLI_VERSION=
ARG QODER_CLI_VERSION=
ARG CODEX_CLI_VERSION=
COPY --from=cli-tools-builder /opt/flori-cli-root/ /
LABEL io.flori.cli.claude="${CLAUDE_CLI_VERSION}" \
      io.flori.cli.qoder="${QODER_CLI_VERSION}" \
      io.flori.cli.codex="${CODEX_CLI_VERSION}" \
      io.flori.cli.source="${CLI_TOOLS_SOURCE_DIGEST}"

# CI 有稳定基座时按 immutable digest 注入外部镜像;缺省引用上面的内部 stage。
FROM ${CLI_TOOLS_IMAGE} AS cli-tools-source

# api 的 YouTube playlist 枚举与 worker 下载共用同一受支持 JS runtime。
FROM common AS deno
ARG TARGETARCH
ARG DENO_VERSION=v2.9.3
COPY docker/install-deno.sh /tmp/install-deno.sh
RUN DENO_VERSION="${DENO_VERSION}" TARGETARCH="${TARGETARCH}" sh /tmp/install-deno.sh \
    && rm -f /tmp/install-deno.sh

# scheduler:core + 调度器惰性调用的服务层,不携带 API routes
FROM common AS scheduler
COPY shared/ shared/
COPY configs/ configs/
COPY api/__init__.py api/__init__.py
COPY api/services/ api/services/
COPY scheduler/ scheduler/
COPY tunnel_stats/ tunnel_stats/
# Scheduler 惰性调用 Radar 与概念重综合;构建时验证精确复制的服务边界可导入。
RUN python -c "from api.services import concepts, evidence, radar"
ARG FLORI_BUILD_SHA=
ENV FLORI_BUILD_SHA=${FLORI_BUILD_SHA}
ARG FLORI_VERSION=
ENV FLORI_VERSION=${FLORI_VERSION}

# api:+[api,mcp](api + mcp_server),无 claude/ffmpeg。/data/prompts seed(profiles 管理读它)
FROM deno AS api
RUN --mount=type=cache,target=/root/.cache/pip pip install ".[api,mcp]" \
    && python -c "import yt_dlp_ejs"
COPY shared/ shared/
COPY configs/ configs/
COPY api/ api/
COPY scripts/dr_snapshot.py scripts/dr_snapshot.py
# prompts_dir 运行时 = /data/prompts(config.data_dir/'prompts');api 的 /api/profiles 读 profiles。
# 生产 /data 是命名卷,首建空卷时被 seed,之后持久化(rebuild 不覆盖卷内旧内容,需手动同步)。
COPY configs/prompts/ /data/prompts/
ARG FLORI_BUILD_SHA=
ENV FLORI_BUILD_SHA=${FLORI_BUILD_SHA}
ARG FLORI_VERSION=
ENV FLORI_VERSION=${FLORI_VERSION}

# compute worker:io/cpu/gpu 共用的媒体与论文计算运行时,不携带任何 CLI agent。
# cpu 池当前同时承载 Document/Video/Audio,先保留完整 [steps,gpu,worker] 依赖以守住认领契约。
FROM deno AS worker-compute
ARG USE_USTC_MIRROR=1
# poppler-utils:Document PDF adapter 用 pdfinfo、pdftohtml XML 和 pdftotext bbox 建立文本层。
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get -o Acquire::Retries=10 update \
    && apt-get -o Acquire::Retries=10 -o APT::Keep-Downloaded-Packages=true \
        install -y --no-install-recommends ffmpeg poppler-utils
RUN --mount=type=cache,target=/root/.cache/pip pip install ".[steps,gpu,worker]" \
    && python -c "import yt_dlp_ejs"
# Document布局模型只在02_parse懒加载;统一worker镜像让所有CPU worker具备同一能力。
ARG DOCUMENT_LAYOUT_MODEL_URL="https://huggingface.co/wybxc/DocLayout-YOLO-DocStructBench-onnx/resolve/89c9fe92dd1384e26612846b4ab68a811dc98d61/doclayout_yolo_docstructbench_imgsz1024.onnx?download=true"
ARG DOCUMENT_LAYOUT_MODEL_SHA256="fece9af02f618b603ff7921ccec6861d13e7e1f9830e091dfb7e8ad9311e5b21"
RUN mkdir -p /app/models/document-layout \
    && curl -fL --retry 5 --retry-all-errors --retry-delay 5 --connect-timeout 30 \
        --speed-limit 1024 --speed-time 300 --max-time 900 \
        "$DOCUMENT_LAYOUT_MODEL_URL" -o /tmp/document-layout.onnx \
    && echo "$DOCUMENT_LAYOUT_MODEL_SHA256  /tmp/document-layout.onnx" | sha256sum -c - \
    && mv /tmp/document-layout.onnx /app/models/document-layout/doclayout-yolo.onnx
ENV FLORI_DOCUMENT_LAYOUT_MODEL=/app/models/document-layout/doclayout-yolo.onnx \
    FLORI_DOCUMENT_LAYOUT_MODEL_SHA256=${DOCUMENT_LAYOUT_MODEL_SHA256} \
    FLORI_DOCUMENT_LAYOUT_CONFIDENCE=0.2 \
    FLORI_DOCUMENT_LAYOUT_THREADS=4
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
ENV DISABLE_AUTOUPDATER=1 \
    DISABLE_UPDATES=1 \
    FLORI_WORKER_IMAGE_PROFILE=compute

# cpu 与 gpu 先共享同一文件系统层。独立 target/repository 允许只滚动一种能力,
# 后续增加 CUDA runtime 时不会再次改变 cpu 镜像。
FROM worker-compute AS worker-cpu

FROM worker-compute AS worker-gpu

# AI Worker 不安装 ffmpeg/poppler/OCR/Whisper/Deno/布局模型。AI 步只依赖 core + worker,
# 三种 concrete provider 在末层各取一套 CLI,避免一个容器携带其它 Provider 工具。
FROM common AS worker-ai-base
RUN --mount=type=cache,target=/root/.cache/pip pip install ".[worker,ai-runtime]" \
    && python -c "from PIL import Image; assert Image is not None"
COPY shared/ shared/
COPY configs/ configs/
COPY steps/ steps/
COPY worker/ worker/
COPY configs/prompts/ /data/prompts/
ARG FLORI_BUILD_SHA=
ENV FLORI_BUILD_SHA=${FLORI_BUILD_SHA}
ARG FLORI_VERSION=
ENV FLORI_VERSION=${FLORI_VERSION}
ENV DISABLE_AUTOUPDATER=1 \
    DISABLE_UPDATES=1

FROM worker-ai-base AS worker-ai-claude
COPY --from=cli-tools-source /usr/local/bin/claude /usr/local/bin/claude
COPY --from=cli-tools-source /usr/local/lib/flori-cli/claude /usr/local/lib/flori-cli/claude
ENV FLORI_WORKER_IMAGE_PROFILE=ai \
    FLORI_IMAGE_CLI_PROVIDER=claude-cli

FROM worker-ai-base AS worker-ai-qoder
COPY --from=cli-tools-source /usr/local/bin/qodercli /usr/local/bin/qodercli
COPY --from=cli-tools-source /usr/local/lib/flori-cli/qodercli /usr/local/lib/flori-cli/qodercli
COPY --from=cli-tools-source /etc/flori/qoder-settings.json /etc/flori/qoder-settings.json
ENV FLORI_WORKER_IMAGE_PROFILE=ai \
    FLORI_IMAGE_CLI_PROVIDER=qoder-cli

FROM worker-ai-base AS worker-ai-codex
COPY --from=cli-tools-source /usr/local/lib/codex/ /usr/local/lib/codex/
RUN ln -s /usr/local/lib/codex/packages/standalone/current/bin/codex /usr/local/bin/codex \
    && codex --version
ENV FLORI_WORKER_IMAGE_PROFILE=ai \
    FLORI_IMAGE_CLI_PROVIDER=codex-cli

# test-runtime 不含源码,供 CI 构建一次后由各 runner 拉取;测试源码通过 Compose bind mount 注入.
FROM common AS test-runtime
RUN --mount=type=cache,target=/root/.cache/pip pip install ".[api,worker,mcp,test]"

# test(普通):纯逻辑单测镜像 —— 仅 [api,worker,mcp,test],无 ffmpeg / 无 [steps] 媒体库(opencv/pymupdf/skimage 等)。
#    跑非 step 测试,即 scheduler/api/shared/db/redis 等绝大多数:app+tests 无任何顶层 import 重库(全惰性 + mock),
#    collection 与运行都不需要。与部署镜像同理拆普通与 worker 两档:普通镜像轻(~350MB,build/load 秒级)。
FROM test-runtime AS test
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

# test-worker-runtime 复用普通测试依赖并追加媒体库,仍然不含源码.
FROM test-runtime AS test-worker-runtime
RUN apt-get -o Acquire::Retries=5 update \
    && apt-get -o Acquire::Retries=5 install -y --no-install-recommends ffmpeg libgl1 libglib2.0-0 libsm6 libxext6 libxrender1 poppler-utils \
    && rm -rf /var/lib/apt/lists/*
RUN --mount=type=cache,target=/root/.cache/pip pip install ".[steps]"

# test-worker(重):跑 step/worker 测试(tests/steps/ + tests/test_step_*.py + test_worker.py,真 import
#    opencv/pymupdf/scikit-image/trafilatura/imagehash)。复用现有 [steps] extras,不含 [gpu](测试全 mock 不需)。
#    runtime 与源码层分离,改源码只重最终 COPY,apt/[steps] 依赖层恒命中 buildcache.
FROM test-worker-runtime AS test-worker
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
