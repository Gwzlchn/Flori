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

```
/                           首页（快速投递 + 概览 + 进行中 + 最近完成）
/jobs                       任务列表
/jobs/:id                   任务详情（步骤进度）
/notes/:jobId               笔记阅读（智能版）
/notes/:jobId/mechanical    机械版笔记
/search                     全局搜索 (M2)
/collections                集合列表 (M2)
/collections/:id            集合详情 (M2)
/workers                    Worker 管理（状态/统计/操作）
/settings                   设置（cookies/AI Provider/存储）
```

## 3. 布局

### 手机 (<768px)

```
┌──────────────────────┐
│ ☰  视频知识库    ⚙️   │  ← 顶栏
├──────────────────────┤
│                      │
│      页面内容         │
│                      │
├──────────────────────┤
│ 🏠  📋  ➕  🔍  📚  │  ← 底部导航
└──────────────────────┘
```

### 电脑 (≥768px)

```
┌─────────────────────────────────────────────────┐
│ 视频知识库          🔍 搜索...           ⚙️ 设置 │
├────────┬────────────────────────────────────────┤
│ 侧边栏  │              页面内容                  │
│ 首页    │                                        │
│ 任务    │                                        │
│ 集合    │                                        │
│ 设置    │                                        │
└────────┴────────────────────────────────────────┘
```

## 4. 核心页面

### 首页

- 快速投递框（URL + 集合选择 + 投递按钮）
- 概览统计（视频数/笔记数/处理中/术语数）
- 进行中任务列表（实时进度条，WebSocket 更新）
- 最近完成（标题 + 评分 + 入口）

### 任务详情

- 内容信息（标题/来源/类型特有信息）
- 步骤进度条（步骤数由 pipeline 决定，实时 WebSocket 更新）
- 产物入口（笔记/逐字稿/评审）
- 失败时显示错误 + 重试按钮

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
│  │ 🟡 ai-c3d4     AI     busy → 08_smart (j_xxx)    │  │
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
│  复制以下命令到目标机器执行:                              │
│  ┌──────────────────────────────────────────────────┐    │
│  │ docker run -e REDIS_URL=... -e MINIO_URL=...    │    │
│  │   worker:latest python3 worker.py --type gpu     │    │
│  └──────────────────────────────────────────────────┘    │
│  [复制命令]  类型: [GPU ▼]                               │
└──────────────────────────────────────────────────────────┘
```

**状态灯**：🟢 idle / 🟡 busy / 🟠 draining / 🔴 offline

**操作**：
- 排空（draining）：完成当前任务后不再接新任务，用于安全下线
- 备注：给 Worker 加人工备注（如"内网机器，有 Claude 订阅"）
- 移除：清理已下线 Worker 的历史记录

**接入引导**：页面底部自动生成 docker run 命令（含当前的 Redis/MinIO 连接信息），复制到目标机器执行即可接入。

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

```
components/
├── layout/        AppHeader, AppSidebar, AppBottomNav, AppLayout
├── job/           JobCard, JobProgress, JobStepBar, JobSubmitForm
├── notes/         MarkdownViewer, VideoPlayer, TimestampLink, NotesSplitView
├── auth/          BilibiliQrLogin, CookieUpload
├── search/        SearchBar, SearchResult (M2)
├── collection/    CollectionCard, GlossaryPanel (M2)
└── common/        ProgressBar, StatusBadge, Toast, ConfirmDialog
```

## 7. 响应式断点

```
< 768px:   手机 — 底部导航 + 单列 + 全屏内容
≥ 768px:   平板/电脑 — 侧边栏 + 双列
≥ 1024px:  笔记分屏（左笔记 + 右视频）
```
