"""_sync_published_at:论文/文章 title 从 intermediate/parsed.json 兜底同步进 DB
(metadata.json / article_meta.json 都没有时;不覆盖已有标题)。"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scheduler.scheduler import Scheduler


class _Storage:
    def __init__(self, files: dict):
        self._files = files

    async def read_file(self, job_id: str, rel: str):
        v = self._files.get(rel)
        return json.dumps(v).encode("utf-8") if v is not None else None


class _DB:
    def __init__(self, title: str = ""):
        self.job = SimpleNamespace(title=title)
        self.updates: dict = {}

    def get_job(self, job_id):
        return self.job

    def update_job(self, job_id, **fields):
        self.updates.update(fields)


def _engine(storage, db):
    return Scheduler(redis=None, db=db,
                     config=SimpleNamespace(jobs_dir=Path("/tmp/x")), storage=storage)


@pytest.mark.asyncio
async def test_paper_title_synced_from_parsed_json():
    # 论文标题只在 parsed.json(metadata/article_meta 都没有)→ 兜底同步。
    storage = _Storage({"intermediate/parsed.json": {"title": "AlpaServe", "date": "2023-07"}})
    db = _DB(title="")
    await _engine(storage, db)._sync_published_at("jobs_paper_x")
    assert db.updates.get("title") == "AlpaServe"
    assert db.updates.get("published_at") == "2023-07"


@pytest.mark.asyncio
async def test_existing_title_not_overwritten():
    storage = _Storage({"intermediate/parsed.json": {"title": "AlpaServe"}})
    db = _DB(title="用户已填的标题")
    await _engine(storage, db)._sync_published_at("jobs_paper_x")
    assert "title" not in db.updates   # 已有标题不被覆盖


@pytest.mark.asyncio
async def test_metadata_title_preferred_over_parsed():
    # metadata.json 有 title 时优先,不被 parsed.json 覆盖。
    storage = _Storage({
        "input/metadata.json": {"title": "来自下载的标题"},
        "intermediate/parsed.json": {"title": "来自解析的标题"},
    })
    db = _DB(title="")
    await _engine(storage, db)._sync_published_at("jobs_paper_x")
    assert db.updates.get("title") == "来自下载的标题"
