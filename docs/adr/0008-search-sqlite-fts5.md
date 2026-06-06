# ADR-0008: SQLite FTS5 做全文搜索

## 背景

M2 需要跨视频搜索笔记、逐字稿、OCR 文字、弹幕。

## 选项

| 选项 | 优点 | 缺点 |
|------|------|------|
| SQLite FTS5 | 零运维、内嵌、中文分词可用 | 不如 ES 强大 |
| Elasticsearch | 功能最强、中文分词好 | 重量级（需 2GB+）、多一个组件 |
| MeiliSearch | 轻量、中文友好 | 多一个组件 |
| 纯 grep | 零依赖 | 慢、无排名 |

## 决定

SQLite FTS5。

## 理由

1. 数据量小（百级视频，万级段落），FTS5 足够
2. 已经用 SQLite 做元数据存储，FTS5 内嵌不需要额外组件
3. 查询 <1 秒（目标）
4. 个人工具不需要 ES 级别的复杂查询
5. 中文用 simple tokenizer（按字分词）或 jieba 分词，效果可接受

## 影响

- M2 在 SQLite 中创建 FTS5 虚拟表
- 每个视频完成后更新搜索索引
- API 暴露 `/api/knowledge/search` 端点
