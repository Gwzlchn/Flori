"""YouTube source-adapter,支持频道和播放列表逐视频枚举。

用 yt-dlp 子进程 `--flat-playlist --dump-json` 浅枚举频道投稿(不深解析每条,快)。
source_id 可以是频道页 URL(/@handle、/channel/UC...、/c/...)、youtu.be/频道主页,
也可以是裸 handle(@xxx)或裸频道 id(UC...);统一规整为频道 /videos 标签 URL 后枚举。

下载链路已支持 youtube(yt-dlp,cookies 经中心分发注入,见 steps/common/step_01_download.py),
故每个视频 item 的 url 走标准 watch 链接即可,content_type 固定 video。

去重在 sync_collection 层按 ingested_item_ids 做,本适配器只枚举全集、不自去重。
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# yt-dlp 浅枚举每页/每条投稿可能很多,留足超时;频道页投稿数大时 --flat-playlist 仍较快。
_YT_DLP_TIMEOUT_SEC = 180


def _normalize_channel_url(source_id: str) -> str:
    """把各种频道标识规整为 yt-dlp 可枚举的频道视频列表 URL。

    支持:
      - 完整频道 URL:https://www.youtube.com/@handle、/channel/UC...、/c/name、/user/name
      - 裸 handle:@handle 或 handle(无 @ 前缀的也按 handle 处理)
      - 裸频道 id:UC 开头 24 位
    规整时统一加 /videos 后缀,只枚举投稿(避开频道首页混入的 shorts/直播聚合块);
    已自带 /videos /streams /shorts 等 tab 的 URL 保持原样。
    """
    sid = (source_id or "").strip()
    if not sid:
        return sid

    # 已是 http(s) URL:只接受频道形态,避免 playlist/watch 被错建成频道订阅。
    if re.match(r"https?://", sid):
        parsed = urlparse(sid)
        if (parsed.hostname or "").lower() not in _YOUTUBE_HOSTS or not re.match(
            r"^/(?:@[^/]+|channel/[^/]+|c/[^/]+|user/[^/]+)(?:/(?:videos|streams|shorts|playlists|featured|community))?/?$",
            parsed.path,
            re.IGNORECASE,
        ):
            raise ValueError("invalid YouTube channel source")
        return _ensure_videos_tab(sid)

    # 裸频道 id(UC + 22 位)→ /channel/<id>/videos
    if re.fullmatch(r"UC[\w-]{22}", sid):
        return f"https://www.youtube.com/channel/{sid}/videos"

    # 裸 handle(@xxx 或 xxx)→ /@handle/videos
    handle = sid.lstrip("@")
    return f"https://www.youtube.com/@{handle}/videos"


_PLAYLIST_ID_RE = re.compile(r"[A-Za-z0-9_-]{12,128}")
_YOUTUBE_HOSTS = {
    "youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be",
}


def _normalize_playlist_url(source_id: str) -> str:
    """把 playlist URL 或裸 ID 规整为稳定的 YouTube playlist URL。

    watch/youtu.be 链接只有带 list 参数时才接受。任意外站 URL、缺 list 参数和可疑裸值均拒绝,
    避免 youtube_playlist 适配器被借作通用 yt-dlp URL 执行入口。
    """
    sid = (source_id or "").strip()
    playlist_id = ""
    if sid.startswith(("http://", "https://")):
        parsed = urlparse(sid)
        if (parsed.hostname or "").lower() not in _YOUTUBE_HOSTS:
            raise ValueError("invalid YouTube playlist source")
        playlist_id = (parse_qs(parsed.query).get("list") or [""])[0].strip()
    elif _PLAYLIST_ID_RE.fullmatch(sid):
        playlist_id = sid
    if not _PLAYLIST_ID_RE.fullmatch(playlist_id):
        raise ValueError("invalid YouTube playlist source")
    return f"https://www.youtube.com/playlist?list={playlist_id}"


_TAB_RE = re.compile(r"/(videos|streams|shorts|playlists|featured|community)/?$", re.I)


def _ensure_videos_tab(url: str) -> str:
    """频道主页 URL 末尾补 /videos;已带 tab(videos/streams/...)或非频道页则原样返回。"""
    u = url.rstrip("/")
    # 已经指向某个 tab → 原样
    if _TAB_RE.search(u + "/"):
        return u
    # 仅对频道页形态(/@handle、/channel/UC...、/c/x、/user/x)补 /videos;
    # 其它(如直接给 playlist?list= / watch?v=)不动,交 yt-dlp 自行处理。
    if re.search(r"youtube\.com/(@[^/]+|channel/[^/]+|c/[^/]+|user/[^/]+)$", u, re.I):
        return u + "/videos"
    return u


def _run_yt_dlp(args: list[str], timeout: int = _YT_DLP_TIMEOUT_SEC) -> str:
    """跑 yt-dlp 子进程,返回 stdout(文本)。失败抛 CalledProcessError/TimeoutExpired。

    单独抽成模块级函数,便于单测经模块属性 monkeypatch
    (monkeypatch.setattr("shared.subscriptions.youtube._run_yt_dlp", fake)),
    无需真正起子进程或联网。
    """
    import subprocess

    proc = subprocess.run(
        ["yt-dlp", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=True,
    )
    return proc.stdout


def _parse_entries(stdout: str) -> tuple[str | None, list[dict]]:
    """解析 --dump-json 的逐行 JSON(每行一个 entry)。返回 (channel_title, [entry dict])。

    --flat-playlist 下每行是一个浅 entry,常见字段:
      id / title / url / channel / uploader / channel_id / playlist_title ...
    频道名优先取 entry 的 channel,退 uploader,再退 playlist_title / playlist。
    非 JSON 行(yt-dlp 的告警/进度有时混入 stdout)跳过,容错。
    """
    channel_title: str | None = None
    entries: list[dict] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(obj, dict):
            continue
        entries.append(obj)
        if channel_title is None:
            channel_title = (
                obj.get("channel")
                or obj.get("uploader")
                or obj.get("playlist_title")
                or obj.get("playlist")
                or None
            )
    return channel_title, entries


def _watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


from shared.subscriptions.base import (
    SourceContext,
    SourceItem,
    register,
    register_source_id_normalizer,
)

register_source_id_normalizer("youtube_playlist", _normalize_playlist_url)


def _validate_channel_source_id(source_id: str) -> str:
    """建集合前验证频道形态,但保留既有 source_id 表示和 collection ID。"""
    _normalize_channel_url(source_id)
    return source_id


register_source_id_normalizer("youtube_channel", _validate_channel_source_id)


async def _enumerate_youtube_url(
    source_url: str, ctx: SourceContext,
) -> tuple[str | None, list[dict]]:
    """用统一 cookies 与 yt-dlp 参数浅枚举一个 YouTube 聚合 URL。"""
    args = [
        "--flat-playlist",
        "--dump-json",
        "--ignore-errors",
        "--no-warnings",
    ]
    import tempfile
    cookies_text = ""
    if ctx.db is not None:
        cookies_text = (await asyncio.to_thread(
            ctx.db.get_credential, "youtube_cookies") or "").strip()
    cookies_file: str | None = None
    try:
        if cookies_text:
            with tempfile.NamedTemporaryFile(
                "w", suffix=".txt", delete=False, encoding="utf-8"
            ) as f:
                f.write(cookies_text + "\n")
                cookies_file = f.name
            args += ["--cookies", cookies_file]
        args += ["--", source_url]
        import shared.subscriptions.youtube as _self

        stdout = await asyncio.to_thread(_self._run_yt_dlp, args)
    finally:
        if cookies_file:
            Path(cookies_file).unlink(missing_ok=True)
    return _parse_entries(stdout)


def _source_items(entries: list[dict]) -> list[SourceItem]:
    """把 yt-dlp 浅条目转为稳定 watch URL,并按 video ID 保序去重。"""
    items: list[SourceItem] = []
    seen: set[str] = set()
    for entry in entries:
        video_id = entry.get("id")
        if not video_id or video_id in seen:
            continue
        seen.add(video_id)
        items.append(SourceItem(
            item_id=video_id,
            title=(entry.get("title") or "").strip(),
            url=_watch_url(video_id),
            content_type="video",
        ))
    return items


@register("youtube_channel")
async def enumerate_youtube_channel(
    source_id: str, ctx: SourceContext,
) -> tuple[str | None, list[SourceItem]]:
    """枚举某 YouTube 频道/用户的全部投稿 → (频道名, [SourceItem(video)])。

    流程:规整 source_id 为频道视频列表 URL → yt-dlp --flat-playlist --dump-json 浅枚举
    → 逐行解析 → 每条投稿映射为一个 video SourceItem(item_id=videoId,
    url=https://www.youtube.com/watch?v=<id>)。

    yt-dlp 是阻塞子进程,放进线程池避免堵塞事件循环。经 _run_yt_dlp 模块属性调用,
    便于单测 monkeypatch(不联网)。频道名拿不到回退 None(命名层用 source_id 兜底)。
    """
    channel_url = _normalize_channel_url(source_id)
    if not channel_url:
        return None, []

    channel_title, entries = await _enumerate_youtube_url(channel_url, ctx)
    return channel_title, _source_items(entries)


@register("youtube_playlist")
async def enumerate_youtube_playlist(
    source_id: str, ctx: SourceContext,
) -> tuple[str | None, list[SourceItem]]:
    """枚举一个 YouTube playlist,每个可识别视频映射为独立 video SourceItem。"""
    playlist_url = _normalize_playlist_url(source_id)
    fallback_title, entries = await _enumerate_youtube_url(playlist_url, ctx)
    playlist_title = next((
        (entry.get("playlist_title") or entry.get("playlist") or "").strip()
        for entry in entries
        if (entry.get("playlist_title") or entry.get("playlist") or "").strip()
    ), None)
    return playlist_title or fallback_title, _source_items(entries)
