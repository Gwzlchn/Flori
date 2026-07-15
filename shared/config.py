"""配置加载:YAML + 环境变量替换 + GitLab-CI 风格流水线归一化。"""

from __future__ import annotations

import copy
import fnmatch
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml


_ENV_PATTERN = re.compile(r"\$\{(\w+)(?::-(.*?))?\}")

# 流水线内变量引用:$VAR 或 ${VAR},作用域是 pipeline 的 variables 而非 OS env。
_PIPE_VAR_PATTERN = re.compile(r"\$\{(\w+)\}|\$(\w+)")

# extends 继承链深度上限,防环/防失控(对标 GitLab 建议 ≤3 级)。
_MAX_EXTENDS_DEPTH = 5

# 新→旧字段名映射:归一化后落到 worker/scheduler 现有消费的 step dict 字段。
_FIELD_ALIASES = {
    "run": "module",
    "needs": "depends_on",
    "timeout": "timeout_sec",
    "retry": "retries",
}

# 顶层非 pipeline 的保留键(模板/默认/包含/变量),归一化时不当作内容类型。
_RESERVED_TOP_KEYS = {"default", "include", "variables"}
_AI_TIERS = {"primary", "fallback", "text_fallback"}


def _pipeline_variable_references(value) -> set[str]:
    refs: set[str] = set()
    if isinstance(value, str):
        refs.update(
            (match.group(1) or match.group(2))
            for match in _PIPE_VAR_PATTERN.finditer(value)
        )
    elif isinstance(value, dict):
        for item in value.values():
            refs.update(_pipeline_variable_references(item))
    elif isinstance(value, list):
        for item in value:
            refs.update(_pipeline_variable_references(item))
    return refs


def _validate_shared_ai_variables(raw: dict) -> None:
    variables = raw.get("variables") or {}
    if not isinstance(variables, dict):
        raise ValueError("top-level variables must be a mapping")
    ai_variables = {
        name: value
        for name, value in variables.items()
        if name.startswith("AI_")
    }
    if not ai_variables:
        return
    invalid = sorted(
        name for name, value in ai_variables.items()
        if not isinstance(value, str) or not value.strip()
    )
    if invalid:
        raise ValueError(f"AI variables must be non-empty strings: {invalid}")
    bodies = {
        name: body for name, body in raw.items()
        if name not in _RESERVED_TOP_KEYS and not name.startswith(".")
    }
    references = {
        name for name in _pipeline_variable_references(bodies)
        if name.startswith("AI_")
    }
    undefined = sorted(references - set(ai_variables))
    unused = sorted(set(ai_variables) - references)
    if undefined or unused:
        raise ValueError(
            f"AI variable contract mismatch: undefined={undefined}, unused={unused}"
        )


def validate_ai_pipeline_contract(pipelines: dict, providers: dict | None = None) -> None:
    """校验归一化 AI route,不改变 tier 顺序或调用次数."""
    known = None
    if providers is not None:
        provider_map = providers.get("providers") if isinstance(providers, dict) else None
        if not isinstance(provider_map, dict):
            raise ValueError("providers config must contain a providers mapping")
        known = set(provider_map)
    for pipeline, body in pipelines.items():
        for step in body.get("steps", []):
            if step.get("pool") != "ai":
                continue
            ai = step.get("ai")
            if not isinstance(ai, dict) or not ai:
                raise ValueError(f"AI route is missing: {pipeline}/{step.get('name')}")
            illegal = sorted(set(ai) - _AI_TIERS)
            if illegal:
                raise ValueError(
                    f"invalid AI tier for {pipeline}/{step.get('name')}: {illegal}"
                )
            for tier, route in ai.items():
                if not isinstance(route, dict) or set(route) != {"provider", "model"}:
                    raise ValueError(
                        f"invalid AI route shape: {pipeline}/{step.get('name')}/{tier}"
                    )
                provider = route.get("provider")
                model = route.get("model")
                if not isinstance(provider, str) or not provider.strip():
                    raise ValueError("AI provider must be a non-empty string")
                if not isinstance(model, str) or not model.strip():
                    raise ValueError("AI model must be a non-empty string")
                if (
                    _PIPE_VAR_PATTERN.search(provider)
                    or _PIPE_VAR_PATTERN.search(model)
                ):
                    raise ValueError(
                        f"unresolved AI variable: {pipeline}/{step.get('name')}/{tier}"
                    )
                if known is not None and provider not in known:
                    raise ValueError(f"unknown AI provider: {provider}")


def validate_provenance_pipeline_contract(pipelines: dict) -> None:
    """校验 index candidate 的 sidecar producer 与引入版本边界。"""
    for pipeline, body in pipelines.items():
        steps = body.get("steps", [])
        step_by_name = {
            step.get("name"): step
            for step in steps
            if isinstance(step, dict) and isinstance(step.get("name"), str)
        }
        for owner in steps:
            if not isinstance(owner, dict):
                continue
            for effect in owner.get("on_complete") or []:
                if not isinstance(effect, dict) or effect.get("action") != "index_note":
                    continue
                candidates = effect.get("candidates")
                if not isinstance(candidates, list) or not candidates:
                    raise ValueError(
                        f"index_note candidates are invalid: {pipeline}/{owner.get('name')}"
                    )
                for index, candidate in enumerate(candidates):
                    if not isinstance(candidate, dict):
                        raise ValueError(
                            f"index_note candidate is invalid: "
                            f"{pipeline}/{owner.get('name')}/{index}"
                        )
                    sidecar_fields = (
                        "source_manifest", "provenance",
                        "provenance_step", "provenance_since_version",
                    )
                    if not any(field in candidate for field in sidecar_fields):
                        if any(field in candidate for field in (
                            "legacy_provenance_step",
                            "legacy_provenance_since_version",
                        )):
                            raise ValueError(
                                f"legacy provenance requires sidecar fields: "
                                f"{pipeline}/{owner.get('name')}/{index}"
                            )
                        continue
                    if any(
                        not isinstance(candidate.get(field), str)
                        or not candidate[field].strip()
                        for field in sidecar_fields
                    ):
                        raise ValueError(
                            f"provenance candidate fields are invalid: "
                            f"{pipeline}/{owner.get('name')}/{index}"
                        )
                    legacy_fields = (
                        "legacy_provenance_step",
                        "legacy_provenance_since_version",
                    )
                    has_legacy = any(field in candidate for field in legacy_fields)
                    if has_legacy and any(
                        not isinstance(candidate.get(field), str)
                        or not candidate[field].strip()
                        for field in legacy_fields
                    ):
                        raise ValueError(
                            f"legacy provenance boundary fields are invalid: "
                            f"{pipeline}/{owner.get('name')}/{index}"
                        )
                    boundaries = [
                        ("provenance_step", "provenance_since_version"),
                    ]
                    if has_legacy:
                        boundaries.append((
                            "legacy_provenance_step",
                            "legacy_provenance_since_version",
                        ))
                    for step_field, since_field in boundaries:
                        producer_name = candidate[step_field]
                        producer = step_by_name.get(producer_name)
                        if producer is None:
                            raise ValueError(
                                f"provenance producer step is unknown: "
                                f"{pipeline}/{producer_name}"
                            )
                        since_text = candidate[since_field]
                        current_version = producer.get("version", "1")
                        current_text = (
                            str(current_version)
                            if type(current_version) in (str, int) else ""
                        )
                        if (
                            not since_text.isdigit()
                            or int(since_text) < 1
                            or not current_text.isdigit()
                            or int(current_text) < int(since_text)
                        ):
                            raise ValueError(
                                f"provenance version boundary is invalid: "
                                f"{pipeline}/{producer_name}"
                            )
                        outputs = producer.get("outputs")
                        provenance_path = candidate["provenance"]
                        if not isinstance(outputs, list) or not any(
                            isinstance(pattern, str)
                            and fnmatch.fnmatch(provenance_path, pattern)
                            for pattern in outputs
                        ):
                            raise ValueError(
                                f"provenance output is not declared by producer: "
                                f"{pipeline}/{producer_name}"
                            )


def resolve_env_vars(text: str) -> str:
    """替换 ${VAR} 和 ${VAR:-default} 格式的环境变量引用。
    - 有值:替换为环境变量值
    - 无值+有默认值:替换为默认值
    - 无值+无默认值:保留原文(运行时可能才需要)
    """

    def _replacer(match: re.Match) -> str:
        var_name = match.group(1)
        default = match.group(2)
        value = os.environ.get(var_name)
        if value is not None:
            return value
        if default is not None:
            return default
        return match.group(0)

    return _ENV_PATTERN.sub(_replacer, text)


def _coerce_scalar(text: str):
    """把整段就是一个数字的字符串还原为数值,使 $VAR 注入的数值保持原类型。
    先试 int(timeout/retry 等),再试 float(如 1.5),都不是则保留原字符串。"""
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text


def _resolve_pipeline_vars(value, variables: dict):
    """递归把结构里的 $VAR / ${VAR} 替换为 pipeline variables 的值。未定义变量保留原文。"""
    if isinstance(value, str):
        def _sub(match: re.Match) -> str:
            name = match.group(1) or match.group(2)
            return str(variables[name]) if name in variables else match.group(0)

        replaced = _PIPE_VAR_PATTERN.sub(_sub, value)
        # 整串恰为一个变量引用时还原数值类型(timeout/retry 等需要 int)。
        if replaced != value and _PIPE_VAR_PATTERN.fullmatch(value):
            return _coerce_scalar(replaced)
        return replaced
    if isinstance(value, dict):
        return {k: _resolve_pipeline_vars(v, variables) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_pipeline_vars(v, variables) for v in value]
    return value


def load_yaml(path: Path) -> dict:
    """加载 YAML 文件并替换环境变量。文件不存在抛 FileNotFoundError。"""
    text = path.read_text(encoding="utf-8")
    resolved = resolve_env_vars(text)
    return yaml.safe_load(resolved) or {}


def _load_optional(path: Path) -> dict:
    """加载可选 YAML,不存在返回空 dict。"""
    if path.exists():
        return load_yaml(path)
    return {}


def _deep_merge(base: dict, overlay: dict) -> dict:
    """按键深合并:dict 递归合并,其余键 overlay 覆盖 base(对标 GitLab extends 语义)。"""
    result = copy.deepcopy(base)
    for key, val in overlay.items():
        if isinstance(val, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = copy.deepcopy(val)
    return result


def _resolve_extends(job: dict, templates: dict, _depth: int = 0) -> dict:
    """展开 job 的 extends 链:父模板(可多级)作底,子 job 字段深合并覆盖。"""
    parent_name = job.get("extends")
    if not parent_name:
        return {k: v for k, v in job.items() if k != "extends"}
    if _depth >= _MAX_EXTENDS_DEPTH:
        raise ValueError(f"extends 链过深（>{_MAX_EXTENDS_DEPTH} 级）: {parent_name}")
    if parent_name not in templates:
        raise ValueError(f"extends 引用了不存在的模板: {parent_name}")
    parent = _resolve_extends(templates[parent_name], templates, _depth + 1)
    child = {k: v for k, v in job.items() if k != "extends"}
    return _deep_merge(parent, child)


# rules 中 exists glob → 旧 condition 字符串的等价映射,保证 scheduler 行为不变。
_RULES_EXISTS_TO_CONDITION = {
    ("input/*.srt", "skip"): "no_subtitle",
    ("input/*.srt", "on"): "has_subtitle",
    ("input/*.ass", "on"): "has_danmaku",
}


def _normalize_when(when) -> str:
    """归一 when 取值:YAML 1.1 把裸 on/off 解析为布尔,统一回字符串语义。"""
    if when is True:
        return "on"
    if when is False:
        return "skip"
    return str(when) if when is not None else "on"


def _rules_to_condition(rules: list) -> str | None:
    """把已知的 exists 规则归一化回旧 condition 字符串,行为与硬编码判断等价。
    无法识别的规则原样保留在 step 的 rules 字段,由调度器的 rules 求值器处理。"""
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        glob = rule.get("exists")
        when = _normalize_when(rule.get("when"))
        if glob is not None:
            mapped = _RULES_EXISTS_TO_CONDITION.get((glob, when))
            if mapped:
                return mapped
    return None


def _normalize_job(name: str, job: dict) -> dict:
    """把单个新格式 job 归一化为旧 step dict(字段重命名 + 默认值 + 保留 image)。"""
    step: dict = {}
    for key, val in job.items():
        step[_FIELD_ALIASES.get(key, key)] = val

    step["name"] = name
    step.setdefault("depends_on", [])
    step.setdefault("image", "flori/step-base")

    # retry 可为 int 或 {max, when};归一化为旧的 retries 整数(worker/scheduler 只读次数)。
    retry = step.get("retries")
    if isinstance(retry, dict):
        step["retries"] = retry.get("max", 0)

    # rules → condition:已知 exists 规则映回旧字符串,行为与 check_condition 一致。
    rules = step.get("rules")
    if rules and "condition" not in step:
        mapped = _rules_to_condition(rules)
        if mapped:
            step["condition"] = mapped

    return step


def normalize_pipeline(raw_pipeline: dict, *, default: dict | None = None,
                       templates: dict | None = None) -> dict:
    """把单条流水线归一化为 worker/scheduler 消费的形状:{"steps": [step_dict, ...]}。
    既接受旧格式(steps: 列表),也接受新格式(jobs: 字典 + extends/needs/rules/variables)。
    归一化输出与旧格式逐字段等价,保证下游行为不变。"""
    templates = templates or {}

    # 旧格式:已是 {"steps": [...]},仅补全 image 默认值后原样返回。
    if "steps" in raw_pipeline and "jobs" not in raw_pipeline:
        steps = []
        for s in raw_pipeline["steps"]:
            step = dict(s)
            step.setdefault("image", "flori/step-base")
            step.setdefault("depends_on", [])
            steps.append(step)
        return {"steps": steps}

    variables = {**(raw_pipeline.get("variables") or {})}
    jobs = raw_pipeline.get("jobs") or {}

    steps: list[dict] = []
    for name, job in jobs.items():
        merged = _deep_merge(default or {}, _resolve_extends(job, templates))
        step = _normalize_job(name, merged)
        step = _resolve_pipeline_vars(step, variables)
        steps.append(step)
    return {"steps": steps}


def _collect_includes(raw: dict, config_dir: Path) -> dict:
    """合并 include 的 local 文件到主结构(顶层按键深合并,后者覆盖前者)。"""
    merged: dict = {k: v for k, v in raw.items() if k != "include"}
    for entry in raw.get("include") or []:
        local = entry.get("local") if isinstance(entry, dict) else entry
        if not local:
            continue
        included = load_yaml(config_dir / local)
        included = _collect_includes(included, config_dir)
        merged = _deep_merge(merged, included)
    return merged


def normalize_pipelines(raw: dict, config_dir: Path | None = None) -> dict:
    """把整份 pipelines.yaml 归一化:处理 include / default / 模板 / 变量,
    输出 {pipeline_name: {"steps": [...]}},与 worker/scheduler 消费的 in-memory 结构逐字段等价。"""
    if config_dir is not None:
        raw = _collect_includes(raw, config_dir)

    strict_ai_contract = any(
        isinstance(name, str) and name.startswith("AI_")
        for name in (
            (raw.get("variables") or {})
            if isinstance(raw.get("variables") or {}, dict)
            else {}
        )
    )
    _validate_shared_ai_variables(raw)

    default = raw.get("default") or {}
    # '.' 前缀的隐藏模板供 extends 引用,不作为内容类型流水线。
    templates = {k: v for k, v in raw.items() if k.startswith(".")}
    global_vars = raw.get("variables") or {}

    result: dict = {}
    for name, body in raw.items():
        if name in _RESERVED_TOP_KEYS or name.startswith("."):
            continue
        if not isinstance(body, dict):
            continue
        # pipeline 级变量叠加全局变量(pipeline 优先),消除 prod 与 integration 的双份定义。
        if "jobs" in body:
            body = {**body, "variables": {**global_vars, **(body.get("variables") or {})}}
        result[name] = normalize_pipeline(body, default=default, templates=templates)
    validate_provenance_pipeline_contract(result)
    if strict_ai_contract:
        validate_ai_pipeline_contract(result)
    return result


def load_pipelines(path: Path) -> dict:
    """加载并归一化 pipelines.yaml;支持新旧两种格式。"""
    raw = load_yaml(path)
    return normalize_pipelines(raw, config_dir=path.parent)


@dataclass
class AppConfig:
    data_dir: Path
    db_path: Path
    jobs_dir: Path
    config_dir: Path
    prompts_dir: Path
    pipelines: dict
    pools: dict
    providers: dict
    # 网络区域路由(configs/sources.yaml 的 net_routing 段):net_steps。
    # 区域(net-cn/net-global)由 shared.net_zone 按 URL + CN 域名表判;缺省空 dict → 回落内置默认。
    net_routing: dict = field(default_factory=dict)
    # 资源槽上限(configs/resources.yaml 的 resources 段):资源名 -> 并发上限。
    # 缺省空 dict → 不启用任何资源限流(无 resources.yaml 也能跑)。
    resources: dict = field(default_factory=dict)


def load_config(
    config_dir: str | Path = "/data/configs",
    data_dir: str | Path = "/data",
) -> AppConfig:
    """一次性加载全部配置。"""
    config_dir = Path(config_dir)
    data_dir = Path(data_dir)
    pipelines = load_pipelines(config_dir / "pipelines.yaml")
    providers = _load_optional(config_dir / "providers.yaml")
    validate_ai_pipeline_contract(pipelines, providers)
    return AppConfig(
        data_dir=data_dir,
        db_path=data_dir / "db" / "analyzer.db",
        jobs_dir=data_dir / "jobs",
        config_dir=config_dir,
        prompts_dir=data_dir / "prompts",
        pipelines=pipelines,
        pools=load_yaml(config_dir / "pools.yaml"),
        providers=providers,
        net_routing=(_load_optional(config_dir / "sources.yaml") or {}).get("net_routing") or {},
        resources=(_load_optional(config_dir / "resources.yaml") or {}).get("resources") or {},
    )


def load_domain_profile(config_dir: Path, domain: str) -> dict:
    """加载 domain/*.yaml,不存在返回空 dict。"""
    path = config_dir / "domain" / f"{domain}.yaml"
    return _load_optional(path)


# providers.yaml 在加载期已把 ${API_KEY} 解析成明文,按安全要求绝不下放给步骤。
_PROVIDER_SECRET_KEYS = ("api_key", "secret_key", "token")


def sanitize_providers(providers: dict) -> dict:
    """剥离 providers 配置里的明文密钥,只留 provider/model 选择给步骤进程。
    密钥由 ai_gateway 在调用时从 env 读取,绝不经 .{step}.config.json 落盘或代理。"""
    providers_map = providers.get("providers")
    if not isinstance(providers_map, dict):
        return copy.deepcopy(providers)
    clean = copy.deepcopy(providers)
    for cfg in clean["providers"].values():
        if isinstance(cfg, dict):
            for secret in _PROVIDER_SECRET_KEYS:
                cfg.pop(secret, None)
    return clean


def build_step_config(
    app_config: AppConfig,
    pipeline: str,
    step_name: str,
    domain: str = "general",
    style_tags: list[str] | None = None,
) -> dict:
    """Worker 调用:合并三层配置,返回传给步骤进程的 dict。"""
    pipeline_steps = app_config.pipelines[pipeline]["steps"]
    step_cfg = next(s for s in pipeline_steps if s["name"] == step_name)
    domain_cfg = load_domain_profile(app_config.config_dir, domain)

    step_node: dict = {
        "name": step_name,
        "pipeline": pipeline,
        "pool": step_cfg["pool"],
        "timeout_sec": step_cfg.get("timeout_sec", 600),
        "retries": step_cfg.get("retries", 0),
        # pipeline 定义版本(在 pipelines.yaml 维护,非代码):随 step def_digest 进步骤指纹。
        # 使用者在 YAML 给某步加/改 `version`(或改 ai 模型)即触发该步+下游重跑,无需改代码(见 step_base._def_digest)。
        "version": str(step_cfg.get("version", "1")),
    }
    if "capability_rules" in step_cfg:
        step_node["capability_rules"] = copy.deepcopy(step_cfg["capability_rules"])
    if "prompt_template" in step_cfg:
        step_node["prompt_template"] = step_cfg["prompt_template"]
    # 超时随媒体时长伸缩(可选):仅当 pipeline 给了 timeout_per_min 才透传,worker 跑步前据
    # input/metadata.json 的 duration_sec 算有效超时(见 worker.compute_effective_timeout)。
    # 缺省时不写这俩键,行为完全不变。
    if step_cfg.get("timeout_per_min"):
        step_node["timeout_per_min"] = int(step_cfg["timeout_per_min"])
        step_node["timeout_max_sec"] = int(step_cfg.get("timeout_max_sec", 0)) or None

    return {
        "step": step_node,
        "ai": step_cfg.get("ai", {}),
        "domain": {"name": domain, **domain_cfg},
        "style_tags": style_tags or [],
        "paths": {
            "data_dir": str(app_config.data_dir),
            "prompts_dir": str(app_config.prompts_dir),
            "config_dir": str(app_config.config_dir),
        },
        "providers": sanitize_providers(app_config.providers),
    }
