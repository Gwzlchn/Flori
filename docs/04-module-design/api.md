# API 服务

> FastAPI 应用。职责：接收用户请求、管理任务、服务文件、推送进度。
> 不执行分析步骤，不直接操作 Redis 队列（通过调度器）。

## 1. 模块结构

以 `api/routes/` 为准：

```
api/
├── main.py                 # FastAPI app + 启动（挂载各 router，含 jobs.providers_router）
├── deps.py                 # 依赖注入（Redis/DB/Auth）
├── routes/
│   ├── jobs.py             # /api/jobs（+facets、/{id}/concepts、/retry/rerun/rerun-smart/resubmit）+ /api/providers
│   ├── notes.py            # 笔记/截图/产物文件服务（含视频 Range 流）
│   ├── ws.py               # WebSocket 进度推送（/api/ws/jobs/{id}、/api/ws/global）
│   ├── auth.py             # YouTube cookies / 平台 cookie 文件状态
│   ├── bili.py             # B站扫码登录（/api/bili/*，cookie 入库）
│   ├── admin.py            # 系统状态 / 健康 / 配置热更新（/api/status、/api/health、/api/config/*）
│   ├── collections.py      # 集合管理（含订阅集合 /{id}/sync）
│   ├── domains.py          # 领域知识中心（总览/建库/工作台/术语/主题/概念时间线）
│   ├── glossary.py         # 术语库 / 概念图（CRUD + accept + topic）
│   ├── search.py           # FTS5 全文检索（/api/search）
│   ├── profiles.py         # 领域 Profile（角色/风格/术语表 + 展示元数据）
│   ├── workers.py          # Worker 管理（列表/详情/更新/移除 + 铸接入 token）
│   └── runner.py           # Worker 网关（/api/runner/*：注册/心跳/认领/上报/产物代理）
└── Dockerfile
```

各端点契约以 [docs/03-contracts.md](../03-contracts.md) 为准。

## 2. 认证

单层 Bearer Token（个人工具，Basic Auth 在边缘 Caddy 层做；用户名 flori）：

```python
API_TOKEN = os.environ["API_TOKEN"]

@app.middleware("http")
async def auth_middleware(request, call_next):
    if request.url.path.startswith("/api/") and request.url.path != "/api/health":
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if token != API_TOKEN:
            return JSONResponse(status_code=401, content={"error": "unauthorized"})
    return await call_next(request)
```

前端经边缘 Caddy 的 Basic Auth（用户名 flori）进入后，在前端 JS 中注入 API Token。

## 3. 任务创建流程

```python
@router.post("/api/jobs")
async def create_job(req: JobCreateRequest, redis: Redis, db: sqlite3.Connection):
    job_id = generate_job_id()
    job_dir = Path(f"/data/jobs/{job_id}")
    job_dir.mkdir(parents=True)

    # 写 job.json（Worker 读取）
    (job_dir / "job.json").write_text(json.dumps({
        "id": job_id, "url": req.url, "source": detect_source(req.url),
        "domain": req.domain, "created_at": now_iso(),
    }))

    # 写 DB
    db.execute("INSERT INTO jobs (id, url, domain, source, status, created_at, updated_at) VALUES (?, ?, ?, ?, 'pending', ?, ?)",
               (job_id, req.url, req.domain, detect_source(req.url), now_iso(), now_iso()))

    # 通知调度器
    await redis.publish("new_job", json.dumps({"job_id": job_id}))

    return {"job_id": job_id, "status": "pending", "created_at": now_iso()}
```

## 4. 文件服务

视频流式播放支持 HTTP Range（拖拽进度条）：

```python
@router.get("/api/jobs/{job_id}/video")
async def stream_video(job_id: str, request: Request):
    video_path = find_video(job_id)  # /data/jobs/{id}/input/*.mp4
    return FileResponse(video_path, media_type="video/mp4",
                       headers={"Accept-Ranges": "bytes"})
```

笔记中的截图路径替换：前端请求 `/api/jobs/{id}/assets/frame-0012.jpg`，API 直接返回文件。

## 5. WebSocket 进度推送

```python
@router.websocket("/api/ws/jobs/{job_id}")
async def ws_job(ws: WebSocket, job_id: str, redis: Redis):
    await ws.accept()
    pubsub = redis.pubsub()
    await pubsub.subscribe(f"events:{job_id}")
    try:
        async for msg in pubsub.listen():
            if msg["type"] == "message":
                await ws.send_text(msg["data"])
    except WebSocketDisconnect:
        pass
    finally:
        await pubsub.unsubscribe()
```

## 6. 平台扫码登录

B站支持扫码登录获取 cookies（用于 1080P 下载）。流程（路由见 `api/routes/bili.py`）：

1. `POST /api/bili/login/start` → 调 passport 生成二维码 → 返回 `qrcode_key` + 渲染好的 PNG data URI（`qr_png`）
2. 前端直接把 `qr_png` 当 `img src` 渲染，用户用 B站 App 扫码
3. `GET /api/bili/login/poll?qrcode_key={key}` → 轮询扫码状态（`waiting`/`scanned`/`confirmed`/`expired`）
4. `confirmed` 时从 Set-Cookie 取 SESSDATA/bili_jct/DedeUserID 入库（`app_credentials` 表，非文件）
5. `GET /api/bili/status` 查登录态、`POST /api/bili/logout` 清除 cookie

下载步凭证由中心分发:worker 认领 01_download 时经 transport 领取(redis 镜像/runner API,docs/03 §1.7.1),job 侧载凭证仅同机场景。

## 7. 限流

```python
from slowapi import Limiter
limiter = Limiter(key_func=lambda: "global")

@router.post("/api/jobs")
@limiter.limit("10/minute")
async def create_job(...):
    ...
```

## 8. Dockerfile

```dockerfile
FROM python:3.11-slim
RUN pip install --no-cache-dir \
    fastapi uvicorn[standard] redis aiofiles httpx slowapi python-multipart
WORKDIR /app
COPY api/ .
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
```
