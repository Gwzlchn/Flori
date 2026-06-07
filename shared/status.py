"""Worker 状态判定（后端权威）。

公共状态由心跳新鲜度 + 是否有在跑任务 + 管理员叠加位算出，前端只渲染，不再
用时间戳自算（容器跑 UTC、浏览器 UTC+8 会把刚心跳的 worker 算成离线）。
"""

from __future__ import annotations

from datetime import datetime, timezone

# 与 configs/pools.yaml 的 worker_status 段保持一致；配置缺失时的兜底。
DEFAULT_ONLINE_WINDOW_SEC = 30
DEFAULT_STALE_WINDOW_SEC = 900

ONLINE_IDLE = "online-idle"
ONLINE_BUSY = "online-busy"
OFFLINE = "offline"
STALE = "stale"
DRAINING = "draining"


def compute_worker_status(
    last_heartbeat: datetime | None,
    current_job: str | None,
    admin_status: str | None,
    now: datetime | None = None,
    online_window_sec: int = DEFAULT_ONLINE_WINDOW_SEC,
    stale_window_sec: int = DEFAULT_STALE_WINDOW_SEC,
) -> str:
    """把存量字段折算成对外公共状态。

    判定优先级：draining(管理员叠加，仍在线才生效) → offline → stale → online-busy → online-idle。
    心跳缺失或超过 online_window 即视作不在线；超过 stale_window 进一步判 stale（GC 信号）。
    """
    if now is None:
        now = datetime.now(timezone.utc)

    age = None
    if last_heartbeat is not None:
        if last_heartbeat.tzinfo is None:
            last_heartbeat = last_heartbeat.replace(tzinfo=timezone.utc)
        age = (now - last_heartbeat).total_seconds()

    online = age is not None and age <= online_window_sec

    if admin_status == DRAINING:
        # 管理员置位 draining：仍在线显示 draining；已离线则按离线/失联归类。
        if online:
            return DRAINING

    if not online:
        if age is None or age > stale_window_sec:
            return STALE
        return OFFLINE

    if current_job:
        return ONLINE_BUSY
    return ONLINE_IDLE
