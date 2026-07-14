"""SQLite 迁移链与兼容门。"""

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
