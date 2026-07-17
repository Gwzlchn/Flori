"""通用 RSS / Atom source-adapter(source_type='rss')。

source_id 是 feed URL(RSS 或 Atom 均可)。用 feedparser 解析,逐条 entry 映射为
SourceItem。公众号(经 RSSHub / wechat2rss 桥产出的 feed)、博客、arxiv RSS、播客
feed、YouTube 频道 RSS 等都走这里 —— 只要对方吐标准 RSS/Atom。

content_type/document_kind 判定(决定走哪条 pipeline/profile):
  - link 含 arxiv.org                          -> document/research_paper
  - link 含 youtube.com / youtu.be             -> video
  - entry 带 audio enclosure(type 含 audio,
    或 href 后缀属 source_detect.AUDIO_SUFFIXES)  -> audio
  - 否则(普通网页/公众号文章)                  -> document/article

去重键 item_id:优先 entry.id(RSS guid / Atom id,最稳定),回退到 link。
source_title:feed.feed.title(频道/公众号名),拿不到返回 None(命名层回退 source_id)。
"""

from __future__ import annotations

from shared import rss_fetch
from shared.source_detect import AUDIO_SUFFIXES
from shared.subscriptions.base import SourceContext, SourceItem, register


def _is_audio_enclosure(enc: object) -> bool:
    """一个 enclosure(feedparser 的 link/enclosure dict)是否为音频:
    type 含 'audio'(如 audio/mpeg),或 href 后缀属 AUDIO_SUFFIXES。"""
    get = getattr(enc, "get", None)
    if not callable(get):
        return False
    etype = (get("type") or "").lower()
    if "audio" in etype:
        return True
    href = (get("href") or get("url") or "").lower().split("?")[0]
    return href.endswith(AUDIO_SUFFIXES)


def _enc_href(enc: object) -> str:
    """从一个 enclosure(feedparser 的 link/enclosure dict)取音频直链 href。"""
    get = getattr(enc, "get", None)
    if not callable(get):
        return ""
    return (get("href") or get("url") or "").strip()


def _audio_enclosure_href(entry: object) -> str | None:
    """取 entry 第一个音频 enclosure 的真链 href(enclosures 优先,再 links rel=enclosure)。
    无音频 enclosure → None。供 audio 条目把页面 link 换成音频直链喂下载步。"""
    get = getattr(entry, "get", None)
    if not callable(get):
        return None
    for enc in get("enclosures", None) or []:
        if _is_audio_enclosure(enc) and _enc_href(enc):
            return _enc_href(enc)
    for lk in get("links", None) or []:
        lget = getattr(lk, "get", None)
        if callable(lget) and (lget("rel") or "") == "enclosure" \
                and _is_audio_enclosure(lk) and _enc_href(lk):
            return _enc_href(lk)
    return None


def _entry_has_audio(entry: object) -> bool:
    """entry 是否带音频 enclosure。feedparser 把 enclosure 放进 entry.enclosures,
    同时 links 里 rel=='enclosure' 的项也算(不同源结构不一,两处都查)。"""
    return _audio_enclosure_href(entry) is not None


def _content_type_for(link: str, entry: object) -> tuple[str, str | None]:
    """按平台与 enclosure 同时返回顶层类型和文档体裁。"""
    low = (link or "").lower()
    if "arxiv.org" in low:
        return "document", "research_paper"
    if "youtube.com" in low or "youtu.be" in low:
        return "video", None
    if _entry_has_audio(entry):
        return "audio", None
    return "document", "article"


@register("rss")
async def enumerate_rss(
    source_id: str, ctx: SourceContext,
) -> tuple[str | None, list[SourceItem]]:
    """枚举一个 RSS/Atom feed(source_id=feed URL)的全部 entry → SourceItem 列表。

    经 rss_fetch.parse_feed(模块属性调用,便于测试 monkeypatch)解析,内部走
    feedparser。返回 (source_title, items);不做去重(去重在 sync_collection 层)。"""
    feed = rss_fetch.parse_feed(source_id)

    feed_meta = getattr(feed, "feed", None)
    source_title = None
    if feed_meta is not None:
        get_meta = getattr(feed_meta, "get", None)
        if callable(get_meta):
            source_title = (get_meta("title") or "").strip() or None

    items: list[SourceItem] = []
    for entry in getattr(feed, "entries", None) or []:
        get = getattr(entry, "get", None)
        if not callable(get):
            continue
        link = (get("link") or "").strip()
        # item_id:优先稳定的 guid/id,回退 link。两者皆空则跳过(无去重键)。
        item_id = (get("id") or "").strip() or link
        if not item_id:
            continue
        content_type, document_kind = _content_type_for(link, entry)
        # audio 条目 url 用音频 enclosure 真链而非页面 link:否则下载步 curl 到的是网页 HTML,
        # whisper 无音源会挂。enclosure 缺失时回退页面 link(下载步再 best-effort 解析)。
        url = link or item_id
        if content_type == "audio":
            url = _audio_enclosure_href(entry) or url
        items.append(SourceItem(
            item_id=item_id,
            title=(get("title") or "").strip(),
            url=url,
            content_type=content_type,
            document_kind=document_kind,
        ))
    return source_title, items
