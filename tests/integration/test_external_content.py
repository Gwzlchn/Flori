"""对显式公网样本运行 Document、audio、RSS 与 YouTube 验证。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import pytest

from shared.subscriptions.base import SourceContext
from shared.subscriptions.rss import enumerate_rss
from shared.subscriptions.youtube import enumerate_youtube_channel, enumerate_youtube_playlist
from steps.common.step_01_download import DownloadStep
from steps.document.adapters.generic_html import parse_generic_html


pytestmark = pytest.mark.external
_SENSITIVE_QUERY_NAMES = {"access_token", "api_key", "apikey", "key", "secret", "signature", "token"}
_DOWNLOAD_CONFIG = {"step": {"timeout_sec": 180}}


def _public_url(env_name: str) -> str:
    url = os.environ.get(env_name, "").strip()
    if not url:
        pytest.fail(f"{env_name} 未配置,选定场景不得记为 skipped", pytrace=False)
    parsed = urlsplit(url)
    assert parsed.scheme in {"http", "https"} and parsed.hostname
    assert parsed.username is None and parsed.password is None, "外网样本 URL 不得内嵌账号密码"
    query_names = {name.lower() for name, _ in parse_qsl(parsed.query, keep_blank_values=True)}
    assert not query_names & _SENSITIVE_QUERY_NAMES, "外网样本 URL 不得内嵌 secret query"
    return url


def _write_job(job_dir: Path, url: str, content_type: str) -> None:
    job_dir.mkdir()
    (job_dir / "job.json").write_text(
        json.dumps({"url": url, "content_type": content_type}), encoding="utf-8",
    )


def test_external_article_download_and_parse(tmp_path) -> None:
    url = _public_url("FLORI_EXTERNAL_ARTICLE_URL")
    job_dir = tmp_path / "article"
    _write_job(job_dir, url, "document")

    DownloadStep("01_download", job_dir, _DOWNLOAD_CONFIG).execute()
    document, quality = parse_generic_html(job_dir, {
        "job_id": job_dir.name,
        "content_type": "document",
        "document_kind": "article",
        "url": url,
    })

    assert quality["status"] != "rejected"
    assert document["document_kind"] == "article"
    assert sum(len(block.get("text") or "") for block in document["blocks"]) >= 200


def test_external_audio_download_is_playable(tmp_path) -> None:
    url = _public_url("FLORI_EXTERNAL_AUDIO_URL")
    job_dir = tmp_path / "audio"
    _write_job(job_dir, url, "audio")

    result = DownloadStep("01_download", job_dir, _DOWNLOAD_CONFIG).execute()

    source = job_dir / "input" / "source.mp4"
    assert source.is_file() and source.stat().st_size > 0
    assert result is not None and result["duration_sec"] > 0


async def test_external_rss_enumerates_real_items() -> None:
    url = _public_url("FLORI_EXTERNAL_RSS_URL")
    title, items = await enumerate_rss(url, SourceContext())

    assert title
    assert items
    assert all(item.item_id and item.url for item in items)
    assert {item.content_type for item in items} <= {"document", "audio", "video"}
    assert all(
        item.document_kind in {"article", "research_paper"}
        for item in items if item.content_type == "document"
    )


async def test_external_youtube_enumerates_real_channel() -> None:
    url = _public_url("FLORI_EXTERNAL_YOUTUBE_URL")
    title, items = await enumerate_youtube_channel(url, SourceContext())

    assert title
    assert items
    assert all(item.content_type == "video" for item in items)
    assert all(item.url.startswith("https://www.youtube.com/watch?v=") for item in items)


async def test_external_youtube_playlist_enumerates_real_items() -> None:
    url = _public_url("FLORI_EXTERNAL_YOUTUBE_PLAYLIST_URL")
    title, items = await enumerate_youtube_playlist(url, SourceContext())

    assert title
    assert items
    assert len({item.item_id for item in items}) == len(items)
    assert all(item.content_type == "video" for item in items)
    assert all(item.url == f"https://www.youtube.com/watch?v={item.item_id}" for item in items)
