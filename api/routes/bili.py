"""B站扫码登录路由:passport QR 流程 + cookie 入库。"""

from __future__ import annotations

import asyncio
import base64
import io
import json

from fastapi import APIRouter, Depends, HTTPException

from shared.db import Database
from shared.redis_client import RedisClient

from api.deps import get_db, get_redis, verify_token

router = APIRouter(prefix="/api/bili", tags=["bili"], dependencies=[Depends(verify_token)])

# 凭证存于 app_credentials 的固定 key,值为 JSON(sessdata/bili_jct/dedeuserid/uname)。
_CRED_KEY = "bili_cookies"

# B站 WAF 对无浏览器 UA 的请求返回 412 + HTML,故所有 passport 请求必须伪装浏览器。
_BILI_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com/",
}

_GENERATE_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
_POLL_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
_NAV_URL = "https://api.bilibili.com/x/web-interface/nav"

# passport poll 的 data.code 到契约 state 的映射。
_CODE_TO_STATE = {
    86101: "waiting",   # 未扫
    86090: "scanned",   # 已扫未确认
    86038: "expired",   # 已过期
    0: "confirmed",     # 成功
}


def _render_qr_png(url: str) -> str:
    """把登录 url 渲染为二维码 PNG 并编码成 data URI,前端可直接当 img src。"""
    import qrcode

    img = qrcode.make(url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{b64}"


async def _fetch_uname(sessdata: str) -> str | None:
    """用 SESSDATA 调 nav 接口取昵称;任何失败均降级为 None,不阻断登录。"""
    import httpx

    try:
        async with httpx.AsyncClient(headers=_BILI_HEADERS) as client:
            resp = await client.get(
                _NAV_URL, cookies={"SESSDATA": sessdata}, timeout=10,
            )
        data = resp.json()
    except Exception:
        return None
    if data.get("code") != 0:
        return None
    return data.get("data", {}).get("uname") or None


def _load_cookies(db: Database) -> dict | None:
    """读已入库的 B站 cookie JSON,无则 None。"""
    raw = db.get_credential(_CRED_KEY)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None


@router.post("/login/start")
async def login_start():
    """请求 passport 生成二维码,返回 qrcode_key + 渲染好的 PNG data URI。"""
    import httpx

    try:
        async with httpx.AsyncClient(headers=_BILI_HEADERS) as client:
            resp = await client.get(_GENERATE_URL, timeout=10)
        data = resp.json()
    except Exception as e:
        raise HTTPException(502, f"bilibili passport unreachable: {e}")

    if data.get("code") != 0:
        raise HTTPException(502, f"bilibili passport error: {data}")
    payload = data.get("data", {})
    url = payload.get("url")
    qrcode_key = payload.get("qrcode_key")
    if not url or not qrcode_key:
        raise HTTPException(502, "bilibili passport returned no qrcode")

    qr_png = await asyncio.to_thread(_render_qr_png, url)
    return {"qrcode_key": qrcode_key, "qr_png": qr_png, "url": url}


@router.get("/login/poll")
async def login_poll(
    qrcode_key: str,
    db: Database = Depends(get_db),
    redis: RedisClient = Depends(get_redis),
):
    """轮询扫码态;confirmed 时从 Set-Cookie 取 SESSDATA/bili_jct/DedeUserID 入库。"""
    import httpx

    try:
        async with httpx.AsyncClient(headers=_BILI_HEADERS) as client:
            resp = await client.get(
                _POLL_URL, params={"qrcode_key": qrcode_key}, timeout=10,
            )
        data = resp.json()
    except Exception as e:
        raise HTTPException(502, f"bilibili passport unreachable: {e}")

    code = data.get("data", {}).get("code", -1)
    state = _CODE_TO_STATE.get(code, "expired")

    if state != "confirmed":
        return {"state": state, "logged_in": False, "uname": None}

    # 成功:cookie 在响应的 Set-Cookie 里,httpx resp.cookies 取三件套。
    cookies = resp.cookies
    sessdata = cookies.get("SESSDATA")
    bili_jct = cookies.get("bili_jct")
    dedeuserid = cookies.get("DedeUserID")
    buvid3 = cookies.get("buvid3")  # 若 passport 响应带 buvid3 则一并入库(bili_space 用其降风控)
    if not sessdata:
        raise HTTPException(502, "bilibili passport confirmed but no SESSDATA")

    uname = await _fetch_uname(sessdata)
    creds = {
        "sessdata": sessdata,
        "bili_jct": bili_jct,
        "dedeuserid": dedeuserid,
        "buvid3": buvid3,
        "uname": uname,
    }
    raw = json.dumps(creds, ensure_ascii=False)
    await asyncio.to_thread(db.set_credential, _CRED_KEY, raw)
    # 同步分发镜像:worker 下个下载任务即用新 SESSDATA,无需逐机刷文件。
    from shared.credentials import mirror_credential
    await mirror_credential(redis, _CRED_KEY, raw)
    return {"state": "confirmed", "logged_in": True, "uname": uname}


@router.get("/status")
async def status(db: Database = Depends(get_db)):
    """返回当前 B站登录态(依据库里是否有 cookie)。"""
    creds = await asyncio.to_thread(_load_cookies, db)
    if not creds or not creds.get("sessdata"):
        return {"logged_in": False, "uname": None}
    return {"logged_in": True, "uname": creds.get("uname")}


@router.post("/logout")
async def logout(
    db: Database = Depends(get_db),
    redis: RedisClient = Depends(get_redis),
):
    """清除已入库的 B站 cookie(连同分发镜像,worker 即刻回到匿名)。"""
    await asyncio.to_thread(db.delete_credential, _CRED_KEY)
    from shared.credentials import mirror_credential
    await mirror_credential(redis, _CRED_KEY, None)
    return {"ok": True}
