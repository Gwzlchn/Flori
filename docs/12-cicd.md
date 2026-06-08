# 12 · CI/CD & 发布

> GitHub Actions（GitHub-hosted runner）+ ghcr.io 镜像发布；self-hosted runner 可选。

## 1. Pipeline 概览

```
Push/PR to main   → Unit Test（容器内全部单测）
Merge to main     → + Build + Push Image (ghcr.io) → Watchtower 自动拉取重建（CD）
```

测试只有容器内单测一道（`test` job），无独立的 PR 集成测试 job。

## 2. 镜像发布

```
Registry: ghcr.io/gwzlchn/mnemo, ghcr.io/gwzlchn/mnemo-frontend
Tags:     latest, <git-short-sha>
```

用户一键部署：
```bash
git clone https://github.com/gwzlchn/mnemo
cp .env.example .env   # 填 API key
docker compose up -d   # 拉公开镜像，不需要本地 build
```

## 3. Runner 选择

默认且当前唯一在用的是 GitHub-hosted runner：公开仓库免费无限分钟，自带 Docker + buildx，跑单测与构建镜像足够、零维护。

self-hosted runner 仅在将来需要本地资源时可选（如用本地视频素材跑端到端验证、国内 USTC 镜像加速）；目前 CI 不依赖它。

安全：公开仓库不要用 self-hosted runner 处理 fork PR（不受信代码可读取 secrets、在你机器上执行）；如需自托管，仅限私仓或 push/已审核 PR 触发。

self-hosted 安装（可选）：
```bash
mkdir -p ~/actions-runner && cd ~/actions-runner
curl -o actions-runner-linux-x64.tar.gz -L \
  https://github.com/actions/runner/releases/latest/download/actions-runner-linux-x64-2.321.0.tar.gz
tar xzf ./actions-runner-linux-x64.tar.gz
./config.sh --url https://github.com/gwzlchn/mnemo --token <TOKEN>
sudo ./svc.sh install && sudo ./svc.sh start
```

## 4. Workflow 设计

实际实现见 `.github/workflows/ci.yml`（主 CI）+ `.github/workflows/step-images.yml`（按步执行镜像，仅手动触发）：

- `test`：push/PR 到 main 触发，`docker compose -f docker-compose.test.yml run --rm test`（全部单测）。
- `build-push`：仅 main、测试通过后，用 buildx 构建 **amd64**（所有目标机均为 x86，不构 arm64）推 ghcr.io；
  矩阵两个镜像 `mnemo`（api/scheduler/worker 共用）与 `mnemo-frontend`。
- `step-images.yml`：步骤执行镜像（`mnemo-step-base` / `mnemo-step-heavy` / `mnemo-step-gpu`）独立于主 CI，`workflow_dispatch` 手动触发，同样只构 amd64。

部署为自动 CD：生产 `docker-compose.yml` 跑 Watchtower（`containrrr/watchtower`），每 120s 查 ghcr，只更新带 `com.centurylinklabs.watchtower.enable=true` 标签的容器，自动 pull + 重建 + 清理旧镜像。无 SSH 自动部署脚本。

## 5. docker-compose.yml 改造

```yaml
# 生产用：拉远程镜像
services:
  api:
    image: ghcr.io/gwzlchn/mnemo:latest
    # ...
```

```yaml
# 开发用（docker-compose.dev.yml）：本地 build + 挂载源码
services:
  api:
    build:
      context: .
      dockerfile: docker/base.Dockerfile
    volumes:
      - ./shared:/app/shared
    # ...
```

## 6. .env.example

```bash
# === 必填 ===
ANTHROPIC_API_KEY=sk-ant-...    # 或留空用 DRY_RUN
DEEPSEEK_API_KEY=sk-...         # AI 笔记生成

# === 可选 ===
API_TOKEN=                      # API 认证 token（留空不鉴权）
HTTPS_PROXY=                    # 代理（不需要可留空）
DRY_RUN=0                       # 1=AI 步骤不调真实 API

# === 高级 ===
DATA_DIR=/data                  # 数据目录
CONFIG_DIR=/data/configs        # 配置目录
```

## 7. GitHub Secrets

| Secret | 用途 |
|--------|------|
| `ANTHROPIC_API_KEY` | 生产环境 |
| `DEEPSEEK_API_KEY` | 生产环境 |

> 推镜像到 ghcr.io 用 Actions 内置 `GITHUB_TOKEN`（`packages: write` 权限），无需额外 secret。CI `test` job 跑容器内单测，不需 API key。

## 8. TODO

- [x] 创建 `.github/workflows/ci.yml`（test + amd64 build-push 到 ghcr.io）
- [x] docker-compose.yml 改用 `image: ghcr.io/gwzlchn/mnemo:latest`（拉远程镜像部署）
- [x] docker-compose.yml 接入 Watchtower 自动 CD
- [x] 创建 `.env.example`
- [ ] 首次 push 后到仓库 Packages 确认镜像、Watchtower 自动更新验证
