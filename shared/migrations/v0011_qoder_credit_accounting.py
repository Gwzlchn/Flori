"""为 Qoder 调用增加独立 credit 计量,不把订阅额度伪装成美元成本。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from . import (
    v0001_legacy_baseline,
    v0006_concept_definition_history,
    v0010_concept_replay_states,
)

VERSION = 11
NAME = "qoder-credit-accounting"

_CREDIT_COLUMN = (
    "credits REAL CHECK("
    "(credits IS NULL OR provider IS 'qoder-cli') AND "
    "(provider IS NULL OR provider<>'qoder-cli' OR ("
    "typeof(cost_usd) IN ('integer','real') AND cost_usd=0)) AND "
    "(credits IS NULL OR (typeof(credits) IN ('integer','real') "
    "AND credits >= 0 AND credits <= 1.0e308)))"
)

CURRENT_SCHEMA_SQL = v0010_concept_replay_states.CURRENT_SCHEMA_SQL.replace(
    """    cached INTEGER DEFAULT 0,
    created_at TEXT NOT NULL
);""",
    """    cached INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    credits REAL CHECK(
        (credits IS NULL OR provider IS 'qoder-cli')
        AND (provider IS NULL OR provider<>'qoder-cli' OR (
            typeof(cost_usd) IN ('integer','real') AND cost_usd=0
        ))
        AND (credits IS NULL OR (
            typeof(credits) IN ('integer','real')
            AND credits >= 0 AND credits <= 1.0e308
        ))
    )
);""",
    1,
).replace(
    """    record_json TEXT,
    created_at TEXT NOT NULL
);""",
    """    record_json TEXT,
    created_at TEXT NOT NULL,
    credits REAL CHECK(
        (credits IS NULL OR provider IS 'qoder-cli')
        AND (provider IS NULL OR provider<>'qoder-cli' OR (
            typeof(cost_usd) IN ('integer','real') AND cost_usd=0
        ))
        AND (credits IS NULL OR (
            typeof(credits) IN ('integer','real')
            AND credits >= 0 AND credits <= 1.0e308
        ))
    )
);""",
    1,
)


def source_payload() -> str:
    return Path(__file__).read_text(encoding="utf-8")


def apply(connection: sqlite3.Connection) -> None:
    connection.execute(f"ALTER TABLE ai_usage ADD COLUMN {_CREDIT_COLUMN}")
    connection.execute(f"ALTER TABLE ai_task_logs ADD COLUMN {_CREDIT_COLUMN}")


def validate(connection: sqlite3.Connection) -> None:
    v0001_legacy_baseline._validate_complete_schema(connection, CURRENT_SCHEMA_SQL)
    v0006_concept_definition_history._replay_frozen_validator(
        connection, v0010_concept_replay_states.validate,
    )
    for table in ("ai_usage", "ai_task_logs"):
        invalid = connection.execute(
            f"""SELECT 1 FROM {table}
                WHERE (
                    credits IS NOT NULL AND (
                        typeof(credits) NOT IN ('integer','real')
                        OR credits < 0 OR credits > 1.0e308
                        OR provider IS NOT 'qoder-cli'
                    )
                ) OR (
                    provider='qoder-cli' AND (
                        typeof(cost_usd) NOT IN ('integer','real')
                        OR cost_usd<>0
                    )
                ) LIMIT 1"""
        ).fetchone()
        if invalid is not None:
            raise sqlite3.DatabaseError(f"{table} 的 AI 计量单位非法")
