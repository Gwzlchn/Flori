"""tests for shared/db.py"""

import threading

import pytest

from shared.db import Database
from shared.models import (
    AIUsage,
    Collection,
    Job,
    JobStatus,
    Step,
    StepStatus,
    Worker,
)


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "test.db")
    d.init_schema()
    yield d
    d.close()


@pytest.fixture
def sample_job():
    return Job(
        id="j_20260517_aaaaaa",
        content_type="video",
        pipeline="video",
        domain="deep-learning",
        url="https://example.com",
        style_tags=["case-study"],
        meta={"duration_sec": 485},
    )


class TestSchema:
    def test_init_idempotent(self, tmp_path):
        d = Database(tmp_path / "test.db")
        d.init_schema()
        d.init_schema()
        d.close()


class TestJobCRUD:
    def test_create_and_get(self, db, sample_job):
        db.create_job(sample_job)
        got = db.get_job(sample_job.id)
        assert got is not None
        assert got.id == sample_job.id
        assert got.content_type == "video"
        assert got.domain == "deep-learning"
        assert got.status == JobStatus.PENDING
        assert got.style_tags == ["case-study"]
        assert got.meta == {"duration_sec": 485}

    def test_get_nonexistent(self, db):
        assert db.get_job("nope") is None

    def test_list_all(self, db, sample_job):
        db.create_job(sample_job)
        j2 = Job(id="j_20260517_bbbbbb", content_type="paper", pipeline="paper")
        db.create_job(j2)
        total, jobs = db.list_jobs()
        assert total == 2

    def test_list_filter_status(self, db, sample_job):
        db.create_job(sample_job)
        db.update_job(sample_job.id, status=JobStatus.PROCESSING)
        total, jobs = db.list_jobs(status="processing")
        assert total == 1
        assert jobs[0].status == JobStatus.PROCESSING

    def test_list_pagination(self, db):
        for i in range(5):
            db.create_job(Job(id=f"j_20260517_{i:06d}", content_type="video", pipeline="video"))
        total, page = db.list_jobs(limit=2, offset=0)
        assert total == 5
        assert len(page) == 2

    def test_update_job(self, db, sample_job):
        db.create_job(sample_job)
        db.update_job(sample_job.id, status=JobStatus.DONE, progress_pct=100)
        got = db.get_job(sample_job.id)
        assert got.status == JobStatus.DONE
        assert got.progress_pct == 100

    def test_update_json_field(self, db, sample_job):
        db.create_job(sample_job)
        db.update_job(sample_job.id, meta={"duration_sec": 500, "extra": True})
        got = db.get_job(sample_job.id)
        assert got.meta == {"duration_sec": 500, "extra": True}

    def test_delete_job(self, db, sample_job):
        db.create_job(sample_job)
        db.delete_job(sample_job.id)
        assert db.get_job(sample_job.id) is None

    def test_delete_cascades_steps(self, db, sample_job):
        db.create_job(sample_job)
        db.upsert_step(Step(job_id=sample_job.id, name="01_scene", pool="scene"))
        db.delete_job(sample_job.id)
        assert db.get_steps(sample_job.id) == []


class TestStepCRUD:
    def test_upsert_and_get(self, db, sample_job):
        db.create_job(sample_job)
        step = Step(
            job_id=sample_job.id,
            name="01_scene",
            status=StepStatus.RUNNING,
            pool="scene",
            meta={"scenes": 76},
        )
        db.upsert_step(step)
        steps = db.get_steps(sample_job.id)
        assert len(steps) == 1
        assert steps[0].name == "01_scene"
        assert steps[0].status == StepStatus.RUNNING
        assert steps[0].meta == {"scenes": 76}

    def test_upsert_replaces(self, db, sample_job):
        db.create_job(sample_job)
        db.upsert_step(Step(job_id=sample_job.id, name="01_scene", pool="scene"))
        db.upsert_step(Step(
            job_id=sample_job.id,
            name="01_scene",
            status=StepStatus.DONE,
            pool="scene",
            duration_sec=120.5,
        ))
        steps = db.get_steps(sample_job.id)
        assert len(steps) == 1
        assert steps[0].status == StepStatus.DONE
        assert steps[0].duration_sec == 120.5

    def test_update_step(self, db, sample_job):
        db.create_job(sample_job)
        db.upsert_step(Step(job_id=sample_job.id, name="01_scene", pool="scene"))
        db.update_step(sample_job.id, "01_scene", status="done", duration_sec=99.0)
        steps = db.get_steps(sample_job.id)
        assert steps[0].status == StepStatus.DONE
        assert steps[0].duration_sec == 99.0

    def test_get_steps_sorted(self, db, sample_job):
        db.create_job(sample_job)
        for name in ["03_dedup", "01_scene", "02_frames"]:
            db.upsert_step(Step(job_id=sample_job.id, name=name, pool="cpu"))
        steps = db.get_steps(sample_job.id)
        assert [s.name for s in steps] == ["01_scene", "02_frames", "03_dedup"]


class TestWorkerCRUD:
    def test_upsert_and_get(self, db):
        w = Worker(
            id="cpu-12345678",
            type="cpu",
            pools=["scene", "cpu"],
            tags={"vision"},
            reject_tags={"private"},
            hostname="my-pc",
        )
        db.upsert_worker(w)
        got = db.get_worker("cpu-12345678")
        assert got is not None
        assert got.pools == ["scene", "cpu"]
        assert got.tags == {"vision"}
        assert got.reject_tags == {"private"}

    def test_upsert_updates(self, db):
        w = Worker(id="ai-aabbccdd", type="ai")
        db.upsert_worker(w)
        w.status = "busy"
        w.tasks_completed = 5
        db.upsert_worker(w)
        got = db.get_worker("ai-aabbccdd")
        assert got.status == "busy"
        assert got.tasks_completed == 5

    def test_increment_stats(self, db):
        db.upsert_worker(Worker(id="ai-aabbccdd", type="ai"))
        db.increment_worker_stats("ai-aabbccdd", completed=3, failed=1, duration=100.0)
        db.increment_worker_stats("ai-aabbccdd", completed=2, duration=50.0)
        got = db.get_worker("ai-aabbccdd")
        assert got.tasks_completed == 5
        assert got.tasks_failed == 1
        assert got.total_duration_sec == 150.0

    def test_list_workers(self, db):
        db.upsert_worker(Worker(id="cpu-1", type="cpu"))
        db.upsert_worker(Worker(id="ai-1", type="ai"))
        assert len(db.list_workers()) == 2

    def test_delete_worker(self, db):
        db.upsert_worker(Worker(id="cpu-1", type="cpu"))
        db.delete_worker("cpu-1")
        assert db.get_worker("cpu-1") is None

    def test_update_worker_heartbeat_refreshes_timestamp(self, db):
        from datetime import datetime, timedelta

        old = datetime.now() - timedelta(minutes=10)
        db.upsert_worker(
            Worker(id="cpu-1", type="cpu", status="idle",
                   first_seen=old, last_heartbeat=old)
        )
        db.update_worker_heartbeat("cpu-1")
        got = db.get_worker("cpu-1")
        # 心跳被刷新到接近现在（而非停在 10 分钟前）
        assert (datetime.now() - got.last_heartbeat).total_seconds() < 5
        assert got.status == "idle"

    def test_update_worker_heartbeat_updates_status_and_task(self, db):
        db.upsert_worker(Worker(id="ai-1", type="ai", status="idle"))
        db.update_worker_heartbeat(
            "ai-1", status="busy", current_job="j1", current_step="A"
        )
        got = db.get_worker("ai-1")
        assert got.status == "busy"
        assert got.current_job == "j1"
        assert got.current_step == "A"


class TestAIUsage:
    def test_record_and_summary(self, db):
        u = AIUsage(
            exec_id="ai-abc:1716000:0",
            provider="anthropic",
            model="claude-sonnet-4-6",
            job_id="j_xxx",
            step="08_smart",
            input_tokens=1000,
            output_tokens=500,
            cost_usd=0.0105,
        )
        assert db.record_ai_usage(u) is True
        summary = db.get_usage_summary(job_id="j_xxx")
        assert summary["calls"] == 1
        assert summary["total_input_tokens"] == 1000
        assert summary["total_cost_usd"] == pytest.approx(0.0105)

    def test_exec_id_dedup(self, db):
        u = AIUsage(exec_id="dup-id", provider="test", model="test")
        assert db.record_ai_usage(u) is True
        assert db.record_ai_usage(u) is False
        summary = db.get_usage_summary()
        assert summary["calls"] == 1

    def test_summary_empty(self, db):
        summary = db.get_usage_summary()
        assert summary["calls"] == 0
        assert summary["total_cost_usd"] == 0


class TestCollection:
    def test_create_and_get(self, db):
        c = Collection(id="my-dl", name="深度学习", domain="deep-learning", tags=["论文"])
        db.create_collection(c)
        got = db.get_collection("my-dl")
        assert got.name == "深度学习"
        assert got.tags == ["论文"]

    def test_get_collection_not_found(self, db):
        assert db.get_collection("nonexistent") is None

    def test_list(self, db):
        db.create_collection(Collection(id="c1", name="c1", domain="ml"))
        db.create_collection(Collection(id="c2", name="c2", domain="deep-learning"))
        assert len(db.list_collections()) == 2


class TestUpdateValidation:
    def test_update_job_invalid_column(self, db, sample_job):
        db.create_job(sample_job)
        with pytest.raises(ValueError, match="Invalid job columns"):
            db.update_job(sample_job.id, hacked_field="bad")

    def test_update_step_invalid_column(self, db, sample_job):
        db.create_job(sample_job)
        db.upsert_step(Step(job_id=sample_job.id, name="01_scene", pool="scene"))
        with pytest.raises(ValueError, match="Invalid step columns"):
            db.update_step(sample_job.id, "01_scene", hacked_field="bad")

    def test_update_job_style_tags_roundtrip(self, db, sample_job):
        db.create_job(sample_job)
        db.update_job(sample_job.id, style_tags=["lecture", "code-tutorial"])
        got = db.get_job(sample_job.id)
        assert got.style_tags == ["lecture", "code-tutorial"]


class TestDBEdgeCases:
    def test_create_duplicate_job_id(self, db):
        job1 = Job(id="j_dup", content_type="video", pipeline="test")
        db.create_job(job1)
        job2 = Job(id="j_dup", content_type="paper", pipeline="test2")
        # Should raise IntegrityError or similar
        with pytest.raises(Exception):
            db.create_job(job2)

    def test_get_usage_summary_with_since(self, db):
        """get_usage_summary filters by since parameter."""
        from datetime import datetime, timedelta

        now = datetime.now()
        old = AIUsage(
            exec_id="old-usage-1",
            provider="anthropic",
            model="claude-opus-4-6",
            job_id="j1",
            step="A",
            input_tokens=100,
            output_tokens=50,
            created_at=now - timedelta(days=10),
        )
        recent = AIUsage(
            exec_id="recent-usage-1",
            provider="anthropic",
            model="claude-opus-4-6",
            job_id="j2",
            step="B",
            input_tokens=200,
            output_tokens=100,
            created_at=now - timedelta(hours=1),
        )
        db.record_ai_usage(old)
        db.record_ai_usage(recent)
        # since parameter is a string (ISO format) compared against created_at
        since_str = (now - timedelta(days=1)).isoformat()
        summary = db.get_usage_summary(since=since_str)
        # Only recent usage should be included
        assert summary["total_input_tokens"] == 200
        assert summary["total_output_tokens"] == 100


class TestConcurrency:
    def test_parallel_writes(self, db, sample_job):
        db.create_job(sample_job)
        errors = []

        def update(n):
            try:
                db.update_job(sample_job.id, progress_pct=n)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=update, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        # After the parallel writes, verify the job still exists and is readable
        got = db.get_job(sample_job.id)
        assert got is not None
        assert got.progress_pct in range(10)
