"""平台认证路由:YouTube cookies + 平台 cookie 状态(B站扫码登录见 api/routes/bili.py)。"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File

from api.deps import verify_token

router = APIRouter(prefix="/api/auth", tags=["auth"], dependencies=[Depends(verify_token)])

# 跟随 DATA_DIR(与 shared/config.data_dir 同源):改 DATA_DIR 时 cookies 目录一并跟随;
# test 可 monkeypatch 本模块属性覆盖。
COOKIES_DIR = Path(os.environ.get("DATA_DIR", "/data")) / "cookies"


@router.get("/status")
async def auth_status():
    """平台 cookie 文件状态。B站登录态以 /api/bili/status(DB)为准,此处仅反映
    下载步的本地文件回退 bilibili.txt 是否存在 + YouTube cookies 是否已配置。"""
    COOKIES_DIR.mkdir(parents=True, exist_ok=True)
    bilibili = (COOKIES_DIR / "bilibili.txt").exists()
    youtube = (COOKIES_DIR / "youtube.txt").exists()
    return {
        "bilibili": {"has_cookies": bilibili, "status": "ok" if bilibili else "missing"},
        "youtube": {"has_cookies": youtube, "status": "ok" if youtube else "missing"},
    }


# 平台 → cookie 文件名白名单。前端 CookieUpload 用动态 platform 拼 /api/auth/{platform}/cookies,
# 此处白名单与之匹配并挡任意路径写入。目前仅 youtube(B站走扫码登录,见 /api/bili,非 cookie 上传)。
_COOKIE_PLATFORMS = {"youtube": "youtube.txt"}


@router.post("/{platform}/cookies")
async def upload_platform_cookies(platform: str, file: UploadFile = File(...)):
    """上传指定平台的 cookie 文件(Netscape 格式)。platform 走白名单。"""
    fname = _COOKIE_PLATFORMS.get(platform)
    if fname is None:
        raise HTTPException(400, f"unsupported platform: {platform}")
    COOKIES_DIR.mkdir(parents=True, exist_ok=True)
    # cookie 文件本应几 KB,流式累加设小上限,避免已认证用户误传大文件全量读进内存(对齐 jobs 上传)。
    MAX_COOKIE_SIZE = 1024 * 1024  # 1 MiB
    buf = bytearray()
    while chunk := await file.read(64 * 1024):
        buf.extend(chunk)
        if len(buf) > MAX_COOKIE_SIZE:
            raise HTTPException(413, f"cookie file too large (max {MAX_COOKIE_SIZE})")
    (COOKIES_DIR / fname).write_bytes(bytes(buf))
    return {"status": "ok", "message": f"{platform} cookies 已保存"}
