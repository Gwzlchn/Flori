# 知识存储

> M2 实现时补充。以下为设计方向。

## 概述

从"44 份独立笔记"升级为"可搜索、可关联的个人知识库"。

## 核心功能

### 集合管理

一个学习主题 = 一个集合（可包含多来源、多内容类型）。同 domain 的集合共享 Profile。

```
API:
  POST   /api/collections           创建集合
  GET    /api/collections           列出所有
  GET    /api/collections/{id}      集合详情
  POST   /api/collections/{id}/sync 同步创作者最新内容
```

### 全文搜索

SQLite FTS5，跨集合检索笔记、逐字稿、OCR、弹幕。

```sql
SELECT job_id, title,
       snippet(search_index, 5, '<mark>', '</mark>', '...', 30) as excerpt
FROM search_index
WHERE search_index MATCH '注意力 蒸馏'
ORDER BY rank;
```

### 术语词典

从笔记中自动提取术语，跨集合关联。

```
API:
  GET /api/knowledge/glossary           全部术语
  GET /api/knowledge/glossary/{term}    单个术语
  GET /api/knowledge/search?q=...       全文搜索
```

### 学习路径（远期）

跨集合编排视频学习顺序。

## 与 M1 的关系

M1 不需要集合和搜索。Job 直接创建，不属于任何集合。M2 追加集合维度后，可以给已有 Job 补充 collection_id。
