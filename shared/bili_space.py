"""B站 UP 主空间视频枚举。用 bilibili-api(内置 wbi 签名/buvid/ticket,解 -352 风控)。

凭证取自 DB app_credentials.bili_cookies(扫码登录入库,小写键 sessdata/bili_jct/
dedeuserid/buvid3,与 api/routes/bili.py 入库格式一致)。未登录也可枚举公开投稿,但带
登录态更稳、清晰度更高。
"""

from __future__ import annotations

import json


def _credential(bili_cookies_raw: str | None):
    from bilibili_api import Credential
    if not bili_cookies_raw:
        return None
    try:
        d = json.loads(bili_cookies_raw)
    except (ValueError, TypeError):
        return None
    # 键名必须与 api/routes/bili.py login_poll 入库的小写一致,
    # 否则读不到凭证,enumerate_up 会退化为匿名枚举。
    return Credential(
        sessdata=d.get("sessdata"),
        bili_jct=d.get("bili_jct"),
        dedeuserid=str(d.get("dedeuserid", "")) or None,
        buvid3=d.get("buvid3"),
    )


async def enumerate_up(mid: str, bili_cookies_raw: str | None = None) -> list[dict]:
    """列出某 UP 全部投稿(按发布时间倒序)。返回 [{bvid,title,duration,pic,created}]。"""
    import asyncio
    from bilibili_api import user

    cred = _credential(bili_cookies_raw)
    u = user.User(int(mid), credential=cred)
    out: list[dict] = []
    pn = 1
    while True:
        res = await u.get_videos(pn=pn, ps=50, order=user.VideoOrder.PUBDATE)
        vlist = res.get("list", {}).get("vlist", [])
        for v in vlist:
            out.append({
                "bvid": v["bvid"],
                "title": v.get("title", ""),
                "duration": v.get("length", ""),
                "pic": v.get("pic", ""),
                "created": v.get("created"),
            })
        total = res.get("page", {}).get("count", 0)
        if len(out) >= total or not vlist:
            break
        pn += 1
        await asyncio.sleep(1)  # 翻页间隔,降风控触发
    return out


async def up_name(mid: str, bili_cookies_raw: str | None = None) -> str | None:
    """取 UP 主昵称(订阅集合命名用,存纯真实名)。尽力而为,失败返回 None。"""
    from bilibili_api import user
    try:
        info = await user.User(int(mid), credential=_credential(bili_cookies_raw)).get_user_info()
        return (info.get("name") or "").strip() or None
    except Exception:
        return None


async def enumerate_fav(media_id: str, bili_cookies_raw: str | None = None) -> tuple[str | None, list[dict]]:
    """列出某收藏夹(media_id)全部视频。返回 (收藏夹名|None, [{bvid,title,...}])。

    走 bilibili_api.favorite_list.get_video_favorite_list_content,该函数返回接口 data:
    {"info": {"title": 收藏夹名, ...}, "medias": [{bvid,title,duration,cover,...}], "has_more": bool}。
    翻页直到 has_more 为假(每页固定 20)。非视频条目(失效/非视频 type)无 bvid,由调用方过滤。"""
    import asyncio
    from bilibili_api import favorite_list

    cred = _credential(bili_cookies_raw)
    title: str | None = None
    out: list[dict] = []
    seen: set[str] = set()
    page = 1
    while True:
        res = await favorite_list.get_video_favorite_list_content(
            int(media_id), page=page, credential=cred,
        )
        if title is None:
            title = (res.get("info") or {}).get("title") or None
        medias = res.get("medias") or []
        for m in medias:
            bvid = m.get("bvid")
            if not bvid or bvid in seen:
                continue
            seen.add(bvid)
            out.append({
                "bvid": bvid,
                "title": m.get("title", ""),
                "duration": m.get("duration", ""),
                "pic": m.get("cover", ""),
                "created": m.get("pubtime"),
            })
        if not res.get("has_more") or not medias:
            break
        page += 1
        await asyncio.sleep(1)  # 翻页间隔,降风控触发
    return title, out


async def enumerate_collection(
    mid: str, series_id: str, is_season: bool, bili_cookies_raw: str | None = None,
) -> tuple[str | None, list[dict]]:
    """列出某合集/系列(UP mid + season/series id)全部视频。
    返回 (合集名|None, [{bvid,title,...}])。

    is_season=True 走合集(B站新概念多 P,season_id);False 走视频列表(series_id)。
    经 bilibili_api.channel_series.ChannelSeries:其构造在 __init__ 内拉一次 meta(取合集名),
    再 get_videos 翻页。两类接口都返回 {"archives": [{aid,bvid,title,...}], "page": {...}}。"""
    import asyncio
    from bilibili_api import channel_series

    cred = _credential(bili_cookies_raw)
    type_ = (
        channel_series.ChannelSeriesType.SEASON
        if is_season
        else channel_series.ChannelSeriesType.SERIES
    )
    cs = channel_series.ChannelSeries(
        uid=int(mid), type_=type_, id_=int(series_id), credential=cred,
    )
    meta = cs.get_meta() or {}
    title = meta.get("name") or meta.get("title") or None

    out: list[dict] = []
    seen: set[str] = set()
    pn = 1
    while True:
        res = await cs.get_videos(pn=pn, ps=100)
        archives = res.get("archives") or []
        for a in archives:
            bvid = a.get("bvid")
            if not bvid or bvid in seen:
                continue
            seen.add(bvid)
            out.append({
                "bvid": bvid,
                "title": a.get("title", ""),
                "duration": a.get("duration", ""),
                "pic": a.get("pic", ""),
                "created": a.get("pubdate"),
            })
        total = (res.get("page") or {}).get("total", 0)
        if not archives or len(out) >= total:
            break
        pn += 1
        await asyncio.sleep(1)  # 翻页间隔,降风控触发
    return title, out
