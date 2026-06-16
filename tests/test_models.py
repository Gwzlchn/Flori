"""tests for shared/models.py"""

import re
from datetime import datetime

from shared.models import (
    AIUsage,
    Collection,
    Job,
    JobStatus,
    LLMRequest,
    LLMResponse,
    Step,
    StepStatus,
    Worker,
    generate_job_id,
    generate_worker_id,
)


class TestEnums:
    def test_job_status_values(self):
        assert set(JobStatus) == {
            JobStatus.PENDING,
            JobStatus.DOWNLOADING,
            JobStatus.PROCESSING,
            JobStatus.DONE,
            JobStatus.FAILED,
        }

    def test_step_status_values(self):
        assert set(StepStatus) == {
            StepStatus.WAITING,
            StepStatus.READY,
            StepStatus.RUNNING,
            StepStatus.DONE,
            StepStatus.FAILED,
            StepStatus.SKIPPED,
        }

    def test_job_status_is_str(self):
        assert JobStatus.PENDING == "pending"
        assert isinstance(JobStatus.DONE, str)

    def test_step_status_is_str(self):
        assert StepStatus.RUNNING == "running"
        assert isinstance(StepStatus.SKIPPED, str)


class TestJobId:
    def test_format(self):
        jid = generate_job_id()
        assert re.match(r"^j_\d{8}_[0-9a-f]{6}$", jid)

    def test_uniqueness(self):
        # 后缀仅 24 bit（token_hex(3)），同一天内取 1000 个按生日悖论约 3% 概率
        # 出现 1 次碰撞，断言绝对唯一会 flaky。真正的唯一性由 DB 主键约束兜底，
        # 这里只验证生成器碰撞率足够低（容忍生日悖论下的极少量碰撞）。
        ids = {generate_job_id() for _ in range(1000)}
        assert len(ids) >= 998

    def test_contains_date(self):
        jid = generate_job_id()
        date_part = jid.split("_")[1]
        datetime.strptime(date_part, "%Y%m%d")


class TestWorkerId:
    def test_format(self):
        wid = generate_worker_id("cpu")
        assert re.match(r"^cpu-[0-9a-f]{8}$", wid)

    def test_type_prefix(self):
        for t in ["download", "cpu", "gpu", "ai"]:
            wid = generate_worker_id(t)
            assert wid.startswith(f"{t}-")

    def test_uniqueness(self):
        ids = {generate_worker_id("ai") for _ in range(1000)}
        assert len(ids) == 1000


class TestJobDefaults:
    def test_minimal_creation(self):
        job = Job(id="j_20260517_abc123", content_type="video", pipeline="video")
        assert job.status == JobStatus.PENDING
        assert job.domain == "general"
        assert job.style_tags == []
        assert job.progress_pct == 0
        assert job.meta == {}
        assert job.error is None
        assert job.current_step is None
        assert isinstance(job.created_at, datetime)

    def test_full_creation(self):
        job = Job(
            id="j_20260517_abc123",
            content_type="paper",
            pipeline="paper",
            status=JobStatus.PROCESSING,
            domain="ml",
            style_tags=["lecture"],
            meta={"pages": 12},
        )
        assert job.content_type == "paper"
        assert job.style_tags == ["lecture"]
        assert job.meta["pages"] == 12


class TestStepDefaults:
    def test_minimal(self):
        step = Step(job_id="j_xxx", name="03_scene")
        assert step.status == StepStatus.WAITING
        assert step.pool == ""
        assert step.retries == 0
        assert step.meta == {}


class TestWorkerDefaults:
    def test_minimal(self):
        w = Worker(id="cpu-12345678", type="cpu")
        assert w.pools == []
        assert w.tags == set()
        assert w.reject_tags == set()
        assert w.status == "offline"
        assert w.tasks_completed == 0

    def test_with_tags(self):
        w = Worker(
            id="ai-abcd1234",
            type="ai",
            tags={"vision", "claude-cli"},
            reject_tags={"private"},
        )
        assert "vision" in w.tags
        assert "private" in w.reject_tags


class TestCollectionDefaults:
    def test_minimal(self):
        c = Collection(id="my-dl", name="深度学习", domain="deep-learning")
        assert c.description == ""
        assert c.tags == []
        assert c.job_count == 0


class TestAIUsage:
    def test_creation(self):
        u = AIUsage(
            exec_id="ai-abc:1716000000000:0",
            provider="anthropic",
            model="claude-sonnet-4-6",
            job_id="j_xxx",
            step="10_smart",
            input_tokens=1000,
            output_tokens=500,
            cost_usd=0.0105,
        )
        assert u.exec_id == "ai-abc:1716000000000:0"
        assert u.cached is False


class TestLLMRequest:
    def test_defaults(self):
        req = LLMRequest(messages=[{"role": "user", "content": "hello"}])
        assert req.model is None
        assert req.max_tokens == 4096
        assert req.temperature == 0.7
        assert req.images == []
        assert req.system is None
        assert req.response_format is None


class TestLLMResponse:
    def test_creation(self):
        resp = LLMResponse(
            content="result", model="claude-sonnet-4-6", provider="anthropic"
        )
        assert resp.cost_usd == 0.0
        assert resp.cached is False
