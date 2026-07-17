# 按步执行镜像:纯 Python 步骤(下载/弹幕/标点/AI 改写/审校/分节),无重型原生依赖。
# docker 执行器每步起一容器跑此镜像;命令与 subprocess 同构(python3 -m <module> ...)。
FROM python:3.11-slim

# 默认用 USTC 镜像源(国内构建快);海外 CI 传 --build-arg USE_USTC_MIRROR=0 用官方源。
ARG USE_USTC_MIRROR=1
ARG TARGETARCH
ARG DENO_VERSION=v2.9.3

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

COPY docker/install-deno.sh /tmp/install-deno.sh
RUN DENO_VERSION="${DENO_VERSION}" TARGETARCH="${TARGETARCH}" sh /tmp/install-deno.sh \
    && rm -f /tmp/install-deno.sh

# 纯 Python 步骤只需下载器,不装 ffmpeg/scenedetect/ocr/pdf(各拆专用镜像)。
COPY pyproject.toml .
RUN pip install --no-cache-dir . \
    && pip install --no-cache-dir "yutto>=2.0,<3" "yt-dlp[default]>=2025.11.12,<2027.0" \
    && python -c "import yt_dlp_ejs"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY shared/ shared/
COPY steps/ steps/
