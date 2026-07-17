"""加载并校验内容与订阅来源注册表。"""

from __future__ import annotations

import copy
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .document_registry import (
    DOCUMENT_KIND_NAMES,
    SOURCE_PROFILE_NAMES,
    document_catalog,
    validate_document_kind,
)


class SourceRegistryError(ValueError):
    """来源注册表或投递路由不满足完整性约束。"""


@dataclass(frozen=True)
class ResolvedJobRoute:
    """单次解析后的投递路由，供门禁和真实创建共用。"""

    source: str
    content_type: str
    document_kind: str | None
    pipeline: str
    source_profile: str | None


_DEFAULT_PATH = Path(__file__).resolve().parents[1] / "configs" / "sources.yaml"
_SLUG_STRATEGIES = {"plain", "youtube", "hash", "directory"}


def registry_path() -> Path:
    """返回当前来源配置路径;运行配置不存在时回退镜像内置配置。"""
    config_dir = os.environ.get("CONFIG_DIR")
    configured = Path(config_dir) / "sources.yaml" if config_dir else None
    return configured if configured is not None and configured.is_file() else _DEFAULT_PATH


def load_source_registry(path: str | Path | None = None) -> dict[str, Any]:
    """读取来源 YAML 并 fail-closed 校验跨消费方必需字段。"""
    source = Path(path) if path is not None else registry_path()
    try:
        raw = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise SourceRegistryError(f"cannot load source registry: {source}") from exc
    _validate_registry(raw)
    return raw


def _mapping(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key)
    if not isinstance(value, dict) or not value:
        raise SourceRegistryError(f"{key} must be a non-empty mapping")
    return value


def _validate_registry(raw: dict[str, Any]) -> None:
    if not isinstance(raw, dict):
        raise SourceRegistryError("source registry root must be a mapping")
    content_types = _mapping(raw, "content_types")
    job_sources = _mapping(raw, "job_sources")
    subscriptions = _mapping(raw, "subscription_sources")

    known_types = set(content_types)
    known_document_kinds = set(DOCUMENT_KIND_NAMES)
    seen_extensions: set[str] = set()
    seen_pipelines: set[str] = set()
    for name, spec in content_types.items():
        if not isinstance(spec, dict) or not spec.get("label") or not spec.get("pipeline"):
            raise SourceRegistryError(f"content_types.{name} misses label or pipeline")
        pipeline = str(spec["pipeline"])
        if pipeline in seen_pipelines:
            raise SourceRegistryError(f"duplicate content pipeline: {pipeline}")
        seen_pipelines.add(pipeline)
        extensions = spec.get("upload_extensions") or []
        if not isinstance(extensions, list):
            raise SourceRegistryError(f"content_types.{name}.upload_extensions must be a list")
        for extension in extensions:
            ext = str(extension).lower()
            if not ext.startswith(".") or ext in seen_extensions:
                raise SourceRegistryError(f"invalid or duplicate upload extension: {extension}")
            seen_extensions.add(ext)

    if "upload" not in job_sources or "other" not in job_sources:
        raise SourceRegistryError("job_sources must declare upload and other")
    for name, spec in job_sources.items():
        if not isinstance(spec, dict) or not spec.get("label"):
            raise SourceRegistryError(f"job_sources.{name} misses label")
        declared_types = spec.get("content_types") or []
        if not isinstance(declared_types, list):
            raise SourceRegistryError(f"job_sources.{name}.content_types must be a list")
        allowed = set(declared_types)
        if not allowed <= known_types:
            raise SourceRegistryError(f"job_sources.{name} references unknown content type")
        default = spec.get("default_content_type")
        if default is not None and default not in allowed:
            raise SourceRegistryError(f"job_sources.{name} has invalid default_content_type")
        kinds = spec.get("document_kinds") or []
        if not isinstance(kinds, list) or not set(kinds) <= known_document_kinds:
            raise SourceRegistryError(f"job_sources.{name} has invalid document_kinds")
        default_kind = spec.get("default_document_kind")
        if default_kind is not None and default_kind not in kinds:
            raise SourceRegistryError(f"job_sources.{name} has invalid default_document_kind")
        if kinds and "document" not in allowed:
            raise SourceRegistryError(
                f"job_sources.{name} declares document_kinds without document content type"
            )
        source_profile = spec.get("default_source_profile")
        if source_profile is not None and source_profile not in SOURCE_PROFILE_NAMES:
            raise SourceRegistryError(f"job_sources.{name} has invalid default_source_profile")
        patterns = spec.get("patterns") or []
        suffixes = spec.get("suffixes") or []
        if not isinstance(patterns, list) or not isinstance(suffixes, list):
            raise SourceRegistryError(f"job_sources.{name} patterns/suffixes must be lists")
        for pattern in patterns:
            try:
                re.compile(str(pattern), re.IGNORECASE)
            except re.error as exc:
                raise SourceRegistryError(f"job_sources.{name} has invalid regex") from exc
        for suffix in suffixes:
            if not str(suffix).startswith("."):
                raise SourceRegistryError(f"job_sources.{name} has invalid suffix")
    if job_sources["other"].get("creatable", True):
        raise SourceRegistryError("job_sources.other must be non-creatable")

    required = {
        "label", "group", "icon", "collection_prefix", "slug_strategy",
        "id_label", "placeholder", "hint",
    }
    for name, spec in subscriptions.items():
        if not isinstance(spec, dict) or not required <= set(spec):
            raise SourceRegistryError(f"subscription_sources.{name} misses required metadata")
        if spec["slug_strategy"] not in _SLUG_STRATEGIES:
            raise SourceRegistryError(f"subscription_sources.{name} has invalid slug strategy")


SOURCE_REGISTRY = load_source_registry()
CONTENT_TYPE_SPECS: dict[str, dict[str, Any]] = SOURCE_REGISTRY["content_types"]
JOB_SOURCE_SPECS: dict[str, dict[str, Any]] = SOURCE_REGISTRY["job_sources"]
SUBSCRIPTION_SOURCE_SPECS: dict[str, dict[str, Any]] = SOURCE_REGISTRY["subscription_sources"]
CONTENT_TYPE_NAMES = tuple(CONTENT_TYPE_SPECS)
SUBSCRIPTION_SOURCE_NAMES = tuple(SUBSCRIPTION_SOURCE_SPECS)


def detect_registered_source(value: str | None) -> str:
    """按 YAML 顺序识别投递来源;没有匹配时返回 fail-closed 的 other。"""
    if not value:
        return "other"
    suffix_target = value.lower().split("?", 1)[0]
    for name, spec in JOB_SOURCE_SPECS.items():
        if name in {"upload", "other"}:
            continue
        if any(re.search(pattern, value, re.IGNORECASE) for pattern in spec.get("patterns") or []):
            return name
        if any(suffix_target.endswith(str(suffix).lower()) for suffix in spec.get("suffixes") or []):
            return name
    return "other"


def content_type_for_filename(filename: str | None) -> str | None:
    """按 registry 扩展名识别上传类型;未知扩展名返回 None。"""
    name = (filename or "").lower()
    for content_type, spec in CONTENT_TYPE_SPECS.items():
        if any(name.endswith(str(ext).lower()) for ext in spec.get("upload_extensions") or []):
            return content_type
    return None


def default_content_type(source: str) -> str | None:
    spec = JOB_SOURCE_SPECS.get(source) or {}
    value = spec.get("default_content_type")
    return str(value) if value else None


def default_document_kind(source: str) -> str:
    """返回来源可证明的默认体裁；缺失时显式 unknown。"""
    spec = JOB_SOURCE_SPECS.get(source) or {}
    return validate_document_kind(spec.get("default_document_kind"))


def default_source_profile(source: str) -> str | None:
    spec = JOB_SOURCE_SPECS.get(source) or {}
    value = spec.get("default_source_profile")
    return str(value) if value else None


def pipeline_for_content_type(content_type: str) -> str | None:
    spec = CONTENT_TYPE_SPECS.get(content_type)
    return str(spec["pipeline"]) if spec else None


def resolve_job_route(
    source: str,
    content_type: str | None = None,
    *,
    document_kind: str | None = None,
    allow_internal: bool = False,
) -> ResolvedJobRoute:
    """默认值、校验和 pipeline/profile 必须在一处解析，避免前置门禁与创建分叉。"""
    resolved_type = content_type or default_content_type(source)
    resolved_kind = (
        validate_document_kind(
            document_kind if document_kind not in (None, "")
            else default_document_kind(source)
        )
        if resolved_type == "document"
        else None
    )
    validate_job_route(
        source,
        resolved_type,
        document_kind=resolved_kind,
        allow_internal=allow_internal,
    )
    pipeline = pipeline_for_content_type(str(resolved_type))
    if not pipeline:
        raise SourceRegistryError(f"content_type {resolved_type} has no pipeline")
    return ResolvedJobRoute(
        source=source,
        content_type=str(resolved_type),
        document_kind=resolved_kind,
        pipeline=pipeline,
        source_profile=default_source_profile(source),
    )


def validate_job_route(
    source: str,
    content_type: str | None,
    *,
    document_kind: str | None = None,
    allow_internal: bool = False,
) -> None:
    """保证来源可创建且内容类型有真实 pipeline,否则拒绝入队。"""
    source_spec = JOB_SOURCE_SPECS.get(source)
    internally_allowed = bool(
        allow_internal and source_spec is not None and source_spec.get("internal", False)
    )
    if source_spec is None or (
        not source_spec.get("creatable", True) and not internally_allowed
    ):
        raise SourceRegistryError(f"unsupported source: {source}")
    if not content_type or content_type not in CONTENT_TYPE_SPECS:
        raise SourceRegistryError(f"unsupported content_type: {content_type or '<missing>'}")
    if content_type not in (source_spec.get("content_types") or []):
        raise SourceRegistryError(
            f"source {source} does not support content_type {content_type}"
        )
    if not pipeline_for_content_type(content_type):
        raise SourceRegistryError(f"content_type {content_type} has no pipeline")
    if content_type == "document":
        try:
            kind = validate_document_kind(document_kind)
        except ValueError as exc:
            raise SourceRegistryError(str(exc)) from exc
        allowed_kinds = set(source_spec.get("document_kinds") or [])
        if kind not in allowed_kinds:
            raise SourceRegistryError(
                f"source {source} does not support document_kind {kind}"
            )
    elif document_kind not in (None, ""):
        raise SourceRegistryError("document_kind is only valid for document content")


def subscription_source_spec(source_type: str) -> dict[str, Any] | None:
    spec = SUBSCRIPTION_SOURCE_SPECS.get(source_type)
    return spec if spec is not None else None


def source_catalog() -> dict[str, Any]:
    """返回前端/OpenAPI 可消费的无检测实现细节视图。"""
    content_types = [
        {"type": name, **copy.deepcopy(spec)}
        for name, spec in CONTENT_TYPE_SPECS.items()
    ]
    job_sources = [
        {
            "type": name,
            "label": spec["label"],
            "content_types": list(spec.get("content_types") or []),
            "document_kinds": list(spec.get("document_kinds") or []),
            "default_document_kind": spec.get("default_document_kind"),
            "default_source_profile": spec.get("default_source_profile"),
            "creatable": bool(spec.get("creatable", True)),
        }
        for name, spec in JOB_SOURCE_SPECS.items()
    ]
    subscriptions = [
        {
            "type": name,
            **{
                key: copy.deepcopy(value)
                for key, value in spec.items()
                if key not in {"collection_prefix", "slug_strategy"}
            },
        }
        for name, spec in SUBSCRIPTION_SOURCE_SPECS.items()
    ]
    return {
        "content_types": content_types,
        "job_sources": job_sources,
        "subscription_sources": subscriptions,
        **document_catalog(),
    }
