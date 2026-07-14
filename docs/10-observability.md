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

步骤通过 `self.progress.report()` 写 `.{step}.progress` 文件。Worker 轮询此文件，通过 Redis publish 转发给 WebSocket。

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

### 存储、轮转与保留

API、scheduler、Worker、Redis、MinIO、Caddy、Watchtower、tunnel 和 Dozzle 都只写容器 stdout/stderr，不在 `/data/logs` 复制第二份。生产/NAS、边缘和远程 Worker 统一使用 Docker `local` logging driver，单文件 `10m`、保留 `3` 份并压缩，单容器硬预算约 30MB；tunnel 为 `5m × 3`，开发栈为 `5m × 2`。配置覆盖根 Compose、`deploy/edge`、`deploy/tunnel` 和边缘 Worker，新增常驻服务必须复用对应 `x-logging` 锚点。`DockerStepRunner` 创建的短生命周期步骤容器不经 Compose，也在 SDK `containers.run` 中显式设置相同的 `local` 驱动和轮转参数。

步骤子进程/步骤容器的业务日志保留在 `/data/jobs/{id}/logs/{step}.log`。每个文件默认上限 10MiB（`FLORI_STEP_LOG_MAX_BYTES` 可覆盖）；写入前先判断是否越界，超大 chunk 不会先落到真实路径形成瞬时超限，而是把旧尾与新 chunk 直接写入低水位临时文件，再用 `os.replace` 原子切换。低水位给后续小写入留出余量，避免每个 chunk 都旋转；不会留下被删除但仍由进程持有的 inode。任务删除时其日志随任务产物一起清理。

保留语义分三类：

- 普通运行日志和结构化 `audit` 事件进入 Docker 压缩环形保留，供 `docker logs`/Dozzle 查询；需要跨轮转长期审计时，应接 Docker 日志采集器归档，不能靠扩大本机无界文件。
- AI 计费事实写 SQLite `ai_usage`，不依赖容器日志保留；AI 调用取证随 job 写 `output/ai_logs/*.jsonl`，由备份/恢复策略覆盖。
- 步骤排障日志按单步 10MiB 上限保留尾部，API 默认再只返回最后 256KiB。

可用 `docker inspect -f '{{json .HostConfig.LogConfig}}' <container>` 检查运行容器是否采用预期上限；Compose 不变量测试负责阻止常驻服务漏配。

## 3. 存活与接单健康

健康模型分成两层，不能混用：

- `GET /api/health/live`：liveness，只证明 API 进程可响应。依赖故障仍为 `200`，Compose 用它决定容器进程健康，避免 Redis 等外部故障触发 API 重启风暴。
- `GET /api/health/ready`：readiness，证明系统能安全接收新任务。阻断项存在时返回 `503`；`GET /api/health` 返回相同响应体但兼容旧监控始终 `200`。

readiness 检查 Redis、SQLite WAL 写事务回滚、数据目录真实写入、磁盘剩余 GB 与百分比、中心存储 put/delete canary、scheduler 心跳和 Worker pool。SQLite/MinIO 探针采用短 TTL singleflight；缓存过期后不回陈旧绿灯，超时或异常 fail-closed。MinIO 专用 SDK 客户端同时限制 connect/read 并关闭重试，黑洞网络不会按探针周期持续累积阻塞线程。阈值、探针 TTL/timeout 与 pool 角色的单一来源是 `configs/pools.yaml::readiness`：`io/cpu/ai` 为必要能力，任一全离线或全部暂停即 `not_ready`；`gpu` 为可选能力，离线只返回 `degraded`，不会封锁无关流水线。Worker 判活复用 `/api/workers` 的 SQLite 累计资料 + Redis 实时心跳合并结果，按每个 Worker 声明的多 pool 汇总，不再以陈旧 DB 心跳误判远端 Worker。

```
GET /api/health/ready

{
  "status": "ready" | "degraded" | "not_ready",
  "ready": true | false,
  "checks": {
    "redis": {"status": "ok", "required": true},
    "db": {"status": "ok", "required": true, "journal_mode": "wal"},
    "workers": {"status": "ok", "required": true, "online": 3, "paused": 0},
    "disk": {"status": "ok", "required": true, "free_gb": 600.0},
    "pool:gpu": {"status": "degraded", "required": false, "online": 0}
  },
  "reasons": [{"code": "pool:gpu", "severity": "degraded", "message": "...", "recovery": "..."}]
}
```

`GET /api/status` 的 `health` 字段复用同一模型，SystemView 直接渲染后端给出的阻断原因和恢复建议，不在浏览器重新推断阈值。状态请求失败时前端会清除旧健康快照并显示获取失败，不能让失联前的绿色状态继续冒充当前健康；原因超过四条时同时显示剩余数量。

### Prometheus 指标 — `GET /api/metrics`

免鉴权（同 `/health`），返回 Prometheus 文本曝露格式，供外部 Prometheus 抓取（个人工具不内置时序库）：

```
flori_up 1
flori_ready 1
flori_degraded 0
flori_redis_up 1
flori_db_up 1
flori_disk_free_gb 600.0
flori_workers_total 4
flori_workers_online 4
flori_jobs{status="done"} 60
flori_jobs{status="processing"} 2
```

`flori_up` 对应 liveness；`flori_ready`/`flori_degraded` 直接来自统一 readiness 模型。只暴露计数/容量，无敏感信息。阈值告警在 Prometheus/Alertmanager 侧配置（如 `flori_ready == 0` 或 `flori_disk_free_gb < 10`）。

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
