"""shared/ai_gateway.py 测试:计价、重试策略、provider 回退链、CLI 用量解析。"""

import copy
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from shared.ai_gateway import (
    AIGateway,
    AnthropicProvider,
    ClaudeCLIProvider,
    CodexCLIProvider,
    DryRunProvider,
    OpenAICompatibleProvider,
    QoderCLIProvider,
    _extract_cli_model,
    calc_cost,
    collect_usage_from_file,
    record_usage_to_file,
)
from shared.errors import AIProviderError, AIRateLimitError, AllProvidersFailedError
from shared.models import AIUsage, LLMRequest, LLMResponse


class TestCalcCost:
    def test_known_model(self):
        cost = calc_cost("anthropic", "claude-sonnet-4-6", 1_000_000, 1_000_000)
        assert cost == pytest.approx(3.0 + 15.0)

    def test_unknown_model(self):
        cost = calc_cost("unknown", "unknown-model", 1000, 1000)
        assert cost == 0.0

    def test_zero_tokens(self):
        cost = calc_cost("anthropic", "claude-sonnet-4-6", 0, 0)
        assert cost == 0.0

    def test_input_output_priced_separately(self):
        # 不对称用例:input/output 分别计价。test_known_model 用 1M/1M 对称(3+15==15+3),
        # 抓不到 input/output 价互换;这里分别只给一侧 token 才能钉死方向。
        c_in = calc_cost("anthropic", "claude-sonnet-4-6", 1_000_000, 0)
        c_out = calc_cost("anthropic", "claude-sonnet-4-6", 0, 1_000_000)
        assert c_in == pytest.approx(3.0)       # sonnet input $3/M
        assert c_out == pytest.approx(15.0)     # sonnet output $15/M
        assert c_in != c_out

    def test_cost_divides_by_million(self):
        # 防 /1_000_000 被改:半百万 input 应恰为整百万的一半。
        full = calc_cost("anthropic", "claude-sonnet-4-6", 1_000_000, 0)
        half = calc_cost("anthropic", "claude-sonnet-4-6", 500_000, 0)
        assert full == pytest.approx(3.0)
        assert half == pytest.approx(full / 2)


class TestRetryPolicy:
    def test_rate_limit_long_backoff(self):
        from shared.errors import RETRY_POLICY, get_retry_delay
        # 限流:递增长退避,等 CLI 额度恢复(而非 90s 内烧完转终态)。
        assert RETRY_POLICY["ai_rate_limit"]["max"] == 5
        assert get_retry_delay("ai_rate_limit", 0) == 300
        assert get_retry_delay("ai_rate_limit", 4) == 1800
        assert get_retry_delay("ai_rate_limit", 5) is None
        # 普通 ai 错误仍是短退避 3 次。
        assert get_retry_delay("ai", 0) == 30 and get_retry_delay("ai", 3) is None


class TestDryRunProvider:
    @pytest.mark.asyncio
    async def test_returns_response(self):
        p = DryRunProvider()
        req = LLMRequest(
            messages=[{"role": "user", "content": "hello"}],
            model="test-model",
        )
        resp = await p.complete(req)
        assert "[DRY_RUN]" in resp.content
        assert resp.provider == "dry-run"
        assert resp.cost_usd == 0.0
        assert resp.input_tokens == 0
        assert resp.tier_used == "primary"
        assert resp.attempts == [{
            "tier": "primary", "provider": "dry-run", "model": "test-model", "ok": True,
        }]

    @pytest.mark.asyncio
    async def test_document_translation_echoes_strict_segment_shape(self):
        prompt = (
            "你是 Document 流水线的忠实翻译器。\nINPUT="
            '{"schema_version":1,"segments":['
            '{"id":"S1","kind":"paragraph","text":"Latency is 3 ms.",'
            '"protected_tokens":["3 ms"]}]}'
        )
        response = await DryRunProvider().complete(LLMRequest(
            messages=[{"role": "user", "content": prompt}],
            response_format="json",
        ))

        assert json.loads(response.content) == {
            "segments": [{"id": "S1", "text": "Latency is 3 ms."}],
        }

    @pytest.mark.asyncio
    async def test_document_knowledge_prompts_return_contract_shapes(self):
        provider = DryRunProvider()
        smart = await provider.complete(LLMRequest(messages=[{
            "role": "user",
            "content": (
                "--- 可引用来源坐标 ---\n"
                "引用事实时保留 [[source:ID]]。\n"
                "[[source:S1]] Latency is 3 ms."
            ),
        }]))
        concepts = await provider.complete(LLMRequest(
            messages=[{"role": "user", "content": (
                '--- 已验证概念证据锚点(JSON) ---\n'
                '{"anchors":["Latency is 3 ms."],"truncated":false}\n'
            )}],
            response_format="json",
        ))
        review = await provider.complete(LLMRequest(
            messages=[{"role": "user", "content": (
                "评分维度（每项打 1-5 的整数）：\n"
                "1. accuracy: 准确\n2. traceability: 可追溯\n"
            )}],
            response_format="json",
        ))
        semantic = await provider.complete(LLMRequest(
            messages=[{"role": "user", "content": (
                '你是独立证据核验器\nINPUT={"items":[{"decision_id":"d000"}]}'
            )}],
            response_format="json",
        ))

        assert "[[source:S1]]" in smart.content
        assert "[[source:ID]]" not in smart.content
        assert json.loads(concepts.content)["key_terms"][0]["term"] == "Latency"
        assert json.loads(review.content)["accuracy"] == 4
        assert json.loads(semantic.content)["decisions"][0]["decision_id"] == "d000"


class TestProviderKeyFromEnv:
    """配置不落密钥时,_create_provider 按 {NAME}_API_KEY 约定从环境补齐。"""

    def test_anthropic_key_from_env_when_config_empty(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "env-secret")
        gw = AIGateway({"providers": {"anthropic": {"type": "anthropic"}}}, {"steps": []})
        assert gw._create_provider("anthropic")._api_key == "env-secret"

    def test_openai_compatible_key_from_env(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-secret")
        gw = AIGateway(
            {"providers": {"deepseek": {"type": "openai_compatible", "base_url": "http://x"}}},
            {"steps": []},
        )
        assert gw._create_provider("deepseek")._api_key == "ds-secret"

    def test_config_key_takes_precedence_over_env(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "env-secret")
        gw = AIGateway(
            {"providers": {"anthropic": {"type": "anthropic", "api_key": "cfg-key"}}},
            {"steps": []},
        )
        assert gw._create_provider("anthropic")._api_key == "cfg-key"

    def test_codex_provider_created(self):
        gw = AIGateway(
            {"providers": {"codex-cli": {"type": "codex_cli", "command": ["codex", "exec"]}}},
            {"steps": []},
        )
        assert isinstance(gw._create_provider("codex-cli"), CodexCLIProvider)


class TestAIGateway:
    @pytest.fixture
    def gateway_config(self):
        providers_config = {
            "providers": {
                "mock_primary": {"type": "anthropic", "api_key": "fake"},
                "mock_fallback": {"type": "openai_compatible", "base_url": "http://fake", "api_key": "fake"},
                "mock_text": {"type": "openai_compatible", "base_url": "http://fake2", "api_key": "fake"},
            }
        }
        pipelines_config = {
            "steps": [
                {
                    "name": "10_smart",
                    "ai": {
                        "primary": {"provider": "mock_primary", "model": "claude-sonnet-4-6"},
                        "fallback": {"provider": "mock_fallback", "model": "gpt-4o"},
                        "text_fallback": {"provider": "mock_text", "model": "deepseek-v4-pro"},
                    },
                },
                {
                    "name": "no_ai_step",
                },
            ]
        }
        return providers_config, pipelines_config

    @pytest.mark.asyncio
    async def test_dry_run_mode(self, gateway_config, monkeypatch):
        monkeypatch.setenv("DRY_RUN", "1")
        gw = AIGateway(*gateway_config)
        req = LLMRequest(messages=[{"role": "user", "content": "test"}])
        resp = await gw.call("10_smart", req)
        assert "[DRY_RUN]" in resp.content

    @pytest.mark.asyncio
    async def test_primary_success(self, gateway_config, monkeypatch):
        monkeypatch.delenv("DRY_RUN", raising=False)
        gw = AIGateway(*gateway_config)

        mock_resp = LLMResponse(
            content="ok", model="m", provider="p",
            input_tokens=11, output_tokens=22, cost_usd=0.123)

        async def mock_complete(self, request):
            return mock_resp

        gw._providers["mock_primary"] = type("P", (), {"complete": mock_complete})()
        resp = await gw.call("10_smart", LLMRequest(messages=[{"role": "user", "content": "test"}]))
        assert resp.content == "ok"
        # 透传层不能把 provider 算好的成本/token 清零或吞掉。
        assert resp.cost_usd == 0.123
        assert (resp.input_tokens, resp.output_tokens) == (11, 22)

    @pytest.mark.asyncio
    async def test_fallback_on_primary_failure(self, gateway_config, monkeypatch):
        monkeypatch.delenv("DRY_RUN", raising=False)
        gw = AIGateway(*gateway_config)

        async def fail_complete(self, request):
            raise AIProviderError("down")

        mock_resp = LLMResponse(content="fallback_ok", model="m", provider="p")

        async def ok_complete(self, request):
            return mock_resp

        gw._providers["mock_primary"] = type("P", (), {"complete": fail_complete})()
        gw._providers["mock_fallback"] = type("P", (), {"complete": ok_complete})()
        resp = await gw.call("10_smart", LLMRequest(messages=[{"role": "user", "content": "test"}]))
        assert resp.content == "fallback_ok"

    @pytest.mark.asyncio
    async def test_text_fallback_strips_images(self, gateway_config, monkeypatch):
        monkeypatch.delenv("DRY_RUN", raising=False)
        gw = AIGateway(*gateway_config)

        async def fail_complete(self, request):
            raise AIProviderError("down")

        captured_request = {}

        async def text_complete(self, request):
            captured_request["images"] = request.images
            return LLMResponse(content="text_ok", model="m", provider="p")

        gw._providers["mock_primary"] = type("P", (), {"complete": fail_complete})()
        gw._providers["mock_fallback"] = type("P", (), {"complete": fail_complete})()
        gw._providers["mock_text"] = type("P", (), {"complete": text_complete})()

        req = LLMRequest(
            messages=[{"role": "user", "content": "test"}],
            images=[Path("/fake/img.jpg")],
        )
        resp = await gw.call("10_smart", req)
        assert resp.content == "text_ok"
        # text_fallback 用副本去图调用,原始 request 的 images 不能被清空(防复用/重试丢图)。
        assert captured_request["images"] == []
        assert req.images == [Path("/fake/img.jpg")]

    @pytest.mark.asyncio
    async def test_all_fail_raises(self, gateway_config, monkeypatch):
        monkeypatch.delenv("DRY_RUN", raising=False)
        gw = AIGateway(*gateway_config)

        async def fail_complete(self, request):
            raise AIProviderError("down")

        gw._providers["mock_primary"] = type("P", (), {"complete": fail_complete})()
        gw._providers["mock_fallback"] = type("P", (), {"complete": fail_complete})()
        gw._providers["mock_text"] = type("P", (), {"complete": fail_complete})()

        with pytest.raises(AllProvidersFailedError):
            await gw.call(
                "10_smart",
                LLMRequest(
                    messages=[{"role": "user", "content": "test"}],
                    images=[Path("/fake/img.jpg")],
                ),
            )

    @pytest.mark.asyncio
    async def test_all_fail_rate_limited_marks_rate_limit(self, gateway_config, monkeypatch):
        """任一 provider 限流 → AllProvidersFailedError.error_type=ai_rate_limit(走长退避)。"""
        monkeypatch.delenv("DRY_RUN", raising=False)
        gw = AIGateway(*gateway_config)

        async def rl(self, request):
            raise AIRateLimitError("usage limit reached")

        for p in ("mock_primary", "mock_fallback", "mock_text"):
            gw._providers[p] = type("P", (), {"complete": rl})()
        with pytest.raises(AllProvidersFailedError) as ei:
            await gw.call("10_smart", LLMRequest(
                messages=[{"role": "user", "content": "t"}], images=[Path("/f.jpg")]))
        assert ei.value.error_type == "ai_rate_limit"

    @pytest.mark.asyncio
    async def test_all_fail_generic_keeps_ai_type(self, gateway_config, monkeypatch):
        monkeypatch.delenv("DRY_RUN", raising=False)
        gw = AIGateway(*gateway_config)

        async def fail(self, request):
            raise AIProviderError("5xx")

        for p in ("mock_primary", "mock_fallback", "mock_text"):
            gw._providers[p] = type("P", (), {"complete": fail})()
        with pytest.raises(AllProvidersFailedError) as ei:
            await gw.call("10_smart", LLMRequest(
                messages=[{"role": "user", "content": "t"}], images=[Path("/f.jpg")]))
        assert ei.value.error_type == "ai"

    @pytest.mark.asyncio
    async def test_no_ai_config_raises(self, gateway_config, monkeypatch):
        monkeypatch.delenv("DRY_RUN", raising=False)
        gw = AIGateway(*gateway_config)
        with pytest.raises(AllProvidersFailedError):
            await gw.call("no_ai_step", LLMRequest(messages=[{"role": "user", "content": "test"}]))


class TestUsageFile:
    def test_record_and_collect(self, tmp_path):
        u1 = AIUsage(
            exec_id="ai-abc:1716000:0",
            provider="anthropic",
            model="claude-sonnet-4-6",
            step="10_smart",
            input_tokens=100,
            cost_usd=0.01,
        )
        u2 = AIUsage(
            exec_id="ai-abc:1716000:1",
            provider="deepseek",
            model="deepseek-v4-pro",
            step="10_smart",
            input_tokens=200,
            cost_usd=0.005,
        )
        record_usage_to_file(u1, tmp_path)
        record_usage_to_file(u2, tmp_path)

        collected = collect_usage_from_file(tmp_path, "10_smart")
        assert len(collected) == 2
        assert collected[0].exec_id == "ai-abc:1716000:0"
        assert collected[1].provider == "deepseek"

    def test_collect_missing_file(self, tmp_path):
        assert collect_usage_from_file(tmp_path, "nonexistent") == []

    def test_collect_full_roundtrip(self, tmp_path):
        # 每个字段给唯一可区分值,record→collect 后逐字段断言——钉死 collect 的字段映射,
        # 防 provider↔model、input↔output、cost↔duration 之类互换变异存活。
        u = AIUsage(
            exec_id="ai-xyz:42:7",
            provider="anthropic",
            model="claude-opus-4-8",
            job_id="job-777",
            step="11_review",
            input_tokens=123,
            output_tokens=456,
            cost_usd=0.0789,
            duration_sec=12.5,
            cached=True,
            created_at=datetime(2026, 6, 22, 13, 30, 5),
        )
        record_usage_to_file(u, tmp_path)
        (got,) = collect_usage_from_file(tmp_path, "11_review")
        assert got.exec_id == "ai-xyz:42:7"
        assert got.provider == "anthropic"
        assert got.model == "claude-opus-4-8"
        assert got.job_id == "job-777"
        assert got.step == "11_review"
        assert got.input_tokens == 123
        assert got.output_tokens == 456
        assert got.cost_usd == pytest.approx(0.0789)
        assert got.duration_sec == pytest.approx(12.5)
        assert got.cached is True
        assert got.created_at == datetime(2026, 6, 22, 13, 30, 5)

    def test_collect_applies_defaults_for_missing_optional(self, tmp_path):
        # 历史/精简记录缺可选字段时,collect 应回退到正确默认值——
        # 钉死 .get(key, DEFAULT) 的默认值(防 0→1、0.0→1.0、False→True、None→"x" 变异)。
        path = tmp_path / ".09_mechanical.usage.json"
        path.write_text(json.dumps([{
            "exec_id": "e1", "provider": "p", "model": "m",
            "created_at": "2026-06-22T00:00:00",
        }]))
        (got,) = collect_usage_from_file(tmp_path, "09_mechanical")
        assert got.job_id is None
        assert got.step is None
        assert got.input_tokens == 0
        assert got.output_tokens == 0
        assert got.cost_usd == 0.0
        assert got.duration_sec == 0.0
        assert got.cached is False

    def test_record_creates_nested_dir_and_appends(self, tmp_path):
        # mkdir parents + 文件名 .{step}.usage.json + 追加(非覆盖)+ 保序。
        sub = tmp_path / "deep" / "logs"
        record_usage_to_file(
            AIUsage(exec_id="e1", provider="p", model="m", step="06_ocr"), sub)
        f = sub / ".06_ocr.usage.json"
        assert f.exists()
        assert len(json.loads(f.read_text())) == 1
        record_usage_to_file(
            AIUsage(exec_id="e2", provider="p", model="m", step="06_ocr"), sub)
        data = json.loads(f.read_text())
        assert [d["exec_id"] for d in data] == ["e1", "e2"]

    def test_parallel_stage_usage_keeps_every_unique_exec(self, tmp_path):
        def write(index: int) -> None:
            record_usage_to_file(AIUsage(
                exec_id=f"exec:chapter-{index}", provider="qoder-cli",
                model="ultimate", step="05_smart", credits=float(index),
            ), tmp_path)

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(write, range(32)))

        collected = collect_usage_from_file(tmp_path, "05_smart")
        assert len(collected) == 32
        assert {item.exec_id for item in collected} == {
            f"exec:chapter-{index}" for index in range(32)
        }


class TestAnthropicProvider:
    @pytest.mark.asyncio
    async def test_call_success(self):
        provider = AnthropicProvider(api_key="sk-test")

        mock_usage = MagicMock()
        mock_usage.input_tokens = 100
        mock_usage.output_tokens = 50
        mock_usage.cache_read_input_tokens = 0
        mock_usage.cache_creation_input_tokens = 0   # 真实 Usage 对象该字段是 int/None,非 MagicMock

        mock_response = MagicMock()
        mock_response.content = [MagicMock(type="text", text="Hello world")]
        mock_response.usage = mock_usage

        mock_client = MagicMock()
        mock_client.messages.create = MagicMock(return_value=mock_response)
        provider._client = mock_client

        req = LLMRequest(
            messages=[{"role": "user", "content": "hello"}],
            model="claude-sonnet-4-6",
        )
        resp = await provider.complete(req)
        assert resp.content == "Hello world"
        assert resp.provider == "anthropic"
        assert resp.input_tokens == 100
        assert resp.output_tokens == 50
        # 计费接缝:complete 必须把 calc_cost 算进 cost_usd(否则金额静默丢 0 测试照绿)。
        assert resp.cost_usd == pytest.approx(
            calc_cost("anthropic", "claude-sonnet-4-6", 100, 50))
        assert resp.cached is False   # cache_read_input_tokens == 0

    def test_build_messages_attaches_image_to_last_user_only(self, tmp_path):
        """_build_messages:只给最后一条 user message 附图(多轮不把同组图按 base64 重复),媒体类型大小写归一。"""
        img = tmp_path / "f.JPG"; img.write_bytes(b"x")
        provider = AnthropicProvider(api_key="sk-test")
        req = LLMRequest(
            messages=[{"role": "user", "content": "a"},
                      {"role": "assistant", "content": "b"},
                      {"role": "user", "content": "c"}],
            images=[img],
        )
        msgs = provider._build_messages(req)
        assert msgs[0]["content"] == "a"             # 非最后 user → 纯文本不附图
        assert isinstance(msgs[2]["content"], list)   # 最后 user → 附图
        img_part = next(p for p in msgs[2]["content"] if p["type"] == "image")
        assert img_part["source"]["media_type"] == "image/jpeg"   # .JPG → jpeg(大小写归一)

    def test_build_messages_rejects_unknown_image_type(self, tmp_path):
        """不支持的后缀显式报错,避免发出非法 media_type 被 API 拒。"""
        img = tmp_path / "f.bmp"; img.write_bytes(b"x")
        provider = AnthropicProvider(api_key="sk-test")
        req = LLMRequest(messages=[{"role": "user", "content": "a"}], images=[img])
        with pytest.raises(AIProviderError):
            provider._build_messages(req)

    @pytest.mark.asyncio
    async def test_cached_flag_set_when_cache_read(self):
        """cache_read_input_tokens>0 → cached=True(prompt 缓存命中标记,影响计费观感)。"""
        provider = AnthropicProvider(api_key="sk-test")

        mock_usage = MagicMock()
        mock_usage.input_tokens = 100
        mock_usage.output_tokens = 50
        mock_usage.cache_read_input_tokens = 80

        mock_response = MagicMock()
        mock_response.content = [MagicMock(type="text", text="hi")]
        mock_response.usage = mock_usage

        mock_client = MagicMock()
        mock_client.messages.create = MagicMock(return_value=mock_response)
        provider._client = mock_client

        resp = await provider.complete(LLMRequest(
            messages=[{"role": "user", "content": "hello"}], model="claude-sonnet-4-6"))
        assert resp.cached is True

    @pytest.mark.asyncio
    async def test_joins_multiple_text_blocks(self):
        """多 text block(思考型/分段响应)要拼接,不能只取 content[0]。"""
        provider = AnthropicProvider(api_key="sk-test")

        mock_usage = MagicMock()
        mock_usage.input_tokens = 10
        mock_usage.output_tokens = 20
        mock_usage.cache_read_input_tokens = 0

        # 两个 text block + 一个非 text block(应被跳过)。
        block1 = MagicMock(type="text", text="Hello ")
        block2 = MagicMock(type="text", text="world")
        block_other = MagicMock(type="thinking", text="(should be skipped)")

        mock_response = MagicMock()
        mock_response.content = [block1, block_other, block2]
        mock_response.usage = mock_usage

        mock_client = MagicMock()
        mock_client.messages.create = MagicMock(return_value=mock_response)
        provider._client = mock_client

        req = LLMRequest(
            messages=[{"role": "user", "content": "hi"}],
            model="claude-sonnet-4-6",
        )
        resp = await provider.complete(req)
        assert resp.content == "Hello world"

    @pytest.mark.asyncio
    async def test_rate_limit_raises(self):
        provider = AnthropicProvider(api_key="sk-test")

        mock_client = MagicMock()
        mock_client.messages.create = MagicMock(
            side_effect=Exception("rate limit exceeded 429")
        )
        provider._client = mock_client

        req = LLMRequest(
            messages=[{"role": "user", "content": "hello"}],
            model="claude-sonnet-4-6",
        )
        with pytest.raises(AIRateLimitError):
            await provider.complete(req)

    @pytest.mark.asyncio
    async def test_generic_error_raises_provider_error(self):
        provider = AnthropicProvider(api_key="sk-test")

        mock_client = MagicMock()
        mock_client.messages.create = MagicMock(
            side_effect=Exception("server error")
        )
        provider._client = mock_client

        req = LLMRequest(
            messages=[{"role": "user", "content": "hello"}],
            model="claude-sonnet-4-6",
        )
        with pytest.raises(AIProviderError):
            await provider.complete(req)


class TestOpenAICompatibleProvider:
    @pytest.mark.asyncio
    async def test_call_success(self):
        provider = OpenAICompatibleProvider(
            base_url="http://fake", api_key="sk-test", provider_name="deepseek"
        )

        mock_usage = MagicMock()
        mock_usage.prompt_tokens = 80
        mock_usage.completion_tokens = 40

        mock_choice = MagicMock()
        mock_choice.message.content = "OpenAI response"

        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_response.usage = mock_usage

        mock_client = MagicMock()
        mock_client.chat.completions.create = MagicMock(return_value=mock_response)
        provider._client = mock_client

        req = LLMRequest(
            messages=[{"role": "user", "content": "hello"}],
            model="deepseek-v4-pro",
        )
        resp = await provider.complete(req)
        assert resp.content == "OpenAI response"
        assert resp.provider == "deepseek"
        assert resp.input_tokens == 80
        assert resp.output_tokens == 40
        # 计费接缝:成本按 provider_name(deepseek)而非固定串计价。
        assert resp.cost_usd == pytest.approx(
            calc_cost("deepseek", "deepseek-v4-pro", 80, 40))

    @pytest.mark.asyncio
    async def test_rate_limit_raises(self):
        provider = OpenAICompatibleProvider(
            base_url="http://fake", api_key="sk-test"
        )

        mock_client = MagicMock()
        mock_client.chat.completions.create = MagicMock(
            side_effect=Exception("429 rate limit")
        )
        provider._client = mock_client

        req = LLMRequest(
            messages=[{"role": "user", "content": "hello"}],
            model="gpt-4o",
        )
        with pytest.raises(AIRateLimitError):
            await provider.complete(req)


class TestClaudeCLIProvider:
    @pytest.mark.asyncio
    async def test_call_success(self):
        # sh -c 包裹:provider 追加的 --allowedTools/--max-turns 落到 $0/$1 被忽略,不污染输出。
        provider = ClaudeCLIProvider(
            command_template=["sh", "-c", "echo CLI output"]
        )
        req = LLMRequest(
            messages=[{"role": "user", "content": "hello"}],
            model="claude-opus-4-8[1m]",
        )
        resp = await provider.complete(req)
        assert resp.content == "CLI output"
        assert resp.provider == "claude-cli"
        assert resp.cost_usd == 0.0

    @pytest.mark.asyncio
    async def test_cli_failure_raises(self):
        provider = ClaudeCLIProvider(
            command_template=["false"]
        )
        req = LLMRequest(
            messages=[{"role": "user", "content": "hello"}],
            model="claude-opus-4-8[1m]",
        )
        with pytest.raises(AIProviderError):
            await provider.complete(req)

    @pytest.mark.asyncio
    async def test_cli_timeout_raises(self):
        """Timeout 应 kill 进程并抛 AIProviderError。
        用 mock 子进程(communicate 永挂)触发超时路径,刻意不起真子进程:真子进程的 communicate 被
        wait_for 取消后,其 transport 会残留到本测试事件循环关闭之后才 GC,__del__ 关管道报
        'Event loop is closed'(PytestUnraisableExceptionWarning;生产循环长驻不触发,纯测试噪声)。"""
        import asyncio
        provider = ClaudeCLIProvider(command_template=["claude", "-p"])
        killed = {"n": 0}

        class HangingProc:
            returncode = -9
            async def communicate(self, data=None):
                await asyncio.sleep(999)        # 永挂 → 触发 communicate 的 wait_for 超时
            def kill(self):
                killed["n"] += 1
            async def wait(self):
                return -9

        async def fake_exec(*cmd, **kw):
            return HangingProc()

        original_wait_for = asyncio.wait_for

        async def fast_timeout(coro, timeout):
            # 只缩短大超时(communicate,≥600s)触发超时路径;proc.wait()(timeout=5)保持原样。
            return await original_wait_for(coro, timeout=0.1 if timeout > 10 else timeout)

        req = LLMRequest(
            messages=[{"role": "user", "content": "hello"}],
            model="claude-opus-4-8[1m]",
        )
        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec), \
             patch("shared.ai_gateway.asyncio.wait_for", side_effect=fast_timeout):
            with pytest.raises(AIProviderError, match="timeout"):
                await provider.complete(req)
        assert killed["n"] == 1                 # 超时路径确实 kill 了进程

    @pytest.mark.asyncio
    async def test_slow_text_call_uses_step_level_budget(self, monkeypatch):
        """Opus 完整论文生成不得被旧的 600 秒 provider 预算提前终止。"""
        import asyncio

        seen = []

        async def fake_exec(*_args, **_kwargs):
            return _FakeProc(json.dumps({"result": "ok", "usage": {}}).encode())

        original_wait_for = asyncio.wait_for

        async def capture_timeout(coro, timeout):
            seen.append(timeout)
            return await original_wait_for(coro, timeout=timeout)

        monkeypatch.setattr("shared.ai_gateway.asyncio.create_subprocess_exec", fake_exec)
        monkeypatch.setattr("shared.ai_gateway.asyncio.wait_for", capture_timeout)
        await ClaudeCLIProvider(["claude", "-p"]).complete(
            LLMRequest(messages=[{"role": "user", "content": "x"}]),
        )

        assert seen == [1800]


class TestClaudeCLIVision:
    """claude-cli provider:prompt 走 stdin;有帧图则追加路径 + --allowedTools Read --add-dir。"""

    @pytest.mark.asyncio
    async def test_vision_appends_paths_and_read_tool(self, tmp_path, monkeypatch):
        img = tmp_path / "f1.jpg"; img.write_bytes(b"x")
        cap = {}
        class FakeProc:
            returncode = 0
            async def communicate(self, data=None):
                cap["stdin"] = data; return (b"NOTE", b"")
        async def fake_exec(*cmd, **kw):
            cap["cmd"] = list(cmd); return FakeProc()
        monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
        p = ClaudeCLIProvider(["claude", "-p", "--output-format", "text"])
        resp = await p.complete(LLMRequest(messages=[{"role": "user", "content": "hi"}], images=[img]))
        assert resp.content == "NOTE" and resp.provider == "claude-cli" and resp.cost_usd == 0.0
        assert str(img.resolve()).encode() in cap["stdin"]      # 图路径进 prompt(stdin)
        assert "--allowedTools" in cap["cmd"] and "Read" in cap["cmd"]
        assert "--add-dir" in cap["cmd"] and str(tmp_path.resolve()) in cap["cmd"]
        assert "--max-turns" in cap["cmd"]                       # 限轮数,防多图上下文膨胀拖垮

    @pytest.mark.asyncio
    async def test_text_only_strips_prompt_file_and_no_read(self, monkeypatch):
        cap = {}
        class FakeProc:
            returncode = 0
            async def communicate(self, data=None):
                cap["stdin"] = data; return (b"OK", b"")
        async def fake_exec(*cmd, **kw):
            cap["cmd"] = list(cmd); return FakeProc()
        monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
        # 旧模板残留 {prompt_file} 必须被剥掉
        p = ClaudeCLIProvider(["claude", "-p", "{prompt_file}", "--output-format", "text"])
        resp = await p.complete(LLMRequest(messages=[{"role": "user", "content": "hello"}]))
        assert resp.content == "OK"
        assert "{prompt_file}" not in cap["cmd"]
        assert "Read" not in cap["cmd"]                 # 无图不放开 Read
        assert "--max-turns" in cap["cmd"]              # 纯文本限 1 轮,逼单次生成(防 agentic 拖慢)
        # 纯文本必须 --tools "" 禁用全部工具:否则 claude -p 默认带工具,
        # 大 prompt 下会试调工具消耗唯一一轮→"Reached max turns (1)" 硬失败(线上 11_review 实测)。
        ti = cap["cmd"].index("--tools")
        assert cap["cmd"][ti + 1] == ""
        assert b"hello" in cap["stdin"]

    @pytest.mark.asyncio
    async def test_nonzero_raises(self, monkeypatch):
        class FakeProc:
            returncode = 1
            async def communicate(self, data=None): return (b"", b"boom")
        async def fake_exec(*cmd, **kw): return FakeProc()
        monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
        p = ClaudeCLIProvider(["claude", "-p"])
        with pytest.raises(AIProviderError):
            await p.complete(LLMRequest(messages=[{"role": "user", "content": "x"}]))


# ClaudeCLIProvider: --output-format json 用量解析

class _FakeProc:
    """假 subprocess:communicate 回固定 stdout,returncode=0。"""
    def __init__(self, stdout: bytes, rc: int = 0):
        self._stdout = stdout
        self.returncode = rc

    async def communicate(self, _input=None):
        return self._stdout, b""

    def kill(self):
        pass

    async def wait(self):
        return self.returncode


@pytest.mark.parametrize(
    ("provider_class", "command", "state_dir"),
    (
        (ClaudeCLIProvider, ["claude", "-p"], ".claude"),
        (QoderCLIProvider, ["qodercli", "-p"], ".qoder"),
    ),
)
@pytest.mark.parametrize(
    ("payload", "expected_detail"),
    (
        pytest.param(
            {
                "type": "result", "subtype": "error_during_execution",
                "is_error": True, "errors": ["Qoder API error: BAD_REQUEST"],
                "error_code": 400, "session_id": "p010-session",
            },
            "Qoder API error: BAD_REQUEST",
            id="p010-bad-request",
        ),
        pytest.param(
            {
                "type": "result", "subtype": "error_during_execution",
                "is_error": True, "errors": ["Error in upstream response"],
                "error_code": 500, "session_id": "p015-session",
            },
            "Error in upstream response",
            id="p015-upstream-error",
        ),
    ),
)
@pytest.mark.asyncio
async def test_cli_exit_zero_error_envelope_fails_closed_with_transcript(
    tmp_path, monkeypatch, provider_class, command, state_dir,
    payload, expected_detail,
):
    """真实章节失败形状不得因 CLI exit 0 被当成模型正文。"""
    transcript_dir = tmp_path / state_dir / "projects" / "-app"
    transcript_dir.mkdir(parents=True)
    transcript = transcript_dir / f"{payload['session_id']}.jsonl"
    transcript.write_text('{"type":"assistant"}\n', encoding="utf-8")

    async def fake_exec(*_args, **_kwargs):
        return _FakeProc(json.dumps(payload).encode())

    monkeypatch.setattr("shared.ai_gateway.asyncio.create_subprocess_exec", fake_exec)
    provider = provider_class(command, env={"HOME": str(tmp_path)})
    with pytest.raises(AIProviderError) as caught:
        await provider.complete(LLMRequest(messages=[{"role": "user", "content": "x"}]))

    assert not isinstance(caught.value, AIRateLimitError)
    assert expected_detail in str(caught.value)
    assert "result is missing" in str(caught.value)
    assert caught.value.transcript_path == str(transcript)


@pytest.mark.parametrize(
    ("provider_class", "command"),
    (
        (ClaudeCLIProvider, ["claude", "-p"]),
        (QoderCLIProvider, ["qodercli", "-p"]),
    ),
)
@pytest.mark.parametrize("result", (None, 7, [], {}))
@pytest.mark.asyncio
async def test_cli_envelope_rejects_non_string_result(
    monkeypatch, provider_class, command, result,
):
    payload = {
        "type": "result", "subtype": "success", "is_error": False,
        "result": result,
    }

    async def fake_exec(*_args, **_kwargs):
        return _FakeProc(json.dumps(payload).encode())

    monkeypatch.setattr("shared.ai_gateway.asyncio.create_subprocess_exec", fake_exec)
    with pytest.raises(AIProviderError, match="result is not a string"):
        await provider_class(command).complete(
            LLMRequest(messages=[{"role": "user", "content": "x"}]),
        )


@pytest.mark.parametrize(
    ("provider_class", "command"),
    (
        (ClaudeCLIProvider, ["claude", "-p"]),
        (QoderCLIProvider, ["qodercli", "-p"]),
    ),
)
@pytest.mark.parametrize(
    ("status", "expected"),
    (
        ({"is_error": True, "subtype": "success"}, "is_error=true"),
        (
            {"is_error": False, "subtype": "error_during_execution"},
            "subtype=error_during_execution",
        ),
    ),
)
@pytest.mark.asyncio
async def test_cli_error_status_rejects_even_string_result(
    monkeypatch, provider_class, command, status, expected,
):
    payload = {"type": "result", "result": "partial", **status}

    async def fake_exec(*_args, **_kwargs):
        return _FakeProc(json.dumps(payload).encode())

    monkeypatch.setattr("shared.ai_gateway.asyncio.create_subprocess_exec", fake_exec)
    with pytest.raises(AIProviderError, match=expected):
        await provider_class(command).complete(
            LLMRequest(messages=[{"role": "user", "content": "x"}]),
        )


@pytest.mark.parametrize(
    ("provider_class", "command"),
    (
        (ClaudeCLIProvider, ["claude", "-p"]),
        (QoderCLIProvider, ["qodercli", "-p"]),
    ),
)
@pytest.mark.asyncio
async def test_cli_exit_zero_rate_limit_envelope_is_bounded_and_classified(
    monkeypatch, provider_class, command,
):
    payload = {
        "type": "result", "subtype": "error_during_execution",
        "is_error": True, "result": None,
        "errors": ["x" * 100_000, "quota exceeded, limit reached"],
        "secret": "must-not-leak",
    }

    async def fake_exec(*_args, **_kwargs):
        return _FakeProc(json.dumps(payload).encode())

    monkeypatch.setattr("shared.ai_gateway.asyncio.create_subprocess_exec", fake_exec)
    with pytest.raises(AIRateLimitError) as caught:
        await provider_class(command).complete(
            LLMRequest(messages=[{"role": "user", "content": "x"}]),
        )

    assert len(str(caught.value)) <= 560
    assert "must-not-leak" not in str(caught.value)


@pytest.mark.parametrize(
    ("provider_class", "command"),
    (
        (ClaudeCLIProvider, ["claude", "-p"]),
        (QoderCLIProvider, ["qodercli", "-p"]),
    ),
)
@pytest.mark.asyncio
async def test_cli_success_envelope_keeps_empty_string_result(
    monkeypatch, provider_class, command,
):
    payload = {
        "type": "result", "subtype": "success", "is_error": False, "result": "",
    }

    async def fake_exec(*_args, **_kwargs):
        return _FakeProc(json.dumps(payload).encode())

    monkeypatch.setattr("shared.ai_gateway.asyncio.create_subprocess_exec", fake_exec)
    response = await provider_class(command).complete(
        LLMRequest(messages=[{"role": "user", "content": "x"}]),
    )
    assert response.content == ""


@pytest.mark.asyncio
async def test_claude_cli_parses_json_usage(monkeypatch):
    """claude -p --output-format json → result/usage(含 cache token)/total_cost_usd/num_turns 全解析。"""
    payload = {
        "result": "你好,这是结构化笔记。",
        "model": "claude-opus-4-8",
        "total_cost_usd": 0.0123,
        "num_turns": 3,
        "usage": {
            "input_tokens": 100, "output_tokens": 50,
            "cache_creation_input_tokens": 200, "cache_read_input_tokens": 800,
        },
    }

    async def fake_exec(*a, **k):
        return _FakeProc(json.dumps(payload).encode())

    monkeypatch.setattr("shared.ai_gateway.asyncio.create_subprocess_exec", fake_exec)
    p = ClaudeCLIProvider(command_template=["claude", "-p"])
    r = await p.complete(LLMRequest(messages=[{"role": "user", "content": "x"}]))

    assert r.content == "你好,这是结构化笔记。"
    assert r.provider == "claude-cli" and r.model == "claude-opus-4-8"
    assert r.input_tokens == 100 and r.output_tokens == 50
    assert r.cache_creation_input_tokens == 200
    assert r.cache_read_input_tokens == 800
    assert r.cost_usd == pytest.approx(0.0123)
    assert r.num_turns == 3
    assert r.cached is True   # cache_read>0
    # 命中率可据此算:read/(input+read+creation)=800/1100
    assert r.cache_read_input_tokens / (
        r.input_tokens + r.cache_read_input_tokens + r.cache_creation_input_tokens
    ) == pytest.approx(800 / 1100)


@pytest.mark.asyncio
async def test_claude_cli_fallback_non_json(monkeypatch):
    """旧 CLI / 非 json 输出 → 回退原始文本 + 零统计(向后兼容,不让步骤失败)。"""
    async def fake_exec(*a, **k):
        return _FakeProc(b"plain text answer, not json")

    monkeypatch.setattr("shared.ai_gateway.asyncio.create_subprocess_exec", fake_exec)
    p = ClaudeCLIProvider(command_template=["claude", "-p"])
    r = await p.complete(LLMRequest(messages=[{"role": "user", "content": "x"}]))

    assert r.content == "plain text answer, not json"
    assert r.input_tokens == 0 and r.output_tokens == 0
    assert r.cache_read_input_tokens == 0 and r.cost_usd == 0.0
    assert r.num_turns == 0 and r.cached is False


@pytest.mark.asyncio
async def test_claude_cli_usage_roundtrips_file(tmp_path, monkeypatch):
    """provider→record_usage_to_file→collect_usage_from_file 全程保住 cache/num_turns/worker_id。"""
    payload = {"result": "ok", "model": "claude-opus-4-8", "total_cost_usd": 0.01,
               "num_turns": 2, "usage": {"input_tokens": 10, "output_tokens": 5,
               "cache_creation_input_tokens": 7, "cache_read_input_tokens": 9}}

    async def fake_exec(*a, **k):
        return _FakeProc(json.dumps(payload).encode())

    monkeypatch.setattr("shared.ai_gateway.asyncio.create_subprocess_exec", fake_exec)
    p = ClaudeCLIProvider(command_template=["claude", "-p"])
    r = await p.complete(LLMRequest(messages=[{"role": "user", "content": "x"}]))

    u = AIUsage(exec_id="e1", provider=r.provider, model=r.model, job_id="j", step="11_smart",
                worker_id="ai-abc", input_tokens=r.input_tokens, output_tokens=r.output_tokens,
                cache_creation_input_tokens=r.cache_creation_input_tokens,
                cache_read_input_tokens=r.cache_read_input_tokens, cost_usd=r.cost_usd,
                duration_sec=r.duration_sec, num_turns=r.num_turns, cached=r.cached)
    record_usage_to_file(u, tmp_path)
    back = collect_usage_from_file(tmp_path, "11_smart")
    assert len(back) == 1
    b = back[0]
    assert b.worker_id == "ai-abc" and b.num_turns == 2
    assert b.cache_creation_input_tokens == 7 and b.cache_read_input_tokens == 9


@pytest.mark.asyncio
async def test_claude_cli_forces_json_output_format(monkeypatch):
    """模板带 `--output-format text` 时,provider 必须剔除并强制 json(否则 usage 统计失效)。"""
    seen = {}

    async def fake_exec(*a, **k):
        seen["cmd"] = list(a)
        return _FakeProc(json.dumps({"result": "x", "usage": {}}).encode())

    monkeypatch.setattr("shared.ai_gateway.asyncio.create_subprocess_exec", fake_exec)
    p = ClaudeCLIProvider(command_template=["claude", "-p", "--output-format", "text"])
    await p.complete(LLMRequest(messages=[{"role": "user", "content": "x"}]))
    cmd = seen["cmd"]
    assert "--output-format" in cmd
    assert cmd[cmd.index("--output-format") + 1] == "json"
    assert "text" not in cmd   # 旧 text 已被剔除


def test_calc_cost_cache_aware():
    """缓存感知:写缓存≈1.25× 输入价、读缓存≈0.1× 输入价。"""
    # opus-4-8: input 15 / output 75 per 1M
    base = calc_cost("anthropic", "claude-opus-4-8", 1_000_000, 0)
    assert base == pytest.approx(15.0)
    creation = calc_cost("anthropic", "claude-opus-4-8", 0, 0, cache_creation_tokens=1_000_000)
    assert creation == pytest.approx(15.0 * 1.25)
    read = calc_cost("anthropic", "claude-opus-4-8", 0, 0, cache_read_tokens=1_000_000)
    assert read == pytest.approx(15.0 * 0.1)
    # 读缓存比同量纯输入便宜 10×
    assert read == pytest.approx(base * 0.1)


# claude-cli 真实 model 提取:用量按 provider+model 分组,需要真实模型名而非占位。

class TestExtractCliModel:
    def test_prefers_top_level_model(self):
        # 老/简单格式:顶层 model 直接用,且优先于 modelUsage。
        obj = {"model": "claude-opus-4-8",
               "modelUsage": {"claude-sonnet-4-6": {"inputTokens": 999}}}
        assert _extract_cli_model(obj) == "claude-opus-4-8"

    def test_from_model_usage_picks_max_tokens(self):
        # 新 CLI 顶层无 model,从 modelUsage 取 token 总数最大的键(主力模型)。
        obj = {"modelUsage": {
            "claude-haiku-4": {"inputTokens": 10, "outputTokens": 5},        # 合计 15
            "claude-opus-4-8": {"inputTokens": 800, "outputTokens": 200,
                                "cacheReadInputTokens": 100},                 # 合计 1100
        }}
        assert _extract_cli_model(obj) == "claude-opus-4-8"

    def test_returns_empty_when_missing(self):
        # 两种字段都没有 → 调用方用请求模型兜底。
        assert _extract_cli_model({}) == ""
        assert _extract_cli_model({"model": "", "modelUsage": {}}) == ""

    def test_ignores_non_numeric_token_values(self):
        # token 值非数字(异常 JSON)不应让求和崩溃;仍能挑出唯一可计的键。
        obj = {"modelUsage": {
            "model-a": {"inputTokens": "oops"},
            "model-b": {"inputTokens": 42},
        }}
        assert _extract_cli_model(obj) == "model-b"


@pytest.mark.asyncio
async def test_claude_cli_uses_model_usage_when_no_top_model(monkeypatch):
    """端到端:顶层无 model、有 modelUsage → LLMResponse.model = token 最多的模型。"""
    payload = {
        "result": "ok",
        "modelUsage": {
            "claude-opus-4-8": {"inputTokens": 500, "outputTokens": 120},
            "claude-haiku-4": {"inputTokens": 10},
        },
        "usage": {"input_tokens": 510, "output_tokens": 120},
    }

    async def fake_exec(*a, **k):
        return _FakeProc(json.dumps(payload).encode())

    monkeypatch.setattr("shared.ai_gateway.asyncio.create_subprocess_exec", fake_exec)
    p = ClaudeCLIProvider(command_template=["claude", "-p"])
    r = await p.complete(LLMRequest(messages=[{"role": "user", "content": "x"}]))
    assert r.model == "claude-opus-4-8"


class TestClaudeCLIModelFlag:
    """默认模型 yaml 可配置:--model 优先级 = 步级 request.model(显式模型)> provider 默认 > 不传。"""

    def _cap_provider(self, monkeypatch, cap, **kw):
        class FakeProc:
            returncode = 0
            async def communicate(self, data=None):
                return (b'{"result":"OK","session_id":null}', b"")
        async def fake_exec(*cmd, **kwargs):
            cap["cmd"] = list(cmd)
            cap["env"] = kwargs.get("env") or {}
            return FakeProc()
        monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
        return ClaudeCLIProvider(command_template=["claude", "-p"], **kw)

    @pytest.mark.asyncio
    async def test_request_model_used_when_explicit(self, monkeypatch):
        cap = {}
        p = self._cap_provider(monkeypatch, cap, model="claude-opus-4-8[1m]")
        await p.complete(LLMRequest(messages=[{"role": "user", "content": "x"}], model="claude-opus-4-8[1m]"))
        i = cap["cmd"].index("--model")
        assert cap["cmd"][i + 1] == "claude-opus-4-8[1m]"

    @pytest.mark.asyncio
    async def test_provider_default_model_used_when_request_omits_model(self, monkeypatch):
        cap = {}
        p = self._cap_provider(monkeypatch, cap, model="claude-opus-4-8[1m]")
        await p.complete(LLMRequest(messages=[{"role": "user", "content": "x"}]))
        i = cap["cmd"].index("--model")
        assert cap["cmd"][i + 1] == "claude-opus-4-8[1m]"

    @pytest.mark.asyncio
    async def test_step_level_model_overrides_provider_default(self, monkeypatch):
        cap = {}
        p = self._cap_provider(monkeypatch, cap, model="claude-opus-4-8[1m]")
        await p.complete(LLMRequest(messages=[{"role": "user", "content": "x"}], model="claude-sonnet-4-6"))
        i = cap["cmd"].index("--model")
        assert cap["cmd"][i + 1] == "claude-sonnet-4-6"       # pipelines 步级钉死优先

    @pytest.mark.asyncio
    async def test_no_model_configured_omits_flag(self, monkeypatch):
        cap = {}
        p = self._cap_provider(monkeypatch, cap)              # provider 无默认
        await p.complete(LLMRequest(messages=[{"role": "user", "content": "x"}]))
        assert "--model" not in cap["cmd"]                    # 向后兼容:沿用 CLI 自身默认

    @pytest.mark.asyncio
    async def test_template_model_not_duplicated(self, monkeypatch):
        cap = {}
        class FakeProc:
            returncode = 0
            async def communicate(self, data=None):
                return (b'{"result":"OK"}', b"")
        async def fake_exec(*cmd, **kwargs):
            cap["cmd"] = list(cmd); return FakeProc()
        monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
        p = ClaudeCLIProvider(["claude", "-p", "--model", "claude-haiku-4-5"], model="claude-opus-4-8[1m]")
        await p.complete(LLMRequest(messages=[{"role": "user", "content": "x"}], model="claude-opus-4-8[1m]"))
        assert cap["cmd"].count("--model") == 1               # 模板已带则尊重模板


class TestClaudeCLITranscript:
    """agentic 全轨迹白盒:按 session_id 定位 CLI 自写 transcript;成功/失败都尽力回收。"""

    def _mk_transcript(self, tmp_path, sid="sess-abc"):
        d = tmp_path / ".claude" / "projects" / "-app"
        d.mkdir(parents=True)
        f = d / f"{sid}.jsonl"
        f.write_text('{"type":"user"}\n{"type":"assistant"}\n')
        return f

    def test_find_transcript_by_session_id(self, tmp_path):
        f = self._mk_transcript(tmp_path)
        got = ClaudeCLIProvider._find_transcript("sess-abc", {"HOME": str(tmp_path)})
        assert got == str(f)

    def test_find_transcript_missing_returns_none(self, tmp_path):
        assert ClaudeCLIProvider._find_transcript("nope", {"HOME": str(tmp_path)}) is None
        assert ClaudeCLIProvider._find_transcript(None, {"HOME": str(tmp_path)}) is None

    def test_find_transcript_respects_claude_config_dir(self, tmp_path):
        cfg = tmp_path / "cfg"
        d = cfg / "projects" / "-x"
        d.mkdir(parents=True)
        (d / "s1.jsonl").write_text("{}\n")
        got = ClaudeCLIProvider._find_transcript("s1", {"CLAUDE_CONFIG_DIR": str(cfg)})
        assert got == str(d / "s1.jsonl")

    @pytest.mark.asyncio
    async def test_success_response_carries_transcript_path(self, tmp_path, monkeypatch):
        f = self._mk_transcript(tmp_path, "sess-1")
        class FakeProc:
            returncode = 0
            async def communicate(self, data=None):
                return (b'{"result":"OK","session_id":"sess-1"}', b"")
        async def fake_exec(*cmd, **kwargs):
            return FakeProc()
        monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
        p = ClaudeCLIProvider(["claude", "-p"], env={"HOME": str(tmp_path)})
        resp = await p.complete(LLMRequest(messages=[{"role": "user", "content": "x"}]))
        assert resp.transcript_path == str(f)

    @pytest.mark.asyncio
    async def test_failure_attaches_transcript_to_error(self, tmp_path, monkeypatch):
        f = self._mk_transcript(tmp_path, "sess-f")
        class FakeProc:
            returncode = 1
            async def communicate(self, data=None):
                return (b'{"is_error":true,"session_id":"sess-f"}', b"boom")
        async def fake_exec(*cmd, **kwargs):
            return FakeProc()
        monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
        p = ClaudeCLIProvider(["claude", "-p"], env={"HOME": str(tmp_path)})
        with pytest.raises(AIProviderError) as ei:
            await p.complete(LLMRequest(messages=[{"role": "user", "content": "x"}]))
        assert getattr(ei.value, "transcript_path", None) == str(f)   # 失败留痕经异常带出

    @pytest.mark.asyncio
    async def test_gateway_attempt_carries_failed_transcript(self, monkeypatch, tmp_path):
        """gateway 尝试链把失败 CLI 的 transcript_path 透传给审计层。"""
        f = tmp_path / "t.jsonl"; f.write_text("{}\n")
        err = AIProviderError("CLI failed: x")
        err.transcript_path = str(f)
        class FailProvider:
            async def complete(self, request):
                raise err
        gw = AIGateway({"providers": {"claude-cli": {"type": "claude_cli", "command": ["claude", "-p"]}}},
                       {"steps": [{"name": "s", "ai": {"primary": {"provider": "claude-cli",
                                                                    "model": "claude-opus-4-8[1m]"}}}]})
        monkeypatch.setattr(gw, "_get_provider", lambda name: FailProvider())
        with pytest.raises(AllProvidersFailedError) as ei:
            import asyncio as _a
            await gw.call("s", LLMRequest(messages=[{"role": "user", "content": "x"}]))
        assert ei.value.attempts[0]["transcript_path"] == str(f)


class TestAddDirs:
    """pdf-only 直喂:allowed_tools 分支对 request.add_dirs 追加 --add-dir(Read 出沙箱放行)。"""

    def _cap_provider(self, monkeypatch, cap):
        class FakeProc:
            returncode = 0
            async def communicate(self, data=None):
                return (b'{"result":"OK","session_id":null}', b"")
        async def fake_exec(*cmd, **kwargs):
            cap["cmd"] = list(cmd)
            return FakeProc()
        monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
        return ClaudeCLIProvider(command_template=["claude", "-p"])

    @pytest.mark.asyncio
    async def test_allowed_tools_with_add_dirs(self, monkeypatch):
        cap = {}
        p = self._cap_provider(monkeypatch, cap)
        await p.complete(LLMRequest(
            messages=[{"role": "user", "content": "用 Read 读 /work/input/source.pdf 第 1-2 页"}],
            allowed_tools=["Read"], add_dirs=["/work/input"], max_turns=8,
        ))
        cmd = cap["cmd"]
        assert "--allowedTools" in cmd and "Read" in cmd
        i = cmd.index("--add-dir")
        assert cmd[i + 1] == "/work/input"

    @pytest.mark.asyncio
    async def test_no_add_dirs_no_flag(self, monkeypatch):
        cap = {}
        p = self._cap_provider(monkeypatch, cap)
        await p.complete(LLMRequest(
            messages=[{"role": "user", "content": "x"}], allowed_tools=["WebSearch"],
        ))
        assert "--add-dir" not in cap["cmd"]      # 取证等不涉本地文件:行为不变

    def test_llmrequest_jsonable_roundtrip_add_dirs(self):
        req = LLMRequest(messages=[{"role": "user", "content": "x"}],
                         allowed_tools=["Read"], add_dirs=["/a/b"])
        back = LLMRequest.from_jsonable(req.to_jsonable())
        assert back.add_dirs == ["/a/b"]          # AI task 内联投递不丢


class TestCodexCLIProvider:
    """codex-cli provider:非交互 JSONL 事件流 + final message 文件."""

    @pytest.mark.asyncio
    async def test_parses_jsonl_usage_and_final_file(self, monkeypatch, tmp_path):
        events = "\n".join([
            json.dumps({"type": "thread.started", "thread_id": "th_1"}),
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "fallback"}}),
            json.dumps({"type": "turn.completed", "usage": {
                "input_tokens": 100, "cached_input_tokens": 40,
                "output_tokens": 25, "reasoning_output_tokens": 5,
            }}),
        ])
        seen = {}

        class FakeProc:
            returncode = 0
            async def communicate(self, data=None):
                seen["stdin"] = data
                Path(seen["cmd"][seen["cmd"].index("-o") + 1]).write_text("FINAL", encoding="utf-8")
                return events.encode(), b""

        async def fake_exec(*cmd, **kwargs):
            seen["cmd"] = list(cmd)
            seen["env"] = kwargs.get("env") or {}
            return FakeProc()

        monkeypatch.setenv("FLORI_CODEX_TRACE_DIR", str(tmp_path))
        monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
        p = CodexCLIProvider(["codex", "exec"])
        resp = await p.complete(LLMRequest(messages=[{"role": "user", "content": "Q"}],
                                           system="S", model="claude-opus-4-8[1m]"))

        assert resp.content == "FINAL"
        assert resp.provider == "codex-cli" and resp.model == "claude-opus-4-8[1m]"
        assert resp.input_tokens == 100 and resp.output_tokens == 25
        assert resp.cache_read_input_tokens == 40 and resp.cached is True
        assert resp.cost_usd == 0.0 and resp.session_id == "th_1"
        assert resp.raw["source"] == "codex-jsonl"
        assert Path(resp.transcript_path).read_text(encoding="utf-8") == events
        cmd = seen["cmd"]
        assert cmd[:2] == ["codex", "exec"]
        assert "-c" in cmd and "approval_policy=never" in cmd
        assert "--ignore-user-config" in cmd and "--ignore-rules" in cmd
        assert "--skip-git-repo-check" in cmd and "--json" in cmd
        assert "default_permissions=flori-locked" in cmd
        assert cmd[-1] == "-"
        assert b"[System]\nS" in seen["stdin"] and b"[User]\nQ" in seen["stdin"]

    @pytest.mark.asyncio
    async def test_images_and_model_flags(self, monkeypatch, tmp_path):
        img = tmp_path / "f.png"
        img.write_bytes(b"x")
        seen = {}

        class FakeProc:
            returncode = 0
            async def communicate(self, data=None):
                Path(seen["cmd"][seen["cmd"].index("-o") + 1]).write_text("OK", encoding="utf-8")
                return b'{"type":"turn.completed","usage":{}}\n', b""

        async def fake_exec(*cmd, **kwargs):
            seen["cmd"] = list(cmd)
            return FakeProc()

        monkeypatch.setenv("FLORI_CODEX_TRACE_DIR", str(tmp_path / "runs"))
        monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
        p = CodexCLIProvider(["codex", "exec"], model="gpt-test")
        await p.complete(LLMRequest(messages=[{"role": "user", "content": "Q"}],
                                    images=[img], model="claude-opus-4-8[1m]"))
        cmd = seen["cmd"]
        assert "--image" in cmd and str(img.resolve()) in cmd
        assert "--model" in cmd and cmd[cmd.index("--model") + 1] == "claude-opus-4-8[1m]"

    @pytest.mark.asyncio
    async def test_read_allowed_tools_maps_to_readonly_sandbox(self, monkeypatch, tmp_path):
        """read 对等:allowed_tools=[Read] 不再拒绝,read-only 沙箱 + add_dirs 透传。"""
        seen = {}

        class FakeProc:
            returncode = 0
            async def communicate(self, data=None):
                seen["stdin"] = data
                Path(seen["cmd"][seen["cmd"].index("-o") + 1]).write_text("OK", encoding="utf-8")
                return b'{"type":"turn.completed","usage":{}}\n', b""

        async def fake_exec(*cmd, **kwargs):
            seen["cmd"] = list(cmd)
            return FakeProc()

        monkeypatch.setenv("FLORI_CODEX_TRACE_DIR", str(tmp_path))
        monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
        p = CodexCLIProvider(["codex", "exec"])
        resp = await p.complete(LLMRequest(
            messages=[{"role": "user", "content": "读 /a/b/source.pdf"}],
            allowed_tools=["Read"], add_dirs=["/a/b"],
        ))
        assert resp.provider == "codex-cli"
        cmd = seen["cmd"]
        assert "default_permissions=flori-locked" in cmd
        # codex 的 --add-dir 是可写目录,不拿它承载读授权,见 TestCodexSandboxIsolation。
        assert "--add-dir" not in cmd
        # 不越权:不开 workspace-write / danger-full-access / 绕沙箱开关。
        assert "workspace-write" not in cmd and "danger-full-access" not in cmd
        assert "--dangerously-bypass-approvals-and-sandbox" not in cmd
        # codex 无 Read 工具,提示语引导用读命令看文件。
        assert "读命令".encode() in seen["stdin"]

    @pytest.mark.asyncio
    async def test_websearch_maps_to_config_override(self, monkeypatch, tmp_path):
        """WebSearch 对等:映射服务端原生 web_search(-c web_search=live),
        它不在本地沙箱执行,read-only 断网约束管不到;沙箱与审批不因此放开。"""
        seen = {}

        class FakeProc:
            returncode = 0
            async def communicate(self, data=None):
                seen["stdin"] = data
                Path(seen["cmd"][seen["cmd"].index("-o") + 1]).write_text("OK", encoding="utf-8")
                return b'{"type":"turn.completed","usage":{}}\n', b""

        async def fake_exec(*cmd, **kwargs):
            seen["cmd"] = list(cmd)
            return FakeProc()

        monkeypatch.setenv("FLORI_CODEX_TRACE_DIR", str(tmp_path))
        monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
        p = CodexCLIProvider(["codex", "exec"])
        resp = await p.complete(LLMRequest(
            messages=[{"role": "user", "content": "搜处罚文号"}],
            allowed_tools=["WebSearch"],
        ))
        assert resp.provider == "codex-cli"
        cmd = seen["cmd"]
        i = cmd.index("web_search=live")
        assert cmd[i - 1] in ("-c", "--config")
        # 安全不变量:开 web search 不等于放开用户配置或沙箱。
        assert "--ignore-user-config" in cmd
        assert "default_permissions=flori-locked" in cmd and "--sandbox" not in cmd
        assert "--dangerously-bypass-approvals-and-sandbox" not in cmd
        # 纯搜索请求不注入读文件提示语。
        assert "读命令".encode() not in seen["stdin"]

    @pytest.mark.asyncio
    async def test_no_websearch_request_no_web_search_key(self, monkeypatch, tmp_path):
        """不带 WebSearch 时不得开 web_search:恒带 --ignore-user-config,宿主配置也漏不进来。"""
        seen = {}

        class FakeProc:
            returncode = 0
            async def communicate(self, data=None):
                Path(seen["cmd"][seen["cmd"].index("-o") + 1]).write_text("OK", encoding="utf-8")
                return b'{"type":"turn.completed","usage":{}}\n', b""

        async def fake_exec(*cmd, **kwargs):
            seen["cmd"] = list(cmd)
            return FakeProc()

        monkeypatch.setenv("FLORI_CODEX_TRACE_DIR", str(tmp_path))
        monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
        p = CodexCLIProvider(["codex", "exec"])
        await p.complete(LLMRequest(messages=[{"role": "user", "content": "Q"}]))
        assert not any(part.startswith("web_search=") for part in seen["cmd"])
        assert "--ignore-user-config" in seen["cmd"]

    @pytest.mark.asyncio
    async def test_template_pinned_web_search_respected(self, monkeypatch, tmp_path):
        seen = {}

        class FakeProc:
            returncode = 0
            async def communicate(self, data=None):
                Path(seen["cmd"][seen["cmd"].index("-o") + 1]).write_text("OK", encoding="utf-8")
                return b'{"type":"turn.completed","usage":{}}\n', b""

        async def fake_exec(*cmd, **kwargs):
            seen["cmd"] = list(cmd)
            return FakeProc()

        monkeypatch.setenv("FLORI_CODEX_TRACE_DIR", str(tmp_path))
        monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
        p = CodexCLIProvider(["codex", "exec", "-c", "web_search=cached"])
        await p.complete(LLMRequest(messages=[{"role": "user", "content": "Q"}],
                                    allowed_tools=["WebSearch", "Read"]))
        counts = [part for part in seen["cmd"] if part.startswith("web_search=")]
        assert counts == ["web_search=cached"]

    @pytest.mark.asyncio
    async def test_non_mapped_tools_fail_closed(self):
        """Bash 等本地执行的 Claude 工具语义仍不映射:静默降级会出假证据。"""
        p = CodexCLIProvider(["codex", "exec"])
        with pytest.raises(AIProviderError, match="unmapped tools.*Bash"):
            await p.complete(LLMRequest(messages=[{"role": "user", "content": "Q"}],
                                        allowed_tools=["WebSearch", "Bash", "Read"]))

    @pytest.mark.asyncio
    async def test_ignore_user_config_always_present(self, monkeypatch, tmp_path):
        """安全不变量回归锚:任何请求形态都必须隔离宿主 ~/.codex/config.toml。
        宿主配置常含 sandbox_mode danger-full-access 与 approval_policy never,一旦不隔离,
        这些会漏进 AI 执行链给模型生成的 shell 命令放开全权限。能力键(web_search 等)
        只允许经 -c 显式传入,任何人不得以简化冗余为由删除该 flag。"""
        seen = {}

        class FakeProc:
            returncode = 0
            async def communicate(self, data=None):
                Path(seen["cmd"][seen["cmd"].index("-o") + 1]).write_text("OK", encoding="utf-8")
                return b'{"type":"turn.completed","usage":{}}\n', b""

        async def fake_exec(*cmd, **kwargs):
            seen["cmd"] = list(cmd)
            return FakeProc()

        monkeypatch.setenv("FLORI_CODEX_TRACE_DIR", str(tmp_path))
        monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
        requests = [
            LLMRequest(messages=[{"role": "user", "content": "Q"}]),
            LLMRequest(messages=[{"role": "user", "content": "Q"}], allowed_tools=["Read"]),
            LLMRequest(messages=[{"role": "user", "content": "Q"}], allowed_tools=["WebSearch"]),
        ]
        for req in requests:
            await CodexCLIProvider(["codex", "exec"]).complete(req)
            assert seen["cmd"].count("--ignore-user-config") == 1, req.allowed_tools
        # 模板已带时不重复注入,flag 仍恰好一次。
        await CodexCLIProvider(["codex", "exec", "--ignore-user-config"]).complete(requests[0])
        assert seen["cmd"].count("--ignore-user-config") == 1

    @pytest.mark.asyncio
    async def test_rate_limit_error_carries_event_path(self, monkeypatch, tmp_path):
        seen = {}

        class FakeProc:
            returncode = 1
            async def communicate(self, data=None):
                return b'{"type":"error","message":"rate limit"}\n', b"429 rate limit"

        async def fake_exec(*cmd, **kwargs):
            seen["cmd"] = list(cmd)
            return FakeProc()

        monkeypatch.setenv("FLORI_CODEX_TRACE_DIR", str(tmp_path))
        monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
        p = CodexCLIProvider(["codex", "exec"])
        with pytest.raises(AIRateLimitError) as ei:
            await p.complete(LLMRequest(messages=[{"role": "user", "content": "Q"}]))
        assert Path(ei.value.transcript_path).is_file()


class TestCodexSandboxIsolation:
    """codex-cli 子进程隔离契约。

    边界按 codex 0.147.0 实测锚定,别照 claude 的心智模型推断:read-only 档位在 Linux 上是
    bubblewrap,整盘可读;要收窄读取只能改用 permissions 档案,而 --sandbox flag 会把档案
    整体顶掉。所以这里同时锚 deny 名单存在、--sandbox 不出现、写与出网仍全禁。
    mock 层锚的是"我们发出去的参数构成了这些约束",真实读写拒绝由容器/宿主实测覆盖。"""

    SECRETS = {
        "WORKER_TOKEN": "S_WORKERTOKEN",
        "FLORI_API_TOKEN": "S_APITOKEN",
        "MINIO_SECRET_KEY": "S_MINIOSECRET",
        "MINIO_ROOT_PASSWORD": "S_MINIOROOT",
        "REDIS_URL": "redis://:S_REDISPASS@redis-host/0",
        "ANTHROPIC_API_KEY": "S_ANTHROPICKEY",
        "OPENAI_API_KEY": "S_OPENAIKEY",
        "BILI_COOKIES": "S_BILICOOKIE",
        "RUNNER_REGISTRATION_TOKEN": "S_REGTOKEN",
    }

    @staticmethod
    def _capture(monkeypatch, tmp_path):
        seen: dict = {}

        class FakeProc:
            returncode = 0

            async def communicate(self, data=None):
                seen["stdin"] = (data or b"").decode()
                Path(seen["cmd"][seen["cmd"].index("-o") + 1]).write_text("OK", encoding="utf-8")
                return seen.get("events", b'{"type":"turn.completed","usage":{}}\n'), b""

        async def fake_exec(*cmd, **kwargs):
            seen["cmd"] = list(cmd)
            seen["env"] = dict(kwargs.get("env") or {})
            return FakeProc()

        monkeypatch.setenv("FLORI_CODEX_TRACE_DIR", str(tmp_path))
        monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
        return seen

    @staticmethod
    def _configs(cmd: list[str]) -> dict[str, str]:
        out = {}
        for i, part in enumerate(cmd[:-1]):
            if part in ("-c", "--config"):
                key, _, val = cmd[i + 1].partition("=")
                out[key] = val
        return out

    @pytest.mark.asyncio
    async def test_approval_goes_through_config_not_removed_flag(self, monkeypatch, tmp_path):
        """codex exec 没有 -a/--ask-for-approval。发出去会被参数解析判成 unexpected argument,
        rc=2,每次调用都失败。审批策略必须走 -c approval_policy。"""
        seen = self._capture(monkeypatch, tmp_path)
        await CodexCLIProvider(["codex", "exec"]).complete(
            LLMRequest(messages=[{"role": "user", "content": "Q"}]))
        assert "-a" not in seen["cmd"] and "--ask-for-approval" not in seen["cmd"]
        assert self._configs(seen["cmd"])["approval_policy"] == "never"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("tools", [None, ["Read"], ["WebSearch"], ["Read", "WebSearch"]])
    async def test_read_deny_profile_applied_and_sandbox_flag_absent(
            self, monkeypatch, tmp_path, tools):
        """--sandbox 一旦出现就会顶掉 permissions 档案,deny 名单被静默忽略。
        任何请求形态都必须是"无 --sandbox + 有 default_permissions"。"""
        seen = self._capture(monkeypatch, tmp_path)
        await CodexCLIProvider(["codex", "exec"]).complete(
            LLMRequest(messages=[{"role": "user", "content": "Q"}], allowed_tools=tools))
        cmd = seen["cmd"]
        assert "--sandbox" not in cmd and "-s" not in cmd
        cfg = self._configs(cmd)
        assert cfg["default_permissions"] == "flori-locked"
        fs = cfg["permissions.flori-locked.filesystem"]
        assert fs.startswith('{"/"="read"')

    @pytest.mark.asyncio
    async def test_deny_list_covers_credentials_and_skips_missing_paths(
            self, monkeypatch, tmp_path):
        """拒绝项要挂载点存在,指向不存在的路径会让整个沙箱构建失败,所以只列真实存在的。
        三个 CLI 的凭证根、worker token 与部署追加路径都要进名单。"""
        home = tmp_path / "home"
        (home / ".codex").mkdir(parents=True)
        (home / ".claude").mkdir(parents=True)
        token = tmp_path / "worker.token"
        token.write_text("t", encoding="utf-8")
        extra = tmp_path / "extra"
        extra.mkdir()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("WORKER_TOKEN_FILE", str(token))
        monkeypatch.setenv("FLORI_CODEX_DENY_READ", f"{extra}:/definitely/missing/xyz")
        monkeypatch.delenv("CODEX_HOME", raising=False)
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        monkeypatch.delenv("QODER_CONFIG_DIR", raising=False)
        seen = self._capture(monkeypatch, tmp_path)
        await CodexCLIProvider(["codex", "exec"]).complete(
            LLMRequest(messages=[{"role": "user", "content": "Q"}]))
        fs = self._configs(seen["cmd"])["permissions.flori-locked.filesystem"]
        for present in (home / ".codex", home / ".claude", token, extra):
            assert f'"{present}"="deny"' in fs
        # ~/.qoder 没建,不得进名单;不存在的部署追加路径同理。
        assert str(home / ".qoder") not in fs
        assert "/definitely/missing/xyz" not in fs

    @pytest.mark.asyncio
    @pytest.mark.parametrize("tools", [None, ["Read"]])
    async def test_add_dirs_never_become_write_grant(self, monkeypatch, tmp_path, tools):
        """codex 的 --add-dir 是额外可写目录,与 claude/qoder 的读授权语义相反。
        请求带 add_dirs 也不得转成写授权。"""
        seen = self._capture(monkeypatch, tmp_path)
        await CodexCLIProvider(["codex", "exec"]).complete(LLMRequest(
            messages=[{"role": "user", "content": "Q"}], allowed_tools=tools,
            add_dirs=["/", "/data/jobs/other-job"]))
        assert "--add-dir" not in seen["cmd"]

    @pytest.mark.asyncio
    async def test_shell_env_policy_and_strict_config_pinned(self, monkeypatch, tmp_path):
        """inherit=core 把 CODEX_HOME 与代理 URL 挡在模型 shell 的环境外;
        --strict-config 让写错的安全键报错而不是被静默忽略。"""
        seen = self._capture(monkeypatch, tmp_path)
        await CodexCLIProvider(["codex", "exec"]).complete(
            LLMRequest(messages=[{"role": "user", "content": "Q"}]))
        assert self._configs(seen["cmd"])["shell_environment_policy.inherit"] == "core"
        assert seen["cmd"].count("--strict-config") == 1

    @pytest.mark.asyncio
    async def test_cwd_is_fresh_empty_dir_per_call(self, monkeypatch, tmp_path):
        """工作根每次新建且为空,模型的相对路径操作碰不到任何既有产物。"""
        seen = self._capture(monkeypatch, tmp_path)
        p = CodexCLIProvider(["codex", "exec"])
        dirs = []
        for _ in range(2):
            await p.complete(LLMRequest(messages=[{"role": "user", "content": "Q"}]))
            cwd = Path(seen["cmd"][seen["cmd"].index("--cd") + 1])
            assert cwd.is_dir() and not list(cwd.iterdir())
            dirs.append(cwd)
        assert dirs[0] != dirs[1]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("tools", [None, ["Read"], ["WebSearch"]])
    async def test_worker_secrets_reach_neither_env_argv_nor_prompt(
            self, monkeypatch, tmp_path, tools):
        """恶意用例:worker 进程环境塞满 secrets,断言三条外泄通道都干净。
        env 由白名单拦下,argv 与 stdin prompt 本就不该出现 secret 字面量。"""
        for key, val in self.SECRETS.items():
            monkeypatch.setenv(key, val)
        monkeypatch.setenv("CODEX_HOME", "/home/worker/.codex")
        seen = self._capture(monkeypatch, tmp_path)
        await CodexCLIProvider(["codex", "exec"]).complete(LLMRequest(
            messages=[{"role": "user", "content": "Q"}], allowed_tools=tools))
        blob = "\n".join([*seen["cmd"], seen["stdin"], *seen["env"].values()])
        for key, val in self.SECRETS.items():
            assert val not in blob, key
            assert key not in seen["env"], key
        # codex 自己的配置根仍要给,否则 CLI 找不到凭证。
        assert seen["env"]["CODEX_HOME"] == "/home/worker/.codex"

    @pytest.mark.asyncio
    async def test_web_search_live_is_a_separate_layer_from_local_sandbox(
            self, monkeypatch, tmp_path):
        """web_search 在服务端执行,开它不等于本地沙箱松掉:同一条命令里
        deny 名单、无 --sandbox、写禁止三项一个都不能少,且查询里不夹带本地 secret。"""
        for key, val in self.SECRETS.items():
            monkeypatch.setenv(key, val)
        seen = self._capture(monkeypatch, tmp_path)
        await CodexCLIProvider(["codex", "exec"]).complete(LLMRequest(
            messages=[{"role": "user", "content": "Q"}], allowed_tools=["WebSearch"]))
        cfg = self._configs(seen["cmd"])
        assert cfg["web_search"] == "live"
        assert cfg["default_permissions"] == "flori-locked"
        assert "--sandbox" not in seen["cmd"]
        blob = "\n".join([*seen["cmd"], seen["stdin"], *seen["env"].values()])
        assert not any(v in blob for v in self.SECRETS.values())

    @pytest.mark.asyncio
    async def test_read_request_fails_closed_when_sandbox_cannot_start(
            self, monkeypatch, tmp_path):
        """沙箱起不来时 codex 仍以 rc=0 收尾,只是每条命令都带 bwrap 错误。
        请求要 Read 却一个文件都没读到,结论没有依据,必须失败而不是照收。"""
        seen = self._capture(monkeypatch, tmp_path)
        seen["events"] = (
            b'{"type":"item.completed","item":{"type":"command_execution",'
            b'"aggregated_output":"bwrap: No permissions to create a new namespace"}}\n'
            b'{"type":"turn.completed","usage":{}}\n'
        )
        with pytest.raises(AIProviderError, match="sandbox unavailable"):
            await CodexCLIProvider(["codex", "exec"]).complete(LLMRequest(
                messages=[{"role": "user", "content": "Q"}], allowed_tools=["Read"]))

    @pytest.mark.asyncio
    async def test_text_only_request_tolerates_sandbox_noise(self, monkeypatch, tmp_path):
        """纯文本请求不依赖本地读,沙箱噪声不构成证据缺失,不应把它判失败。"""
        seen = self._capture(monkeypatch, tmp_path)
        seen["events"] = (
            b'{"type":"item.completed","item":{"type":"command_execution",'
            b'"aggregated_output":"bwrap: No permissions to create a new namespace"}}\n'
            b'{"type":"turn.completed","usage":{}}\n'
        )
        resp = await CodexCLIProvider(["codex", "exec"]).complete(
            LLMRequest(messages=[{"role": "user", "content": "Q"}]))
        assert resp.provider == "codex-cli"

    @pytest.mark.parametrize("template", [
        ["codex", "exec", "-c", "notify=[\"/bin/sh\"]"],
        ["codex", "exec", "-c", "hooks.pre=x"],
        ["codex", "exec", "-c", "mcp_servers.evil.command=sh"],
        ["codex", "exec", "-c", "model_provider=evil"],
        ["codex", "exec", "-c", "model_providers.evil.base_url=http://evil"],
        ["codex", "exec", "-c", "chatgpt_base_url=http://evil"],
        ["codex", "exec", "-c", "permissions.x.filesystem={}"],
        ["codex", "exec", "-c", "default_permissions=wide"],
    ])
    def test_template_cannot_reach_out_of_sandbox_execution(self, template):
        """这些配置根都在沙箱外拿到执行或出网能力:notify 与 hooks 由 codex 自己 spawn,
        mcp_servers 是常驻子进程,provider 类键能把 prompt 与鉴权头引到任意端点,
        permissions 类键能改写拒绝名单。"""
        with pytest.raises(AIProviderError, match="安全配置键"):
            CodexCLIProvider(template)

    @pytest.mark.parametrize("template", [
        ["codex", "exec", "--enable", "some_feature"],
        ["codex", "exec", "--disable", "some_feature"],
        ["codex", "exec", "--dangerously-bypass-hook-trust"],
    ])
    def test_template_cannot_alias_around_config_review(self, template):
        """--enable/--disable 只是 -c features.<name>= 的别名,等于绕开 -c 键审查。"""
        with pytest.raises(AIProviderError, match="安全参数"):
            CodexCLIProvider(template)


# QoderCLIProvider: qodercli -p -o json(顶层 JSON 与 claude 同构)

class TestQoderCLIProvider:
    @pytest.mark.asyncio
    async def test_factory_dispatches_qoder_cli(self):
        gw = AIGateway(
            {"providers": {"qoder-cli": {"type": "qoder_cli", "command": ["qodercli", "-p"]}}},
            {"steps": []},
        )
        assert isinstance(gw._create_provider("qoder-cli"), QoderCLIProvider)

    @pytest.mark.asyncio
    async def test_parses_json_and_forces_zero_cost(self, monkeypatch):
        """包月订阅无按量成本:即便 CLI 回 total_cost_usd 也强制 0;token 用量照实。"""
        payload = {
            "result": "结构化笔记", "model": "Cantus", "total_cost_usd": 1.23,
            "total_credits": 2.2650132450000005,
            "num_turns": 2, "session_id": "sid-1", "subtype": "success", "is_error": False,
            "usage": {"input_tokens": 10, "output_tokens": 5,
                      "cache_creation_input_tokens": 3, "cache_read_input_tokens": 7},
        }
        seen = {}

        async def fake_exec(*a, **k):
            seen["cmd"] = list(a)
            return _FakeProc(json.dumps(payload).encode())

        monkeypatch.setattr("shared.ai_gateway.asyncio.create_subprocess_exec", fake_exec)
        p = QoderCLIProvider(command_template=["qodercli", "-p"], model="Cantus")
        r = await p.complete(LLMRequest(messages=[{"role": "user", "content": "x"}]))
        assert r.content == "结构化笔记" and r.provider == "qoder-cli"
        assert r.model == "Cantus" and r.session_id == "sid-1"
        assert r.input_tokens == 10 and r.output_tokens == 5
        assert r.cache_creation_input_tokens == 3 and r.cache_read_input_tokens == 7
        assert r.cost_usd == 0.0 and r.num_turns == 2 and r.cached is True
        assert r.credits == pytest.approx(2.2650132450000005)
        assert r.finish_reason == "success"
        cmd = seen["cmd"]
        assert cmd[cmd.index("-o") + 1] == "json"
        assert "--model" in cmd and cmd[cmd.index("--model") + 1] == "Cantus"

    @pytest.mark.asyncio
    async def test_slow_text_call_uses_step_level_budget(self, monkeypatch):
        """大论文纯文本生成不得被旧的 600 秒 provider 预算提前终止。"""
        import asyncio

        seen = []

        async def fake_exec(*_args, **_kwargs):
            return _FakeProc(json.dumps({"result": "ok", "usage": {}}).encode())

        original_wait_for = asyncio.wait_for

        async def capture_timeout(coro, timeout):
            seen.append(timeout)
            return await original_wait_for(coro, timeout=timeout)

        monkeypatch.setattr("shared.ai_gateway.asyncio.create_subprocess_exec", fake_exec)
        monkeypatch.setattr("shared.ai_gateway.asyncio.wait_for", capture_timeout)
        await QoderCLIProvider(["qodercli", "-p"]).complete(
            LLMRequest(messages=[{"role": "user", "content": "x"}]),
        )

        assert seen == [1800]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("raw_credits", "expected"),
        [
            (None, None),
            (0, 0.0),
            (-1, None),
            ("2.5", None),
            (float("inf"), None),
            (10**400, None),
            (True, None),
        ],
    )
    async def test_credits_are_nullable_and_fail_closed(
        self, monkeypatch, raw_credits, expected,
    ):
        payload = {"result": "ok", "usage": {}}
        if raw_credits is not None:
            payload["total_credits"] = raw_credits

        async def fake_exec(*_args, **_kwargs):
            return _FakeProc(json.dumps(payload).encode())

        monkeypatch.setattr("shared.ai_gateway.asyncio.create_subprocess_exec", fake_exec)
        response = await QoderCLIProvider(["qodercli", "-p"]).complete(
            LLMRequest(messages=[{"role": "user", "content": "x"}]),
        )

        assert response.credits == expected
        assert response.content == "ok"

    @pytest.mark.asyncio
    async def test_forces_json_replacing_template_output_format(self, monkeypatch):
        seen = {}

        async def fake_exec(*a, **k):
            seen["cmd"] = list(a)
            return _FakeProc(json.dumps({"result": "x", "usage": {}}).encode())

        monkeypatch.setattr("shared.ai_gateway.asyncio.create_subprocess_exec", fake_exec)
        p = QoderCLIProvider(["qodercli", "-p", "--output-format", "text"])
        await p.complete(LLMRequest(messages=[{"role": "user", "content": "x"}]))
        cmd = seen["cmd"]
        assert "text" not in cmd
        assert cmd[cmd.index("-o") + 1] == "json"

    @pytest.mark.asyncio
    async def test_text_only_disables_tools_without_max_turns(self, monkeypatch):
        """qodercli 无 --max-turns:纯文本靠 --tools "" 禁工具逼单轮,不能带不存在的 flag。"""
        cap = {}

        class FakeProc:
            returncode = 0
            async def communicate(self, data=None):
                cap["stdin"] = data
                return (b"OK", b"")

        async def fake_exec(*cmd, **kw):
            cap["cmd"] = list(cmd)
            return FakeProc()

        monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
        p = QoderCLIProvider(["qodercli", "-p", "{prompt_file}"])
        r = await p.complete(LLMRequest(messages=[{"role": "user", "content": "hello"}]))
        assert r.content == "OK"
        assert "{prompt_file}" not in cap["cmd"]
        assert "--max-turns" not in cap["cmd"]
        ti = cap["cmd"].index("--tools")
        assert cap["cmd"][ti + 1] == ""
        assert b"hello" in cap["stdin"]

    @pytest.mark.asyncio
    async def test_websearch_passthrough_allowed_tools(self, monkeypatch):
        """web search 对等:qodercli 内置工具表含 WebSearch,经 --allowed-tools 透传即生效。"""
        cap = {}

        class FakeProc:
            returncode = 0
            async def communicate(self, data=None):
                return (b"OK", b"")

        async def fake_exec(*cmd, **kw):
            cap["cmd"] = list(cmd)
            return FakeProc()

        monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
        p = QoderCLIProvider(["qodercli", "-p"])
        await p.complete(LLMRequest(messages=[{"role": "user", "content": "搜处罚文号"}],
                                    allowed_tools=["WebSearch"]))
        cmd = cap["cmd"]
        assert cmd[cmd.index("--allowed-tools") + 1] == "WebSearch"
        assert "--tools" not in cmd            # 不是禁工具档

    @pytest.mark.asyncio
    async def test_no_websearch_request_stays_tools_disabled(self, monkeypatch):
        cap = {}

        class FakeProc:
            returncode = 0
            async def communicate(self, data=None):
                return (b"OK", b"")

        async def fake_exec(*cmd, **kw):
            cap["cmd"] = list(cmd)
            return FakeProc()

        monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
        p = QoderCLIProvider(["qodercli", "-p"])
        await p.complete(LLMRequest(messages=[{"role": "user", "content": "x"}]))
        assert "--allowed-tools" not in cap["cmd"]
        assert cap["cmd"][cap["cmd"].index("--tools") + 1] == ""

    @pytest.mark.asyncio
    async def test_vision_appends_paths_and_read_tool(self, tmp_path, monkeypatch):
        img = tmp_path / "f1.jpg"; img.write_bytes(b"x")
        cap = {}

        class FakeProc:
            returncode = 0
            async def communicate(self, data=None):
                cap["stdin"] = data
                return (b"NOTE", b"")

        async def fake_exec(*cmd, **kw):
            cap["cmd"] = list(cmd)
            return FakeProc()

        monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
        p = QoderCLIProvider(["qodercli", "-p"])
        r = await p.complete(LLMRequest(messages=[{"role": "user", "content": "hi"}], images=[img]))
        assert r.content == "NOTE" and r.provider == "qoder-cli"
        assert str(img.resolve()).encode() in cap["stdin"]
        assert "--allowed-tools" in cap["cmd"] and "Read" in cap["cmd"]
        assert "--add-dir" in cap["cmd"] and str(tmp_path.resolve()) in cap["cmd"]
        assert "--tools" not in cap["cmd"]

    @pytest.mark.asyncio
    async def test_rate_limit_classified(self, monkeypatch):
        class FakeProc:
            returncode = 1
            async def communicate(self, data=None):
                return (b"", b"quota exceeded, limit reached")

        async def fake_exec(*cmd, **kw):
            return FakeProc()

        monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
        p = QoderCLIProvider(["qodercli", "-p"])
        with pytest.raises(AIRateLimitError):
            await p.complete(LLMRequest(messages=[{"role": "user", "content": "x"}]))

    @pytest.mark.asyncio
    async def test_fallback_non_json(self, monkeypatch):
        async def fake_exec(*a, **k):
            return _FakeProc(b"plain text, not json")

        monkeypatch.setattr("shared.ai_gateway.asyncio.create_subprocess_exec", fake_exec)
        p = QoderCLIProvider(["qodercli", "-p"])
        r = await p.complete(LLMRequest(messages=[{"role": "user", "content": "x"}]))
        assert r.content == "plain text, not json"
        assert r.input_tokens == 0 and r.cost_usd == 0.0


class TestConcreteCliReadiness:
    def test_cli_provider_ready_checks_codex_auth(self, monkeypatch, tmp_path):
        """codex 就绪判据与 worker 打 tag 同源:二进制在 且 CODEX_HOME/auth.json 非空。"""
        from shared.ai_gateway import cli_provider_ready

        monkeypatch.setenv("CODEX_HOME", str(tmp_path))
        with patch("shutil.which", lambda name: "/usr/bin/codex" if name == "codex" else None):
            assert cli_provider_ready("codex-cli") is False
            (tmp_path / "auth.json").write_text("{}", encoding="utf-8")
            assert cli_provider_ready("codex-cli") is True
        with patch("shutil.which", lambda name: None):
            assert cli_provider_ready("codex-cli") is False

# 推理档位 per-call 传递:请求级 > providers.yaml 默认 > 不传 flag(CLI 自定)

class TestReasoningEffort:
    @pytest.mark.asyncio
    async def test_claude_request_effort_maps_to_effort_flag(self, monkeypatch):
        seen = {}

        async def fake_exec(*cmd, **kw):
            seen["cmd"] = list(cmd)
            return _FakeProc(json.dumps({"result": "ok", "usage": {}}).encode())

        monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
        p = ClaudeCLIProvider(["claude", "-p"], reasoning_effort="medium")
        await p.complete(LLMRequest(messages=[{"role": "user", "content": "x"}],
                                    reasoning_effort="high"))
        cmd = seen["cmd"]
        assert cmd[cmd.index("--effort") + 1] == "high"   # 请求级压过 provider 默认

    @pytest.mark.asyncio
    async def test_claude_provider_default_effort_used_when_request_omits(self, monkeypatch):
        seen = {}

        async def fake_exec(*cmd, **kw):
            seen["cmd"] = list(cmd)
            return _FakeProc(json.dumps({"result": "ok", "usage": {}}).encode())

        monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
        p = ClaudeCLIProvider(["claude", "-p"], reasoning_effort="max")
        await p.complete(LLMRequest(messages=[{"role": "user", "content": "x"}]))
        cmd = seen["cmd"]
        assert cmd[cmd.index("--effort") + 1] == "max"

    @pytest.mark.asyncio
    async def test_claude_no_effort_configured_omits_flag(self, monkeypatch):
        seen = {}

        async def fake_exec(*cmd, **kw):
            seen["cmd"] = list(cmd)
            return _FakeProc(json.dumps({"result": "ok", "usage": {}}).encode())

        monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
        p = ClaudeCLIProvider(["claude", "-p"])
        await p.complete(LLMRequest(messages=[{"role": "user", "content": "x"}]))
        assert "--effort" not in seen["cmd"]

    @pytest.mark.asyncio
    async def test_qoder_effort_flag_and_template_pin(self, monkeypatch):
        seen = {}

        async def fake_exec(*cmd, **kw):
            seen["cmd"] = list(cmd)
            return _FakeProc(json.dumps({"result": "ok", "usage": {}}).encode())

        monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
        p = QoderCLIProvider(["qodercli", "-p"], reasoning_effort="max")
        await p.complete(LLMRequest(messages=[{"role": "user", "content": "x"}],
                                    reasoning_effort="low"))
        cmd = seen["cmd"]
        assert cmd[cmd.index("--reasoning-effort") + 1] == "low"

        # 模板钉死档位时尊重模板,不重复追加(旧配置升级路径)。
        p2 = QoderCLIProvider(["qodercli", "-p", "--reasoning-effort", "max"],
                              reasoning_effort="high")
        await p2.complete(LLMRequest(messages=[{"role": "user", "content": "x"}],
                                     reasoning_effort="low"))
        cmd2 = seen["cmd"]
        assert cmd2.count("--reasoning-effort") == 1
        assert cmd2[cmd2.index("--reasoning-effort") + 1] == "max"

    @pytest.mark.asyncio
    async def test_codex_effort_via_config_override(self, monkeypatch, tmp_path):
        seen = {}

        class FakeProc:
            returncode = 0
            async def communicate(self, data=None):
                Path(seen["cmd"][seen["cmd"].index("-o") + 1]).write_text("OK", encoding="utf-8")
                return b'{"type":"turn.completed","usage":{}}\n', b""

        async def fake_exec(*cmd, **kw):
            seen["cmd"] = list(cmd)
            return FakeProc()

        monkeypatch.setenv("FLORI_CODEX_TRACE_DIR", str(tmp_path))
        monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
        p = CodexCLIProvider(["codex", "exec"], reasoning_effort="medium")
        await p.complete(LLMRequest(messages=[{"role": "user", "content": "x"}],
                                    reasoning_effort="xhigh"))
        cmd = seen["cmd"]
        assert "model_reasoning_effort=xhigh" in cmd
        assert cmd[cmd.index("model_reasoning_effort=xhigh") - 1] == "-c"

        # 模板已用 -c 钉同键时尊重模板;其它 -c 键不拦截判断。
        p2 = CodexCLIProvider(
            ["codex", "exec", "-c", "model_reasoning_effort=high", "-c", "foo=bar"],
        )
        await p2.complete(LLMRequest(messages=[{"role": "user", "content": "x"}],
                                     reasoning_effort="low"))
        cmd2 = seen["cmd"]
        assert "model_reasoning_effort=high" in cmd2
        assert "model_reasoning_effort=low" not in cmd2

    def test_factory_passes_reasoning_effort_defaults(self):
        gw = AIGateway({"providers": {
            "claude-cli": {"type": "claude_cli", "command": ["claude", "-p"],
                           "reasoning_effort": "high"},
            "codex-cli": {"type": "codex_cli", "command": ["codex", "exec"],
                          "reasoning_effort": "medium"},
            "qoder-cli": {"type": "qoder_cli", "command": ["qodercli", "-p"],
                          "reasoning_effort": "max"},
        }}, {"steps": []})
        assert gw._create_provider("claude-cli")._reasoning_effort == "high"
        assert gw._create_provider("codex-cli")._reasoning_effort == "medium"
        assert gw._create_provider("qoder-cli")._reasoning_effort == "max"

    def test_llmrequest_jsonable_roundtrip_reasoning_effort(self):
        req = LLMRequest(messages=[{"role": "user", "content": "x"}],
                         reasoning_effort="xhigh")
        back = LLMRequest.from_jsonable(req.to_jsonable())
        assert back.reasoning_effort == "xhigh"
        assert LLMRequest.from_jsonable(
            LLMRequest(messages=[]).to_jsonable(),
        ).reasoning_effort is None


# 落地前的取值域复核:三个 CLI 对越界档位都不报错,只能在这里拒

_DOMAIN_PROVIDERS = {"providers": {
    "claude-cli": {
        "type": "claude_cli", "command": ["claude", "-p"],
        "model": "claude-opus-4-8[1m]", "models": ["claude-opus-4-8[1m]"],
        "reasoning_efforts": ["low", "medium", "high", "xhigh", "max"],
    },
    "codex-cli": {
        "type": "codex_cli", "command": ["codex", "exec"],
        "model": "gpt-5-codex", "models": ["gpt-5-codex"],
        "reasoning_efforts": ["low", "medium", "high", "xhigh"],
    },
    "qoder-cli": {
        "type": "qoder_cli", "command": ["qodercli", "-p"],
        "model": "Cantus", "models": ["Cantus"],
        "reasoning_efforts": ["low", "medium", "high", "xhigh", "max"],
    },
}}


def _domain_gateway(provider: str, model: str, step: str = "s") -> AIGateway:
    return AIGateway(_DOMAIN_PROVIDERS, {"steps": [
        {"name": step, "ai": {"primary": {"provider": provider, "model": model}}},
    ]})


class TestCliParamDomainFailClosed:
    @pytest.fixture(autouse=True)
    def _no_subprocess(self, monkeypatch):
        """越界参数必须在 spawn 之前拒:真跑起来就已经静默降级了。"""
        async def forbidden(*cmd, **kw):
            raise AssertionError(f"CLI 不应被启动: {cmd}")

        monkeypatch.setattr("shared.ai_gateway.asyncio.create_subprocess_exec", forbidden)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("provider", "model", "effort"), [
        ("claude-cli", "claude-opus-4-8[1m]", "turbo"),
        ("codex-cli", "gpt-5-codex", "max"),
        ("qoder-cli", "Cantus", "ultra"),
    ])
    async def test_request_effort_outside_domain_is_rejected(
        self, provider, model, effort,
    ):
        gw = _domain_gateway(provider, model)
        with pytest.raises(AllProvidersFailedError) as exc:
            await gw.call("s", LLMRequest(
                messages=[{"role": "user", "content": "x"}], reasoning_effort=effort,
            ))
        assert "reasoning_effort_not_in_provider_domain" in str(exc.value)
        assert provider in str(exc.value)

    @pytest.mark.asyncio
    async def test_provider_default_effort_outside_domain_is_rejected(self):
        providers = copy.deepcopy(_DOMAIN_PROVIDERS)
        providers["providers"]["qoder-cli"]["reasoning_effort"] = "turbo"
        gw = AIGateway(providers, {"steps": [
            {"name": "s", "ai": {"primary": {"provider": "qoder-cli", "model": "Cantus"}}},
        ]})

        with pytest.raises(AllProvidersFailedError) as exc:
            await gw.call("s", LLMRequest(messages=[{"role": "user", "content": "x"}]))
        assert "reasoning_effort_not_in_provider_domain" in str(exc.value)

    @pytest.mark.asyncio
    async def test_route_model_outside_domain_is_rejected(self):
        gw = _domain_gateway("qoder-cli", "claude-opus-4-8[1m]")
        with pytest.raises(AllProvidersFailedError) as exc:
            await gw.call("s", LLMRequest(messages=[{"role": "user", "content": "x"}]))
        assert "model_not_in_provider_domain" in str(exc.value)


class TestCliParamDomainAllows:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(("provider", "model", "effort"), [
        ("claude-cli", "claude-opus-4-8[1m]", "max"),
        ("codex-cli", "gpt-5-codex", "xhigh"),
        ("qoder-cli", "Cantus", "max"),
    ])
    async def test_effort_inside_domain_reaches_the_cli(
        self, monkeypatch, provider, model, effort,
    ):
        seen = {}

        async def fake_exec(*cmd, **kw):
            seen["cmd"] = list(cmd)
            return _FakeProc(json.dumps({"result": "ok", "usage": {}}).encode())

        monkeypatch.setattr("shared.ai_gateway.asyncio.create_subprocess_exec", fake_exec)
        gw = _domain_gateway(provider, model)
        response = await gw.call("s", LLMRequest(
            messages=[{"role": "user", "content": "x"}], reasoning_effort=effort,
        ))
        assert response.provider == provider
        assert effort in " ".join(seen["cmd"])

    @pytest.mark.asyncio
    async def test_provider_without_declared_model_domain_still_runs(self, monkeypatch):
        """模型取值域未声明时交 CLI 自己报错,不在这里拦掉默认链。"""
        async def fake_exec(*cmd, **kw):
            return _FakeProc(json.dumps({"result": "ok", "usage": {}}).encode())

        monkeypatch.setattr("shared.ai_gateway.asyncio.create_subprocess_exec", fake_exec)
        gw = AIGateway({"providers": {"claude-cli": {
            "type": "claude_cli", "command": ["claude", "-p"], "model": "whatever",
        }}}, {"steps": [
            {"name": "s", "ai": {"primary": {"provider": "claude-cli", "model": "whatever"}}},
        ]})
        response = await gw.call("s", LLMRequest(messages=[{"role": "user", "content": "x"}]))
        assert response.provider == "claude-cli"


# 安全矩阵 B:AI 子进程最小环境白名单 + command template 安全参数拒绝

_FLORI_SECRET_ENV = {
    "WORKER_REGISTRATION_TOKEN": "flw-secret",
    "MINIO_ACCESS_KEY": "minio-user",
    "MINIO_SECRET_KEY": "minio-secret",
    "API_TOKEN": "api-secret",
    "FLORI_SECRET_KEY": "fernet-secret",
    "REDIS_URL": "redis://internal:6379/0",
    "GATEWAY_URL": "https://internal-gateway",
    "DEEPSEEK_API_KEY": "ds-secret",
    "KIMI_API_KEY": "kimi-secret",
    "OPENAI_API_KEY": "oa-secret",
}


class TestCliSubprocessEnvWhitelist:
    @pytest.fixture(autouse=True)
    def _worker_env(self, monkeypatch):
        for key, val in _FLORI_SECRET_ENV.items():
            monkeypatch.setenv(key, val)
        monkeypatch.setenv("HOME", "/home/worker")
        monkeypatch.setenv("HTTPS_PROXY", "http://proxy:11081")
        monkeypatch.setenv("NO_PROXY", "redis,minio")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "claude-native-key")
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/home/worker/.claude")
        monkeypatch.setenv("CODEX_HOME", "/home/worker/.codex")
        monkeypatch.setenv("QODER_CONFIG_DIR", "/home/worker/.qoder")

    def test_whitelist_keeps_cli_essentials(self):
        from shared.ai_gateway import build_cli_env

        env = build_cli_env("claude-cli")
        assert env["PATH"] == os.environ["PATH"]
        assert env["HOME"] == "/home/worker"
        assert env["HTTPS_PROXY"] == "http://proxy:11081"
        assert env["NO_PROXY"] == "redis,minio"
        assert env["CLAUDE_CONFIG_DIR"] == "/home/worker/.claude"
        # claude 的原生鉴权路径保留(与就绪判据同源)。
        assert env["ANTHROPIC_API_KEY"] == "claude-native-key"

    @pytest.mark.parametrize("provider", ["claude-cli", "codex-cli", "qoder-cli"])
    def test_worker_secrets_never_inherited(self, provider):
        from shared.ai_gateway import build_cli_env

        env = build_cli_env(provider)
        for key in _FLORI_SECRET_ENV:
            assert key not in env, f"{key} leaked into {provider} subprocess env"

    def test_other_cli_does_not_get_claude_native_key(self):
        from shared.ai_gateway import build_cli_env

        assert "ANTHROPIC_API_KEY" not in build_cli_env("codex-cli")
        assert "ANTHROPIC_API_KEY" not in build_cli_env("qoder-cli")
        assert build_cli_env("codex-cli")["CODEX_HOME"] == "/home/worker/.codex"
        assert build_cli_env("qoder-cli")["QODER_CONFIG_DIR"] == "/home/worker/.qoder"

    def test_provider_config_env_still_applies(self):
        from shared.ai_gateway import build_cli_env

        env = build_cli_env("claude-cli", {"CUSTOM_FLAG": "1"})
        assert env["CUSTOM_FLAG"] == "1"

    @pytest.mark.asyncio
    async def test_claude_subprocess_receives_whitelisted_env(self, monkeypatch):
        seen = {}

        async def fake_exec(*cmd, **kw):
            seen["env"] = kw["env"]
            return _FakeProc(json.dumps({"result": "ok", "usage": {}}).encode())

        monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
        await ClaudeCLIProvider(["claude", "-p"]).complete(
            LLMRequest(messages=[{"role": "user", "content": "x"}]))
        assert "PATH" in seen["env"] and seen["env"]["HOME"] == "/home/worker"
        for key in _FLORI_SECRET_ENV:
            assert key not in seen["env"]

    @pytest.mark.asyncio
    async def test_codex_subprocess_receives_whitelisted_env(self, monkeypatch, tmp_path):
        seen = {}

        class FakeProc:
            returncode = 0
            async def communicate(self, data=None):
                Path(seen["cmd"][seen["cmd"].index("-o") + 1]).write_text("OK", encoding="utf-8")
                return b'{"type":"turn.completed","usage":{}}\n', b""

        async def fake_exec(*cmd, **kw):
            seen["cmd"] = list(cmd)
            seen["env"] = kw["env"]
            return FakeProc()

        monkeypatch.setenv("FLORI_CODEX_TRACE_DIR", str(tmp_path))
        monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
        await CodexCLIProvider(["codex", "exec"]).complete(
            LLMRequest(messages=[{"role": "user", "content": "x"}]))
        assert "PATH" in seen["env"] and seen["env"]["CODEX_HOME"] == "/home/worker/.codex"
        for key in _FLORI_SECRET_ENV:
            assert key not in seen["env"]
        assert "ANTHROPIC_API_KEY" not in seen["env"]

    @pytest.mark.asyncio
    async def test_qoder_subprocess_receives_whitelisted_env(self, monkeypatch):
        seen = {}

        async def fake_exec(*cmd, **kw):
            seen["env"] = kw["env"]
            return _FakeProc(json.dumps({"result": "ok", "usage": {}}).encode())

        monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
        await QoderCLIProvider(["qodercli", "-p"]).complete(
            LLMRequest(messages=[{"role": "user", "content": "x"}]))
        assert "PATH" in seen["env"]
        assert seen["env"]["QODER_CONFIG_DIR"] == "/home/worker/.qoder"
        for key in _FLORI_SECRET_ENV:
            assert key not in seen["env"]
        assert "ANTHROPIC_API_KEY" not in seen["env"]


class TestUnsafeTemplateRejection:
    """安全矩阵 B:模板预置沙箱/审批/权限/工具面/目录授权一律拒绝,不是尊重。"""

    @pytest.mark.parametrize("template", [
        ["claude", "-p", "--dangerously-skip-permissions"],
        ["claude", "-p", "--permission-mode", "bypassPermissions"],
        ["claude", "-p", "--permission-mode=bypassPermissions"],
        ["claude", "-p", "--allowedTools", "Bash"],
        ["claude", "-p", "--tools", "Bash"],
        ["claude", "-p", "--add-dir", "/"],
        ["claude", "-p", "--settings", "/tmp/perm.json"],
        ["claude", "-p", "--mcp-config", "/tmp/mcp.json"],
    ])
    def test_claude_template_injection_rejected(self, template):
        with pytest.raises(AIProviderError, match="安全参数"):
            ClaudeCLIProvider(template)

    @pytest.mark.parametrize("template", [
        ["qodercli", "-p", "--dangerously-skip-permissions"],
        ["qodercli", "-p", "--permission-mode", "acceptAll"],
        ["qodercli", "-p", "--allowed-tools", "Bash"],
        ["qodercli", "-p", "--tools", "Bash"],
        ["qodercli", "-p", "--add-dir", "/"],
    ])
    def test_qoder_template_injection_rejected(self, template):
        with pytest.raises(AIProviderError, match="安全参数"):
            QoderCLIProvider(template)

    @pytest.mark.parametrize("template", [
        ["codex", "exec", "-s", "danger-full-access"],
        ["codex", "exec", "--sandbox", "danger-full-access"],
        ["codex", "exec", "--dangerously-bypass-approvals-and-sandbox"],
        ["codex", "exec", "--yolo"],
        ["codex", "exec", "--full-auto"],
        ["codex", "exec", "-a", "on-request"],
        ["codex", "exec", "--ask-for-approval", "untrusted"],
        ["codex", "exec", "--profile", "danger"],
        ["codex", "exec", "--add-dir", "/"],
        ["codex", "exec", "--cd", "/"],
    ])
    def test_codex_template_flag_injection_rejected(self, template):
        with pytest.raises(AIProviderError, match="安全参数"):
            CodexCLIProvider(template)

    @pytest.mark.parametrize("template", [
        ["codex", "exec", "-c", "sandbox_mode=danger-full-access"],
        ["codex", "exec", "--config", "approval_policy=never"],
        ["codex", "exec", "--config=sandbox_mode=danger-full-access"],
        ["codex", "exec", "-c", "sandbox_workspace_write.network_access=true"],
        ["codex", "exec", "-c", "shell_environment_policy.inherit=all"],
    ])
    def test_codex_template_config_injection_rejected(self, template):
        with pytest.raises(AIProviderError, match="安全配置键"):
            CodexCLIProvider(template)

    def test_benign_templates_still_accepted(self):
        ClaudeCLIProvider(["claude", "-p", "--output-format", "text"])
        QoderCLIProvider(["qodercli", "-p", "--context-window", "1000000"])
        CodexCLIProvider(["codex", "exec", "-c", "model_reasoning_effort=high",
                          "-c", "web_search=live"])

    @pytest.mark.asyncio
    async def test_gateway_call_rejects_poisoned_template(self):
        # providers.yaml 被塞入危险模板时,该 tier 以清晰原因失败,不带毒执行。
        gw = AIGateway(
            {"providers": {"claude-cli": {
                "type": "claude_cli",
                "command": ["claude", "-p", "--dangerously-skip-permissions"],
            }}},
            {"steps": [{"name": "s", "ai": {
                "primary": {"provider": "claude-cli", "model": "m"},
            }}]},
        )
        with pytest.raises(AllProvidersFailedError, match="安全参数"):
            await gw.call("s", LLMRequest(messages=[{"role": "user", "content": "x"}]))


class TestResolvedProviderFeatureRecheck:
    """选定具体 CLI 后按配置 features 复核请求能力。"""

    def _claude_gw(self, features):
        return AIGateway(
            {"providers": {
                "claude-cli": {"type": "claude_cli", "command": ["claude", "-p"],
                               "model": "m", "features": features},
            }},
            {"steps": [{"name": "s", "ai": {
                "primary": {"provider": "claude-cli", "model": "m"},
            }}]},
        )

    @pytest.mark.asyncio
    async def test_missing_websearch_feature_fails_closed(self, monkeypatch):
        gw = self._claude_gw(["vision", "read"])
        with pytest.raises(AllProvidersFailedError, match="websearch"):
            await gw.call("s", LLMRequest(
                messages=[{"role": "user", "content": "x"}],
                allowed_tools=["WebSearch"],
            ))

    @pytest.mark.asyncio
    async def test_missing_vision_feature_fails_for_images(self, monkeypatch, tmp_path):
        img = tmp_path / "f.jpg"
        img.write_bytes(b"x")
        gw = self._claude_gw(["read", "websearch"])
        with pytest.raises(AllProvidersFailedError, match="vision"):
            await gw.call("s", LLMRequest(
                messages=[{"role": "user", "content": "x"}], images=[str(img)],
            ))

    @pytest.mark.asyncio
    async def test_direct_cli_tier_also_rechecked(self):
        gw = AIGateway(
            {"providers": {"claude-cli": {
                "type": "claude_cli", "command": ["claude", "-p"], "features": [],
            }}},
            {"steps": [{"name": "s", "ai": {
                "primary": {"provider": "claude-cli", "model": "m"},
            }}]},
        )
        with pytest.raises(AllProvidersFailedError, match="read"):
            await gw.call("s", LLMRequest(
                messages=[{"role": "user", "content": "x"}], allowed_tools=["Read"],
            ))

    @pytest.mark.asyncio
    async def test_enabled_features_pass(self, monkeypatch):
        async def fake_exec(*cmd, **kw):
            return _FakeProc(json.dumps({"result": "ok", "usage": {}}).encode())

        monkeypatch.setattr("shared.ai_gateway.asyncio.create_subprocess_exec", fake_exec)
        gw = self._claude_gw(["vision", "read", "websearch"])
        r = await gw.call("s", LLMRequest(
            messages=[{"role": "user", "content": "x"}], allowed_tools=["WebSearch"],
        ))
        assert r.provider == "claude-cli"
