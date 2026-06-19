# Mnemo 重设计 · 设计规范（DESIGN-SPEC）

> 全站 UI 重设计的实现契约。配合 `prototype.html`（可交互高保真原型）与 `mnemo.css`（设计系统底座）使用。
> 面向并行 Claude Code 开发：每个会话认领一组页面，照「本文档 + 原型对应 section + 后端 API」适配实现。

## 0. 文件清单与用法

| 文件 | 内容 | 怎么用 |
|------|------|--------|
| `design/prototype.html` | 13 页 + 5 弹窗的可交互原型，浏览器打开，底部黑色切换条跳页 | 视觉/布局/交互的「所见即所得」基准 |
| `design/mnemo.css` | 设计 token（CSS 变量）+ 全部组件类 | 直接转成 `tailwind.config` + 基础组件；类名即组件清单 |
| `design/DESIGN-SPEC.md` | 本文档 | IA/导航/组件/token/三态/交互流/状态映射/能力边界 |

**实现顺序建议**：先把第 2 节 token 写进 `tailwind.config.js` 与全局 CSS 变量 → 再实现第 4 节共享组件 → 再按第 6 节逐页适配（每页对照原型 section + 后端 API）。

---

## 1. 信息架构与导航

### 1.1 三轴心智模型（不变的根基）

```
              知识库 Domain —— 一切的锚（一等实体，命名空间隔离；可新建/改名/删，见决策 6）
   ┌───────────────────────┼───────────────────────┐
 语义层（学会了什么）      情景层（看了什么）        运维层（系统在干嘛）
 概念/主题 = 知识轴        集合/内容 = 归属轴         任务状态/Worker = 系统轴
```

导航必须让用户随时清楚「我在哪个知识库 / 看的是知识·内容·还是系统」。知识库切换是高频动作。

### 1.2 顶级目的地（唯一权威集合，桌面=移动同一套）

| 目的地 | 路由 | 桌面位置 | 移动位置 | 图标(lucide) |
|--------|------|----------|----------|--------------|
| 知识库 | `/` | 侧栏 | 底栏① | `layers` |
| 所有来源 | `/content` | 侧栏 | 底栏② | `inbox` |
| 投递 | （弹窗，全局） | 侧栏顶部主按钮 | 底栏③ 中间凸起 FAB | `plus` |
| 搜索 | `/search` | 顶栏 ⌘K omnibox | 底栏④ | `search` |
| 设置 | `/settings` | 侧栏 | 底栏⑤ | `settings` |
| 系统健康 | `/system` | 侧栏底/顶栏状态点 | 设置内 | `server` |

**集合、概念库、概念详情、主题、内容详情、集合详情** 不进顶级导航——它们从知识库工作台 / 内容 / 搜索逐级进入（见 1.3 决策）。

### 1.3 IA 决策（brief 第 3 节的最终方案 + 理由）

1. **导航统一一套，"全部内容"→"所有来源"**。桌面侧栏 = 移动底栏暴露同一组目的地；搜索桌面端做顶栏常驻 omnibox（`⌘K`），移动端做底栏项。「所有来源」页按 状态 / 来源 / 知识库 三组筛选，组内多选可取消、跨组取交集；列表无限滚动（替代「加载更多」），备选分页。*理由：消灭桌面/移动/文档三处不一致。*
2. **集合不进顶级**，归口在知识库工作台「内容」tab 顶部的集合筛选 chips + 「管理集合」入口；保留 `/collections` 全量 CRUD 页（从工作台进）。*理由：集合恒属单一知识库，提顶级会丢知识库上下文。*
3. **内容与任务合并为一个「内容详情」页**（路由 `/content/:id`），tab：`笔记 / 处理过程 / 信息`。**完成态默认落「笔记」**；未完成（处理中/失败）默认落「处理过程」。*理由：读笔记最高频，处理细节退居次要；一个 Job 既是内容又是任务，合并消除割裂。*
4. **Worker 监控降级**为顶栏/侧栏一个系统健康状态点（🟢正常/🟡降级/🔴故障）→ 点开进 `/system`（Worker 详情 + 接入引导）；设置「运维」也留入口。*理由：单用户场景运维最低频，不该占主导航。*
5. **移动底栏 5 项**：知识库 · 所有来源 · 投递(凸起 FAB) · 搜索 · 设置。概念/集合在知识库内进，与桌面一致。*理由：投递高频保留凸起；其余与桌面同集合。*
6. **知识库升级为一等实体**：支持「+ 新建知识库」（建空知识库 + 初始 Profile）、重命名、删除。⚠️ **需后端新增**：知识库成为正式表 + 增删改接口（见 §9）。*理由：用户希望能显式新建/管理知识库，而非只能靠投递派生。*
7. **知识库 Profile 移入知识库**：Profile 是知识库级设定，从「设置」挪到「知识库工作台」头部的「知识库设定」入口（内含 改名 / 删除 / 角色 / 上下文 / 术语表 / 禁止事项）。设置只保留全局项（平台登录、运维）。*理由：知识库级的东西在知识库里编辑更顺手。*

### 1.4 命名统一（全站强制）

| 旧 | 新 | 说明 |
|----|----|------|
| 术语 / 术语库 | **概念 / 概念库** | 与「概念」彻底统一为一个词 |
| 任务 / Job / 全部内容 | **内容 / 所有来源** | "内容"指单条；跨域列表页叫"所有来源"。处理中用状态徽章，不再叫"任务" |
| 情景层 / 语义层（页面黑话） | **内容 / 概念**（tab 名） | 心智模型保留，但 UI 用大白话 |
| 概念主题 | （并入概念列表） | 不单列，用 `主题概念` 徽章在概念列表里高亮 |

### 1.5 导航组件规范

- **桌面侧栏（≥768px）· 可折叠**：品牌(点 logo 回首页) →「投递内容」主按钮 → 导航：**知识库**(其下直接列出各知识库，彩色小标 + 名称，可一键切换；末尾「新建知识库」) / **所有来源** → 底部工具排：**系统状态图标**(带健康点) + **设置图标** + **折叠键**，三者并排、始终可见。折叠后整栏收成 64px 图标栏，折叠键变 `»` 一键展开，图标 hover 出 tooltip；折叠态建议持久化(localStorage)。
- **顶栏（自适应）**：左 breadcrumb；右 ⌘K 搜索 omnibox + 系统健康点。**空间不足时的降级顺序**：① 搜索 omnibox 先收成放大镜图标（点击进搜索页）；② breadcrumb 再中间截断为 `知识库 / … / 当前页`，末段 ellipsis。**面包屑每一级可点**，作为全站统一的「回上一层」入口；子页另保留显式返回按钮。（原型 `renderCrumb()` + CSS `seg-mid/seg-ell/seg-last` 即实现样例。）
- **移动顶栏**：当前页标题 + 右上下文动作（如返回/更多）。
- **移动底栏**：5 项如 1.2；③ 为中间凸起 FAB（投递）。
- **高亮逻辑**：`知识库` 在 `/`、`/domains/*`(含 terms/topics)、`/glossary` 下高亮；`所有来源` 在 `/content`、`/content/:id`、`/collections/*`、`/search` 下高亮；底部 `系统` 图标在 `/system`、`/workers/:id` 下高亮；`设置` 图标在 `/settings`、`/about` 下高亮。（原型 JS 的 `NAVOF` 映射即此逻辑的实现样例。）

### 1.6 本轮交互细化（实现要点）

- **侧栏知识库列表**：「知识库」项下直接列出各知识库（彩色小标 + 名称）一键切换，末尾「新建知识库」；折叠态隐藏该列表。
- **侧栏底部工具排**：系统状态图标(带健康点) · 设置图标 · 折叠键，三者始终可见；折叠键 `«`↔`»`，明确解决「折叠后如何展开」。
- **所有来源筛选**：三组 `按状态 / 按来源 / 按知识库`，组内 chip 多选可取消、跨组取交集，配「清除筛选」；列表无限滚动（滚动到底自动加载），备选分页。
- **面包屑回上层**：每级可点导航，全站统一「回上一层」；子页另留显式返回按钮。
- **新建 / 编辑知识库选图标**：弹窗内置图标库选择器（lucide 图标网格）+ 配色；知识库为一等实体（见决策 6），名称 / 图标 / 颜色 / Profile 一处配齐。
- **Worker 详情页**：系统页点 Worker 卡进入，展示信息 + 该机处理过的任务历史。

---

## 2. 设计 Token

全部已在 `mnemo.css` `:root` 落地为 CSS 变量。下表为权威值，迁移到 `tailwind.config.js → theme.extend` 即可。

### 2.1 颜色

| 组 | Token | 值 | 用途 |
|----|-------|----|----|
| 品牌 | `brand-50/100/200/300/500/600/700` | `#eef2ff … #4f46e5 … #4338ca` | 靛蓝主色；600=主按钮/激活，50=浅底，700=hover |
| 中性 | `ink-900…300` | `#0f172a / 1e293b / 334155 / 475569 / 64748b / 94a3b8 / cbd5e1 / e2e8f0` | 文本 900 标题→500 正文→400 弱化；用 slate |
| 线/面 | `line #e8ebf1` `line-soft #f1f4f8` `surface #fff` `bg #f7f8fc` `raised #fcfdff` | — | 边框/分隔/卡片/页底/微凸 |
| 语义 | `ok #16a34a` `info #2563eb` `run #4f46e5` `warn #d97706` `bad #dc2626` `mut #64748b` | 各带 `-bg`/`-bd` 浅底浅边 | 见第 3 节状态映射 |
| 强度 | `amber #f59e0b` | — | 概念佐证 ★ |
| 类型 | `t-video #2563eb` `t-paper #7c3aed` `t-article #059669` `t-audio #ea580c` | 各带 `-bg` | 内容类型图标块（装饰色，与语义色分离）|

### 2.2 字体 / 字号阶 / 形

- 字族：`Inter` + `PingFang SC / Microsoft YaHei` + system；等宽 `JetBrains Mono`（BV 号/步骤键名/时间戳/ID）。
- 字号阶：`21`(页标题 700) · `18`(次级标题/详情标题) · `15`(卡内标题 650) · `14`(正文/行标题 600) · `13.5`(正文) · `13/12.5`(次要) · `12`(元信息) · `11.5`(徽章/标签)。**最小 11px**。
- 字重：400/500/600/650/700。中文标题用 700，正文 400–500。
- 圆角：`sm 8px`(按钮/输入/chip) · `md 12px`(卡片/行/弹窗内块) · `lg 16px`(弹窗/手机框)。
- 阴影：`sm`(卡片静止) · `md`(hover/悬浮) · `lg`(弹窗)。克制，无炫光无渐变滥用（仅品牌 logo/评审面板用极淡渐变）。
- 间距：纵向节律 16/20/24px；组件内 6/8/12px。

---

## 3. 状态枚举 → 视觉映射（实现必须忠实于此）

所有状态来自后端权威枚举。徽章颜色/图标/圆点严格按此表，跨页一致。

### 3.1 Job（内容）`StatusBadge`
| 状态 | 中文 | 徽章类 | 附加 |
|------|------|--------|------|
| `pending` | 等待 | `badge b-mut` | — |
| `downloading` | 下载中 | `badge b-info` | 进度条 + 当前步骤 |
| `processing` | 处理中 | `badge b-run` | 进度条 `progress_pct` + `current_step` 标签 + WS 实时 |
| `done` | 已完成 | `badge b-ok` | — |
| `failed` | 失败 | `badge b-bad` | 错误信息 + 重试按钮 |

### 3.2 Step（步骤）`tl-ic`（时间线圆圈）
| 状态 | 圆圈类 | 图标 | 备注 |
|------|--------|------|------|
| `waiting` | `tl-ic waiting` | 编号 | 未开始 |
| `ready` | `tl-ic waiting` | 编号 | 就绪 |
| `running` | `tl-ic running` | `loader`(转) | 品牌色 |
| `done` | `tl-ic done` | `check` | 绿 |
| `failed` | `tl-ic failed` | `x` | 红 + 错误框 |
| `skipped` | `tl-ic skipped` | `minus` | **虚线灰边，绝不能像 failed**；跳过是正常（如已有字幕跳过转写） |

### 3.3 Worker `dot` + `badge`
| 状态 | 圆点 | 徽章 | 判定 |
|------|------|------|------|
| `online-idle` | `dot d-ok` | `b-ok 在线空闲` | 心跳 ≤30s 且空闲 |
| `online-busy` | `dot d-info` | `b-info 在线忙碌` | 心跳新鲜 + 有任务（显示当前步骤+job）|
| `draining` | `dot d-warn` | `b-warn 排空中` | 管理员叠加位，仅在线时 |
| `offline` | `dot d-mut` | `b-mut 离线` | 心跳 >30s |
| `stale` | `dot d-bad` | `b-bad 失联` | 心跳 >900s 或从无心跳，GC 信号 |

### 3.4 Concept（概念）/ Collection / 类型
- 概念状态：`suggested` → `badge b-warn 待确认`（amber）；`accepted` → 正式（列表不必标，或 `b-ok`）。
- `is_topic` → `badge b-brand 主题概念` + `bookmark`(pin) 图标；`definition_locked` → `lock` 小图标。
- 佐证强度 ★：`stars` 1–5（`.f` 实心 amber），由来源广度×类型多样性派生，是概念排序键。
- Collection：手动 → `folder`(灰块)；订阅 → `rss`(蓝块) + `badge b-info 订阅`；`sync_enabled` → `switch` 开关；显示 `last_synced_at`。
- content_type：`type-pill t-video|t-paper|t-article|t-audio` + 图标 `play|file-text|newspaper|headphones`。

---

## 4. 组件规范

下表组件均已在 `mnemo.css` 实现并在原型中使用。Vue 实现时一一对应封装。

| 组件 | 类 | 用途 | 变体 / 状态 |
|------|----|----|------|
| 状态徽章 StatusBadge | `badge b-ok/b-info/b-run/b-warn/b-bad/b-mut/b-brand` | 映射所有枚举（第 3 节） | 6 语义 + brand |
| 卡片 Card | `card` / `card.pad` / `card-h`(卡内标题) | 容器 | 默认 |
| 指标卡 Metric | `metric`(.v 数 / .l 标签) | 概览数字 | 2–4 列网格 |
| 按钮 Button | `btn` / `btn.pri` / `btn.danger` / `btn.pink` / `btn.sm` | 操作 | 主/次/危险/B站粉/小号 |
| 文字/图标按钮 | `ghost` / `iconbtn` | 弱操作 | — |
| 投递主按钮 | `btn-submit` | 侧栏常驻投递 | — |
| 标签芯片 Chip | `chip` / `chip.on` (`.n` 计数) | 筛选/主题标签 | 选中态 |
| 内容行 Row（=JobCard） | `row`(.title/.meta/.body) + `type-pill` + `bar` | 内容列表项 | **点击规则：done→内容详情默认笔记 tab；否则→处理过程 tab** |
| 概念行 | `concept`(.t/.d) + `stars` + `pin` | 概念列表项 | 主题概念高亮 |
| 域卡 | `dcard` | 知识库网格 | hover 抬升 |
| Tab / 分段 | `tabs` / `seg` | 页内/二级切换 | on 态 |
| 步骤时间线 | `timeline` + `tl-step` + `tl-ic` | 处理过程工作台左栏 | 6 步骤状态 |
| 产物 | `art-grid`/`art-thumb` + `media-box`(+`.ctrl`) | 步骤产物；**媒体走 range 流，必须可播放/拖动** | 图片/媒体/文本 |
| 评审 8 维 | `review` + `dims`/`dim-g`(.track) | 质量评审面板 | 8 维条形 + 总分 |
| 键值表 | `kv` | 元信息/详情 | — |
| 出现处 | `occ` | 概念出现位置 | hover |
| 开关 | `switch` / `switch.on` | 自动同步等 | 开/关 |
| 标签编辑 | `taglist` + `tag`(可删) | 术语表/do_not/tags | — |
| 表单 | `field`/`input`/`textarea.input`/`row2`/`drop` | 输入 | focus 环 |
| 弹窗 Modal | `overlay`+`modal`(.hd/.bd/.ft) / `modal.wide` | 5 类弹窗 | 点遮罩关 |
| 确认框 ConfirmDialog | `overlay.confirm` + `danger-ic` | 危险操作 | danger 红 |
| 三态 | `state`(+`.big` 图标) / `spinner` / `skel` | 空/加载/错误 | 见第 5 节 |
| 提示条 Callout | `callout.warn` / `callout.info` | 待确认概念等 | 警示/信息 |
| Toast | `toast.ok/bad/info` | 操作反馈 | 右上 3s 自动消失 |
| 进度条 | `bar` / `bar.lg` | 进度 | — |
| 系统健康点 | `dot d-ok/d-warn/d-bad` | 顶栏/侧栏 | 绿/黄/红 |

**5 类弹窗**：投递(`m-submit`) · 确认框(`m-confirm`) · 集合编辑(`m-collection`) · 概念新增/编辑(`m-concept`) · Profile 编辑器(`m-profile`)。移动端「更多」抽屉已被 5 项底栏取代，不再需要。

---

## 5. 三态规范（每个列表/详情页都要有）

| 态 | 呈现 | 类 |
|----|------|----|
| 加载 | 列表用骨架卡（`skel`，仿真实结构 3 条）；详情用居中 `spinner` + 文案 | `state` / `skel` / `spinner` |
| 空 | 居中淡图标(`state .big`，stroke 细) + 一句文案 + 必要时主行动按钮（如「投递一条」） | `state` |
| 错误 | 红字文案 + 「重试」按钮（重新拉取） | `state` + `btn` |

文案中文、具体（如「该知识库还没有内容」「集合不存在或已删除」「请至少输入 3 个字符」）。原型每页已示范。

---

## 6. 页面清单（路由 → 原型 section → 后端 API）

| # | 页面 | 路由 | 原型 id | 默认态 | 主要后端 API |
|---|------|------|---------|--------|--------------|
| 1 | 知识库总览（首页） | `/` | `home` | 知识库网格 | `GET /api/domains` · `GET /api/jobs?limit=8` |
| 2 | 知识库工作台 | `/domains/:d` | `domain` | 「内容」tab；头部「知识库设定」= Profile + 改名 + 删除 | `GET /api/domains/{d}` · `…/topic-concepts` · `PUT /api/profiles/{d}` · ⚠️`PUT/DELETE /api/domains/{d}` |
| 3 | 概念详情 | `/domains/:d/concepts/:term` | `term` | — | `GET /api/domains/{d}/terms/{term}` · `POST …/topic` |
| 4 | 主题页 | `/domains/:d/topics/:t` | `topic` | — | `GET /api/domains/{d}/topics/{t}` |
| 5 | 所有来源 | `/content` | `content` | 来源/类型/状态筛选 | `GET /api/jobs?status=&source=&content_type=&limit=&offset=` |
| 6 | 内容详情（合并） | `/content/:id` | `detail` | done→笔记 / 处理中→处理过程 | `GET /api/jobs/{id}` · `…/notes` · `…/review` · `…/artifacts` · `…/media` · WS `…/ws/jobs/{id}` |
| 7 | 集合列表 | `/collections` | `collections` | 列表 | `GET/POST/PUT/DELETE /api/collections` |
| 8 | 集合详情 | `/collections/:id` | `collection` | — | `GET …/{id}` · `…/jobs` · `POST …/sync` |
| 9 | 搜索 | `/search` | `search` | 未搜索引导 | `GET /api/search?q=&domain=&content_type=` |
| 10 | 概念库 | `/glossary` | `glossary` | 待审 + 已采纳 | `GET /api/glossary` · `…/accept` · CRUD |
| 11 | 系统/Worker | `/system` | `system` | 概览 + 列表（卡片可点进详情） | `GET /api/workers` · `…/registration-token` |
| 11b | Worker 详情 | `/workers/:id` | `wdetail` | 信息 + 该机任务历史 | `GET /api/workers/{id}` · `…/jobs` |
| 12 | 设置 | `/settings` | `settings` | 平台认证 + 运维 + 关于（Profile 已移入知识库工作台） | `GET /api/auth/status` · `/api/bili/*` |
| 12b | 关于 | `/about` | `about` | 项目介绍 + 使用说明 | — |
| 13 | 手机预览 | （原型演示） | `mobile` | — | — |

> 每页的全部按钮/操作/数据形态以 brief 第 4 节为权威；原型 section 给视觉与交互；本表给路由与 API 对接点。

---

## 7. 关键交互流

1. **添加内容**：任意页点「投递」→ 弹窗(`m-submit`) 粘贴 URL（自动识别来源/类型徽章）或拖文件 + 选知识库/集合/风格标签 → 提交 → 跳「内容详情/处理过程」tab → WS 实时看步骤推进。
2. **看处理**：内容详情 →「处理过程」tab → 左步骤时间线（点步骤选中）→ 右步骤详情(kv) + 产物(图片缩略/媒体可拖动播放/文本)；可「从步骤重跑」；失败步骤红框 + 重试。
3. **读笔记**（最高频）：完成态内容默认落「笔记」tab → 智能/机械分段切换 → 版本 chips 切换（各带 ★）→ 评审面板看 8 维 + 改进 → 读正文（`term-link` 蓝虚线词点进概念 / `ts` 灰等宽时间戳 / OCR 折叠块 / 章节 TOC）→「换 provider 重跑」生成新版本（轮询）。
4. **采纳概念（沉淀闭环）**：笔记评审面板或概念库里把候选 `accept` → 它进入知识库工作台「概念」、获得佐证 ★ → 后续笔记 Prompt 用其定义、正文自动把它包成 `term-link`。这是 Mnemo 的灵魂，UI 要让它顺手可见。
5. **管理订阅**：集合列表「新建」→ 选「订阅 B站 UP 主」+ 填 mid + 真实知识库（非 general）→ 创建 → 集合详情「立即同步」拉新内容 → 看追更状态徽章 → `switch` 开关自动同步。
6. **看 Worker**：系统健康点 → `/system` → 指标条（在线/忙碌/今日完成）→ Worker 卡（状态灯/算力/吞吐/心跳/当前步骤，排空/备注/移除）→「接入新 Worker」生成 token + 复制 docker 命令。

---

## 8. 实现提示

- **技术栈**：Vue 3 + TS + Tailwind + `lucide-vue-next` + markdown-it。图标统一从 lucide 选。
- **响应式**：`md`(768) 切侧栏/底栏；`lg`(1024) 笔记正文 + 右侧 TOC 分栏（手机 TOC 收进可展开条）。原型 `@media(max-width:900px)` 已示范网格降列。
- **实时**：WebSocket 推送任务进度 + 全局计数(2s)。处理中页显示「实时更新中 / 连接断开重连中」连接指示。
- **Markdown 渲染规则**（笔记正文，务必实现）：`assets/xxx.jpg` → `GET /api/jobs/{id}/assets/xxx.jpg`；`[mm:ss]` → 灰等宽时间戳（视频版可点跳转）；命中已采纳概念**首次出现**包成 `.term-link` → 概念详情；`> OCR：…` 折叠为 `<details>`。
- **媒体**：步骤产物里视频/音频走 `GET /api/jobs/{id}/media?path=`（range 流式），必须支持拖动播放。
- 工程细节：`@` 别名未配，import 走相对路径（设计无需关心）。

## 9. 后端能力边界（设计触及处已标注）

- **已支持**：知识库/内容/集合/概念/Profile/Worker/搜索 的读写、笔记多版本与评审、产物与媒体流、B站扫码与 cookie、WS 实时。详见 brief 第 7 节清单。
- **需后端新增**：**① 知识库升级为一等实体（本次设计依赖）**——知识库从派生视图变成正式表 + `POST/PUT/DELETE /api/domains`（新建空知识库 / 重命名并级联更新归属 / 删除），且 `GET /api/domains` 需能返回尚无内容的空知识库。② 知识图谱可视化（现仅列表，无图数据接口）。③ 笔记/视频标注 annotation（有表无路由）。④ AI 用量/成本看板（有聚合无前端路由）。
- **故意不做**：跨域概念合并/去重（知识库隔离是特性）。

---

*本规范 + `prototype.html` + `mnemo.css` 三件套自包含，可据此并行开发全站。读笔记是最高频场景，阅读体验与「概念沉淀闭环」的顺手度是验收重点。*
