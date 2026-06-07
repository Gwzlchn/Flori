# ROADMAP

> 里程碑 + 当前进度。详细 TODO 拆到各 Milestone 下。

## 当前状态

**阶段**：**M1 + Worker 层 GitLab-runner 化（M-W）+ M2 知识库 全部完成** · 远程 worker 单出站 HTTPS 接入；笔记可集合/全文搜索/术语积累
**最近会话**：2026-06-07 · ① M-W Worker 重构（aware-UTC + 管理页 + StepRunner + worker-gateway/token + GitLab-CI 配置 + DockerStepRunner/每步镜像 + 认领/上报/产物搬 gateway 真零隧道 + 安全加固）② B站扫码登录（DB 单一真相 + 随 job 下发 + yutto 注入）③ docker 执行器镜像名解析 + PYTHONPATH 修复（真机 e2e 抓到)④ M2 知识库（集合 + FTS5 trigram 全文搜索 + glossary 术语积累）；CI Actions 升 Node 24;NAS+ECS 全量部署新镜像验证（4 worker 在线、无头 e2e 通过）；单元测试 423 → **740 pass**（全程 CI 绿）
**下一步**：M5 GPU 加速（whisper 已就绪待 GPU 机验 + PaddleOCR-GPU 实现）/ M2.5 AI-native（RAG + 知识对话，先证伪 FTS5 不够再上向量，见 .local/IDEAS.txt）

## 里程碑

### M0 · 架构设计就绪

目标：所有架构文档齐全，新 Claude 会话读完 `CLAUDE.md` + `docs/` 能直接上手。

- [x] 文档体系大纲 (`docs/README.md`)
- [x] 愿景 (`docs/00-vision.md`)
- [x] 系统架构 (`docs/01-architecture.md`)
- [x] 领域模型 (`docs/02-domain-model.md`)
- [x] 接口契约 (`docs/03-contracts.md`)
- [x] 模块详设 (`docs/04-module-design/*.md`) — 10 个文件
- [x] 内容适配器 (`docs/05-content-adapters.md`)
- [x] Prompt 工程 (`docs/06-prompt-engineering.md`)
- [x] 安全 (`docs/07-security.md`)
- [x] 部署 (`docs/08-deployment.md`)
- [x] 测试 (`docs/09-testing.md`)
- [x] 可观测 (`docs/10-observability.md`)
- [x] 开发流程 (`docs/11-dev-workflow.md`)
- [x] ADR (`docs/adr/*.md`) — 8 个 + README
- [x] CLAUDE.md — 已更新
- [x] README.md — 已更新

### M1 · 核心 MVP（视频 + 论文）

目标：投递视频 URL 或上传 PDF → 自动处理 → 在线阅读笔记。

- [x] 共享层（models/db/redis/storage/ai_gateway/step_base）
- [x] 调度器 + Redis + Worker 框架 + StorageBackend
- [x] AI 网关（多 Provider 路由 + 成本追踪 + DRY_RUN 模式）
- [x] 领域 Profile + 风格标签
- [x] 视频分析步骤（16 个步骤 + StepBase 改造）
- [x] 论文分析步骤（PDF 解析 + 章节/图表提取 + AI 笔记）
- [x] Worker 管理（注册/心跳/持久记录/draining）
- [x] FastAPI 服务（任务管理 + 文件服务 + Worker API）
- [x] 前端：投递 + 进度 + 笔记阅读 + Worker 管理（手机版）
- [x] 单元测试 423 pass（容器内 docker compose test 实跑）
- [x] 集成测试基础设施（docker-compose.integration.yml + E2E 脚本）
- [x] 集成测试：下载 + CPU 步骤链 4/4 pass
  - [x] 视频上传 → 全 video pipeline CPU 链（scene 26s + frames 18s + dedup 2s + OCR 189s）
  - [x] B站 BV 号真实下载 → CPU 链 + 弹幕解析
  - [x] PDF 上传 → paper pipeline CPU 链（parse + sections + figures）
  - [x] arXiv URL 真实下载 → paper pipeline CPU 链
- [x] Bug fixes：tag 调度 + 场景检测 callback + yutto 参数 + 文件搜索范围（6 个）
- [x] 集成测试：AI 步骤（TC-AI-1 视频 + TC-AI-2 论文，Kimi provider）
- [x] 并发安全测试（乐观锁 CAS 冲突 + exec_id 去重 + on_step_done 幂等 + skip 死锁守卫 + 延迟任务取消）
- [ ] Cloudflare Tunnel 公网暴露
- [ ] B站扫码登录
- [x] CI/CD（GitHub Actions + ghcr.io 镜像发布 + Watchtower 自动部署；Actions 已升 Node 24）

### M-W · Worker 层 GitLab-runner 化 ✅（2026-06-07 完成）

目标：worker 高内聚低耦合、易拓展；远程 worker（公司 GPU 机）零隧道、单出站 HTTPS 接入。
全程可灰度、可独立回退；每刀经对抗式等价核对 + 全量回归，保留 DAG/资源池/scene↔cpu 互斥/exec_id 去重/WS 进度等不变量。

- [x] 全后端 aware-UTC + Worker 管理页（状态后端权威）+ 运行中日志可见
- [x] `WorkerTransport` + `StepRunner` 执行器抽象
- [x] worker-gateway 注册/心跳 + per-worker 可吊销 token + `GatewayTransport`
- [x] pipelines 改 GitLab-CI 风格（variables/extends/rules/needs）+ `DockerStepRunner` + 每步镜像（base/heavy/gpu）
- [x] 认领/上报搬服务端（`/api/runner/jobs/*` + 共享 `runner_ops`）+ 产物经网关代理 + 纯网关模式（worker 不连 redis/minio）
- [x] 安全加固：密钥按需注入 + token 按 pools 授权 + 重试按失败类型

### M2 · 知识库 ✅（2026-06-07 完成主体）

目标：多视频成为知识库，可搜索、有记忆。

- [x] 集合管理（按主题/课程/系列组织笔记）——CRUD + 删集合解绑保留 job + job_count 维护
- [x] Profile 动态积累——glossary 表 + scheduler 从 review.missing_concepts 采集候选 → 一键采纳 → 同步进 Profile.terminology（OCR/字幕高频词抽取为后续增强，未做）
- [x] SQLite FTS5 全文搜索——notes_fts5 虚表(trigram 中文子串)+ scheduler 侧索引 + /api/search facet/高亮
- [x] 前端：集合视图 + 搜索 + 术语 + Profile 编辑

### M2.5 · AI-native 知识交互（核心拐点）

目标：从"处理工具"变为"知识应用"。用户可以和自己的知识库对话、提问、发现关联。

- [ ] 向量嵌入（笔记分段 → embedding → sqlite-vec / chromadb）
- [ ] RAG 检索（语义搜索 + FTS5 混合排序）
- [ ] 知识对话（Chat with your KB）
  - 跨文档问答：「Transformer 有哪些注意力变体？」→ 检索多篇笔记 → 综合回答
  - 带视觉证据：回答嵌入截图 + 时间戳跳转（纯文本 RAG 做不到的差异化）
  - 对比分析：「这篇论文和那个视频的观点有什么不同？」
- [ ] 知识图谱 / 自动关联
  - 实体提取（人名、公司、术语、概念）
  - 跨笔记交叉引用：「此方法与 BV1z6 的思路类似」
  - 概念网络可视化
- [ ] 自动标签 + 智能分类（摄入时自动归类到已有集合）

### M3 · 原生客户端（iOS + Mac）

目标：手机/电脑上有原生体验，随时投递和阅读笔记。

- [ ] Mac App（WKWebView 包 Vue3，快速出 MVP）
  - 菜单栏快捷投递（粘贴 URL → 一键入库）
  - 原生通知（任务完成/失败推送）
  - 本地 Worker 可选启动（利用 Mac 本机算力）
- [ ] iOS App（SwiftUI）
  - Share Extension（从 B 站/Safari 分享到 App 直接投递）
  - 笔记阅读 + 截图浏览 + 时间戳跳转
  - 推送通知（任务完成）
- [ ] 视频回放 + 时间戳跳转（AVPlayer / 嵌入播放器）
- [ ] 标注/高亮功能
- [ ] 离线阅读（已完成笔记缓存到本地）

### M4 · Agent 自主行为

目标：系统不只被动处理，还能主动发现、推荐、提醒。

- [ ] RSS / UP 主订阅（自动监控新视频，满足条件时自动入库）
- [ ] 知识缺口分析（「你的强化学习知识只有 2 篇，推荐补充这些」）
- [ ] 矛盾检测（新摄入内容与已有知识矛盾时提醒）
- [ ] 学习反馈环
  - 笔记 → 自动生成闪卡 / Quiz
  - 间隔重复（Spaced Repetition）
  - 掌握程度追踪 → 影响后续内容的 AI 笔记深度
- [ ] 周报 / 摘要（「本周入库 12 篇，核心发现：…」）

### M5 · GPU 加速

目标：处理速度大幅提升。

- [ ] GPU Worker + Whisper
- [ ] PaddleOCR GPU
- [ ] 场景检测 GPU 解码
- [x] GitLab Runner 化接入（见 M-W：gateway + token + 每步镜像；GPU worker 单出站接入就绪）

### M6 · 文章分析 + 多源扩展 ✅（2026-06-07 完成）

目标：网页文章/公众号/播客也能入库。

- [x] 网页抓取适配器（source_detect http_article + step_00_download 抓 HTML）
- [x] 正文提取（trafilatura，中文友好，纯 Python）
- [x] 文章笔记模板（article pipeline：parse→sections→smart→review）
- [x] 播客 / 音频支持（单集音频 URL + 上传；audio pipeline：whisper→分段→smart_podcast→review；RSS 订阅追更留 M4/Agent）

### M7 · 多租户 + 商业化

目标：支持多用户，可选云端 Worker 付费模式。

- [x] Storage 换 S3/MinIO（跨机器 Worker 共享产物；远程 worker 可经网关代理免直连）
- [ ] 多租户隔离（用户注册 + OAuth / Apple Sign In）
- [ ] SQLite → Postgres（多用户并发）
- [ ] 云端 Worker 集群（托管 GPU/AI 算力）
- [ ] 计费系统（按任务或按 token 计费）
- [ ] Worker 混合部署（用户自建 Worker 免费 + 云端 Worker 付费）
- [ ] 多人协作 / 共享知识库
- [ ] PDF 导出 + Anki / Obsidian 导出

## 原则

1. **每个里程碑可独立交付和使用**，M1 完成就能日常使用
2. **先视频后扩展**，视频是最复杂的（音视频+截图+字幕），论文/文章简单得多
3. **设计先行**，M0 做透设计，M1 开始才写代码
4. **AI 协作**，每个 M 可以开独立 Claude 会话并行开发
5. **M2.5 是拐点**，有了 RAG + 对话，从工具变应用
6. **M3 提升体验**，原生 App 让日常使用更顺手
7. **M7 才商业化**，先把产品做好再考虑收费
