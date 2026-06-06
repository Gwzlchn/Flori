FROM python:3.11-slim

# 默认用 USTC 镜像源（国内构建快）；海外 CI runner 传 --build-arg USE_USTC_MIRROR=0 用官方源。
ARG USE_USTC_MIRROR=1

RUN if [ "$USE_USTC_MIRROR" = "1" ]; then \
        sed -i 's|deb.debian.org|mirrors.ustc.edu.cn|g' /etc/apt/sources.list.d/debian.sources; \
    fi \
    && apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl \
    && rm -rf /var/lib/apt/lists/*

RUN if [ "$USE_USTC_MIRROR" = "1" ]; then \
        pip config set global.index-url https://mirrors.ustc.edu.cn/pypi/web/simple; \
    fi

WORKDIR /app

COPY pyproject.toml .
RUN pip install --no-cache-dir ".[steps,api]" && \
    pip install --no-cache-dir websockets httpx

# 不写 .pyc/__pycache__：配合 test/dev compose 的 bind-mount，避免容器内 pytest
# 把缓存写回宿主源码目录(此前"在 docker 里测试仍冒缓存"的根因)。日志不缓冲。
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY shared/ shared/
COPY steps/ steps/
COPY api/ api/
COPY scheduler/ scheduler/
COPY worker/ worker/
COPY configs/ configs/
COPY configs/prompts/ /data/prompts/
