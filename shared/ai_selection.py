"""解析一次 AI 调用最终生效的 provider/model/推理档位,供输入指纹与审计共用同一份结论。"""

from __future__ import annotations

import hashlib
import json

# provider 来源:定义直接给出 / 原子 claim 选中 / 声明无效。
PROVIDER_FROM_DECLARATION = "declaration"
PROVIDER_FROM_CLAIM = "claim"
PROVIDER_UNRESOLVED = "unresolved"

MODEL_FROM_DECLARATION = "declaration"
MODEL_FROM_OVERRIDE = "override"
MODEL_FROM_PROVIDER = "provider_default"

EFFORT_FROM_REQUEST = "request"
EFFORT_FROM_PROVIDER = "provider_default"
# 双方都没给档位,交给 CLI 自己的默认。审计写 None 时必须同时写这个来源,才能和
# "provider 默认恰好等于 None" 区分开。
EFFORT_UNSET = "unset"

# tier 尝试顺序与 AIGateway 一致;pipelines.yaml 若新增 tier 名,按字典序缀在后面保证摘要稳定。
_TIER_ORDER = ("primary", "fallback", "text_fallback")


def provider_config(providers_config: dict | None, provider: object) -> dict:
    """取单个 provider 的运行配置;缺失或形状不对回空 dict,让调用方走各自的兜底。"""
    if not isinstance(provider, str) or not provider:
        return {}
    providers = (providers_config or {}).get("providers")
    entry = providers.get(provider) if isinstance(providers, dict) else None
    return entry if isinstance(entry, dict) else {}


def provider_features(providers_config: dict | None, provider: object) -> list[str]:
    """provider 实际启用的能力。能力被摘掉会让同一 prompt 走不同执行路径,属于影响结果的配置。"""
    features = provider_config(providers_config, provider).get("features")
    if not isinstance(features, list):
        return []
    return sorted({item for item in features if type(item) is str})


def resolve_provider(
    providers_config: dict | None, declared: object,
) -> tuple[str | None, str]:
    """返回声明的具体 provider;缺失时保留不可执行状态供调用方 fail-closed。"""
    if not isinstance(declared, str) or not declared:
        return None, PROVIDER_UNRESOLVED
    return declared, PROVIDER_FROM_DECLARATION


def override_tier_model(
    providers_config: dict | None, provider: str, params: dict | None,
) -> str:
    """job 级 provider 覆盖时合成 tier 用的模型。AIInvocation 与指纹快照共用,防止两处漂移。"""
    override = (params or {}).get("model")
    if override:
        return override
    config = provider_config(providers_config, provider)
    # 声明的默认优先于取值域首项:models 是覆盖的合法域,不是默认值来源。
    # 反过来排会让重排 models 顺序静默换掉执行模型,而默认值本该是配置的单一来源。
    declared = config.get("model")
    if declared:
        return str(declared)
    models = config.get("models")
    if isinstance(models, list) and models:
        return str(models[0])
    return "unknown"


def effective_model(
    providers_config: dict | None,
    provider: object,
    declared_model: object,
) -> tuple[str | None, str]:
    """tier 最终送进 provider 的模型。tier 没写模型时由 provider 配置兜底,与各 CLI 的
    `request.model or self._model` 同序。"""
    if isinstance(declared_model, str) and declared_model:
        return declared_model, MODEL_FROM_DECLARATION
    fallback = provider_config(providers_config, provider).get("model")
    return (fallback or None), MODEL_FROM_PROVIDER


def effective_reasoning_effort(
    providers_config: dict | None,
    provider: object,
    requested: object,
) -> tuple[str | None, str]:
    """请求级档位优先,其次 providers.yaml 的 provider 默认,都没有才交给 CLI 自定。
    返回值第二项是来源,审计据它区分 "没人设过" 和 "设成了这个值"。"""
    if isinstance(requested, str) and requested:
        return requested, EFFORT_FROM_REQUEST
    default = provider_config(providers_config, provider).get("reasoning_effort")
    if isinstance(default, str) and default:
        return default, EFFORT_FROM_PROVIDER
    return None, EFFORT_UNSET


def _ordered_tiers(ai_config: dict | None) -> list[tuple[str, dict]]:
    tiers = ai_config if isinstance(ai_config, dict) else {}
    known = [(name, tiers[name]) for name in _TIER_ORDER if isinstance(tiers.get(name), dict)]
    extra = sorted(
        (name, value) for name, value in tiers.items()
        if name not in _TIER_ORDER and isinstance(value, dict)
    )
    return known + extra


def selection_snapshot(
    *,
    providers_config: dict | None,
    ai_config: dict | None,
    override_provider: str = "",
    override_params: dict | None = None,
) -> dict:
    """一次调用的完整有效选择视图,给审计读。

    有 job 级 provider 覆盖时按 AIInvocation 的合成规则只剩一个 primary tier,
    与实际路由完全一致;没有覆盖时逐 tier 展开 pipeline 声明的降级链。
    """
    params = override_params or {}
    override: dict[str, str] = {}
    for key, value in (
        ("provider", override_provider),
        ("model", params.get("model", "")),
        ("reasoning_effort", params.get("reasoning_effort", "")),
    ):
        if value:
            override[key] = value

    if override_provider:
        declarations = [(
            "primary",
            {
                "provider": override_provider,
                "model": override_tier_model(providers_config, override_provider, params),
            },
        )]
    else:
        declarations = _ordered_tiers(ai_config)

    tiers = []
    for name, declaration in declarations:
        declared_provider = declaration.get("provider")
        provider, provider_source = resolve_provider(providers_config, declared_provider)
        if declaration.get("provider_source") == PROVIDER_FROM_CLAIM:
            provider_source = PROVIDER_FROM_CLAIM
        declared_model = declaration.get("model")
        model, model_source = effective_model(providers_config, provider, declared_model)
        if override_provider and params.get("model"):
            model_source = MODEL_FROM_OVERRIDE
        effort, effort_source = effective_reasoning_effort(
            providers_config, provider, params.get("reasoning_effort"),
        )
        tiers.append({
            "tier": name,
            "declared_provider": declared_provider if isinstance(declared_provider, str) else None,
            "provider": provider,
            "provider_source": provider_source,
            "model": model,
            "model_source": model_source,
            "reasoning_effort": effort,
            "reasoning_effort_source": effort_source,
            "features": provider_features(providers_config, provider),
        })
    return {"override": override, "tiers": tiers}


def fingerprint_projection(snapshot: dict) -> dict:
    """从有效选择里投影出输入指纹该负责的部分。

    pipeline 定义里声明的 provider/model 已经进了 def_digest,重复放进输入指纹会让
    定义改动同时移动两个摘要,边界就糊了。留下的是定义看不见的东西:job 级覆盖、
    claim 命中的具体 provider、provider 配置兜底出来的模型、生效档位和启用能力。
    """
    override = dict(snapshot.get("override") or {})
    job_owned = bool(override.get("provider"))
    tiers = []
    for tier in snapshot.get("tiers") or []:
        entry: dict = {"tier": tier.get("tier")}
        if job_owned or tier.get("provider_source") != PROVIDER_FROM_DECLARATION:
            entry["provider"] = tier.get("provider")
            entry["provider_source"] = tier.get("provider_source")
        if job_owned or tier.get("model_source") != MODEL_FROM_DECLARATION:
            entry["model"] = tier.get("model")
            entry["model_source"] = tier.get("model_source")
        # 档位进指纹只看来源, 不看 job 是否覆盖了 provider。
        # job_owned 只说明 provider 由 job 决定, 不代表档位也是 job 选的:
        # 那种情况下档位仍来自 providers.yaml 默认, 跟着进指纹就会让改一次部署配置
        # 失效全部存量 manifest, 与"默认档位不追溯"的契约相反(docs/08-deployment.md)。
        if tier.get("reasoning_effort_source") == EFFORT_FROM_REQUEST:
            entry["reasoning_effort"] = tier.get("reasoning_effort")
            entry["reasoning_effort_source"] = tier.get("reasoning_effort_source")
        entry["features"] = tier.get("features")
        tiers.append(entry)
    return {"override": override, "tiers": tiers}


def selection_digest(snapshot: dict) -> str:
    """投影后的有效选择摘要,直接当输入指纹值。摘要而非明文:指纹值有长度上限,
    provider 配置长度不可控;可读版在 ai_logs 的 routing.selection 里。"""
    blob = json.dumps(
        fingerprint_projection(snapshot), sort_keys=True, ensure_ascii=False,
    )
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()
