"""AI provider 硬标签的单一投影与 rerun 角色。"""

import pytest

from shared.ai_routing import (
    READ_TOOL_TAG,
    ai_required_tags,
    parse_ai_override,
    pipeline_ai_roles,
    provider_required_tag,
    provider_required_tags,
    step_required_capability_tags,
    step_required_capability_tags_sync,
)
from shared.models import AITask, LLMRequest


@pytest.mark.parametrize(("provider", "tag"), [
    ("claude-cli", "claude-cli"), ("codex-cli", "codex-cli"),
    ("anthropic", "anthropic-api"), ("deepseek", "deepseek-api"),
    ("openai", "openai-api"), ("kimi", "kimi-api"), ("local", "local"),
])
def test_provider_projection(provider, tag):
    assert provider_required_tag(provider) == tag


def test_unknown_provider_fails_closed():
    with pytest.raises(ValueError, match="unknown"):
        provider_required_tag("typo-provider")


def test_all_executable_tiers_are_required_and_override_is_single():
    ai = {"primary": {"provider": "claude-cli"}, "fallback": {"provider": "openai"}}
    assert ai_required_tags(ai) == ["claude-cli", "openai-api"]
    assert ai_required_tags(ai, override="deepseek") == ["deepseek-api"]


def test_read_tool_capability_has_one_provider_routing_gate():
    providers = {"providers": {
        "claude-cli": {"type": "cli", "features": ["vision", READ_TOOL_TAG]},
        "openai": {"type": "openai", "features": ["vision"]},
    }}
    assert provider_required_tags(
        "claude-cli", providers, required_tags=[READ_TOOL_TAG],
    ) == ["claude-cli", READ_TOOL_TAG]
    with pytest.raises(ValueError, match="read"):
        provider_required_tags("openai", providers, required_tags=[READ_TOOL_TAG])
    with pytest.raises(ValueError, match="read"):
        ai_required_tags(
            {"primary": {"provider": "openai"}}, providers,
            required_tags=[READ_TOOL_TAG],
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(("nonempty", "expected"), [
    (set(), [READ_TOOL_TAG]),
    ({"output/original.md"}, []),
    ({"output/translated.md"}, []),
])
async def test_scheduler_and_step_capability_evaluators_are_identical(nonempty, expected):
    step = {"capability_rules": {
        READ_TOOL_TAG: {
            "unless_any_nonempty": [
                "output/translated.md", "output/original.md",
            ],
        },
    }}

    async def async_has(path):
        return path in nonempty

    assert await step_required_capability_tags(step, async_has) == expected
    assert step_required_capability_tags_sync(
        step, lambda path: path in nonempty,
    ) == expected


@pytest.mark.parametrize(("document", "reason"), [
    (None, "job_root_not_object"),
    ([], "job_root_not_object"),
    (False, "job_root_not_object"),
    (0, "job_root_not_object"),
    ("job", "job_root_not_object"),
    ({"ai_overrides": None}, "ai_overrides_not_object"),
    ({"ai_overrides": []}, "ai_overrides_not_object"),
    ({"ai_overrides": False}, "ai_overrides_not_object"),
    ({"ai_overrides": 0}, "ai_overrides_not_object"),
    ({"ai_overrides": "openai"}, "ai_overrides_not_object"),
    ({"ai_overrides": {"step": None}}, "step_override_not_nonempty_string"),
    ({"ai_overrides": {"step": []}}, "step_override_not_nonempty_string"),
    ({"ai_overrides": {"step": False}}, "step_override_not_nonempty_string"),
    ({"ai_overrides": {"step": 0}}, "step_override_not_nonempty_string"),
    ({"ai_overrides": {"step": {}}}, "step_override_not_nonempty_string"),
    ({"ai_overrides": {"step": "  "}}, "step_override_not_nonempty_string"),
])
def test_ai_override_parser_rejects_every_non_object_or_non_string_shape(document, reason):
    assert parse_ai_override(document, "step") == (None, reason)


def test_ai_override_parser_distinguishes_missing_and_normalizes_valid_value():
    assert parse_ai_override({}, "step") == (None, None)
    assert parse_ai_override({"ai_overrides": {}}, "step") == (None, None)
    assert parse_ai_override({"ai_overrides": {"step": " openai "}}, "step") == (
        "openai", None,
    )


def test_ai_override_parser_rejects_provider_missing_from_runtime_config():
    providers = {"providers": {"openai": {"type": "openai"}}}
    assert parse_ai_override(
        {"ai_overrides": {"step": "typo-provider"}}, "step", providers,
    ) == (None, "unknown_provider")
    assert parse_ai_override(
        {"ai_overrides": {"step": "openai"}}, "step", providers,
    ) == ("openai", None)


def test_ai_task_cannot_remove_provider_hard_gate():
    task = AITask(task_id="t", request=LLMRequest(messages=[]), provider="openai",
                  require_tags=["vision"])
    assert task.require_tags == ["openai-api", "vision"]


@pytest.mark.parametrize(("pipeline", "steps"), [
    ("video", ("11_smart", "12_review")),
    ("paper", ("05_smart_paper", "06_review")),
    ("article", ("04_smart_article", "06_review")),
    ("audio", ("04_smart_podcast", "05_review")),
])
def test_pipeline_rerun_roles(pipeline, steps):
    assert pipeline_ai_roles(pipeline) == steps
