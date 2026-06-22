"""tests for shared/models.py"""

import re

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
    derive_job_id,
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


class TestDeriveJobId:
    def test_bilibili_bv(self):
        # B 站用 BV 号(稳定/唯一/路径安全)
        assert derive_job_id("https://b23.tv/BV1xx411c7mD", "video", "bilibili") == "jobs_bili_BV1xx411c7mD"

    def test_url_hash_stable(self):
        a = derive_job_id("https://example.com/x", "article")
        b = derive_job_id("https://example.com/x", "article")
        assert a == b and a.startswith("jobs_article_")          # 同 url 稳定

    def test_no_url_random(self):
        assert derive_job_id(None, "paper").startswith("jobs_paper_")


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
        # 仅断承载契约的默认:PENDING 是初始态(调度据此挑活)、domain 缺省 general(驱动 prompt 选型)。
        # 空容器/0/None/created_at 工厂等纯 dataclass 机制默认已删(变了不改变行为)。
        job = Job(id="j_20260517_abc123", content_type="video", pipeline="video")
        assert job.status == JobStatus.PENDING
        assert job.domain == "general"

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
        # 契约默认:WAITING 是初始态(DAG 据此推进);pool="" 是"未分配"路由哨兵。
        # retries==0 / meta=={} 纯机制默认已删。
        step = Step(job_id="j_xxx", name="03_scene")
        assert step.status == StepStatus.WAITING
        assert step.pool == ""


class TestWorkerDefaults:
    def test_minimal(self):
        # 契约默认:新 Worker 初始 offline(注册后才转 idle)。空容器/0 计数纯机制默认已删。
        w = Worker(id="cpu-12345678", type="cpu")
        assert w.status == "offline"

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
        # 契约默认:新 collection 初始 job_count==0(入库计数从 0 起算)。空串/空容器机制默认已删。
        c = Collection(id="my-dl", name="深度学习", domain="deep-learning")
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
        # 契约默认:max_tokens=4096 / temperature=0.7 直接随请求发给 LLM,改了会改变生成行为。
        # model/images/system/response_format 的 None/空默认是纯机制,已删。
        req = LLMRequest(messages=[{"role": "user", "content": "hello"}])
        assert req.max_tokens == 4096
        assert req.temperature == 0.7


class TestLLMResponse:
    def test_creation(self):
        resp = LLMResponse(
            content="result", model="claude-sonnet-4-6", provider="anthropic"
        )
        assert resp.cost_usd == 0.0
        assert resp.cached is False
