# Flori 文档入口

## rust-vnext 分支

从 [vNext 文档入口](vnext/README.md) 开始。它是 Rust 重写的唯一产品与架构真相。

建议只读：

1. `CLAUDE.md`
2. [vNext 产品范围](vnext/product.md)
3. 当前任务对应的 architecture、contracts、pipeline-runner、development 或 deployment 文档

不要先通读旧系统全部文档，也不要从旧 Python 类型和测试推导兼容要求。

## Python 生产系统

WP16 冷切换前的 Python 生产事实只保留在 `main` 和 Git 历史。本分支不携带旧架构文档，避免未来 Agent 把历史设计当成兼容要求。

## 写文档的边界

- 稳定产品决策写 `docs/vnext/` 或 ADR。
- Rust 类型、SQLite migration 和生成 OpenAPI 是实现后的代码级真相，文档不复制字段表。
- 开发过程只写一份简短 WP 记录；普通小改不建长日志。
- 对外契约、实现、消费方和验收必须在同一个 WP 闭环。
