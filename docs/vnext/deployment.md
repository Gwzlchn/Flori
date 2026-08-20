# vNext 开发与部署

## 环境

- Rust 开发允许宿主机安装 `rust-toolchain.toml` 锁定的工具链，直接执行 fmt、check、clippy 和 test。
- Python、Node 和外部媒体工具不安装到宿主机；需要时使用 Docker target。
- 生产只以 Docker 镜像部署，不直接运行宿主二进制。
- 每个 worktree 使用独立 Cargo target 和测试临时根；共享 registry/git 下载缓存。

WP04 只提供三个 Compose 入口：

| 文件 | 用途 |
|---|---|
| `compose.dev.yml` | 宿主 Cargo 与容器前端的开发循环 |
| `compose.test.yml` | Rust、前端和基础契约检查 |
| `compose.prod.yml` | 五个冻结镜像的部署拓扑骨架 |

`compose.prod.yml` 现在可启动 Edge、Home Core 和按 profile 选择的三个 Runner，但仍不是生产发布方案。公网隧道、Runner 注册令牌、健康检查和生产切换继续留在 WP15-WP16。

WP09 后 Server 控制面可用显式命令启动：

```text
flori-server serve <listen> <sqlite> <artifact-root> <artifact-download-base> <max-artifact-bytes> <lease-ms>
```

进程先严格打开当前SQLite schema和NAS根，再按upload ledger完成恢复；只有恢复成功才绑定 `listen`。参数缺失、旧schema、损坏ledger、非法下载base或NAS错误都会直接退出。Compose把该命令固定到`/data/flori.sqlite`和`/data/artifacts`，并要求`FLORI_SERVER_URL`与Runner实际访问的无尾斜杠HTTPS地址一致。Edge只负责静态页面和`/api`反向代理；WP15前不把这套配置描述为生产部署方案。

## 镜像

首版只发布：

1. `flori-edge`
2. `flori-server`
3. `flori-runner-media`
4. `flori-runner-ai-qoder`
5. `flori-runner-ai-codex`

测试不复制五套 runtime 镜像。document extractor、yt-dlp、yutto、FFmpeg 和 Whisper 属于 media Runner 内的短命执行器。

QoderCLI 和 CodexCLI 使用人工锁定版本。vNext首版固定为 QoderCLI `1.1.26` 和 CodexCLI `0.148.0`：

1. 修改一个版本锁。
2. 只构建受影响 AI Runner 镜像。
3. 检查 CLI 版本、model/effort 枚举和最小调用。
4. 用真实 Runner 凭据做一次 websearch 探测。
5. 发布不可变镜像 digest；失败继续使用上一个 digest。

media、Qoder 和 Codex Runner 使用三个独立二进制与 Dockerfile，分别裁剪未使用的 media/Qoder/Codex 代码。任一 Runner 的工具层或代理变化不共用另一个 Runner 的构建图，镜像名仍固定为上面的五个。

本地 Qoder 和 Codex build 分别只读取 `FLORI_QODER_BUILD_PROXY` 与 `FLORI_CODEX_BUILD_PROXY`，不设共享 fallback；media build 不读取这两个变量。GitHub build 不读取、不要求本地代理。BuildKit 缓存按镜像、CLI版本和依赖层复用，不在每次构建解析 latest。CI并行构建五个镜像，并在缓存填充后对每个 warm rebuild 执行120秒硬门；冷构建时间必须单独报告，不能冒充两分钟验收。

两个AI镜像必须在构建时以最终非root用户执行无费用版本和帮助探针。Qoder镜像核对精确版本与`--tools`，Codex镜像核对精确版本、`--search`、`exec --json`和`--output-schema`；任一不符直接构建失败。探针不登录、不调用模型。

AI Runner 容器启动后先严格读取Server URL、token、model、effort、spool和登录态目录。缺失或非法配置在poll和CLI前非零退出。Compose不为这些值提供可运行默认值；未激活AI profile时也不因变量插值阻断Server等其它服务。

Qoder 与 Codex Runner 分别必须显式提供裸 `http://host[:port]` 形式的 `FLORI_QODER_PROXY_URL` 和 `FLORI_CODEX_PROXY_URL`。每个值只注入对应的单次 CLI 子进程，不进入另一 Provider、Job、日志或 Artifact；任一缺失或非法都在poll前失败，不读取宿主代理，也不跨Provider fallback。容器内必须使用自身可达的代理地址；SSH tunnel、私网路由和代理服务由部署环境负责，Flori 不创建或守护网络隧道。

## 普通升级

- 代码或镜像变化但 schema 未变：停止相关容器并启动新镜像，不备份 SQLite。
- Artifact 文件不由 Flori 备份；NAS 自己负责快照和硬盘恢复。
- 不支持新旧 Server/Runner 混部、旧 writer 写新 schema 或历史协议兼容。

## schema 变化

1. 暂停新投递和订阅。
2. 等短 Task 完成，取消并 fence 长 Task。
3. 停止全部 Home Core writer。
4. 保留旧 SQLite 文件和旧镜像引用。
5. 使用单一迁移 owner 执行当前 schema 到下一 schema 的迁移。
6. 校验 schema version、usage 唯一性、current/previous、readiness 和组件版本。
7. 成功后恢复；失败立即退出并换回旧 SQLite 和旧镜像。

不开发历史 schema 读取、双写、在线兼容或通用备份恢复 UI。

## Python 到 Rust 冷切换

- Rust 使用空 SQLite 和新的 NAS Artifact 根。
- 不导入旧 Job、Source、Glossary、订阅、索引、usage 或 Artifact。
- Domain、Collection、Profile、Prompt 和订阅通过 Rust 正常 UI/API 重建。
- PDF、视频、频道和本地文件通过 Rust 正常投递重新生成。
- 切换前给最后 Python 生产版本打 tag，冻结回退命令。
- Rust 真实验收通过后才切公网入口；旧系统在观察窗口结束前保持可回退。
- 清理旧 Python、Redis、MinIO 和旧数据属于 WP16 单独授权的破坏性操作。
