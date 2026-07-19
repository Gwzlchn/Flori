"""注册当前程序支持的冻结迁移链。"""

from __future__ import annotations

from types import ModuleType

from . import v0001_legacy_baseline as migration_v1
from . import v0002_immutable_ledger as migration_v2
from . import v0003_srs_consistency as migration_v3
from . import v0004_study_suggestions as migration_v4
from . import v0005_canonical_evidence as migration_v5
from . import v0006_concept_definition_history as migration_v6
from . import v0007_unified_document as migration_v7
from . import v0008_multipart_jobs as migration_v8
from . import v0009_concept_projection_ledger as migration_v9
from .runner import Migration


# 链的唯一来源, 元组顺序即版本顺序. 新增迁移只在末尾追加并同步 manifest.json.
MIGRATION_MODULES: tuple[ModuleType, ...] = (
    migration_v1,
    migration_v2,
    migration_v3,
    migration_v4,
    migration_v5,
    migration_v6,
    migration_v7,
    migration_v8,
    migration_v9,
)


def migration_steps() -> tuple[Migration, ...]:
    """返回代码迁移注册表，DB 启动与灾备验证共用此入口。"""
    return tuple(
        Migration(
            version=module.VERSION,
            name=module.NAME,
            payload=module.source_payload(),
            apply=module.apply,
            validate=module.validate,
        )
        for module in MIGRATION_MODULES
    )


def current_migration_module() -> ModuleType:
    """返回链尾迁移模块, 它持有当前 schema 的 CURRENT_SCHEMA_SQL 和完整 validator.

    需要"当前 schema"的调用方走这个入口, 不要 import 具体的 vNNNN 模块,
    否则每次追加迁移都得改一遍链尾引用.
    """
    return MIGRATION_MODULES[-1]
