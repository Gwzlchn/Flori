"""URL 来源识别与来源页面辅助解析。"""

from __future__ import annotations

import re

from .source_registry import JOB_SOURCE_SPECS, detect_registered_source

# rss.py、subscriptions/local_dir.py 继续从这里导入,值由来源 registry 派生。
AUDIO_SUFFIXES = tuple(JOB_SOURCE_SPECS["podcast"].get("suffixes") or [])


def detect_source(url: str) -> str:
    """判断 URL 或裸标识符(如 BV 号)的来源平台;空值/未识别返回 "other"。"""
    return detect_registered_source(url)


# 页面里可能藏音频直链的几处:og:audio meta、<audio src>、<source src>、<enclosure url>、
# 裸 <a href="*.mp3">。从最权威(og:audio)到最弱(裸链)依次尝试。
_OG_AUDIO_RE = re.compile(
    r'<meta[^>]+(?:property|name)=["\']og:audio(?::secure_url)?["\'][^>]*'
    r'content=["\']([^"\']+)["\']',
    re.I,
)
_OG_AUDIO_RE2 = re.compile(  # content 在 property 之前的写法
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*'
    r'(?:property|name)=["\']og:audio(?::secure_url)?["\']',
    re.I,
)
_AUDIO_SRC_RE = re.compile(r'<(?:audio|source)[^>]+src=["\']([^"\']+)["\']', re.I)
_ENCLOSURE_RE = re.compile(r'<enclosure[^>]+url=["\']([^"\']+)["\']', re.I)
_HREF_RE = re.compile(r'href=["\']([^"\']+)["\']', re.I)


def extract_audio_enclosure(html: str, base_url: str = "") -> str | None:
    """从一段网页/RSS HTML 里 best-effort 解析出音频直链,供播客页面 URL 回退取真链。

    顺序:og:audio meta > <audio>/<source src> > <enclosure url> > 裸 <a href="*.mp3">。
    相对链接用 base_url 解析为绝对。挑出第一个看着像音频(后缀属 AUDIO_SUFFIXES,或
    og:audio/enclosure 这类语义明确的标签)的链接;找不到返回 None。不发网络、纯解析,便于测。"""
    from urllib.parse import urljoin

    if not html:
        return None

    def _abs(u: str) -> str:
        u = (u or "").strip()
        return urljoin(base_url, u) if base_url else u

    def _is_audio(u: str) -> bool:
        return u.lower().split("?")[0].endswith(AUDIO_SUFFIXES)

    # 1) og:audio:语义最权威,即便无音频后缀也采信(部分 CDN 直链不带扩展名)。
    for rx in (_OG_AUDIO_RE, _OG_AUDIO_RE2):
        m = rx.search(html)
        if m and m.group(1).strip():
            return _abs(m.group(1))

    # 2) <audio>/<source src>:有音频后缀才采信(避免误取封面等)。
    for m in _AUDIO_SRC_RE.finditer(html):
        cand = _abs(m.group(1))
        if _is_audio(cand):
            return cand
    # 无后缀但页面只有一个 <audio src> 时也采信(同 og:audio 的容忍)。
    first_src = _AUDIO_SRC_RE.search(html)
    if first_src and first_src.group(1).strip():
        return _abs(first_src.group(1))

    # 3) <enclosure url>:RSS 风格(页面其实是 feed 时)。
    for m in _ENCLOSURE_RE.finditer(html):
        cand = _abs(m.group(1))
        if _is_audio(cand):
            return cand

    # 4) 裸 <a href="*.mp3">:最弱兜底。
    for m in _HREF_RE.finditer(html):
        cand = _abs(m.group(1))
        if _is_audio(cand):
            return cand
    return None


def extract_bilibili_bvid(url: str) -> str | None:
    """从 URL 或纯 BV 号提取 BV ID。"""
    m = re.search(r"(BV[a-zA-Z0-9]{10})", url)
    return m.group(1) if m else None


# arXiv ID:新式 2301.00001(可带 vN 版本)或旧式 hep-th/9901001 / math.AG/0601001。
# abs/pdf 路径段可选,裸 ID / 无 abs|pdf 的链接也能提取(保留版本号)。
_ARXIV_ID_RE = re.compile(
    r"(?:arxiv\.org/(?:abs|pdf)/)?"
    r"(\d+\.\d+(?:v\d+)?|[a-z-]+(?:\.[A-Za-z]{2})?/\d{7}(?:v\d+)?)",
    re.I,
)


def extract_arxiv_id(url: str) -> str | None:
    """从 arXiv URL / 裸 ID 提取论文 ID(新式 2301.00001[vN] 或旧式 hep-th/9901001[vN])。"""
    m = _ARXIV_ID_RE.search(url or "")
    return m.group(1) if m else None
