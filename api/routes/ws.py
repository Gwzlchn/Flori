"""WebSocket 进度推送。

设计要点（见 shared/redis_client.py:subscribe 同款经验）：
- 纯推送 WS（服务端 → 客户端）不会在 listen/send 之外感知到客户端断开。
  ws_job 空闲时不发任何数据，因此必须用一个并发的 receive() 任务来探测
  断开，否则连接半开后会一直挂着轮询任务直到下次消息到达。
- 绝不用 ``pubsub.listen()`` 异步生成器：redis 关闭空闲 pubsub 连接后它会抛
  TimeoutError / 静默停迭代，旧实现把它当 error 用 logger.exception 记录 →
  Dozzle 里 API 容器“全是 error”。改用带 timeout 的 ``get_message`` 轮询：
  空闲返回 None（不抛异常），连接级 Timeout/Connection/OSError 静默退避重连，
  绝不逃逸成 error 日志。
"""

from __future__ import annotations

import asyncio
import json

import structlog
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from redis.exceptions import (
    ConnectionError as RedisConnectionError,
    TimeoutError as RedisTimeoutError,
)

from shared.redis_client import RedisClient

logger = structlog.get_logger(component="ws")

router = APIRouter(tags=["websocket"])


async def _watch_disconnect(websocket: WebSocket) -> None:
    """阻塞直到客户端断开。

    纯推送 WS 检测断开的正确做法：并发 await receive()。客户端正常关闭时首次
    receive() 拿到 disconnect 帧并抛 WebSocketDisconnect；若已收到 disconnect
    帧后再 receive()，Starlette 改抛 RuntimeError。两者都视为“连接已断”信号，
    静默返回，让主循环走 finally 清理。
    """
    try:
        while True:
            await websocket.receive()
    except WebSocketDisconnect:
        return
    except RuntimeError:
        # Starlette 在连接已断后再 receive() 会抛 RuntimeError，等价于断开。
        return


@router.websocket("/api/ws/jobs/{job_id}")
async def ws_job(websocket: WebSocket, job_id: str):
    await websocket.accept()
    redis: RedisClient = websocket.app.state.redis
    channel = f"events:{job_id}"

    # 并发探测客户端断开：空闲时也能及时退出，不泄漏轮询任务。
    disconnect_task = asyncio.create_task(_watch_disconnect(websocket))

    pubsub = redis.r.pubsub()
    subscribed = False
    backoff = 1
    try:
        while True:
            # 客户端已断开 → 干净退出。
            if disconnect_task.done():
                break

            # 确保订阅存在（首次或连接级故障重连后重订阅）。
            if not subscribed:
                try:
                    await pubsub.subscribe(channel)
                    subscribed = True
                    backoff = 1
                except asyncio.CancelledError:
                    raise
                except (RedisConnectionError, RedisTimeoutError, OSError):
                    # 连接级故障：静默退避重连，不当 error。
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

            # 带 timeout 的轮询：空闲返回 None（不抛异常），不产生 error 日志。
            try:
                msg = await pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=1.0
                )
            except asyncio.CancelledError:
                raise
            except (RedisConnectionError, RedisTimeoutError, OSError):
                # redis 连接级 timeout/断开：静默处理，标记重订阅后退避重连。
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
                    # 客户端在推送瞬间断开：干净退出。
                    break
    except WebSocketDisconnect:
        pass
    except asyncio.CancelledError:
        raise
    except Exception:
        # 仅真正意外（非连接级、非断开）才作为 error 记录。
        logger.exception("ws_job_error", job_id=job_id)
    finally:
        disconnect_task.cancel()
        try:
            await disconnect_task
        except (asyncio.CancelledError, Exception):
            pass
        # finally 里所有清理都吞异常，绝不再抛。
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
    """退避等待 delay 秒，但若期间客户端断开则提前返回 True。"""
    try:
        await asyncio.wait_for(asyncio.shield(disconnect_task), timeout=delay)
        return True  # disconnect_task 完成 = 客户端已断开
    except asyncio.TimeoutError:
        return disconnect_task.done()


@router.websocket("/api/ws/global")
async def ws_global(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            status = await _build_global_status(websocket.app)
            try:
                await websocket.send_text(json.dumps(status, ensure_ascii=False))
            except (WebSocketDisconnect, RuntimeError):
                # 客户端断开：干净退出，不刷 error 日志。
                break
            await asyncio.sleep(2)
    except WebSocketDisconnect:
        pass
    except asyncio.CancelledError:
        raise
    except Exception:
        # 仅 _build_global_status（DB 查询）真正出错才记 error，属应当暴露的故障。
        logger.exception("ws_global_error")


async def _build_global_status(app) -> dict:
    db = app.state.db

    total, _ = await asyncio.to_thread(db.list_jobs, limit=0)
    done_total, _ = await asyncio.to_thread(db.list_jobs, status="done", limit=0)
    failed_total, _ = await asyncio.to_thread(db.list_jobs, status="failed", limit=0)
    processing_total, _ = await asyncio.to_thread(db.list_jobs, status="processing", limit=0)

    return {
        "jobs": {
            "total": total,
            "done": done_total,
            "processing": processing_total,
            "failed": failed_total,
        },
    }
