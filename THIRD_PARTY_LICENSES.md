# 第三方许可与致谢

本文件列出随 Flori 镜像与构建产物分发的第三方组件,以及各自的许可。覆盖范围:pip 运行时依赖(含 `steps`/`api`/`gpu`/`worker` 可选组)、镜像内 apt 安装的系统二进制、前端运行时依赖。版本以 `pyproject.toml`、`docker/*.Dockerfile`、`frontend/package.json` 声明的区间为准。

## 重点提示:Copyleft 组件

以下组件采用强 Copyleft 许可,集成与分发方式需单独评估:

- **PyMuPDF (`fitz`)** — **AGPL-3.0**。当前 paper pipeline 已不 import 它，`04_figures` 也已删除；
  但旧 `docker/step-heavy.Dockerfile` 仍显式安装该包，因此分发这个兼容镜像时仍必须按实际镜像内容评估
  AGPL 义务，不能因为当前步骤未调用就从许可清单删除。
  - https://github.com/pymupdf/PyMuPDF · License: https://www.gnu.org/licenses/agpl-3.0.html
- **yutto** — **GPL-3.0**。B站视频下载器,作为命令行/进程被调用,随 `steps` 组及纯 Python 步骤镜像(`docker/step-base.Dockerfile`)分发。
  - https://github.com/yutto-dev/yutto · License: https://www.gnu.org/licenses/gpl-3.0.html

集成方式与许可影响的讨论见 `docs/13-dependencies.md` 第 9 节。

## pip 依赖 — 核心(`dependencies`)

| 名称 | License | 用途 | 链接 |
|------|---------|------|------|
| redis (redis-py) | MIT | Redis 异步客户端(队列/事件/心跳) | https://github.com/redis/redis-py |
| PyYAML | MIT | 加载 YAML 配置 | https://github.com/yaml/pyyaml |
| structlog | Apache-2.0 OR MIT(双许可) | 结构化日志 | https://github.com/hynek/structlog |
| anthropic | MIT | Claude API SDK | https://github.com/anthropics/anthropic-sdk-python |
| openai | Apache-2.0 | OpenAI 兼容 API SDK | https://github.com/openai/openai-python |
| httpx | BSD-3-Clause | 异步 HTTP 客户端 | https://github.com/encode/httpx |
| pydantic | MIT | 数据模型与校验 | https://github.com/pydantic/pydantic |
| minio (minio-py) | Apache-2.0 | 分布式 worker 模式的对象存储客户端 | https://github.com/minio/minio-py |
| websockets | BSD-3-Clause | WebSocket(base 镜像额外安装) | https://github.com/python-websockets/websockets |

## pip 依赖 — 步骤(`steps`)

| 名称 | License | 用途 | 链接 |
|------|---------|------|------|
| **yutto** | **GPL-3.0** | B站视频下载(见上方重点提示) | https://github.com/yutto-dev/yutto |
| yt-dlp | Unlicense(公有领域) | YouTube/通用视频下载 | https://github.com/yt-dlp/yt-dlp |
| scenedetect (PySceneDetect) | BSD-3-Clause | 视频场景检测 | https://github.com/Breakthrough/PySceneDetect |
| opencv-python-headless | Apache-2.0 | 帧提取/图像处理(由 `scenedetect[opencv]` 引入) | https://github.com/opencv/opencv-python |
| Pillow | HPND(MIT-CMU 风格) | 图像读写/处理 | https://github.com/python-pillow/Pillow |
| imagehash | BSD-2-Clause | 感知哈希去重 | https://github.com/JohannesBuchner/imagehash |
| scikit-image | BSD-3-Clause | SSIM 结构相似度 | https://github.com/scikit-image/scikit-image |
| rapidocr-onnxruntime | Apache-2.0 | CPU OCR(ONNX 推理) | https://github.com/RapidAI/RapidOCR |
| **PyMuPDF (`fitz`)** | **AGPL-3.0** | 旧 step-heavy 镜像残留依赖，当前 pipeline 不调用(见上方重点提示) | https://github.com/pymupdf/PyMuPDF |
| trafilatura | Apache-2.0 | 网页正文提取 | https://github.com/adbar/trafilatura |

## pip 依赖 — API(`api`)

| 名称 | License | 用途 | 链接 |
|------|---------|------|------|
| FastAPI | MIT | Web API 框架 | https://github.com/fastapi/fastapi |
| uvicorn | BSD-3-Clause | ASGI 服务器 | https://github.com/encode/uvicorn |
| aiofiles | Apache-2.0 | 异步文件 IO | https://github.com/Tinche/aiofiles |
| slowapi | MIT | 限流中间件 | https://github.com/laurents/slowapi |
| python-multipart | Apache-2.0 | 解析 multipart 表单/上传 | https://github.com/Kludex/python-multipart |
| qrcode | BSD-3-Clause | 生成 B站登录二维码 PNG(`api/routes/bili.py`) | https://github.com/lincolnloop/python-qrcode |
| Pillow | HPND(MIT-CMU 风格) | qrcode 图像后端 | https://github.com/python-pillow/Pillow |

## pip 依赖 — GPU(`gpu`)

| 名称 | License | 用途 | 链接 |
|------|---------|------|------|
| faster-whisper | MIT | Whisper 语音转写(CTranslate2 加速) | https://github.com/SYSTRAN/faster-whisper |

> GPU 镜像基于 `nvidia/cuda` 运行时镜像;CUDA/cuDNN 等 NVIDIA 组件受 NVIDIA 各自的许可条款约束,随基础镜像分发,不在本清单逐项展开。

## pip 依赖 — Worker(`worker`)

| 名称 | License | 用途 | 链接 |
|------|---------|------|------|
| docker (docker-py) | Apache-2.0 | docker 执行器的 Python SDK | https://github.com/docker/docker-py |

## 系统二进制(镜像内 apt 安装)

| 名称 | License | 用途 | 链接 |
|------|---------|------|------|
| FFmpeg | LGPL-2.1+ / GPL-2.0+(取决于构建配置) | 音视频解码/编码/抽帧 | https://ffmpeg.org/ · https://ffmpeg.org/legal.html |
| curl | curl(MIT/X 风格) | 镜像内 HTTP 工具 | https://curl.se/ · https://curl.se/docs/copyright.html |

> FFmpeg 的有效许可取决于发行版构建是否启用了 GPL 组件;Debian/Ubuntu 软件源提供的 ffmpeg 通常包含 GPL 组件,故按 GPL 对待更稳妥。

## 前端运行时依赖(`frontend/package.json` dependencies)

| 名称 | License | 用途 | 链接 |
|------|---------|------|------|
| vue | MIT | UI 框架 | https://github.com/vuejs/core |
| vue-router | MIT | 路由 | https://github.com/vuejs/router |
| pinia | MIT | 状态管理 | https://github.com/vuejs/pinia |
| markdown-it | MIT | Markdown 渲染 | https://github.com/markdown-it/markdown-it |
| lucide-vue-next | ISC | 图标组件 | https://github.com/lucide-icons/lucide |

> `frontend/package.json` 仍声明了 npm 包 `qrcode`(MIT),但前端源码(`frontend/src/`)未实际 `import` 它——登录二维码由后端 Python `qrcode` 生成 PNG,前端只渲染 `<img>`。该 npm 依赖未进入前端构建产物,可在后续清理时移除。
>
> 构建期工具(Vite、Tailwind CSS、TypeScript、vue-tsc、PostCSS 等 `devDependencies`)不随前端产物分发,故不在此逐项列出;它们均为 MIT/ISC 许可。

## Apache-2.0 NOTICE 传递

上表中 Apache-2.0 组件(openai、minio-py、aiofiles、python-multipart、opencv-python-headless、rapidocr-onnxruntime、trafilatura、docker-py、structlog 的 Apache 选项等)随镜像/产物再分发时,需保留其许可与各自的 NOTICE(如随包附带)。本文件即作为对这些组件的署名与许可声明。

## 设计致谢

- **执行器接口/设计**:docker 执行器(每步一容器、共享工作卷、cleanup 必执行、防泄漏 labels)的接口与设计借鉴自 **GitLab Runner**(MIT License, © GitLab Inc.)。
