"""LiteLLM 价表:模型计费的单一来源(取代原硬编码 PRICING,后者降为拉取失败时的兜底)。

每天拉一份 LiteLLM 的 model_prices_and_context_window.json(扁平 dict,key=模型名,
单价均为 per-token,且自带 cache_creation/cache_read 单价),存 MinIO,用最新数据算成本。
其他供应商模型多变(kimi 2.7 / deepseek 4pro 等)无需写死,每天拉最新即可。

【计费在 api 侧做】api 有网 + MinIO;纯网关 worker 不直连 MinIO/Redis。流程:worker 报原始
token,api record_ai_usage 据本表填 cost(claude-cli 订阅路径用 CLI total_cost_usd,不经本表)。
未命中本表 / 拉取失败 → 调用方回退 ai_gateway.calc_cost(硬编码 PRICING)。
"""

from __future__ import annotations

import httpx

LITELLM_PRICING_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/"
    "model_prices_and_context_window.json"
)

# 我们 provider 名 → LiteLLM key 前缀。LiteLLM 当代 anthropic/openai/deepseek 多为裸键(claude-opus-4-8
# / gpt-4o / deepseek-v4-pro);kimi 在 LiteLLM 归 moonshot,模型键带 moonshot/ 前缀。
_PROVIDER_PREFIX = {"kimi": "moonshot", "deepseek": "deepseek", "openai": "openai", "anthropic": "anthropic"}

# LiteLLM 单价字段名(per-token)。
_F_IN = "input_cost_per_token"
_F_OUT = "output_cost_per_token"
_F_CC = "cache_creation_input_token_cost"
_F_CR = "cache_read_input_token_cost"


async def fetch_litellm_pricing(url: str = LITELLM_PRICING_URL, timeout: float = 90.0) -> dict:
    """拉 LiteLLM 价表(直连,不走代理)。剔除 sample_spec 示例项。调用方负责存 MinIO + 兜底处理异常。"""
    # trust_env=False:忽略 HTTP(S)_PROXY,直连 raw.githubusercontent(代理对 github 不稳,见运维规约)。
    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        table = resp.json()
    if isinstance(table, dict):
        table.pop("sample_spec", None)
    return table


def resolve_model_key(table: dict, provider: str, model: str) -> str | None:
    """我们的 (provider, model) → LiteLLM 表 key。试:裸键 → <provider 前缀>/<model>。未命中返回 None。"""
    if not model:
        return None
    if model in table:
        return model
    for pfx in {provider, _PROVIDER_PREFIX.get(provider, provider)}:
        if pfx:
            k = f"{pfx}/{model}"
            if k in table:
                return k
    return None


def cost_from_table(
    table: dict, provider: str, model: str,
    input_tokens: int, output_tokens: int,
    cache_creation_tokens: int = 0, cache_read_tokens: int = 0,
) -> float | None:
    """据 LiteLLM 表算成本(per-token 单价,缓存感知)。未命中返回 None(调用方回退硬编码 PRICING)。"""
    key = resolve_model_key(table, provider, model)
    if key is None:
        return None
    entry = table.get(key) or {}

    def _price(field: str) -> float:
        v = entry.get(field)
        return float(v) if isinstance(v, (int, float)) else 0.0

    return (
        input_tokens * _price(_F_IN)
        + output_tokens * _price(_F_OUT)
        + cache_creation_tokens * _price(_F_CC)
        + cache_read_tokens * _price(_F_CR)
    )
