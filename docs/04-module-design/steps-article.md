# 文章分析步骤

> M5 实现时补充。以下为初步设计方向。

## 概述

网页文章 / 微信公众号文章自动生成笔记。是所有内容类型中最简单的——不需要视频处理，只需提取正文 + AI 摘要。

## 步骤草案

```
00_download → 20_extract → 21_smart_article → 22_review
```

| 步骤 | 说明 |
|------|------|
| 20_extract | 正文提取（readability / trafilatura） |
| 21_smart_article | Claude 生成结构化笔记 |
| 22_review | 质量评审 |

## 与视频步骤的复用

- 共用 StepBase、调度器、Worker 框架
- 共用 00_download（增加网页抓取支持）
- 21_smart_article 和 22_review 复用 Prompt 模板结构
