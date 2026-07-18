"""步骤语义定义摘要:AI/CPU 统一的 definition_digest 单一来源。

设计稿 §2.4:凡影响输出的定义字段必须进入摘要,纯运行字段(pool/timeout/retries/
队列路由等)必须排除。纳入与排除键集在本模块显式声明;归一化 step config 出现
未声明归属的键时 fail-closed 抛错,禁止静默漏出摘要。

输入契约:只接受 shared.config.normalize_pipeline 产出的归一化模板 config,或
shared.pipeline_scope.expand_pipeline_steps 的展开态(展开键在入口剥离并做身份
自洽校验,保证同一模板的各 Part 节点与模板本身摘要一致)。原始 YAML 键
(run/needs/timeout/retry)直接拒绝,避免 raw/normalized 双形态产生两个摘要。
"""

from __future__ import annotations

from typing import Mapping

# 字段别名单一来源在 shared.config(run->module 等);这里只用于识别并拒绝原始键。
from .config import _FIELD_ALIASES
from .step_manifest import (
    ManifestError,
    canonical_digest,
    ensure_no_secret_name,
    ensure_no_secret_text,
    validate_digest,
)
from .step_scope import execution_step_key, parse_execution_step, part_id_from_scope


SEMANTIC_DEFINITION_FORMAT = "flori-step-semantic-definition"
SEMANTIC_DEFINITION_FORMAT_VERSION = 1

# 纳入摘要的语义键(归一化字段名):步骤身份/实现/版本、scope、输出所有权、
# fan-in/依赖拓扑、rules 及其归一化 condition、能力降级规则、AI 路由、
# prompt 绑定与锁定、显式声明的工具链语义版本。
SEMANTIC_STEP_KEYS = frozenset({
    "name", "module", "version", "scope", "outputs", "output_policy",
    "fan_in", "depends_on", "rules", "condition", "capability_rules", "ai",
    "prompt_template", "prompt_locked", "toolchain",
})

# 排除的纯运行键(设计稿 §2.4 排除清单 + 现有 pipelines.yaml 全部运行字段):
# 改这些只影响怎么跑,不影响产物内容。image 默认排除,镜像 digest 留 producer 审计。
RUNTIME_STEP_KEYS = frozenset({
    "label", "pool", "timeout_sec", "timeout_per_min", "timeout_max_sec",
    "retries", "tags", "image", "on_complete", "weight", "concurrency",
    "worker_id", "variables", "extends",
})

# expand_pipeline_steps 注入的展开键:属运行节点身份,不属语义定义。
# 入口剥离并校验自洽,保证 part1 == part2 == 模板 三者摘要相等。
_EXPANSION_KEYS = ("template_step", "scope_key", "part_id", "part_index")

_PROMPT_KEYS = frozenset({"template", "version", "sha256"})

# 语义槽位固定全集:缺省键以 None/空集合占位,保证"新增可选语义字段但未使用"不抖动摘要。
_SEMANTIC_DEFAULTS: dict[str, object] = {
    "module": None,
    "version": "1",
    "scope": "job",
    "outputs": [],
    "output_policy": None,
    "fan_in": [],
    "depends_on": [],
    "rules": None,
    "condition": None,
    "capability_rules": None,
    "ai": None,
    "prompt_template": None,
    "prompt_locked": False,
}


class SemanticDefinitionError(ValueError):
    """语义定义构建违规;未知字段/密钥样式/绝对路径一律 fail-closed。"""


def _ensure_semantic_payload(value: object, field: str) -> None:
    """语义载荷四重门:JSON-safe、无 lone surrogate、无密钥样式、无绝对路径。递归覆盖嵌套结构。"""
    if value is None or isinstance(value, (bool, int, float)):
        return
    if isinstance(value, str):
        if any(0xD800 <= ord(ch) <= 0xDFFF for ch in value):
            raise SemanticDefinitionError(f"{field}: lone surrogate is not allowed")
        try:
            ensure_no_secret_text(value, field)
        except ManifestError as exc:
            raise SemanticDefinitionError(str(exc)) from exc
        if value.startswith("/"):
            raise SemanticDefinitionError(
                f"{field}: absolute path must not enter semantic definition: {value!r}"
            )
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if type(key) is not str:
                raise SemanticDefinitionError(f"{field}: dict key must be str")
            if any(0xD800 <= ord(ch) <= 0xDFFF for ch in key):
                raise SemanticDefinitionError(f"{field}: lone surrogate in dict key")
            try:
                ensure_no_secret_name(key, field)
            except ManifestError as exc:
                raise SemanticDefinitionError(str(exc)) from exc
            _ensure_semantic_payload(item, f"{field}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _ensure_semantic_payload(item, f"{field}[{index}]")
        return
    raise SemanticDefinitionError(
        f"{field}: unsupported type {type(value).__name__} in semantic definition"
    )


def _validate_digest_map(mapping: object, field: str) -> dict[str, str]:
    if mapping is None:
        return {}
    if not isinstance(mapping, Mapping):
        raise SemanticDefinitionError(f"{field}: must be a str->digest map")
    result: dict[str, str] = {}
    for key, value in mapping.items():
        if type(key) is not str or not key or len(key) > 200:
            raise SemanticDefinitionError(f"{field}: invalid key")
        try:
            validate_digest(value, f"{field}[{key}]")
        except ManifestError as exc:
            raise SemanticDefinitionError(str(exc)) from exc
        result[key] = value
    return result


def _validate_version_map(mapping: object, field: str) -> dict[str, str]:
    if mapping is None:
        return {}
    if not isinstance(mapping, Mapping):
        raise SemanticDefinitionError(f"{field}: must be a str->str map")
    result: dict[str, str] = {}
    for key, value in mapping.items():
        if type(key) is not str or not key or len(key) > 200:
            raise SemanticDefinitionError(f"{field}: invalid key")
        if type(value) is not str or not value or len(value) > 200:
            raise SemanticDefinitionError(f"{field}[{key}]: invalid value")
        _ensure_semantic_payload({key: value}, field)
        result[key] = value
    return result


def _validate_prompt(prompt: object) -> dict[str, str] | None:
    """resolved prompt 只进摘要三元组(template/version/sha256),正文留审计位置。"""
    if prompt is None:
        return None
    if not isinstance(prompt, Mapping):
        raise SemanticDefinitionError("prompt: must be a map")
    keys = set(prompt)
    if keys != _PROMPT_KEYS:
        raise SemanticDefinitionError(
            f"prompt: keys must be exactly {sorted(_PROMPT_KEYS)}, got {sorted(keys)}"
        )
    template = prompt["template"]
    version = prompt["version"]
    if type(template) is not str or not template:
        raise SemanticDefinitionError("prompt.template: must be a non-empty str")
    if type(version) is not str or not version:
        raise SemanticDefinitionError("prompt.version: must be a non-empty str")
    try:
        validate_digest(prompt["sha256"], "prompt.sha256")
    except ManifestError as exc:
        raise SemanticDefinitionError(str(exc)) from exc
    return {"template": template, "version": version, "sha256": prompt["sha256"]}


def _strip_expansion(normalized: dict[str, object]) -> None:
    """剥离展开键并校验身份自洽,把 name 还原为模板 step 名。

    expand_pipeline_steps 会把 name 改写成执行键(part:pt_x::01_download)并注入
    template_step/scope_key/part_id/part_index;语义定义必须以模板身份计算,
    否则同一定义按 Part 产生 N 个摘要,rerun/复用判定全部失效。
    """
    expansion = {key: normalized.pop(key) for key in _EXPANSION_KEYS if key in normalized}
    if not expansion:
        return
    template_step = expansion.get("template_step")
    scope_key = expansion.get("scope_key")
    if type(template_step) is not str or not template_step or type(scope_key) is not str:
        raise SemanticDefinitionError(
            "step_config: expanded config requires template_step and scope_key together"
        )
    try:
        expected_name = execution_step_key(scope_key, template_step)
        derived_part = part_id_from_scope(scope_key)
    except ValueError as exc:
        raise SemanticDefinitionError(f"step_config: invalid expansion identity: {exc}") from exc
    # 断言 name 与 template_step 严格对应:job scope 下执行键即模板名,
    # part scope 下为 part:{id}::step;错拼展开直接拒绝。
    if normalized.get("name") != expected_name:
        raise SemanticDefinitionError(
            f"step_config: expanded name {normalized.get('name')!r} does not match "
            f"template_step {template_step!r} under {scope_key!r}"
        )
    if derived_part is None:
        if expansion.get("part_id") is not None or expansion.get("part_index") is not None:
            raise SemanticDefinitionError(
                "step_config: job scope expansion must not carry part identity"
            )
    else:
        if expansion.get("part_id") != derived_part:
            raise SemanticDefinitionError("step_config: expansion part_id does not match scope_key")
        part_index = expansion.get("part_index")
        if type(part_index) is not int or part_index < 1:
            raise SemanticDefinitionError("step_config: expansion part_index must be int >= 1")
    normalized["name"] = template_step


def _descope_depends_on(
    depends_on: object, fan_in: list[str], field: str,
) -> list[str]:
    """依赖列表还原为模板 step 名:去 scope 前缀、剔除 fan-in 展开边、保序去重。

    展开态的依赖是执行键(part:pt::step),且 job 步的 fan-in 会按 Part 数展开进
    depends_on;fan-in 语义由 fan_in 字段自身表达,留在这里会随 Part 数抖动摘要。
    """
    if depends_on is None:
        return []
    if not isinstance(depends_on, list):
        raise SemanticDefinitionError(f"{field}: must be a list")
    result: list[str] = []
    for entry in depends_on:
        if type(entry) is not str or not entry:
            raise SemanticDefinitionError(f"{field}: invalid dependency entry")
        try:
            _, step_name = parse_execution_step(entry)
        except ValueError as exc:
            raise SemanticDefinitionError(f"{field}: invalid dependency {entry!r}") from exc
        if step_name in fan_in:
            continue
        if step_name not in result:
            result.append(step_name)
    return result


def build_step_semantic_definition(
    *,
    pipeline: str,
    step_config: Mapping[str, object],
    prompt: Mapping[str, object] | None = None,
    config_digests: Mapping[str, str] | None = None,
    toolchain: Mapping[str, str] | None = None,
) -> dict:
    """从归一化 step config 与解析后的安全上下文构建语义定义(canonical-ready dict)。

    prompt/config_digests/toolchain 是运行时解析产物,由集成层传入:
    - prompt: resolved 模板的 {template, version, sha256};
    - config_digests: 影响输出的 domain/profile/style/config 摘要 map;
    - toolchain: 显式声明的工具链语义版本(与 step config 内 toolchain 键合并,重名报错)。
    未声明归属的 step config 键直接抛 SemanticDefinitionError。
    """
    if type(pipeline) is not str or not pipeline:
        raise SemanticDefinitionError("pipeline: must be a non-empty str")
    _ensure_semantic_payload(pipeline, "pipeline")
    if not isinstance(step_config, Mapping):
        raise SemanticDefinitionError("step_config: must be a map")

    normalized: dict[str, object] = {}
    for key, value in step_config.items():
        if type(key) is not str:
            raise SemanticDefinitionError("step_config: keys must be str")
        if key in _FIELD_ALIASES:
            raise SemanticDefinitionError(
                f"step_config: raw pipeline key {key!r}; pass the normalized template "
                f"config (shared.config.normalize_pipeline output uses "
                f"{_FIELD_ALIASES[key]!r})"
            )
        normalized[key] = value

    _strip_expansion(normalized)

    unclassified = sorted(set(normalized) - SEMANTIC_STEP_KEYS - RUNTIME_STEP_KEYS)
    if unclassified:
        raise SemanticDefinitionError(
            f"step_config: keys without declared semantic ownership: {unclassified}; "
            "add them to SEMANTIC_STEP_KEYS or RUNTIME_STEP_KEYS explicitly"
        )

    step_name = normalized.get("name")
    if type(step_name) is not str or not step_name:
        raise SemanticDefinitionError("step_config.name: must be a non-empty str")

    definition: dict[str, object] = {}
    for key, default in _SEMANTIC_DEFAULTS.items():
        definition[key] = normalized.get(key, default)
    # version 语义与既有 def_digest 对齐:缺省 "1",数字与字符串等价归一为 str。
    version = definition["version"]
    definition["version"] = str(version) if version is not None else "1"
    prompt_locked = definition["prompt_locked"]
    if type(prompt_locked) is not bool:
        raise SemanticDefinitionError("step_config.prompt_locked: must be bool")
    fan_in = definition["fan_in"]
    if not isinstance(fan_in, list) or any(type(item) is not str for item in fan_in):
        raise SemanticDefinitionError("step_config.fan_in: must be a list of str")
    definition["depends_on"] = _descope_depends_on(
        definition["depends_on"], fan_in, "step_config.depends_on",
    )
    _ensure_semantic_payload(definition, "step_config")

    config_toolchain = _validate_version_map(normalized.get("toolchain"), "step_config.toolchain")
    declared_toolchain = _validate_version_map(toolchain, "toolchain")
    overlap = sorted(set(config_toolchain) & set(declared_toolchain))
    if overlap:
        raise SemanticDefinitionError(f"toolchain: conflicting declarations for {overlap}")
    merged_toolchain = {**config_toolchain, **declared_toolchain}

    return {
        "format": SEMANTIC_DEFINITION_FORMAT,
        "format_version": SEMANTIC_DEFINITION_FORMAT_VERSION,
        "pipeline": pipeline,
        "step": step_name,
        "definition": definition,
        "prompt": _validate_prompt(prompt),
        "config_digests": _validate_digest_map(config_digests, "config_digests"),
        "toolchain": merged_toolchain,
    }


def step_semantic_definition_digest(
    *,
    pipeline: str,
    step_config: Mapping[str, object],
    prompt: Mapping[str, object] | None = None,
    config_digests: Mapping[str, str] | None = None,
    toolchain: Mapping[str, str] | None = None,
) -> str:
    """语义定义的 canonical 摘要;写入 manifest.compatibility.definition_digest。

    worker 在派发前按模板 config 计算并随 step_cfg 注入;步骤子进程与 scheduler
    对账都消费该值,不各自重算(分工见 shared.step_manifest 模块 docstring)。
    """
    definition = build_step_semantic_definition(
        pipeline=pipeline,
        step_config=step_config,
        prompt=prompt,
        config_digests=config_digests,
        toolchain=toolchain,
    )
    try:
        return canonical_digest(definition)
    except ManifestError as exc:
        # 双保险:构建层校验已覆盖,canonical 层若仍拒绝按本模块错误类型上抛。
        raise SemanticDefinitionError(str(exc)) from exc
