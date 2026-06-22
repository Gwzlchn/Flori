# 前端

> Vue 3 + Vite + Tailwind CSS。手机优先、电脑增强。
> 个人工具，重功能和信息密度，不做花哨动画。

## 1. 技术栈

| 组件 | 选型 | 理由 |
|------|------|------|
| 框架 | Vue 3 + Composition API | 轻量、响应式 |
| 构建 | Vite | 快速 HMR |
| 样式 | Tailwind CSS | 原子类、手机优先 |
| 状态 | Pinia | Vue 3 官方状态管理 |
| 路由 | Vue Router 4 | SPA |
| Markdown | markdown-it + 自定义插件 | 时间戳链接、图片替换 |
| 视频 | video.js | 时间戳跳转 |
| 二维码 | qrcode-vue3 | B站扫码 |
| PDF | html2pdf.js | 前端导出 |
| WebSocket | 原生 | 进度推送 |

## 2. 路由

前端 IA 以**领域（知识库）为锚**：知识库 ⊃ 集合 ⊃ 内容；概念（术语 / 主题）是知识层。后端路由（`/api/domains`、`/api/jobs` 等）不变，仅前端路径与文案用新词。以 `frontend/src/router/index.ts` 为准：

```
/                               知识库列表（领域卡片网格）          HomeView
/kb/:domain                     领域工作台（集合 + 内容 + 概念 + 主题）DomainWorkspaceView
/kb/:domain/concepts/:term      概念详情（定义 / 出现处 / 关联）     TermDetailView
/kb/:domain/topics/:topic       主题页（跨集合/来源聚合）            TopicView
/content                        内容列表（全部 job，含分面过滤）     JobListView
/content/:id                    内容详情（步骤进度 + 笔记/评审入口）  JobDetailView
/collections                    集合列表（含订阅集合）              CollectionsView
/collections/:id                集合详情                          CollectionDetailView
/search                         全文检索                          SearchView
/glossary                       术语库（CRUD / 采纳候选 / 标主题）   GlossaryView
/system                         系统 / Worker 管理                 WorkersView
/system/workers/:id             Worker 详情                       WorkerDetailView
/settings                       设置（cookies / AI Provider / 存储）SettingsView
/about                          关于                              AboutView
```

旧路径（`/jobs`、`/jobs/:id`、`/workers`、`/domains/:domain/terms/:term` 等）保留为 `redirect`，过渡期后清理。

## 3. 布局

### 手机 (<768px)

```
┌──────────────────────┐
│ ☰  知识库        ⚙️   │  ← 顶栏
├──────────────────────┤
│                      │
│      页面内容         │
│                      │
├──────────────────────┤
│ 🏠  📚  ➕  🔍  ⚙️  │  ← 底部导航（知识库/内容/投递/搜索/系统）
└──────────────────────┘
```

### 电脑 (≥768px)

```
┌─────────────────────────────────────────────────┐
│ 知识库              🔍 搜索...           ⚙️ 设置 │
├────────┬────────────────────────────────────────┤
│ 侧边栏  │              页面内容                  │
│ 知识库  │                                        │
│ 内容    │                                        │
│ 集合    │                                        │
│ 术语库  │                                        │
│ 搜索    │                                        │
│ 系统    │                                        │
└────────┴────────────────────────────────────────┘
```

布局壳由 `AppShell` / `TopBar` / `AppSidebar` / `AppBottomNav` 组成（Notion 风格，重信息密度）。

## 4. 核心页面

### 知识库列表（HomeView，`/`）

- 领域卡片网格：每张卡含展示元数据（`display_name` / `icon` / `color` / `description`）+ 计数（集合数 / 内容数 / 概念数 / 订阅数 / 最近活跃），数据来自 `GET /api/domains`
- 「新建知识库」入口（`POST /api/domains`，写 profile 元数据，可建空领域）
- 快速投递框（URL / 上传 + 领域 + 集合选择，`POST /api/jobs` / `/api/jobs/upload`）

### 领域工作台（DomainWorkspaceView，`/kb/:domain`）

聚合该领域的情景层与语义层（`GET /api/domains/{domain}`）：

- 情景层：集合卡（含订阅集合）+ 最近内容
- 语义层：Top 概念（含 `suggested` 候选 + 待审计数）、主题（`style_tags` 聚合）
- 概念时间线视图（`ConceptTimeline` 组件，`GET /api/domains/{domain}/concept-timeline`）

### 概念详情 / 主题页

- 概念详情（TermDetailView）：定义 + 出现处（typed occurrences：`{job_id, content_type, location}`）+ 关联概念 + 是否主题
- 主题页（TopicView）：该领域内带某 `style_tags` 标签的内容，跨集合 / 跨来源聚合

### 术语库（GlossaryView，`/glossary`）

- 列术语（按 domain / status 过滤）、手动新增（直接 `accepted` 并回流 Profile）、编辑定义/关联、采纳候选（`accept`）、标记/取消主题（`is_topic`）、删除
- 走 `/api/glossary/*`

### 内容详情（JobDetailView，`/content/:id`）

- 内容信息（标题/来源/类型特有信息）
- 步骤进度条（步骤数由 pipeline 决定，实时 WebSocket 更新；步骤名为各 pipeline 内 `01..N`）
- 产物入口（智能笔记 / 机械版 / 逐字稿 / 评审）；评审 `parse_failed` 时提示重评
- 失败时显示错误 + 重试 / 重跑（含换 provider 重跑智能笔记 `rerun-smart`）

### 笔记阅读

**手机**：纯 Markdown 渲染 + 内嵌截图 + 可点击时间戳

**电脑**：左侧笔记 + 右侧视频播放器

- 截图路径替换：`![](assets/xxx.jpg)` → `<img src="/api/jobs/{id}/assets/xxx.jpg">`
- 时间戳链接：`[02:34]` → 点击跳转视频到 2:34
- 章节导航：右侧显示 `##` 标题列表
- 标注功能 (M3)：选中文字 → 高亮/笔记/书签

### Worker 管理页

```
┌──────────────────────────────────────────────────────────┐
│ Worker 管理                              [刷新]          │
├──────────────────────────────────────────────────────────┤
│                                                          │
│  概览: 在线 4 / 历史 6    处理中 2    今日完成 47        │
│                                                          │
│  ┌────────────────────────────────────────────────────┐  │
│  │ 🟢 ai-a1b2     AI     idle                        │  │
│  │    office-pc · Claude Max · 已完成 142 · 失败 3    │  │
│  │    运行 7h12m · 上次心跳 5s 前                     │  │
│  │    [排空] [备注]                                   │  │
│  ├────────────────────────────────────────────────────┤  │
│  │ 🟡 ai-c3d4     AI     busy → 10_smart (j_xxx)    │  │
│  │    local-01 · API Key · 已完成 89 · 失败 1         │  │
│  │    运行 3h45m · 当前任务 2m30s                     │  │
│  │    [排空]                                          │  │
│  ├────────────────────────────────────────────────────┤  │
│  │ 🟢 gpu-e5f6    GPU    idle                        │  │
│  │    gpu-server · RTX 4090 · 已完成 88 · 失败 1     │  │
│  │    运行 5h20m · 上次心跳 3s 前                     │  │
│  │    [排空]                                          │  │
│  ├────────────────────────────────────────────────────┤  │
│  │ 🔴 cpu-i9j0    CPU    offline (2h 前)             │  │
│  │    old-laptop · 已完成 23 · 失败 5                 │  │
│  │    [移除记录]                                      │  │
│  └────────────────────────────────────────────────────┘  │
│                                                          │
│  ── 接入新 Worker ──                                     │
│  [生成接入 token]  →  mnw-xxxxxxxx  (仅此一次完整展示)    │
│  复制以下命令到目标机器执行:                              │
│  ┌──────────────────────────────────────────────────┐    │
│  │ docker run -d --restart unless-stopped \         │    │
│  │   -e GATEWAY_URL=https://<host> \                │    │
│  │   -e WORKER_REGISTRATION_TOKEN=mnw-xxxxxxxx \    │    │
│  │   ghcr.io/<owner>/flori --type gpu               │    │
│  └──────────────────────────────────────────────────┘    │
│  [复制命令]  类型: [GPU ▼]                               │
└──────────────────────────────────────────────────────────┘
```

**状态灯**（公共态后端权威派生，前端只渲染）：🟢 online-idle / 🟡 online-busy / 🟠 draining / ⚪ offline / 🔴 stale

**操作**：
- 排空（draining）：完成当前任务后不再接新任务，用于安全下线
- 备注：给 Worker 加人工备注（如"内网机器，有 Claude 订阅"）
- 移除：清理已下线 Worker 的历史记录

**接入引导**（`WorkerJoinGuide` 组件）：远程 worker 走网关 token 模型（见 [ADR-0009](../adr/0009-worker-gateway-outbound-https.md)）——
1. 点「生成接入 token」铸一次性 registration token（`POST /api/workers/registration-token`，可复用、可重置）
2. 复制 `docker run` 命令到目标机器执行，worker 持该 token 经 `POST /api/runner/register` 换取 per-worker token，之后单条出站 HTTPS 接入网关（注册/心跳/认领/上报/产物代理），**不直连 Redis/MinIO**
3. 另提供单机直连 redis/minio 的命令变体（仅同机/内网部署有意义）

## 5. WebSocket 状态管理

```javascript
// stores/jobs.js (Pinia)
export const useJobStore = defineStore('jobs', () => {
  const activeJobs = ref({})

  function connectJob(jobId) {
    const ws = new WebSocket(`/api/ws/jobs/${jobId}`)
    ws.onmessage = (event) => {
      const data = JSON.parse(event.data)
      updateStep(jobId, data)
    }
  }

  function updateStep(jobId, event) {
    const job = activeJobs.value[jobId]
    if (!job) return
    const step = job.steps.find(s => s.name === event.step)
    if (!step) return

    switch (event.event) {
      case 'step_start':    step.status = 'running'; break
      case 'step_progress': step.pct = event.pct; step.detail = `${event.current}/${event.total}`; break
      case 'step_done':     step.status = 'done'; step.duration = event.duration_sec; break
      case 'step_failed':   step.status = 'failed'; step.error = event.error; break
      case 'job_done':      job.status = 'done'; break
    }
  }
})
```

## 6. 组件清单

以 `frontend/src/components/` 为准：

```
components/
├── layout/        AppShell, AppLayout, TopBar, AppHeader, AppSidebar, AppBottomNav
├── job/           JobCard, JobSubmitForm, StepWorkbench
├── notes/         MarkdownViewer, ChapterNav
├── collection/    CollectionCard, CollectionEditDialog
├── worker/        WorkerCard, WorkerJoinGuide
├── settings/      BiliLogin, ProfileEditor
├── auth/          CookieUpload
├── ConceptTimeline.vue   # 领域工作台概念时间线
└── common/        Card, Badge, StatusBadge, ProgressBar, Toast, Modal,
                   ConfirmDialog, PrimaryButton, EmptyState, ErrorState, LoadingState
```

## 7. 响应式断点

```
< 768px:   手机 — 底部导航 + 单列 + 全屏内容
≥ 768px:   平板/电脑 — 侧边栏 + 双列
≥ 1024px:  笔记分屏（左笔记 + 右视频）
```
