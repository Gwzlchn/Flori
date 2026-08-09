"""三种 concrete CLI 的默认路由、原子认领与 Worker 绑定契约。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from shared.ai_routing import CONCRETE_CLI_PROVIDERS
from shared.config import resolve_env_vars
from shared.models import AITask, LLMRequest
from tests.conftest import make_fakeredis
from worker.worker import auto_discover_tags, materialize_claimed_ai_route


_PROVIDERS_YAML = (
    Path(__file__).parent.parent / "configs" / "providers.yaml"
).read_text(encoding="utf-8")


def _real_providers() -> dict:
    return yaml.safe_load(resolve_env_vars(_PROVIDERS_YAML))


@pytest.fixture
async def redis():
    client = make_fakeredis()
    yield client
    await client.close()


def test_release_config_has_only_three_concrete_cli_defaults():
    providers = _real_providers()["providers"]
    assert {
        name: (providers[name]["type"], providers[name]["model"], providers[name]["reasoning_effort"])
        for name in CONCRETE_CLI_PROVIDERS
    } == {
        "claude-cli": ("claude_cli", "opus5", "xhigh"),
        "codex-cli": ("codex_cli", "gpt-5.6-sol", "xhigh"),
        "qoder-cli": ("qoder_cli", "ultimate", "max"),
    }


@pytest.mark.parametrize(("provider", "model"), [
    ("claude-cli", "opus5"),
    ("codex-cli", "gpt-5.6-sol"),
    ("qoder-cli", "ultimate"),
])
def test_claim_materializes_provider_default_model(provider, model):
    route = materialize_claimed_ai_route(
        _real_providers(), provider,
        allowed_providers=list(CONCRETE_CLI_PROVIDERS),
    )
    assert route == {"primary": {
        "provider": provider,
        "model": model,
        "provider_source": "claim",
    }}


@pytest.mark.parametrize("provider", CONCRETE_CLI_PROVIDERS)
def test_worker_registers_only_explicitly_bound_cli(provider, monkeypatch):
    monkeypatch.setenv("FLORI_CLI_PROVIDER", provider)
    with patch("shared.ai_gateway.cli_provider_ready", return_value=True), patch(
        "worker.worker._probe_net_zones", return_value=set(),
    ):
        tags = auto_discover_tags(_real_providers(), provider)
    assert set(CONCRETE_CLI_PROVIDERS).intersection(tags) == {provider}


@pytest.mark.asyncio
async def test_ai_any_of_claim_materializes_one_concrete_provider(redis):
    payload = AITask(
        task_id="at_any", request=LLMRequest(messages=[]),
        allowed_providers=list(CONCRETE_CLI_PROVIDERS),
    ).to_task_payload()
    await redis.enqueue_ai_task(payload)

    assert await redis.claim_ai_task(
        worker_id="worker-ambiguous",
        tags={"claude-cli", "qoder-cli"},
    ) is None
    claim = await redis.claim_ai_task(
        worker_id="worker-qoder", tags={"qoder-cli"},
    )
    assert claim["provider"] == "qoder-cli"
    assert claim["allowed_providers"] == list(CONCRETE_CLI_PROVIDERS)
    assert "model" not in claim


@pytest.mark.asyncio
async def test_preexecution_requeue_can_bind_a_different_provider(redis):
    payload = AITask(
        task_id="at_retry", request=LLMRequest(messages=[]),
        allowed_providers=list(CONCRETE_CLI_PROVIDERS),
    ).to_task_payload()
    await redis.enqueue_ai_task(payload)
    first = await redis.claim_ai_task(
        worker_id="worker-qoder", tags={"qoder-cli"},
        lease_seconds=10, now_epoch=10,
    )
    assert first["provider"] == "qoder-cli"
    assert await redis.reconcile_ai_task_claims(now_epoch=21) == [
        {"task_id": "at_retry", "action": "requeued"},
    ]
    second = await redis.claim_ai_task(
        worker_id="worker-claude", tags={"claude-cli"},
        lease_seconds=10, now_epoch=22,
    )
    assert second["provider"] == "claude-cli"


@pytest.mark.asyncio
async def test_pipeline_any_of_claim_is_atomic_and_concrete(redis):
    await redis.init_job("job-any", "video", {})
    await redis.set_step_status("job-any", "11_smart", "ready")
    await redis.enqueue_step(
        "ai", "job-any", "11_smart", [], 0,
        allowed_providers=list(CONCRETE_CLI_PROVIDERS),
    )
    claim = await redis.claim_pipeline_step_atomic(
        pool="ai", worker_id="worker-codex", exec_id="exec-codex",
        default_limit=2, tags={"codex-cli"}, reject_tags=set(),
    )
    assert claim["provider"] == "codex-cli"
    assert claim["allowed_providers"] == list(CONCRETE_CLI_PROVIDERS)


@pytest.mark.asyncio
async def test_missing_provider_route_fails_closed(redis):
    payload = {"kind": "ai", "task_id": "at-no-route", "request": {}}
    await redis.enqueue_ai_task(payload)
    assert await redis.claim_ai_task(
        worker_id="worker-claude", tags={"claude-cli"},
    ) is None
