FROM python:3.11-slim

RUN sed -i 's|deb.debian.org|mirrors.ustc.edu.cn|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl \
    && rm -rf /var/lib/apt/lists/*

RUN pip config set global.index-url https://mirrors.ustc.edu.cn/pypi/web/simple

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
