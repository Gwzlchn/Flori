"""AI 审计日志(prompt 白盒化)测试:
- gateway 在成功/降级/全败时正确记录 tier_used + 逐 tier attempts;
- step_base.call_ai 把每次 LLM 调用落 output/ai_logs/{step}.jsonl(成功 + 失败均记);
- call_ai_json 解析后回填 output_processed。"""

import json

import pytest

from shared.ai_gateway import AIGateway, DryRunProvider
from shared.errors import AIProviderError, AllProvidersFailedError
from shared.models import LLMRequest, LLMResponse
from shared.step_base import StepBase


# 测试脚手架

class _Step(StepBase):
    def __init__(self, job_dir, config=None):
        super().__init__("11_smart", job_dir, config or {})

    def execute(self):
        return {}


class _FakeGW:
    """注入式假 gateway:返回固定 response 或抛固定异常。"""

    def __init__(self, response=None, exc=None):
        self._r, self._e = response, exc

    async def call(self, step_name, request):
        if self._e:
            raise self._e
        return self._r


def _mk_response(**kw):
    base = dict(
        content="# note", model="subscription", provider="claude-cli",
        input_tokens=100, output_tokens=50, cache_creation_input_tokens=5,
        cache_read_input_tokens=10, cost_usd=0.02, duration_sec=1.5, num_turns=1,
        session_id="sess-1", api_ms=900.0, tier_used="primary",
        attempts=[{"tier": "primary", "provider": "claude-cli", "model": "subscription", "ok": True}],
        raw={"result": "# note", "session_id": "sess-1"},
    )
    base.update(kw)
    return LLMResponse(**base)


def _read_log(job_dir, step="11_smart"):
    p = job_dir / "output" / "ai_logs" / f"{step}.jsonl"
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


# gateway:attempts + tier_used

class TestGatewayAttempts:
    @pytest.mark.asyncio
    async def test_success_sets_tier_and_attempts(self, monkeypatch):
        monkeypatch.delenv("DRY_RUN", raising=False)
        gw = AIGateway(
            {"providers": {"p": {"type": "anthropic", "api_key": "x"}}},
            {"steps": [{"name": "s", "ai": {"primary": {"provider": "p", "model": "m"}}}]},
        )

        async def ok(self, request):
            return LLMResponse(content="ok", model="m", provider="anthropic")

        gw._providers["p"] = type("P", (), {"complete": ok})()
        resp = await gw.call("s", LLMRequest(messages=[{"role": "user", "content": "t"}]))
        assert resp.tier_used == "primary"
        assert resp.attempts == [{"tier": "primary", "provider": "p", "model": "m", "ok": True}]

    @pytest.mark.asyncio
    async def test_fallback_records_chain(self, monkeypatch):
        monkeypatch.delenv("DRY_RUN", raising=False)
        gw = AIGateway(
            {"providers": {"p1": {"type": "anthropic", "api_key": "x"},
                           "p2": {"type": "anthropic", "api_key": "y"}}},
            {"steps": [{"name": "s", "ai": {
                "primary": {"provider": "p1", "model": "m1"},
                "fallback": {"provider": "p2", "model": "m2"}}}]},
        )

        async def fail(self, request):
            raise AIProviderError("down")

        async def ok(self, request):
            return LLMResponse(content="ok", model="m2", provider="anthropic")

        gw._providers["p1"] = type("P", (), {"complete": fail})()
        gw._providers["p2"] = type("P", (), {"complete": ok})()
        resp = await gw.call("s", LLMRequest(messages=[{"role": "user", "content": "t"}]))
        assert resp.tier_used == "fallback"
        assert [a["ok"] for a in resp.attempts] == [False, True]
        assert resp.attempts[0]["error_class"] == "AIProviderError"
        assert "down" in resp.attempts[0]["error"]

    @pytest.mark.asyncio
    async def test_all_fail_exception_carries_attempts(self, monkeypatch):
        monkeypatch.delenv("DRY_RUN", raising=False)
        gw = AIGateway(
            {"providers": {"p1": {"type": "anthropic", "api_key": "x"}}},
            {"steps": [{"name": "s", "ai": {"primary": {"provider": "p1", "model": "m1"}}}]},
        )

        async def fail(self, request):
            raise AIProviderError("down")

        gw._providers["p1"] = type("P", (), {"complete": fail})()
        with pytest.raises(AllProvidersFailedError) as ei:
            await gw.call("s", LLMRequest(messages=[{"role": "user", "content": "t"}]))
        assert len(ei.value.attempts) == 1
        assert ei.value.attempts[0]["ok"] is False

    @pytest.mark.asyncio
    async def test_dryrun_has_raw(self):
        resp = await DryRunProvider().complete(LLMRequest(messages=[{"role": "user", "content": "t"}]))
        assert resp.raw == {"dry_run": True}


# step_base:ai_logs 落盘

class TestAiLogDump:
    def test_call_ai_writes_full_record(self, tmp_path):
        step = _Step(tmp_path, {
            "ai": {"primary": {"provider": "claude-cli", "model": "subscription"}},
            "pool": "ai", "domain": {"name": "finance"},
        })
        step._gateway = _FakeGW(response=_mk_response())
        out = step.call_ai("hello prompt")
        assert out == "# note"

        (r,) = _read_log(tmp_path)
        assert r["step"] == "11_smart"
        assert r["ok"] is True
        assert r["domain"] == "finance"
        assert r["call_index"] == 0
        assert r["prompt"]["rendered"]["user"] == "hello prompt"
        assert r["routing"]["provider"] == "claude-cli"
        assert r["routing"]["tier_used"] == "primary"
        assert r["routing"]["attempts"][0]["ok"] is True
        assert r["usage"]["input_tokens"] == 100
        assert r["usage"]["cache_read_input_tokens"] == 10
        assert r["cost"]["basis"] == "subscription-equiv"
        assert r["session_id"] == "sess-1"
        assert r["raw"]["session_id"] == "sess-1"
        assert r["env"]["pool"] == "ai"

    def test_multiple_calls_append(self, tmp_path):
        step = _Step(tmp_path, {"ai": {}})
        step._gateway = _FakeGW(response=_mk_response())
        step.call_ai("p1")
        step._gateway = _FakeGW(response=_mk_response(content="second"))
        step.call_ai("p2")

        recs = _read_log(tmp_path)
        assert len(recs) == 2
        assert recs[0]["call_index"] == 0 and recs[1]["call_index"] == 1
        assert recs[1]["prompt"]["rendered"]["user"] == "p2"
        assert recs[1]["output"]["content"] == "second"

    def test_failed_call_logged_with_attempts(self, tmp_path):
        step = _Step(tmp_path, {"ai": {}})
        exc = AllProvidersFailedError(
            "all down", attempts=[{"tier": "primary", "provider": "x", "ok": False, "error": "boom"}])
        step._gateway = _FakeGW(exc=exc)
        with pytest.raises(AllProvidersFailedError):
            step.call_ai("doomed")

        (r,) = _read_log(tmp_path)
        assert r["ok"] is False
        assert "all down" in r["error"]
        assert r["routing"]["attempts"][0]["ok"] is False
        assert r["prompt"]["rendered"]["user"] == "doomed"
        assert r["output"]["content"] is None

    def test_call_ai_json_amends_output_processed(self, tmp_path):
        step = _Step(tmp_path, {"ai": {}})
        step._gateway = _FakeGW(
            response=_mk_response(content='{"key_terms": ["A", "B"], "overall": 4}'))
        result, parse_failed = step.call_ai_json("review prompt", fallback={"key_terms": []})
        assert parse_failed is False
        assert result["key_terms"] == ["A", "B"]

        (r,) = _read_log(tmp_path)
        op = r["output_processed"]
        assert op["json_parse"]["ok"] is True
        assert op["json_parse"]["salvaged"] is False
        assert op["extracted"]["key_terms"] == ["A", "B"]

    def test_log_write_never_breaks_main_flow(self, tmp_path):
        """ai_logs 落盘异常不得影响主流程:即便记录组装出错,call_ai 仍返回内容。"""
        step = _Step(tmp_path, {"ai": {}})
        step._gateway = _FakeGW(response=_mk_response(content="resilient"))
        # 破坏组装:让 _build_ai_log_record 抛错,验证被吞、主流程不受影响。
        step._build_ai_log_record = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        assert step.call_ai("x") == "resilient"


class TestTranscriptSidecar:
    """agentic 全轨迹白盒(job 步):transcript 拷为 sidecar output/ai_logs/{step}.turns.{n}.jsonl,记录留引用。"""

    def test_transcript_copied_as_sidecar(self, tmp_path):
        src = tmp_path / "sess.jsonl"
        src.write_text('{"type":"user"}\n{"type":"assistant"}\n')
        step = _Step(tmp_path, {"ai": {}})
        step._gateway = _FakeGW(response=_mk_response(transcript_path=str(src)))
        step.call_ai("hello")
        rec = _read_log(tmp_path)[0]
        assert rec["transcript"]["file"] == "output/ai_logs/11_smart.turns.0.jsonl"
        assert rec["transcript"]["turns"] == 2
        sidecar = tmp_path / "output" / "ai_logs" / "11_smart.turns.0.jsonl"
        assert sidecar.read_text() == src.read_text()          # 全轨迹原样入产物

    def test_sidecar_index_follows_call_index(self, tmp_path):
        src = tmp_path / "sess.jsonl"; src.write_text("{}\n")
        step = _Step(tmp_path, {"ai": {}})
        step._gateway = _FakeGW(response=_mk_response(transcript_path=str(src)))
        step.call_ai("a"); step.call_ai("b")
        recs = _read_log(tmp_path)
        assert recs[0]["transcript"]["file"].endswith(".turns.0.jsonl")
        assert recs[1]["transcript"]["file"].endswith(".turns.1.jsonl")
        assert (tmp_path / "output" / "ai_logs" / "11_smart.turns.1.jsonl").exists()

    def test_no_transcript_records_reason(self, tmp_path):
        step = _Step(tmp_path, {"ai": {}})
        step._gateway = _FakeGW(response=_mk_response(transcript_path=None))
        step.call_ai("hello")
        rec = _read_log(tmp_path)[0]
        assert rec["transcript"]["file"] is None and "reason" in rec["transcript"]

    def test_failed_call_recovers_transcript_from_attempts(self, tmp_path):
        # 失败调用:gateway 尝试链带 transcript_path(CLI 失败留痕)→ 同样拷 sidecar
        src = tmp_path / "fail.jsonl"; src.write_text('{"e":1}\n')
        exc = AllProvidersFailedError(
            "all failed", error_type="ai",
            attempts=[{"tier": "primary", "provider": "claude-cli", "model": "subscription",
                       "ok": False, "transcript_path": str(src)}])
        step = _Step(tmp_path, {"ai": {}})
        step._gateway = _FakeGW(exc=exc)
        with pytest.raises(AllProvidersFailedError):
            step.call_ai("hello")
        rec = _read_log(tmp_path)[0]
        assert rec["ok"] is False
        assert rec["transcript"]["file"] == "output/ai_logs/11_smart.turns.0.jsonl"
        assert (tmp_path / "output" / "ai_logs" / "11_smart.turns.0.jsonl").read_text() == '{"e":1}\n'
