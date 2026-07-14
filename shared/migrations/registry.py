"""注册当前程序支持的冻结迁移链。"""

from __future__ import annotations

from . import v0001_legacy_baseline as migration_v1
from . import v0002_immutable_ledger as migration_v2
from . import v0003_srs_consistency as migration_v3
from . import v0004_study_suggestions as migration_v4
from . import v0005_canonical_evidence as migration_v5
from . import v0006_concept_definition_history as migration_v6
from .runner import Migration


def migration_steps() -> tuple[Migration, ...]:
    """返回代码迁移注册表，DB 启动与灾备验证共用此入口。"""
    return (
        Migration(
            version=migration_v1.VERSION,
            name=migration_v1.NAME,
            payload=migration_v1.source_payload(),
            apply=migration_v1.apply,
            validate=migration_v1.validate,
        ),
        Migration(
            version=migration_v2.VERSION,
            name=migration_v2.NAME,
            payload=migration_v2.source_payload(),
            apply=migration_v2.apply,
            validate=migration_v2.validate,
        ),
        Migration(
            version=migration_v3.VERSION,
            name=migration_v3.NAME,
            payload=migration_v3.source_payload(),
            apply=migration_v3.apply,
            validate=migration_v3.validate,
        ),
        Migration(
            version=migration_v4.VERSION,
            name=migration_v4.NAME,
            payload=migration_v4.source_payload(),
            apply=migration_v4.apply,
            validate=migration_v4.validate,
        ),
        Migration(
            version=migration_v5.VERSION,
            name=migration_v5.NAME,
            payload=migration_v5.source_payload(),
            apply=migration_v5.apply,
            validate=migration_v5.validate,
        ),
        Migration(
            version=migration_v6.VERSION,
            name=migration_v6.NAME,
            payload=migration_v6.source_payload(),
            apply=migration_v6.apply,
            validate=migration_v6.validate,
        ),
    )
