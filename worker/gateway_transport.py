"""GatewayTransport:把 register/heartbeat/update_status 换成出站 HTTPS,其余委派内层。

有内层(RedisTransport)时:worker 仍直连 redis/db,认领走内层,注册/心跳额外打 gateway。
无内层(inner=None)时:不连 redis/db,只出站 HTTPS;认领/产物全走 gateway,
无内层可退回——不可达时只 log,不崩。内层委派方法在 inner 为空时返回安全默认值。
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx
import structlog

from worker.transport import RedisTransport

logger = structlog.get_logger(component="gateway_transport")


class GatewayTransport:
    """包裹可选内层 RedisTransport:生命周期方法走 gateway,其余委派或返回默认值。"""

    def __init__(
        self,
        base_url: str,
        *,
        registration_token: str,
        id_file: str,
        inner: Optional[RedisTransport] = None,
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
            from shared.net import gateway_tls_verify

            self._client = httpx.AsyncClient(
                base_url=self._base_url, timeout=35, verify=gateway_tls_verify(),
            )
        return self._client

    @property
    def worker_token(self) -> str:
        # 供 GatewayStorage 经 token_getter 读取 register 拿到的 per-worker token。
        return self._worker_token

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
        # 有内层时镜像写一份到 redis/db(认领仍走内层);无内层则跳过。
        if self._inner is not None:
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
        # 有内层才退回维持 redis/db 新鲜;纯网关无内层,gateway 已是唯一通路。
        if self._inner is not None:
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
        if self._inner is not None:
            await self._inner.update_status(
                worker_id, status, current_job, current_step,
            )

    # ── 粗粒度认领/上报:走 gateway HTTP,不委派内层,避免经 redis 双重认领 ──

    def _auth(self) -> dict:
        return {"Authorization": f"Bearer {self._worker_token}"}

    async def request_step(self, worker_id, pools, pool_limits, tags, reject_tags):
        # 认领走服务端长轮询;httpx 出错只 log+返回 None(worker 空转重试),绝不退回内层
        # ——退回内层会经 redis 再认领一次,造成双重认领。
        try:
            resp = await self._http.post(
                "/api/runner/jobs/request",
                headers=self._auth(),
                json={
                    "pools": pools, "pool_limits": pool_limits,
                    "tags": sorted(tags), "reject_tags": sorted(reject_tags),
                },
            )
            resp.raise_for_status()
            return resp.json().get("claim")
        except httpx.HTTPError:
            logger.warning("gateway_request_step_failed", worker_id=worker_id)
            return None

    async def _report_best_effort(self, url, json_body, *, op,
                                  job_id="", step=""):
        """上报通道(complete/fail/release/usage)统一 best-effort:有界重试后仍失败只 log,
        绝不抛——上报抖动不得把 returncode==0 的成功步骤翻成 failed,也不得经 execute 的
        finally release 逃逸杀掉整个 worker 主循环(审计 I-H2/I-H3)。对照同文件 heartbeat/
        request_step/report_step_alive 同为 best-effort,唯独这四个上报方法此前裸 raise。"""
        last_exc = None
        for attempt in range(3):
            try:
                resp = await self._http.post(url, headers=self._auth(), json=json_body)
                resp.raise_for_status()
                return
            except httpx.HTTPError as e:
                last_exc = e
                if attempt < 2:
                    await asyncio.sleep(0.5 * (attempt + 1))
        logger.warning(
            f"gateway_{op}_failed", job_id=job_id, step=step,
            error=str(last_exc)[:200],
        )

    async def report_done(self, claim, duration, started_at):
        job_id, step = claim["job_id"], claim["step"]
        await self._report_best_effort(
            f"/api/runner/jobs/{job_id}/steps/{step}/complete",
            {
                "pool": claim["pool"], "exec_id": claim["exec_id"],
                "duration": duration, "started_at": started_at,
            },
            op="report_done", job_id=job_id, step=step,
        )

    async def report_failed(self, claim, error, error_type, duration,
                            started_at, count_stats):
        job_id, step = claim["job_id"], claim["step"]
        await self._report_best_effort(
            f"/api/runner/jobs/{job_id}/steps/{step}/fail",
            {
                "pool": claim["pool"], "exec_id": claim["exec_id"],
                "error": error, "error_type": error_type,
                "duration": duration, "started_at": started_at,
                "count_stats": count_stats,
            },
            op="report_failed", job_id=job_id, step=step,
        )

    async def release(self, claim):
        job_id, step = claim["job_id"], claim["step"]
        await self._report_best_effort(
            f"/api/runner/jobs/{job_id}/steps/{step}/release",
            {"pool": claim["pool"], "exec_id": claim["exec_id"]},
            op="release", job_id=job_id, step=step,
        )

    async def record_ai_usage(self, usage):
        # usage 是 AIUsage 数据类;created_at 由服务端补默认,这里只发可序列化字段。
        await self._report_best_effort(
            "/api/runner/usage",
            {
                "exec_id": usage.exec_id, "provider": usage.provider,
                "model": usage.model, "job_id": usage.job_id, "step": usage.step,
                "input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens,
                "cost_usd": usage.cost_usd, "duration_sec": usage.duration_sec,
                "cached": usage.cached,
            },
            op="record_ai_usage", job_id=usage.job_id, step=usage.step,
        )

    async def publish_step_event(self, channel, data):
        # worker 只通过 on_progress 发 events:{job} 进度;映射到 progress 端点。
        # 非 events 频道(step_started/completed/failed)现由服务端发,worker 不再走这里。
        if channel.startswith("events:"):
            job_id = channel.split(":", 1)[1]
            try:
                resp = await self._http.post(
                    f"/api/runner/jobs/{job_id}/steps/_/progress",
                    headers=self._auth(),
                    json={"payload": data},
                )
                resp.raise_for_status()
            except httpx.HTTPError:
                logger.warning("gateway_progress_failed", job_id=job_id)

    async def report_step_alive(self, job_id, step):
        # 步进度心跳走 gateway(best-effort,失败只 log,绝不影响步骤执行)。
        try:
            resp = await self._http.post(
                f"/api/runner/jobs/{job_id}/steps/{step}/alive",
                headers=self._auth(),
            )
            resp.raise_for_status()
        except httpx.HTTPError:
            logger.warning("gateway_step_alive_failed", job_id=job_id, step=step)

    # ── 其余方法:有内层(混合模式)则委派,无内层(纯网关)返回安全默认值 ──
    # gateway 模式 worker 不调这些细粒度方法(claim 已在服务端 enrich),
    # 此处仅作防御:纯网关无内层时绝不抛 AttributeError。

    async def get_worker_status(self, worker_id):
        if self._inner is None:
            return None
        return await self._inner.get_worker_status(worker_id)

    async def is_pool_frozen(self, pool):
        if self._inner is None:
            return False
        return await self._inner.is_pool_frozen(pool)

    async def try_acquire_slot(self, pool, limit):
        if self._inner is None:
            return True
        return await self._inner.try_acquire_slot(pool, limit)

    async def release_slot(self, pool):
        if self._inner is not None:
            await self._inner.release_slot(pool)

    async def freeze_pool(self, pool):
        if self._inner is not None:
            await self._inner.freeze_pool(pool)

    async def unfreeze_pool(self, pool):
        if self._inner is not None:
            await self._inner.unfreeze_pool(pool)

    async def dequeue_step_raw(self, pool):
        if self._inner is None:
            return None
        return await self._inner.dequeue_step_raw(pool)

    async def return_step(self, pool, raw_json, score):
        if self._inner is not None:
            await self._inner.return_step(pool, raw_json, score)

    async def cas_step_status(self, job_id, step, expected, new):
        if self._inner is None:
            return True
        return await self._inner.cas_step_status(job_id, step, expected, new)

    async def set_step_worker(self, job_id, step, worker_id):
        if self._inner is not None:
            await self._inner.set_step_worker(job_id, step, worker_id)

    async def update_step_result(self, job_id, step, *, status, worker_id,
                                 started_at, finished_at, duration_sec,
                                 error=None):
        if self._inner is not None:
            await self._inner.update_step_result(
                job_id, step, status=status, worker_id=worker_id,
                started_at=started_at, finished_at=finished_at,
                duration_sec=duration_sec, error=error,
            )

    async def increment_worker_stats(self, worker_id, *, completed=0,
                                     failed=0, duration=0.0):
        if self._inner is not None:
            await self._inner.increment_worker_stats(
                worker_id, completed=completed, failed=failed, duration=duration,
            )

    async def get_job_pipeline(self, job_id):
        if self._inner is None:
            return None
        return await self._inner.get_job_pipeline(job_id)

    async def get_job_info(self, job_id):
        if self._inner is None:
            return {}
        return await self._inner.get_job_info(job_id)

    async def close(self):
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        if self._inner is not None:
            await self._inner.close()
