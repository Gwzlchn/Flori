"""GatewayTransport:把 register/heartbeat/update_status 换成出站 HTTPS,其余委派内层。

P1 影子模式:worker 仍直连 redis/db(内层 RedisTransport),认领/产物保持直连;
只有注册/心跳/下线额外打到 gateway,验证出站接入链路。gateway 不可达时退回内层,不崩。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import httpx
import structlog

from worker.transport import RedisTransport

logger = structlog.get_logger(component="gateway_transport")


class GatewayTransport:
    """包裹内层 RedisTransport:生命周期方法走 gateway,其余原样委派。"""

    def __init__(
        self,
        base_url: str,
        *,
        registration_token: str,
        id_file: str,
        inner: RedisTransport,
    ):
        self._base_url = base_url.rstrip("/")
        self._registration_token = registration_token
        self._id_file = Path(id_file)
        self._inner = inner
        self._worker_token = ""
        self._client: httpx.AsyncClient | None = None
        # 心跳要带 worker_id + 当前状态;状态由 update_status 记下,避免心跳把 busy 覆成 idle。
        self._status = "idle"
        self._current_job = ""
        self._current_step = ""

    @property
    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=self._base_url, timeout=35)
        return self._client

    def _load_cached_id(self) -> str | None:
        try:
            cached = self._id_file.read_text().strip()
            return cached or None
        except OSError:
            return None

    def _save_id(self, worker_id: str) -> None:
        try:
            self._id_file.parent.mkdir(parents=True, exist_ok=True)
            self._id_file.write_text(worker_id)
        except OSError:
            logger.warning("worker_id_persist_failed", worker_id=worker_id)

    # ── 生命周期 / 心跳(走 gateway) ──

    async def register(self, worker_id, worker_type, pools, tags,
                       reject_tags, hostname, now):
        # 缓存的 id 优先,让 worker 重启后复用同一身份(注册幂等)。
        effective_id = self._load_cached_id() or worker_id
        body = {
            "worker_id": effective_id,
            "type": worker_type,
            "pools": pools,
            "tags": sorted(tags),
            "reject_tags": sorted(reject_tags),
            "hostname": hostname,
        }
        resp = await self._http.post(
            "/api/runner/register", json=body,
            headers={"Authorization": f"Bearer {self._registration_token}"},
        )
        resp.raise_for_status()
        data = resp.json()
        self._worker_token = data.get("worker_token", "")
        returned_id = data.get("worker_id") or effective_id
        self._save_id(returned_id)
        # 影子写:让 redis/db 也有这行,认领仍走直连。
        await self._inner.register(
            returned_id, worker_type, pools, tags, reject_tags, hostname, now,
        )
        return returned_id

    async def heartbeat(self, worker_id):
        try:
            resp = await self._http.post(
                "/api/runner/heartbeat",
                headers={"Authorization": f"Bearer {self._worker_token}"},
                json={
                    "worker_id": worker_id, "status": self._status,
                    "current_job": self._current_job,
                    "current_step": self._current_step,
                },
            )
            if resp.status_code == 401:
                logger.warning("worker_token_revoked", worker_id=worker_id)
        except httpx.HTTPError:
            logger.warning("gateway_heartbeat_failed", worker_id=worker_id)
        # 影子模式:无论 gateway 结果如何,内层心跳维持 redis/db 新鲜。
        await self._inner.heartbeat(worker_id)

    async def update_status(self, worker_id, status,
                            current_job="", current_step=""):
        # 记下当前状态供心跳上报(gateway 心跳据此写 DB,不会把 busy 覆成 idle)。
        self._status = status
        self._current_job = current_job
        self._current_step = current_step
        if status == "offline":
            try:
                resp = await self._http.post(
                    "/api/runner/offline",
                    headers={"Authorization": f"Bearer {self._worker_token}"},
                    json={"worker_id": worker_id},
                )
                resp.raise_for_status()
            except httpx.HTTPError:
                logger.warning("gateway_offline_failed", worker_id=worker_id)
        await self._inner.update_status(
            worker_id, status, current_job, current_step,
        )

    # ── 其余方法:原样委派内层 ──

    async def get_worker_status(self, worker_id):
        return await self._inner.get_worker_status(worker_id)

    async def is_pool_frozen(self, pool):
        return await self._inner.is_pool_frozen(pool)

    async def try_acquire_slot(self, pool, limit):
        return await self._inner.try_acquire_slot(pool, limit)

    async def release_slot(self, pool):
        await self._inner.release_slot(pool)

    async def freeze_pool(self, pool):
        await self._inner.freeze_pool(pool)

    async def unfreeze_pool(self, pool):
        await self._inner.unfreeze_pool(pool)

    async def dequeue_step_raw(self, pool):
        return await self._inner.dequeue_step_raw(pool)

    async def return_step(self, pool, raw_json, score):
        await self._inner.return_step(pool, raw_json, score)

    async def cas_step_status(self, job_id, step, expected, new):
        return await self._inner.cas_step_status(job_id, step, expected, new)

    async def set_step_worker(self, job_id, step, worker_id):
        await self._inner.set_step_worker(job_id, step, worker_id)

    async def update_step_result(self, job_id, step, *, status, worker_id,
                                 started_at, finished_at, duration_sec,
                                 error=None):
        await self._inner.update_step_result(
            job_id, step, status=status, worker_id=worker_id,
            started_at=started_at, finished_at=finished_at,
            duration_sec=duration_sec, error=error,
        )

    async def increment_worker_stats(self, worker_id, *, completed=0,
                                     failed=0, duration=0.0):
        await self._inner.increment_worker_stats(
            worker_id, completed=completed, failed=failed, duration=duration,
        )

    async def record_ai_usage(self, usage):
        await self._inner.record_ai_usage(usage)

    async def get_job_pipeline(self, job_id):
        return await self._inner.get_job_pipeline(job_id)

    async def get_job_info(self, job_id):
        return await self._inner.get_job_info(job_id)

    async def publish_step_event(self, channel, data):
        await self._inner.publish_step_event(channel, data)

    async def close(self):
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        await self._inner.close()
