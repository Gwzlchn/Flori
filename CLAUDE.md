# CLAUDE.md — AI 协作者指南

> 每个 Claude Code 会话开始时读这个文件。

## 项目

AI 辅助的个人学习知识库。把视频/论文/文章自动转化为结构化笔记，积累为可检索的知识体系。

## 文档体系

```
docs/README.md              → 文档大纲（先读这个）
docs/00-vision.md           → 为什么做、不做什么
docs/01-architecture.md     → 系统全景图 + 部署拓扑
docs/02-domain-model.md     → 领域模型 + 状态机 + DB Schema
docs/03-contracts.md        → API / WebSocket / Redis / 文件 Schema
docs/04-module-design/      → 各模块详设（10 个文件）
docs/05-content-adapters.md → 内容适配器（视频/论文/文章）
docs/06-prompt-engineering.md → Prompt 工程
docs/07-security.md         → 安全
docs/08-deployment.md       → 部署（单机/分层/GPU 接入）
docs/09-testing.md          → 测试
docs/10-observability.md    → 可观测
docs/11-dev-workflow.md     → 开发流程 + 并行会话
docs/12-cicd.md             → CI/CD & 发布（GitHub Actions + 镜像）
docs/13-dependencies.md     → 开源依赖（工具选型 / License）
docs/adr/                   → 8 个架构决策记录
ROADMAP.md                  → 里程碑和进度
```

## 开发约定

### 代码风格
- Python 3.11+，type hints
- 异步用 asyncio（调度器/API/Worker）
- 同步用 subprocess（步骤脚本调外部工具）
- 配置用 YAML，数据用 JSON
- 日志用 structlog（结构化 JSON）

### 架构规则
- **全程容器化**：开发、测试、部署全在 Docker 内，宿主机不装任何 Python/Node 依赖
  - 开发：`docker compose -f docker-compose.dev.yml up`（挂载源码热更新）
  - 测试：`docker compose run --rm test pytest`（容器内跑测试）
  - 部署：`docker compose up -d`（生产镜像）
  - 每个模块有 Dockerfile，最终产物是可直接部署的镜像
- **镜像源**：Dockerfile 中 pip 使用 USTC 源（`-i https://mirrors.ustc.edu.cn/pypi/web/simple`），apt 同理
- **文件是接口**：步骤间通过 JSON/MD 文件通信，不共享内存
- **幂等**：每步检查输入指纹，输入没变就跳过
- **故障隔离**：单任务失败不影响其他任务
- **配置与代码分离**：领域知识在 YAML/Prompt 文件里，不硬编码

### 不做的事
- 不用 ORM（SQLite 直接用 sql）
- 不用消息中间件（Redis Streams 够用）
- 不用 Kubernetes（Docker Compose 够用）
- 不做国际化（中文为主）
- 不做用户系统（个人工具，Basic Auth）

## 系统要求

| 项目 | 最低 | 推荐 |
|------|------|------|
| CPU | 4 核 | 6+ 核 |
| 内存 | 8 GB | 16 GB |
| 磁盘 | 50 GB | 500 GB+ |
| Docker | 20.0+ | 最新稳定版 |
| Claude | CLI 可用（订阅或 API） | 订阅 Max |
| 网络 | 能访问 Claude API | 代理可选 |
| GPU | 不需要（可选） | NVIDIA 8GB+ 显存 |

## 项目目录结构

```
mnemo/
├── CLAUDE.md                    # 本文件（AI 协作者指南）
├── ROADMAP.md                   # 里程碑和进度
├── README.md                    # 项目说明
├── pyproject.toml               # Python 包定义 + 依赖
│
├── docker/
│   └── base.Dockerfile          # 基础镜像（所有 Python 服务共用）
├── docker-compose.yml           # 生产部署
├── docker-compose.dev.yml       # 开发（挂载源码热更新）
├── docker-compose.test.yml      # 测试
│
├── shared/                      # 共享层（所有模块依赖此层）
│   ├── models.py                # 数据模型 + 枚举
│   ├── errors.py                # 错误层级 + 重试策略
│   ├── config.py                # 配置加载
│   ├── db.py                    # SQLite 数据库层
│   ├── redis_client.py          # Redis 客户端封装
│   ├── storage.py               # StorageBackend (Local/Remote)
│   ├── ai_gateway.py            # AI Gateway (多 Provider)
│   └── step_base.py             # StepBase 基类
│
├── scheduler/                   # 调度器（M1 实现）
├── worker/                      # Worker 主循环（M1 实现）
├── api/                         # FastAPI 服务（M1 实现）
├── steps/                       # 步骤脚本（从原型迁移）
├── frontend/                    # Vue3 前端（M1 实现）
│
├── configs/                     # 运行时配置
│   ├── pipelines.yaml           # 步骤链定义
│   ├── pools.yaml               # 资源池配置
│   └── providers.yaml           # AI Provider 配置
│
├── tests/                       # 测试（容器内运行）
│   ├── conftest.py
│   └── test_*.py
│
├── docs/                        # 设计文档（00-13 + adr/）
└── LOCAL.md                     # 本地开发笔记（不入 git）
```

**Docker 镜像策略**：所有 Python 服务（API、调度器、Worker）共用一个基础镜像 `docker/base.Dockerfile`，启动命令不同。一次 build，所有服务用。

## 开发方式

- 全部在本地 Docker 开发（localhost），部署时改 `.env`
- 一个里程碑可以开多个并行 Claude 会话（基础设施/业务/前端）
- 步骤代码独立开发+验证，用已有产物做测试输入
- 每完成一个模块提交 git + 更新 ROADMAP

