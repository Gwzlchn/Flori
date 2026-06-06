# ADR-0002: Redis 做任务队列

## 背景

需要步骤间的任务分发和状态管理机制。

## 选项

| 选项 | 优点 | 缺点 |
|------|------|------|
| Redis (Sorted Set + Pub/Sub) | 轻量、持久化、支持优先级、已有丰富数据结构 | 非专业消息队列 |
| RabbitMQ | 专业消息队列、可靠投递 | 多一个重量级组件 |
| Celery | Python 生态、Worker 管理 | 抽象过重、配置复杂 |
| 纯文件系统 | 零依赖 | 没有阻塞等待、轮询低效 |

## 决定

Redis。用 Sorted Set 做优先级队列，Pub/Sub 做事件通知，Hash 做状态存储。

## 理由

1. 一个 Redis 覆盖队列+状态+事件三种需求，不需要额外组件
2. Sorted Set 天然支持优先级调度
3. 内存 <50MB（个人工具规模），Alpine 镜像 <10MB
4. AOF 持久化，重启不丢数据
5. 远程 Worker 可以直连公网 Redis（加 TLS），不需要额外中间件

## 影响

调度器、Worker、API 都依赖 Redis。Redis 是唯一的有状态中间件（除了 SQLite）。
