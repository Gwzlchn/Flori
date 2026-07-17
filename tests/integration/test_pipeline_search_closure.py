"""三类 pipeline 与两种 Document 体裁完成事件的检索闭环。"""

from __future__ import annotations

import json

import pytest
from httpx import ASGITransport, AsyncClient

from api.main import create_app
from api.mcp_server.server import build_server
from scheduler.scheduler import Scheduler
from shared.models import Job, JobStatus
from shared.step_base import def_digest_for
from shared.storage import LocalStorage
from tests.integration.provenance_fixture import publish_provenance_fixture


pytestmark = pytest.mark.integration


_CASES = [
    ("video", "video", None, "闭环视频证据", "smart"),
    ("research", "document", "research_paper", "闭环论文证据", "smart"),
    ("article", "document", "article", "闭环文章证据", "original"),
    ("audio", "audio", None, "闭环音频证据", "smart"),
]


async def _seed_artifacts(
    storage, job_id: str, pipeline: str, document_kind: str | None,
    keyword: str,
) -> None:
    await storage.write_file(
        job_id, "input/metadata.json",
        json.dumps({"title": f"{pipeline} 检索闭环"}, ensure_ascii=False).encode(),
    )
    await storage.write_file(
        job_id, "output/concepts.json",
        json.dumps({
            "key_terms": [
                {
                    "term": "闭环主概念",
                    "definition": "主概念定义",
                    "related": [{"term": "闭环辅概念", "rel": "prerequisite"}],
                },
                {"term": "闭环辅概念", "definition": "辅助概念定义"},
            ],
        }, ensure_ascii=False).encode(),
    )
    notes: dict[str, tuple[str, bytes]] = {}
    if pipeline == "document":
        original_path = "intermediate/document_index.md"
        original_data = f"# Document 原文投影\n{keyword}由结构完成事件写入索引。".encode()
        await storage.write_file(job_id, original_path, original_data)
        notes["original"] = (original_path, original_data)
    if pipeline != "document" or document_kind == "research_paper":
        note_path = "output/versions/notes_smart_anthropic_opus_20260714-012500.md"
        note_data = f"# 智能笔记\n{keyword}由真实完成事件写入索引。".encode()
        await storage.write_file(job_id, note_path, note_data)
        notes["smart"] = (note_path, note_data)
    if pipeline == "video":
        mechanical_path = "output/notes_mechanical.md"
        mechanical_data = f"# 机械笔记\n{keyword}的机械稿证据。".encode()
        await storage.write_file(
            job_id, mechanical_path, mechanical_data,
        )
        notes["mechanical"] = (mechanical_path, mechanical_data)
    await publish_provenance_fixture(
        storage, job_id=job_id, pipeline=pipeline, notes=notes,
    )


async def _complete_real_pipeline(scheduler, redis, db, config, job_id: str) -> None:
    """按真实归一化步骤表发送完成事件;已由 rules 跳过的步骤保持 skipped."""
    for step in config.pipelines[db.get_job(job_id).pipeline]["steps"]:
        name = step["name"]
        status = await redis.get_step_status(job_id, name)
        if status == "skipped":
            continue
        await redis.set_step_status(job_id, name, "running")
        await scheduler.on_step_done(job_id, name, duration=0.01, worker="test-worker")


def _index_counts(db, job_id: str) -> tuple[int, int, int]:
    return tuple(
        db._conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE job_id=?", (job_id,),
        ).fetchone()[0]
        for table in ("notes_fts5", "note_chunks", "note_chunks_fts5")
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case_name", "pipeline", "document_kind", "keyword", "expected_note_type"),
    _CASES,
)
async def test_pipeline_completion_reaches_search_ask_and_mcp(
    db, test_config, case_name, pipeline, document_kind, keyword,
    expected_note_type, integration_redis,
):
    redis = integration_redis
    storage = LocalStorage(test_config.jobs_dir)
    job_id = f"j_closure_{case_name}"
    domain = f"closure-{case_name}"
    flags = {"smart_note": document_kind != "article"}
    job = Job(
        id=job_id, content_type=pipeline, pipeline=pipeline, domain=domain,
        document_kind=document_kind or "", title=f"{case_name} 检索闭环",
        meta={"flags": flags},
    )
    db.create_job(job)
    await _seed_artifacts(storage, job_id, pipeline, document_kind, keyword)
    scheduler = Scheduler(redis, db, test_config, storage=storage)

    async def _workers_present(_pool):
        return True

    scheduler._pool_has_workers = _workers_present
    try:
        await scheduler.submit_job(job)
        await _complete_real_pipeline(scheduler, redis, db, test_config, job_id)

        assert db.get_job(job_id).status == JobStatus.DONE
        total, rows = db.search_notes(keyword, domain=domain)
        assert total >= 1
        assert any(
            row["job_id"] == job_id and row["note_type"] == expected_note_type
            for row in rows
        )

        app = create_app(db=db, redis=redis, config=test_config)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
        ) as client:
            search = await client.get("/api/search", params={"q": keyword, "domain": domain})
            assert search.status_code == 200
            search_item = next(
                item for item in search.json()["items"] if item["job_id"] == job_id
            )
            assert search_item.get("document_kind") == (document_kind or None)

            ask = await client.post(
                "/api/ask", json={"question": keyword, "domain": domain},
            )
            assert ask.status_code == 202
            ask_source = next(
                source for source in ask.json()["sources"] if source["job_id"] == job_id
            )
            assert ask_source.get("document_kind") == (document_kind or None)

        mcp = build_server(db, storage)
        result = await mcp.call_tool(
            "search", {"query": keyword, "domain": domain, "limit": 10},
        )
        assert job_id in str(result)
        if document_kind:
            assert document_kind in str(result)

        before = _index_counts(db, job_id)
        concept_before = db.get_glossary_term(domain, "闭环主概念")
        assert len(concept_before["occurrences"]) == 1
        assert concept_before["related"] == [
            {"term": "闭环辅概念", "rel": "prerequisite"},
        ]

        # 重复 complete 被 CAS 丢弃;终态 reconcile 会重放声明,两者都不能累加索引或概念边.
        index_step = next(
            step["name"]
            for step in test_config.pipelines[pipeline]["steps"]
            if any(e.get("action") == "index_note" for e in step.get("on_complete", []))
        )
        await scheduler.on_step_done(job_id, index_step, duration=0.01, worker="late-worker")
        assert await scheduler._reconcile_completed_effects(job_id) is True
        assert await scheduler._reconcile_completed_effects(job_id) is True

        assert _index_counts(db, job_id) == before
        concept_after = db.get_glossary_term(domain, "闭环主概念")
        assert len(concept_after["occurrences"]) == 1
        assert concept_after["related"] == concept_before["related"]
    finally:
        await redis.r.flushdb()


@pytest.mark.asyncio
async def test_missing_index_artifact_keeps_job_active_for_reconcile(
    db, test_config, integration_redis,
):
    redis = integration_redis
    storage = LocalStorage(test_config.jobs_dir)
    job = Job(
        id="j_missing_audio", content_type="audio", pipeline="audio", domain="closure",
    )
    db.create_job(job)
    await storage.write_file(
        job.id, "output/concepts.json", b'{"key_terms": []}',
    )
    scheduler = Scheduler(redis, db, test_config, storage=storage)

    async def _workers_present(_pool):
        return True

    scheduler._pool_has_workers = _workers_present
    try:
        await scheduler.submit_job(job)
        await _complete_real_pipeline(scheduler, redis, db, test_config, job.id)
        assert db.get_job(job.id).status != JobStatus.DONE
        assert job.id in await redis.get_active_jobs()

        await storage.write_file(
            job.id,
            "output/versions/notes_smart_anthropic_opus_20260714-012600.md",
            note_data := "# 恢复\n补齐产物后周期对账可以完成索引。".encode(),
        )
        await publish_provenance_fixture(
            storage,
            job_id=job.id,
            pipeline=job.pipeline,
            notes={
                "smart": (
                    "output/versions/notes_smart_anthropic_opus_20260714-012600.md",
                    note_data,
                ),
            },
        )
        # 周期任务下一拍发生在 finalizer 租约之后;压缩测试墙钟但保留真实重取租约路径。
        await redis.r.hset(f"job:{job.id}:finalizer", "lease_until", "0")
        await scheduler.reconcile_completion_effects()
        assert db.get_job(job.id).status == JobStatus.DONE
        assert db.search_notes("周期对账")[0] == 1
    finally:
        await redis.r.flushdb()


@pytest.mark.asyncio
async def test_reconcile_backfills_legacy_done_job_without_redis_state(
    db, test_config, integration_redis,
):
    redis = integration_redis
    storage = LocalStorage(test_config.jobs_dir)
    job = Job(
        id="j_legacy_audio", content_type="audio", pipeline="audio",
        domain="closure", status=JobStatus.DONE,
    )
    db.create_job(job)
    await storage.write_file(
        job.id,
        "output/versions/notes_smart_anthropic_opus_20260714-012700.md",
        "# 历史补账\n历史完成任务无需 Redis 状态也能补齐检索。".encode(),
    )
    producer = next(
        step for step in test_config.pipelines["audio"]["steps"]
        if step["name"] == "04_smart_podcast"
    )
    await storage.write_file(
        job.id,
        ".04_smart_podcast.done",
        json.dumps({
            "step": "04_smart_podcast",
            "input_hashes": {},
            "def_digest": def_digest_for("1", producer.get("ai")),
            "finished_at": "2026-07-01T00:00:00+00:00",
        }, sort_keys=True).encode(),
    )
    scheduler = Scheduler(redis, db, test_config, storage=storage)
    try:
        assert [item.id for item in db.list_unindexed_done_jobs()] == [job.id]
        await scheduler.reconcile_completion_effects()
        assert db.search_notes("历史完成任务")[0] == 1
        assert db.list_unindexed_done_jobs() == []
    finally:
        await redis.r.flushdb()


@pytest.mark.asyncio
async def test_current_note_without_provenance_stays_pending(
    db, test_config, integration_redis,
):
    redis = integration_redis
    storage = LocalStorage(test_config.jobs_dir)
    job = Job(
        id="j_current_without_provenance",
        content_type="audio",
        pipeline="audio",
        domain="closure",
    )
    db.create_job(job)
    await storage.write_file(
        job.id,
        "output/versions/notes_smart_fixture_20260714-012900.md",
        "# 当前产物\n只有笔记不能越过当前溯源门。".encode(),
    )
    await storage.write_file(job.id, "output/concepts.json", b'{"key_terms": []}')
    scheduler = Scheduler(redis, db, test_config, storage=storage)

    async def _workers_present(_pool):
        return True

    scheduler._pool_has_workers = _workers_present
    try:
        await scheduler.submit_job(job)
        await _complete_real_pipeline(scheduler, redis, db, test_config, job.id)

        assert db.get_job(job.id).status == JobStatus.PENDING
        assert job.id in await redis.get_active_jobs()
        assert db.search_notes("只有笔记")[0] == 0
    finally:
        await redis.r.flushdb()
