# 10 · 可观测

> 进度系统、结构化日志、健康检查、卡住检测。

## 1. 进度系统

### 三层进度

```
Job 整体进度 (0-100%)
  └── N 个步骤（由 pipeline 定义），各有状态 (waiting/ready/running/done/failed/skipped)
        └── 步骤内细粒度 (current/total, 如 "85/162 帧")
```

### Job 整体进度计算

步骤权重在 `pipelines.yaml` 中按 pipeline 定义（不同内容类型权重不同）：

```python
def calc_progress(steps: list[dict]) -> int:
    # steps 中每项包含从 pipelines.yaml 读取的 weight
    done_weight = sum(s["weight"] for s in steps if s["status"] in ("done", "skipped"))
    total_weight = sum(s["weight"] for s in steps)
    return round(100 * done_weight / max(total_weight, 1))
```

### 步骤内进度

StepBase.report_progress() 写 `.{step}.progress` 文件。Worker 轮询此文件，通过 Redis publish 转发给 WebSocket。

## 2. 结构化日志

```python
import structlog

logger = structlog.get_logger()

# 所有日志带 component/job_id/step/worker 字段
logger.info("step_started",
    component="worker", job_id="j_abc", step="03_scene", worker="cpu-01")

# 输出 JSON
# {"event": "step_started", "component": "worker",
#  "job_id": "j_abc", "step": "03_scene", "worker": "cpu-01",
#  "timestamp": "2026-05-16T20:00:00"}
```

### 日志存储

```
/data/logs/
├── scheduler.log
├── api.log
└── workers/
    ├── cpu-a1b2.log
    └── ai-c3d4.log

/data/jobs/{id}/logs/       # 每步的执行日志
├── 03_scene.log
└── 10_smart.log
```

## 3. 健康检查

```
GET /api/health

{
  "status": "healthy" | "degraded" | "unhealthy",
  "checks": {
    "redis": "ok",
    "db": "ok",
    "disk_free_gb": 600.0,
    "workers_online": 4,
    "workers_by_type": {"download": 1, "cpu": 1, "ai": 2, "gpu": 0}
  }
}
```

降级条件：
- `degraded`：某类型 Worker 为 0，但其他正常
- `unhealthy`：Redis 不通 或 磁盘 <5GB

`/api/health` 也被 compose 的 api healthcheck 与 watchtower 用作存活探针（探的是 `/openapi.json`，始终免鉴权）。

### Prometheus 指标 — `GET /api/metrics`

免鉴权（同 `/health`），返回 Prometheus 文本曝露格式，供外部 Prometheus 抓取（个人工具不内置时序库）：

```
mnemo_up 1
mnemo_redis_up 1
mnemo_db_up 1
mnemo_disk_free_gb 600.0
mnemo_workers_total 4
mnemo_workers_online 4
mnemo_jobs{status="done"} 60
mnemo_jobs{status="processing"} 2
```

只暴露计数/容量，无敏感信息。阈值告警在 Prometheus/Alertmanager 侧配置（如 `mnemo_disk_free_gb < 10`）。

## 4. 卡住检测（两层）

### 第一层：Worker 消失 → 自动回收（调度器 orphan_scan）

Worker 心跳 30s 过期 → 调度器检测到 running 步骤的 Worker 不存在 → 释放资源槽 → 触发重试。

详见 [scheduler.md §8 孤儿步骤回收](04-module-design/scheduler.md)。

### 第二层：进度停滞 → 告警（可能真卡住）

Worker 还活着（心跳正常），但进度文件长时间没更新。

因为 Worker 心跳进度每 10 秒写一次 `.progress` 文件，所以**任何步骤**的进度文件都会持续更新。如果超过 60 秒没更新，说明 Worker 进程本身有问题（死锁、OOM 等）：

```python
async def check_stuck():
    for key in await redis.keys("job:*:steps"):
        job_id = key.split(":")[1]
        steps = await redis.hgetall(key)
        for step, status in steps.items():
            if status != "running":
                continue

            progress_file = Path(f"/data/jobs/{job_id}/.{step}.progress")
            if not progress_file.exists():
                continue  # 刚开始，还没写第一次心跳

            data = json.loads(progress_file.read_text())
            age = time.time() - data["updated_at"]

            if age > 60:
                # Worker 心跳进度 10s 一次，60s 没更新 = Worker 进程异常
                logger.warning("step_stuck", job_id=job_id, step=step, age_sec=age)
                await asyncio.to_thread(notify, "step_stuck", ...)  # 主动告警(见下)
                await redis.publish("step_failed", json.dumps({
                    "job_id": job_id, "step": step,
                    "status": "failed",
                    "error": f"progress stale ({age:.0f}s, worker process may be stuck)"
                }))
```

### 主动告警 — `ALERT_WEBHOOK_URL`

`shared/notify.notify(event, message, **fields)` 是轻量告警钩子:设了 `ALERT_WEBHOOK_URL`（Slack/Discord/通用 webhook，payload 同时带 `text`/`content` 字段）就把关键事件 POST 出去，否则只 `structlog`。best-effort（超时 5s、吞所有异常），绝不反过来拖垮主流程；异步上下文用 `await asyncio.to_thread(notify, ...)`。当前接入点：调度器第二层卡死检测（`step_stuck`）。磁盘/容量类阈值告警走 Prometheus 抓 `/api/metrics`（§3）。

### 两层检测对照

| 场景 | 第一层（orphan_scan） | 第二层（progress stale） |
|------|---------------------|------------------------|
| Worker 崩溃/断网 | 30s 后心跳过期 → 回收 | 不会触发（orphan_scan 先处理） |
| Worker 进程死锁 | 心跳线程还在续期 → 不触发 | 60s 进度停更 → 告警+重试 |
| 步骤正常但慢（Whisper 30min） | 心跳正常 → 不触发 | 心跳进度每 10s 更新 → 不触发 ✓ |
| subprocess 卡住（如 ffmpeg hang） | 心跳正常 → 不触发 | Worker 心跳循环和 subprocess 独立 → 心跳仍更新 → **不触发** |

最后一种情况（subprocess 卡住但 Worker 心跳正常）靠 **subprocess timeout**（pipelines.yaml 的 `timeout_sec`）兜底——Worker 层面 kill 子进程。

### 前端展示

```
正常运行（有细粒度进度）:
  04 OCR  ████████░░░░ 52%  85/162 帧

正常运行（无细粒度进度，靠 Worker 心跳）:
  08 智能笔记  ⏳ 运行中 3m0s

疑似卡住（进度长时间不动）:
  01 场景检测  ⚠️ 可能卡住 (5m12s 无进度更新)
```

## 5. 系统状态面板

GET /api/status 返回全局概览，前端 Settings 页展示：

- Worker 在线状态（绿/红灯）
- 各池队列长度
- 任务统计（处理中/待处理/完成/失败）
- 磁盘使用
