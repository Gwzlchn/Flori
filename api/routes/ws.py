"""WebSocket 进度推送。事件契约见 docs/03-contracts.md §2。

设计要点(与 shared/redis_client.py:subscribe 同款经验):
- 纯推送 WS 不会在 listen/send 之外感知到客户端断开。
  ws_job 空闲时不发任何数据,因此必须用一个并发的 receive() 任务来探测
  断开,否则连接半开后会一直挂着轮询任务直到下次消息到达。
- 绝不用 ``pubsub.listen()`` 异步生成器:redis 关闭空闲 pubsub 连接后它会抛
  TimeoutError 或静默停迭代,把它当 error 记录会让 API 容器日志刷满 error。
  这里用带 timeout 的 ``get_message`` 轮询:空闲返回 None 不抛异常,
  连接级 Timeout/Connection/OSError 静默退避重连,绝不逃逸成 error 日志。
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os

import structlog
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from redis.exceptions import (
    ConnectionError as RedisConnectionError,
    TimeoutError as RedisTimeoutError,
)

from shared.redis_client import RedisClient

logger = structlog.get_logger(component="ws")

router = APIRouter(tags=["websocket"])


def _ws_authorized(websocket: WebSocket) -> bool:
    """WS 鉴权:与 deps.verify_token 同款 fail-closed 策略,但 token 取自 query 参数
    (``?token=...``),因为浏览器无法为 WebSocket 握手设置 Authorization 头。
    - 设了 API_TOKEN:query token 必须常量时间匹配,否则拒。
    - 未设 API_TOKEN:必须显式 API_ALLOW_NO_AUTH=1(仅可信内网)才放行,否则拒。
    放行返回 True,否则 False(调用方 close(1008) 后返回)。"""
    api_token = os.environ.get("API_TOKEN", "")
    if not api_token:
        allow = (os.environ.get("API_ALLOW_NO_AUTH") or "").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
        return allow
    presented = websocket.query_params.get("token", "")
    return hmac.compare_digest(presented.encode(), api_token.encode())


async def _watch_disconnect(websocket: WebSocket) -> None:
    """阻塞直到客户端断开。

    纯推送 WS 检测断开的正确做法:并发 await receive()。客户端正常关闭时首次
    receive() 拿到 disconnect 帧并抛 WebSocketDisconnect;若已收到 disconnect
    帧后再 receive(),Starlette 改抛 RuntimeError。两者都视为连接已断信号,
    静默返回,让主循环走 finally 清理。
    """
    try:
        while True:
            await websocket.receive()
    except WebSocketDisconnect:
        return
    except RuntimeError:
        return


@router.websocket("/api/ws/jobs/{job_id}")
async def ws_job(websocket: WebSocket, job_id: str):
    if not _ws_authorized(websocket):
        await websocket.close(code=1008)  # policy violation:未授权
        return
    await websocket.accept()
    redis: RedisClient = websocket.app.state.redis
    channel = f"events:{job_id}"

    # 并发探测客户端断开:空闲时也能及时退出,不泄漏轮询任务。
    disconnect_task = asyncio.create_task(_watch_disconnect(websocket))

    pubsub = redis.r.pubsub()
    subscribed = False
    backoff = 1
    try:
        while True:
            # 客户端已断开,干净退出.
            if disconnect_task.done():
                break

            # 确保订阅存在(首次或连接级故障重连后重订阅)。
            if not subscribed:
                try:
                    await pubsub.subscribe(channel)
                    subscribed = True
                    backoff = 1
                except asyncio.CancelledError:
                    raise
                except (RedisConnectionError, RedisTimeoutError, OSError):
                    # 连接级故障:静默退避重连,不当 error。
                    subscribed = False
                    if await _sleep_or_disconnect(disconnect_task, backoff):
                        break
                    backoff = min(backoff * 2, 30)
                    try:
                        await pubsub.aclose()
                    except Exception:
                        pass
                    try:
                        await redis.reconnect()
                    except Exception:
                        pass
                    pubsub = redis.r.pubsub()
                    continue

            # 带 timeout 的轮询:空闲返回 None(不抛异常),不产生 error 日志。
            try:
                msg = await pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=1.0
                )
            except asyncio.CancelledError:
                raise
            except (RedisConnectionError, RedisTimeoutError, OSError):
                # redis 连接级 timeout/断开:静默处理,标记重订阅后退避重连。
                subscribed = False
                if await _sleep_or_disconnect(disconnect_task, backoff):
                    break
                backoff = min(backoff * 2, 30)
                try:
                    await pubsub.aclose()
                except Exception:
                    pass
                try:
                    await redis.reconnect()
                except Exception:
                    pass
                pubsub = redis.r.pubsub()
                continue

            if msg is None:
                continue
            if msg.get("type") == "message":
                backoff = 1
                try:
                    await websocket.send_text(msg["data"])
                except (WebSocketDisconnect, RuntimeError):
                    # 客户端在推送瞬间断开:干净退出。
                    break
    except WebSocketDisconnect:
        pass
    except asyncio.CancelledError:
        raise
    except Exception:
        # 仅真正意外(非连接级、非断开)才作为 error 记录。
        logger.exception("ws_job_error", job_id=job_id)
    finally:
        disconnect_task.cancel()
        try:
            await disconnect_task
        except (asyncio.CancelledError, Exception):
            pass
        # finally 里所有清理都吞异常,绝不再抛。
        if subscribed:
            try:
                await pubsub.unsubscribe(channel)
            except Exception:
                pass
        try:
            await pubsub.aclose()
        except Exception:
            pass


async def _sleep_or_disconnect(disconnect_task: asyncio.Task, delay: float) -> bool:
    """退避等待 delay 秒,但若期间客户端断开则提前返回 True。"""
    try:
        await asyncio.wait_for(asyncio.shield(disconnect_task), timeout=delay)
        return True  # disconnect_task 完成 = 客户端已断开
    except asyncio.TimeoutError:
        return disconnect_task.done()


@router.websocket("/api/ws/global")
async def ws_global(websocket: WebSocket):
    if not _ws_authorized(websocket):
        await websocket.close(code=1008)  # policy violation:未授权
        return
    await websocket.accept()
    try:
        while True:
            status = await _build_global_status(websocket.app)
            try:
                await websocket.send_text(json.dumps(status, ensure_ascii=False))
            except (WebSocketDisconnect, RuntimeError):
                # 客户端断开:干净退出,不刷 error 日志。
                break
            await asyncio.sleep(2)
    except WebSocketDisconnect:
        pass
    except asyncio.CancelledError:
        raise
    except Exception:
        # 仅 _build_global_status(DB 查询)真正出错才记 error,属应当暴露的故障。
        logger.exception("ws_global_error")


async def _build_global_status(app) -> dict:
    # 只推 live 子集(workers/pools/jobs/disk)。组件探测走 redis INFO / minio bucket_exists,
    # 属慢变量(心跳 30s 级),每 2s 跑会给 redis/minio 加无谓负载;
    # 含组件的全量状态由 HTTP 轮询 GET /api/status 承担,契约见 docs/03-contracts.md §2。
    from api.routes.admin import build_live_status
    return await build_live_status(app.state.db, app.state.redis, app.state.config)
