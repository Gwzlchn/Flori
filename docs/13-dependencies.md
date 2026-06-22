# 13 · 开源依赖

> 项目用到的开源工具和库。选型原则：优先成熟活跃的项目，优先中文生态好的工具。

## 1. 视频下载

| 工具 | 用途 | License | 说明 |
|------|------|---------|------|
| [yutto](https://github.com/yutto-dev/yutto) | **B站下载**（当前选用） | GPL-3.0 | Python，支持字幕/弹幕/批量/扫码登录 |
| [yt-dlp](https://github.com/yt-dlp/yt-dlp) | **YouTube + 通用下载** | Unlicense | 支持 1000+ 网站，最活跃的下载器 |
| [bilibili-api](https://github.com/Nemo2011/bilibili-api-python) | B站 API SDK | GPL-3.0 | 备选：扫码登录/视频信息/弹幕接口 |
| [bilix](https://github.com/HFrost0/bilix) | B站高速下载 | Apache-2.0 | 备选：asyncio，批量速度快 |

当前方案：B站用 yutto（原型已验证），其他平台用 yt-dlp 兜底。

## 2. 视频处理

| 工具 | 步骤 | License | 说明 |
|------|------|---------|------|
| [PySceneDetect](https://github.com/Breakthrough/PySceneDetect) | 03_scene | BSD-3 | 场景检测，AdaptiveDetector |
| [opencv-python-headless](https://github.com/opencv/opencv-python) | 03/04 | Apache-2.0 | 帧提取/图像处理 |
| [imagehash](https://github.com/JohannesBuchner/imagehash) | 05_dedup | BSD-2 | pHash 快速去重 |
| [scikit-image](https://github.com/scikit-image/scikit-image) | 05_dedup | BSD-3 | SSIM 结构相似度（精确确认） |
| [RapidOCR](https://github.com/RapidAI/RapidOCR) | 06_ocr (CPU) | Apache-2.0 | ONNX 推理，不依赖 PaddlePaddle |
| [PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR) | 06_ocr (GPU) | Apache-2.0 | 中文识别最强，需 GPU |
| [pysrt](https://github.com/byroot/pysrt) | 08_punctuate | GPL-3.0 | SRT 字幕解析 |
| [ffmpeg](https://ffmpeg.org/) | 多步骤 | LGPL/GPL | 视频解码/编码，系统依赖 |

## 3. 语音转写

| 工具 | 步骤 | License | 说明 |
|------|------|---------|------|
| [faster-whisper](https://github.com/SYSTRAN/faster-whisper) | 02_whisper（当前选用） | MIT | CTranslate2 加速，比原版快 4x |
| [openai/whisper](https://github.com/openai/whisper) | 备选 | MIT | 原版，更稳但慢 |
| [FunASR](https://github.com/modelscope/FunASR) | 待评估 | MIT | 阿里开源，中文识别可能优于 Whisper |

## 4. PDF 论文处理

| 工具 | 步骤 | License | 说明 |
|------|------|---------|------|
| [PyMuPDF](https://github.com/pymupdf/PyMuPDF) | 02_pdf_parse（当前选用） | AGPL-3.0 | 快，文本/图片/表格 |
| [pdfplumber](https://github.com/jsvine/pdfplumber) | 备选 | MIT | 表格解析优于 PyMuPDF |
| [marker](https://github.com/VikParuchuri/marker) | **待评估** | GPL-3.0 | PDF → Markdown，含公式/表格/图片 |
| [MinerU](https://github.com/opendatalab/MinerU) | **待评估** | AGPL-3.0 | 上海 AI Lab，中文论文效果好 |
| [Nougat](https://github.com/facebookresearch/nougat) | 待评估 | MIT | Meta，学术论文专用 |

> **marker 和 MinerU 值得重点评估**：它们直接将 PDF 转为结构化 Markdown（含公式/图表），可能替代 02_pdf_parse + 03_sections + 04_figures 三个步骤。M1 实现论文 pipeline 前应先对比测试。

## 5. 文章抓取（M6 已实现）

| 工具 | License | 说明 |
|------|---------|------|
| [trafilatura](https://github.com/adbar/trafilatura) | Apache-2.0 | 网页正文提取，Python |
| [readability](https://github.com/mozilla/readability) | Apache-2.0 | Mozilla 出品（JS） |
| [newspaper3k](https://github.com/codelucas/newspaper) | MIT | 新闻文章提取，含图片 |

## 6. 后端

| 工具 | 用途 | License |
|------|------|---------|
| [FastAPI](https://github.com/tiangolo/fastapi) | API 框架 | MIT |
| [uvicorn](https://github.com/encode/uvicorn) | ASGI 服务器 | BSD-3 |
| [redis-py](https://github.com/redis/redis-py) | Redis 客户端（asyncio） | MIT |
| [minio-py](https://github.com/minio/minio-py) | MinIO/S3 客户端 | Apache-2.0 |
| [structlog](https://github.com/hynek/structlog) | 结构化日志 | Apache-2.0 |
| [httpx](https://github.com/encode/httpx) | HTTP 客户端（asyncio） | BSD-3 |
| [anthropic](https://github.com/anthropics/anthropic-sdk-python) | Claude API | MIT |
| [openai](https://github.com/openai/openai-python) | OpenAI 兼容 API | Apache-2.0 |

## 7. 前端

| 工具 | 用途 | License |
|------|------|---------|
| [Vue 3](https://github.com/vuejs/core) | UI 框架 | MIT |
| [Vite](https://github.com/vitejs/vite) | 构建工具 | MIT |
| [Tailwind CSS](https://github.com/tailwindlabs/tailwindcss) | 样式 | MIT |
| [Pinia](https://github.com/vuejs/pinia) | 状态管理 | MIT |
| [Vue Router](https://github.com/vuejs/router) | 路由 | MIT |
| [video.js](https://github.com/videojs/video.js) | 视频播放器 | Apache-2.0 |
| [markdown-it](https://github.com/markdown-it/markdown-it) | Markdown 渲染 | MIT |
| [qrcode-vue3](https://github.com/nicedash/qrcode-vue3) | 二维码 | MIT |
| [html2pdf.js](https://github.com/eKoopmans/html2pdf.js) | PDF 导出（M3） | MIT |

## 8. 基础设施

| 工具 | 用途 | License |
|------|------|---------|
| [Redis](https://github.com/redis/redis) | 任务队列 + 事件 + 心跳 | BSD-3 |
| [MinIO](https://github.com/minio/minio) | 远程 Worker 文件中转 | AGPL-3.0 |
| [Docker](https://www.docker.com/) | 容器化部署 | Apache-2.0 |
| [Caddy](https://github.com/caddyserver/caddy) | 公网入口（自签 TLS + Basic Auth，边缘反代） | Apache-2.0 |
| [autossh](https://www.harding.motd.ca/autossh/) | NAS→边缘 反向 SSH 隧道（保活） | BSD/GPL |
| [Ollama](https://github.com/ollama/ollama) | 本地 LLM 运行 | MIT |

## 9. License 注意

| License | 影响 | 涉及工具 |
|---------|------|---------|
| **AGPL-3.0** | 网络使用需开源 | PyMuPDF, MinerU, MinIO |
| **Apache-2.0** | 公网入口（替代 Cloudflare Tunnel） | Caddy |
| **GPL-3.0** | 分发需开源 | yutto, pysrt, marker |
| MIT/Apache/BSD | 无限制 | 其他大部分工具 |

本项目计划以 MIT 开源。AGPL/GPL 工具的集成方式因运行模式而异，需分两种情况看待：

- **docker 模式（`STEP_RUNTIME=docker`）**：每个步骤在独立容器内作为独立进程运行，本项目代码与 AGPL/GPL 组件不在同一进程、不发生链接。这种"独立进程调用"的形态通常被视为未构成衍生作品，但是否满足对应 License 的全部义务仍需自行确认。
- **默认 subprocess 模式（`STEP_RUNTIME=subprocess`，worker 的默认值）**：步骤以 `python3 -m <module>` 子进程运行，步骤代码与库**同进程加载**——例如 `steps/paper/step_02_pdf_parse.py` 直接 `import fitz`（PyMuPDF，AGPL-3.0）。此时步骤代码与 AGPL 组件构成同进程链接，通常被视为衍生作品，分发/对外提供服务时可能触发 AGPL 的源码提供义务，需自行确认合规边界。

因此在默认模式下，"独立进程、不链接"的论断不成立。若要规避 AGPL/GPL 传染，可选项包括：仅以 docker 模式运行涉及这些组件的步骤、将相关步骤替换为更宽松许可的实现（如 PyMuPDF → pdfplumber），或就具体分发场景咨询法律意见。本节为工程性说明，不构成法律结论。
