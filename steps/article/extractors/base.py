"""文章内容提取器(extractor)基类 + 通用实现。

个人知识库粘贴任意 URL,站点无限长尾 → 必须有强通用兜底(trafilatura 出正文/元数据)。
站点差异最大的是正文图片(trafilatura 丢图,要从原始 HTML 重抽,各站图片标记不同),其次是作者。
故 extractor 只覆盖这两处差异;正文 markdown / 元数据仍由 step 的 trafilatura 通用逻辑产出。

注册表见 __init__.py:按 matches(url, html) 选站点 extractor,否则 GenericExtractor 兜底。
加新站 = 新建一个 extractor 文件,覆盖 matches() + 需定制的方法,不动通用逻辑。
"""

from __future__ import annotations

import json
import re


def authors_from_jsonld(html: str) -> list[str]:
    """从 <script type="application/ld+json"> 兜底抽 author.name(best-effort)。"""
    out: list[str] = []
    for m in re.finditer(
        r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html, re.S | re.I
    ):
        try:
            obj = json.loads(m.group(1).strip())
        except (json.JSONDecodeError, ValueError):
            continue
        for node in (obj if isinstance(obj, list) else [obj]):
            a = node.get("author") if isinstance(node, dict) else None
            for one in (a if isinstance(a, list) else [a]):
                if isinstance(one, dict) and one.get("name"):
                    out.append(str(one["name"]).strip())
                elif isinstance(one, str) and one.strip():
                    out.append(one.strip())
        if out:
            break
    seen, dedup = set(), []
    for a in out:
        if a not in seen:
            seen.add(a); dedup.append(a)
    return dedup


def authors_from_page_json(html: str) -> list[str]:
    """SPA 页面内嵌 JSON 的 author 兜底:"author":{...,"display_name":"..."} 或 "name":"..."。
    取首个非空。best-effort。"""
    for m in re.finditer(r'"author"\s*:\s*\{(.*?)\}', html, re.S):
        blob = m.group(1)
        nm = re.search(r'"(?:display_name|name)"\s*:\s*"([^"]+)"', blob)
        if nm and nm.group(1).strip():
            return [nm.group(1).strip()]
    return []


def generic_content_image_urls(html: str) -> list[str]:
    """通用正文级图片 URL 提取。滤掉头像/图标/logo/svg、小图(缩略图/相关文章),
    以及促销 banner,即 <a> 链到站外页面的可点图。
    关键:有的站正文图恰恰是 <a href=大图.png><img>(点开看大图),href 指向图片本身的应保留,
    只排除 href 指向页面的促销图。尺寸判据:w_1456 / w/680 / width= 识别宽,h_72 等识别高;
    宽<400,或无宽且高<200,视为非正文。"""
    promo_linked: set[str] = set()
    for a_attrs, img_src in re.findall(
        r'<a\b([^>]*)>\s*(?:<[^/a][^>]*>\s*)*<img\b[^>]*\bsrc=["\']([^"\']+)', html, re.I):
        href_m = re.search(r'\bhref=["\']([^"\']+)', a_attrs, re.I)
        href = (href_m.group(1) if href_m else "").lower()
        is_img_href = bool(href) and (
            "/image/" in href or "substackcdn" in href
            or re.search(r'\.(png|jpe?g|gif|webp)(\?|$)', href))
        if href and not is_img_href:
            promo_linked.add(img_src.strip())

    def _dim(url_pat: str, tag_pat: str, low: str, tag: str) -> int | None:
        m = re.search(url_pat, low)
        if m:
            return int(m.group(1))
        m = re.search(tag_pat, tag, re.I)
        return int(m.group(1)) if m else None

    urls: list[str] = []
    seen: set[str] = set()
    for tag in re.findall(r'<img\b[^>]*>', html, re.I):
        src_m = re.search(r'\bsrc=["\']([^"\']+)["\']', tag, re.I)
        if not src_m:
            continue
        src = src_m.group(1).strip()
        if src in promo_linked:
            continue
        low = src.lower()
        if src.startswith("data:") or any(
            k in low for k in ("avatar", "/logo", "icon", "sprite", "emoji", ".svg", "/badge")
        ):
            continue
        w = _dim(r'[,/_-]w[,/=_](\d+)', r'\bwidth=["\']?(\d+)', low, tag)
        h = _dim(r'[,/_-]h[,/=_](\d+)', r'\bheight=["\']?(\d+)', low, tag)
        if w is not None and w < 400:
            continue
        if w is None and h is not None and h < 200:
            continue
        key = src.split("?")[0]
        if key not in seen:
            seen.add(key)
            urls.append(src)
    return urls


class ArticleExtractor:
    """站点提取器基类。子类覆盖 matches() + 需要定制的 content_image_urls() / authors()。
    默认实现 = 通用启发,没覆盖的方法自动走通用,新 extractor 只写差异。"""

    name = "base"

    def matches(self, url: str, html: str) -> bool:
        return False

    def content_image_urls(self, html: str) -> list[str]:
        return generic_content_image_urls(html)

    def authors(self, html: str) -> list[str]:
        return authors_from_jsonld(html) or authors_from_page_json(html)


class GenericExtractor(ArticleExtractor):
    """通用兜底:覆盖绝大多数没写过适配的站点。matches 恒真 → 放注册表最后。"""

    name = "generic"

    def matches(self, url: str, html: str) -> bool:
        return True
