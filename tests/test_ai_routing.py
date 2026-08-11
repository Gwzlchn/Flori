"""AI provider 硬标签的单一投影与 rerun 角色。"""

import pytest

from shared.ai_routing import (
    AI_PARAM_DOMAIN_NOT_STRING_LIST,
    AI_PARAM_EFFORT_DOMAIN_MISSING,
    AI_PARAM_EFFORT_NOT_ALLOWED,
    AI_PARAM_MODEL_DOMAIN_MISSING,
    AI_PARAM_MODEL_NOT_ALLOWED,
    AI_PARAM_REQUIRES_PROVIDER,
    AI_PARAM_UNKNOWN_PROVIDER,
    READ_TOOL_TAG,
    WEBSEARCH_TOOL_TAG,
    CONCRETE_CLI_PROVIDERS,
    allowed_providers_from_ai,
    ai_required_tags,
    parse_ai_override,
    parse_ai_param_override,
    pipeline_ai_roles,
    provider_required_tag,
    provider_required_tags,
    step_required_capability_tags,
    step_required_capability_tags_sync,
    step_required_route_tags,
    validate_ai_param_override,
    validate_job_ai_document,
    validate_provider_defaults,
    worker_satisfies_requirements,
)
from shared.models import AITask, LLMRequest


@pytest.mark.parametrize(("provider", "tag"), [
    ("claude-cli", "claude-cli"), ("codex-cli", "codex-cli"),
    ("qoder-cli", "qoder-cli"),
    ("anthropic", "anthropic-api"), ("deepseek", "deepseek-api"),
    ("openai", "openai-api"), ("kimi", "kimi-api"), ("local", "local"),
])
def test_provider_projection(provider, tag):
    assert provider_required_tag(provider) == tag


def test_unknown_provider_fails_closed():
    with pytest.raises(ValueError, match="unknown"):
        provider_required_tag("typo-provider")


def test_allowed_providers_are_or_route_not_and_tags():
    providers = {"providers": {
        name: {"type": ptype, "features": [READ_TOOL_TAG]}
        for name, ptype in zip(
            CONCRETE_CLI_PROVIDERS,
            ("claude_cli", "codex_cli", "qoder_cli"), strict=True,
        )
    }}
    ai = {"allowed_providers": list(CONCRETE_CLI_PROVIDERS)}
    assert ai_required_tags(ai, providers, required_tags=[READ_TOOL_TAG]) == [READ_TOOL_TAG]
    assert allowed_providers_from_ai(ai, providers) == CONCRETE_CLI_PROVIDERS


def test_qoder_cli_supports_read_capability_gate():
    providers = {"providers": {
        "qoder-cli": {"type": "qoder_cli", "features": ["vision", READ_TOOL_TAG]},
    }}
    assert provider_required_tags(
        "qoder-cli", providers, required_tags=[READ_TOOL_TAG],
    ) == ["qoder-cli", READ_TOOL_TAG]
    # 配置未启用 read 时仍 fail-closed,能力门控不因 CLI 类型放宽。
    with pytest.raises(ValueError, match="read"):
        provider_required_tags(
            "qoder-cli",
            {"providers": {"qoder-cli": {"type": "qoder_cli", "features": ["vision"]}}},
            required_tags=[READ_TOOL_TAG],
        )


def test_codex_cli_supports_read_capability_gate():
    """三 CLI 对等:codex 与 claude/qoder 走同一 read 门控。"""
    providers = {"providers": {
        "codex-cli": {"type": "codex_cli", "features": ["vision", READ_TOOL_TAG]},
    }}
    assert provider_required_tags(
        "codex-cli", providers, required_tags=[READ_TOOL_TAG],
    ) == ["codex-cli", READ_TOOL_TAG]
    with pytest.raises(ValueError, match="read"):
        provider_required_tags(
            "codex-cli",
            {"providers": {"codex-cli": {"type": "codex_cli", "features": ["vision"]}}},
            required_tags=[READ_TOOL_TAG],
        )


@pytest.mark.parametrize(("document", "reason"), [
    (3, "job_root_not_object"),
    ({"ai_param_overrides": []}, "ai_param_overrides_not_object"),
    ({"ai_param_overrides": {"A": None}}, "step_param_override_not_object"),
    ({"ai_param_overrides": {"A": "high"}}, "step_param_override_not_object"),
    ({"ai_param_overrides": {"A": {}}}, "step_param_override_not_object"),
    ({"ai_param_overrides": {"A": {"temperature": 1}}}, "unknown_param_override_key"),
    ({"ai_param_overrides": {"A": {"model": 3}}}, "param_override_not_nonempty_string"),
    ({"ai_param_overrides": {"A": {"reasoning_effort": " "}}},
     "param_override_not_nonempty_string"),
])
def test_ai_param_override_parser_rejects_bad_shapes(document, reason):
    assert parse_ai_param_override(document, "A") == ({}, reason)


def test_ai_param_override_parser_missing_and_valid():
    assert parse_ai_param_override({}, "A") == ({}, None)
    assert parse_ai_param_override({"ai_param_overrides": {"B": {"model": "m"}}}, "A") \
        == ({}, None)
    parsed, err = parse_ai_param_override(
        {"ai_param_overrides": {"A": {"model": " m1 ", "reasoning_effort": "high"}}}, "A",
    )
    assert err is None
    assert parsed == {"model": "m1", "reasoning_effort": "high"}


_THREE_CLI_PROVIDERS = {"providers": {
    "claude-cli": {
        "type": "claude_cli",
        "model": "claude-opus-5",
        "models": ["claude-opus-5", "claude-sonnet-4-6"],
        "reasoning_efforts": ["low", "medium", "high", "xhigh", "max"],
    },
    "codex-cli": {
        "type": "codex_cli",
        "model": "gpt-5.6-sol",
        "models": ["gpt-5.6-sol"],
        # codex 没有 max 档,这条差异是跨环境复核必须抓到的典型。
        "reasoning_efforts": ["low", "medium", "high", "xhigh"],
    },
    "qoder-cli": {
        "type": "qoder_cli",
        "model": "ultimate",
        "models": ["ultimate", "Ultimate"],
        "reasoning_effort": "max",
        "reasoning_efforts": ["low", "medium", "high", "xhigh", "max"],
    },
    "openai": {"type": "openai", "models": ["gpt-4o"]},
}}


@pytest.mark.parametrize(("provider", "params", "code"), [
    ("", {"model": "gpt-5.6-sol"}, AI_PARAM_REQUIRES_PROVIDER),
    ("", {"reasoning_effort": "high"}, AI_PARAM_REQUIRES_PROVIDER),
    ("ghost-cli", {"reasoning_effort": "high"}, AI_PARAM_UNKNOWN_PROVIDER),
    ("codex-cli", {"reasoning_effort": "max"}, AI_PARAM_EFFORT_NOT_ALLOWED),
    ("qoder-cli", {"reasoning_effort": "turbo"}, AI_PARAM_EFFORT_NOT_ALLOWED),
    ("claude-cli", {"model": "Cantus"}, AI_PARAM_MODEL_NOT_ALLOWED),
    ("qoder-cli", {"model": "claude-opus-4-8[1m]"}, AI_PARAM_MODEL_NOT_ALLOWED),
    ("openai", {"reasoning_effort": "high"}, AI_PARAM_EFFORT_DOMAIN_MISSING),
])
def test_param_override_rejections_are_stable_codes(provider, params, code):
    violation = validate_ai_param_override(provider, params, _THREE_CLI_PROVIDERS)
    assert violation is not None and violation.code == code
    assert code in violation.message()


@pytest.mark.parametrize(("provider", "params"), [
    ("claude-cli", {"model": "claude-sonnet-4-6", "reasoning_effort": "max"}),
    ("codex-cli", {"reasoning_effort": "xhigh"}),
    ("qoder-cli", {"model": "Ultimate", "reasoning_effort": "max"}),
    ("", {}),
])
def test_param_override_accepts_values_inside_target_domain(provider, params):
    assert validate_ai_param_override(
        provider, params, _THREE_CLI_PROVIDERS,
    ) is None


def test_param_override_reports_domain_and_location():
    violation = validate_ai_param_override(
        "codex-cli", {"reasoning_effort": "max"}, _THREE_CLI_PROVIDERS,
    )
    assert violation.provider == "codex-cli"
    assert violation.field == "reasoning_effort" and violation.value == "max"
    assert violation.allowed == ("low", "medium", "high", "xhigh")


def test_param_override_needs_declared_model_domain():
    providers = {"providers": {"bare-cli": {"type": "cli", "model": "x"}}}
    violation = validate_ai_param_override("bare-cli", {"model": "y"}, providers)
    assert violation.code == AI_PARAM_MODEL_DOMAIN_MISSING


@pytest.mark.parametrize("domain", [[], "high", ["", " "], [3], ["ok", 3]])
def test_param_override_rejects_unusable_declared_domain(domain):
    providers = {"providers": {"cli": {"type": "cli", "reasoning_efforts": domain}}}
    violation = validate_ai_param_override(
        "cli", {"reasoning_effort": "high"}, providers,
    )
    expected = (
        AI_PARAM_EFFORT_DOMAIN_MISSING if domain == []
        else AI_PARAM_DOMAIN_NOT_STRING_LIST
    )
    assert violation.code == expected


def test_job_ai_document_reports_every_offending_step():
    document = {
        "ai_overrides": {"11_smart": "codex-cli", "12_review": "ghost-cli"},
        "ai_param_overrides": {
            "11_smart": {"reasoning_effort": "max"},
            "12_review": {"reasoning_effort": "high"},
            "13_orphan": {"model": "Cantus"},
        },
    }

    found = {
        step: violation.code
        for step, violation in validate_job_ai_document(document, _THREE_CLI_PROVIDERS)
    }

    assert found == {
        "11_smart": AI_PARAM_EFFORT_NOT_ALLOWED,
        "12_review": AI_PARAM_UNKNOWN_PROVIDER,
        "13_orphan": AI_PARAM_REQUIRES_PROVIDER,
    }


def test_job_ai_document_accepts_legacy_document_without_param_overrides():
    document = {"ai_overrides": {"11_smart": "claude-cli"}, "parts": []}
    assert validate_job_ai_document(document, _THREE_CLI_PROVIDERS) == []


@pytest.mark.parametrize(("document", "code"), [
    ("not-a-document", "job_root_not_object"),
    ({"ai_overrides": []}, "ai_overrides_not_object"),
    ({"ai_param_overrides": 7}, "ai_param_overrides_not_object"),
    ({"ai_overrides": {"11_smart": ""}}, "step_override_not_nonempty_string"),
    ({"ai_overrides": {"11_smart": "claude-cli"},
      "ai_param_overrides": {"11_smart": {"temperature": "1"}}},
     "unknown_param_override_key"),
])
def test_job_ai_document_folds_shape_damage_into_violations(document, code):
    violations = validate_job_ai_document(document, _THREE_CLI_PROVIDERS)
    assert [item.code for _step, item in violations] == [code]


def test_provider_defaults_pass_for_shipped_shape():
    assert validate_provider_defaults(_THREE_CLI_PROVIDERS) == []


# provider 名必须用真实的三个 CLI:自定义 CLI 会先被 cli_provider_readiness_unprovable 拦下,
# 那样测的就不是这里要验的默认值自洽性了。
@pytest.mark.parametrize(("name", "entry", "code"), [
    ("qoder-cli", {"type": "qoder_cli", "reasoning_effort": "turbo",
      "reasoning_efforts": ["low", "max"]}, AI_PARAM_EFFORT_NOT_ALLOWED),
    ("claude-cli", {"type": "claude_cli", "reasoning_effort": "high"},
     AI_PARAM_EFFORT_DOMAIN_MISSING),
    ("claude-cli", {"type": "claude_cli", "model": "ghost", "models": ["real"]},
     AI_PARAM_MODEL_NOT_ALLOWED),
    ("claude-cli", {"type": "claude_cli", "models": "not-a-list"},
     AI_PARAM_DOMAIN_NOT_STRING_LIST),
])
def test_provider_defaults_catch_self_inconsistent_config(name, entry, code):
    violations = validate_provider_defaults({"providers": {name: entry}})
    assert [item.code for item in violations] == [code]


def test_source_root_worker_is_exclusive_to_same_root_tasks():
    worker = {
        "status": "idle",
        "admin_status": "",
        "pools": "cpu,io,ai",
        "tags": "source-root:zg-library",
    }

    assert not worker_satisfies_requirements(worker, "cpu", set())
    assert not worker_satisfies_requirements(worker, "cpu", {"net-global"})
    assert worker_satisfies_requirements(
        worker, "cpu", {"source-root:zg-library"},
    )


def test_all_executable_tiers_are_required_and_override_is_single():
    ai = {"primary": {"provider": "claude-cli"}, "fallback": {"provider": "openai"}}
    assert ai_required_tags(ai) == ["claude-cli", "openai-api"]
    assert ai_required_tags(ai, override="deepseek") == ["deepseek-api"]


def test_read_tool_capability_has_one_provider_routing_gate():
    providers = {"providers": {
        "claude-cli": {"type": "claude_cli", "features": ["vision", READ_TOOL_TAG]},
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


@pytest.mark.parametrize(("provider", "ptype"), [
    ("claude-cli", "claude_cli"), ("codex-cli", "codex_cli"),
    ("qoder-cli", "qoder_cli"),
])
def test_websearch_capability_gate_is_peer_across_clis(provider, ptype):
    """三 CLI 对等具备联网搜索,走同一 websearch 能力门;features 未启用时 fail-closed。"""
    providers = {"providers": {
        provider: {"type": ptype, "features": ["vision", READ_TOOL_TAG, WEBSEARCH_TOOL_TAG]},
    }}
    assert provider_required_tags(
        provider, providers, required_tags=[WEBSEARCH_TOOL_TAG],
    ) == sorted([provider, WEBSEARCH_TOOL_TAG])
    with pytest.raises(ValueError, match="websearch"):
        provider_required_tags(
            provider,
            {"providers": {provider: {"type": ptype, "features": ["vision", READ_TOOL_TAG]}}},
            required_tags=[WEBSEARCH_TOOL_TAG],
        )


def test_websearch_capability_rejected_for_api_provider():
    providers = {"providers": {"openai": {"type": "openai", "features": ["vision"]}}}
    with pytest.raises(ValueError, match="websearch"):
        provider_required_tags("openai", providers, required_tags=[WEBSEARCH_TOOL_TAG])


def test_step_static_websearch_tag_projects_capability_gate():
    """静态 websearch 是 AND 能力门,具体 CLI 留在独立 OR 集合。"""
    providers = {"providers": {
        "claude-cli": {"type": "claude_cli", "features": [WEBSEARCH_TOOL_TAG]},
        "codex-cli": {"type": "codex_cli", "features": [WEBSEARCH_TOOL_TAG]},
        "qoder-cli": {"type": "qoder_cli", "features": [WEBSEARCH_TOOL_TAG]},
        "openai": {"type": "openai", "features": ["vision"]},
    }}
    step = {
        "name": "10_evidence", "pool": "ai", "tags": [WEBSEARCH_TOOL_TAG],
        "ai": {"allowed_providers": list(CONCRETE_CLI_PROVIDERS)},
    }
    tags = step_required_route_tags(
        step, providers, source="upload", url="", net_steps=set(),
    )
    assert tags == [WEBSEARCH_TOOL_TAG]
    assert not {"claude-cli", "codex-cli", "qoder-cli"}.intersection(tags)
    with pytest.raises(ValueError, match="websearch"):
        step_required_route_tags(
            step, providers, source="upload", url="", net_steps=set(),
            override="openai",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(("nonempty", "expected"), [
    (set(), [READ_TOOL_TAG]),
    ({"input/source.html"}, []),
    ({"input/source.pdf"}, []),
])
async def test_scheduler_and_step_capability_evaluators_are_identical(nonempty, expected):
    step = {"capability_rules": {
        READ_TOOL_TAG: {
            "unless_any_nonempty": [
                "input/source.html", "input/source.pdf",
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


def test_ai_task_allowed_providers_do_not_become_and_tags():
    task = AITask(
        task_id="t", request=LLMRequest(messages=[]),
        allowed_providers=list(CONCRETE_CLI_PROVIDERS), require_tags=["vision"],
    )
    payload = task.to_task_payload()
    assert payload["allowed_providers"] == list(CONCRETE_CLI_PROVIDERS)
    assert payload["require_tags"] == ["vision"]
    assert "provider" not in payload and "model" not in payload


@pytest.mark.parametrize(("pipeline", "steps"), [
    ("video", ("11_smart", "11_semantic_attestation", "12_concepts", "12_review")),
    ("document", ("05_smart", "07_concepts", "08_review")),
    ("audio", ("04_smart_podcast", "04_semantic_attestation", "05_concepts", "05_review")),
])
def test_pipeline_rerun_roles(pipeline, steps):
    assert pipeline_ai_roles(pipeline) == steps

def test_effort_domain_is_taken_per_model_not_union():
    """档位域按有效模型取,不是全局并集。并集会放行 CLI 只能静默降级的组合,
    审计还会记下一个实际未生效的档位。"""
    import yaml
    from pathlib import Path

    from shared.ai_routing import validate_ai_param_override

    cfg = yaml.safe_load(
        (Path(__file__).parent.parent / "configs" / "providers.yaml").read_text(
            encoding="utf-8",
        ),
    )
    # 并集里有 ultra,但这两个模型不支持,必须拒。
    for model in ("gpt-5.4", "gpt-5.6-luna"):
        violation = validate_ai_param_override(
            "codex-cli", {"model": model, "reasoning_effort": "ultra"}, cfg,
        )
        assert violation is not None
        assert violation.code == "reasoning_effort_not_in_provider_domain"
    # 对照:支持的组合仍放行,证明不是一刀切拒绝。
    assert validate_ai_param_override(
        "codex-cli", {"model": "gpt-5.6-sol", "reasoning_effort": "ultra"}, cfg,
    ) is None
    assert validate_ai_param_override(
        "codex-cli", {"model": "gpt-5.6-luna", "reasoning_effort": "max"}, cfg,
    ) is None
    # 没覆盖 model 时按 provider 声明的默认模型取域,不回落并集。
    assert validate_ai_param_override(
        "codex-cli", {"reasoning_effort": "ultra"}, cfg,
    ) is None

def test_config_load_rejects_custom_cli_provider():
    """自定义 CLI 没有可达注册路径:cli_provider_ready 只认三个固定名, worker 自证不出标签,
    手填又被受保护集合拒绝。与其留一条走不通的死路, 不如在配置加载期就拒绝。"""
    from shared.ai_routing import validate_provider_defaults

    bad = {"providers": {"my-cli": {
        "type": "cli", "model": "m", "models": ["m"], "features": [],
    }}}
    codes = [v.code for v in validate_provider_defaults(bad)]
    assert "cli_provider_readiness_unprovable" in codes


def test_default_effort_is_checked_against_default_model_subdomain():
    """默认档位要按默认模型的子域校验。只查全局并集时,
    model=gpt-5.4 加 reasoning_effort=ultra 会正常启动, 直到每次执行才失败。"""
    import copy
    from pathlib import Path

    import yaml

    from shared.ai_routing import validate_provider_defaults

    cfg = yaml.safe_load(
        (Path(__file__).parent.parent / "configs" / "providers.yaml").read_text(
            encoding="utf-8",
        ),
    )
    bad = copy.deepcopy(cfg)
    bad["providers"]["codex-cli"]["model"] = "gpt-5.4"
    bad["providers"]["codex-cli"]["reasoning_effort"] = "ultra"
    codes = [v.code for v in validate_provider_defaults(bad)]
    assert "reasoning_effort_not_in_provider_domain" in codes
    # 对照:同一模型的合法默认档位不得被误拒。
    ok = copy.deepcopy(cfg)
    ok["providers"]["codex-cli"]["model"] = "gpt-5.4"
    ok["providers"]["codex-cli"]["reasoning_effort"] = "xhigh"
    assert validate_provider_defaults(ok) == []
