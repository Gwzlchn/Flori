"""tests for shared/models.py"""

import json
import re
from pathlib import Path

from shared.models import (
    AITask,
    AIUsage,
    Collection,
    Job,
    JobStatus,
    LLMRequest,
    LLMResponse,
    Step,
    StepStatus,
    TaskKind,
    Worker,
    derive_job_id,
    generate_worker_id,
)
from shared.ids import lineage_key, lineage_key_of


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
        # B 站用 BV 号(稳定/唯一/路径安全);job_id 带时间戳后缀,lineage_key 是去时间戳的基础 id。
        jid = derive_job_id("https://b23.tv/BV1xx411c7mD", "video", "bilibili")
        assert jid.startswith("jobs_bili_BV1xx411c7mD_")
        assert lineage_key("https://b23.tv/BV1xx411c7mD", "video", "bilibili") == "jobs_bili_BV1xx411c7mD"
        assert lineage_key_of(jid) == "jobs_bili_BV1xx411c7mD"

    def test_timestamped_unique_same_lineage(self):
        # 同 url 重投 = 不同 job_id(时间戳)但同 lineage_key(同源归组)。
        a = derive_job_id("https://example.com/x", "article")
        b = derive_job_id("https://example.com/x", "article")
        assert a != b                                            # 时间戳/随机 → 各自新快照
        assert a.startswith("jobs_article_")
        assert lineage_key_of(a) == lineage_key_of(b)            # 同 lineage
        assert lineage_key("https://example.com/x", "article") == lineage_key_of(a)

    def test_no_url_random(self):
        assert derive_job_id(None, "paper").startswith("jobs_paper_")


class TestWorkerId:
    def test_format(self):
        wid = generate_worker_id("cpu")
        assert re.match(r"^cpu-[0-9a-f]{8}$", wid)

    def test_type_prefix(self):
        for t in ["io", "cpu", "gpu", "ai"]:
            wid = generate_worker_id(t)
            assert wid.startswith(f"{t}-")

    def test_uniqueness(self):
        ids = {generate_worker_id("ai") for _ in range(1000)}
        assert len(ids) == 1000


class TestJobDefaults:
    def test_minimal_creation(self):
        # 仅断承载契约的默认:PENDING 是初始态,调度据此挑活;domain 缺省 general,驱动 prompt 选型。
        # 纯 dataclass 机制默认(空容器/0/None/created_at 工厂)不断言:变了不改变行为。
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
        # retries==0 / meta=={} 是纯机制默认,不断言。
        step = Step(job_id="j_xxx", name="03_scene")
        assert step.status == StepStatus.WAITING
        assert step.pool == ""


class TestWorkerDefaults:
    def test_minimal(self):
        # 契约默认:新 Worker 初始 offline(注册后才转 idle)。空容器/0 计数是纯机制默认,不断言。
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
        # 契约默认:新 collection 初始 job_count==0(入库计数从 0 起算)。空串/空容器是纯机制默认,不断言。
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
        # model/images/system/response_format 的 None/空默认是纯机制,不断言。
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


class TestLLMRequestJsonable:
    """to_jsonable/from_jsonable 往返(AI task 内联投递依赖它,images Path 须 json 安全)。"""

    def test_round_trip(self):
        req = LLMRequest(
            messages=[{"role": "user", "content": "hi"}], system="sys",
            max_tokens=2048, temperature=0.3, allowed_tools=["WebSearch"], max_turns=3,
        )
        d = req.to_jsonable()
        json.dumps(d)  # 必须 json 安全(可入队)
        assert d["images"] == []
        back = LLMRequest.from_jsonable(d)
        assert back.messages == req.messages and back.system == "sys"
        assert back.max_tokens == 2048 and back.temperature == 0.3
        assert back.allowed_tools == ["WebSearch"] and back.max_turns == 3

    def test_images_path_to_str(self):
        req = LLMRequest(messages=[], images=[Path("/tmp/a.png"), Path("/tmp/b.png")])
        d = req.to_jsonable()
        assert d["images"] == ["/tmp/a.png", "/tmp/b.png"]
        assert [str(p) for p in LLMRequest.from_jsonable(d).images] == ["/tmp/a.png", "/tmp/b.png"]


class TestLLMResponseJsonable:
    def test_round_trip(self):
        resp = LLMResponse(
            content="ans", model="claude-opus-4-8", provider="claude-cli",
            cost_usd=0.18, num_turns=1, attempts=[{"tier": "primary"}], raw={"x": 1},
        )
        d = resp.to_jsonable()
        json.dumps(d)  # airesult 回写须 json 安全
        back = LLMResponse.from_jsonable(d)
        assert back.content == "ans" and back.provider == "claude-cli"
        assert back.cost_usd == 0.18 and back.attempts == [{"tier": "primary"}]

    def test_from_jsonable_ignores_extra_keys(self):
        resp = LLMResponse.from_jsonable(
            {"content": "a", "model": "m", "provider": "p", "_extra": 1}
        )
        assert resp.content == "a" and resp.provider == "p"


class TestAITask:
    """独立 AI task:无 job、kind='ai'、内联 LLMRequest;payload 往返 + 默认。"""

    def test_payload_round_trip(self):
        req = LLMRequest(messages=[{"role": "user", "content": "Q"}], system="S", temperature=0.3)
        payload = AITask(task_id="at_1", request=req, step_name="synthesis", domain="dl").to_task_payload()
        assert payload["kind"] == TaskKind.AI.value == "ai"
        assert payload["task_id"] == "at_1" and payload["step"] == "synthesis"
        assert payload["require_tags"] == ["claude-cli"] and payload["pool"] == "ai"
        assert "job_id" not in payload  # AI task 不挂 job
        json.dumps(payload)  # 可入队
        back = AITask.from_task_payload(payload)
        assert back.task_id == "at_1" and back.step_name == "synthesis" and back.domain == "dl"
        assert back.request.messages == req.messages and back.request.system == "S"

    def test_defaults(self):
        task = AITask(task_id="at_2", request=LLMRequest(messages=[]))
        assert task.require_tags == ["claude-cli"] and task.step_name == "ai" and task.domain is None
