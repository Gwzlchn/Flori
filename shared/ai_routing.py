"""AI provider 的硬路由标签与各内容链智能步骤角色。"""

from __future__ import annotations

import os

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Awaitable, Callable, Mapping

from .errors import InputInvalidError
from .net_zone import required_zone


_KNOWN_API_PROVIDERS = {"anthropic", "deepseek", "kimi", "openai"}
CONCRETE_CLI_PROVIDERS = ("claude-cli", "codex-cli", "qoder-cli")
CLI_PROVIDER_TYPES_BY_NAME = {
    "claude-cli": "claude_cli",
    "codex-cli": "codex_cli",
    "qoder-cli": "qoder_cli",
}
CLI_PROVIDER_TYPES = frozenset(CLI_PROVIDER_TYPES_BY_NAME.values())
_CLI_PROVIDERS = frozenset(CONCRETE_CLI_PROVIDERS)
READ_TOOL_TAG = "read"
# websearch = 联网搜索能力,命名对齐工具语义(WebSearch)而非某个 CLI 的开关名。
# 三个 CLI 的实现机制各异:claude 内置 WebSearch 工具;codex 走 Responses API 服务端
# 原生 web_search(不受本地 read-only 沙箱限制);qoder 内置工具表含 WebSearch。
# 步骤按此能力标签路由,不钉死 provider;某 CLI 失去该能力时从其条目移除即 fail-closed。
WEBSEARCH_TOOL_TAG = "websearch"
_ROUTING_CAPABILITIES = {READ_TOOL_TAG, WEBSEARCH_TOOL_TAG}
# 受保护标签单一来源:provider 投影标签与路由能力标签只能由 worker 运行时探测自证
# (二进制 + 凭证 + 配置 feature),手工传入等于伪造能力声明。集合从投影同源常量派生,
# worker 启动校验与 scheduler 侧测试都引用这里,不得另抄一份。
PROTECTED_CAPABILITY_TAGS = frozenset(
    _CLI_PROVIDERS
    | {f"{name}-api" for name in _KNOWN_API_PROVIDERS}
    | {"local"}
    | _ROUTING_CAPABILITIES
)
def resolve_api_credential(name: str, entry: Mapping | None) -> str:
    """API provider 的密钥解析唯一入口:配置显式值优先,缺省按 {NAME}_API_KEY 约定读环境。

    坑:配置加载对未定义的 ${VAR} 保留原文(shared.config.resolve_env_vars),
    未解析的占位符不是密钥。Gateway 与 Worker 必须用同一份判据,
    否则会出现 Gateway 调得通、Worker 却永远不注册标签的死角。
    """
    value = (entry or {}).get("api_key")
    if type(value) is str:
        text = value.strip()
        if text and "${" not in text:
            return text
    return (os.environ.get(f"{str(name).upper()}_API_KEY") or "").strip()


def protected_capability_tags(providers_config: dict | None = None) -> frozenset[str]:
    """受保护标签的真实集合:内置常量 + 已加载配置里每个 provider 的投影标签。

    只用内置常量会漏掉动态 provider:providers.yaml 里新增一个 provider 就多一个可投影的
    标签,而它不在 _CLI_PROVIDERS 与 _KNOWN_API_PROVIDERS 里,手工 --tags 就能伪造。
    能力标签只能由运行时探测自证,任何可被投影出来的标签都必须一起保护。
    """
    tags = set(PROTECTED_CAPABILITY_TAGS)
    configured = (providers_config or {}).get("providers")
    if isinstance(configured, dict):
        for name in configured:
            if type(name) is not str or not name.strip():
                continue
            try:
                tags.add(provider_required_tag(name, providers_config))
            except ValueError:
                continue
    return frozenset(tags)


_PROVIDER_RUNTIME_CAPABILITIES = {
    "claude-cli": {READ_TOOL_TAG, WEBSEARCH_TOOL_TAG},
    "codex-cli": {READ_TOOL_TAG, WEBSEARCH_TOOL_TAG},
    "qoder-cli": {READ_TOOL_TAG, WEBSEARCH_TOOL_TAG},
}

# rerun-smart 只消费这一份角色映射,避免 API 为 video 写死步骤名。
PIPELINE_AI_ROLES: dict[str, tuple[str, str]] = {
    "video": ("11_smart", "12_review"),
    "document": ("05_smart", "08_review"),
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


AI_PARAM_OVERRIDE_KEYS = ("model", "reasoning_effort")


def parse_ai_param_override(
    document: Any,
    step_name: str,
) -> tuple[dict[str, str], str | None]:
    """从 job.json 取单步 model/reasoning_effort 覆盖;异常形状返回原因且绝不透传原值。
    只校验形状:与 provider 覆盖的耦合由执行端判定。"""
    if not isinstance(document, dict):
        return {}, "job_root_not_object"
    if "ai_param_overrides" not in document:
        return {}, None
    overrides = document.get("ai_param_overrides")
    if not isinstance(overrides, dict):
        return {}, "ai_param_overrides_not_object"
    if step_name not in overrides:
        return {}, None
    value = overrides.get(step_name)
    if not isinstance(value, dict) or not value:
        return {}, "step_param_override_not_object"
    if any(key not in AI_PARAM_OVERRIDE_KEYS for key in value):
        return {}, "unknown_param_override_key"
    parsed: dict[str, str] = {}
    for key in AI_PARAM_OVERRIDE_KEYS:
        if key not in value:
            continue
        item = value[key]
        if type(item) is not str or not item.strip():
            return {}, "param_override_not_nonempty_string"
        parsed[key] = item.strip()
    return parsed, None


# AI 参数不可执行时的稳定机器码。API 4xx、导入计划冲突、恢复物化拒绝、执行端 fail-closed
# 和配置加载错配共用同一批码:同一份快照在两个环境被拒时,凭码就能对齐原因。
# 形状类原因码由 parse_ai_override / parse_ai_param_override 产出,不在此重复定义。
AI_PARAM_REQUIRES_PROVIDER = "param_override_requires_provider"
AI_PARAM_UNKNOWN_PROVIDER = "unknown_provider"
AI_PARAM_DOMAIN_NOT_STRING_LIST = "provider_domain_not_string_list"
AI_PARAM_MODEL_DOMAIN_MISSING = "provider_declares_no_model_domain"
AI_PARAM_MODEL_NOT_ALLOWED = "model_not_in_provider_domain"
AI_PARAM_EFFORT_DOMAIN_MISSING = "provider_declares_no_reasoning_effort_domain"
AI_PARAM_EFFORT_NOT_ALLOWED = "reasoning_effort_not_in_provider_domain"
AI_PARAM_EFFORT_BY_MODEL_MALFORMED = "reasoning_efforts_by_model_malformed"
AI_PARAM_EFFORT_BY_MODEL_INCOMPLETE = "reasoning_efforts_by_model_incomplete"
AI_PROVIDER_CLI_READINESS_UNKNOWN = "cli_provider_readiness_unprovable"
AI_ROUTE_ALLOWED_PROVIDERS_REQUIRED = "ai_route_allowed_providers_required"

@dataclass(frozen=True)
class AIParamViolation:
    """一条 AI 参数不可执行记录:稳定机器码 + 定位信息。

    跨环境恢复要能回答"哪个 provider 的哪个字段、当前环境允许什么",所以 allowed
    随违规一起带出;调用方只加 job/step 前缀,不重写判定或文案。
    """

    code: str
    provider: str = ""
    field: str = ""
    value: str = ""
    allowed: tuple[str, ...] = ()

    def message(self) -> str:
        where = f"provider '{self.provider}'" if self.provider else "AI 覆盖"
        detail = f" {self.field}={self.value!r}" if self.field else ""
        domain = f", 当前环境取值域 [{', '.join(self.allowed)}]" if self.allowed else ""
        return f"{where}{detail} 不可执行: {self.code}{domain}"


def _provider_entry(provider: str, providers_config: dict | None) -> dict | None:
    providers = (providers_config or {}).get("providers")
    if not isinstance(providers, dict):
        return None
    entry = providers.get(provider)
    return entry if isinstance(entry, dict) else None


def _declared_domain(entry: Mapping, key: str) -> tuple[str, ...] | None:
    """读 provider 声明的取值域。缺键或空表返回空元组,声明了但形状不可信返回 None。"""
    raw = entry.get(key)
    if raw is None:
        return ()
    if not isinstance(raw, list):
        return None
    if not raw:
        return ()
    if not all(type(item) is str and item.strip() for item in raw):
        return None
    return tuple(item.strip() for item in raw)


def _coerce_domain(value: Any) -> tuple[str, ...] | None:
    """取值域规整:非字符串列表返回 None(配置错),空列表返回空元组(未声明)。"""
    if value is None:
        return ()
    if not isinstance(value, list) or not all(type(i) is str for i in value):
        return None
    return tuple(value)


def validate_ai_param_override(
    provider: str,
    params: Mapping[str, str] | None,
    providers_config: dict | None,
) -> AIParamViolation | None:
    """按目标环境配置判定单步 model/effort 覆盖能否执行;合法返回 None。

    唯一判定入口,API 创建、导入计划、恢复物化、执行端和配置加载都调这里。
    取值域必须由目标环境声明:未声明就拒绝覆盖,不回落 provider 默认也不放行任意值,
    否则源环境合法的档位会在取值域更窄的目标环境被 CLI 静默降级。
    """
    if not params:
        return None
    if not provider:
        return AIParamViolation(AI_PARAM_REQUIRES_PROVIDER)
    entry = _provider_entry(provider, providers_config)
    if entry is None:
        return AIParamViolation(AI_PARAM_UNKNOWN_PROVIDER, provider)
    # 档位域按有效模型取,不是全局并集:同一个档位在某些模型上不存在,
    # 用并集判定会放行 CLI 只能静默降级的组合,审计还会记下一个实际未生效的档位。
    # 有效模型 = 本次覆盖值,没覆盖就是 provider 声明的默认。
    effective_model = params.get("model") or entry.get("model")
    effort_domain_key = "reasoning_efforts"
    by_model = entry.get("reasoning_efforts_by_model")
    if isinstance(by_model, dict):
        # 声明了按模型域就必须命中。回落全局并集会 fail-open:漏配一个模型子项,
        # 该模型就重新接受并集里它其实不支持的档位。
        if effective_model not in by_model:
            if params.get("reasoning_effort"):
                return AIParamViolation(
                    AI_PARAM_EFFORT_BY_MODEL_INCOMPLETE, provider,
                    "reasoning_efforts_by_model", str(effective_model),
                )
        else:
            effort_domain_key = ("reasoning_efforts_by_model", str(effective_model))
    checks = (
        ("model", "models", AI_PARAM_MODEL_DOMAIN_MISSING, AI_PARAM_MODEL_NOT_ALLOWED),
        ("reasoning_effort", effort_domain_key,
         AI_PARAM_EFFORT_DOMAIN_MISSING, AI_PARAM_EFFORT_NOT_ALLOWED),
    )
    for field, domain_key, missing_code, rejected_code in checks:
        value = params.get(field)
        if not value:
            continue
        if isinstance(domain_key, tuple):
            nested = entry.get(domain_key[0])
            sub = nested.get(domain_key[1]) if isinstance(nested, dict) else None
            domain = _coerce_domain(sub)
            domain_key = f"{domain_key[0]}.{domain_key[1]}"
        else:
            domain = _declared_domain(entry, domain_key)
        if domain is None:
            return AIParamViolation(
                AI_PARAM_DOMAIN_NOT_STRING_LIST, provider, domain_key,
            )
        if not domain:
            return AIParamViolation(missing_code, provider, field, value)
        if value not in domain:
            return AIParamViolation(rejected_code, provider, field, value, domain)
    return None


def validate_job_ai_document(
    document: Any,
    providers_config: dict | None,
) -> list[tuple[str, AIParamViolation]]:
    """按目标环境配置复核 job.json 全部步骤的 provider 与参数覆盖。

    导入与恢复合并 job.json 时用它替代"只查形状":源环境合法不等于目标环境合法。
    返回 (step, violation) 列表,step 为空串表示容器级问题;空列表表示可执行。
    """
    if not isinstance(document, dict):
        return [("", AIParamViolation("job_root_not_object"))]
    violations: list[tuple[str, AIParamViolation]] = []
    overrides = document.get("ai_overrides")
    if "ai_overrides" in document and not isinstance(overrides, dict):
        violations.append(("", AIParamViolation("ai_overrides_not_object")))
        overrides = None
    params_map = document.get("ai_param_overrides")
    if "ai_param_overrides" in document and not isinstance(params_map, dict):
        violations.append(("", AIParamViolation("ai_param_overrides_not_object")))
        params_map = None
    steps = set(overrides or {}) | set(params_map or {})
    for step in sorted(steps, key=str):
        if type(step) is not str or not step.strip():
            violations.append(("", AIParamViolation("step_key_not_nonempty_string")))
            continue
        declared = (overrides or {}).get(step)
        provider, reason = parse_ai_override(document, step, providers_config or {})
        if reason is not None:
            violations.append((step, AIParamViolation(
                reason, declared if type(declared) is str else "",
            )))
            continue
        params, reason = parse_ai_param_override(document, step)
        if reason is not None:
            violations.append((step, AIParamViolation(reason, provider or "")))
            continue
        violation = validate_ai_param_override(
            provider or "", params, providers_config or {},
        )
        if violation is not None:
            violations.append((step, violation))
    return violations


def validate_provider_defaults(providers_config: dict | None) -> list[AIParamViolation]:
    """provider 自己的默认值必须落在自己声明的取值域,配置加载期就暴露错配。

    默认值越界比覆盖越界更隐蔽:没人显式选它,却是每次调用的实际取值。
    """
    providers = (providers_config or {}).get("providers")
    if not isinstance(providers, dict):
        return []
    violations: list[AIParamViolation] = []
    for name, entry in sorted(providers.items(), key=lambda item: str(item[0])):
        if not isinstance(entry, dict):
            continue
        model = entry.get("model")
        expected_cli_type = CLI_PROVIDER_TYPES_BY_NAME.get(str(name))
        actual_type = entry.get("type")
        if expected_cli_type is not None and actual_type != expected_cli_type:
            violations.append(AIParamViolation(
                AI_PROVIDER_CLI_READINESS_UNKNOWN, str(name), "type", str(actual_type),
                (expected_cli_type,),
            ))
            continue
        # 默认档位要按默认模型的子域校验, 不是全局并集:
        # model=gpt-5.4 加 reasoning_effort=ultra 这种组合并集里有、子域里没有,
        # 只查并集就会正常启动, 直到每次执行才失败, 该 provider 全面不可用。
        default_effort_domain_key = "reasoning_efforts"
        by_model_map = entry.get("reasoning_efforts_by_model")
        if isinstance(by_model_map, dict) and model in by_model_map:
            default_effort_domain_key = ("reasoning_efforts_by_model", str(model))
        checks = (
            ("model", "models", model, None, AI_PARAM_MODEL_NOT_ALLOWED),
            ("reasoning_effort", default_effort_domain_key, entry.get("reasoning_effort"),
             AI_PARAM_EFFORT_DOMAIN_MISSING, AI_PARAM_EFFORT_NOT_ALLOWED),
        )
        legacy_cli_type = actual_type == "cli" or (
            isinstance(actual_type, str) and actual_type.startswith("cli_")
        )
        if (actual_type in CLI_PROVIDER_TYPES or legacy_cli_type) and str(name) not in _CLI_PROVIDERS:
            # 自定义 CLI 没有可达的注册路径:cli_provider_ready 只认三个固定名,
            # worker 自证不出标签, 手填又被受保护集合拒绝, 配了也永远拿不到任务。
            # 2.6.0 只承诺这三个 CLI, 因此在加载期就拒绝而不是留一条走不通的死路。
            violations.append(AIParamViolation(
                AI_PROVIDER_CLI_READINESS_UNKNOWN, str(name), "type",
                str(entry.get("type")),
            ))
            continue
        by_model = entry.get("reasoning_efforts_by_model")
        if by_model is not None:
            models_domain = _declared_domain(entry, "models") or ()
            if not isinstance(by_model, dict) or not by_model:
                violations.append(AIParamViolation(
                    AI_PARAM_EFFORT_BY_MODEL_MALFORMED, str(name),
                    "reasoning_efforts_by_model",
                ))
            else:
                for key, sub in sorted(by_model.items(), key=lambda i: str(i[0])):
                    if _coerce_domain(sub) in (None, ()):
                        violations.append(AIParamViolation(
                            AI_PARAM_EFFORT_BY_MODEL_MALFORMED, str(name),
                            "reasoning_efforts_by_model", str(key),
                        ))
                # 声明了按模型域就必须覆盖全部声明模型,漏一个就是 fail-open 缺口。
                for declared in models_domain:
                    if declared not in by_model:
                        violations.append(AIParamViolation(
                            AI_PARAM_EFFORT_BY_MODEL_INCOMPLETE, str(name),
                            "reasoning_efforts_by_model", str(declared),
                        ))
        for field, domain_key, value, missing_code, rejected_code in checks:
            if isinstance(domain_key, tuple):
                nested = entry.get(domain_key[0])
                sub = nested.get(domain_key[1]) if isinstance(nested, dict) else None
                domain = _coerce_domain(sub)
                domain_key = f"{domain_key[0]}.{domain_key[1]}"
            else:
                domain = _declared_domain(entry, domain_key)
            if domain is None:
                violations.append(AIParamViolation(
                    AI_PARAM_DOMAIN_NOT_STRING_LIST, str(name), domain_key,
                ))
                continue
            if value is None:
                continue
            if type(value) is not str or not value.strip():
                violations.append(AIParamViolation(
                    AI_PARAM_DOMAIN_NOT_STRING_LIST, str(name), field, str(value),
                ))
                continue
            if not domain:
                # 模型未声明取值域时无从核对,交 CLI 自己报错;档位必须可核对,
                # 三个 CLI 对越界档位都不报错,静默按自家默认跑。
                if missing_code is not None:
                    violations.append(AIParamViolation(
                        missing_code, str(name), field, value.strip(),
                    ))
                continue
            if value.strip() not in domain:
                violations.append(AIParamViolation(
                    rejected_code, str(name), field, value.strip(), domain,
                ))
    return violations


def worker_satisfies_requirements(
    worker: Any,
    pool: str,
    required_tags: set[str] | list[str] | tuple[str, ...],
    allowed_providers: set[str] | list[str] | tuple[str, ...] = (),
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
    required = set(required_tags)
    source_roots = {tag for tag in tags if tag.startswith("source-root:")}
    if source_roots and source_roots.isdisjoint(required):
        return False
    allowed = set(allowed_providers)
    provider_matches = tags.intersection(allowed)
    if allowed and len(provider_matches) != 1:
        return False
    return pool in pools and required.issubset(tags)


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
        if kind in CLI_PROVIDER_TYPES:
            # CLI provider 用自己的名字当标签。2.6.0 只支持三个内置 CLI,
            # 自定义 CLI 在配置加载期就被拒(validate_provider_defaults),不会走到这里。
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
    """返回本步全部 AND 硬标签。provider 的 OR 集合不混进这里。"""
    if override:
        return provider_required_tags(
            override, providers_config, required_tags=required_tags,
        )
    tags: set[str] = set()
    body = ai if isinstance(ai, dict) else {}
    if "allowed_providers" in body:
        allowed_providers_from_ai(body, providers_config, required_tags=required_tags)
        return sorted(set(required_tags))
    for tier_name, tier in body.items():
        if tier_name == "allowed_providers":
            continue
        if not isinstance(tier, dict) or not tier.get("provider"):
            continue
        tags.update(provider_required_tags(
            str(tier["provider"]), providers_config, required_tags=required_tags,
        ))
    if required_tags and not tags:
        raise ValueError("AI capability has no configured provider")
    return sorted(tags)


def allowed_providers_from_ai(
    ai: dict | None,
    providers_config: dict | None = None,
    *,
    required_tags: set[str] | list[str] | tuple[str, ...] = (),
) -> tuple[str, ...]:
    """解析 AI route 的 concrete-provider OR 集合;缺失返回空元组。

    OR 集合只接受三个内置 CLI。每个候选都必须配置且支持步骤能力,否则整个 route
    fail-closed,不能把错误候选静默摘掉后改变调度语义。
    """
    body = ai if isinstance(ai, dict) else {}
    raw = body.get("allowed_providers")
    if raw is None:
        return ()
    if (
        not isinstance(raw, list) or not raw
        or not all(type(item) is str and item.strip() for item in raw)
    ):
        raise ValueError("allowed_providers must be a non-empty string list")
    values = tuple(item.strip() for item in raw)
    if len(set(values)) != len(values):
        raise ValueError("allowed_providers must not contain duplicates")
    for provider in values:
        if provider not in _CLI_PROVIDERS:
            raise ValueError(f"unsupported concrete CLI provider: {provider}")
        if providers_config is None:
            continue
        if not provider_is_configured(provider, providers_config):
            raise ValueError(f"unknown AI provider: {provider}")
        provider_required_tags(
            provider, providers_config, required_tags=required_tags,
        )
    return values


def step_allowed_providers(
    step: dict,
    providers_config: dict | None,
    *,
    override: str | None = None,
    capability_tags: set[str] | list[str] | tuple[str, ...] = (),
) -> list[str]:
    """返回 enqueue/claim 使用的 provider OR 集合;显式 override 改走 require_tags。"""
    if override:
        return []
    return list(allowed_providers_from_ai(
        step.get("ai"), providers_config, required_tags=capability_tags,
    ))


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
        for capability in (READ_TOOL_TAG, WEBSEARCH_TOOL_TAG):
            if capability in required:
                ai_capabilities.add(capability)
        required.update(ai_required_tags(
            step.get("ai"), providers_config, override=override,
            required_tags=sorted(ai_capabilities),
        ))
    if source not in {"upload", "nas_source"} and step.get("name") in net_steps:
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
