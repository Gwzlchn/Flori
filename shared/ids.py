"""所有 id 生成的唯一归处:job_id / lineage_key / collection_id / worker_id。

只依赖 stdlib + source_detect(低层,无循环导入)。id 仅作主键/URL slug;
去重靠业务键(ingested_items 等),与 id 无关。

- lineage_key(url,...) = jobs_{前缀}_{原生id}:同源稳定基础 id(去时间戳),同一内容的所有快照共用。
- content_job_id(url,...) = lineage_key + 时间戳:所有 job 带时间戳,重投/重建 = 同 lineage 的新快照。
- subscription_collection_id / generate_worker_id / generate_collection_id 同此处维护。
"""

from __future__ import annotations

import hashlib
import re
import secrets
from datetime import datetime

from .source_detect import detect_source, extract_arxiv_id, extract_bilibili_bvid


def _hash(s: str | None) -> str:
    return hashlib.sha1((s or "").encode()).hexdigest()[:10]


def extract_youtube_id(url: str | None) -> str | None:
    """从 YouTube URL 提取 11 位视频 ID(watch?v= / youtu.be/ / shorts / embed)。"""
    m = re.search(r"(?:v=|youtu\.be/|/shorts/|/embed/)([A-Za-z0-9_-]{11})", url or "")
    return m.group(1) if m else None


# 内容来源 → Job ID 前缀 + 原生 ID 提取器
_PLATFORM: dict[str, tuple[str, "callable | None"]] = {
    "bilibili": ("bili", extract_bilibili_bvid),
    "youtube": ("yt", extract_youtube_id),
    "arxiv": ("arxiv", extract_arxiv_id),
    "podcast": ("audio", None),
    "http_article": ("article", None),
}
_TYPE_PREFIX = {"video": "video", "article": "article", "paper": "paper", "audio": "audio"}

# job_id 末尾时间戳段:%y%m%d%H%M%S(12 位数字)+ 4 位 hex 随机(同秒不撞)。lineage_key_of 据此剥离。
_TS_RE = re.compile(r"_\d{12}[0-9a-f]{4}$")


def lineage_key(url: str | None, content_type: str | None = None, source: str | None = None) -> str:
    """同源稳定基础 id(不含时间戳):jobs_{前缀}_{原生id}。
    前缀按来源(平台)或内容类型(上传/本地);原生 id 取平台稳定 id,取不到/无 url 则短哈希/随机。
    同一 url → 同 lineage_key(一个 lineage = 同一内容的所有快照:重投、来源更新、pipeline 重建)。
    无 url(上传)→ native 随机,各自独立 lineage。统一规则,无来源特例。"""
    src = source or (detect_source(url) if url else "upload")
    if src in _PLATFORM:
        prefix, extractor = _PLATFORM[src]
        native = (extractor(url) if (extractor and url) else None) or _hash(url)
    else:
        prefix = _TYPE_PREFIX.get(content_type or "", content_type or "x")
        native = _hash(url) if url else secrets.token_hex(4)
    return f"jobs_{prefix}_{native}"


def _timestamp() -> str:
    """紧凑可排序时间戳(运行时真实本地时间)+ 4 位 hex 随机,保证同秒唯一。"""
    return datetime.now().strftime("%y%m%d%H%M%S") + secrets.token_hex(2)


def content_job_id(url: str | None, content_type: str | None = None, source: str | None = None) -> str:
    """Job ID = lineage_key + _时间戳。所有 job 带时间戳;同一内容重投/重建 = 同 lineage 的新快照。"""
    return f"{lineage_key(url, content_type, source)}_{_timestamp()}"


def lineage_key_of(job_id: str) -> str:
    """从 job_id 反推 lineage_key(剥离末尾时间戳段);无时间戳的旧 id 原样返回。"""
    return _TS_RE.sub("", job_id)


def generate_worker_id(worker_type: str) -> str:
    """Worker ID: {type}-{8 hex chars}。"""
    return f"{worker_type}-{secrets.token_hex(4)}"


def generate_collection_id() -> str:
    """手动集合 ID(随机):col_{8 hex}。"""
    return f"col_{secrets.token_hex(4)}"


# 订阅源类型 → 来源徽标 + Collection ID
def _yt_slug(sid: str) -> str:
    m = (re.search(r"@([A-Za-z0-9._-]+)", sid)
         or re.search(r"(UC[A-Za-z0-9_-]{20,})", sid)
         or re.search(r"/(?:c|user)/([A-Za-z0-9._-]+)", sid))
    return re.sub(r"[^A-Za-z0-9_-]+", "-", m.group(1)).strip("-") if m else _hash(sid)


def _dir_slug(sid: str) -> str:
    base = sid.rstrip("/").rsplit("/", 1)[-1]
    return re.sub(r"[^A-Za-z0-9_-]+", "-", base).strip("-") or _hash(sid)


def _plain_slug(sid: str) -> str:
    """短而干净(纯数字/短 id)直接用;长串或含 URL → 短哈希。"""
    clean = re.sub(r"[^A-Za-z0-9_-]+", "-", (sid or "").strip()).strip("-")
    return clean if (clean and len(clean) <= 24 and "://" not in (sid or "")) else _hash(sid)


# source_type → (徽标, collection-id 标签, slug 提取器)
_SUBSCRIPTION: dict[str, tuple[str, str, "callable"]] = {
    "bilibili_up": ("bilibili", "bili_up", _plain_slug),
    "bilibili_fav": ("bilibili", "bili_fav", _plain_slug),
    "bilibili_collection": ("bilibili", "bili_col", _plain_slug),
    "youtube_channel": ("youtube", "yt", _yt_slug),
    "rss": ("rss", "rss", _hash),
    "local_dir": ("local", "local", _dir_slug),
}


def subscription_badge(source_type: str) -> str:
    """订阅源类型 → 来源短标签(前端徽标 + 集合显示)。未登记回退 source_type 本身。"""
    e = _SUBSCRIPTION.get(source_type)
    return e[0] if e else source_type


def subscription_collection_id(source_type: str, source_id: str) -> str:
    """Collection ID = col_{标签}_{slug}。统一规则,无来源特例(B站 UP 自然得 col_bili_up_{mid})。"""
    e = _SUBSCRIPTION.get(source_type)
    if e:
        _, label, slug = e
        return f"col_{label}_{slug(source_id)}"
    label = re.sub(r"[^A-Za-z0-9]+", "_", source_type).strip("_")
    return f"col_{label}_{_plain_slug(source_id)}"
