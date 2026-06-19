# Mnemo 前端开发交接（HANDOFF）

> 给接手的 Claude Code：照这份文档 + 原型 + 起步工程，直接开发真实前端。三者配套，本文件是入口。

## 0. 交付物

| 文件 | 是什么 | 怎么用 |
|------|--------|--------|
| `design/prototype.html` | 全站高保真原型（15 页 + 弹窗，可交互） | 视觉与交互的「所见即所得」基准。浏览器打开，底部黑条切换页面 |
| `design/mnemo.css` | 设计系统：token（CSS 变量）+ 全部组件类 | 已拷进起步工程 `src/assets/mnemo.css`，全局引入；组件直接用它的类 |
| `design/DESIGN-SPEC.md` | IA 心智模型、状态枚举、后端能力清单、逐页 API 对接点 | 查后端 API 与状态映射。**注意：视觉/命名以原型为准，此文档为早期版，部分偏旧** |
| `design/handoff/frontend-starter/` | **Vue 3 起步工程**（骨架已搭好） | `npm install && npm run dev` |

## 1. 跑起来

```bash
cd design/handoff/frontend-starter
npm install
npm run dev
```

技术栈：Vite · Vue 3（`<script setup lang="ts">`）· Vue Router 4 · lucide-vue-next。

## 2. 心智模型 / 命名（不可改）

```
知识库(顶层锚) ⊃ 集合 ⊃ 内容        概念 = 知识层（从内容抽取，跨内容连成网）
```

- 命名一律用：**知识库 / 集合 / 内容 / 概念**。不要出现 领域 / 任务·job / 术语。
- 后端路由仍是 `/api/domains`、`/api/jobs`（后端契约不改），但 **UI 文案用新词**。
- 侧栏是三级折叠树：知识库 → 集合 → 内容（来源 B站/arXiv/公众号 作为每条内容的小标记）。

## 3. 工程约定

- **样式**：全局 `import './assets/mnemo.css'`（`main.ts` 已做）。组件直接用 mnemo.css 的类：`class="btn pri"` / `card pad` / `badge b-ok` / `chip on` …。**不要重写样式、不要用 Tailwind 工具类堆样式。** `tailwind.config.js` 里的 token 仅备用（需要工具类时取 `text-ink-700` / `bg-brand-600` / `rounded-md` 等）。
- **图标**：`lucide-vue-next`，如 `<Plus :size="16" />`。尺寸跟随原型 mnemo.css 的层级（标题 18 / 卡标 15 / 正文 16 / 元信息 13 / 徽章 12）。
- **数据**：起步工程里是写死的示例数据；接后端处都留了 `// TODO: GET /api/...` 注释。

## 4. 页面清单（原型 → Vue → 路由 → 后端 API）

| 页面 | Vue 文件 | 路由 | 主要 API |
|------|----------|------|----------|
| 知识库总览（首页） | `KnowledgeBasesView.vue` | `/` | `GET /api/domains` · `GET /api/jobs?limit=` |
| 知识库工作台 | `KnowledgeBaseView.vue` | `/kb/:id` | `GET /api/domains/{d}` · `…/topic-concepts` · 概念时间线（聚合） |
| 所有来源（跨库内容） | `ContentListView.vue` | `/content` | `GET /api/jobs?status=&source=&domain=&limit=&offset=` |
| 内容详情（笔记/概念/流水线/元信息） | `ContentDetailView.vue` | `/content/:id` | `GET /api/jobs/{id}` · `…/notes` · `…/review` · `…/note-versions` · `…/artifacts` · `…/media` · WS `…/ws/jobs/{id}` |
| 集合列表 | `CollectionsView.vue` | `/collections` | `GET/POST/PUT/DELETE /api/collections` |
| 集合详情 | `CollectionDetailView.vue` | `/collections/:id` | `GET …/{id}` · `…/jobs` · `POST …/sync` · `PUT …{sync_enabled}` |
| 搜索 | `SearchView.vue` | `/search` | `GET /api/search?q=&domain=&content_type=` |
| 概念库 | `GlossaryView.vue` | `/glossary` | `GET /api/glossary` · `…/accept` · CRUD |
| 系统 / Worker | `SystemView.vue` | `/system` | `GET /api/workers` · `…/registration-token` |
| 设置 | `SettingsView.vue` | `/settings` | `GET /api/auth/status` · `/api/bili/*` · `/api/auth/youtube/cookies` |
| 关于 | `AboutView.vue` | `/about` | — |

> 知识库 Profile（角色/上下文/概念表/禁止事项）在「知识库工作台」头部的「知识库设定」入口（`PUT /api/profiles/{d}`），不在设置页。

## 5. 组件清单

**base/**（薄封装，emit mnemo.css 类）
- `BaseButton`（`variant: pri|default|danger|ghost`，`size?: sm`）· `BaseBadge`（`variant → b-ok/b-info/b-run/b-warn/b-bad/b-mut/b-brand`）· `StatusBadge`（Job 状态枚举 → 中文 + 色）· `BaseCard` · `Chip` · `Modal`（`v-model` 控制显隐，overlay+modal）· `EmptyState`

**layout/**
- `AppShell`（`.app` 网格 = 侧栏 + 主区）· `AppSidebar`（三级折叠树 + 底部 系统/设置/折叠）· `TopBar`（面包屑 + 可展开搜索）

## 6. 状态枚举 → 视觉（实现照此，详见 DESIGN-SPEC §3 + 原型）

- **Job**：pending 灰 / downloading 蓝 / processing 蓝+进度条 / done 绿 / failed 红
- **Step**：waiting·ready 灰 / running 蓝转 / done 绿 / failed 红 / **skipped 虚线灰（绝不能像 failed）**
- **Worker**：online-idle 绿 / online-busy 蓝 / draining 黄 / offline 灰 / stale 红
- **Concept**：suggested 琥珀候选 / accepted 正式；`is_topic` 主题概念徽章；佐证强度 ★1–5
- **Collection**：订阅(rss/蓝) vs 手动(folder/灰)；`sync_enabled` 开关

## 7. 已完成 vs 待接手

**已搭好**：工程骨架、设计系统、7 个 base 组件、外壳/侧栏(三级折叠)/顶栏、11 个 view 的结构与示例数据、路由。

**待接手做（TODO）**：
1. 接后端 API，替换所有示例数据（按第 4 节对接点）。
2. WebSocket 实时进度（内容详情「流水线」tab + 全局计数）。
3. Markdown 渲染：markdown-it + `assets/` 图片改写、`[mm:ss]` 时间戳、命中概念包 `.term-link`、`> OCR：` 折叠。
4. 媒体 range 流播放（产物里的视频/音频，`GET …/media?path=`，支持拖动）。
5. 各页三态（加载骨架 / 空 / 错误）接真实数据。
6. 表单提交（投递、集合 CRUD、概念采纳、Profile）。
7. 概念时间线接真实聚合数据（原型用 Chart.js 堆叠柱状图，点柱子下钻出处、粒度随范围 年/季/月）。

## 8. 关键交互（原型已实现，Vue 里照搬这套逻辑）

- 侧栏三级折叠：知识库 → 集合 → 内容（每一级独立展开/收起）。
- 内容详情 tab：笔记 / 概念 / 流水线 / 元信息；**完成态默认落「笔记」**，未完成默认「流水线」。
- 顶栏搜索：点击原地展开成横栏 + 下拉建议，**不直接跳转**；点「查看全部结果」才进搜索页。
- 面包屑：放得下显全，挤不下才中间截断（不要一上来就 `…`）。
- 所有来源筛选：按状态 / 按来源 / 按知识库 三组，组内多选、跨组取交集、每维度可单独清除。
- 概念沉淀闭环：笔记评审面板里「采纳」候选概念 → 进概念库 → 后续笔记正文自动把它包成 `.term-link`。

## 9. 其它

- 全中文 UI；单用户，无登录页 / 账户菜单（网关层 Basic Auth 兜底）。
- 改了 `mnemo.css` 想在原型里看效果，记得 bump `prototype.html` 里的 `mnemo.css?v=N`；开发期建议 DevTools → Network 勾「Disable cache」。
- Notion 风格基调：暖灰中性 + Notion 蓝点缀、小圆角、扁平阴影、激活态用中性灰。读笔记是最高频场景，阅读体验优先级最高。
