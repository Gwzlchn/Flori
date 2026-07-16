"""youtube_channel source-adapter 单测(不依赖网络:mock yt-dlp 子进程输出)。

覆盖:
  - URL 规整(/@handle、/channel/UC...、/c/...、裸 handle、裸 id、已带 tab)
  - --flat-playlist --dump-json 逐行解析 → SourceItem(item_id/url/content_type)
  - 频道名提取(channel / uploader / playlist_title 回退)
  - 容错:空行/非 JSON 行跳过,无 id 条目跳过,重复 id 去重
  - 注册:@register('youtube_channel') 进入 SOURCE_ADAPTERS / enumerate_source 可分派
"""

from __future__ import annotations

import json

import pytest

import shared.subscriptions.youtube as yt
from shared.subscriptions.base import SourceContext, SourceItem
from shared.subscriptions.youtube import (
    _ensure_videos_tab,
    _normalize_channel_url,
    _normalize_playlist_url,
    _parse_entries,
    enumerate_youtube_channel,
    enumerate_youtube_playlist,
)


# URL 规整
@pytest.mark.parametrize(
    "source_id, expected",
    [
        ("https://www.youtube.com/@SomeHandle",
         "https://www.youtube.com/@SomeHandle/videos"),
        ("https://www.youtube.com/channel/UCabcdefghijklmnopqrstuv",
         "https://www.youtube.com/channel/UCabcdefghijklmnopqrstuv/videos"),
        ("https://www.youtube.com/c/SomeName",
         "https://www.youtube.com/c/SomeName/videos"),
        ("https://www.youtube.com/user/SomeUser",
         "https://www.youtube.com/user/SomeUser/videos"),
        # 已带 tab → 不重复补
        ("https://www.youtube.com/@SomeHandle/videos",
         "https://www.youtube.com/@SomeHandle/videos"),
        ("https://www.youtube.com/@SomeHandle/streams",
         "https://www.youtube.com/@SomeHandle/streams"),
        # 裸 handle
        ("@BareHandle", "https://www.youtube.com/@BareHandle/videos"),
        ("BareHandle", "https://www.youtube.com/@BareHandle/videos"),
        # 裸频道 id(UC + 22)
        ("UCabcdefghijklmnopqrstuv",
         "https://www.youtube.com/channel/UCabcdefghijklmnopqrstuv/videos"),
        # 尾部斜杠规整
        ("https://www.youtube.com/@SomeHandle/",
         "https://www.youtube.com/@SomeHandle/videos"),
    ],
)
def test_normalize_channel_url(source_id, expected):
    assert _normalize_channel_url(source_id) == expected


def test_normalize_channel_url_empty():
    assert _normalize_channel_url("") == ""
    assert _normalize_channel_url("   ") == ""


@pytest.mark.parametrize("source_id", [
    "https://www.youtube.com/playlist?list=PL1234567890abcdef",
    "https://www.youtube.com/watch?v=abcdefghijk",
    "https://example.com/@channel",
])
def test_normalize_channel_url_rejects_non_channel_url(source_id):
    with pytest.raises(ValueError, match="invalid YouTube channel"):
        _normalize_channel_url(source_id)


@pytest.mark.parametrize(
    "source_id, expected",
    [
        ("PL1234567890abcdef", "https://www.youtube.com/playlist?list=PL1234567890abcdef"),
        ("EC1234567890abcdef", "https://www.youtube.com/playlist?list=EC1234567890abcdef"),
        ("https://www.youtube.com/playlist?list=PLabc_123-xyz",
         "https://www.youtube.com/playlist?list=PLabc_123-xyz"),
        ("https://youtube.com/watch?v=abcdefghijk&list=PLabc_123-xyz&index=2",
         "https://www.youtube.com/playlist?list=PLabc_123-xyz"),
        ("https://youtu.be/abcdefghijk?list=PLabc_123-xyz",
         "https://www.youtube.com/playlist?list=PLabc_123-xyz"),
    ],
)
def test_normalize_playlist_url(source_id, expected):
    assert _normalize_playlist_url(source_id) == expected


@pytest.mark.parametrize("source_id", [
    "", "   ", "https://example.com/playlist?list=PLabc_123-xyz",
    "https://www.youtube.com/playlist", "not a playlist id", "-dangerous",
    "abcdefghijk", "PL123456789",
])
def test_normalize_playlist_url_rejects_invalid_source(source_id):
    with pytest.raises(ValueError, match="invalid YouTube playlist"):
        _normalize_playlist_url(source_id)


# 逐行 JSON 解析
def test_parse_entries_basic():
    lines = [
        json.dumps({"id": "vid1", "title": "T1", "channel": "酷频道"}),
        json.dumps({"id": "vid2", "title": "T2", "channel": "酷频道"}),
    ]
    title, entries = _parse_entries("\n".join(lines))
    assert title == "酷频道"
    assert [e["id"] for e in entries] == ["vid1", "vid2"]


def test_parse_entries_channel_fallback_uploader():
    lines = [json.dumps({"id": "v", "title": "T", "uploader": "上传者名"})]
    title, _ = _parse_entries("\n".join(lines))
    assert title == "上传者名"


def test_parse_entries_channel_fallback_playlist_title():
    lines = [json.dumps({"id": "v", "title": "T", "playlist_title": "播放列表名"})]
    title, _ = _parse_entries("\n".join(lines))
    assert title == "播放列表名"


def test_parse_entries_skips_blank_and_non_json():
    raw = "\n".join([
        "",
        "WARNING: something not json",
        json.dumps({"id": "vid1", "title": "T1", "channel": "C"}),
        "   ",
        "[generic] some progress line",
        json.dumps({"id": "vid2", "title": "T2", "channel": "C"}),
    ])
    title, entries = _parse_entries(raw)
    assert title == "C"
    assert [e["id"] for e in entries] == ["vid1", "vid2"]


def test_parse_entries_empty():
    title, entries = _parse_entries("")
    assert title is None
    assert entries == []


# 适配器主体(mock _run_yt_dlp,无子进程/网络)
def _fake_stdout(entries: list[dict]) -> str:
    return "\n".join(json.dumps(e) for e in entries)


@pytest.mark.asyncio
async def test_enumerate_maps_items(monkeypatch):
    captured: dict = {}

    def fake_run(args, timeout=None):
        captured["args"] = args
        return _fake_stdout([
            {"id": "aaa", "title": " 第一集 ", "channel": "测试频道"},
            {"id": "bbb", "title": "第二集", "channel": "测试频道"},
        ])

    # 适配器内部经模块属性调用 _run_yt_dlp,monkeypatch 模块属性才能命中假实现。
    monkeypatch.setattr("shared.subscriptions.youtube._run_yt_dlp", fake_run)

    title, items = await enumerate_youtube_channel(
        "https://www.youtube.com/@测试", SourceContext(),
    )

    assert title == "测试频道"
    assert all(isinstance(i, SourceItem) for i in items)
    assert [i.item_id for i in items] == ["aaa", "bbb"]
    assert items[0].title == "第一集"  # 已 strip
    assert items[0].url == "https://www.youtube.com/watch?v=aaa"
    assert items[1].url == "https://www.youtube.com/watch?v=bbb"
    assert all(i.content_type == "video" for i in items)

    # 传给 yt-dlp 的参数:浅枚举 + dump-json + -- 分隔 + 规整后的 /videos URL
    args = captured["args"]
    assert "--flat-playlist" in args
    assert "--dump-json" in args
    assert "--" in args
    assert args[-1] == "https://www.youtube.com/@测试/videos"


@pytest.mark.asyncio
async def test_enumerate_playlist_maps_items_and_title(monkeypatch):
    captured: dict = {}

    def fake_run(args, timeout=None):
        captured["args"] = args
        return _fake_stdout([
            {"id": "aaa", "title": " 第一课 ", "playlist_title": "CS336 2026"},
            {"id": "bbb", "title": "第二课", "playlist_title": "CS336 2026"},
            {"id": "aaa", "title": "重复", "playlist_title": "CS336 2026"},
            {"title": "不可用条目", "playlist_title": "CS336 2026"},
        ])

    monkeypatch.setattr("shared.subscriptions.youtube._run_yt_dlp", fake_run)
    title, items = await enumerate_youtube_playlist(
        "https://www.youtube.com/watch?v=abcdefghijk&list=PLabc_123-xyz",
        SourceContext(),
    )

    assert title == "CS336 2026"
    assert [item.item_id for item in items] == ["aaa", "bbb"]
    assert [item.title for item in items] == ["第一课", "第二课"]
    assert all(item.content_type == "video" for item in items)
    assert captured["args"][-1] == "https://www.youtube.com/playlist?list=PLabc_123-xyz"
    assert "--flat-playlist" in captured["args"]
    assert "--ignore-errors" in captured["args"]


@pytest.mark.asyncio
async def test_enumerate_dedup_and_skip_missing_id(monkeypatch):
    def fake_run(args, timeout=None):
        return _fake_stdout([
            {"id": "x", "title": "A", "channel": "C"},
            {"id": "x", "title": "A-dup", "channel": "C"},   # 重复 id → 去重
            {"title": "no-id"},                               # 无 id → 跳过
            {"id": "y", "title": "B", "channel": "C"},
        ])

    monkeypatch.setattr("shared.subscriptions.youtube._run_yt_dlp", fake_run)
    title, items = await enumerate_youtube_channel("@chan", SourceContext())
    assert title == "C"
    assert [i.item_id for i in items] == ["x", "y"]


@pytest.mark.asyncio
async def test_enumerate_empty_source_id(monkeypatch):
    called = {"hit": False}

    def fake_run(args, timeout=None):
        called["hit"] = True
        return ""

    monkeypatch.setattr("shared.subscriptions.youtube._run_yt_dlp", fake_run)
    title, items = await enumerate_youtube_channel("   ", SourceContext())
    assert title is None
    assert items == []
    assert called["hit"] is False  # 空 source_id 直接短路,不起子进程


@pytest.mark.asyncio
async def test_enumerate_no_channel_name_returns_none(monkeypatch):
    def fake_run(args, timeout=None):
        # entry 无 channel/uploader/playlist_title → 频道名回退 None
        return _fake_stdout([{"id": "z", "title": "T"}])

    monkeypatch.setattr("shared.subscriptions.youtube._run_yt_dlp", fake_run)
    title, items = await enumerate_youtube_channel("@x", SourceContext())
    assert title is None
    assert [i.item_id for i in items] == ["z"]


# 注册
def test_registered_in_table():
    from shared.subscriptions.base import SOURCE_ADAPTERS
    assert SOURCE_ADAPTERS.get("youtube_channel") is enumerate_youtube_channel
    assert SOURCE_ADAPTERS.get("youtube_playlist") is enumerate_youtube_playlist


@pytest.mark.asyncio
async def test_dispatch_via_enumerate_source(monkeypatch):
    from shared.subscriptions.base import enumerate_source

    def fake_run(args, timeout=None):
        return _fake_stdout([{"id": "q", "title": "T", "channel": "频道Q"}])

    monkeypatch.setattr("shared.subscriptions.youtube._run_yt_dlp", fake_run)
    title, items = await enumerate_source(
        "youtube_channel", "@q", SourceContext(),
    )
    assert title == "频道Q"
    assert [i.item_id for i in items] == ["q"]


@pytest.mark.asyncio
async def test_dispatch_youtube_playlist(monkeypatch):
    from shared.subscriptions.base import enumerate_source

    def fake_run(args, timeout=None):
        return _fake_stdout([
            {"id": "q", "title": "T", "playlist_title": "课程列表"},
        ])

    monkeypatch.setattr("shared.subscriptions.youtube._run_yt_dlp", fake_run)
    title, items = await enumerate_source(
        "youtube_playlist", "PLabc_123-xyz", SourceContext(),
    )
    assert title == "课程列表"
    assert [item.item_id for item in items] == ["q"]


@pytest.mark.asyncio
async def test_enumerate_source_unknown_type_raises():
    # 契约:未知 source_type 抛 ValueError,调用方转 4xx/记日志。
    from shared.subscriptions.base import enumerate_source

    with pytest.raises(ValueError, match="unsupported source_type"):
        await enumerate_source("no_such_source", "x", SourceContext())


# _ensure_videos_tab 契约:频道页补 /videos,watch/playlist/已带 tab 透传
@pytest.mark.parametrize("url,expected", [
    # 频道页形态 → 补 /videos
    ("https://www.youtube.com/@chan", "https://www.youtube.com/@chan/videos"),
    ("https://www.youtube.com/channel/UCabc", "https://www.youtube.com/channel/UCabc/videos"),
    # 已带 tab → 原样
    ("https://www.youtube.com/@chan/streams", "https://www.youtube.com/@chan/streams"),
    ("https://www.youtube.com/@chan/videos", "https://www.youtube.com/@chan/videos"),
    # watch/playlist 非频道页 → 透传不动,交 yt-dlp 自处理
    ("https://www.youtube.com/watch?v=abc", "https://www.youtube.com/watch?v=abc"),
    ("https://www.youtube.com/playlist?list=PL123", "https://www.youtube.com/playlist?list=PL123"),
])
def test_ensure_videos_tab(url, expected):
    assert _ensure_videos_tab(url) == expected


@pytest.mark.asyncio
async def test_enumerate_propagates_subprocess_error(monkeypatch):
    # 契约:_run_yt_dlp 失败(check=True → CalledProcessError)如约透传,
    # 由上层转重试/记日志,不被适配器静默吞成空结果。
    import subprocess

    def boom(args, timeout=None):
        raise subprocess.CalledProcessError(1, ["yt-dlp"], stderr="boom")

    monkeypatch.setattr("shared.subscriptions.youtube._run_yt_dlp", boom)
    with pytest.raises(subprocess.CalledProcessError):
        await enumerate_youtube_channel("@x", SourceContext())
