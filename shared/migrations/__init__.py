"""SQLite 迁移链与兼容门。

本模块的导出面必须保持精简: DR 恢复会用合成的最小 registry 加载归档里的迁移包,
这里多导出一个名字, 归档包就会因缺该名字而无法加载。新 helper 放 registry 并从那里导入。
"""

from .runner import (
    DEFAULT_MANIFEST_PATH,
    Migration,
    MigrationExecutionError,
    MigrationHistoryError,
    SchemaCompatibilityError,
    UnsupportedSchemaVersionError,
    assert_schema_compatible,
    current_schema_version,
    load_manifest,
    migration_manifest_fingerprint,
    run_migrations,
    validate_registry,
)
from .registry import migration_steps

__all__ = [
    "DEFAULT_MANIFEST_PATH",
    "Migration",
    "MigrationExecutionError",
    "MigrationHistoryError",
    "SchemaCompatibilityError",
    "UnsupportedSchemaVersionError",
    "assert_schema_compatible",
    "current_schema_version",
    "load_manifest",
    "migration_steps",
    "migration_manifest_fingerprint",
    "run_migrations",
    "validate_registry",
]
