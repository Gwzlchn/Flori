# Mnemo 前端重建 · 统一规范（REBUILD-NOTES）

> 目标：把 `frontend/src` 的全部视图按 `design/prototype.html` 从零重建，套 `design/mnemo.css` 设计系统，对接后端契约。数据层 / 构建配置 / 外壳已就绪，保留。**动手前先读本文件。**

## 0. 先读这三样
- `design/prototype.html` —— 视觉与交互基准（浏览器可开，底部切页）。每个视图对应一个 `<section class="page" id="…">`，映射见 §6。
- `design/mnemo.css` —— 设计系统（已拷到 `frontend/src/assets/mnemo.css` 且全局引入）。**直接用它的类**。
- 契约以本文 §5 为准（已是 origin/main `af4b1ff` 现状；工作树里的 `docs/03-contracts.md` 偏旧，勿依赖）。

## 1. 硬约定
- 一律 `<script setup lang="ts">`；Vue 3 + vue-router + Pinia。
- 样式**只用 mnemo.css 的类**（`.page .h1 .lead .card .card.pad .card-h .btn .btn.pri .ghost .iconbtn .badge .chip .metric .grid2 .grid3 .seclabel .meta` 等），颜色用 CSS 变量（`var(--ink-700)` `var(--brand-600)` …）。**禁止 Tailwind utility 类**（旧视图里的 `bg-white/flex/px-4` 一律换掉）。
- 图标用 `lucide-vue-next`：`import { FileText } from 'lucide-vue-next'` → `<FileText :size="16" />`。尺寸跟随层级：标题 18 / 卡标 15 / 正文 16 / 元信息 13–14 / 徽章 12。
- 文案命名一律 **知识库 / 集合 / 内容 / 概念**；**不要**出现 领域 / 任务·job / 术语。后端路由仍是 `/api/domains` `/api/jobs`（**不改**），仅 UI 文案换词。
- **不要改**：`stores/*`、`types/*`、`composables/*`、`router/`、外壳（`AppShell/AppSidebar/TopBar`）、`main.ts`、本文件、`StatusBadge`。你只建/改自己负责的视图文件及其私有子组件。
- 数据优先用 store 方法；store 没有的端点用 `useApi()`：`const api = useApi(); await api.get<T>(url) / api.post(url,body) / api.put / api.del / api.getText(url)`。
- 每个列表/详情都做三态：加载（`.card` 里放“加载中…”或骨架）、空（居中提示 + 行动按钮）、错误（提示 + 重试）。**不要把演示数据硬编码进成品**，接真实 store/api（拿不到时显示空态）。
- 路由跳转用新路径：`/`、`/kb/:domain`、`/kb/:domain/concepts/:term`、`/kb/:domain/topics/:topic`、`/content`、`/content/:id`、`/collections`、`/collections/:id`、`/search`、`/glossary`、`/system`、`/system/workers/:id`、`/settings`、`/about`。

## 2. 复用清单（直接 import，别重造）
- `components/notes/MarkdownViewer.vue` —— 笔记 markdown 渲染（图片改写 `/api/jobs/{id}/assets/`、`[mm:ss]` 时间戳、命中概念 `.term-link`、`> OCR：` 折叠）。props：`content, jobId, terms?, domain?`；emit `headings`。
- `components/job/StepWorkbench.vue` —— 流水线步骤工作台（产物清单 / 日志）。
- `components/settings/BiliLogin.vue`、`components/auth/CookieUpload.vue`、`components/settings/ProfileEditor.vue` —— 设置页复用。
- `components/common/StatusBadge.vue` —— 状态徽章：`<StatusBadge :status="job.status" />`（见 §4，已含全部枚举）。
- Toast：`import { inject } from 'vue'; const showToast = inject<(m:string,t?:string)=>void>('showToast', ()=>{})`。
- 时间格式：`import { fmtDateTime } from '../utils/datetime'`。

## 3. Store 方法（`stores/*.ts`，均为 setup store，`const s = useXStore()`）
- **jobs** `useJobStore`：state `list,total,loading`；`fetchList({status?,limit,offset})`、`fetchDetail(id)`、`createJob(body)`、`uploadJob(fd)`、`retry(id)`、`rerun(id,fromStep)`、`del(id)`。直接端点：`api.getText('/api/jobs/{id}/notes/smart'|'/mechanical'|'/transcript')`、`api.get('/api/jobs/{id}/review')`、`/api/jobs/{id}/note-versions`、`/api/providers`、`POST /api/jobs/{id}/rerun-smart {provider}`、`/api/jobs/{id}/artifacts`、`/api/jobs/{id}/steps/{step}/log`。
- **collections** `useCollectionStore`：`collections,loading`；`fetchAll(domain?)`、`get(id)`、`create(body)`、`update(id,body)`、`remove(id)`、`fetchJobs(id,{limit,offset})`、`sync(id)`。
- **domains** `useDomainStore`：`domains,loading`；`fetchAll()`、`workspace(domain)`、`term(domain,t)`、`topic(domain,t)`、`topicConcepts(domain)`。
- **workers** `useWorkerStore`：`workers,loading`；`fetchAll()`、PUT 改 `{status:'draining'|'idle'}`/`{admin_note}`/`{tags}`、`remove(id)`、`mintToken()`、`fetchJobs(id)`。
- **global** `useGlobalStore`：`profiles,styleTags`；`fetchProfiles()`、`fetchStyleTags()`。

## 4. 状态 → 中文 + 色（用 `StatusBadge`，勿自造）
| 域 | 枚举 → 文案/色 |
|----|----------------|
| Job | pending 等待·灰 / downloading 下载中·蓝 / processing 处理中·蓝 / done 已完成·绿 / failed 失败·红 |
| Step | waiting·ready 等待/就绪·灰 / running 运行中·蓝 / done 完成·绿 / failed 失败·红 / **skipped 跳过·虚线灰（绝不像 failed）** |
| Worker | online-idle 空闲·绿 / online-busy 忙碌·蓝 / draining 排空中·黄 / offline 离线·灰 / stale 失联·红 |
| Concept | suggested 候选·琥珀 / accepted 已采纳·绿；`is_topic` 主题概念另加标记 |

内容类型色：video 蓝 / paper 紫 / article 绿 / audio 橙（mnemo.css 有 `--t-video` 等变量与 `.type-pill`）。

## 5. 契约速查（current · origin/main af4b1ff）
后端路由不变，Base `/api`。鉴权：Caddy 全站 Basic（浏览器自动带），app 不主动发 Authorization。

**Job**：`JobResponse {job_id, content_type(video|paper|article|audio), status(pending|downloading|processing|done|failed), progress_pct, title, source, domain, collection_id, created_at}`。`GET /api/jobs?status=&limit=&offset=` → `{total, items:[JobResponse]}`。`GET /api/jobs/{id}` → 加 `meta, steps:[{name,status,duration_sec,meta,error}]`。`POST /api/jobs`(body `{url,content_type,domain,style_tags,collection_id}`)、`/upload`(FormData)、`/{id}/retry`、`/{id}/rerun {from_step}`、`/{id}/rerun-smart {provider}`、`DELETE /{id}`。笔记：`GET /{id}/notes/smart|mechanical|transcript`(markdown)、`/{id}/review`(json，含 `key_terms`)、`/{id}/note-versions`。

**Collections**：`CollectionResponse {id,name,domain,description,tags[],job_count,created_at, subscription:{source_type,source_id,enabled,last_synced_at}|null}`。`GET /api/collections?domain=` → **裸数组**。`GET/{id}`、`POST`(`{name,domain,description?,tags?,source_type?,source_id?,sync_now?}`)、`PUT/{id}`(`{name?,description?,tags?,sync_enabled?}`)、`DELETE/{id}`(204)、`POST/{id}/sync` → `{total,new,skipped}`、`GET/{id}/jobs?limit=&offset=` → `{total,items}`。

**Domains（知识库）**：`GET /api/domains` → `{domains:[{domain,collection_count,job_count,concept_count,subscription_count,last_active_at}]}`（按 domain 升序，可能 last_active_at=null）。`GET /api/domains/{domain}`（工作台聚合）→ `{domain, stats:{…同上一行…}, collections:[{id,name,job_count,is_subscription,source_id,sync_enabled}], recent_jobs:[JobResponse 子集，最近12], top_concepts:[{term,definition,source_count,status,is_topic}]（Top30 按 source_count 降序）, topics:[{topic,count}], suggested_count}`。`GET /api/domains/{domain}/topic-concepts` → `[{term,definition,occurrence_count,related[],is_topic}]`。`GET /api/domains/{domain}/terms/{term}` → `GlossaryTermResponse`。`GET /api/domains/{domain}/topics/{topic}?limit=` → `{domain,topic,jobs:[JobResponse],total}`。

**Glossary（概念库）**：`GlossaryTermResponse {domain,term,definition,occurrences:[{job_id,content_type,location}],related[],status(suggested|accepted),is_topic,definition_locked,created_at}`。`GET /api/glossary?domain=&status=` → 数组。`POST /api/glossary?domain=`(body `{term,definition?,related?}` → accepted)、`GET/PUT/DELETE /api/glossary/{domain}/{term}`、`POST …/{term}/accept`、`POST …/{term}/topic {is_topic}`。

**Search**：`GET /api/search?q=&collection_id=&domain=&content_type=&limit=&offset=`（q≥3 字符）→ `{total, items:[{job_id,title,note_type(smart|mechanical|transcript),snippet(含 <mark>),content_type,domain,collection_id}]}`。

**Profiles（知识库设定）**：`GET /api/profiles` → `[{domain,role,terminology_count}]`。`GET /api/profiles/{domain}` → `{domain,role,domain_context,output_style,terminology[],do_not[]}`。`PUT /api/profiles/{domain}`(部分字段)、`POST …/terms {term}`、`DELETE …/terms/{term}`。

**Providers**：`GET /api/providers` → `{providers:[{… name, available …}]}`（rerun-smart 的 provider 须 available）。

**System**：`GET /api/status` → `{workers:{download/cpu/ai/gpu:{online,busy}}, pools:{…}, jobs:{total,done,processing,failed,pending}, disk:{used_gb,available_gb}}`。`GET /api/workers` → `{workers:[Worker]}`；`Worker {id,type,pools[],tags[],hostname,gpu_name?,status(online-idle|online-busy|draining|offline|stale),current_job,current_step,tasks_completed,tasks_failed,total_duration_sec,first_seen,last_heartbeat,admin_note}`。`GET /api/workers/{id}` 加 `recent_tasks:[{job_id,step,status,duration_sec,finished_at}]`。`PUT /api/workers/{id}`、`DELETE`、`POST /api/workers/registration-token` → `{token}`。

**平台认证（设置页）**：`GET /api/auth/status`、B站 `/api/bili/login/start|poll|status|logout`、`POST /api/auth/youtube/cookies`。

**WebSocket**：`/api/ws/jobs/{id}`（事件 step_ready/step_start/step_progress/step_done/step_failed/step_skipped/job_done/job_failed，由 `useJobWs(id)` 消费）；`/api/ws/global`（每2秒系统状态，`useGlobalWs`）。

## 6. 原型 section → 视图文件
| 原型 `#id` | 视图文件 | 路由 |
|-----------|----------|------|
| `#home` | `views/HomeView.vue`（知识库总览） | `/` |
| `#domain` | `views/DomainWorkspaceView.vue`（工作台·内容/概念/时间线 tab） | `/kb/:domain` |
| `#detail` | `views/JobDetailView.vue`（内容详情·笔记/概念/流水线/元信息 tab） | `/content/:id` |
| `#content` | `views/JobListView.vue`（所有来源·三组筛选） | `/content` |
| `#term` | `views/TermDetailView.vue`（概念详情） | `/kb/:domain/concepts/:term` |
| `#topic` | `views/TopicView.vue`（主题） | `/kb/:domain/topics/:topic` |
| `#glossary` | `views/GlossaryView.vue`（概念库） | `/glossary` |
| `#collections` | `views/CollectionsView.vue`（集合列表） | `/collections` |
| `#collection` | `views/CollectionDetailView.vue`（集合详情） | `/collections/:id` |
| `#search` | `views/SearchView.vue` | `/search` |
| `#system` | `views/WorkersView.vue`（系统/Worker） | `/system` |
| `#wdetail` | `views/WorkerDetailView.vue` | `/system/workers/:id` |
| `#settings` | `views/SettingsView.vue` | `/settings` |
| `#about` | `views/AboutView.vue` | `/about` |

工作台「时间线」tab 用 `components/ConceptTimeline.vue`（由集成方提供；import 占位即可，先放一个 `<ConceptTimeline :domain="domain" />`）。

## 7. 别做
后端 / docker / shared 别碰。数据层别重写。别用 Tailwind。别硬编码演示数据进成品视图。别新建 store/types。每个视图最终要能被 `vue-tsc` 通过（注意 import 路径、类型、未用变量）。
