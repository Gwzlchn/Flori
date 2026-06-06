# 12 · CI/CD & 发布

> GitHub Actions（GitHub-hosted runner）+ ghcr.io 镜像发布；self-hosted runner 可选。

## 1. Pipeline 概览

```
Push/PR           → Lint + Unit Test     (~2min)
PR to main        → + Integration Test   (~10min)
Merge to main     → + Build + Push Image → Deploy
```

## 2. 镜像发布

```
Registry: ghcr.io/<your-github-username>/mnemo
Tags:     latest, <git-short-sha>, v0.1.0 (release)
```

用户一键部署：
```bash
git clone https://github.com/<your-github-username>/mnemo
cp .env.example .env   # 填 API key
docker compose up -d   # 拉公开镜像，不需要本地 build
```

## 3. Runner 选择

默认 GitHub-hosted runner：公开仓库免费无限分钟，自带 Docker + buildx，跑单测与构建镜像足够、零维护。

self-hosted runner 仅在需要本地资源时可选：
- 集成测试需要本地视频素材（不入 git，仅存于自托管 runner 本地）
- 国内 USTC 镜像加速

安全：公开仓库不要用 self-hosted runner 处理 fork PR（不受信代码可读取 secrets、在你机器上执行）；如需自托管，仅限私仓或 push/已审核 PR 触发。

self-hosted 安装（可选）：
```bash
mkdir -p ~/actions-runner && cd ~/actions-runner
curl -o actions-runner-linux-x64.tar.gz -L \
  https://github.com/actions/runner/releases/latest/download/actions-runner-linux-x64-2.321.0.tar.gz
tar xzf ./actions-runner-linux-x64.tar.gz
./config.sh --url https://github.com/<your-github-username>/mnemo --token <TOKEN>
sudo ./svc.sh install && sudo ./svc.sh start
```

## 4. Workflow 设计

实际实现见 `.github/workflows/ci.yml`：

- `test`：push/PR 触发，`docker compose -f docker-compose.test.yml run --rm test`（全部单测）。
- `build-push`：仅 main、测试通过后，用 buildx 构建多架构镜像（amd64+arm64）推 ghcr.io；
  两个镜像 `mnemo`（api/scheduler/worker 共用）与 `mnemo-frontend`。

部署为手动：各机 `docker compose pull && docker compose up -d`（不做 SSH 自动部署，降低公网风险）。

## 5. docker-compose.yml 改造

```yaml
# 生产用：拉远程镜像
services:
  api:
    image: ghcr.io/<your-github-username>/mnemo:latest
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
| `DEEPSEEK_API_KEY` | AI 集成测试 |
| `ANTHROPIC_API_KEY` | 生产环境 |
| `GHCR_TOKEN` | 推镜像到 ghcr.io（或用 GITHUB_TOKEN） |

## 8. TODO

- [x] 创建 `.github/workflows/ci.yml`（test + 多架构 build-push 到 ghcr.io）
- [ ] docker-compose.yml 改用 `image: ghcr.io/<owner>/mnemo:latest`（拉远程镜像部署）
- [ ] 创建 `.env.example`
- [ ] 首次 push 后到仓库 Packages 确认镜像、各机 `docker compose pull` 验证
