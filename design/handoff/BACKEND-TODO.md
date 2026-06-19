# 前端重建期间发现的「需后端新增」清单

> 前端在 `feat/fe-redesign` 上已按新设计全量重建完成（vue-tsc + vite build 通过）。下列是重建过程中遇到、当前用前端兜底、但建议后端补齐的能力。前端已能跑（用既有契约兜底），补齐后体验/性能更好。给 Claude Code 参考，按需排期。

| # | 能力 | 现状（前端兜底） | 建议后端 | 影响视图 |
|---|------|------------------|----------|----------|
| 1 | **创建知识库** | 「新建知识库」弹窗只 `showToast` 提示，不发请求 | `POST /api/domains`（name + 图标 + 颜色 + role + 简介） | 知识库总览 |
| 2 | **知识库 图标/颜色 持久化** | 按 `domain` 名哈希派生（稳定但用户不可选） | domain 元数据存 icon/color 字段（随 #1 一起） | 总览 / 工作台 / 侧栏 |
| 3 | **内容列表按 来源/知识库 过滤** | `GET /api/jobs` 仅支持 `status=`；来源/知识库在已加载列表上做客户端过滤，chip 计数也基于已加载 | `GET /api/jobs` 增 `source=` 与 `domain=` query 参数（计数改后端聚合） | 所有来源 |
| 4 | **某内容命中的概念（反查）** | 拉整库 `GET /api/glossary?domain=` 再按 `occurrences.job_id` 客户端筛 | `GET /api/jobs/{id}/concepts`（直接返回该内容命中的概念 + 首次出现位置） | 内容详情·概念 tab |
| 5 | **job → 集合名** | 元信息只能显示 `collection_id`（无单 job→集合名的轻量端点） | `GET /api/jobs/{id}` 的 `meta` 带 `collection_name` | 内容详情·元信息 |
| 6 | **概念时间线聚合** | 用 `glossary.occurrences ⋈ 工作台 recent_jobs 日期` 拼；数据不足时退化为演示数据 | `GET /api/domains/{domain}/concept-timeline?granularity=`（桶 + 各概念计数 + 出处），精确高效 | 工作台·时间线 tab |

> 说明：以上均不阻塞前端运行。契约仍以 `docs/03-contracts.md`（origin/main af4b1ff）为准；若后端新增上述端点，记得同提交更新该契约文档并用 `contract:` 前缀标注，前端会跟进对接。
