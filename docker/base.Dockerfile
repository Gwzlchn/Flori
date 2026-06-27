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
RUN pip install --no-cache-dir ".[steps,api,worker,gpu,mcp]"

# 不写 .pyc/__pycache__：配合 test/dev compose 的 bind-mount，避免容器内 pytest
# 把缓存写回宿主源码目录(此前"在 docker 里测试仍冒缓存"的根因)。日志不缓冲。
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY shared/ shared/
COPY steps/ steps/
COPY api/ api/
COPY scheduler/ scheduler/
COPY worker/ worker/
COPY tunnel_stats/ tunnel_stats/
COPY configs/ configs/
# prompts_dir 运行时解析为 /data/prompts(config.data_dir/'prompts')。此处 build 期把仓库
# configs/prompts(profiles/styles 等)塞进镜像 /data/prompts。注意:生产 /data 是命名卷,首建空卷
# 时被 seed,之后持久化——后续 rebuild 镜像的新 profiles/styles【不会】自动覆盖卷里旧内容。
# 更新仓库 profiles/styles 后需手动同步 /data/prompts(或重置该卷)。profiles 经 API 可运行时编辑,
# 故不能直接只读 bind-mount 仓库目录覆盖。
COPY configs/prompts/ /data/prompts/

# 网络区域路由(net-zone)用的 CN 域名表:【构建时从 GitHub 上游(felixonmars/dnsmasq-china-list)拉取,
# 不自维护】,解析成可注册域集烤进镜像 /app/data/cn_domains.txt(运行时 shared.net_zone 只读不拉)。
# 源用与 apt/pip 同一个 USE_USTC_MIRROR 控制:国内构建(=1)优先 jsdelivr/ghproxy 国内可达镜像,
# 海外 CI(=0)走 github raw;按序兜底,全失败则留空 → 运行时回退仅按 .cn TLD 判 cn(境外仍 net-global)。
RUN mkdir -p /app/data \
    && CN_RAW="https://raw.githubusercontent.com/felixonmars/dnsmasq-china-list/master/accelerated-domains.china.conf" \
    && CN_JSD="https://cdn.jsdelivr.net/gh/felixonmars/dnsmasq-china-list@master/accelerated-domains.china.conf" \
    && CN_GHP="https://ghproxy.net/${CN_RAW}" \
    && if [ "$USE_USTC_MIRROR" = "1" ]; then ORDER="$CN_JSD $CN_GHP $CN_RAW"; else ORDER="$CN_RAW $CN_JSD"; fi \
    && for u in $ORDER; do curl -fsSL --retry 2 --max-time 90 "$u" -o /tmp/cn.conf && break || true; done; \
       sed -n 's#^server=/\([^/]*\)/.*#\1#p' /tmp/cn.conf 2>/dev/null | sort -u > /app/data/cn_domains.txt || true; \
       echo "cn_domains baked: $(wc -l < /app/data/cn_domains.txt 2>/dev/null || echo 0) domains"

# 注:net-zone 探针 URL(NET_PROBE_CN/NET_PROBE_GLOBAL)是【部署/启动配置】,不烤进镜像——
# 由 compose 的 worker 服务 env 注入(见 docker-compose.yml);worker 代码有兜底默认。
# NET_ZONES=cn,global 可在启动时强制跳过探测(如香港 worker 设 NET_ZONES=global)。

# 构建期注入构建短 sha:运行时 shared.version 把它拼到语义版本后(0.2.0+<sha>),用于查"哪台
# worker 跑哪份代码"(代码漂移排查)。放最后,版本变化不影响上面代码层缓存。语义版本来自已装包(pyproject)。
ARG FLORI_BUILD_SHA=
ENV FLORI_BUILD_SHA=${FLORI_BUILD_SHA}
