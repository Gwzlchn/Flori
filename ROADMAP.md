# ROADMAP

> 里程碑 + 当前进度。详细 TODO 拆到各 Milestone 下。

## 当前状态

本页同时记录里程碑的首次交付历史和当前成熟度。下方 `[x]` 表示该里程碑已有可用首版，
不等于可靠性、真实集成和知识质量已经闭环；当前口径以下表为准：

| 状态 | 能力 |
|------|------|
| 完整 | 来源 registry、OpenAPI 枚举、API 入队前 fail-closed 与前端来源目录同源 |
| first-pass | 视频 / 论文 / 文章 / 音频四类摄入；FTS5 Search、跨源 Ask 与 MCP；集合订阅；概念图与评审；手工建卡 SRS；知识雷达；远程 Worker 网关 |
| first-pass | Canonical evidence 已接通视频/音频时间、PDF 页码、文章锚点、OCR image bbox 与四类 smart exact-quote 的 producer / resolver / Search/Ask/MCP/内容详情；跨语言与语义改写仍等待独立 attestation |
| 未开始 | 原生客户端、通知 / PWA、自动分类、知识缺口与矛盾检测、证据型自动卡片 |

向量检索由检索黄金集决定。24 个四类型 job 和 96 条冻结查询证明 exact、跨来源、过滤、
确定性与延迟基线可靠，并因 paraphrase、synonym、cross-language 缺口触发独立候选实验。
候选虽达到质量收益门，但同层 Search warm P95 持续超过 FTS5 的 2 倍预算，因此该阶段已关闭并
完整回滚；生产仍只使用 FTS5，未引入向量 schema、依赖、配置或模型文件。

测试结果不在文档冻结数字。主分支实时结果看 README 的 CI / coverage 徽章；本地唯一入口和
E2E 分层见 [`scripts/test.sh`](scripts/test.sh) 与 [`docs/09-testing.md`](docs/09-testing.md)。

## 里程碑

### M0 · 架构设计就绪

目标：所有架构文档齐全，新协作者读完 `CLAUDE.md` + `docs/` 能直接上手。

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
- [x] ADR (`docs/adr/*.md`) — 14 个 + README
- [x] CLAUDE.md — 已更新
- [x] README.md — 已更新

### M1 · 核心 MVP（视频 + 论文）

目标：投递视频 URL 或上传 PDF → 自动处理 → 在线阅读笔记。

- [x] 共享层（models/db/redis/storage/ai_gateway/step_base）
- [x] 调度器 + Redis + Worker 框架 + StorageBackend
- [x] AI 网关（多 Provider 路由 + 成本追踪 + DRY_RUN 模式）
- [x] 领域 Profile + 风格标签
- [x] 视频分析步骤 + StepBase 改造
- [x] 论文分析步骤（arXiv HTML 优先 / PDF 直读 + 章节 + 条件翻译 + AI 笔记 + 概念 + 评审）
- [x] Worker 管理（注册/心跳/持久记录/暂停恢复/per-worker 并发）
- [x] FastAPI 服务（任务管理 + 文件服务 + Worker API）
- [x] 前端：投递 + 进度 + 笔记阅读 + Worker 管理（手机版）
- [x] 容器化单元测试基线（实时结果由 CI / coverage gate 给出）
- [x] 集成测试基础设施（docker-compose.integration.yml + E2E 脚本）
- [x] 下载 + CPU 步骤链的首版 integration 场景
  - [x] 视频上传 → 全 video pipeline CPU 链（scene 26s + frames 18s + dedup 2s + OCR 189s）
  - [x] B站 BV 号真实下载 → CPU 链 + 弹幕解析
  - [x] PDF 上传 → paper pipeline CPU 链（download + PDF 元数据 + sections）
  - [x] arXiv URL 真实下载 → paper pipeline CPU 链
- [x] Bug fixes：tag 调度 + 场景检测 callback + yutto 参数 + 文件搜索范围（6 个）
- [x] 集成测试：AI 步骤（TC-AI-1 视频 + TC-AI-2 论文，Kimi provider）
- [x] 并发安全测试（乐观锁 CAS 冲突 + exec_id 去重 + on_step_done 幂等 + skip 死锁守卫 + 延迟任务取消）
- [ ] ~~Cloudflare Tunnel 公网暴露~~（未采用：实际边缘为 Caddy + 反向 SSH，无 cloudflared；见 ADR-0006 已废弃 / ADR-0009 网关）
- [x] B站扫码登录（passport QR：`/api/bili/login/start` 生成二维码 + `/login/poll` 轮询 + `/status` + `/logout`）
- [x] CI/CD（GitHub Actions + ghcr.io 镜像发布 + Watchtower 自动部署；Actions 已升 Node 24）

### M-W · Worker 层 GitLab-runner 化 ✅（2026-06-07 完成）

目标：worker 高内聚低耦合、易拓展；远程 worker（含远端 GPU 机）零隧道、单出站 HTTPS 接入，
保留 DAG / 资源池 / scene↔cpu 互斥 / exec_id 去重 / WS 进度等不变量。

- [x] 全后端 aware-UTC + Worker 管理页（状态后端权威）+ 运行中日志可见
- [x] `WorkerTransport` + `StepRunner` 执行器抽象
- [x] worker-gateway 注册/心跳 + per-worker 可吊销 token + `GatewayTransport`
- [x] pipelines 改 GitLab-CI 风格（variables/extends/rules/needs）+ `DockerStepRunner` + 每步镜像（base/heavy/gpu）
- [x] 认领/上报搬服务端（`/api/runner/jobs/*` + 共享 `runner_ops`）+ 产物经网关代理 + 纯网关模式（worker 不连 redis/minio）
- [x] 安全加固：密钥按需注入 + token 按 pools 授权 + 重试按失败类型

### M2 · 知识库 ✅（2026-06-07 完成主体）

目标：多视频成为知识库，可搜索、有记忆。

- [x] 集合管理（按主题/课程/系列组织笔记）——CRUD + 删集合解绑保留 job + job_count 维护
- [x] 订阅集合——集合带 `source_type`/`source_id` 即订阅；支持类型以 `configs/sources.yaml` 为准，无独立 subscription 表/实体
- [x] Profile 动态积累——glossary 表（PK `(domain,term)`，typed occurrences）+ scheduler 从 review.key_terms（讲清楚的概念 + 候选定义）采集候选 → 一键采纳 → 回流 Profile.terminology（missing_concepts 仅评审面板，不入库）
- [x] 领域中心 + 概念图——领域为派生视图（jobs∪collections∪glossary 的 distinct domain ∪ 有 profile 的领域），profile yaml 存展示元数据；术语库 CRUD/accept/标主题（is_topic）；概念时间线/主题聚合
- [x] SQLite FTS5 全文搜索——notes_fts5 虚表(trigram 中文子串)+ scheduler 侧索引 + /api/search facet/高亮
- [x] 前端全站重建（Notion 设计，领域中心式 IA）：领域知识库列表 + 工作台 + 术语/主题页 + 集合视图 + 搜索 + 术语库 CRUD + Profile 编辑

### M2.5 · AI-native 知识交互（first-pass，检索质量门已建立）

目标：从"处理工具"变为"知识应用"。用户可以和自己的知识库对话、提问、发现关联。

- [x] FTS5 检索 + 跨源综合问答 + MCP 搜索 / 读取（四类真实 completion、引用清单与 24/96 质量门已闭环）
- [x] 混合向量检索收益门（候选质量达标但延迟门失败，生产路径完整回滚并保留纯 FTS5）
- [x] 知识对话首版（Ask）
  - 跨文档问答：「Transformer 有哪些注意力变体？」→ 检索多篇笔记 → 综合回答
  - 对比分析：「这篇论文和那个视频的观点有什么不同？」
- [x] Canonical evidence 首版：视频/音频时间、PDF 页码、文章锚点、OCR image bbox 及 Search/Ask/MCP/内容详情同身份闭环
- [x] Smart exact-quote 门：四类 producer 的有界 support text、服务端双重复算与恶意降级拒绝
- [x] 概念定义真实化：append-only history、精确 canonical occurrence、可靠评审 attestation、current+lock CAS 与自动/手动受控重综合；REST/MCP/UI 使用同一安全投影
- [ ] 跨语言 translated/smart 与 paraphrase/semantic claim 的独立 attestation（当前显式空映射）
- [x] 领域概念图首版（术语 / 主题 / occurrence / 时间线与跨来源聚合）
- [ ] 自动实体关系与跨笔记推理关联
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

- [x] 来源订阅首版（B站、YouTube、RSS / Atom、本地目录与在线书目录；类型以 registry 为准）
- [ ] 知识缺口分析（「你的强化学习知识只有 2 篇，推荐补充这些」）
- [ ] 矛盾检测（新摄入内容与已有知识矛盾时提醒）
- [x] 手工建卡 + 间隔重复 SRS 首版
- [ ] 证据型自动卡片、批量采纳与概念掌握度闭环
- [x] 知识雷达 / 周摘要首版（可靠性与质量门待补）

### M5 · GPU 加速

目标：处理速度大幅提升。

- [ ] GPU Worker + Whisper（代码就绪，目前仅在 CPU 上验证过；GPU 路径尚未在真机跑通）
- [ ] PaddleOCR GPU
- [ ] 场景检测 GPU 解码
- [x] GitLab Runner 化接入（见 M-W：gateway + token + 每步镜像；GPU worker 单出站接入就绪）

### M6 · 文章分析 + 多源扩展 ✅（2026-06-07 完成）

目标：网页文章/公众号/播客也能入库。

- [x] 网页抓取适配器（source_detect http_article + step_01_download 抓 HTML）
- [x] 正文提取（trafilatura，中文友好，纯 Python）
- [x] 文章笔记模板（article pipeline：parse→sections→smart→review）
- [x] 播客 / 音频支持（单集音频 URL + 上传；audio pipeline：whisper→分段→smart_podcast→review；RSS / Atom 订阅已提供首版）

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
4. **可并行**，每个 M 可拆成独立模块并行开发
5. **AI-native 以证据和评测驱动**，先补可靠问答与引用质量，再按黄金集决定是否增加向量层
6. **M3 提升体验**，原生 App 让日常使用更顺手
7. **M7 才商业化**，先把产品做好再考虑收费
