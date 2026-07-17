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

## 4. Document 解析

| 工具 | 步骤 | License | 说明 |
|------|------|---------|------|
| [Poppler](https://poppler.freedesktop.org/) | Document `02_parse` | GPL-2.0+ | `pdfinfo` 取元数据，`pdftohtml -xml` 建主文本层，`pdftotext -bbox-layout` 作降级 |
| arXiv LaTeXML HTML | Document `02_parse` | 上游内容 | 学术 HTML 直接保留 DOM、公式和引用结构，不先转换为 Markdown |
| [PyMuPDF](https://github.com/pymupdf/PyMuPDF) | Document `02_parse` | AGPL-3.0 | 扫描 PDF 逐页渲染、OCR 坐标换算和 Figure/Table 区域提取 |
| [RapidOCR](https://github.com/RapidAI/RapidOCR) | Document `02_parse` | Apache-2.0 | 扫描 PDF 无可靠文本层时生成带置信度的 OCR locator |
| [marker](https://github.com/VikParuchuri/marker) | **待评估** | GPL-3.0 | PDF → Markdown，含公式/表格/图片 |
| [MinerU](https://github.com/opendatalab/MinerU) | **待评估** | AGPL-3.0 | 上海 AI Lab，中文论文效果好 |
| [Nougat](https://github.com/facebookresearch/nougat) | 待评估 | MIT | Meta，学术论文专用 |

> HTML 和 PDF 都是原始真相源。PDF adapter 只生成结构化 Document、文本层和定位坐标，不生成原文 Markdown；只有量化评测证明当前 adapter 不满足需求时，才评估 marker/MinerU。

## 5. HTML Document 抓取

| 工具 | License | 说明 |
|------|---------|------|
| [trafilatura](https://github.com/adbar/trafilatura) | Apache-2.0 | 下载阶段的通用元数据与正文辅助提取；Document adapter 仍以不可变 HTML DOM 为原文真相 |
| [newspaper3k](https://github.com/codelucas/newspaper) | MIT | 新闻文章提取，含图片（备选，未用） |

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
| [langdetect](https://github.com/Mimino666/langdetect) | 文章/论文正文主语言检测 | Apache-2.0 |

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
| **AGPL-3.0** | 网络使用与镜像分发需关注源码提供义务 | PyMuPDF(Document PDF/OCR), MinerU(未采用), MinIO |
| **Apache-2.0** | 公网入口（替代 Cloudflare Tunnel） | Caddy |
| **GPL-3.0** | 分发需开源 | yutto, pysrt, marker |
| MIT/Apache/BSD | 无限制 | 其他大部分工具 |

本项目计划以 MIT 开源。AGPL/GPL 工具的集成方式因运行模式而异，需分两种情况看待：

- **docker 模式（`STEP_RUNTIME=docker`）**：每个步骤在独立容器内作为独立进程运行，本项目代码与 AGPL/GPL 组件不在同一进程、不发生链接。这种"独立进程调用"的形态通常被视为未构成衍生作品，但是否满足对应 License 的全部义务仍需自行确认。
- **默认 subprocess 模式（`STEP_RUNTIME=subprocess`，worker 的默认值）**：步骤以 `python3 -m <module>` 子进程运行。Python 库若被步骤直接 import，仍与步骤同进程；Poppler、yutto 等命令行工具则经 subprocess 边界调用。具体分发仍需逐项遵守上游 License。

因此不能笼统用"都在容器/子进程"推断无义务。发布镜像或对外提供服务前应按实际安装依赖和调用边界复核，并在必要时咨询法律意见。本节为工程性说明，不构成法律结论。
