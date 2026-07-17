"""_sync_published_at 从统一 Document metadata 兜底同步标题和发布时间。"""

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


class _Redis:
    async def get_job_pipeline(self, job_id: str):
        return "document"


def _engine(storage, db):
    pipelines = {
        "document": {"steps": [{
            "name": "02_parse",
            "on_complete": [{"action": "sync_metadata"}],
        }]},
    }
    return Scheduler(
        redis=_Redis(), db=db,
        config=SimpleNamespace(jobs_dir=Path("/tmp/x"), pipelines=pipelines),
        storage=storage,
    )


@pytest.mark.asyncio
async def test_document_title_synced_from_document_json():
    storage = _Storage({"intermediate/document.json": {"metadata": {
        "titles": {"original": "AlpaServe"}, "published_at": "2023-07",
    }}})
    db = _DB(title="")
    await _engine(storage, db)._sync_published_at("jobs_document_x")
    assert db.updates.get("title") == "AlpaServe"
    assert db.updates.get("published_at") == "2023-07"


@pytest.mark.asyncio
async def test_existing_title_not_overwritten():
    storage = _Storage({"intermediate/document.json": {"metadata": {
        "titles": {"original": "AlpaServe"},
    }}})
    db = _DB(title="用户已填的标题")
    await _engine(storage, db)._sync_published_at("jobs_document_x")
    assert "title" not in db.updates   # 已有标题不被覆盖


@pytest.mark.asyncio
async def test_document_parse_triggers_title_sync(monkeypatch):
    # 02_parse 完成后同步 canonical metadata，不能等后续 AI 步。
    eng = _engine(_Storage({}), _DB())
    called: list = []

    async def _fake_sync(jid):
        called.append(jid)

    monkeypatch.setattr(eng, "_sync_published_at", _fake_sync)
    await eng._run_step_completion_effects("jobs_document_x", "02_parse")
    assert called == ["jobs_document_x"]


@pytest.mark.asyncio
async def test_download_metadata_title_preferred_over_document():
    # 下载 metadata 有 title 时优先，不被结构化 Document 覆盖。
    storage = _Storage({
        "input/metadata.json": {"title": "来自下载的标题"},
        "intermediate/document.json": {
            "metadata": {"titles": {"original": "来自解析的标题"}},
        },
    })
    db = _DB(title="")
    await _engine(storage, db)._sync_published_at("jobs_document_x")
    assert db.updates.get("title") == "来自下载的标题"


@pytest.mark.asyncio
async def test_suspicious_title_overridden_by_better_candidate():
    # 已入库垃圾标题("10things",pdf-only 内嵌 metadata)→ 允许被更像真标题的候选覆盖。
    storage = _Storage({"intermediate/document.json": {"metadata": {"titles":
                        {"original": "Ten Simple Rules for Reproducible Computational Research"}}}})
    db = _DB(title="10things")
    await _engine(storage, db)._sync_published_at("j1")
    assert db.updates.get("title") == "Ten Simple Rules for Reproducible Computational Research"


@pytest.mark.asyncio
async def test_normal_title_still_not_overwritten():
    storage = _Storage({"intermediate/document.json": {"metadata": {"titles":
                        {"original": "Some Other Candidate Title"}}}})
    db = _DB(title="A Perfectly Good Existing Title")
    await _engine(storage, db)._sync_published_at("j1")
    assert "title" not in db.updates
