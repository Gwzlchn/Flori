"""依赖注入：get_db, get_redis, verify_token, verify_worker_token。"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
from functools import lru_cache

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from shared.config import AppConfig
from shared.db import Database
from shared.redis_client import RedisClient
from shared.storage import StorageBackend

_security = HTTPBearer(auto_error=False)


def get_db(request: Request) -> Database:
    return request.app.state.db


def get_redis(request: Request) -> RedisClient:
    return request.app.state.redis


def get_storage(request: Request) -> StorageBackend:
    return request.app.state.storage


def get_config(request: Request) -> AppConfig:
    return request.app.state.config


async def verify_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(_security),
) -> str:
    api_token = os.environ.get("API_TOKEN", "")
    if not api_token:
        import structlog
        structlog.get_logger().warning("api_token_empty", msg="API_TOKEN not set, auth disabled")
        return "no-auth"
    if credentials is None or not hmac.compare_digest(
        credentials.credentials.encode(), api_token.encode()
    ):
        raise HTTPException(status_code=401, detail="unauthorized")
    return credentials.credentials


async def verify_worker_token(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_security),
) -> str:
    """校验 per-worker token：sha256 后查 worker_tokens，返回归属的 worker_id。
    缺失/未命中/已吊销均 401（不区分以免泄露 token 是否存在）。"""
    if credentials is None:
        raise HTTPException(status_code=401, detail="missing worker token")
    token_hash = hashlib.sha256(credentials.credentials.encode()).hexdigest()
    db: Database = request.app.state.db
    row = await asyncio.to_thread(db.get_worker_token_by_hash, token_hash)
    if row is None or row["revoked"]:
        raise HTTPException(status_code=401, detail="invalid or revoked worker token")
    return row["worker_id"]


async def verify_registration_token(presented: str, redis: RedisClient) -> None:
    """接入门禁：放行 Redis 铸造的一次性 token，或 env 兜底 token（常量时间比对）。
    两者都没配置 → 503 fail closed；配置了但不匹配 → 401。"""
    minted = await redis.get_registration_token()
    env_token = os.environ.get("WORKER_REGISTRATION_TOKEN", "")
    if not minted and not env_token:
        raise HTTPException(status_code=503, detail="registration disabled")
    presented_b = (presented or "").encode()
    if minted and hmac.compare_digest(presented_b, minted.encode()):
        return
    if env_token and hmac.compare_digest(presented_b, env_token.encode()):
        return
    raise HTTPException(status_code=401, detail="bad registration token")
