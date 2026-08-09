"""为概念 occurrence 重放增加持久判定与重试状态,消除候选窗口饥饿。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from . import (
    v0001_legacy_baseline,
    v0006_concept_definition_history,
    v0009_concept_projection_ledger,
)

VERSION = 10
NAME = "concept-replay-retry-states"

# 与 shared.db._EMPTY_CONCEPT_PROJECTION_DIGEST 同值(sha256 of "[]")。
# 迁移 payload 冻结,不 import 运行时模块,常量在此固定为字面量。
_EMPTY_PROJECTION_DIGEST = (
    "sha256:4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"
)

# 回填行的哨兵时刻:epoch 恒小于任何真实 next_retry_at,升级后立即到期可重放。
_EPOCH_ISO = "1970-01-01T00:00:00+00:00"

REPLAY_STATE_SCHEMA_SQL = """
CREATE TABLE concept_occurrence_replay_state (
    job_id TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
    state TEXT NOT NULL CHECK(state IN ('verified_empty', 'retry')),
    reason TEXT NOT NULL CHECK(length(reason) BETWEEN 1 AND 64),
    source_digest TEXT CHECK(
        source_digest IS NULL OR (
            substr(source_digest, 1, 7)='sha256:'
            AND length(source_digest)=71
            AND substr(source_digest, 8) NOT GLOB '*[^0-9a-f]*'
        )
    ),
    attempt_count INTEGER NOT NULL CHECK(attempt_count >= 0),
    last_attempt_at TEXT,
    next_retry_at TEXT,
    updated_at TEXT NOT NULL,
    CHECK(
        (
            state='verified_empty'
            AND source_digest IS NOT NULL
            AND next_retry_at IS NULL
        )
        OR (state='retry' AND next_retry_at IS NOT NULL)
    )
);

CREATE INDEX idx_concept_replay_state_next_retry
    ON concept_occurrence_replay_state(state, next_retry_at);
""".strip()

CURRENT_SCHEMA_SQL = (
    v0009_concept_projection_ledger.CURRENT_SCHEMA_SQL
    + "\n\n"
    + REPLAY_STATE_SCHEMA_SQL
)


def source_payload() -> str:
    return Path(__file__).read_text(encoding="utf-8")


def apply(connection: sqlite3.Connection) -> None:
    v0001_legacy_baseline._execute_sql_script(connection, REPLAY_STATE_SCHEMA_SQL)
    # 存量空投影 marker 无法在迁移期读对象存储定性,统一回填为立即到期的
    # retry 状态。首轮重放后各自收敛:真源可读且确实为空 -> verified_empty;
    # 真源可读且有概念 -> 重放为非空并删本行;真源缺失 -> 有界退避继续重试。
    connection.execute(
        """INSERT INTO concept_occurrence_replay_state
           (job_id, state, reason, source_digest, attempt_count,
            last_attempt_at, next_retry_at, updated_at)
           SELECT job_id, 'retry', 'legacy_empty_projection', NULL, 0,
                  NULL, ?, ?
           FROM concept_occurrence_projection
           WHERE projection_digest = ?""",
        (_EPOCH_ISO, _EPOCH_ISO, _EMPTY_PROJECTION_DIGEST),
    )


def validate(connection: sqlite3.Connection) -> None:
    v0001_legacy_baseline._validate_complete_schema(connection, CURRENT_SCHEMA_SQL)
    v0006_concept_definition_history._replay_frozen_validator(
        connection, v0009_concept_projection_ledger.validate,
    )
    orphan = connection.execute(
        """SELECT job_id FROM concept_occurrence_replay_state
           WHERE NOT EXISTS (SELECT 1 FROM jobs WHERE jobs.id=job_id)
           LIMIT 1"""
    ).fetchone()
    if orphan is not None:
        raise sqlite3.DatabaseError(
            f"concept replay state has orphan job: {orphan[0]}"
        )
