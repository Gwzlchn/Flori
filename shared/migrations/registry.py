"""注册当前程序支持的冻结迁移链。"""

from __future__ import annotations

from . import v0001_legacy_baseline as migration_v1
from . import v0002_immutable_ledger as migration_v2
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
    )
