"""依赖注入：get_db, get_redis, verify_token。"""

from __future__ import annotations

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
