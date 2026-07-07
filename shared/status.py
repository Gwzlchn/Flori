"""Worker 状态判定(后端权威)。

公共状态由心跳新鲜度 + 是否有在跑任务 + 管理员叠加位算出,前端只渲染,不用
时间戳自算(容器跑 UTC、浏览器 UTC+8 会把刚心跳的 worker 算成离线)。
"""

from __future__ import annotations

from datetime import datetime, timezone

# 与 configs/pools.yaml 的 worker_status 段保持一致;配置缺失时的兜底。
DEFAULT_ONLINE_WINDOW_SEC = 30
DEFAULT_STALE_WINDOW_SEC = 900

ONLINE_IDLE = "online-idle"
ONLINE_BUSY = "online-busy"
OFFLINE = "offline"
STALE = "stale"
PAUSED = "paused"


def compute_worker_status(
    last_heartbeat: datetime | None,
    current_job: str | None,
    admin_status: str | None,
    now: datetime | None = None,
    online_window_sec: int = DEFAULT_ONLINE_WINDOW_SEC,
    stale_window_sec: int = DEFAULT_STALE_WINDOW_SEC,
) -> str:
    """把存量字段折算成对外公共状态。

    判定优先级从高到低:paused(管理员叠加,仍在线才生效)、offline、stale、online-busy、online-idle。
    心跳缺失或超过 online_window 即视作不在线;超过 stale_window 进一步判 stale(GC 信号)。
    """
    if now is None:
        now = datetime.now(timezone.utc)

    age = None
    if last_heartbeat is not None:
        if last_heartbeat.tzinfo is None:
            last_heartbeat = last_heartbeat.replace(tzinfo=timezone.utc)
        age = (now - last_heartbeat).total_seconds()

    online = age is not None and age <= online_window_sec

    if admin_status == PAUSED:
        # 管理员置位 paused:仍在线显示 paused;已离线则按离线/失联归类。
        if online:
            return PAUSED

    if not online:
        if age is None or age > stale_window_sec:
            return STALE
        return OFFLINE

    if current_job:
        return ONLINE_BUSY
    return ONLINE_IDLE


# 组件健康(api/scheduler/redis/minio)
# 组件用专用四态 up/degraded/down/unknown,与 worker 的 online-*/stale 分开:worker 的 stale
# 在前端固定映射红色"该 GC",与组件"心跳刚过期=黄"语义冲突。复用 worker 的
# 30/900 窗口(单一阈值源),仅心跳型组件(scheduler)用本函数。
COMPONENT_UP = "up"
COMPONENT_DEGRADED = "degraded"
COMPONENT_DOWN = "down"
COMPONENT_UNKNOWN = "unknown"


def compute_component_status(
    last_heartbeat: datetime | None,
    now: datetime | None = None,
    online_window_sec: int = DEFAULT_ONLINE_WINDOW_SEC,
    stale_window_sec: int = DEFAULT_STALE_WINDOW_SEC,
) -> str:
    """按心跳新鲜度把组件折算成四态(供 scheduler 心跳判活):
      - 无心跳记录(从未写过/老版本):unknown(非永久 degraded,避免误报"挂了").
      - age <= online_window:up.
      - online_window < age <= stale_window:degraded(错过几拍,缓冲带).
      - age > stale_window:down(TTL 过期/进程死).
    redis/minio 不走本函数(它们靠实时探活,而非心跳)。loop_lag 等附加降级条件由调用方叠加。
    """
    if now is None:
        now = datetime.now(timezone.utc)
    if last_heartbeat is None:
        return COMPONENT_UNKNOWN
    if last_heartbeat.tzinfo is None:
        last_heartbeat = last_heartbeat.replace(tzinfo=timezone.utc)
    age = (now - last_heartbeat).total_seconds()
    if age <= online_window_sec:
        return COMPONENT_UP
    if age <= stale_window_sec:
        return COMPONENT_DEGRADED
    return COMPONENT_DOWN
