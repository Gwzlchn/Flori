"""平台认证路由:YouTube cookies 上传(入库)+ 平台凭证状态(B站扫码见 api/routes/bili.py)。

凭证只存 DB credentials 表并镜像 redis 分发(shared/credentials),worker 认领下载步时
经 transport 领取——cookie 文件共享已废除(加 worker 零预置,过期只刷中心一次)。
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File

from shared.db import Database
from shared.redis_client import RedisClient
from api.deps import get_db, get_redis, verify_token

router = APIRouter(prefix="/api/auth", tags=["auth"], dependencies=[Depends(verify_token)])


@router.get("/status")
async def auth_status(db: Database = Depends(get_db)):
    """平台凭证配置状态(以 DB credentials 为准)。B站登录详情(uname)见 /api/bili/status。"""
    from shared.credentials import extract_bili_sessdata

    bili = bool(extract_bili_sessdata(
        await asyncio.to_thread(db.get_credential, "bili_cookies")))
    youtube = bool((await asyncio.to_thread(db.get_credential, "youtube_cookies") or "").strip())
    return {
        "bilibili": {"has_cookies": bili, "status": "ok" if bili else "missing"},
        "youtube": {"has_cookies": youtube, "status": "ok" if youtube else "missing"},
    }


# 平台 → 凭证存储 key 白名单。前端 CookieUpload 用动态 platform 拼 /api/auth/{platform}/cookies,
# 白名单挡任意 key 写入。目前仅 youtube(B站走扫码登录 /api/bili,非 cookie 上传)。
_COOKIE_PLATFORMS = {"youtube": "youtube_cookies"}


@router.post("/{platform}/cookies")
async def upload_platform_cookies(
    platform: str,
    file: UploadFile = File(...),
    db: Database = Depends(get_db),
    redis: RedisClient = Depends(get_redis),
):
    """上传指定平台的 cookie(Netscape 格式)→ 入库 + 镜像 redis 分发。platform 走白名单。"""
    cred_key = _COOKIE_PLATFORMS.get(platform)
    if cred_key is None:
        raise HTTPException(400, f"unsupported platform: {platform}")
    # cookie 文件本应几 KB,流式累加设小上限,避免已认证用户误传大文件全量读进内存(对齐 jobs 上传)。
    MAX_COOKIE_SIZE = 1024 * 1024  # 1 MiB
    buf = bytearray()
    while chunk := await file.read(64 * 1024):
        buf.extend(chunk)
        if len(buf) > MAX_COOKIE_SIZE:
            raise HTTPException(413, f"cookie file too large (max {MAX_COOKIE_SIZE})")
    value = bytes(buf).decode("utf-8", errors="replace")
    await asyncio.to_thread(db.set_credential, cred_key, value)
    from shared.credentials import mirror_credential
    await mirror_credential(redis, cred_key, value)
    return {"status": "ok", "message": f"{platform} cookies 已保存"}
