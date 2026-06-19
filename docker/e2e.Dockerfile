# 前端端到端冒烟测试镜像。
# 基于官方 Playwright 镜像（已内置 chromium/firefox/webkit + 全部系统依赖，浏览器在 /ms-playwright），
# 仅补装与镜像内浏览器版本对齐的 playwright python 包（pip 不会重新下载浏览器）。
FROM mcr.microsoft.com/playwright/python:v1.55.0-noble

# 默认 USTC 源（国内构建快）；海外 CI 传 --build-arg USE_USTC_MIRROR=0 用官方源。
ARG USE_USTC_MIRROR=1
RUN if [ "$USE_USTC_MIRROR" = "1" ]; then \
        pip config set global.index-url https://mirrors.ustc.edu.cn/pypi/web/simple; \
    fi \
    && pip install --no-cache-dir "playwright==1.55.0"

WORKDIR /work
