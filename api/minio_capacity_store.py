"""api 侧 MinIO 容量缓存:后台定时全量扫 bucket 求对象数+总字节,内存持有供 /api/status 读。

容量统计(对象数/总字节)MinIO 无聚合 API,只能全量 list 求和——贵。绝不能让它同步阻塞
/api/status(每 15s 轮询)。故仿 PricingStore.daily_loop:进程起一个后台循环,每 TTL 秒刷一次,
build_full_status 只读内存快照(无则 minio extra 不填 objects/size_bytes,前端显 —)。
仅生产(_own_resources)且 RemoteStorage 才起;LocalStorage 用 os.walk(便宜)同样支持。
"""

from __future__ import annotations

import asyncio

import structlog

_log = structlog.get_logger(component="minio-capacity")

_REFRESH_SEC = 600   # 10 分钟刷一次(容量慢变量,无需实时)


class MinioCapacityStore:
    def __init__(self) -> None:
        self._cap: dict | None = None   # {"objects": int, "bytes": int};未采集=None

    @property
    def value(self) -> dict | None:
        return self._cap

    async def refresh(self, storage) -> bool:
        """扫一次容量 → 更新内存。失败保留旧值(不致 /api/status 抖掉容量行)。"""
        try:
            cap = await storage.capacity()
        except Exception as e:  # noqa: BLE001
            _log.warning("capacity_scan_failed", error=str(e)[:200])
            return False
        if isinstance(cap, dict):
            self._cap = cap
            _log.info("capacity_refreshed", objects=cap.get("objects"), bytes=cap.get("bytes"))
            return True
        return False

    async def loop(self, storage) -> None:
        """启动先扫一次(warm),此后每 TTL 刷新。"""
        while True:
            try:
                await self.refresh(storage)
            except asyncio.CancelledError:
                raise
            except Exception:
                _log.exception("capacity_loop_error")
            await asyncio.sleep(_REFRESH_SEC)
