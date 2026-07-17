"""加载文档体裁和解析能力注册表。"""

from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import yaml


class DocumentRegistryError(ValueError):
    """文档注册表不能形成完整且无歧义的公开契约。"""


_DEFAULT_PATH = Path(__file__).resolve().parents[1] / "configs" / "document_kinds.yaml"
_PROFILE_CAPABILITIES = {
    "html", "pdf", "math", "bibliography", "embedded_media",
    "text_layer", "ocr", "page_bbox",
}


def registry_path() -> Path:
    """返回运行配置中的文档注册表；缺失时回退镜像内置配置。"""
    config_dir = os.environ.get("CONFIG_DIR")
    configured = Path(config_dir) / "document_kinds.yaml" if config_dir else None
    return configured if configured is not None and configured.is_file() else _DEFAULT_PATH


def load_document_registry(path: str | Path | None = None) -> dict[str, Any]:
    source = Path(path) if path is not None else registry_path()
    try:
        raw = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise DocumentRegistryError(f"cannot load document registry: {source}") from exc
    _validate_registry(raw)
    return raw


def _validate_registry(raw: object) -> None:
    if not isinstance(raw, dict):
        raise DocumentRegistryError("document registry root must be a mapping")
    kinds = raw.get("document_kinds")
    profiles = raw.get("source_profiles")
    if not isinstance(kinds, dict) or not kinds or "unknown" not in kinds:
        raise DocumentRegistryError("document_kinds must include unknown")
    if not isinstance(profiles, dict) or not profiles:
        raise DocumentRegistryError("source_profiles must be a non-empty mapping")
    for name, spec in kinds.items():
        if not isinstance(name, str) or not name or not isinstance(spec, dict):
            raise DocumentRegistryError("invalid document kind")
        required = {"label", "description", "note_profile", "review_profile"}
        if not required <= set(spec) or any(not str(spec[key]).strip() for key in required):
            raise DocumentRegistryError(f"document_kinds.{name} misses required metadata")
    for name, spec in profiles.items():
        if not isinstance(spec, dict) or not str(spec.get("label") or "").strip():
            raise DocumentRegistryError(f"source_profiles.{name} misses label")
        capabilities = spec.get("capabilities")
        if not isinstance(capabilities, list) or not capabilities:
            raise DocumentRegistryError(f"source_profiles.{name} misses capabilities")
        unknown = set(capabilities) - _PROFILE_CAPABILITIES
        if unknown:
            raise DocumentRegistryError(
                f"source_profiles.{name} has unknown capabilities: {sorted(unknown)}"
            )


DOCUMENT_REGISTRY = load_document_registry()
DOCUMENT_KIND_SPECS: dict[str, dict[str, Any]] = DOCUMENT_REGISTRY["document_kinds"]
SOURCE_PROFILE_SPECS: dict[str, dict[str, Any]] = DOCUMENT_REGISTRY["source_profiles"]
DOCUMENT_KIND_NAMES = tuple(DOCUMENT_KIND_SPECS)
SOURCE_PROFILE_NAMES = tuple(SOURCE_PROFILE_SPECS)


def validate_document_kind(value: str | None) -> str:
    """返回已注册体裁；缺失用 unknown，显式未知值 fail-closed。"""
    if value is None or not str(value).strip():
        return "unknown"
    normalized = str(value).strip()
    if normalized not in DOCUMENT_KIND_SPECS:
        raise DocumentRegistryError(f"unsupported document_kind: {normalized}")
    return normalized


def document_catalog() -> dict[str, Any]:
    """返回 API 和前端可直接消费的稳定注册表投影。"""
    return {
        "document_kinds": [
            {"kind": name, **copy.deepcopy(spec)}
            for name, spec in DOCUMENT_KIND_SPECS.items()
        ],
        "source_profiles": [
            {"profile": name, **copy.deepcopy(spec)}
            for name, spec in SOURCE_PROFILE_SPECS.items()
        ],
    }
