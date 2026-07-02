"""通用 RSS/Atom source-adapter(shared/subscriptions/rss.py)单测。

不联网:用静态 RSS/Atom XML 字符串喂真实 feedparser,parse 既吃 URL 也吃 XML 串。
覆盖 content_type 四种判定(article / arxiv->paper / youtube->video / audio enclosure),
item_id 回退按 id 优先、缺则 link,另验 source_title 提取。

autouse fixture 把 shared.rss_fetch.parse_feed 换成直接 feedparser.parse,
source_id 就能当 XML 串喂进适配器,不发网络。
"""

from __future__ import annotations

import pytest

feedparser = pytest.importorskip("feedparser")  # 测试镜像经 [api] extra 装(base.Dockerfile)

from shared.subscriptions.base import SourceContext
from shared.subscriptions.rss import enumerate_rss


# 一个 RSS 2.0 feed,4 条 entry 覆盖四种 content_type + guid 回退。
RSS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>测试频道</title>
    <link>https://example.com/</link>
    <item>
      <title>普通文章</title>
      <link>https://blog.example.com/post/1</link>
      <guid>urn:guid:article-1</guid>
    </item>
    <item>
      <title>arxiv 论文</title>
      <link>https://arxiv.org/abs/2301.00001</link>
      <guid>urn:guid:paper-1</guid>
    </item>
    <item>
      <title>youtube 视频</title>
      <link>https://www.youtube.com/watch?v=abc123</link>
      <guid>urn:guid:video-1</guid>
    </item>
    <item>
      <title>播客单集</title>
      <link>https://podcast.example.com/ep/5</link>
      <enclosure url="https://cdn.example.com/ep5.mp3" type="audio/mpeg" length="123"/>
    </item>
  </channel>
</rss>
"""

# Atom feed:验证 Atom 也能解析,且 entry.id 作 item_id、enclosure 经 link rel 判 audio。
ATOM_XML = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Atom 公众号</title>
  <link href="https://wechat.example.com/"/>
  <entry>
    <title>公众号文章</title>
    <id>https://wechat.example.com/s/aaa</id>
    <link href="https://wechat.example.com/s/aaa"/>
  </entry>
  <entry>
    <title>Atom 音频(无 type,仅 .m4a 后缀)</title>
    <id>https://wechat.example.com/s/bbb</id>
    <link href="https://wechat.example.com/s/bbb"/>
    <link rel="enclosure" href="https://cdn.example.com/show.m4a"/>
  </entry>
</feed>
"""


def _feed_of(xml: str):
    """parse_feed 的测试替身:直接 feedparser.parse 给定 XML,不联网。"""
    return feedparser.parse(xml)


@pytest.fixture(autouse=True)
def _patch_parse(monkeypatch):
    # 替换后 parse_feed 收到的 source_id 即测试塞的 XML 串,直接解析。
    monkeypatch.setattr("shared.rss_fetch.parse_feed", _feed_of)


@pytest.mark.asyncio
async def test_rss_content_type_detection():
    title, items = await enumerate_rss(RSS_XML, SourceContext())
    assert title == "测试频道"
    assert len(items) == 4

    by_id = {it.item_id: it for it in items}
    assert by_id["urn:guid:article-1"].content_type == "article"
    assert by_id["urn:guid:paper-1"].content_type == "paper"
    assert by_id["urn:guid:video-1"].content_type == "video"

    # 第 4 条无 guid → item_id 回退为页面 link;audio enclosure(type=audio/mpeg)→ audio。
    # url 用音频 enclosure 真链(而非页面 link),否则下载步 curl 到的是网页 → whisper 挂。
    audio = next(it for it in items if it.title == "播客单集")
    assert audio.content_type == "audio"
    assert audio.item_id == "https://podcast.example.com/ep/5"   # 去重键仍用页面 link
    assert audio.url == "https://cdn.example.com/ep5.mp3"        # 下载用音频真链


@pytest.mark.asyncio
async def test_rss_item_fields():
    _, items = await enumerate_rss(RSS_XML, SourceContext())
    art = next(it for it in items if it.item_id == "urn:guid:article-1")
    assert art.title == "普通文章"
    assert art.url == "https://blog.example.com/post/1"


@pytest.mark.asyncio
async def test_atom_parses_and_audio_by_suffix():
    title, items = await enumerate_rss(ATOM_XML, SourceContext())
    assert title == "Atom 公众号"
    assert len(items) == 2

    by_id = {it.item_id: it for it in items}
    # Atom entry.id 作 item_id
    assert by_id["https://wechat.example.com/s/aaa"].content_type == "article"
    # 仅靠 .m4a 后缀(无 audio type)也判 audio;url = enclosure 真链(非页面)
    bbb = by_id["https://wechat.example.com/s/bbb"]
    assert bbb.content_type == "audio"
    assert bbb.url == "https://cdn.example.com/show.m4a"


@pytest.mark.asyncio
async def test_empty_feed_returns_empty():
    title, items = await enumerate_rss("<rss version='2.0'><channel></channel></rss>",
                                      SourceContext())
    # 空 feed:无 entry,title 为空 → None
    assert items == []
    assert title is None
