# 重 CPU 步骤镜像:媒体(场景/抽帧/去重)+ OCR + PDF 合一。
# CUDA 单独拆 step-gpu;其余重原生依赖归这一个,避免按步再细分多个镜像。
FROM python:3.11-slim

# 默认用 USTC 镜像源(国内构建快);海外 CI 传 --build-arg USE_USTC_MIRROR=0 用官方源。
ARG USE_USTC_MIRROR=1

RUN if [ "$USE_USTC_MIRROR" = "1" ]; then \
        sed -i 's|deb.debian.org|mirrors.ustc.edu.cn|g' /etc/apt/sources.list.d/debian.sources; \
    fi \
    && apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl poppler-utils \
    && rm -rf /var/lib/apt/lists/*

RUN if [ "$USE_USTC_MIRROR" = "1" ]; then \
        pip config set global.index-url https://mirrors.ustc.edu.cn/pypi/web/simple; \
    fi

WORKDIR /app

COPY pyproject.toml .
RUN pip install --no-cache-dir . \
    && pip install --no-cache-dir \
        "scenedetect[opencv]>=0.6,<1" "imagehash>=4.3,<5" "scikit-image>=0.22,<1" \
        "rapidocr-onnxruntime>=1.3,<2" "pymupdf>=1.24,<2" "Pillow>=10.0,<13"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY shared/ shared/
COPY steps/ steps/
