"""B站 收藏夹 / 合集 source-adapter 单测(不依赖网络:mock bili_space 枚举函数)。

覆盖:
  - bilibili_fav:
    - source_id 解析(纯 media_id / favlist URL 的 fid / 兜底取数字)
    - enumerate_fav 返回 (收藏夹名, [{bvid,title,...}]) → SourceItem 映射
    - 无 bvid 条目跳过、source_title 透传
  - bilibili_collection:
    - source_id 解析(紧凑式 mid:season|series:sid / collectiondetail|seriesdetail URL / lists?type= URL)
    - enumerate_collection(mid, sid, is_season, cookies) 调用参数正确
    - 合集名作 source_title、video 映射、无效写法报错
  - 注册:@register 进入 SOURCE_ADAPTERS / enumerate_source 可分派

mock 方式:经模块属性 monkeypatch `shared.bili_space.enumerate_fav` /
`shared.bili_space.enumerate_collection`(适配器内经 bili_space.<fn> 调用,故生效)。
"""

from __future__ import annotations

import pytest

from shared.subscriptions.base import SourceContext, SourceItem
from shared.subscriptions.bilibili import (
    _parse_collection,
    _parse_fav_media_id,
    enumerate_bilibili_collection,
    enumerate_bilibili_fav,
)


# 收藏夹 source_id 解析
@pytest.mark.parametrize(
    "source_id, expected",
    [
        ("123456", "123456"),
        ("  123456  ", "123456"),
        ("https://space.bilibili.com/247209804/favlist?fid=987654&ftype=create",
         "987654"),
        # fid 在其它参数之后也能取到
        ("https://space.bilibili.com/1/favlist?ftype=create&fid=42", "42"),
        # 无 fid 的 URL → 兜底取第一串数字(尽力而为)
        ("https://space.bilibili.com/555/favlist", "555"),
    ],
)
def test_parse_fav_media_id(source_id, expected):
    assert _parse_fav_media_id(source_id) == expected


def test_parse_fav_media_id_invalid():
    with pytest.raises(ValueError):
        _parse_fav_media_id("no-digits-here")


# 收藏夹适配器主体
@pytest.mark.asyncio
async def test_fav_enumerate_maps_items(monkeypatch):
    captured: dict = {}

    async def fake_enum_fav(media_id, cookies=None):
        captured["media_id"] = media_id
        captured["cookies"] = cookies
        return "我的收藏夹", [
            {"bvid": "BV1aaaaaaaaa", "title": " 第一 ", "duration": 60},
            {"bvid": "BV1bbbbbbbbb", "title": "第二", "duration": 120},
            {"title": "失效条目无 bvid"},  # 无 bvid → 跳过
        ]

    monkeypatch.setattr("shared.bili_space.enumerate_fav", fake_enum_fav)

    title, items = await enumerate_bilibili_fav(
        "https://space.bilibili.com/1/favlist?fid=789",
        SourceContext(bili_cookies="COOKIE"),
    )

    assert captured["media_id"] == "789"
    assert captured["cookies"] == "COOKIE"
    assert title == "我的收藏夹"
    assert all(isinstance(i, SourceItem) for i in items)
    assert [i.item_id for i in items] == ["BV1aaaaaaaaa", "BV1bbbbbbbbb"]
    assert items[0].title == "第一"  # 已 strip
    assert items[0].url == "https://www.bilibili.com/video/BV1aaaaaaaaa"
    assert all(i.content_type == "video" for i in items)


@pytest.mark.asyncio
async def test_fav_title_none_passthrough(monkeypatch):
    async def fake_enum_fav(media_id, cookies=None):
        return None, [{"bvid": "BV1ccccccccc", "title": "x"}]

    monkeypatch.setattr("shared.bili_space.enumerate_fav", fake_enum_fav)
    title, items = await enumerate_bilibili_fav("100", SourceContext())
    assert title is None
    assert [i.item_id for i in items] == ["BV1ccccccccc"]


# 合集 source_id 解析
@pytest.mark.parametrize(
    "source_id, mid, sid, is_season",
    [
        # 紧凑式
        ("100:season:200", "100", "200", True),
        ("100:series:200", "100", "200", False),
        # season URL(collectiondetail / type=season)
        ("https://space.bilibili.com/100/channel/collectiondetail?sid=200",
         "100", "200", True),
        ("https://space.bilibili.com/100/lists/200?type=season",
         "100", "200", True),
        # series URL(seriesdetail / type=series / 默认非 season)
        ("https://space.bilibili.com/100/channel/seriesdetail?sid=200&ctype=0",
         "100", "200", False),
        ("https://space.bilibili.com/100/lists/200?type=series",
         "100", "200", False),
    ],
)
def test_parse_collection(source_id, mid, sid, is_season):
    assert _parse_collection(source_id) == (mid, sid, is_season)


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "just-text",
        "100",                       # 只有 mid,缺 sid
        "https://space.bilibili.com/100/channel/seriesdetail",  # URL 无 sid
        "100:wrongtype:200",         # 类型词非 season/series
    ],
)
def test_parse_collection_invalid(bad):
    with pytest.raises(ValueError):
        _parse_collection(bad)


# 合集适配器主体
@pytest.mark.asyncio
async def test_collection_enumerate_maps_items(monkeypatch):
    captured: dict = {}

    async def fake_enum_coll(mid, series_id, is_season, cookies=None):
        captured.update(mid=mid, series_id=series_id,
                        is_season=is_season, cookies=cookies)
        return "合集·教程", [
            {"bvid": "BV1ddddddddd", "title": "P1"},
            {"bvid": "BV1eeeeeeeee", "title": " P2 "},
            {"title": "无 bvid"},  # 跳过
        ]

    monkeypatch.setattr("shared.bili_space.enumerate_collection", fake_enum_coll)

    title, items = await enumerate_bilibili_collection(
        "100:season:200", SourceContext(bili_cookies="CK"),
    )

    assert captured == {"mid": "100", "series_id": "200",
                        "is_season": True, "cookies": "CK"}
    assert title == "合集·教程"
    assert [i.item_id for i in items] == ["BV1ddddddddd", "BV1eeeeeeeee"]
    assert items[1].title == "P2"  # strip
    assert items[0].url == "https://www.bilibili.com/video/BV1ddddddddd"
    assert all(i.content_type == "video" for i in items)


@pytest.mark.asyncio
async def test_collection_series_url_is_not_season(monkeypatch):
    captured: dict = {}

    async def fake_enum_coll(mid, series_id, is_season, cookies=None):
        captured["is_season"] = is_season
        return None, []

    monkeypatch.setattr("shared.bili_space.enumerate_collection", fake_enum_coll)
    await enumerate_bilibili_collection(
        "https://space.bilibili.com/9/channel/seriesdetail?sid=8",
        SourceContext(),
    )
    assert captured["is_season"] is False


# 注册 / 分派
def test_registered_in_table():
    from shared.subscriptions.base import SOURCE_ADAPTERS, source_label

    assert SOURCE_ADAPTERS.get("bilibili_fav") is enumerate_bilibili_fav
    assert SOURCE_ADAPTERS.get("bilibili_collection") is enumerate_bilibili_collection
    # 两者命名标签都收敛到 bilibili
    assert source_label("bilibili_fav") == "bilibili"
    assert source_label("bilibili_collection") == "bilibili"


@pytest.mark.asyncio
async def test_dispatch_fav_via_enumerate_source(monkeypatch):
    from shared.subscriptions.base import enumerate_source

    async def fake_enum_fav(media_id, cookies=None):
        return "夹A", [{"bvid": "BV1fffffffff", "title": "t"}]

    monkeypatch.setattr("shared.bili_space.enumerate_fav", fake_enum_fav)
    title, items = await enumerate_source("bilibili_fav", "321", SourceContext())
    assert title == "夹A"
    assert [i.item_id for i in items] == ["BV1fffffffff"]


@pytest.mark.asyncio
async def test_dispatch_collection_via_enumerate_source(monkeypatch):
    from shared.subscriptions.base import enumerate_source

    async def fake_enum_coll(mid, series_id, is_season, cookies=None):
        return "集B", [{"bvid": "BV1ggggggggg", "title": "t"}]

    monkeypatch.setattr("shared.bili_space.enumerate_collection", fake_enum_coll)
    title, items = await enumerate_source(
        "bilibili_collection", "5:series:6", SourceContext(),
    )
    assert title == "集B"
    assert [i.item_id for i in items] == ["BV1ggggggggg"]
