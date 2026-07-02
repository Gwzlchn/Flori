"""Substack 平台提取器。

semianalysis.com 等自定义域名 + *.substack.com 都跑在 substack 平台上,故按页面特征匹配
(substackcdn / Image2ToDOM),不按域名,一个 extractor 通吃整个平台。
注:substack 没有 <meta generator="Substack">,别靠它判;substackcdn 出现频次最高最可靠。

正文图在 substack 里可靠地包在 <figure> 内 → 直接按 figure 抽,比通用启发更稳:
天然排除头像/logo/订阅条(它们不在 figure),无需依赖尺寸/链接启发。
"""

from __future__ import annotations

import re

from .base import ArticleExtractor, generic_content_image_urls


def substack_figure_images(html: str) -> list[str]:
    """抽 substack 正文图:每个 <figure> 内首个 <img src>,去 data:/svg。
    按 URL 去重:substack SSR + 水合会重复出现同图。"""
    urls: list[str] = []
    seen: set[str] = set()
    for fig in re.findall(r'<figure\b[^>]*>(.*?)</figure>', html, re.S | re.I):
        m = re.search(r'<img\b[^>]*\bsrc=["\']([^"\']+)', fig, re.I)
        if not m:
            continue
        src = m.group(1).strip()
        low = src.lower()
        if src.startswith("data:") or ".svg" in low:
            continue
        key = src.split("?")[0]
        if key not in seen:
            seen.add(key)
            urls.append(src)
    return urls


class SubstackExtractor(ArticleExtractor):
    name = "substack"

    def matches(self, url: str, html: str) -> bool:
        return "substackcdn.com" in html or "Image2ToDOM" in html

    def content_image_urls(self, html: str) -> list[str]:
        # figure 抽不到(罕见模板)再回退通用启发,保证不漏。
        return substack_figure_images(html) or generic_content_image_urls(html)
