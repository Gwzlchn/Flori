# Mnemo

> 自托管的 AI 学习知识库 —— 把视频和论文自动炼成带截图与时间戳的结构化笔记，沉淀为可检索的个人知识体系。
>
> *Self-hosted AI knowledge base that turns videos & papers into structured, searchable notes.*

![Python](https://img.shields.io/badge/python-3.11+-blue) ![License](https://img.shields.io/badge/license-MIT-green) ![Docker](https://img.shields.io/badge/deploy-docker-2496ED)

投递一个视频链接或一篇 PDF，Mnemo 自动下载、转写、截图、OCR，再用 AI 整理成结构化笔记，攒成你自己的知识库。取名 Mnemo（记忆女神 Mnemosyne）——目标不止于"存下来"，而是"学得会、记得住"（学习/复习回路见 [ROADMAP](ROADMAP.md) M4）。

## 能做什么

- **多源摄入**：B 站 / YouTube / 本地视频，arXiv / 本地 PDF 论文
- **视频流水线**：下载 → 场景检测 → 关键帧 → 去重 → OCR → 字幕转写 → 标点 → 机械版笔记 → AI 智能版 → 质量评审
- **论文流水线**：PDF 解析 → 章节结构 → 图表提取 → AI 笔记 → 评审
- **两份笔记**：机械版（带标点逐字稿 + 关键帧截图 + OCR + 弹幕）/ 智能版（AI 按主题重组，含术语解释、要点回顾）
- **视觉证据**：笔记内嵌关键帧截图与时间戳，定位到原片对应片段
- **多 Provider AI 网关**：Anthropic / DeepSeek / Kimi / OpenAI / 本地 Ollama，带成本追踪与 `DRY_RUN` 空跑
- **分布式 Worker**：资源池 + 标签亲和，可随时加一台 GPU 机器接入
- **全 Docker、自托管、数据完全自有**

## 设计原则

文件是接口（步骤间用 JSON/MD 通信）· 幂等（输入指纹未变则跳过）· 故障隔离（单任务失败不影响其他）· 配置与代码分离（领域知识在 YAML/Prompt 里）。

## 架构

```
[手机/浏览器] ──HTTP──> [前端 nginx :80] ──/api · /ws──> [API :8000]
                                                            │  事件 ↕ Redis
                           [调度器(DAG)] ──队列(资源池/标签)──> [Worker: download · cpu · ai (+可选 gpu)]
                                                            └── SQLite(元数据) + 文件(产物)
```

同一套代码既能单机 `docker compose up` 全起，也能拆成「公网入口 + 后端服务器 + GPU 机」分布式部署（Worker 连同一个 Redis，按标签自取任务）。

## 快速开始（单机）

```bash
git clone https://github.com/Gwzlchn/Mnemo.git && cd Mnemo
cp .env.example .env            # 填 API_TOKEN(强随机串) + 一个 AI Provider 的 key

# 方式 A：拉取 CI 预构建镜像（推荐；私有镜像先 docker login ghcr.io）
docker compose pull && docker compose up -d

# 方式 B：本地从源码构建运行
docker compose -f docker-compose.dev.yml up -d --build

# 浏览器打开 http://<服务器IP>/ ，用 API_TOKEN 登录，投递第一个视频
```

> 公网访问：开放 80 端口 + 设好 `API_TOKEN` 即可，纯 IP 访问不需要域名。完整部署（含 GPU 机、分布式）见 [docs/08-deployment.md](docs/08-deployment.md)。

## 技术栈

Python 3.11 · FastAPI · Redis · SQLite · Vue 3 · Docker

## 系统要求

最低 4 核 / 8 GB / 50 GB；推荐 6+ 核 / 16 GB。GPU 可选（加速 Whisper / OCR）。

## 状态

**M1（视频 + 论文 MVP）已完成**，423 个单元测试在容器内通过。后续里程碑（知识库搜索、RAG 对话、学习/复习回路、原生客户端）见 [ROADMAP.md](ROADMAP.md)。

## 文档

设计文档见 [docs/README.md](docs/README.md)：系统架构、领域模型、接口契约、各模块详设、ADR。AI 协作开发约定见 [CLAUDE.md](CLAUDE.md)。

## License

[MIT](LICENSE)
