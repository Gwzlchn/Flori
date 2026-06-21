FROM python:3.11-slim

# 默认用 USTC 镜像源（国内构建快）；海外 CI runner 传 --build-arg USE_USTC_MIRROR=0 用官方源。
ARG USE_USTC_MIRROR=1

RUN if [ "$USE_USTC_MIRROR" = "1" ]; then \
        sed -i 's|deb.debian.org|mirrors.ustc.edu.cn|g' /etc/apt/sources.list.d/debian.sources; \
    fi \
    && apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl nodejs npm \
    && rm -rf /var/lib/apt/lists/*

# Claude Code CLI:claude-cli provider(订阅出笔记、看帧图)需要 `claude` 在 PATH。
RUN if [ "$USE_USTC_MIRROR" = "1" ]; then \
        npm config set registry https://registry.npmmirror.com; \
    fi \
    && npm install -g @anthropic-ai/claude-code \
    && rm -rf /root/.npm

RUN if [ "$USE_USTC_MIRROR" = "1" ]; then \
        pip config set global.index-url https://mirrors.ustc.edu.cn/pypi/web/simple; \
    fi

WORKDIR /app

COPY pyproject.toml .
# httpx 已是核心依赖(pyproject [project].dependencies)、websockets 由 [api] 的 uvicorn[standard]
# 传递引入,故不再裸装(原 `pip install websockets httpx` 冗余且不带版本上界)。
RUN pip install --no-cache-dir ".[steps,api,worker,gpu]"

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
# prompts_dir 运行时解析为 /data/prompts(config.data_dir/'prompts')。此处 build 期把仓库
# configs/prompts(profiles/styles 等)塞进镜像 /data/prompts。注意:生产 /data 是命名卷,首建空卷
# 时被 seed,之后持久化——后续 rebuild 镜像的新 profiles/styles【不会】自动覆盖卷里旧内容。
# 更新仓库 profiles/styles 后需手动同步 /data/prompts(或重置该卷)。profiles 经 API 可运行时编辑,
# 故不能直接只读 bind-mount 仓库目录覆盖。
COPY configs/prompts/ /data/prompts/
