"""tests for shared/ai_gateway.py"""

from datetime import datetime
from functools import partial
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.ai_gateway import (
    AIGateway,
    AnthropicProvider,
    ClaudeCLIProvider,
    DryRunProvider,
    OpenAICompatibleProvider,
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


class TestRetryPolicy:
    def test_rate_limit_long_backoff(self):
        from shared.errors import RETRY_POLICY, get_retry_delay
        # 限流：递增长退避，等订阅配额恢复（而非 90s 内烧完转终态）。
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


class TestProviderKeyFromEnv:
    """密钥脱敏后,_create_provider 按 {NAME}_API_KEY 约定从环境补齐。"""

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

        mock_resp = LLMResponse(content="ok", model="m", provider="p")

        async def mock_complete(self, request):
            return mock_resp

        gw._providers["mock_primary"] = type("P", (), {"complete": mock_complete})()
        resp = await gw.call("10_smart", LLMRequest(messages=[{"role": "user", "content": "test"}]))
        assert resp.content == "ok"

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
        assert captured_request["images"] == []

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

    @pytest.mark.asyncio
    async def test_compare_not_implemented(self, gateway_config):
        gw = AIGateway(*gateway_config)
        with pytest.raises(NotImplementedError):
            await gw.compare("10_smart", LLMRequest(messages=[]))


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


class TestAnthropicProvider:
    @pytest.mark.asyncio
    async def test_call_success(self):
        """Normal response returns text content."""
        provider = AnthropicProvider(api_key="sk-test")

        mock_usage = MagicMock()
        mock_usage.input_tokens = 100
        mock_usage.output_tokens = 50
        mock_usage.cache_read_input_tokens = 0

        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="Hello world")]
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

    @pytest.mark.asyncio
    async def test_rate_limit_raises(self):
        """Rate limit error should raise AIRateLimitError."""
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
        """Non-rate-limit error should raise AIProviderError."""
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
        """Normal response returns text content."""
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

    @pytest.mark.asyncio
    async def test_rate_limit_raises(self):
        """Rate limit error should raise AIRateLimitError."""
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
        """Successful CLI call returns stdout as content."""
        # sh -c 包裹:provider 追加的 --allowedTools/--max-turns 落到 $0/$1 被忽略,不污染输出。
        provider = ClaudeCLIProvider(
            command_template=["sh", "-c", "echo CLI output"]
        )
        req = LLMRequest(
            messages=[{"role": "user", "content": "hello"}],
            model="subscription",
        )
        resp = await provider.complete(req)
        assert resp.content == "CLI output"
        assert resp.provider == "claude-cli"
        assert resp.cost_usd == 0.0

    @pytest.mark.asyncio
    async def test_cli_failure_raises(self):
        """Non-zero exit code should raise AIProviderError."""
        provider = ClaudeCLIProvider(
            command_template=["false"]
        )
        req = LLMRequest(
            messages=[{"role": "user", "content": "hello"}],
            model="subscription",
        )
        with pytest.raises(AIProviderError):
            await provider.complete(req)

    @pytest.mark.asyncio
    async def test_cli_timeout_raises(self):
        """Timeout should kill process and raise AIProviderError."""
        provider = ClaudeCLIProvider(
            command_template=["sh", "-c", "sleep 999"]
        )
        import asyncio
        original_wait_for = asyncio.wait_for

        async def fast_timeout(coro, timeout):
            return await original_wait_for(coro, timeout=0.1)

        req = LLMRequest(
            messages=[{"role": "user", "content": "hello"}],
            model="subscription",
        )
        with patch("shared.ai_gateway.asyncio.wait_for", side_effect=fast_timeout):
            with pytest.raises(AIProviderError, match="timeout"):
                await provider.complete(req)


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
