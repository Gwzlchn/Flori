"""下载凭证中心分发:DB 单一持久源 → redis 镜像 → worker 经 transport 领取。

设计(docs/03 §1.7.1):凭证只存 DB credentials 表(B站扫码/YouTube 上传入库);
写入时镜像到 redis cred:{dispatch_key},worker 认领下载步时按 dispatch key 领取,
本地(redis GET)与远程(GET /api/runner/credentials/{key})同一抽象。
cookie 文件共享已废除:加 worker 零预置,cookie 过期只刷中心一次。

dispatch key 与存储 key 是两层:存储层 bili_cookies 是扫码全量 JSON,
分发层只给 worker 最小可用值(bili_sessdata=提取出的 SESSDATA)。
"""

from __future__ import annotations

import json

# 可下发给 worker 的凭证白名单(runner 端点/transport 只认这些 key)。
DISPATCH_KEYS = ("bili_sessdata", "youtube_cookies")

# DB 存储 key → 派生 dispatch key 的映射来源(镜像/兜底解析共用)。
_STORED_KEYS = ("bili_cookies", "youtube_cookies")


def extract_bili_sessdata(raw: str | None) -> str | None:
    """从扫码入库的 bili_cookies JSON 提取 SESSDATA;缺失/损坏返回 None。"""
    if not raw:
        return None
    try:
        return json.loads(raw).get("sessdata") or None
    except (json.JSONDecodeError, ValueError, AttributeError):
        return None


def derive_dispatch(stored_key: str, value: str | None) -> dict[str, str | None]:
    """存储 key 的值 → 它派生的全部 dispatch 凭证(值为 None 表示应清除镜像)。"""
    if stored_key == "bili_cookies":
        return {"bili_sessdata": extract_bili_sessdata(value)}
    if stored_key == "youtube_cookies":
        return {"youtube_cookies": (value or "").strip() or None}
    return {}


def resolve_from_db(db, dispatch_key: str) -> str | None:
    """redis 镜像 miss 时的 DB 兜底解析(runner 端点/本地 transport 共用)。"""
    if dispatch_key == "bili_sessdata":
        return extract_bili_sessdata(db.get_credential("bili_cookies"))
    if dispatch_key == "youtube_cookies":
        return (db.get_credential("youtube_cookies") or "").strip() or None
    return None


async def mirror_credential(redis, stored_key: str, value: str | None) -> None:
    """凭证写入/清除后同步 redis 镜像(api 路由层调用,保持 shared/db 纯 sync)。"""
    for dk, dv in derive_dispatch(stored_key, value).items():
        await redis.set_dispatch_credential(dk, dv)


async def mirror_all_from_db(redis, db) -> None:
    """DB 全部凭证灌 redis 镜像(scheduler 启动 reconcile 调用,防 redis 卷重建丢镜像)。"""
    import asyncio

    for sk in _STORED_KEYS:
        value = await asyncio.to_thread(db.get_credential, sk)
        await mirror_credential(redis, sk, value)
