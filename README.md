# Flori

把 B站、YouTube 视频和 arXiv 论文变成 Obsidian 笔记的家庭 NAS 应用。

- 网页投递链接,看流水线与日志,预览 Markdown,播放视频,管理领域、集合和订阅。
- 视频:下载 → 字幕或 whisper 转写 → 关键帧 → AI 笔记 → 非中文时自动翻译,中文时由 AI 只加标点 → 发布。
- arXiv:官方 HTML(回退 ar5iv)→ Markdown → AI 笔记 → 自动翻译 → 发布。
- 发布即把笔记文件夹原子写入 vault;Obsidian 用 Remotely Save 经 WebDAV 同步。

契约见 [docs/contracts.md](docs/contracts.md),开发约定见 [CLAUDE.md](CLAUDE.md)。

## 开发

```sh
just check     # fmt、clippy、Rust 测试、前端类型与构建、Python 测试
just openapi   # 改了 src/api.rs 后重新生成 docs/openapi.json 与前端类型
```

依赖:Rust 1.95(rust-toolchain.toml)、Node 24、Python 3.11、just。

## 布局

```text
src/          Rust 服务端、执行器与内置步骤
steps/        Python 步骤:arxiv.py、feed.py、whisper.py
migrations/   SQLite 迁移
frontend/     Vue 3 + TypeScript
docker/       镜像与部署
tests/        fixtures、假步骤与假 AI CLI、Python 测试
```

## License

MIT
