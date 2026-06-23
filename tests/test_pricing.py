"""tests for shared/pricing.py(LiteLLM 价表)。夹具取自真实 LiteLLM JSON 结构(2026-06 拉取核对)。"""

import pytest

from shared.pricing import cost_from_table, fetch_litellm_pricing, resolve_model_key

# 真实结构样本(字段/数值对齐 raw.githubusercontent litellm model_prices_and_context_window.json)。
SAMPLE = {
    "sample_spec": {"input_cost_per_token": 0.0, "note": "schema example, must be dropped"},
    "claude-opus-4-8": {
        "input_cost_per_token": 5e-06, "output_cost_per_token": 2.5e-05,
        "cache_creation_input_token_cost": 6.25e-06, "cache_read_input_token_cost": 5e-07,
        "litellm_provider": "anthropic", "mode": "chat",
    },
    "deepseek-v4-pro": {
        "input_cost_per_token": 4.35e-07, "output_cost_per_token": 8.7e-07,
        "cache_creation_input_token_cost": 0.0, "cache_read_input_token_cost": 3.625e-09,
        "litellm_provider": "deepseek",
    },
    "moonshot/moonshot-v1-8k": {  # kimi → moonshot 前缀
        "input_cost_per_token": 2e-07, "output_cost_per_token": 2e-06,
        "litellm_provider": "moonshot",  # 无 cache 字段
    },
    "gpt-4o": {
        "input_cost_per_token": 2.5e-06, "output_cost_per_token": 1e-05,
        "cache_read_input_token_cost": 1.25e-06, "litellm_provider": "openai",
    },
}


class TestResolveModelKey:
    def test_bare_key_exact(self):
        assert resolve_model_key(SAMPLE, "anthropic", "claude-opus-4-8") == "claude-opus-4-8"
        assert resolve_model_key(SAMPLE, "deepseek", "deepseek-v4-pro") == "deepseek-v4-pro"
        assert resolve_model_key(SAMPLE, "openai", "gpt-4o") == "gpt-4o"

    def test_provider_prefixed(self):
        # 我们的 kimi/moonshot-v1-8k → LiteLLM moonshot/moonshot-v1-8k
        assert resolve_model_key(SAMPLE, "kimi", "moonshot-v1-8k") == "moonshot/moonshot-v1-8k"

    def test_miss(self):
        assert resolve_model_key(SAMPLE, "openai", "no-such-model") is None
        assert resolve_model_key(SAMPLE, "anthropic", "") is None


class TestCostFromTable:
    def test_input_output(self):
        # opus: 1M in + 1M out = 5e-6*1e6 + 2.5e-5*1e6 = 5 + 25 = 30
        c = cost_from_table(SAMPLE, "anthropic", "claude-opus-4-8", 1_000_000, 1_000_000)
        assert c == pytest.approx(30.0)

    def test_cache_aware(self):
        # opus 读缓存 1M = 5e-7*1e6 = 0.5;写缓存 1M = 6.25e-6*1e6 = 6.25
        cr = cost_from_table(SAMPLE, "anthropic", "claude-opus-4-8", 0, 0, cache_read_tokens=1_000_000)
        assert cr == pytest.approx(0.5)
        cc = cost_from_table(SAMPLE, "anthropic", "claude-opus-4-8", 0, 0, cache_creation_tokens=1_000_000)
        assert cc == pytest.approx(6.25)

    def test_missing_cache_fields_default_zero(self):
        # moonshot 无 cache 字段 → cache token 不计费,只算 in/out
        c = cost_from_table(SAMPLE, "kimi", "moonshot-v1-8k", 1_000_000, 0,
                            cache_read_tokens=999, cache_creation_tokens=999)
        assert c == pytest.approx(2e-07 * 1_000_000)  # =0.2,缓存项为 0

    def test_unknown_model_returns_none(self):
        # 未命中 → None(调用方回退硬编码 PRICING)
        assert cost_from_table(SAMPLE, "openai", "no-such", 100, 100) is None


@pytest.mark.asyncio
async def test_fetch_drops_sample_spec(monkeypatch):
    """fetch 必须剔除 sample_spec 示例项;trust_env=False 直连。"""
    import shared.pricing as pricing

    class _Resp:
        def raise_for_status(self): pass
        def json(self): return dict(SAMPLE)  # 含 sample_spec

    class _Client:
        def __init__(self, *a, **k):
            assert k.get("trust_env") is False  # 不走代理
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url): return _Resp()

    monkeypatch.setattr(pricing.httpx, "AsyncClient", _Client)
    table = await fetch_litellm_pricing()
    assert "sample_spec" not in table
    assert "claude-opus-4-8" in table
