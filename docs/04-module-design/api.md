# API 服务

> FastAPI 应用。职责：接收用户请求、管理任务、服务文件、推送进度。
> 不执行分析步骤，不直接操作 Redis 队列（通过调度器）。

## 1. 模块结构

```
api/
├── main.py                 # FastAPI app + 启动
├── deps.py                 # 依赖注入（Redis/DB/Auth）
├── routes/
│   ├── jobs.py             # POST/GET/DELETE /jobs
│   ├── notes.py            # 笔记/截图/视频文件服务
│   ├── ws.py               # WebSocket 进度推送
│   ├── auth.py             # B站扫码 / YouTube cookies
│   ├── admin.py            # 系统状态 / 配置热更新
│   └── collections.py      # 集合管理 (M2)
└── Dockerfile
```

## 2. 认证

单层 Bearer Token（个人工具，Basic Auth 在 Cloudflare Access 或 Caddy 层做）：

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

前端通过 Cloudflare Access（邮箱验证或密码）进入后，在前端 JS 中注入 API Token。

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

笔记中的截图路径替换：前端请求 `/api/jobs/{id}/assets/scene_0012_63.5s.jpg`，API 直接返回文件。

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

B站支持扫码登录获取 cookies（用于 1080P 下载）。流程：

1. `POST /api/auth/bilibili/qrcode` → 调用 B站公开扫码 API → 返回二维码 URL + key
2. 前端渲染二维码，用户用 B站 App 扫码
3. `GET /api/auth/bilibili/poll?key={key}` → 轮询扫码状态
4. 扫码成功 → 保存 cookies 到 `/data/cookies/bilibili.txt`

具体 API endpoint 参考 B站开放文档或 yutto/bilibili-api 等开源项目的实现。

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
