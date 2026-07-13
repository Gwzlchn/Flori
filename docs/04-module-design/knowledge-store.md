# 知识存储

> 把每篇笔记的产出沉淀为「可搜索、可关联」的个人知识体系：领域中心 + 概念图 + 全文搜索 + 集合/订阅。
> 数据模型以 [02-domain-model](../02-domain-model.md) 为准，REST/Schema 以 [03-contracts](../03-contracts.md) 为准；本文只讲设计意图与各部件如何串起来。

## 概述

单篇笔记完成后，知识层在其上叠加三个维度：

- **领域（domain）**：派生视图，按领域把内容/集合/概念分桶，是知识库的入口（领域中心）。
- **概念图（glossary）**：从评审产物自动采集的术语/主题，带类型化来源（occurrences），跨内容关联。
- **全文搜索（FTS5）**：跨内容类型检索笔记、逐字稿、OCR、弹幕。

## 领域（派生视图，无 domains 表）

领域不是独立实体，而是 **jobs ∪ collections ∪ glossary 的 distinct `domain` ∪ 有 Profile 的领域** 这一并集的派生视图。Profile（`profiles/{domain}.yaml`）额外存展示元数据：`display_name / icon / color / description / role`，供领域中心卡片渲染。

领域工作台聚合该领域的内容、术语、主题、时间线。

```
POST /api/domains             建库（写 Profile 元数据，领域随即出现在中心）
GET  /api/domains             领域中心列表
GET  /api/domains/{d}         领域工作台（聚合内容/术语/主题）
GET  /api/domains/{d}/terms/{term}        术语详情（含各处 occurrence）
GET  /api/domains/{d}/topics/{topic}      主题详情
GET  /api/domains/{d}/topic-concepts      主题及其下概念
GET  /api/domains/{d}/concept-timeline    概念随时间出现的脉络
```

## 概念图 / 术语库（glossary）

PK `(domain, term)`。每个术语记录：

- `definition` / `status`（`suggested` | `accepted`）/ `is_topic`（是否粗粒度浏览主题）/ `definition_locked`
- `related`：关联术语
- `occurrences`：**类型化** 来源列表 `[{job_id, content_type, location}]`（替代旧的 `sources=[job_id]` 无类型形式）

### 喂养与回流

来源是评审步（`11_review` 等）产出的 `review.json`：

- scheduler 读 `key_terms`（这篇**讲清楚**的概念 + 候选定义）→ `add_glossary_suggestion`，落为 `status='suggested'` 的候选术语，并记一条 occurrence。
- `missing_concepts`（知识缺口）**不入库**，仅供评审面板/查漏。

用户在术语库审阅候选，采纳（accept）后 `status -> 'accepted'`，并**同步回流写入 Profile.terminology**，让后续 AI 步骤（如 `10_smart`）能用统一措辞，形成正反馈。详见 [06-prompt-engineering §4](../06-prompt-engineering.md)。

```
GET    /api/glossary                    术语列表（可按 domain/status 过滤）
POST   /api/glossary                    手动新增（直接 accepted + 回流 Profile）
GET    /api/glossary/{domain}/{term}    单个术语
PUT    /api/glossary/{domain}/{term}    编辑定义
POST   /api/glossary/{domain}/{term}/accept   采纳候选 → 回流 Profile
POST   /api/glossary/{domain}/{term}/topic    置/取消 is_topic
DELETE /api/glossary/{domain}/{term}    删除（不动 Profile）
```

单篇内容的概念可单独查看：

```
GET /api/jobs/{id}/concepts             这篇贡献/命中的概念
```

## 全文搜索

SQLite FTS5 虚拟表 `notes_fts5`，`tokenize=trigram`（适配中文无分词）。索引列：`job_id / content_type / note_type / collection_id / domain / title / body`。跨集合检索笔记、逐字稿、OCR、弹幕。

```
GET /api/search?q=...                   全文搜索（支持 content_type / domain / collection 过滤）
```

## 集合与订阅

一个学习主题 = 一个集合（可含多来源、多内容类型）。同 `domain` 的集合共享 Profile。

**订阅不是独立实体**：集合的 `source_type`/`source_id` 非空即为订阅集合。没有独立的
subscriptions 表/页面。`/{id}/sync` 从对应适配器枚举最新内容并入该集合；支持类型、展示元数据和
集合 id 策略以 `configs/sources.yaml` 为准。

```
POST /api/collections             创建集合（带 source_type/source_id 即订阅）
GET  /api/collections             列表
GET  /api/collections/{id}        详情
POST /api/collections/{id}/sync   同步订阅源最新内容
```

## 学习路径（远期）

跨集合/跨领域编排学习顺序，尚未实现。
