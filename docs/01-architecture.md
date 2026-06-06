# 01 · 系统架构

> 系统全景图。读完这个文档，你就知道系统由哪些组件构成、怎么通信、怎么部署。

## 1. 一句话

主机跑全部核心服务，Cloudflare Tunnel 做公网入口，中转服务器（可选）给 GPU 传话。

## 2. 组件分层

```mermaid
graph TD
    subgraph presentation["展示层 Presentation"]
        vue["Vue 3 SPA<br/>(手机/电脑浏览器)"]
        tunnel_p["Cloudflare Tunnel"]
        vue --- tunnel_p --- api_entry["API 入口"]
    end

    subgraph service["服务层 Service"]
        api["API (FastAPI)<br/>任务管理 · 文件服务"]
        scheduler["调度器<br/>DAG 推进 · 资源池管理"]
        ws["WebSocket<br/>进度推送"]
        redis["Redis<br/>任务队列 · 事件流 · Worker 心跳"]
        api --- redis
        scheduler --- redis
        ws --- redis
    end

    subgraph execution["执行层 Execution"]
        dl["Download Worker<br/>获取原始内容"]
        cpu["CPU Worker<br/>视频:帧/OCR<br/>论文:PDF解析"]
        ai["AI Worker<br/>摘要/笔记/评审"]
        gpu["GPU Worker ⚡<br/>Whisper/OCR<br/>（可选，通过中转服务器）"]
    end

    subgraph storage["存储层 Storage"]
        fs["文件系统<br/>/data/jobs/<br/>原始内容+产物"]
        sqlite["SQLite<br/>元数据 · 任务/步骤"]
        fts["SQLite FTS5<br/>全文搜索 (M2)"]
    end

    presentation -->|HTTPS| service
    service --> execution
    execution --> storage

    style presentation fill:#e0f2fe,stroke:#0284c7
    style service fill:#fef3c7,stroke:#d97706
    style execution fill:#dcfce7,stroke:#16a34a
    style storage fill:#f3e8ff,stroke:#9333ea
```

## 3. 部署拓扑

两种推荐架构，应用逻辑完全相同，区别只在 StorageBackend 和网络配置。**先在单机完成全部开发和测试，再拆分到多机**。

### 架构一：All-in-One（推荐起步）

一台机器，一个 docker-compose，全部服务。自托管服务器、PC、云服务器均可。

```mermaid
graph TD
    client["📱 手机/电脑"]
    client -->|"HTTPS（公网）或 HTTP（局域网）"| host

    subgraph host["主机 (Docker Compose)"]
        cloudflared["cloudflared — 可选，公网入口"]
        redis_s["redis — 任务队列"]
        api_s["api — FastAPI"]
        scheduler_s["scheduler — 调度器"]
        dl_s["worker-download — 下载 ×1"]
        cpu_s["worker-cpu — CPU步骤 ×1"]
        ai_s["worker-ai — AI ×2"]
        data["/data/jobs/ — 视频+产物"]
        db["/db/analyzer.db — SQLite"]
    end

    style host fill:#f0fdf4,stroke:#16a34a
```

局域网直接访问 `http://主机IP:3000`，公网加 Cloudflare Tunnel。

**开发和测试全部在此模式完成**。所有 Worker 用 `LocalStorage`（直接读写 /data/jobs/），无 MinIO。

### 架构二：分层部署（核心 + 中转 + Worker 集群）

有独立 GPU 机器或多台 Worker 机器时，拆为三层。每层一个 docker-compose：

```mermaid
graph TD
    client2["📱 手机/电脑"]
    cf["Cloudflare Tunnel"]

    client2 -->|HTTPS| cf
    cf --> host2

    subgraph host2["主机（核心）"]
        h_cf["cloudflared"]
        h_api["API + 调度器"]
        h_ai["Worker-ai ×2"]
        h_dl["Worker-download ×1"]
        h_cpu["Worker-cpu ×1（兜底）"]
        h_data["/data/jobs/（持久）"]
    end

    subgraph relay["中转服务器（公网）"]
        r_redis["Redis (TLS)"]
        r_minio["MinIO (HTTPS)"]
        r_note["无持久数据 · 可随时重建"]
    end

    subgraph gpu["GPU 机器"]
        g_worker["Worker-gpu ×1"]
        g_task["Whisper / 场景 / OCR"]
        g_poll["轮询中转 Redis"]
        g_file["从 MinIO 下载/上传"]
        g_idle["空闲自动退出"]
    end

    host2 -->|"文件中转"| relay
    relay -->|"出站轮询"| gpu

    style host2 fill:#dbeafe,stroke:#2563eb
    style relay fill:#fef9c3,stroke:#ca8a04
    style gpu fill:#dcfce7,stroke:#16a34a
```

远程 Worker 用 `RemoteStorage`（MinIO pull/push），核心服务器用 `LocalStorage`。**应用代码完全不变**——只改环境变量（`MINIO_URL`）。

**三条独立通信线路**：

| 线路 | 路径 | 用途 | 中转被攻破影响 |
|------|------|------|---------------|
| 用户访问 | 用户 → Cloudflare → 核心 | Web UI + API | **无影响** |
| Worker 中转 | 核心 ↔ 中转 ↔ Worker | 任务分发 + 文件中转 | Worker 断开，核心 CPU 兜底 |
| 核心本地 | 容器间 localhost | 内部通信 | **无影响** |

> 如果核心和 Worker 都有公网 IP，不需要中转——Worker 直连核心 Redis。

### 从 All-in-One 到分层：零代码改动

| 改什么 | All-in-One | 分层 |
|--------|-----------|------|
| Worker 代码 | 不变 | 不变 |
| StorageBackend | LocalStorage | RemoteStorage（加 `MINIO_URL` 环境变量） |
| Redis 连接 | `redis://localhost` | `rediss://:pass@中转IP:6380` |
| docker-compose | 一个文件 | 每台机器一个文件 |

**多机额外测试项**（单机测完后仅需验证）：

| 测试项 | 内容 |
|--------|------|
| MinIO 文件传输 | 大文件（1GB+视频）上传下载完整性 |
| Redis TLS 连接 | Worker 通过公网连接 Redis |
| 网络延迟下的心跳 | Worker 心跳在高延迟下不误判为离线 |

这些是基础设施测试，不涉及应用逻辑。10 分钟跑通即可。

## 4. 数据流

### 通用处理模型

所有内容类型共享同一个处理框架，只是步骤链（pipeline）不同：

```mermaid
graph TD
    submit["用户投递 URL / 文件"]
    submit --> identify["API 识别 content_type"]
    identify --> create["创建 Job"]
    create --> load["加载对应 pipeline"]
    load --> enqueue["Redis 就绪队列"]
    enqueue --> download["Download"]
    download --> steps["内容特定步骤链"]
    steps --> ai_note["AI 笔记生成"]
    ai_note --> review["质量评审"]
    review --> done["Job 完成"]
    done --> notify["WebSocket 通知"]
    notify --> readable["笔记可读"]
```

### 各内容类型的步骤链

调度器从 `pipelines.yaml` 加载对应 content_type 的步骤 DAG：

**视频 (video)** — M1 实现：
```mermaid
graph LR
    dl["00_download"]

    dl --> scene["01_scene"]
    scene --> frames["02_frames"]
    frames --> dedup["03_dedup"]
    dedup --> ocr["04_ocr"]

    dl --> danmaku["05_danmaku"]
    dl --> punctuate["06_punctuate"]
    dl --> whisper["00b_whisper<br/>(条件)"]
    whisper --> punctuate

    ocr --> mechanical["07_mechanical"]
    danmaku --> mechanical
    punctuate --> mechanical

    mechanical --> smart["08_smart"]
    smart --> review_v["09_review"]

    style whisper fill:#fff3cd,stroke:#d97706
```

**论文 (paper)** — M1 实现：
```mermaid
graph LR
    dl_p["00_download"] --> parse["10_pdf_parse"]
    parse --> sections["11_sections"]
    parse --> figures["12_figures"]
    sections --> smart_p["14_smart_paper"]
    figures --> smart_p
    smart_p --> review_p["15_review"]
```

**文章 (article)** — M5 实现：
```mermaid
graph LR
    dl_a["00_download"] --> extract["20_extract"] --> smart_a["21_smart_article"] --> review_a["22_review"]
```

每种类型共享 `00_download`（下载/获取原始内容）和 `*_smart` / `*_review`（AI 笔记 + 评审）的模式，中间的处理步骤按内容类型各异。

### 视频步骤 DAG 详解

无依赖的步骤并行执行。07_mechanical 等待 04_ocr + 05_danmaku + 06_punctuate 三路汇合。

## 5. 资源池模型

步骤映射到资源池，资源池限制并发。池与内容类型无关——不同类型的步骤可以共享同一个池：

| 池名 | 并发上限 | 说明 | 示例步骤 |
|------|---------|------|---------|
| io | 不限 | 轻量 IO | 00_download, 05_danmaku, 07_mechanical, 20_extract |
| scene | 1 | CPU 全占，与 cpu 池互斥 | 01_scene |
| cpu | 3 | 中等 CPU | 02_frames, 03_dedup, 04_ocr, 10_pdf_parse |
| ai | 2 | LLM 并发（按 Provider 各自限速） | 06_punctuate, 08_smart, 09_review, 14_smart_paper |
| gpu | 1 | GPU 独占 | 00b_whisper |

**互斥规则**：scene 运行时冻结 cpu 池（场景检测吃满全部核心）。

**优先级**：已完成步骤越多的 Job 优先调度（减少在制品，用户更快看到第一批结果）。

## 6. 依赖规则

```mermaid
graph LR
    P["展示层"] -->|"前端只调 API"| S["服务层"]
    S -->|"API/调度器通过 Redis 通信"| E["执行层"]
    E -->|"Worker 读写 /data/jobs/"| ST["存储层"]

    P -.-x|"❌ 禁止"| ST
    E -.-x|"❌ 禁止"| S
    E -.-x|"❌ Worker 间禁止直连"| E

    style P fill:#e0f2fe,stroke:#0284c7
    style S fill:#fef3c7,stroke:#d97706
    style E fill:#dcfce7,stroke:#16a34a
    style ST fill:#f3e8ff,stroke:#9333ea
```

**允许的依赖方向**：展示层 → 服务层 → 执行层 → 存储层

**禁止的依赖**：
- 展示层 → 存储层（前端不能直读文件/DB）
- 执行层 → 服务层（Worker 不调 API，通过 Redis 事件通信）
- Worker 之间直接通信（通过文件和 Redis 解耦）

## 7. 关键不变量

**数据与部署**：
1. **主机是唯一持久节点**：所有数据在主机。中转服务器/GPU 丢了不丢数据（MinIO 只做临时中转）。
2. **零公网端口**：主机和 GPU 都不开入站端口（Cloudflare Tunnel 出站建立）。
3. **容器隔离**：宿主机不装任何依赖，全部在 Docker 内运行。

**步骤与执行**：  

4. **文件是接口**：步骤间通过 JSON/MD 文件通信，不共享内存。Worker 通过 StorageBackend（pull/push）访问文件，不直接依赖本地文件系统。
5. **幂等执行**：每步 hash 实际输入文件内容 + 配置 + prompt，没变就跳过。上游重跑导致输出变化时，下游自动级联重跑。DB 写入通过 exec_id 去重，防止重复计费。
6. **故障隔离**：单任务失败不影响其他任务，单 Worker 挂掉不影响调度器（孤儿步骤自动回收）。

**Worker 与调度**：  

7. **Worker 无状态**：任何 Worker 可跑在任何机器。通过 StorageBackend 拉取输入、推送产物，不依赖本地数据。加减 Worker 不需要改调度器。
8. **Tag 亲和性**：步骤声明需求标签，Worker 声明能力标签和排斥标签，调度自动匹配。
9. **AI Provider 解耦**：步骤调用 `call_ai()`，不关心底层用哪个 Provider/Model。路由由 AI Gateway 根据配置决定。

**配置驱动**：
10. **配置与代码分离**：新增内容类型 = 加 pipeline YAML + 步骤脚本，不改框架。领域 Profile、风格标签、Provider 配置全在 YAML 文件里。

## 8. 技术选型总览

| 组件 | 选型 | ADR |
|------|------|-----|
| 语言 | Python 3.11+ | [ADR-0001](adr/0001-language-python.md) |
| 队列 | Redis (Sorted Set + Pub/Sub) | [ADR-0002](adr/0002-queue-redis.md) |
| 存储 | 本地文件系统 (M4+ MinIO 中转) | [ADR-0003](adr/0003-storage-local-first.md) |
| LLM | 多 Provider AI 网关 | [ADR-0004](adr/0004-llm-multi-provider.md) |
| 前端 | Vue 3 + Vite + Tailwind | [ADR-0005](adr/0005-frontend-vue3.md) |
| 网关 | Cloudflare Tunnel | [ADR-0006](adr/0006-gateway-cloudflare-tunnel.md) |
| 远程 Worker | Redis 轮询 + MinIO 文件中转 | [ADR-0007](adr/0007-remote-worker-polling.md) |
| 搜索 | SQLite FTS5 | [ADR-0008](adr/0008-search-sqlite-fts5.md) |

## 9. M1 → M4 演进路径

| 阶段 | 部署 | 新增组件 | 测试 |
|------|------|---------|------|
| **M1** | All-in-One | 调度器 + Worker + StorageBackend + AI Gateway + Profile + 风格标签 + 视频 pipeline + 论文 pipeline + API + Worker 管理 + 前端 + Tunnel | 单步验证 + 并发安全 + DRY_RUN |
| **M2** | All-in-One | 集合管理 + FTS5 搜索 + Profile 动态积累 | 搜索质量 |
| **M3** | All-in-One | 视频回放 + 标注 + PDF 导出 | 前端交互 |
| **M4** | 分层部署 | 中转 Redis/MinIO + RemoteStorage + GPU Worker | 网络连通 + 文件传输 |

**开发路径**：M1-M3 全在 All-in-One 模式下开发和测试。M4 只加分层部署能力，应用代码不变——只验证 MinIO 传输和 Redis TLS 连接。

**M1 测试重点**：LLM 调用花真钱。必须在 `DRY_RUN=1` 下验证乐观锁、exec_id 去重、事件幂等后，再接真 Provider。详见 [09-testing.md §5](09-testing.md)。
