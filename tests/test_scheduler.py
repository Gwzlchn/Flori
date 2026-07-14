"""tests for scheduler — 使用 fakeredis + 临时 DB。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from tests.conftest import make_fakeredis
from tests.pubsub_helpers import subscription_barrier
from shared.config import AppConfig
from shared.db import Database
from shared.models import Job, JobStatus, StepStatus, Step, AIUsage
from scheduler.scheduler import Scheduler


# Fixtures


@pytest.fixture
def tmp_jobs_dir(tmp_path):
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    return jobs


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "test.db")
    d.init_schema()
    yield d
    d.close()


@pytest.fixture
async def redis():
    client = make_fakeredis()
    yield client
    await client.close()


@pytest.fixture
def simple_pipelines():
    """三步线性 pipeline: A → B → C"""
    return {
        "test": {
            "steps": [
                {"name": "A", "pool": "cpu", "depends_on": [], "retries": 2},
                {"name": "B", "pool": "cpu", "depends_on": ["A"], "retries": 1},
                {"name": "C", "pool": "cpu", "depends_on": ["B"], "retries": 0},
            ]
        }
    }


@pytest.fixture
def parallel_pipelines():
    """A → {B, C} → D"""
    return {
        "par": {
            "steps": [
                {"name": "A", "pool": "cpu", "depends_on": []},
                {"name": "B", "pool": "cpu", "depends_on": ["A"]},
                {"name": "C", "pool": "io", "depends_on": ["A"]},
                {"name": "D", "pool": "cpu", "depends_on": ["B", "C"]},
            ]
        }
    }


@pytest.fixture
def video_pipelines(configs_dir):
    """使用真实 pipelines.yaml(归一化为内部 step 结构)"""
    from shared.config import load_pipelines
    return load_pipelines(configs_dir / "pipelines.yaml")


@pytest.fixture
def config(tmp_path, tmp_jobs_dir, simple_pipelines, configs_dir):
    return AppConfig(
        data_dir=tmp_path,
        db_path=tmp_path / "test.db",
        jobs_dir=tmp_jobs_dir,
        config_dir=configs_dir,
        prompts_dir=tmp_path / "prompts",
        pipelines=simple_pipelines,
        pools={"pools": {"cpu": {"limit": 3}, "io": {"limit": 999}}},
        providers={},
    )


def make_config(tmp_path, tmp_jobs_dir, pipelines, configs_dir):
    return AppConfig(
        data_dir=tmp_path,
        db_path=tmp_path / "test.db",
        jobs_dir=tmp_jobs_dir,
        config_dir=configs_dir,
        prompts_dir=tmp_path / "prompts",
        pipelines=pipelines,
        pools={"pools": {"cpu": {"limit": 3}, "io": {"limit": 999}, "scene": {"limit": 1}}},
        providers={},
    )


def _stub_workers_present(s):
    """让 scheduler 视所有 pool 都有 worker,跳过 skip_no_worker 死锁打破逻辑。"""
    async def _has_workers(_pool):
        return True
    s._pool_has_workers = _has_workers
    return s


async def _skip_step(scheduler, redis, db, job_id, step):
    """测试辅助:模拟某步被跳过并触发下游检查。"""
    await redis.set_step_status(job_id, step, "skipped")
    db.update_step(job_id, step, status="skipped")
    await scheduler._check_downstream(job_id)


@pytest.fixture
def scheduler(redis, db, config):
    return _stub_workers_present(Scheduler(redis, db, config))


def make_job(pipeline="test", job_id="j_test_001"):
    return Job(
        id=job_id,
        content_type="video",
        pipeline=pipeline,
        domain="general",
    )


# Tests


class TestSubmitJob:
    @pytest.mark.asyncio
    async def test_initializes_all_steps(self, scheduler, redis, db):
        job = make_job()
        db.create_job(job)
        await scheduler.submit_job(job)

        statuses = await redis.get_all_step_statuses("j_test_001")
        assert set(statuses.keys()) == {"A", "B", "C"}

    @pytest.mark.asyncio
    async def test_enqueues_root_steps(self, scheduler, redis, db):
        job = make_job()
        db.create_job(job)
        await scheduler.submit_job(job)

        status_a = await redis.get_step_status("j_test_001", "A")
        assert status_a == "ready"
        status_b = await redis.get_step_status("j_test_001", "B")
        assert status_b == "waiting"

        queue = await redis.get_queue_info("cpu")
        assert queue["length"] == 1

    @pytest.mark.asyncio
    async def test_adds_to_active_jobs(self, scheduler, redis, db):
        job = make_job()
        db.create_job(job)
        await scheduler.submit_job(job)

        active = await redis.get_active_jobs()
        assert "j_test_001" in active

    @pytest.mark.asyncio
    async def test_unknown_pipeline_fails_job(self, scheduler, redis, db):
        """Submitting a job with unknown pipeline should mark it as FAILED."""
        job = make_job(pipeline="nonexistent")
        db.create_job(job)
        await scheduler.submit_job(job)

        db_job = db.get_job("j_test_001")
        assert db_job.status == JobStatus.FAILED
        active = await redis.get_active_jobs()
        assert "j_test_001" not in active


class TestSkipNoWorker:
    """覆盖 skip_no_worker 死锁打破器(_check_downstream 末段)。
    只在剩余未完成步骤全部为 ready 且其 pool 无 worker 时介入,并用
    CAS(ready→skipped) 避免覆盖被 worker 抢成 running 的步骤。用真实
    _pool_has_workers,不走 scheduler fixture 的桩。"""

    @pytest.mark.asyncio
    async def test_pool_has_workers_reflects_registration(self, redis, db, config):
        s = Scheduler(redis, db, config)
        assert await s._pool_has_workers("cpu") is False
        await redis.register_worker(
            "w1", {"type": "cpu", "pools": "cpu,io", "status": "idle"}
        )
        assert await s._pool_has_workers("cpu") is True
        assert await s._pool_has_workers("gpu") is False

    @pytest.mark.asyncio
    async def test_all_ready_no_worker_required_step_not_skipped(self, redis, db, config):
        # 仅剩 A=ready(必需步,无 condition/rules)、其余 done/skipped、cpu 无 worker:
        # 死锁打破器不 skip 必需步——留给 check_no_worker 超宽限 fail-fast,避免不完整却显示完成。
        s = Scheduler(redis, db, config)
        job = make_job()
        db.create_job(job)
        await s.submit_job(job)
        await redis.set_step_status("j_test_001", "B", "skipped")
        await redis.set_step_status("j_test_001", "C", "skipped")
        await s._check_downstream("j_test_001")
        assert await redis.get_step_status("j_test_001", "A") == "ready"

    @pytest.mark.asyncio
    async def test_running_step_blocks_eager_skip(self, redis, db, config):
        # 存在 running 在途步骤时,no-worker 的 ready 兄弟不被误 skip(守卫核心)
        s = Scheduler(redis, db, config)
        job = make_job()
        db.create_job(job)
        await s.submit_job(job)
        await redis.set_step_status("j_test_001", "A", "done")
        await redis.set_step_status("j_test_001", "B", "running")
        await redis.set_step_status("j_test_001", "C", "ready")
        await s._check_downstream("j_test_001")
        assert await redis.get_step_status("j_test_001", "C") == "ready"

    @pytest.mark.asyncio
    async def test_waiting_step_not_skipped(self, redis, db, config):
        # 仍有 waiting(依赖未满足)属正常等待,不是死锁,不触发 skip
        s = Scheduler(redis, db, config)
        job = make_job()
        db.create_job(job)
        await s.submit_job(job)  # A=ready, B/C=waiting
        await s._check_downstream("j_test_001")
        assert await redis.get_step_status("j_test_001", "A") == "ready"
        assert await redis.get_step_status("j_test_001", "B") == "waiting"

    @pytest.mark.asyncio
    async def test_ready_won_by_worker_during_skip_not_skipped(self, redis, db, config):
        # CAS 保护:判定无 worker 后该步骤被 worker 抢成 running,skip 应放弃。
        # 死锁打破器只 skip 条件步,故把 A 视为条件步以走到 ready→skipped 的 CAS 路径。
        s = Scheduler(redis, db, config)
        s._step_is_conditional = lambda cfg: True  # 令 A 走条件步 skip 分支(测 CAS 保护)

        async def _no_workers(_pool):
            return False
        s._pool_has_workers = _no_workers

        job = make_job()
        db.create_job(job)
        await s.submit_job(job)
        await redis.set_step_status("j_test_001", "B", "skipped")
        await redis.set_step_status("j_test_001", "C", "skipped")

        real_cas = redis.cas_step_status

        async def _racing_cas(job_id, step, expected, new):
            if step == "A" and expected == "ready" and new == "skipped":
                await redis.set_step_status(job_id, "A", "running")  # worker 抢先
                return False
            return await real_cas(job_id, step, expected, new)
        redis.cas_step_status = _racing_cas

        await s._check_downstream("j_test_001")
        assert await redis.get_step_status("j_test_001", "A") == "running"

    @pytest.mark.asyncio
    async def test_ready_step_survives_when_pool_has_worker(self, redis, db, config):
        s = Scheduler(redis, db, config)
        await redis.register_worker(
            "w1", {"type": "cpu", "pools": "cpu,io", "status": "idle"}
        )
        job = make_job()
        db.create_job(job)
        await s.submit_job(job)
        await redis.set_step_status("j_test_001", "B", "skipped")
        await redis.set_step_status("j_test_001", "C", "skipped")
        await s._check_downstream("j_test_001")
        assert await redis.get_step_status("j_test_001", "A") == "ready"


class TestNoWorkerFailFast:
    """check_no_worker:无 running 且所有 ready 步的 pool 无 worker、超宽限期 → fail-fast,
    避免未部署 gpu worker 时 audio 永久挂起。用真实 _pool_has_workers。"""

    @pytest.mark.asyncio
    async def test_stuck_job_fails_after_grace(self, redis, db, config):
        s = Scheduler(redis, db, config)
        s._NO_WORKER_GRACE_SEC = 0  # 立即判定,免等宽限
        job = make_job()
        db.create_job(job)
        await s.submit_job(job)  # A=ready(cpu 无 worker), B/C=waiting
        await s.check_no_worker()
        assert "j_test_001" not in await redis.get_active_jobs()
        assert (await asyncio.to_thread(db.get_job, "j_test_001")).status == JobStatus.FAILED

    @pytest.mark.asyncio
    async def test_within_grace_not_failed(self, redis, db, config):
        s = Scheduler(redis, db, config)  # 默认宽限 90s
        job = make_job()
        db.create_job(job)
        await s.submit_job(job)
        await s.check_no_worker()  # 首次只记时,不该失败
        assert "j_test_001" in await redis.get_active_jobs()

    @pytest.mark.asyncio
    async def test_not_failed_when_pool_has_worker(self, redis, db, config):
        s = Scheduler(redis, db, config)
        s._NO_WORKER_GRACE_SEC = 0
        await redis.register_worker(
            "w1", {"type": "cpu", "pools": "cpu,io", "status": "idle"}
        )
        job = make_job()
        db.create_job(job)
        await s.submit_job(job)
        await s.check_no_worker()  # cpu 有 worker → 可推进,不失败
        assert "j_test_001" in await redis.get_active_jobs()

    @pytest.mark.asyncio
    async def test_worker_availability_cached_across_jobs(self, redis, db, config):
        s = Scheduler(redis, db, config)
        calls = []

        async def _has_workers(pool, require_tags):
            calls.append((pool, tuple(require_tags)))
            return True

        s._pool_has_workers_for = _has_workers
        for jid in ["j_test_001", "j_test_002", "j_test_003"]:
            job = make_job(job_id=jid)
            db.create_job(job)
            await s.submit_job(job)

        await s.check_no_worker()

        assert calls == [("cpu", ())]
        assert set(await redis.get_active_jobs()) == {
            "j_test_001", "j_test_002", "j_test_003",
        }

    @pytest.mark.asyncio
    async def test_running_step_not_failed(self, redis, db, config):
        s = Scheduler(redis, db, config)
        s._NO_WORKER_GRACE_SEC = 0
        job = make_job()
        db.create_job(job)
        await s.submit_job(job)
        await redis.set_step_status("j_test_001", "A", "running")
        await s.check_no_worker()  # 有 running 步 → 在推进,不失败
        assert "j_test_001" in await redis.get_active_jobs()

    async def _bili_dl_job(self, redis, db, config, tmp_path, tmp_jobs_dir, configs_dir,
                           jid, worker_tags):
        pipelines = {"v": {"steps": [{"name": "01_download", "pool": "io", "depends_on": [], "tags": []}]}}
        cfg = make_config(tmp_path, tmp_jobs_dir, pipelines, configs_dir)
        s = Scheduler(redis, db, cfg)
        s._NO_WORKER_GRACE_SEC = 0
        await redis.register_worker("w_io", {"type": "io", "pools": "io",
                                             "tags": worker_tags, "status": "idle"})
        job = Job(id=jid, content_type="video", pipeline="v")
        db.create_job(job)
        await redis.init_job(jid, "v", {"source": "bilibili", "url": "https://b23.tv/x"})
        await redis.set_step_status(jid, "01_download", "ready")
        await redis.add_active_job(jid)
        await s.check_no_worker()

    @pytest.mark.asyncio
    async def test_cn_download_no_zone_worker_fails(self, redis, db, config, tmp_path, tmp_jobs_dir, configs_dir):
        """B站源的 01_download 落 io 池但 io worker 不覆盖 net-cn → 超宽限 fail-fast。
        只看池不看 tag 会误判可推进、永久卡 ready。"""
        await self._bili_dl_job(redis, db, config, tmp_path, tmp_jobs_dir, configs_dir, "j_nozone", "")
        assert "j_nozone" not in await redis.get_active_jobs()

    @pytest.mark.asyncio
    async def test_cn_download_with_zone_worker_ok(self, redis, db, config, tmp_path, tmp_jobs_dir, configs_dir):
        """io worker 覆盖 net-cn → 满足 B站下载 require {net-cn},不 fail-fast。"""
        await self._bili_dl_job(redis, db, config, tmp_path, tmp_jobs_dir, configs_dir, "j_haszone", "net-cn")
        assert "j_haszone" in await redis.get_active_jobs()


class TestMarkdownToText:
    """_markdown_to_text 在入 FTS 索引前剥 HTML 标签,断高亮 snippet XSS 之源。"""

    def test_preserves_headings_paragraphs_and_fenced_code_body(self):
        from scheduler.scheduler import _markdown_to_text

        out = _markdown_to_text(
            "# 主标题\n\n"
            "第一段保留。\n\n"
            "```python\nprint('fenced-code-token')\n```\n\n"
            "## 次级标题\n\n第二段保留。\n"
        )

        assert out.startswith("# 主标题\n\n")
        assert "\n\n第一段保留。\n\n" in out
        assert "print('fenced-code-token')" in out
        assert "```" not in out
        assert "\n\n## 次级标题\n\n第二段保留。" in out

    def test_preserves_fenced_code_indentation_at_document_edges(self):
        from scheduler.scheduler import _markdown_to_text

        out = _markdown_to_text(
            "```python\n    if ready:\n        print('keep-indent')\n```"
        )
        assert out == "    if ready:\n        print('keep-indent')"

    def test_strips_html_tags(self):
        from scheduler.scheduler import _markdown_to_text
        out = _markdown_to_text("天 <img src=x onerror=alert(1)> 气 <script>bad</script>")
        assert "<" not in out and ">" not in out
        assert "onerror" not in out
        assert "天" in out and "气" in out


class TestDAGProgression:
    @pytest.mark.asyncio
    async def test_linear_chain(self, scheduler, redis, db):
        """A done → B ready → B done → C ready"""
        job = make_job()
        db.create_job(job)
        await scheduler.submit_job(job)

        await redis.set_step_status("j_test_001", "A", "running")
        await scheduler.on_step_done("j_test_001", "A")

        assert await redis.get_step_status("j_test_001", "B") == "ready"
        assert await redis.get_step_status("j_test_001", "C") == "waiting"

        await redis.set_step_status("j_test_001", "B", "running")
        await scheduler.on_step_done("j_test_001", "B")

        assert await redis.get_step_status("j_test_001", "C") == "ready"

    @pytest.mark.asyncio
    async def test_parallel_join(self, scheduler, redis, db, tmp_path, tmp_jobs_dir, parallel_pipelines, configs_dir):
        """A → {B, C} → D. D waits for both B and C."""
        config = make_config(tmp_path, tmp_jobs_dir, parallel_pipelines, configs_dir)
        sched = _stub_workers_present(Scheduler(redis, db, config))

        job = make_job(pipeline="par")
        db.create_job(job)
        await sched.submit_job(job)

        await redis.set_step_status("j_test_001", "A", "running")
        await sched.on_step_done("j_test_001", "A")

        assert await redis.get_step_status("j_test_001", "B") == "ready"
        assert await redis.get_step_status("j_test_001", "C") == "ready"
        assert await redis.get_step_status("j_test_001", "D") == "waiting"

        await redis.set_step_status("j_test_001", "B", "running")
        await sched.on_step_done("j_test_001", "B")
        assert await redis.get_step_status("j_test_001", "D") == "waiting"

        await redis.set_step_status("j_test_001", "C", "running")
        await sched.on_step_done("j_test_001", "C")
        assert await redis.get_step_status("j_test_001", "D") == "ready"

    @pytest.mark.asyncio
    async def test_mark_job_done_when_all_complete(self, scheduler, redis, db):
        job = make_job()
        db.create_job(job)
        await scheduler.submit_job(job)

        for step in ["A", "B", "C"]:
            await redis.set_step_status("j_test_001", step, "running")
            await scheduler.on_step_done("j_test_001", step)

        db_job = db.get_job("j_test_001")
        assert db_job.status == JobStatus.DONE

        active = await redis.get_active_jobs()
        assert "j_test_001" not in active


class TestSkipPropagation:
    @pytest.mark.asyncio
    async def test_skipped_unblocks_downstream(self, scheduler, redis, db):
        """A done, B skipped → C should become ready (skipped counts as done for deps)."""
        job = make_job()
        db.create_job(job)
        await scheduler.submit_job(job)

        await redis.set_step_status("j_test_001", "A", "running")
        await scheduler.on_step_done("j_test_001", "A")

        await _skip_step(scheduler, redis, db, "j_test_001", "B")

        assert await redis.get_step_status("j_test_001", "C") == "ready"

    @pytest.mark.asyncio
    async def test_all_skipped_marks_job_done(self, scheduler, redis, db):
        job = make_job()
        db.create_job(job)
        await scheduler.submit_job(job)

        await redis.set_step_status("j_test_001", "A", "running")
        await scheduler.on_step_done("j_test_001", "A")
        await _skip_step(scheduler, redis, db, "j_test_001", "B")

        await redis.set_step_status("j_test_001", "C", "running")
        await scheduler.on_step_done("j_test_001", "C")

        db_job = db.get_job("j_test_001")
        assert db_job.status == JobStatus.DONE


class TestConditions:
    @pytest.mark.asyncio
    async def test_has_subtitle_true(self, scheduler, tmp_jobs_dir):
        job_dir = tmp_jobs_dir / "j_cond"
        (job_dir / "input").mkdir(parents=True)
        (job_dir / "input" / "test.srt").write_text("subtitle")

        assert await scheduler.check_condition("j_cond", "has_subtitle") is True
        assert await scheduler.check_condition("j_cond", "no_subtitle") is False

    @pytest.mark.asyncio
    async def test_has_subtitle_false(self, scheduler, tmp_jobs_dir):
        job_dir = tmp_jobs_dir / "j_cond2"
        (job_dir / "input").mkdir(parents=True)

        assert await scheduler.check_condition("j_cond2", "has_subtitle") is False
        assert await scheduler.check_condition("j_cond2", "no_subtitle") is True

    @pytest.mark.asyncio
    async def test_has_danmaku(self, scheduler, tmp_jobs_dir):
        job_dir = tmp_jobs_dir / "j_cond3"
        (job_dir / "input").mkdir(parents=True)
        (job_dir / "input" / "danmaku.ass").write_text("ass")

        assert await scheduler.check_condition("j_cond3", "has_danmaku") is True

    @pytest.mark.asyncio
    async def test_nonexistent_dir(self, scheduler):
        assert await scheduler.check_condition("j_nodir", "has_subtitle") is False
        assert await scheduler.check_condition("j_nodir", "no_subtitle") is True

    @pytest.mark.asyncio
    async def test_unknown_condition_defaults_true(self, scheduler, tmp_jobs_dir):
        # 文档化契约(见 scheduler.py check_condition):未知条件名默认 True,放行而不静默跳过该步。
        # 配置期无条件名白名单,故此默认是误拼/打错条件名时的安全兜底,值得钉死。
        (tmp_jobs_dir / "j_cond_unknown" / "input").mkdir(parents=True)
        assert await scheduler.check_condition("j_cond_unknown", "bogus_condition") is True

    @pytest.mark.asyncio
    async def test_unknown_condition_returns_true(self, scheduler):
        """Unknown conditions should default to True."""
        result = await scheduler.check_condition("j_any", "some_new_condition")
        assert result is True

    @pytest.mark.asyncio
    async def test_condition_uses_storage_not_local_disk(self, redis, db, config):
        """分布式部署:产物在对象存储、调度器本地盘为空。条件须查 storage,否则
        has_subtitle/has_danmaku 永远 False → 05/06 被误跳、whisper 误跑。"""
        from unittest.mock import AsyncMock
        storage = AsyncMock()
        storage.list_files.return_value = ["input/subtitle.srt", "input/danmaku.ass", "input/source.mp4"]
        s = _stub_workers_present(Scheduler(redis, db, config, storage=storage))
        # 本地 jobs_dir 完全没有该 job 的文件,仅靠 storage 判断
        assert await s.check_condition("j_remote", "has_subtitle") is True
        assert await s.check_condition("j_remote", "has_danmaku") is True
        assert await s.check_condition("j_remote", "no_subtitle") is False
        # 声明式 rules 同源
        assert await s._eval_rules("j_remote", [{"exists": "input/*.srt", "when": "skip"}]) is False
        assert await s._eval_rules("j_remote", [{"exists": "input/*.ass", "when": True}]) is True

    @pytest.mark.asyncio
    async def test_rules_if_flag(self, redis, db, config):
        """if_flag:投递开关求值——flag 真→本条生效(run);假→落兜底规则(skip)。
        article 链的 smart_note 用此机制让智能笔记/评审可选。"""
        from unittest.mock import AsyncMock
        storage = AsyncMock()
        storage.list_files.return_value = []
        s = _stub_workers_present(Scheduler(redis, db, config, storage=storage))
        rules = [{"if_flag": "smart_note", "when": "on"}, {"when": "skip"}]
        await redis.init_job("j_on", "article", {"flags": {"smart_note": True}})
        assert await s._eval_rules("j_on", rules) is True
        await redis.init_job("j_off", "article", {"flags": {"smart_note": False}})
        assert await s._eval_rules("j_off", rules) is False
        await redis.init_job("j_none", "article", {})   # 无 flags 视为假,落 skip
        assert await s._eval_rules("j_none", rules) is False

    @pytest.mark.asyncio
    async def test_condition_storage_empty_means_absent(self, redis, db, config):
        from unittest.mock import AsyncMock
        storage = AsyncMock()
        storage.list_files.return_value = []
        s = _stub_workers_present(Scheduler(redis, db, config, storage=storage))
        assert await s.check_condition("j_empty", "has_subtitle") is False
        assert await s.check_condition("j_empty", "no_subtitle") is True


class TestWhisperPunctuateRecheck:
    @pytest.mark.asyncio
    async def test_skipped_revives_when_condition_met(
        self, redis, db, tmp_path, tmp_jobs_dir, configs_dir
    ):
        """02_whisper generates srt → skipped 08_punctuate revives to ready."""
        pipelines = {
            "video_mini": {
                "steps": [
                    {"name": "01_download", "pool": "io", "depends_on": []},
                    {"name": "02_whisper", "pool": "gpu", "depends_on": ["01_download"],
                     "condition": "no_subtitle", "tags": ["gpu"]},
                    {"name": "08_punctuate", "pool": "ai", "depends_on": ["01_download"],
                     "condition": "has_subtitle"},
                    {"name": "09_mechanical", "pool": "io",
                     "depends_on": ["08_punctuate"]},
                ]
            }
        }
        config = make_config(tmp_path, tmp_jobs_dir, pipelines, configs_dir)
        sched = _stub_workers_present(Scheduler(redis, db, config))

        job = make_job(pipeline="video_mini")
        db.create_job(job)
        await sched.submit_job(job)

        job_dir = tmp_jobs_dir / "j_test_001"
        (job_dir / "input").mkdir(parents=True)

        await redis.set_step_status("j_test_001", "01_download", "running")
        await sched.on_step_done("j_test_001", "01_download")

        assert await redis.get_step_status("j_test_001", "02_whisper") == "ready"
        assert await redis.get_step_status("j_test_001", "08_punctuate") == "skipped"

        (job_dir / "input" / "generated.srt").write_text("whisper output")

        await redis.set_step_status("j_test_001", "02_whisper", "running")
        await sched.on_step_done("j_test_001", "02_whisper")

        assert await redis.get_step_status("j_test_001", "08_punctuate") == "ready"


class TestNewFormatConsumption:
    """调度器消费归一化后的新格式(needs→DAG、rules→skip/run):行为与直写 steps 的格式等价。"""

    def _normalized(self, raw_new):
        from shared.config import normalize_pipelines
        return normalize_pipelines(raw_new)

    @pytest.mark.asyncio
    async def test_needs_produce_dag_order(
        self, redis, db, tmp_path, tmp_jobs_dir, configs_dir
    ):
        # needs 声明的依赖边归一化为 depends_on,调度器据此推进 DAG。
        raw_new = {
            "default": {"image": "flori/step-base"},
            "nf": {
                "jobs": {
                    "A": {"run": "m.a", "pool": "cpu"},
                    "B": {"run": "m.b", "pool": "cpu", "needs": ["A"]},
                    "C": {"run": "m.c", "pool": "cpu", "needs": ["B"]},
                }
            },
        }
        config = make_config(tmp_path, tmp_jobs_dir, self._normalized(raw_new), configs_dir)
        sched = _stub_workers_present(Scheduler(redis, db, config))

        job = make_job(pipeline="nf")
        db.create_job(job)
        await sched.submit_job(job)

        assert await redis.get_step_status("j_test_001", "A") == "ready"
        assert await redis.get_step_status("j_test_001", "B") == "waiting"

        await redis.set_step_status("j_test_001", "A", "running")
        await sched.on_step_done("j_test_001", "A")
        assert await redis.get_step_status("j_test_001", "B") == "ready"
        assert await redis.get_step_status("j_test_001", "C") == "waiting"

    @pytest.mark.asyncio
    async def test_rules_exists_skip_and_run(
        self, redis, db, tmp_path, tmp_jobs_dir, configs_dir
    ):
        # rules: exists+when=skip → 有 srt 则跳过 whisper;exists+when=on → 有 srt 才跑 punctuate。
        raw_new = {
            "default": {"image": "flori/step-base"},
            "nf": {
                "jobs": {
                    "download": {"run": "m.dl", "pool": "io"},
                    "whisper": {
                        "run": "m.ws", "pool": "gpu", "needs": ["download"],
                        "rules": [{"exists": "input/*.srt", "when": "skip"}],
                    },
                    "punctuate": {
                        "run": "m.pu", "pool": "ai", "needs": ["download"],
                        "rules": [{"exists": "input/*.srt", "when": "on"}],
                    },
                }
            },
        }
        config = make_config(tmp_path, tmp_jobs_dir, self._normalized(raw_new), configs_dir)
        sched = _stub_workers_present(Scheduler(redis, db, config))

        job = make_job(pipeline="nf")
        db.create_job(job)
        await sched.submit_job(job)

        job_dir = tmp_jobs_dir / "j_test_001"
        (job_dir / "input").mkdir(parents=True)
        (job_dir / "input" / "subs.srt").write_text("srt")

        await redis.set_step_status("j_test_001", "download", "running")
        await sched.on_step_done("j_test_001", "download")

        # 已有 srt:whisper(when=skip) 跳过,punctuate(when=on) 运行。
        assert await redis.get_step_status("j_test_001", "whisper") == "skipped"
        assert await redis.get_step_status("j_test_001", "punctuate") == "ready"

    @pytest.mark.asyncio
    async def test_rules_exists_run_when_no_match(
        self, redis, db, tmp_path, tmp_jobs_dir, configs_dir
    ):
        # 无 srt:whisper(when=skip,未命中→默认运行) 运行。
        raw_new = {
            "default": {"image": "flori/step-base"},
            "nf": {
                "jobs": {
                    "download": {"run": "m.dl", "pool": "io"},
                    "whisper": {
                        "run": "m.ws", "pool": "gpu", "needs": ["download"],
                        "rules": [{"exists": "input/*.srt", "when": "skip"}],
                    },
                }
            },
        }
        config = make_config(tmp_path, tmp_jobs_dir, self._normalized(raw_new), configs_dir)
        sched = _stub_workers_present(Scheduler(redis, db, config))

        job = make_job(pipeline="nf")
        db.create_job(job)
        await sched.submit_job(job)

        job_dir = tmp_jobs_dir / "j_test_001"
        (job_dir / "input").mkdir(parents=True)  # 无 srt

        await redis.set_step_status("j_test_001", "download", "running")
        await sched.on_step_done("j_test_001", "download")

        # 无 srt:whisper 的 skip 规则未命中 → 默认运行。
        assert await redis.get_step_status("j_test_001", "whisper") == "ready"


class TestRetry:
    @pytest.mark.asyncio
    async def test_retry_within_limit(self, scheduler, redis, db):
        job = make_job()
        db.create_job(job)
        await scheduler.submit_job(job)

        await redis.set_step_status("j_test_001", "A", "running")
        await scheduler.on_step_failed("j_test_001", "A", "some error", "processing")

        assert await redis.get_step_status("j_test_001", "A") == "ready"
        assert await redis.get_step_retries("j_test_001", "A") == 1

    @pytest.mark.asyncio
    async def test_retry_exhausted(self, scheduler, redis, db):
        job = make_job()
        db.create_job(job)
        await scheduler.submit_job(job)

        await redis.set_step_status("j_test_001", "C", "running")
        await scheduler.on_step_failed("j_test_001", "C", "fatal", "processing")

        assert await redis.get_step_status("j_test_001", "C") == "failed"
        db_job = db.get_job("j_test_001")
        assert db_job.status == JobStatus.FAILED

    @pytest.mark.asyncio
    async def test_input_missing_no_retry(self, scheduler, redis, db):
        job = make_job()
        db.create_job(job)
        await scheduler.submit_job(job)

        await redis.set_step_status("j_test_001", "A", "running")
        await scheduler.on_step_failed("j_test_001", "A", "missing file", "input_missing")

        assert await redis.get_step_status("j_test_001", "A") == "failed"


class TestRetryByFailureType:
    """失败类型矩阵:BUILD 不重试直接标 failed;SYSTEM 退避重试至上限。
    pipeline_retries 与 RETRY_POLICY.max 取 min:用户配置只能收紧不能放大。"""

    async def _drain_delayed(self, scheduler):
        """ai 类延迟重试经 create_task 异步入队,等其完成再断言。"""
        await asyncio.sleep(0)
        if scheduler._delayed_tasks:
            await asyncio.gather(*list(scheduler._delayed_tasks))

    @pytest.mark.asyncio
    async def test_input_invalid_not_retried(self, scheduler, redis, db):
        # BUILD:input_invalid 确定性失败,A 直接标 failed,job 失败。
        job = make_job()
        db.create_job(job)
        await scheduler.submit_job(job)

        await redis.set_step_status("j_test_001", "A", "running")
        await scheduler.on_step_failed("j_test_001", "A", "bad json", "input_invalid")

        assert await redis.get_step_status("j_test_001", "A") == "failed"
        assert await redis.get_step_retries("j_test_001", "A") == 0
        assert db.get_job("j_test_001").status == JobStatus.FAILED

    @pytest.mark.asyncio
    async def test_unknown_not_retried(self, scheduler, redis, db):
        # 缺表项 unknown 走 BUILD 兜底:不重试。
        job = make_job()
        db.create_job(job)
        await scheduler.submit_job(job)

        await redis.set_step_status("j_test_001", "A", "running")
        await scheduler.on_step_failed("j_test_001", "A", "boom", "unknown")

        assert await redis.get_step_status("j_test_001", "A") == "failed"
        assert await redis.get_step_retries("j_test_001", "A") == 0

    @pytest.mark.asyncio
    async def test_ai_retried_with_configured_delay(self, scheduler, redis, db):
        # SYSTEM:A(pipeline retries=2) 失败 ai → 重试,且用 policy 的 30s 延迟。
        job = make_job()
        db.create_job(job)
        await scheduler.submit_job(job)
        await redis.dequeue_step("cpu")  # drain A 入队

        captured = []

        async def mock_delayed(delay, job_id, step):
            captured.append(delay)
            await scheduler.enqueue_step(job_id, step)

        scheduler._delayed_enqueue = mock_delayed

        await redis.set_step_status("j_test_001", "A", "running")
        await scheduler.on_step_failed("j_test_001", "A", "5xx", "ai")
        await self._drain_delayed(scheduler)

        assert captured == [30]  # ai 首次退避
        assert await redis.get_step_status("j_test_001", "A") == "ready"
        assert await redis.get_step_retries("j_test_001", "A") == 1

    @pytest.mark.asyncio
    async def test_ai_capped_by_pipeline_retries(self, scheduler, redis, db):
        # pipeline_retries 封顶:B(retries=1) 把 ai 的 max 3 收紧到 1。
        # 第一次失败重试;第二次失败已达上限,标 failed。
        job = make_job()
        db.create_job(job)
        await scheduler.submit_job(job)

        delays = []

        async def mock_delayed(delay, job_id, step):
            delays.append(delay)
            await scheduler.enqueue_step(job_id, step)

        scheduler._delayed_enqueue = mock_delayed

        # 第一次 ai 失败:current_retries 0 < min(3,1)=1 → 重试。
        await redis.set_step_status("j_test_001", "B", "running")
        await scheduler.on_step_failed("j_test_001", "B", "5xx", "ai")
        await self._drain_delayed(scheduler)
        assert await redis.get_step_status("j_test_001", "B") == "ready"
        assert await redis.get_step_retries("j_test_001", "B") == 1

        # 第二次 ai 失败:current_retries 1 >= 1 → 不再重试,标 failed。
        await redis.set_step_status("j_test_001", "B", "running")
        await scheduler.on_step_failed("j_test_001", "B", "5xx again", "ai")
        await self._drain_delayed(scheduler)
        assert await redis.get_step_status("j_test_001", "B") == "failed"
        assert delays == [30]  # 只重试过一次
        assert db.get_job("j_test_001").status == JobStatus.FAILED

    @pytest.mark.asyncio
    async def test_ai_capped_by_policy_max(self, scheduler, redis, db):
        # policy_max 封顶反向:A 的 pipeline retries=2 仍受 ai 的 max=3 约束,
        # min(3,2)=2,故 A 可重试两次(验证 policy.max 不会被 pipeline 放大)。
        job = make_job()
        db.create_job(job)
        await scheduler.submit_job(job)
        await redis.dequeue_step("cpu")

        delays = []

        async def mock_delayed(delay, job_id, step):
            delays.append(delay)
            await scheduler.enqueue_step(job_id, step)

        scheduler._delayed_enqueue = mock_delayed

        for expected_retries in (1, 2):
            await redis.set_step_status("j_test_001", "A", "running")
            await scheduler.on_step_failed("j_test_001", "A", "5xx", "ai")
            await self._drain_delayed(scheduler)
            assert await redis.get_step_retries("j_test_001", "A") == expected_retries

        # 第三次:current_retries 2 >= min(3,2)=2 → 停止重试。
        await redis.set_step_status("j_test_001", "A", "running")
        await scheduler.on_step_failed("j_test_001", "A", "5xx", "ai")
        await self._drain_delayed(scheduler)
        assert await redis.get_step_status("j_test_001", "A") == "failed"
        assert delays == [30, 60]  # 两次退避,第三次不再重试


class TestIdempotent:
    @pytest.mark.asyncio
    async def test_duplicate_step_done(self, scheduler, redis, db):
        job = make_job()
        db.create_job(job)
        await scheduler.submit_job(job)

        await redis.dequeue_step("cpu")  # drain A from queue (simulate Worker take)
        await redis.set_step_status("j_test_001", "A", "running")
        await scheduler.on_step_done("j_test_001", "A")
        await scheduler.on_step_done("j_test_001", "A")  # duplicate — should be no-op

        queue = await redis.get_queue_info("cpu")
        assert queue["length"] == 1  # only B, not B+B

    @pytest.mark.asyncio
    async def test_step_done_wrong_status_ignored(self, scheduler, redis, db):
        job = make_job()
        db.create_job(job)
        await scheduler.submit_job(job)

        await scheduler.on_step_done("j_test_001", "A")

        assert await redis.get_step_status("j_test_001", "A") == "ready"


class TestOrphanScan:
    @pytest.mark.asyncio
    async def test_reclaims_lost_worker(self, scheduler, redis, db, monkeypatch):
        job = make_job()
        db.create_job(job)
        await scheduler.submit_job(job)

        await redis.set_step_status("j_test_001", "A", "running")
        await redis.set_step_worker("j_test_001", "A", "cpu-dead")

        received = []

        async def collect():
            async for msg in redis.subscribe("step_failed"):
                received.append(msg)
                break

        ready = subscription_barrier(redis, monkeypatch)
        task = asyncio.create_task(collect())
        await asyncio.wait_for(ready.wait(), timeout=1.0)
        await scheduler.orphan_scan()
        await asyncio.wait_for(task, timeout=2.0)

        assert len(received) == 1
        assert "orphan reclaimed" in received[0]["error"]

    @pytest.mark.asyncio
    async def test_skips_alive_worker(self, scheduler, redis, db):
        job = make_job()
        db.create_job(job)
        await scheduler.submit_job(job)

        await redis.set_step_status("j_test_001", "A", "running")
        await redis.set_step_worker("j_test_001", "A", "cpu-alive")
        await redis.register_worker("cpu-alive", {"type": "cpu", "status": "busy"})

        await scheduler.orphan_scan()

        assert await redis.get_step_status("j_test_001", "A") == "running"

    @pytest.mark.asyncio
    async def test_reclaim_releases_pool_slot(
        self, scheduler, redis, db, tmp_path, tmp_jobs_dir, configs_dir, monkeypatch,
    ):
        """Orphan reclaim should release the pool slot."""
        pipelines = {
            "test": {
                "steps": [
                    {"name": "A", "pool": "cpu", "depends_on": [], "retries": 2},
                    {"name": "B", "pool": "cpu", "depends_on": ["A"], "retries": 1},
                    {"name": "C", "pool": "cpu", "depends_on": ["B"], "retries": 0},
                ]
            }
        }
        config = make_config(tmp_path, tmp_jobs_dir, pipelines, configs_dir)
        sched = Scheduler(redis, db, config)

        job = make_job()
        db.create_job(job)
        await sched.submit_job(job)

        await redis.set_step_status("j_test_001", "A", "running")
        await redis.set_step_worker("j_test_001", "A", "cpu-dead")
        await redis.set_step_exec_id("j_test_001", "A", "exec_dead")  # holder,reclaim 据此 SREM 释放
        await redis.try_acquire_slot("cpu", 3, "exec_dead")
        assert await redis.get_pool_count("cpu") == 1

        received = []
        async def collect():
            async for msg in redis.subscribe("step_failed"):
                received.append(msg)
                break

        ready = subscription_barrier(redis, monkeypatch)
        task = asyncio.create_task(collect())
        await asyncio.wait_for(ready.wait(), timeout=1.0)
        await sched.orphan_scan()
        await asyncio.wait_for(task, timeout=2.0)

        # Pool slot should be released
        assert await redis.get_pool_count("cpu") == 0


class TestReconcileSlots:
    """周期对账并发槽(holder 集合):清掉"持槽但不属任何 running 步"的陈旧 holder,宽限避认领窗口误清。"""

    @pytest.mark.asyncio
    async def test_grace_then_release_stale_keeps_live(
        self, redis, db, tmp_path, tmp_jobs_dir, configs_dir
    ):
        pipelines = {"test": {"steps": [
            {"name": "A", "pool": "cpu", "depends_on": [], "retries": 0},
        ]}}
        config = make_config(tmp_path, tmp_jobs_dir, pipelines, configs_dir)
        sched = Scheduler(redis, db, config)
        db.create_job(make_job())
        await redis.add_active_job("j_test_001")
        # 合法持有者:一个 live running 步,holder=h_live。
        await redis.set_step_status("j_test_001", "A", "running")
        await redis.set_step_exec_id("j_test_001", "A", "h_live")
        await redis.try_acquire_slot("cpu", 9, "h_live")
        # 陈旧:占了 io 槽但没有任何 running 步指向它(模拟 worker 突死/删 job 漏放泄漏)。
        await redis.try_acquire_slot("io", 9, "h_stale")
        assert await redis.get_pool_count("io") == 1

        # 第一拍:仅把 h_stale 记为 suspect,宽限期内不清(避免误清刚占槽尚未写状态的认领)。
        await sched.reconcile_slots()
        assert await redis.get_pool_count("io") == 1
        assert "h_stale" in sched._slot_reconcile_suspect
        assert "h_live" not in sched._slot_reconcile_suspect   # 合法持有者从不进 suspect

        # 第二拍:连续两拍都陈旧 → SREM 清掉;合法持有者 h_live 始终保留。
        await sched.reconcile_slots()
        assert await redis.get_pool_count("io") == 0
        assert await redis.get_pool_count("cpu") == 1
        assert await redis.get_all_holders() == {"h_live"}

    @pytest.mark.asyncio
    async def test_inflight_claim_not_removed_within_grace(
        self, redis, db, tmp_path, tmp_jobs_dir, configs_dir
    ):
        # 认领窗口保护:刚占槽、尚未写 running 状态的 holder,不能在一拍内被误清。
        pipelines = {"test": {"steps": [
            {"name": "A", "pool": "cpu", "depends_on": [], "retries": 0},
        ]}}
        config = make_config(tmp_path, tmp_jobs_dir, pipelines, configs_dir)
        sched = Scheduler(redis, db, config)
        await redis.try_acquire_slot("cpu", 9, "h_inflight")   # 认领中,状态还没落

        await sched.reconcile_slots()                          # 第一拍:不清(仅 suspect)
        assert await redis.get_pool_count("cpu") == 1

        # 认领完成:写了 running 状态 + exec_id → 它成 live,第二拍不再 suspect、不清。
        db.create_job(make_job())
        await redis.add_active_job("j_test_001")
        await redis.set_step_status("j_test_001", "A", "running")
        await redis.set_step_exec_id("j_test_001", "A", "h_inflight")
        await sched.reconcile_slots()
        assert await redis.get_pool_count("cpu") == 1          # 认领完成,槽保留


class TestCheckStuck:
    @pytest.mark.asyncio
    async def test_detects_stale_progress(
        self, scheduler, redis, db, tmp_jobs_dir, monkeypatch,
    ):
        job = make_job()
        db.create_job(job)
        await scheduler.submit_job(job)
        await redis.set_step_status("j_test_001", "A", "running")

        job_dir = tmp_jobs_dir / "j_test_001"
        job_dir.mkdir(exist_ok=True)
        import time
        progress = {"source": "step", "updated_at": time.time() - 240, "current": 5, "total": 10}  # >180s 阈值(60→180:api recreate 心跳断不误杀)
        (job_dir / ".A.progress").write_text(json.dumps(progress))

        received = []

        async def collect():
            async for msg in redis.subscribe("step_failed"):
                received.append(msg)
                break

        ready = subscription_barrier(redis, monkeypatch)
        task = asyncio.create_task(collect())
        await asyncio.wait_for(ready.wait(), timeout=1.0)
        await scheduler.check_stuck()
        await asyncio.wait_for(task, timeout=2.0)

        assert "progress stale" in received[0]["error"]

    @pytest.mark.asyncio
    async def test_ignores_fresh_progress(self, scheduler, redis, db, tmp_jobs_dir):
        job = make_job()
        db.create_job(job)
        await scheduler.submit_job(job)
        await redis.set_step_status("j_test_001", "A", "running")

        job_dir = tmp_jobs_dir / "j_test_001"
        job_dir.mkdir(exist_ok=True)
        import time
        progress = {"source": "step", "updated_at": time.time(), "current": 5, "total": 10}
        (job_dir / ".A.progress").write_text(json.dumps(progress))

        await scheduler.check_stuck()
        assert await redis.get_step_status("j_test_001", "A") == "running"

    @pytest.mark.asyncio
    async def test_ignores_no_updated_at(self, scheduler, redis, db, tmp_jobs_dir):
        """Steps without report_progress (no updated_at) are not flagged."""
        job = make_job()
        db.create_job(job)
        await scheduler.submit_job(job)
        await redis.set_step_status("j_test_001", "A", "running")

        job_dir = tmp_jobs_dir / "j_test_001"
        job_dir.mkdir(exist_ok=True)
        import time
        progress = {"worker_heartbeat_at": time.time()}
        (job_dir / ".A.progress").write_text(json.dumps(progress))

        await scheduler.check_stuck()
        assert await redis.get_step_status("j_test_001", "A") == "running"

    @pytest.mark.asyncio
    async def test_worker_heartbeat_prevents_stuck(self, scheduler, redis, db, tmp_jobs_dir):
        """If step has stale updated_at but fresh worker_heartbeat_at, don't flag as stuck."""
        job = make_job()
        db.create_job(job)
        await scheduler.submit_job(job)
        await redis.set_step_status("j_test_001", "A", "running")

        job_dir = tmp_jobs_dir / "j_test_001"
        job_dir.mkdir(exist_ok=True)
        import time
        progress = {
            "updated_at": time.time() - 120,  # stale
            "worker_heartbeat_at": time.time(),  # fresh
        }
        (job_dir / ".A.progress").write_text(json.dumps(progress))

        await scheduler.check_stuck()
        # Should NOT be flagged as stuck because worker_heartbeat_at is fresh
        assert await redis.get_step_status("j_test_001", "A") == "running"


class TestPriority:
    @pytest.mark.asyncio
    async def test_advanced_job_higher_priority(self, scheduler, redis, db):
        job_a = make_job(job_id="j_advanced")
        job_b = make_job(job_id="j_fresh")
        db.create_job(job_a)
        db.create_job(job_b)
        await scheduler.submit_job(job_a)
        await scheduler.submit_job(job_b)

        # Drain initial A entries from queue (simulate Workers taking them)
        await redis.dequeue_step("cpu")  # j_advanced A or j_fresh A
        await redis.dequeue_step("cpu")  # the other A

        for step in ["A", "B"]:
            await redis.set_step_status("j_advanced", step, "running")
            await scheduler.on_step_done("j_advanced", step)

        await redis.set_step_status("j_fresh", "A", "running")
        await scheduler.on_step_done("j_fresh", "A")

        # j_advanced C (score=-2) should be higher priority than j_fresh B (score=-1)
        item1, score1 = await redis.dequeue_step("cpu")
        assert item1["job_id"] == "j_advanced"
        assert item1["step"] == "C"

        item2, score2 = await redis.dequeue_step("cpu")
        assert score1 < score2  # -2 < -1


class TestRerun:
    @pytest.mark.asyncio
    async def test_resets_downstream(self, scheduler, redis, db, tmp_jobs_dir):
        job = make_job()
        db.create_job(job)
        await scheduler.submit_job(job)

        for step in ["A", "B", "C"]:
            await redis.set_step_status("j_test_001", step, "running")
            await scheduler.on_step_done("j_test_001", step)

        job_dir = tmp_jobs_dir / "j_test_001"
        job_dir.mkdir(exist_ok=True)
        for step in ["B", "C"]:
            (job_dir / f".{step}.done").write_text("{}")

        reset = await scheduler.rerun("j_test_001", "B")
        assert set(reset) == {"B", "C"}

        assert await redis.get_step_status("j_test_001", "A") == "done"
        assert await redis.get_step_status("j_test_001", "B") == "ready"
        assert await redis.get_step_status("j_test_001", "C") == "waiting"

        assert not (job_dir / ".B.done").exists()
        assert not (job_dir / ".C.done").exists()


class TestRecover:
    @pytest.mark.asyncio
    async def test_recovers_orphaned_running(
        self, scheduler, redis, db, monkeypatch,
    ):
        job = make_job()
        db.create_job(job)
        await scheduler.submit_job(job)

        await redis.set_step_status("j_test_001", "A", "running")
        await redis.set_step_worker("j_test_001", "A", "cpu-gone")

        received = []

        async def collect():
            async for msg in redis.subscribe("step_failed"):
                received.append(msg)
                break

        ready = subscription_barrier(redis, monkeypatch)
        task = asyncio.create_task(collect())
        await asyncio.wait_for(ready.wait(), timeout=1.0)
        await scheduler._recover()
        await asyncio.wait_for(task, timeout=2.0)

        assert len(received) == 1

    @pytest.mark.asyncio
    async def test_recovers_ready_steps(self, scheduler, redis, db):
        """If deps are satisfied but step is still waiting, _recover pushes it."""
        job = make_job()
        db.create_job(job)
        await scheduler.submit_job(job)

        await redis.set_step_status("j_test_001", "A", "done")
        await redis.set_step_status("j_test_001", "B", "waiting")

        await scheduler._recover()

        assert await redis.get_step_status("j_test_001", "B") == "ready"

    @pytest.mark.asyncio
    async def test_requeues_ready_orphan(self, scheduler, redis, db):
        """ready-but-not-queued 孤儿(置 ready→入队窗口重启/队列消息丢)→ recover 补投队列。"""
        job = make_job()
        db.create_job(job)
        await scheduler.submit_job(job)
        # 模拟孤儿:A 已 ready 且在队 → 抽干队列(消息丢失),状态仍 ready
        await redis.dequeue_step("cpu")
        assert (await redis.get_queue_info("cpu"))["length"] == 0
        assert await redis.get_step_status("j_test_001", "A") == "ready"

        await scheduler._recover()

        assert (await redis.get_queue_info("cpu"))["length"] == 1  # 补投回队

    @pytest.mark.asyncio
    async def test_requeue_ready_idempotent(self, scheduler, redis, db):
        """已在队的 ready 步,recover 重复补投不产生重复任务(ZADD 同成员幂等)。"""
        job = make_job()
        db.create_job(job)
        await scheduler.submit_job(job)   # A ready 且在队
        await scheduler._recover()
        assert (await redis.get_queue_info("cpu"))["length"] == 1  # 仍 1,无重复


class TestDelayedRetry:
    @pytest.mark.asyncio
    async def test_delayed_enqueue_with_ai_error(self, scheduler, redis, db):
        """error_type="ai" has delay=[30,60,120]. Verify delayed enqueue is triggered."""
        job = make_job()
        db.create_job(job)
        await scheduler.submit_job(job)
        await redis.dequeue_step("cpu")  # drain A

        await redis.set_step_status("j_test_001", "A", "running")

        captured_delays = []

        async def mock_delayed_enqueue(delay, job_id, step):
            captured_delays.append(delay)
            await scheduler.enqueue_step(job_id, step)

        scheduler._delayed_enqueue = mock_delayed_enqueue
        await scheduler.on_step_failed("j_test_001", "A", "rate limit", "ai")
        # _delayed_enqueue 经 create_task 异步运行,等其落盘完成再断言。
        await asyncio.sleep(0)
        await asyncio.gather(*scheduler._delayed_tasks)

        assert captured_delays == [30]
        assert await redis.get_step_status("j_test_001", "A") == "ready"
        assert await redis.get_step_retries("j_test_001", "A") == 1


class TestDelayedTaskTracking:
    """覆盖延迟重试任务的跟踪与取消(防泄漏 / shutdown / rerun 串台)。"""

    async def _trigger_delayed(self, scheduler, redis, db, hang):
        """触发一个 ai 延迟重试 task,并用 hang 替换 _delayed_enqueue 控制其存活。"""
        job = make_job()
        db.create_job(job)
        await scheduler.submit_job(job)
        await redis.dequeue_step("cpu")  # drain A
        await redis.set_step_status("j_test_001", "A", "running")
        scheduler._delayed_enqueue = hang
        await scheduler.on_step_failed("j_test_001", "A", "rate limit", "ai")
        await asyncio.sleep(0)

    @pytest.mark.asyncio
    async def test_task_added_to_set(self, scheduler, redis, db):
        async def _never(delay, job_id, step):
            await asyncio.Event().wait()
        await self._trigger_delayed(scheduler, redis, db, _never)
        assert len(scheduler._delayed_tasks) == 1
        task = next(iter(scheduler._delayed_tasks))
        assert task.get_name() == "delayed_enqueue:j_test_001:A"
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_shutdown_cancels_delayed_tasks(self, scheduler, redis, db):
        async def _sleep(delay, job_id, step):
            await asyncio.sleep(3600)
        await self._trigger_delayed(scheduler, redis, db, _sleep)
        task = next(iter(scheduler._delayed_tasks))
        await scheduler.shutdown()
        await asyncio.sleep(0)  # 让 done_callback(discard)执行
        assert task.cancelled()
        assert task.done()
        assert len(scheduler._delayed_tasks) == 0

    @pytest.mark.asyncio
    async def test_cancel_is_clean_no_enqueue(self, scheduler, redis, db):
        # 真实 _delayed_enqueue:delay 未到就取消 → enqueue 不发生,A 不被改回 ready
        job = make_job()
        db.create_job(job)
        await scheduler.submit_job(job)
        await redis.dequeue_step("cpu")
        await redis.set_step_status("j_test_001", "A", "running")
        await scheduler.on_step_failed("j_test_001", "A", "rate limit", "ai")
        await asyncio.sleep(0)
        task = next(iter(scheduler._delayed_tasks))
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert task.cancelled()
        assert await redis.get_step_status("j_test_001", "A") != "ready"

    @pytest.mark.asyncio
    async def test_rerun_cancels_pending_delayed(self, scheduler, redis, db, tmp_jobs_dir):
        async def _sleep(delay, job_id, step):
            await asyncio.sleep(3600)
        await self._trigger_delayed(scheduler, redis, db, _sleep)
        task = next(iter(scheduler._delayed_tasks))
        (tmp_jobs_dir / "j_test_001").mkdir(parents=True, exist_ok=True)
        await scheduler.rerun("j_test_001", "A")
        await asyncio.sleep(0)
        assert task.cancelled()
        assert await redis.get_step_status("j_test_001", "A") == "ready"


class TestConcurrentCAS:
    """覆盖跨进程并发的 CAS / 去重不变量(fakeredis + gather 验证返回值分支与最终态)。"""

    @pytest.mark.asyncio
    async def test_cas_ready_to_running_only_one_wins(self, redis):
        await redis.set_step_status("j_cas", "A", "ready")
        results = await asyncio.gather(
            redis.cas_step_status("j_cas", "A", "ready", "running"),
            redis.cas_step_status("j_cas", "A", "ready", "running"),
        )
        assert sorted(results) == [False, True]  # 仅一个 worker 抢到 ready→running
        assert await redis.get_step_status("j_cas", "A") == "running"

    @pytest.mark.asyncio
    async def test_duplicate_on_step_done_idempotent(self, scheduler, redis, db):
        job = make_job()
        db.create_job(job)
        await scheduler.submit_job(job)
        await redis.dequeue_step("cpu")  # drain A 的入队
        await redis.set_step_status("j_test_001", "A", "running")
        await asyncio.gather(
            scheduler.on_step_done("j_test_001", "A"),
            scheduler.on_step_done("j_test_001", "A"),
        )
        # 重复 done 只推进一次:CAS 仅一个成功 → B 仅入队一次
        assert await redis.get_step_status("j_test_001", "A") == "done"
        assert await redis.get_step_status("j_test_001", "B") == "ready"
        queue = await redis.get_queue_info("cpu")
        assert queue["length"] == 1

    @pytest.mark.asyncio
    async def test_record_ai_usage_exec_id_dedup(self, db):
        u1 = AIUsage(exec_id="e1", provider="kimi", model="k2", job_id="j", step="10_smart")
        u2 = AIUsage(exec_id="e1", provider="kimi", model="k2", job_id="j", step="10_smart")
        results = await asyncio.gather(
            asyncio.to_thread(db.record_ai_usage, u1),
            asyncio.to_thread(db.record_ai_usage, u2),
        )
        assert set(results) == {True, False}  # exec_id UNIQUE → 一成一败


class TestResubmit:
    @pytest.mark.asyncio
    async def test_adds_new_steps(self, scheduler, redis, db, tmp_path, tmp_jobs_dir, configs_dir):
        job = make_job()
        db.create_job(job)
        await scheduler.submit_job(job)

        for step in ["A", "B", "C"]:
            await redis.set_step_status("j_test_001", step, "running")
            await scheduler.on_step_done("j_test_001", step)

        # Add step D to pipeline
        new_pipelines = {
            "test": {
                "steps": [
                    {"name": "A", "pool": "cpu", "depends_on": [], "retries": 2},
                    {"name": "B", "pool": "cpu", "depends_on": ["A"], "retries": 1},
                    {"name": "C", "pool": "cpu", "depends_on": ["B"], "retries": 0},
                    {"name": "D", "pool": "io", "depends_on": ["C"]},
                ]
            }
        }
        scheduler.config = make_config(tmp_path, tmp_jobs_dir, new_pipelines, configs_dir)
        scheduler.reload_config = lambda: None  # skip file reload

        await scheduler.resubmit("j_test_001")

        assert await redis.get_step_status("j_test_001", "A") == "done"
        assert await redis.get_step_status("j_test_001", "B") == "done"
        assert await redis.get_step_status("j_test_001", "C") == "done"
        assert await redis.get_step_status("j_test_001", "D") == "ready"
        # DB 也要与 redis 步集一致:新步 D 不能只进 redis,须回填 DB,否则两侧分叉
        db_names = {s.name for s in db.get_steps("j_test_001")}
        assert "D" in db_names
        assert db_names == set(await redis.get_all_step_statuses("j_test_001")) == {"A", "B", "C", "D"}

    @pytest.mark.asyncio
    async def test_removes_deleted_steps(self, scheduler, redis, db, tmp_path, tmp_jobs_dir, configs_dir):
        job = make_job()
        db.create_job(job)
        await scheduler.submit_job(job)

        # Shrink pipeline: remove C
        new_pipelines = {
            "test": {
                "steps": [
                    {"name": "A", "pool": "cpu", "depends_on": []},
                    {"name": "B", "pool": "cpu", "depends_on": ["A"]},
                ]
            }
        }
        scheduler.config = make_config(tmp_path, tmp_jobs_dir, new_pipelines, configs_dir)
        scheduler.reload_config = lambda: None

        await scheduler.resubmit("j_test_001")

        assert await redis.get_step_status("j_test_001", "C") is None
        # C 也要从 DB 删除,不能只删 redis 而让 DB 残留旧步
        db_names = {s.name for s in db.get_steps("j_test_001")}
        assert "C" not in db_names
        assert db_names == set(await redis.get_all_step_statuses("j_test_001")) == {"A", "B"}

    @pytest.mark.asyncio
    async def test_resubmit_repairs_redis_db_divergence(self, scheduler, redis, db):
        """复现 redis/DB 分叉:DB 残留 pipeline 已无的旧步 + redis 丢了 pipeline 里的步,
        resubmit 后两侧步集都==当前 pipeline(A/B/C),流水线读 DB 不显示旧步、不漏新步。"""
        job = make_job()
        db.create_job(job)
        await scheduler.submit_job(job)              # A/B/C 物化到 redis+DB
        # 制造分叉:DB 残留已不在 pipeline 的旧步 X_old;redis 丢了 pipeline 里的 C
        db.upsert_step(Step(job_id="j_test_001", name="X_old", status=StepStatus.DONE, pool="cpu"))
        await redis.delete_step_status("j_test_001", "C")
        scheduler.reload_config = lambda: None         # 保持默认 A/B/C pipeline

        await scheduler.resubmit("j_test_001")

        db_names = {s.name for s in db.get_steps("j_test_001")}
        redis_names = set(await redis.get_all_step_statuses("j_test_001"))
        assert db_names == {"A", "B", "C"}             # X_old 从 DB 删除、C 在 DB
        assert redis_names == {"A", "B", "C"}          # C 补回 redis
        assert db_names == redis_names                 # 核心:两侧一致、无分叉

    @pytest.mark.asyncio
    async def test_resubmit_preserves_existing_step_metadata(self, scheduler, redis, db):
        """resubmit 不得抹掉已存在步的时间戳/指纹:对已有行用 update_step 只改 status,
        不能 upsert_step 整行替换,否则已完成步的 started_at/duration/input_hash 被清空,
        流水线显示该步无时间。"""
        job = make_job()
        db.create_job(job)
        await scheduler.submit_job(job)
        # 把 A 标成 done 且带时间戳/时长/指纹(模拟真实跑完的步)
        db.update_step("j_test_001", "A", status=StepStatus.DONE,
                       started_at="2026-01-01T00:00:00+00:00",
                       finished_at="2026-01-01T00:00:05+00:00",
                       duration_sec=5.0, input_hash="deadbeef")
        await redis.set_step_status("j_test_001", "A", "done")
        scheduler.reload_config = lambda: None

        await scheduler.resubmit("j_test_001")

        a = next(s for s in db.get_steps("j_test_001") if s.name == "A")
        assert a.status == StepStatus.DONE
        assert a.started_at is not None                 # 时间戳保留(不被 upsert 抹掉)
        assert a.finished_at is not None
        assert a.duration_sec == 5.0
        assert a.input_hash == "deadbeef"               # 指纹保留


class TestRetryFailed:
    @pytest.mark.asyncio
    async def test_retries_from_first_failed(self, scheduler, redis, db, tmp_jobs_dir):
        job = make_job()
        db.create_job(job)
        await scheduler.submit_job(job)

        for step in ["A", "B", "C"]:
            await redis.set_step_status("j_test_001", step, "running")
            await scheduler.on_step_done("j_test_001", step)

        job_dir = tmp_jobs_dir / "j_test_001"
        job_dir.mkdir(exist_ok=True)

        await redis.set_step_status("j_test_001", "B", "failed")
        await redis.set_step_status("j_test_001", "C", "failed")

        await scheduler._retry_failed("j_test_001")

        assert await redis.get_step_status("j_test_001", "A") == "done"
        assert await redis.get_step_status("j_test_001", "B") == "ready"
        assert await redis.get_step_status("j_test_001", "C") == "waiting"


class TestDispatch:
    @pytest.mark.asyncio
    async def test_dispatch_new_job(self, scheduler, redis, db):
        job = make_job()
        db.create_job(job)

        await scheduler._dispatch({"command": "new_job", "job_id": "j_test_001"})

        statuses = await redis.get_all_step_statuses("j_test_001")
        assert "A" in statuses

    @pytest.mark.asyncio
    async def test_dispatch_rerun(self, scheduler, redis, db, tmp_jobs_dir):
        job = make_job()
        db.create_job(job)
        await scheduler.submit_job(job)

        for step in ["A", "B", "C"]:
            await redis.set_step_status("j_test_001", step, "running")
            await scheduler.on_step_done("j_test_001", step)

        job_dir = tmp_jobs_dir / "j_test_001"
        job_dir.mkdir(exist_ok=True)

        await scheduler._dispatch({
            "command": "rerun", "job_id": "j_test_001", "from_step": "C",
        })

        assert await redis.get_step_status("j_test_001", "C") == "ready"

    @pytest.mark.asyncio
    async def test_rerun_deletes_central_done(self, scheduler, redis, db, tmp_jobs_dir):
        # MinIO 部署下 .done 在中心存储:rerun 只删本地是 no-op(worker pull 回旧 .done 指纹命中跳过),
        # 必须经 storage.delete_file 同步删;删失败只告警不挡主流程。
        from unittest.mock import AsyncMock

        job = make_job(job_id="j_rr_central")
        db.create_job(job)
        await scheduler.submit_job(job)
        for step in ["A", "B", "C"]:
            await redis.set_step_status("j_rr_central", step, "running")
            await scheduler.on_step_done("j_rr_central", step)

        fake_storage = AsyncMock()
        scheduler.storage = fake_storage
        reset = await scheduler.rerun("j_rr_central", "B")
        assert set(reset) == {"B", "C"}
        deleted = {c.args for c in fake_storage.delete_file.await_args_list}
        assert deleted == {("j_rr_central", ".B.done"), ("j_rr_central", ".C.done")}

        # 删失败(网络抖动)→ 告警继续,rerun 仍完成重置
        fake_storage.delete_file.side_effect = RuntimeError("minio down")
        reset = await scheduler.rerun("j_rr_central", "C")
        assert reset == ["C"]

    @pytest.mark.asyncio
    async def test_dispatch_retry(self, scheduler, redis, db, tmp_jobs_dir):
        job = make_job()
        db.create_job(job)
        await scheduler.submit_job(job)

        for step in ["A", "B", "C"]:
            await redis.set_step_status("j_test_001", step, "running")
            await scheduler.on_step_done("j_test_001", step)

        job_dir = tmp_jobs_dir / "j_test_001"
        job_dir.mkdir(exist_ok=True)
        await redis.set_step_status("j_test_001", "C", "failed")

        await scheduler._dispatch({"command": "retry", "job_id": "j_test_001"})

        assert await redis.get_step_status("j_test_001", "C") == "ready"

    @pytest.mark.asyncio
    async def test_dispatch_step_done(self, scheduler, redis, db):
        job = make_job()
        db.create_job(job)
        await scheduler.submit_job(job)

        await redis.set_step_status("j_test_001", "A", "running")
        await scheduler._dispatch({
            "status": "done", "job_id": "j_test_001", "step": "A",
        })

        assert await redis.get_step_status("j_test_001", "A") == "done"

    @pytest.mark.asyncio
    async def test_dispatch_step_failed(self, scheduler, redis, db):
        job = make_job()
        db.create_job(job)
        await scheduler.submit_job(job)

        await redis.set_step_status("j_test_001", "A", "running")
        await scheduler._dispatch({
            "status": "failed", "job_id": "j_test_001", "step": "A",
            "error": "boom", "error_type": "input_missing",
        })

        assert await redis.get_step_status("j_test_001", "A") == "failed"

    @pytest.mark.asyncio
    async def test_dispatch_action_field(self, scheduler, redis, db):
        """_dispatch should accept 'action' as alias for 'command'."""
        job = make_job()
        db.create_job(job)
        await scheduler._dispatch({"action": "new_job", "job_id": "j_test_001"})
        statuses = await redis.get_all_step_statuses("j_test_001")
        assert "A" in statuses


class TestCalcProgress:
    def test_equal_weight(self, scheduler):
        steps = [{"name": "A"}, {"name": "B"}, {"name": "C"}]
        assert scheduler._calc_progress(steps, {"A": "done", "B": "done", "C": "waiting"}) == 67

    def test_custom_weight(self, scheduler):
        steps = [
            {"name": "A", "weight": 1},
            {"name": "B", "weight": 3},
            {"name": "C", "weight": 1},
        ]
        # A done (1) + C skipped (1) = 2 of 5 total
        assert scheduler._calc_progress(
            steps, {"A": "done", "B": "waiting", "C": "skipped"}
        ) == 40

    def test_all_done(self, scheduler):
        steps = [{"name": "A", "weight": 2}, {"name": "B", "weight": 3}]
        assert scheduler._calc_progress(steps, {"A": "done", "B": "done"}) == 100

    def test_none_done(self, scheduler):
        steps = [{"name": "A"}, {"name": "B"}]
        assert scheduler._calc_progress(steps, {"A": "waiting", "B": "waiting"}) == 0


class TestEnqueueTags:
    @pytest.mark.asyncio
    async def test_ai_provider_tags_are_hard_requirements(
        self, redis, db, tmp_path, tmp_jobs_dir, configs_dir,
    ):
        pipelines = {"tagged": {"steps": [{
            "name": "A", "pool": "ai", "depends_on": [], "tags": ["vision"],
            "ai": {"primary": {"provider": "claude-cli"},
                   "fallback": {"provider": "openai"}},
        }]}}
        config = make_config(tmp_path, tmp_jobs_dir, pipelines, configs_dir)
        sched = Scheduler(redis, db, config)
        job = Job(id="j_provider_tags", content_type="video", pipeline="tagged")
        db.create_job(job)
        await sched.submit_job(job)
        item, _ = await redis.dequeue_step("ai")
        assert item["require_tags"] == ["claude-cli", "openai-api", "vision"]

    @pytest.mark.asyncio
    async def test_job_override_replaces_pipeline_provider_tiers(
        self, redis, db, tmp_path, tmp_jobs_dir, configs_dir,
    ):
        class Storage:
            async def read_file(self, job_id, rel):
                assert rel == "job.json"
                return b'{"ai_overrides":{"A":"deepseek"}}'

        pipelines = {"tagged": {"steps": [{
            "name": "A", "pool": "ai", "depends_on": [], "tags": [],
            "ai": {"primary": {"provider": "claude-cli"},
                   "fallback": {"provider": "openai"}},
        }]}}
        config = make_config(tmp_path, tmp_jobs_dir, pipelines, configs_dir)
        config.providers = {"providers": {"deepseek": {"type": "openai"}}}
        sched = Scheduler(redis, db, config, storage=Storage())
        req = await sched._required_tags_for_step("j_override", "A", pipelines["tagged"]["steps"][0])
        assert req == ["deepseek-api"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("provider", "original", "expected", "fails"), [
        ("claude-cli", None, ["claude-cli", "read"], False),
        ("openai", b"# extracted text", ["openai-api"], False),
        ("openai", None, None, True),
        ("openai", b"", None, True),
    ])
    async def test_paper_read_capability_uses_current_artifact_and_provider(
        self, redis, db, tmp_path, tmp_jobs_dir, configs_dir,
        provider, original, expected, fails,
    ):
        class Storage:
            async def read_file(self, job_id, rel):
                assert rel == "job.json"
                return json.dumps({"ai_overrides": {"A": provider}}).encode()

            async def file_size(self, job_id, rel):
                if rel != "output/original.md" or original is None:
                    return None
                return len(original)

            async def open_stream(self, job_id, rel, **kwargs):
                if rel != "output/original.md" or original is None:
                    return None

                async def chunks():
                    yield original

                return chunks()

        pipelines = {"tagged": {"steps": [{
            "name": "A", "pool": "ai", "depends_on": [], "tags": [],
            "capability_rules": {
                "read": {"unless_any_nonempty": ["output/original.md"]},
            },
            "ai": {"primary": {"provider": "claude-cli"}},
        }]}}
        config = make_config(tmp_path, tmp_jobs_dir, pipelines, configs_dir)
        config.providers = {"providers": {
            "claude-cli": {"type": "cli", "features": ["read"]},
            "openai": {"type": "openai", "features": []},
        }}
        sched = Scheduler(redis, db, config, storage=Storage())

        if fails:
            from shared.errors import InputInvalidError
            with pytest.raises(InputInvalidError, match="does not support read"):
                await sched._required_tags_for_step(
                    "j_read", "A", pipelines["tagged"]["steps"][0],
                )
        else:
            assert await sched._required_tags_for_step(
                "j_read", "A", pipelines["tagged"]["steps"][0],
            ) == expected

    @pytest.mark.asyncio
    @pytest.mark.parametrize("document", [
        None, [], False, 0, "job",
        {"ai_overrides": None}, {"ai_overrides": []},
        {"ai_overrides": False}, {"ai_overrides": 0},
        {"ai_overrides": "openai"},
        {"ai_overrides": {"A": None}}, {"ai_overrides": {"A": []}},
        {"ai_overrides": {"A": False}}, {"ai_overrides": {"A": 0}},
        {"ai_overrides": {"A": {}}}, {"ai_overrides": {"A": "  "}},
    ])
    async def test_invalid_job_override_shapes_fail_closed_without_pipeline_fallback(
        self, redis, db, tmp_path, tmp_jobs_dir, configs_dir, document,
    ):
        class Storage:
            async def read_file(self, job_id, rel):
                assert rel == "job.json"
                return json.dumps(document).encode()

        pipelines = {"tagged": {"steps": [{
            "name": "A", "pool": "ai", "depends_on": [], "tags": [],
            "ai": {"primary": {"provider": "claude-cli"},
                   "fallback": {"provider": "openai"}},
        }]}}
        config = make_config(tmp_path, tmp_jobs_dir, pipelines, configs_dir)
        sched = Scheduler(redis, db, config, storage=Storage())

        from shared.errors import InputInvalidError
        with pytest.raises(InputInvalidError, match="invalid AI override"):
            await sched._required_tags_for_step(
                "j_invalid_override", "A", pipelines["tagged"]["steps"][0],
            )

    @pytest.mark.asyncio
    async def test_unknown_job_override_fails_closed_in_enqueue_and_no_worker_paths(
        self, redis, db, tmp_path, tmp_jobs_dir, configs_dir,
    ):
        class Storage:
            async def read_file(self, job_id, rel):
                assert rel == "job.json"
                return b'{"ai_overrides":{"A":"typo-provider"}}'

        pipelines = {"tagged": {"steps": [{
            "name": "A", "pool": "ai", "depends_on": [], "tags": [],
            "ai": {"primary": {"provider": "claude-cli"}},
        }]}}
        config = make_config(tmp_path, tmp_jobs_dir, pipelines, configs_dir)
        config.providers = {"providers": {"claude-cli": {"type": "cli"}}}
        sched = Scheduler(redis, db, config, storage=Storage())
        job = Job(id="j_unknown_override", content_type="video", pipeline="tagged")
        db.create_job(job)

        await sched.submit_job(job)

        assert await redis.get_step_status(job.id, "A") == "failed"
        assert db.get_job(job.id).status == JobStatus.FAILED
        assert await redis.dequeue_step("ai") is None

        await redis.add_active_job(job.id)
        await redis.set_step_status(job.id, "A", "ready")
        await sched.check_no_worker()
        assert db.get_job(job.id).status == JobStatus.FAILED

    @pytest.mark.asyncio
    async def test_ai_pool_merges_domain_tags(self, scheduler, redis, db, tmp_path, tmp_jobs_dir, configs_dir):
        """AI pool steps merge static_tags + domain + style_tags into tags, require_tags = static only."""
        pipelines = {
            "tagged": {
                "steps": [
                    {"name": "A", "pool": "ai", "depends_on": [], "tags": ["vision"]},
                ]
            }
        }
        config = make_config(tmp_path, tmp_jobs_dir, pipelines, configs_dir)
        sched = Scheduler(redis, db, config)

        job = Job(id="j_tag", content_type="video", pipeline="tagged",
                  domain="deep-learning", style_tags=["lecture", "case-study"])
        db.create_job(job)
        await sched.submit_job(job)

        item, _ = await redis.dequeue_step("ai")
        tags = set(item["tags"])
        assert "vision" in tags        # static
        assert "deep-learning" in tags       # domain
        assert "lecture" in tags       # style_tag
        assert "case-study" in tags    # style_tag
        assert item["require_tags"] == ["vision"]  # only static

    @pytest.mark.asyncio
    async def test_non_ai_pool_no_domain_tags(self, scheduler, redis, db, tmp_path, tmp_jobs_dir, configs_dir):
        """Non-AI pool steps should NOT have domain/style tags — only static tags."""
        pipelines = {
            "tagged": {
                "steps": [
                    {"name": "A", "pool": "cpu", "depends_on": [], "tags": ["gpu"]},
                ]
            }
        }
        config = make_config(tmp_path, tmp_jobs_dir, pipelines, configs_dir)
        sched = Scheduler(redis, db, config)

        job = Job(id="j_notag", content_type="video", pipeline="tagged",
                  domain="deep-learning", style_tags=["lecture"])
        db.create_job(job)
        await sched.submit_job(job)

        item, _ = await redis.dequeue_step("cpu")
        assert item["tags"] == ["gpu"]        # static only
        assert "deep-learning" not in item["tags"]  # no domain
        assert item["require_tags"] == ["gpu"]

    @pytest.mark.asyncio
    async def test_tags_merge_invalid_style_tags_json(self, scheduler, redis, db, tmp_path, tmp_jobs_dir, configs_dir):
        """Invalid style_tags JSON in Redis should degrade gracefully."""
        pipelines = {
            "tagged": {
                "steps": [
                    {"name": "A", "pool": "ai", "depends_on": [], "tags": ["vision"]},
                ]
            }
        }
        config = make_config(tmp_path, tmp_jobs_dir, pipelines, configs_dir)
        sched = Scheduler(redis, db, config)

        job = Job(id="j_badtag", content_type="video", pipeline="tagged", domain="cs")
        db.create_job(job)
        await redis.init_job("j_badtag", "tagged", {"domain": "cs", "style_tags": "not-json"})
        for name in ["A"]:
            await redis.set_step_status("j_badtag", name, "waiting")
        await redis.add_active_job("j_badtag")

        await sched.enqueue_step("j_badtag", "A")

        item, _ = await redis.dequeue_step("ai")
        tags = set(item["tags"])
        assert "vision" in tags
        assert "cs" in tags
        assert item["require_tags"] == ["vision"]

    @pytest.mark.asyncio
    async def test_bili_download_requires_net_cn(self, scheduler, redis, db, tmp_path, tmp_jobs_dir, configs_dir):
        """B站源的 01_download → require_tags 含 net-cn(B站属大陆区域);无 bili 路由 tag。"""
        pipelines = {"v": {"steps": [{"name": "01_download", "pool": "io", "depends_on": [], "tags": []}]}}
        config = make_config(tmp_path, tmp_jobs_dir, pipelines, configs_dir)
        sched = Scheduler(redis, db, config)
        job = Job(id="j_bdl", content_type="video", pipeline="v")
        db.create_job(job)
        await redis.init_job("j_bdl", "v", {"source": "bilibili", "url": "https://b23.tv/x"})
        await redis.set_step_status("j_bdl", "01_download", "waiting")
        await redis.add_active_job("j_bdl")
        await sched.enqueue_step("j_bdl", "01_download")
        item, _ = await redis.dequeue_step("io")
        assert "net-cn" in item["require_tags"]   # B站属大陆区域(平台源权威)
        assert "bili" not in item["require_tags"]   # 无 bili 路由 tag:SESSDATA 是 worker 本地的事

    @pytest.mark.asyncio
    async def test_arxiv_download_no_bili(self, scheduler, redis, db, tmp_path, tmp_jobs_dir, configs_dir):
        """境外源 arxiv 的 01_download 不加 bili,且 require net-global(arxiv.org 非 CN 域名)。"""
        pipelines = {"p": {"steps": [{"name": "01_download", "pool": "io", "depends_on": [], "tags": []}]}}
        config = make_config(tmp_path, tmp_jobs_dir, pipelines, configs_dir)
        sched = Scheduler(redis, db, config)
        job = Job(id="j_adl", content_type="paper", pipeline="p")
        db.create_job(job)
        await redis.init_job("j_adl", "p", {"source": "arxiv", "url": "https://arxiv.org/abs/1"})
        await redis.set_step_status("j_adl", "01_download", "waiting")
        await redis.add_active_job("j_adl")
        await sched.enqueue_step("j_adl", "01_download")
        item, _ = await redis.dequeue_step("io")
        assert "bili" not in item["require_tags"]
        assert "net-global" in item["require_tags"]   # arxiv.org 非 CN → 全球区域


class TestCleanupStaleWorkers:
    @pytest.mark.asyncio
    async def test_dead_worker_deleted_alive_marked_offline(self, scheduler, redis, db):
        """DB 心跳超时 + Redis 注册已过期 -> 删除;Redis 仍在 -> 标 offline。"""
        from datetime import datetime, timedelta, timezone
        from shared.models import Worker as WorkerModel

        # 用 aware UTC,与 cleanup_stale_workers 的 datetime.now(timezone.utc) 同基准;
        # naive datetime.now() 在非 UTC 宿主上会把心跳算成未来,致 stale 永假。
        old = datetime.now(timezone.utc) - timedelta(minutes=10)
        # dead: DB 过期,Redis 无注册
        db.upsert_worker(
            WorkerModel(id="cpu-dead", type="cpu", status="busy",
                        first_seen=old, last_heartbeat=old)
        )
        # alive-but-stale: DB 过期,但 Redis 仍有注册键
        db.upsert_worker(
            WorkerModel(id="cpu-alive", type="cpu", status="idle",
                        first_seen=old, last_heartbeat=old)
        )
        await redis.register_worker("cpu-alive", {"type": "cpu", "pools": "cpu"}, ttl=30)

        await scheduler.cleanup_stale_workers(timeout_sec=60)

        assert db.get_worker("cpu-dead") is None  # 真死了 -> 删除
        alive = db.get_worker("cpu-alive")
        assert alive is not None
        assert alive.status == "offline"

    @pytest.mark.asyncio
    async def test_paused_stale_worker_preserved(self, scheduler, redis, db):
        """暂停是管理员意图,worker 停机或重建后不能被 stale GC 删掉。"""
        from datetime import datetime, timedelta, timezone
        from shared.models import Worker as WorkerModel

        old = datetime.now(timezone.utc) - timedelta(minutes=10)
        db.upsert_worker(
            WorkerModel(id="cpu-paused", type="cpu", status="busy",
                        admin_status="paused", current_job="j1", current_step="A",
                        first_seen=old, last_heartbeat=old)
        )

        await scheduler.cleanup_stale_workers(timeout_sec=60)

        got = db.get_worker("cpu-paused")
        assert got is not None
        assert got.admin_status == "paused"
        assert got.status == "offline"

    @pytest.mark.asyncio
    async def test_fresh_worker_untouched(self, scheduler, redis, db):
        from datetime import datetime, timezone
        from shared.models import Worker as WorkerModel

        now = datetime.now(timezone.utc)
        db.upsert_worker(
            WorkerModel(id="cpu-fresh", type="cpu", status="idle",
                        first_seen=now, last_heartbeat=now)
        )
        await scheduler.cleanup_stale_workers(timeout_sec=60)
        got = db.get_worker("cpu-fresh")
        assert got is not None
        # 刚心跳的 worker 不应被回收;公共状态衍生为 online-idle
        assert got.status == "online-idle"

    @pytest.mark.asyncio
    async def test_aware_now_minus_legacy_naive_heartbeat_no_crash(
        self, scheduler, redis, db
    ):
        """兼容旧库回归:cleanup 用 aware now 减去旧库 naive 心跳不能崩
        ('can't subtract offset-naive and offset-aware'),且仍正确判旧 worker stale。"""
        # 直接写一个 naive 心跳的旧行(绕过模型默认值,模拟历史数据)
        db._conn.execute(
            "INSERT INTO workers (id, type, status, first_seen, last_heartbeat) "
            "VALUES (?,?,?,?,?)",
            ("cpu-legacy", "cpu", "busy", "2026-01-01T00:00:00", "2026-01-01T00:00:00"),
        )
        db._conn.commit()
        # Redis 无注册键视为真死,删除
        await scheduler.cleanup_stale_workers(timeout_sec=60)
        assert db.get_worker("cpu-legacy") is None


class TestStaleExecGuard:
    """孤儿重排后旧执行的迟到完成/失败事件按 exec_id 丢弃,不顶替当前在跑实例。"""

    @pytest.mark.asyncio
    async def test_stale_exec_done_ignored(self, redis, db, config):
        s = _stub_workers_present(Scheduler(redis, db, config))
        job = make_job()
        db.create_job(job)
        await s.submit_job(job)
        await redis.set_step_status("j_test_001", "A", "running")
        await redis.set_step_exec_id("j_test_001", "A", "exec_2")
        # 旧执行 exec_1 迟到上报完成 → 应被忽略
        await s.on_step_done("j_test_001", "A", exec_id="exec_1")
        assert await redis.get_step_status("j_test_001", "A") == "running"
        # 当前执行 exec_2 上报 → 正常置 done
        await s.on_step_done("j_test_001", "A", exec_id="exec_2")
        assert await redis.get_step_status("j_test_001", "A") == "done"

    @pytest.mark.asyncio
    async def test_no_exec_record_backward_compatible(self, redis, db, config):
        # 未写 exec_id(旧库) → 不过滤,按普通 CAS 行为置 done
        s = _stub_workers_present(Scheduler(redis, db, config))
        job = make_job()
        db.create_job(job)
        await s.submit_job(job)
        await redis.set_step_status("j_test_001", "A", "running")
        await s.on_step_done("j_test_001", "A", exec_id="exec_x")
        assert await redis.get_step_status("j_test_001", "A") == "done"


class TestOrphanClaimMismatch:
    """在跑步骤的 worker 存活但其上报 current_step 不是本步(认领响应丢失),
    超宽限期回收,避免永久卡 running。"""

    @pytest.mark.asyncio
    async def test_reclaim_when_worker_not_running_this_step(self, redis, db, config):
        s = Scheduler(redis, db, config)
        s._CLAIM_MISMATCH_GRACE_SEC = 0
        job = make_job()
        db.create_job(job)
        await s.submit_job(job)
        await redis.set_step_status("j_test_001", "A", "running")
        await redis.set_step_worker("j_test_001", "A", "w1")
        await redis.register_worker(
            "w1", {"type": "cpu", "pools": "cpu,io", "status": "idle",
                   "current_job": "", "current_step": ""},
        )
        calls = []
        async def fake_reclaim(job_id, step, reason, **kwargs):
            calls.append((job_id, step))
        s._reclaim_step = fake_reclaim
        await s.orphan_scan()
        assert ("j_test_001", "A") in calls

    @pytest.mark.asyncio
    async def test_no_reclaim_when_step_has_fresh_progress(self, redis, db, config):
        # 即便 worker 上报的 current_step 不是本步(并发下只反映 N 步中的 1 步),
        # 只要本步有新鲜进度心跳 → 不回收。
        s = Scheduler(redis, db, config)
        s._CLAIM_MISMATCH_GRACE_SEC = 0
        job = make_job()
        db.create_job(job)
        await s.submit_job(job)
        await redis.set_step_status("j_test_001", "A", "running")
        await redis.set_step_worker("j_test_001", "A", "w1")
        await redis.register_worker(
            "w1", {"type": "cpu", "pools": "cpu,io", "status": "busy",
                   "current_job": "j_test_001", "current_step": "B"},  # 上报的是别的步
        )
        await redis.set_step_progress_at("j_test_001", "A")  # 本步心跳新鲜
        calls = []
        async def fake_reclaim(job_id, step, reason, **kwargs):
            calls.append((job_id, step))
        s._reclaim_step = fake_reclaim
        await s.orphan_scan()
        assert calls == []  # 本步心跳新鲜 → 不回收(尽管 current_step 不匹配)

    @pytest.mark.asyncio
    async def test_concurrent_steps_not_reclaimed(self, redis, db, config):
        # 回归:worker 并发跑多步,current_step 只能反映其一;两步都有新鲜心跳 → 都不回收。
        # 若只按单个 current_step 判活,会把其余并发步全误回收,失败雪崩。
        s = Scheduler(redis, db, config)
        s._CLAIM_MISMATCH_GRACE_SEC = 0
        job = make_job()
        db.create_job(job)
        await s.submit_job(job)
        for st in ("A", "B"):
            await redis.set_step_status("j_test_001", st, "running")
            await redis.set_step_worker("j_test_001", st, "w1")
            await redis.set_step_progress_at("j_test_001", st)
        await redis.register_worker(
            "w1", {"type": "cpu", "pools": "cpu,io", "status": "busy",
                   "current_job": "j_test_001", "current_step": "A"},  # 只报 A
        )
        calls = []
        async def fake_reclaim(job_id, step, reason, **kwargs):
            calls.append((job_id, step))
        s._reclaim_step = fake_reclaim
        await s.orphan_scan()
        assert calls == []  # A、B 都有新鲜心跳 → 都不回收(并发安全)

    @pytest.mark.asyncio
    async def test_within_grace_not_reclaimed(self, redis, db, config):
        s = Scheduler(redis, db, config)  # 默认宽限 30s
        job = make_job()
        db.create_job(job)
        await s.submit_job(job)
        await redis.set_step_status("j_test_001", "A", "running")
        await redis.set_step_worker("j_test_001", "A", "w1")
        await redis.register_worker(
            "w1", {"type": "cpu", "pools": "cpu,io", "status": "idle", "current_step": ""},
        )
        calls = []
        async def fake_reclaim(job_id, step, reason, **kwargs):
            calls.append((job_id, step))
        s._reclaim_step = fake_reclaim
        await s.orphan_scan()  # 首次只记时,不回收
        assert calls == []


class TestSchedulerHeartbeat:
    @pytest.mark.asyncio
    async def test_heartbeat_loop_writes_component(self, scheduler, redis):
        # 起一拍心跳即取消:验 component:scheduler 写入 version/started_at/loop_*/pid。
        scheduler._last_loop_lag = 1.5
        task = asyncio.create_task(scheduler._heartbeat_loop())
        # 轮询等首拍写入(避免赌固定 sleep)。
        for _ in range(40):
            hb = await redis.get_component_heartbeat("scheduler")
            if hb:
                break
            await asyncio.sleep(0.02)
        scheduler._shutdown = True
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert hb is not None
        assert hb["loop_lag_sec"] == "1.5"
        assert hb["loop_interval_sec"] == "30"
        assert "started_at" in hb and "pid" in hb and "ts" in hb

    @pytest.mark.asyncio
    async def test_periodic_loop_measures_loop_lag(self, scheduler, monkeypatch):
        # 用受控 monotonic 模拟两拍间隔 35s(期望 30s)→ loop_lag=5s;sleep no-op,首拍后 shutdown。
        import scheduler.scheduler as sched_mod
        ticks = iter([100.0, 135.0])

        def fake_mono():
            try:
                return next(ticks)
            except StopIteration:
                return 135.0
        monkeypatch.setattr(sched_mod.time, "monotonic", fake_mono)

        calls = {"n": 0}

        async def fake_sleep(_):
            calls["n"] += 1
            if calls["n"] >= 2:
                scheduler._shutdown = True
        monkeypatch.setattr(sched_mod.asyncio, "sleep", fake_sleep)

        # orphan_scan 等设成 no-op,只测 loop_lag 计算。
        async def noop():
            return None
        scheduler.orphan_scan = noop
        scheduler.check_stuck = noop
        scheduler.check_no_worker = noop
        scheduler.cleanup_stale_workers = noop

        await scheduler._periodic_loop()
        # 第一拍 _last_tick=100;第二拍 now=135,lag = (135-100)-30 = 5。
        assert scheduler._last_loop_lag == 5.0


class TestMarkJobFailedClearsSiblings:
    """失败即停:某步终态失败 → mark_job_failed 清掉该 job 残留在队列的并行兄弟 task。
    这些是死任务,job 已 FAILED 不该再跑;不清则 worker 仍会认领,cas_step_status 因 steps hash
    未清而成功,会跑已失败 job 的步甚至把它重标 done,还留下指向 FAILED job 的孤儿 task。
    保留 job:{id}:steps hash 供重试/重跑。"""

    @pytest.mark.asyncio
    async def test_failed_clears_sibling_queue_keeps_steps(
        self, redis, db, tmp_path, tmp_jobs_dir, configs_dir, parallel_pipelines
    ):
        cfg = make_config(tmp_path, tmp_jobs_dir, parallel_pipelines, configs_dir)
        s = _stub_workers_present(Scheduler(redis, db, cfg))
        job = make_job(pipeline="par", job_id="j_par_fail")
        db.create_job(job)
        await s.submit_job(job)                         # A 入队 cpu
        await redis.dequeue_step_raw("cpu")             # 模拟 worker 认领 A 出队
        await redis.set_step_status("j_par_fail", "A", "done")
        db.update_step("j_par_fail", "A", status="done")
        await s._check_downstream("j_par_fail")         # A 完成 → B(cpu)+C(io) 并行入队
        assert (await redis.get_queue_info("cpu"))["length"] == 1   # B
        assert (await redis.get_queue_info("io"))["length"] == 1    # C

        await s.mark_job_failed("j_par_fail", "B 终态失败")

        # 兄弟 task B/C 被清出队列(失败即停)
        assert (await redis.get_queue_info("cpu"))["length"] == 0
        assert (await redis.get_queue_info("io"))["length"] == 0
        # steps hash 保留(供重试):四步状态都还在
        statuses = await redis.get_all_step_statuses("j_par_fail")
        assert set(statuses.keys()) == {"A", "B", "C", "D"}
        # job 终态 FAILED + 移出 active
        assert db.get_job("j_par_fail").status == JobStatus.FAILED
        assert "j_par_fail" not in await redis.get_active_jobs()


class TestTimeoutRetry:
    """超时重试:RETRY_POLICY['timeout']={'max':1} 封顶(非无限循环——线上 GPT-3 翻译步曾因
    DB retries 列不同步被误读);重试时 DB retries 列同步,第二次超时终态失败。"""

    async def _drain_delayed(self, scheduler):
        await asyncio.sleep(0)
        if scheduler._delayed_tasks:
            await asyncio.gather(*list(scheduler._delayed_tasks))

    @pytest.mark.asyncio
    async def test_timeout_retries_once_then_fails(self, scheduler, redis, db):
        job = make_job()
        db.create_job(job)
        await scheduler.submit_job(job)
        await redis.dequeue_step("cpu")

        delays = []

        async def mock_delayed(delay, job_id, step):
            delays.append(delay)
            await scheduler.enqueue_step(job_id, step)

        scheduler._delayed_enqueue = mock_delayed

        # 第一次超时:policy max=1、pipeline A retries=2 → min=1,重试(延迟 10s)。
        await redis.set_step_status("j_test_001", "A", "running")
        await scheduler.on_step_failed("j_test_001", "A", "timeout", "timeout")
        await self._drain_delayed(scheduler)
        assert delays == [10]
        assert await redis.get_step_status("j_test_001", "A") == "ready"
        assert await redis.get_step_retries("j_test_001", "A") == 1
        # DB retries 列必须同步,否则 UI/排查会误判为超时未计数。
        step_row = next(s for s in db.get_steps("j_test_001") if s.name == "A")
        assert step_row.retries == 1

        # 第二次超时:current 1 >= 1 → 终态失败,job 失败(不无限循环)。
        await redis.set_step_status("j_test_001", "A", "running")
        await scheduler.on_step_failed("j_test_001", "A", "timeout", "timeout")
        await self._drain_delayed(scheduler)
        assert await redis.get_step_status("j_test_001", "A") == "failed"
        assert delays == [10]
        assert db.get_job("j_test_001").status == JobStatus.FAILED


class TestTermMapExportAndCollect:
    """submit 导出 term_map 快照,翻译步完成后回流 glossary/集合表。"""



    @pytest.mark.asyncio
    async def test_submit_exports_domain_term_map(self, redis, db, config):
        from unittest.mock import AsyncMock
        storage = AsyncMock()
        storage.read_file.return_value = None            # 无集合表(非 book)
        s = _stub_workers_present(Scheduler(redis, db, config, storage=storage))
        db.add_glossary_suggestion("general", "martingale", "j0",
                                   definition="鞅,一种随机过程")   # 定义首短名可提炼
        db.add_glossary_suggestion("general", "no name term", "j0",
                                   definition="一段无法提炼短名的长解释而已")
        job = make_job()
        db.create_job(job)
        await s.submit_job(job)
        writes = {c.args[1]: c.args[2] for c in storage.write_file.await_args_list}
        assert "input/term_map.json" in writes
        tmap = json.loads(writes["input/term_map.json"])
        assert tmap == {"martingale": "鞅"}               # 提不出的宁缺勿滥

    @pytest.mark.asyncio
    async def test_collection_terms_merged_l2_over_l1(self, redis, db, config):
        from unittest.mock import AsyncMock
        storage = AsyncMock()
        storage.read_file.return_value = json.dumps({"martingale": "马丁格尔"}).encode()  # L2 覆盖 L1
        s = _stub_workers_present(Scheduler(redis, db, config, storage=storage))
        db.add_glossary_suggestion("general", "martingale", "j0", definition="鞅,随机过程")
        job = make_job()
        job.collection_id = "col_book_x"
        db.create_job(job)
        await s.submit_job(job)
        writes = {c.args[1]: c.args[2] for c in storage.write_file.await_args_list}
        tmap = json.loads(writes["input/term_map.json"])
        assert tmap["martingale"] == "马丁格尔"
        storage.read_file.assert_any_await("collections/col_book_x", "terms.json")

    @pytest.mark.asyncio
    async def test_translate_done_collects_pairs_into_glossary(self, redis, db, config):
        from unittest.mock import AsyncMock
        storage = AsyncMock()
        pairs = {"Kelly criterion": "凯利准则"}
        async def read_file(job_id, rel):
            if rel == "output/term_pairs.json":
                return json.dumps(pairs, ensure_ascii=False).encode()
            return None
        storage.read_file.side_effect = read_file
        s = _stub_workers_present(Scheduler(redis, db, config, storage=storage))
        job = make_job()
        db.create_job(job)
        await s._collect_term_pairs("j_test_001")
        row = db.get_glossary_term("general", "Kelly criterion")
        assert row and row["zh_name"] == "凯利准则" and row["status"] == "suggested"

    @pytest.mark.asyncio
    async def test_collect_pairs_merges_collection_terms_first_wins(self, redis, db, config):
        from unittest.mock import AsyncMock
        storage = AsyncMock()
        state = {"collection": json.dumps({"alpha": "阿尔法"}).encode()}
        async def read_file(prefix, rel):
            if rel == "output/term_pairs.json":
                return json.dumps({"alpha": "另一译", "beta": "贝塔"}, ensure_ascii=False).encode()
            if prefix.startswith("collections/") and rel == "terms.json":
                return state["collection"]
            return None
        async def write_file(prefix, rel, data):
            if prefix.startswith("collections/"):
                state["collection"] = data
        storage.read_file.side_effect = read_file
        storage.write_file.side_effect = write_file
        s = _stub_workers_present(Scheduler(redis, db, config, storage=storage))
        job = make_job()
        job.collection_id = "col_book_x"
        db.create_job(job)
        await s._collect_term_pairs("j_test_001")
        merged = json.loads(state["collection"])
        assert merged == {"alpha": "阿尔法", "beta": "贝塔"}   # 先到先得:已有不覆盖,新词并入


class TestBookChainAdvance:
    """章 job 终态后 scheduler 自动 submit 下一待投章,失败也放行。"""

    @pytest.mark.asyncio
    async def test_done_advances_next_chapter(self, redis, db, config):
        from unittest.mock import AsyncMock
        from datetime import datetime, timezone
        from shared.models import Collection
        db.create_collection(Collection(id="col_book_b", name="书", domain="general",
                                        source_type="book_toc", source_id="https://b.example/"))
        for i, jid in enumerate(["j_ch1", "j_ch2"]):
            db.create_job(Job(id=jid, content_type="article", pipeline="test",
                              domain="general", collection_id="col_book_b",
                              created_at=datetime(2026, 7, 6, i, tzinfo=timezone.utc)))
        storage = AsyncMock(); storage.read_file.return_value = None
        s = _stub_workers_present(Scheduler(redis, db, config, storage=storage))
        # ch1 跑完到终态:
        await redis.init_job("j_ch1", "test", {})
        await redis.set_step_status("j_ch1", "A", "done")
        db.update_job("j_ch1", status="done")
        await s._advance_book_chain("j_ch1")
        # ch2 被 submit(steps hash 初始化)
        assert await redis.get_all_step_statuses("j_ch2") != {}
        assert db.get_job("j_ch2") is not None

    @pytest.mark.asyncio
    async def test_non_book_collection_untouched(self, redis, db, config):
        from unittest.mock import AsyncMock
        from shared.models import Collection
        db.create_collection(Collection(id="col_up_x", name="up", domain="general",
                                        source_type="bilibili_up", source_id="1"))
        job = make_job(); job.collection_id = "col_up_x"
        db.create_job(job)
        storage = AsyncMock(); storage.read_file.return_value = None
        s = _stub_workers_present(Scheduler(redis, db, config, storage=storage))
        await s._advance_book_chain("j_test_001")   # 不炸、无副作用即可
