# vNext 开发与部署

## 环境

- Rust 开发允许宿主机安装 `rust-toolchain.toml` 锁定的工具链，直接执行 fmt、check、clippy 和 test。
- Python、Node 和外部媒体工具不安装到宿主机；需要时使用 Docker target。
- 生产只以 Docker 镜像部署，不直接运行宿主二进制。
- 每个 worktree 使用独立 Cargo target 和测试临时根；共享 registry/git 下载缓存。

WP04 最终只提供三个 Compose 入口：

| 入口 | 用途 |
|---|---|
| dev | 本地 Server、Runner 和前端开发 |
| test | SQLite/HTTP/侧车 integration |
| prod | ECS Edge、Home Core 和 Runner 部署 |

## 镜像

首版只发布：

1. `flori-edge`
2. `flori-server`
3. `flori-runner-media`
4. `flori-runner-ai-qoder`
5. `flori-runner-ai-codex`

测试不复制五套 runtime 镜像。document extractor、yt-dlp、yutto、FFmpeg 和 Whisper 属于 media Runner 内的短命执行器。

QoderCLI 和 CodexCLI 使用人工锁定版本：

1. 修改一个版本锁。
2. 只构建受影响 AI Runner 镜像。
3. 检查 CLI 版本、model/effort 枚举和最小调用。
4. 用真实 Runner 凭据做一次 websearch 探测。
5. 发布不可变镜像 digest；失败继续使用上一个 digest。

本地 Docker build 可显式传 PC 代理；GitHub build 不读取、不要求本地代理。BuildKit 缓存按 CLI 版本和依赖层复用，不在每次构建解析 latest。

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
