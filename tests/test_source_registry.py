"""来源注册表跨检测、pipeline、适配器与公开目录完整性测试。"""

from __future__ import annotations

import pytest

from shared.config import load_config
from shared.source_registry import (
    CONTENT_TYPE_SPECS,
    CONTENT_TYPE_NAMES,
    SUBSCRIPTION_SOURCE_NAMES,
    SourceRegistryError,
    content_type_for_filename,
    load_source_registry,
    source_catalog,
    validate_job_route,
)


def test_content_types_match_real_pipelines(configs_dir, tmp_data_dir):
    config = load_config(configs_dir, tmp_data_dir)
    assert set(CONTENT_TYPE_NAMES) == set(config.pipelines)
    assert {spec["pipeline"] for spec in CONTENT_TYPE_SPECS.values()} == set(config.pipelines)


def test_subscription_registry_matches_loaded_adapters():
    from shared.subscriptions import SOURCE_ADAPTERS

    assert set(SUBSCRIPTION_SOURCE_NAMES) == set(SOURCE_ADAPTERS)


def test_catalog_exposes_book_without_private_routing_fields():
    catalog = source_catalog()
    book = next(item for item in catalog["subscription_sources"] if item["type"] == "book_toc")
    assert book["label"] == "在线书目录"
    assert book["group"] == "book"
    assert "collection_prefix" not in book
    assert "slug_strategy" not in book


@pytest.mark.parametrize(
    ("filename", "content_type"),
    [
        ("lesson.MOV", "video"),
        ("paper.pdf", "paper"),
        ("chapter.md", "article"),
        ("episode.flac", "audio"),
        ("archive.zip", None),
    ],
)
def test_upload_extensions_come_from_registry(filename, content_type):
    assert content_type_for_filename(filename) == content_type


def test_job_route_rejects_unknown_source_and_mismatch():
    with pytest.raises(SourceRegistryError, match="unsupported source"):
        validate_job_route("other", "video")
    with pytest.raises(SourceRegistryError, match="does not support"):
        validate_job_route("youtube", "article")
    with pytest.raises(SourceRegistryError, match="unsupported source"):
        validate_job_route("local_file", "article")
    validate_job_route("local_file", "article", allow_internal=True)
    validate_job_route("http_article", "audio")


def test_invalid_registry_fails_closed(tmp_path):
    path = tmp_path / "sources.yaml"
    path.write_text(
        "content_types:\n  video: {label: video, pipeline: video}\n"
        "job_sources:\n  upload: {label: upload, content_types: [video]}\n"
        "subscription_sources:\n  x: {label: x}\n",
        encoding="utf-8",
    )
    with pytest.raises(SourceRegistryError):
        load_source_registry(path)
