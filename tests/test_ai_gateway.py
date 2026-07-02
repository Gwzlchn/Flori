"""shared/ai_gateway.py 测试:计价、重试策略、provider 回退链、CLI 用量解析。"""

import json
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from shared.ai_gateway import (
    AIGateway,
    AnthropicProvider,
    ClaudeCLIProvider,
    DryRunProvider,
    OpenAICompatibleProvider,
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
        # 限流:递增长退避,等订阅配额恢复(而非 90s 内烧完转终态)。
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
            model="subscription",
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
            model="subscription",
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
            model="subscription",
        )
        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec), \
             patch("shared.ai_gateway.asyncio.wait_for", side_effect=fast_timeout):
            with pytest.raises(AIProviderError, match="timeout"):
                await provider.complete(req)
        assert killed["n"] == 1                 # 超时路径确实 kill 了进程


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

    def test_falls_back_to_subscription(self):
        # 两种字段都没有 → 订阅占位。
        assert _extract_cli_model({}) == "subscription"
        assert _extract_cli_model({"model": "", "modelUsage": {}}) == "subscription"

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
    """默认模型 yaml 可配置:--model 优先级 = 步级 request.model(非 subscription 占位)> provider 默认 > 不传。"""

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
    async def test_provider_default_model_used_for_subscription_placeholder(self, monkeypatch):
        cap = {}
        p = self._cap_provider(monkeypatch, cap, model="claude-opus-4-8[1m]")
        await p.complete(LLMRequest(messages=[{"role": "user", "content": "x"}], model="subscription"))
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
        await p.complete(LLMRequest(messages=[{"role": "user", "content": "x"}], model="subscription"))
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
        await p.complete(LLMRequest(messages=[{"role": "user", "content": "x"}], model="subscription"))
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
        gw = AIGateway({"providers": {"claude-cli": {"type": "cli", "command": ["claude", "-p"]}}},
                       {"steps": [{"name": "s", "ai": {"primary": {"provider": "claude-cli",
                                                                    "model": "subscription"}}}]})
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
