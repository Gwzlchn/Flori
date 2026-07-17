"""book_toc source-adapter:在线书目录页 → 章节列表。

book = collection(source_type=book_toc,source_id=目录 URL)+ 每章一个 document job。
首个实现认 jupyter-book / sphinx 结构(QuantEcon 系列即此):目录页 nav 里的
`<a class="reference internal" href="chapter.html">` 有序即章序。

章数上限:env BOOK_MAX_CHAPTERS(默认 5,先试点再放全书)。collection 行无配置字段,
env 是当前最轻的传参形态;要扩全书时改 env 重 sync 即可(ingested 去重保证不重复建章)。

枚举幂等:item_id=章 slug(href 去 .html),同章多次 sync 同 id → sync 层 ingested 去重。
章序 = items 顺序(toc 文档序);顺序投递由 collections.sync(defer)+ scheduler(前章完成
触发下一章)实现,见 api/routes/collections.py 与 scheduler 的 book 投递器。
"""

from __future__ import annotations

import os
import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

from .base import SourceContext, SourceItem, register

_DEFAULT_MAX = 5


class _TocParser(HTMLParser):
    """抽 sphinx/jupyter-book 目录链接:nav/sidebar 中 class 含 reference internal 的 <a>。
    顺带抓 <title> 作书名。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []   # (href, text) 有序
        self._cur_href: str | None = None
        self._buf: list[str] = []
        self._in_title = False
        self.title = ""

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if tag == "title":
            self._in_title = True
        if tag == "a":
            cls = (d.get("class") or "")
            href = d.get("href") or ""
            if "reference" in cls and "internal" in cls and href and not href.startswith("#"):
                self._cur_href = href
                self._buf = []

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if self._cur_href is not None:
            self._buf.append(data)

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag == "a" and self._cur_href is not None:
            text = " ".join("".join(self._buf).split())
            self.links.append((self._cur_href, text))
            self._cur_href = None


def _fetch(url: str, timeout: int = 60) -> str | None:
    """urllib 抓目录页(尊重代理 env);失败返 None。"""
    import urllib.request
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", errors="replace")
    except Exception:
        return None


def parse_toc(html: str, base_url: str, max_chapters: int) -> tuple[str | None, list[SourceItem]]:
    """目录 HTML → (书名, 章节 SourceItem 列表,文档序去重,≤max_chapters)。纯函数供单测。"""
    p = _TocParser()
    p.feed(html)
    base_host = urlparse(base_url).netloc
    items: list[SourceItem] = []
    seen: set[str] = set()
    for href, text in p.links:
        url = urljoin(base_url, href)
        u = urlparse(url)
        if u.netloc != base_host:          # 外链(GitHub/下载徽标等)不是章
            continue
        slug = re.sub(r"\.html?$", "", u.path.strip("/").split("/")[-1] or "")
        if not slug or slug in seen or slug in ("index", "intro-toc", "genindex", "search"):
            continue
        seen.add(slug)
        items.append(SourceItem(
            item_id=slug,
            title=text or slug,
            url=url,
            content_type="document",
            document_kind="book_chapter",
        ))
        if len(items) >= max_chapters:
            break
    title = (p.title or "").split("—")[0].strip() or None
    return title, items


_META_REFRESH_RE = re.compile(
    r'<meta[^>]+http-equiv=["\']?Refresh["\']?[^>]*url=([^"\'>\s]+)', re.IGNORECASE)


@register("book_toc")
async def enum_book_toc(source_id: str, ctx: SourceContext) -> tuple[str | None, list[SourceItem]]:
    """source_id = 书目录 URL(如 https://intro.quantecon.org/)。
    根路径常见 meta-refresh 跳真目录页(QuantEcon `/` → intro.html),非 HTTP 重定向,手动跟(≤2 跳)。"""
    import asyncio
    url = source_id
    html = None
    for _ in range(3):
        html = await asyncio.to_thread(_fetch, url)
        if not html:
            return None, []
        m = _META_REFRESH_RE.search(html) if len(html) < 2048 else None
        if not m:
            break
        url = urljoin(url, m.group(1))
    max_ch = int(os.environ.get("BOOK_MAX_CHAPTERS", str(_DEFAULT_MAX)))
    return parse_toc(html, url, max_ch)
