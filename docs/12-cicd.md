# 12 · CI/CD & 发布

> Self-hosted runner + GitHub Actions + ghcr.io 镜像发布。

## 1. Pipeline 概览

```
Push/PR           → Lint + Unit Test     (~2min)
PR to main        → + Integration Test   (~10min)
Merge to main     → + Build + Push Image → Deploy
```

## 2. 镜像发布

```
Registry: ghcr.io/<your-github-username>/ai-knowledge-base
Tags:     latest, <git-short-sha>, v0.1.0 (release)
```

用户一键部署：
```bash
git clone https://github.com/<your-github-username>/ai-knowledge-base
cp .env.example .env   # 填 API key
docker compose up -d   # 拉公开镜像，不需要本地 build
```

## 3. Self-hosted Runner

为什么不用 GitHub-hosted：
- 集成测试需要本地视频素材（不入 git）
- USTC 镜像在国内 runner 更快
- 集成测试耗时 10+ 分钟（free tier 有限）

安装：
```bash
mkdir -p ~/actions-runner && cd ~/actions-runner
curl -o actions-runner-linux-x64.tar.gz -L \
  https://github.com/actions/runner/releases/latest/download/actions-runner-linux-x64-2.321.0.tar.gz
tar xzf ./actions-runner-linux-x64.tar.gz
./config.sh --url https://github.com/<your-github-username>/ai-knowledge-base --token <TOKEN>
sudo ./svc.sh install && sudo ./svc.sh start
```

## 4. Workflow 设计

### ci.yml（push + PR）

```yaml
jobs:
  lint:         # ruff check
  unit-test:    # docker compose -f docker-compose.test.yml run --rm test
  integration:  # 仅 PR，docker compose up + run_e2e_cpu.sh
```

### deploy.yml（merge to main）

```yaml
jobs:
  build-push:   # docker build → push ghcr.io
  deploy:       # ssh/本地 docker compose up -d
  health-check: # curl /api/health
```

## 5. docker-compose.yml 改造

```yaml
# 生产用：拉远程镜像
services:
  api:
    image: ghcr.io/<your-github-username>/ai-knowledge-base:latest
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

- [ ] 创建 `.github/workflows/ci.yml`
- [ ] 创建 `.github/workflows/deploy.yml`
- [ ] docker-compose.yml 改用 `image: ghcr.io/...`
- [ ] 创建 `.env.example`
- [ ] 配置 self-hosted runner
- [ ] 首次手动 push 镜像验证流程
