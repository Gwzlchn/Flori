"""GatewayTransport:worker 经 Gateway HTTPS 注册、认领、上报和代理产物。

有内层(RedisTransport)时:生命周期可镜像写本地 redis/db,但认领和上报仍走 gateway,
避免双重认领。无内层(inner=None)时:不连 redis/db,只出站 HTTPS。
"""

from __future__ import annotations

import asyncio
import os
import socket
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx
import structlog

from shared.version import FLORI_VERSION
from shared.runner_ops import (
    TaskLease,
    bind_task_lease,
    clear_task_lease,
    current_task_lease,
)
from worker.transport import (
    RedisTransport,
    WorkerAuthRejected,
    WorkerConfigError,
    WorkerContractError,
)

logger = structlog.get_logger(component="gateway_transport")


class GatewayTransport:
    """包裹可选内层 RedisTransport:生命周期方法走 gateway,其余委派或返回默认值。"""

    def __init__(
        self,
        base_url: str,
        *,
        registration_token: str,
        id_file: str,
        token_file: str | None = None,
        inner: Optional[RedisTransport] = None,
    ):
        self._base_url = base_url.rstrip("/")
        self._registration_token = registration_token
        self._id_file = Path(id_file)
        self._token_file = Path(
            token_file
            or os.environ.get("WORKER_TOKEN_FILE", "").strip()
            or (self._id_file.parent / "worker.token")
        )
        self._inner = inner
        self._worker_token = ""
        # 本 worker 当前在跑的 (job_id, step) 集合:心跳捎带上报刷各步进度心跳(并发>1 必需)
        self._running: dict[str, tuple[str, str]] = {}
        # 每个 runner 请求带的身份头(register 后填);即使 401(token 无效)服务端也能据此记下"谁/什么版本"在刷。
        self._identity_headers: dict[str, str] = {}
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

    def _load_cached_token(self) -> str | None:
        try:
            cached = self._token_file.read_text().strip()
            return cached or None
        except OSError:
            return None

    def _save_id(self, worker_id: str) -> None:
        try:
            self._id_file.parent.mkdir(parents=True, exist_ok=True)
            self._id_file.write_text(worker_id)
        except OSError as e:
            raise WorkerConfigError(
                f"failed to persist worker id to {self._id_file}",
                reason="worker_id_persist_failed",
            ) from e

    def _save_worker_token(self, token: str) -> None:
        try:
            self._token_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._token_file.with_name(f".{self._token_file.name}.tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(token)
                f.write("\n")
            os.replace(tmp, self._token_file)
        except OSError as e:
            raise WorkerConfigError(
                f"failed to persist worker token to {self._token_file}",
                reason="worker_token_persist_failed",
            ) from e
        try:
            os.chmod(self._token_file, 0o600)
        except OSError:
            logger.warning("worker_token_chmod_failed", path=str(self._token_file))

    def _identity_body(self, worker_id, worker_type, pools, tags,
                       reject_tags, hostname, concurrency, spec) -> dict:
        return {
            "worker_id": worker_id,
            "type": worker_type,
            "pools": pools,
            "tags": sorted(tags),
            "reject_tags": sorted(reject_tags),
            "hostname": hostname,
            "concurrency": concurrency,
            "spec": spec or {},
        }

    def _set_identity_headers(self, worker_id: str, worker_type: str,
                              hostname: str) -> None:
        self._identity_headers = {
            "X-Worker-Id": worker_id,
            "X-Worker-Type": worker_type or "",
            "X-Worker-Host": hostname or "",
            "X-Worker-Version": FLORI_VERSION,
        }

    def _set_initial_config(self, data: dict) -> None:
        self.initial_config = {
            "desired_config": data.get("desired_config"),
            "cfg_rev": int(data.get("cfg_rev") or 0),
        }

    def _raise_auth_status(self, resp, endpoint: str) -> None:
        status = getattr(resp, "status_code", None)
        if status in (401, 403, 429):
            raise WorkerAuthRejected(status_code=status, endpoint=endpoint)

    def _json(self, resp, endpoint: str) -> dict:
        try:
            data = resp.json()
        except ValueError as e:
            raise WorkerContractError(
                "gateway response is not valid json",
                endpoint=endpoint,
            ) from e
        if not isinstance(data, dict):
            raise WorkerContractError(
                "gateway response json must be an object",
                endpoint=endpoint,
            )
        return data

    # 生命周期 / 心跳(走 gateway)

    async def register(self, worker_id, worker_type, pools, tags,
                       reject_tags, hostname, now, concurrency: int = 1,
                       spec: dict | None = None):
        # 有长期 worker token 时只能 resume。cached token 被拒绝说明被 revoke 或配置错,
        # 不允许回退 registration token 复活。
        effective_id = self._load_cached_id() or worker_id
        body = self._identity_body(
            effective_id, worker_type, pools, tags, reject_tags,
            hostname, concurrency, spec,
        )
        cached_token = self._load_cached_token()
        if cached_token:
            return await self._resume(
                effective_id, cached_token, body, worker_type, pools, tags,
                reject_tags, hostname, now, concurrency,
            )
        if not self._registration_token:
            raise WorkerConfigError(
                "WORKER_REGISTRATION_TOKEN is required for first gateway bootstrap",
                reason="missing_registration_token",
            )
        resp = await self._http.post(
            "/api/runner/register", json=body,
            headers={"Authorization": f"Bearer {self._registration_token}"},
        )
        self._raise_auth_status(resp, "/api/runner/register")
        resp.raise_for_status()
        data = self._json(resp, "/api/runner/register")
        token = data.get("worker_token")
        if not isinstance(token, str) or not token.startswith("flwt-"):
            raise WorkerContractError(
                "register response missing worker_token",
                endpoint="/api/runner/register",
            )
        self._worker_token = token
        self._set_initial_config(data)
        returned_id = data.get("worker_id") or effective_id
        self._save_id(returned_id)
        self._save_worker_token(token)
        self._set_identity_headers(returned_id, worker_type, hostname)
        # 有内层时镜像写一份到 redis/db(认领仍走内层);无内层则跳过。
        if self._inner is not None:
            await self._inner.register(
                returned_id, worker_type, pools, tags, reject_tags, hostname, now,
                concurrency,
            )
        return returned_id

    async def _resume(self, effective_id: str, cached_token: str, body: dict,
                      worker_type, pools, tags, reject_tags, hostname, now,
                      concurrency: int) -> str:
        self._worker_token = cached_token
        self._set_identity_headers(effective_id, worker_type, hostname)
        resp = await self._http.post(
            "/api/runner/resume", json=body, headers=self._auth(),
        )
        self._raise_auth_status(resp, "/api/runner/resume")
        resp.raise_for_status()
        data = self._json(resp, "/api/runner/resume")
        self._set_initial_config(data)
        returned_id = data.get("worker_id") or effective_id
        self._save_id(returned_id)
        self._set_identity_headers(returned_id, worker_type, hostname)
        if self._inner is not None:
            await self._inner.register(
                returned_id, worker_type, pools, tags, reject_tags, hostname, now,
                concurrency,
            )
        return returned_id

    async def heartbeat(self, worker_id, load=None, applied_cfg_rev=0,
                        concurrency: int | None = None):
        cfg_payload: dict | None = None
        try:
            body = {
                "worker_id": worker_id, "status": self._status,
                "current_job": self._current_job,
                "current_step": self._current_step,
                "applied_cfg_rev": applied_cfg_rev,
            }
            if concurrency is not None:
                body["concurrency"] = concurrency
            if load:
                body["load"] = load   # 本机 live 负载,经网关写 redis worker hash,供各节点负载展示
            if self._running:
                body["running"] = [
                    {"job_id": j, "step": st, "exec_id": ex}
                    for ex, (j, st) in sorted(self._running.items())
                ]
            resp = await self._http.post(
                "/api/runner/heartbeat", headers=self._auth(), json=body,
            )
            self._raise_auth_status(resp, "/api/runner/heartbeat")
            resp.raise_for_status()
            if resp.status_code == 200:
                try:
                    data = resp.json()
                    cfg_payload = {
                        "desired_config": data.get("desired_config"),
                        "cfg_rev": int(data.get("cfg_rev") or 0),
                    }
                except ValueError:
                    pass   # 旧网关无配置字段/响应异常:保持现配置,不算错
        except WorkerAuthRejected:
            raise
        except httpx.HTTPError as e:
            logger.warning(
                "gateway_heartbeat_failed", worker_id=worker_id,
                host=socket.gethostname(), endpoint="/api/runner/heartbeat",
                status=getattr(getattr(e, "response", None), "status_code", None),
                error=str(e)[:200],
            )
        # 有内层才退回维持 redis/db 新鲜;纯网关无内层,gateway 已是唯一通路。
        if self._inner is not None:
            await self._inner.heartbeat(worker_id, load=load, concurrency=concurrency)
        return cfg_payload

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
                    headers=self._auth(),
                    json={"worker_id": worker_id},
                )
                self._raise_auth_status(resp, "/api/runner/offline")
                resp.raise_for_status()
            except WorkerAuthRejected:
                raise
            except httpx.HTTPError:
                logger.warning("gateway_offline_failed", worker_id=worker_id)
        if self._inner is not None:
            await self._inner.update_status(
                worker_id, status, current_job, current_step,
            )

    # 粗粒度认领/上报:走 gateway HTTP,不委派内层,避免经 redis 双重认领。

    def _auth(self) -> dict:
        # per-worker token + 身份头(X-Worker-*);所有走 per-worker token 的 runner 请求统一用它。
        return {"Authorization": f"Bearer {self._worker_token}", **self._identity_headers}

    def _lease_auth(self, lease: TaskLease | None = None) -> dict:
        current = lease or current_task_lease()
        headers = self._auth()
        if current is not None:
            headers.update({
                "X-Flori-Lease-Job": current.job_id,
                "X-Flori-Lease-Step": current.step,
                "X-Flori-Lease-Exec": current.exec_id,
            })
        return headers

    async def request_step(self, worker_id, pools, pool_limits, tags, reject_tags):
        # 认领走服务端长轮询;httpx 出错只 log+返回 None(worker 空转重试),绝不退回内层
        # 退回内层会经 redis 再认领一次,造成双重认领。
        try:
            resp = await self._http.post(
                "/api/runner/jobs/request",
                headers=self._auth(),
                json={
                    "pools": pools, "pool_limits": pool_limits,
                    "tags": sorted(tags), "reject_tags": sorted(reject_tags),
                },
            )
            self._raise_auth_status(resp, "/api/runner/jobs/request")
            resp.raise_for_status()
            claim = resp.json().get("claim")
            if claim and claim.get("kind") != "ai":
                # 在跑步集合:心跳捎带上报(见 heartbeat body running),给每个并发步刷进度心跳。
                # 独立 alive 通道在部分外网链路上不达(实测 8 并发 worker alive 0 送达,
                # 步骤 150s 后被 orphan_scan 全量误回收);心跳是实测可靠通道,借道最稳。
                lease = TaskLease(
                    worker_id=worker_id,
                    job_id=claim["job_id"],
                    step=claim["step"],
                    exec_id=claim["exec_id"],
                )
                bind_task_lease(lease)
                self._running[claim["exec_id"]] = (claim["job_id"], claim["step"])
            return claim
        except WorkerAuthRejected:
            raise
        except httpx.HTTPError as e:
            logger.warning(
                "gateway_request_step_failed", worker_id=worker_id,
                host=socket.gethostname(), endpoint="/api/runner/jobs/request",
                status=getattr(getattr(e, "response", None), "status_code", None),
                error=str(e)[:200],
            )
            return None

    async def _report_best_effort(self, url, json_body, *, op,
                                  job_id="", step=""):
        """上报通道对网络/5xx 有界重试;401/403 是长期凭证失效,必须上抛停机。"""
        last_exc = None
        for attempt in range(3):
            try:
                resp = await self._http.post(url, headers=self._lease_auth(), json=json_body)
                self._raise_auth_status(resp, url)
                resp.raise_for_status()
                return
            except WorkerAuthRejected:
                raise
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
        try:
            await self._report_best_effort(
                f"/api/runner/jobs/{job_id}/steps/{step}/release",
                {"pool": claim["pool"], "exec_id": claim["exec_id"]},
                op="release", job_id=job_id, step=step,
            )
        finally:
            self._running.pop(claim["exec_id"], None)
            clear_task_lease()

    async def record_ai_usage(self, usage):
        # usage 是 AIUsage 数据类;created_at 由服务端补默认,这里只发可序列化字段。
        await self._report_best_effort(
            "/api/runner/usage",
            {
                "exec_id": usage.exec_id, "provider": usage.provider,
                "model": usage.model, "job_id": usage.job_id, "step": usage.step,
                "worker_id": usage.worker_id,
                "input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens,
                "cache_creation_input_tokens": usage.cache_creation_input_tokens,
                "cache_read_input_tokens": usage.cache_read_input_tokens,
                "cost_usd": usage.cost_usd, "duration_sec": usage.duration_sec,
                "num_turns": usage.num_turns, "cached": usage.cached,
            },
            op="record_ai_usage", job_id=usage.job_id, step=usage.step,
        )

    async def set_ai_result(self, task_id, result):
        # 独立 AI task 由 require_tags=['claude-cli'] 门控,只直连 ai-worker(RedisTransport)认领执行。
        # 网关模式(无 redis)不应跑 ai-task;真到这里说明路由错配,显式报错而非静默丢结果。
        raise NotImplementedError("AI task 不支持网关模式 worker(需 claude-cli 直连 worker)")

    async def record_ai_task_log(self, log):
        raise NotImplementedError("AI task 审计不支持网关模式 worker(需 claude-cli 直连 worker)")

    async def publish_step_event(self, channel, data):
        # worker 只通过 on_progress 发 events:{job} 进度;映射到 progress 端点。
        # 非 events 频道(step_started/completed/failed)由服务端发,worker 不走这里。
        if channel.startswith("events:"):
            job_id = channel.split(":", 1)[1]
            lease = current_task_lease()
            if lease is None or lease.job_id != job_id:
                raise WorkerContractError(
                    "progress requires current task lease",
                    endpoint=f"/api/runner/jobs/{job_id}/steps/*/progress",
                )
            try:
                resp = await self._http.post(
                    f"/api/runner/jobs/{job_id}/steps/{lease.step}/progress",
                    headers=self._lease_auth(lease),
                    json={"payload": data},
                )
                self._raise_auth_status(
                    resp, f"/api/runner/jobs/{job_id}/steps/{lease.step}/progress",
                )
                resp.raise_for_status()
            except WorkerAuthRejected:
                raise
            except httpx.HTTPError:
                logger.warning("gateway_progress_failed", job_id=job_id)

    async def report_step_alive(self, job_id, step):
        # 网络抖动仍 best-effort;auth 被拒说明 worker 已不可继续。
        try:
            resp = await self._http.post(
                f"/api/runner/jobs/{job_id}/steps/{step}/alive",
                headers=self._lease_auth(),
            )
            self._raise_auth_status(
                resp, f"/api/runner/jobs/{job_id}/steps/{step}/alive",
            )
            resp.raise_for_status()
        except WorkerAuthRejected:
            raise
        except httpx.HTTPError:
            logger.warning("gateway_step_alive_failed", job_id=job_id, step=step)

    # 其余方法:有内层(混合模式)则委派;无内层(纯网关)返回安全默认值。
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

    async def try_acquire_slot(self, pool, limit, holder):
        if self._inner is None:
            return True
        return await self._inner.try_acquire_slot(pool, limit, holder)

    async def release_slot(self, pool, holder):
        if self._inner is not None:
            await self._inner.release_slot(pool, holder)

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

    async def get_credential(self, key):
        # 凭证一律走 gateway(不委派内层):领取集中在服务端,审计事件才有单一记录点。
        # auth 失败停 worker;其余错误降级匿名(None),下载凭证缺失不应导致任务失败。
        try:
            resp = await self._http.get(
                f"/api/runner/credentials/{key}", headers=self._lease_auth(),
            )
            self._raise_auth_status(resp, f"/api/runner/credentials/{key}")
            resp.raise_for_status()
            return resp.json().get("value")
        except WorkerAuthRejected:
            raise
        except Exception as e:
            logger.warning("credential_fetch_failed", key=key, error=str(e)[:120])
            return None

    async def close(self):
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        if self._inner is not None:
            await self._inner.close()
