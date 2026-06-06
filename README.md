# AI 个人学习知识库

把视频、论文、文章等学习材料，自动转化为结构化笔记，积累为可检索的个人知识体系。

## 特点

- **多来源**：B站/YouTube 视频、PDF 论文、网页文章
- **AI 驱动**：Claude 生成结构化笔记、术语解释、质量评审
- **知识积累**：跨视频搜索、术语词典、学习路径
- **手机友好**：粘贴 URL 投递，手机阅读笔记
- **自托管**：Docker 一键部署，数据完全自有

## 快速开始

```bash
git clone https://github.com/xxx/ai-knowledge-base
cd ai-knowledge-base
cp .env.example .env    # 配置 Claude CLI 路径等
docker compose up -d    # 启动全部服务
# 浏览器打开 http://localhost:8080
```

投递第一个视频：粘贴 URL → 等待处理 → 阅读笔记。

## 架构

```
docker compose up 一键启动:

┌────────────────────────────────────────────────┐
│  Caddy (前端 + 反向代理)          :8080        │
│  API (FastAPI)                    :8000        │
│  Scheduler (任务调度)                          │
│  Redis (任务队列)                 :6379        │
│  Worker-CPU (场景检测/OCR/去重)                │
│  Worker-Claude (字幕加标点/笔记生成/评审)      │
└────────────────────────────────────────────────┘
```

可选：接入 GPU 机器加速处理。

```bash
# GPU 机器上一条命令接入
docker run --gpus all -e REDIS_URL=redis://主机IP:6379 worker-gpu
```

## 笔记效果

每个视频生成两份笔记：

- **机械版**：完整逐字稿（带标点）+ 关键帧截图 + OCR 文字 + 弹幕
- **智能版**：AI 按主题重组的结构化笔记，含术语解释、要点回顾

电脑端支持笔记+视频分屏回放，点击时间戳跳转到对应片段。

## 文档

详见 [docs/README.md](docs/README.md)

## 状态

开发中 → 详见 [ROADMAP.md](ROADMAP.md)

## License

MIT
