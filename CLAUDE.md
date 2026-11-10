# Flori Agent 指南

本文件同时供 Claude Code 与 Codex 使用;`AGENTS.md` 是它的软链,只维护这一份。

## 产品

把 B站、YouTube 视频和 arXiv HTML 论文变成 Obsidian 笔记。NAS 上一个 `flori` 容器提供网页(投递、流水线与日志、预览、领域/集合、手动订阅)并执行两条固定流水线,发布到 vault 目录;电脑和手机用 Remotely Save 经 WebDAV 同步。

不做:PDF、网页文章、音频、本地视频、站内搜索、问答、术语/概念、SRS、MCP、多 worker、定时订阅。扩大范围先问用户。

外网访问只经 ECS 原样转发 TCP,TLS 在 NAS 的 Caddy 终止;ECS 未备案,带域名的连接会被重置,所以一律按 IP 访问:WebDAV 走 :8790,网页走 :8792。

使用门:每周投递至少 3 次;连续 2 周未达标,冻结新功能只修 bug。

## 架构

- 单 crate `flori`,单二进制:`flori serve` 与 `flori step <name>`。下载、抓取、转写、AI 都走子进程。
- 按职责成文件,一个文件讲清一件事;文件长到难以维护时按职责拆成多个文件,不为控制行数删功能。不建只转发的层,第三个真实调用方出现前不抽通用框架。
- 契约只在 [docs/contracts.md](docs/contracts.md):步骤 CLI、vault 布局、HTTP API。改契约时 `src/pipeline.rs`、`src/api.rs` 与它同一提交。
- 类型链:Rust 类型 → utoipa → `docs/openapi.json` → openapi-typescript → 前端。前端不手写 API 类型;改接口后跑 `just openapi`。
- SQLite 是唯一结构化真相,SQLx 运行时查询,迁移只向前。
- vault 只追加:发布后服务端不再改或删已发布内容。
- NAS 常驻容器只有 flori、flori-dav、flori-backup。新增常驻容器或跨容器协议先问用户。
- 配置只走环境变量。密钥只在 `.local/secrets.env`,按名注入各服务;flori 容器拿不到 AK 和 WebDAV 口令。

## 工作方式

- 唯一入口是 `justfile`;提交前跑 `just check`。
- 新依赖在提交说明里写理由。
- 处理外部内容的 Python 一律 `python3 -I`。
- 真实工具链与真实 AI 不进 CI,由本地 `just smoke` 覆盖;mock 只替代外部站点、AI CLI 和媒体工具。
- 并行只用于起步冲刺:热点文件(Cargo.toml/lock、migrations/、src/api.rs、docs/、justfile、CI、compose)同一时间只有一个 owner。worktree 放仓库同级的 `../flori-working/wt/<slug>`,合入后立即删除。
- 工作日志写主检出的 `.local/processing/<日期>/`,时间戳先跑 `date`。

## 需要用户单独授权

生产部署、停机、删数据、凭据、外部账号、AI 调用费用、公网变化、push 以外的远程操作(删分支、改默认分支、GHCR、仓库可见性)。先说清目标、拒绝条件、回滚和验收。

## Git

- 用已配置的 `user.name` 和 `user.email`,不在命令里覆盖;缺失时问用户。
- 一个可独立验收和回滚的价值对应一个提交。普通提交标题不带版本号;版本只在 `just release X.Y.Z` 时写进 Cargo.toml 并打 tag。

## 注释与文档

- 注释解释不变量、边界和坑,不复述代码,不写版本号、日期和已删除的历史。
- 直陈短句,半角标点;不用装饰分隔线、符号和嵌套括号长句。
- 一个规则只有一个权威位置,其它地方用链接引用。
