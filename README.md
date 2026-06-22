# Flori

> 自托管的 AI 学习知识库 —— 把视频、论文、文章、播客自动炼成带截图与时间戳的结构化笔记，沉淀为按领域分桶、可检索的个人知识体系。
>
> *Self-hosted AI knowledge base that turns videos, papers, articles & podcasts into structured, searchable notes.*

![Python](https://img.shields.io/badge/python-3.11+-blue) ![License](https://img.shields.io/badge/license-MIT-green) ![Docker](https://img.shields.io/badge/deploy-docker-2496ED)

[![CI](https://github.com/Gwzlchn/Flori/actions/workflows/ci.yml/badge.svg)](https://github.com/Gwzlchn/Flori/actions/workflows/ci.yml) [![backend coverage](https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2FGwzlchn%2FFlori%2Fbadges%2Fcoverage-backend.json)](https://github.com/Gwzlchn/Flori/actions/workflows/ci.yml) [![frontend coverage](https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2FGwzlchn%2FFlori%2Fbadges%2Fcoverage-frontend.json)](https://github.com/Gwzlchn/Flori/actions/workflows/ci.yml)

<!-- 变异分数(测试有效性,每日 cron 更新;徽章值=各核心模块当前分数,点开看趋势表)。首次 daily run 后生效。 -->
[![ai_gateway mutation](https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2FGwzlchn%2FFlori%2Fmutation-data%2Fmutation-ai_gateway.json)](https://github.com/Gwzlchn/Flori/blob/mutation-data/trend.md) [![db mutation](https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2FGwzlchn%2FFlori%2Fmutation-data%2Fmutation-db.json)](https://github.com/Gwzlchn/Flori/blob/mutation-data/trend.md) [![scheduler mutation](https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2FGwzlchn%2FFlori%2Fmutation-data%2Fmutation-scheduler.json)](https://github.com/Gwzlchn/Flori/blob/mutation-data/trend.md) [![worker mutation](https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2FGwzlchn%2FFlori%2Fmutation-data%2Fmutation-worker.json)](https://github.com/Gwzlchn/Flori/blob/mutation-data/trend.md)

投递一个视频/播客链接、一篇 PDF 或一个网页，Flori 自动下载、转写、截图、OCR，再用 AI 整理成结构化笔记，并把"讲清楚的概念"沉淀进按领域分桶的概念图，攒成你自己的知识库。

> **名字来源**：Flori 取自拉丁语 *florilegium*（"采花集"）——中世纪指从群书中采撷精华、汇编成册的选集，正是"把素材摘录、沉淀为知识"的隐喻。目标不止于"存下来"，而是"学得会、记得住"（学习/复习回路见 [ROADMAP](ROADMAP.md) M4）。

## 能做什么

- **多源摄入**：B 站 / YouTube / 本地视频，arXiv / 本地 PDF 论文，网页文章，单集音频/播客（URL 或上传）
- **视频流水线**：下载 → 转写 → 场景检测 → 关键帧 → 去重 → OCR → 弹幕 → 口播稿 → 机械版笔记 → AI 智能版 → 质量评审
- **论文 / 文章 / 播客流水线**：论文（PDF 解析 → 章节 → 图表 → AI 笔记 → 评审）/ 文章（正文解析 → 章节 → AI 笔记 → 评审）/ 播客（转写 → 解析 → AI 笔记 → 评审）
- **两份笔记**：机械版（带标点逐字稿 + 关键帧截图 + OCR + 弹幕）/ 智能版（AI 按主题重组，含术语解释、要点回顾）；智能版视频走两段式（先逐帧看图产视觉描述，再据机械稿 + 视觉描述纯文本生成笔记）
- **视觉证据**：笔记内嵌关键帧截图与时间戳，定位到原片对应片段
- **知识库（领域中心）**：知识按领域分桶成一组并行的概念图——术语页（跨来源综合定义 + 类型化出现处）、主题页（域内跨集合内容聚合）、术语库 CRUD（候选→采纳→回流 Prompt）；评审产出的概念自动喂养
- **全文搜索**：SQLite FTS5（trigram 中文子串匹配），跨领域/集合检索所有笔记
- **集合与订阅**：手动集合策展，或订阅 B 站 UP 主自动追更新内容（订阅是集合的一种属性，非独立实体）
- **多 Provider AI 网关**：Anthropic / DeepSeek / Kimi / OpenAI / 本地 Ollama / Claude CLI，带成本追踪与 `DRY_RUN` 空跑
- **分布式 Worker**：资源池 + 标签亲和，远程 worker 经 API 网关单条出站 HTTPS 接入（不连中心 Redis/MinIO），可随时加一台 GPU 机器
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

同一套代码既能单机 `docker compose up` 全起，也能拆成「公网入口 + 后端服务器 + GPU 机」分布式部署：核心内 Worker 直连 Redis，远程 Worker 经 API 的 `/api/runner/*` 网关单条出站 HTTPS 接入（注册换 per-worker token，长轮询认领、按标签自取任务、产物经网关代理），不直连中心 Redis/MinIO（见 [ADR-0009](docs/adr/0009-worker-gateway-outbound-https.md)）。

## 快速开始（单机）

```bash
git clone https://github.com/Gwzlchn/Flori.git && cd Flori
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

**M1（视频 + 论文 MVP）/ M2（知识库：领域概念图 + 集合订阅 + FTS5 搜索 + 术语库）/ M6（文章 + 播客）/ M-W（远程 worker 网关接入）均已完成**，全量 938 个单元测试在容器内通过（前端已按 Notion 设计全站重建并入 main）。后续里程碑（RAG 对话、学习/复习回路、原生客户端）见 [ROADMAP.md](ROADMAP.md)。

## 文档

设计文档见 [docs/README.md](docs/README.md)：系统架构、领域模型、接口契约、各模块详设、ADR。AI 协作开发约定见 [CLAUDE.md](CLAUDE.md)。

## License

[MIT](LICENSE) — 覆盖 Flori 自身源码。

### Third-party licenses / 运行期依赖许可

Flori 自身代码以 MIT 发布，但运行期会调用若干强 copyleft 依赖：**PyMuPDF / `fitz`（AGPL-3.0，用于 `steps/paper/step_02_pdf_parse.py`）**、**yutto / pysrt / bilibili-api（GPL-3.0）** 等。这些组件作为独立库 / 子进程依赖被调用，Flori 以自托管方式运行、**不作为打包二进制对外分发**，因此 MIT 仅覆盖 Flori 自有源码；如需再分发包含这些依赖的产物，请遵守其各自的 AGPL/GPL 条款。逐工具许可与传染边界分析见 [docs/13-dependencies.md](docs/13-dependencies.md)。
