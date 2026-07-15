"""AI provider 的硬路由标签与各内容链智能步骤角色。"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any, Awaitable, Callable

from .errors import InputInvalidError
from .net_zone import required_zone


_KNOWN_API_PROVIDERS = {"anthropic", "deepseek", "kimi", "openai"}
_CLI_PROVIDERS = {"claude-cli", "codex-cli"}
READ_TOOL_TAG = "read"
_ROUTING_CAPABILITIES = {READ_TOOL_TAG}
_PROVIDER_RUNTIME_CAPABILITIES = {"claude-cli": {READ_TOOL_TAG}}

# rerun-smart 只消费这一份角色映射,避免 API 为 video 写死步骤名。
PIPELINE_AI_ROLES: dict[str, tuple[str, str]] = {
    "video": ("11_smart", "12_review"),
    "paper": ("05_smart_paper", "06_review"),
    "article": ("04_smart_article", "06_review"),
    "audio": ("04_smart_podcast", "05_review"),
}


class InvalidAIOverrideError(InputInvalidError):
    """job.json 的 AI override 不可信时阻止任务继续路由。"""


def provider_is_configured(provider: str, providers_config: dict | None) -> bool:
    """provider 必须存在于当前运行配置,不能仅凭内置名称视为可用。"""
    providers = (providers_config or {}).get("providers")
    return isinstance(providers, dict) and isinstance(providers.get(provider), dict)


def parse_ai_override(
    document: Any,
    step_name: str,
    providers_config: dict | None = None,
) -> tuple[str | None, str | None]:
    """从 job.json 取单步 provider;异常形状返回原因且绝不透传原值。"""
    if not isinstance(document, dict):
        return None, "job_root_not_object"
    if "ai_overrides" not in document:
        return None, None
    overrides = document.get("ai_overrides")
    if not isinstance(overrides, dict):
        return None, "ai_overrides_not_object"
    if step_name not in overrides:
        return None, None
    value = overrides.get(step_name)
    if type(value) is not str or not value.strip():
        return None, "step_override_not_nonempty_string"
    provider = value.strip()
    if providers_config is not None:
        if not provider_is_configured(provider, providers_config):
            return None, "unknown_provider"
        try:
            provider_required_tag(provider, providers_config)
        except ValueError:
            return None, "unknown_provider"
    return provider, None


def worker_satisfies_requirements(
    worker: Any,
    pool: str,
    required_tags: set[str] | list[str] | tuple[str, ...],
) -> bool:
    """按 scheduler 的活 worker 口径检查 pool 与全部硬标签。"""
    if not isinstance(worker, dict):
        return False
    if worker.get("admin_status") == "paused" or worker.get("status") in {"paused", "offline", "stale"}:
        return False
    pools_raw = worker.get("pools")
    tags_raw = worker.get("tags", "")
    if not isinstance(pools_raw, str) or not isinstance(tags_raw, str):
        return False
    pools = {part.strip() for part in pools_raw.split(",") if part.strip()}
    tags = {part.strip() for part in tags_raw.split(",") if part.strip()}
    return pool in pools and set(required_tags).issubset(tags)


def provider_required_tag(provider: str, providers_config: dict | None = None) -> str:
    """把 provider 投影成唯一硬标签;未知 provider fail-closed。"""
    name = (provider or "").strip()
    if name in _CLI_PROVIDERS:
        return name
    if name == "local":
        return "local"
    if name in _KNOWN_API_PROVIDERS:
        return f"{name}-api"

    configured = (providers_config or {}).get("providers", {}).get(name)
    if isinstance(configured, dict):
        kind = configured.get("type")
        if kind in {"cli", "codex_cli"}:
            # 自定义 CLI 必须显式使用 provider 名作为 worker 标签。
            return name
        if kind in {"api", "openai", "anthropic", "openai_compatible"}:
            return f"{name}-api"
    raise ValueError(f"unknown AI provider: {name or '<empty>'}")


def provider_capability_tags(
    provider: str, providers_config: dict | None = None,
) -> set[str]:
    """返回 provider 运行时真实支持且配置启用的路由能力。"""
    name = (provider or "").strip()
    supported = set(_PROVIDER_RUNTIME_CAPABILITIES.get(name, set()))
    if providers_config is None:
        return supported
    if not isinstance(providers_config, dict):
        return set()
    configured = (providers_config.get("providers") or {}).get(name)
    if not isinstance(configured, dict):
        return set()
    features = configured.get("features")
    if not isinstance(features, list) or not all(type(item) is str for item in features):
        return set()
    return supported.intersection(features)


def provider_required_tags(
    provider: str,
    providers_config: dict | None = None,
    *,
    required_tags: set[str] | list[str] | tuple[str, ...] = (),
) -> list[str]:
    """投影 provider 标签并校验条件能力;不支持的能力 fail-closed。"""
    if not all(type(tag) is str and tag in _ROUTING_CAPABILITIES for tag in required_tags):
        raise ValueError("unknown AI provider capability")
    capabilities = set(required_tags)
    missing = capabilities - provider_capability_tags(provider, providers_config)
    if missing:
        raise ValueError(
            f"provider '{provider}' does not support {','.join(sorted(missing))}",
        )
    return sorted({provider_required_tag(provider, providers_config), *capabilities})


def ai_required_tags(
    ai: dict | None,
    providers_config: dict | None = None,
    *,
    override: str | None = None,
    required_tags: set[str] | list[str] | tuple[str, ...] = (),
) -> list[str]:
    """返回本步所有可执行 tier 的 provider 硬标签;override 存在时只投影该 provider。"""
    if override:
        return provider_required_tags(
            override, providers_config, required_tags=required_tags,
        )
    tags: set[str] = set()
    for tier in (ai if isinstance(ai, dict) else {}).values():
        if not isinstance(tier, dict) or not tier.get("provider"):
            continue
        tags.update(provider_required_tags(
            str(tier["provider"]), providers_config, required_tags=required_tags,
        ))
    if required_tags and not tags:
        raise ValueError("AI capability has no configured provider")
    return sorted(tags)


def step_required_route_tags(
    step: dict,
    providers_config: dict | None,
    *,
    source: str,
    url: str,
    net_steps: set[str],
    override: str | None = None,
    capability_tags: set[str] | list[str] | tuple[str, ...] = (),
) -> list[str]:
    """投影 scheduler/API 共用的 static/provider/net 硬标签。"""
    required = {str(tag) for tag in step.get("tags") or [] if tag}
    if step.get("pool") == "ai":
        ai_capabilities = set(capability_tags)
        if READ_TOOL_TAG in required:
            ai_capabilities.add(READ_TOOL_TAG)
        required.update(ai_required_tags(
            step.get("ai"), providers_config, override=override,
            required_tags=sorted(ai_capabilities),
        ))
    if source != "upload" and step.get("name") in net_steps:
        required.add(required_zone(source, url))
    return sorted(required)


def step_task_tags(
    step: dict,
    *,
    domain: str,
    style_tags: list[str],
    required_tags: set[str] | list[str] | tuple[str, ...],
) -> list[str]:
    """投影 claim reject_tags 使用的任务标签,保持与 enqueue 完全同源。"""
    tags = {str(tag) for tag in step.get("tags") or [] if tag}
    if step.get("pool") == "ai":
        tags.update(tag for tag in [domain, *style_tags] if tag)
    tags.update(set(required_tags).intersection({"net-cn", "net-global"}))
    return sorted(tags)


def _step_capability_rules(step: dict) -> dict[str, tuple[str, ...]]:
    """解析唯一 capability_rules schema,供调度端与执行端共用。"""
    rules = step.get("capability_rules")
    if rules is None:
        return {}
    if not isinstance(rules, dict):
        raise ValueError("capability_rules must be an object")
    parsed: dict[str, tuple[str, ...]] = {}
    for capability, rule in rules.items():
        if type(capability) is not str or capability not in _ROUTING_CAPABILITIES:
            raise ValueError("unknown step capability")
        if not isinstance(rule, dict) or set(rule) != {"unless_any_nonempty"}:
            raise ValueError(f"invalid {capability} capability rule")
        paths = rule["unless_any_nonempty"]
        if not isinstance(paths, list) or not paths:
            raise ValueError(f"invalid {capability} capability paths")
        for path in paths:
            if type(path) is not str or not path or path.startswith("/"):
                raise ValueError(f"invalid {capability} capability path")
            parts = PurePosixPath(path).parts
            if not parts or ".." in parts or "." in parts:
                raise ValueError(f"invalid {capability} capability path")
        parsed[capability] = tuple(paths)
    return parsed


def _required_capabilities(
    rules: dict[str, tuple[str, ...]], nonempty_paths: set[str],
) -> list[str]:
    return sorted(
        capability for capability, paths in rules.items()
        if not any(path in nonempty_paths for path in paths)
    )


def step_required_capability_tags_sync(
    step: dict,
    has_nonempty_artifact: Callable[[str], bool],
) -> list[str]:
    """执行端按本地实际产物重算条件能力。"""
    rules = _step_capability_rules(step)
    nonempty = {
        path for paths in rules.values() for path in paths
        if has_nonempty_artifact(path)
    }
    return _required_capabilities(rules, nonempty)


async def step_required_capability_tags(
    step: dict,
    has_nonempty_artifact: Callable[[str], Awaitable[bool]],
) -> list[str]:
    """调度端按中心存储实际产物重算条件能力。"""
    rules = _step_capability_rules(step)
    nonempty: set[str] = set()
    for path in sorted({path for paths in rules.values() for path in paths}):
        if await has_nonempty_artifact(path):
            nonempty.add(path)
    return _required_capabilities(rules, nonempty)


def pipeline_ai_roles(pipeline: str) -> tuple[str, str]:
    """取内容链的智能笔记与评审步骤;未知 pipeline 不猜测。"""
    try:
        return PIPELINE_AI_ROLES[pipeline]
    except KeyError as exc:
        raise ValueError(f"pipeline '{pipeline}' has no smart/review roles") from exc
