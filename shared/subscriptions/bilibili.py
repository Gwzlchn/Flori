"""B站 source-adapter。实现三种来源:
  - bilibili_up         UP 主全部投稿(source_id=mid)
  - bilibili_fav        收藏夹(source_id=media_id 或 favlist URL)
  - bilibili_collection 合集/系列(source_id=合集 URL 或紧凑式 mid:season|series:sid)

三者共用 ctx.bili_cookies(凭证)+ register 机制,枚举本体经 shared.bili_space 的
对应函数(经模块属性调用,便于测试 monkeypatch),都返回 video 类 SourceItem。
"""

from __future__ import annotations

import re

from shared import bili_space
from shared.subscriptions.base import SourceContext, SourceItem, register


def _video_items(videos: list[dict]) -> list[SourceItem]:
    """[{bvid,title,...}] → [SourceItem(video)]。无 bvid 的条目(失效/非视频)跳过。
    item_id=bvid(来源内稳定去重键),url 指向标准视频页,交由 01_download 的 B站分支下载。"""
    items: list[SourceItem] = []
    for v in videos:
        bvid = v.get("bvid")
        if not bvid:
            continue
        items.append(SourceItem(
            item_id=bvid,
            title=(v.get("title") or "").strip(),
            url=f"https://www.bilibili.com/video/{bvid}",
            content_type="video",
        ))
    return items


@register("bilibili_up")
async def enumerate_bilibili_up(
    source_id: str, ctx: SourceContext,
) -> tuple[str | None, list[SourceItem]]:
    """枚举某 UP(source_id=mid)的全部投稿 → SourceItem 列表。

    复用 shared.bili_space.enumerate_up(经模块属性调用,便于测试 monkeypatch)。
    enumerate_up 返回 [{bvid,title,duration,pic,created}],逐条映射为 video 类 SourceItem。
    source_title 取 UP 主真实昵称(bili_space.up_name → get_user_info),用于集合存纯真实名;
    拿不到回退 None(命名层用 source_id 占位)。"""
    videos = await bili_space.enumerate_up(source_id, ctx.bili_cookies)
    title = await bili_space.up_name(source_id, ctx.bili_cookies)
    return title, _video_items(videos)


def _parse_fav_media_id(source_id: str) -> str:
    """收藏夹 source_id → media_id。接受纯数字 media_id,或 favlist URL
    (https://space.bilibili.com/{mid}/favlist?fid={media_id}&... 中的 fid)。"""
    s = (source_id or "").strip()
    if s.isdigit():
        return s
    m = re.search(r"[?&]fid=(\d+)", s)
    if m:
        return m.group(1)
    m = re.search(r"(\d+)", s)  # 兜底:URL 里第一串数字(尽力而为)
    if m:
        return m.group(1)
    raise ValueError(f"无法解析收藏夹 media_id: {source_id!r}")


@register("bilibili_fav")
async def enumerate_bilibili_fav(
    source_id: str, ctx: SourceContext,
) -> tuple[str | None, list[SourceItem]]:
    """枚举某收藏夹(source_id=media_id 或 favlist URL)全部视频 → SourceItem 列表。

    复用 shared.bili_space.enumerate_fav(经模块属性调用,便于测试 monkeypatch),
    返回 (收藏夹名|None, [{bvid,title,...}])。收藏夹名作 source_title(命名层用于 <名>-bilibili)。"""
    media_id = _parse_fav_media_id(source_id)
    title, videos = await bili_space.enumerate_fav(media_id, ctx.bili_cookies)
    return title, _video_items(videos)


def _parse_collection(source_id: str) -> tuple[str, str, bool]:
    """合集 source_id → (mid, sid, is_season)。接受两类写法:

    1. 紧凑式 "mid:season:sid" / "mid:series:sid"(显式类型,最稳)。
    2. 合集/列表 URL,从中提取 mid、sid 与类型:
       - season(「合集·」新概念多 P):.../channel/collectiondetail?sid=  或 .../lists/{sid}?type=season
       - series(普通视频列表)     :.../channel/seriesdetail?sid=     或 .../lists/{sid}?type=series
       URL 里 type=season / collectiondetail 判为 season,否则按 series。

    sid 为 season_id 或 series_id;mid 为 UP 的 uid。三者缺一不可(season 接口也需 mid)。"""
    s = (source_id or "").strip()

    # 紧凑式 mid:type:sid
    m = re.fullmatch(r"(\d+):(season|series):(\d+)", s)
    if m:
        return m.group(1), m.group(3), m.group(2) == "season"

    # URL: mid 在 space.bilibili.com/{mid}/...
    mid_m = re.search(r"space\.bilibili\.com/(\d+)", s)
    sid_m = re.search(r"[?&]sid=(\d+)", s) or re.search(r"/lists/(\d+)", s)
    if mid_m and sid_m:
        is_season = bool(
            re.search(r"collectiondetail", s)
            or re.search(r"[?&]type=season", s)
        )
        return mid_m.group(1), sid_m.group(1), is_season

    raise ValueError(
        f"无法解析合集 source_id: {source_id!r}(用 'mid:season:sid'/'mid:series:sid' 或合集 URL)"
    )


@register("bilibili_collection")
async def enumerate_bilibili_collection(
    source_id: str, ctx: SourceContext,
) -> tuple[str | None, list[SourceItem]]:
    """枚举某合集/系列(source_id=合集 URL 或 mid:season|series:sid)全部视频 → SourceItem 列表。

    复用 shared.bili_space.enumerate_collection(经模块属性调用,便于测试 monkeypatch),
    返回 (合集名|None, [{bvid,title,...}])。合集名作 source_title(命名层用于 <名>-bilibili)。"""
    mid, sid, is_season = _parse_collection(source_id)
    title, videos = await bili_space.enumerate_collection(
        mid, sid, is_season, ctx.bili_cookies,
    )
    return title, _video_items(videos)
