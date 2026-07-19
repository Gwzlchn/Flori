"""便携导入 journal 的独立 SQLite 账本(设计稿 05 号 §2.11-2/3 的最小落地形态)。

为什么不放进业务库:§2.10 阶段5 规定切换前失败要"丢弃新 DB/新 bucket staging"。
journal 若与被丢弃的库同生共死,恰好丢掉记录这次失败的证据;独立文件能在
目标库被丢弃后继续供人排查与 resume。§2.11 明确允许第一版这样落地。

它不是第二份业务状态:导入完成后 Job/Step 状态仍由当前 pipeline + manifest
投影产生(§2.9),这里只回答"这个 snapshot 在这台机器物化到哪一步"。
target_generation 是防串用的绑定键:换库后 generation 变化,旧 journal 立即失效。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

JOURNAL_FORMAT = "flori-content-import-journal/v1"
JOURNAL_USER_VERSION = 3

STATUS_PREPARING = "preparing"
STATUS_MATERIALIZING = "materializing"
STATUS_PROJECTING = "projecting"
STATUS_COMPLETE = "complete"
STATUS_FAILED = "failed"

_STATUSES = frozenset({
    STATUS_PREPARING, STATUS_MATERIALIZING, STATUS_PROJECTING,
    STATUS_COMPLETE, STATUS_FAILED,
})
_ACTIONS = frozenset({"insert", "noop", "conflict", "skip"})

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS content_imports (
    import_id TEXT PRIMARY KEY CHECK (length(trim(import_id)) > 0),
    snapshot_digest TEXT NOT NULL
        CHECK (length(snapshot_digest) = 71
               AND substr(snapshot_digest, 1, 7) = 'sha256:'),
    target_generation TEXT NOT NULL CHECK (length(trim(target_generation)) > 0),
    plan_digest TEXT NOT NULL
        CHECK (length(plan_digest) = 71 AND substr(plan_digest, 1, 7) = 'sha256:'),
    request_digest TEXT NOT NULL
        CHECK (length(request_digest) = 71 AND substr(request_digest, 1, 7) = 'sha256:'),
    -- 目标绑定:库被丢弃重建后 token 变化,旧进度立即失效(防"空库报成功")。
    target_db_path TEXT NOT NULL DEFAULT '',
    target_token TEXT NOT NULL DEFAULT '',
    target_storage TEXT NOT NULL DEFAULT '',
    mode TEXT NOT NULL DEFAULT 'empty' CHECK (mode IN ('empty', 'merge')),
    status TEXT NOT NULL
        CHECK (status IN ('preparing', 'materializing', 'projecting',
                          'complete', 'failed')),
    started_at TEXT NOT NULL CHECK (length(trim(started_at)) > 0),
    completed_at TEXT,
    summary TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(summary)),
    UNIQUE (snapshot_digest, target_generation)
);
CREATE INDEX IF NOT EXISTS idx_content_imports_status
    ON content_imports(status, started_at);

CREATE TABLE IF NOT EXISTS content_import_records (
    import_id TEXT NOT NULL
        REFERENCES content_imports(import_id) ON DELETE CASCADE,
    record_digest TEXT NOT NULL
        CHECK (length(record_digest) = 71
               AND substr(record_digest, 1, 7) = 'sha256:'),
    kind TEXT NOT NULL CHECK (length(trim(kind)) > 0),
    natural_key TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('insert', 'noop', 'conflict', 'skip')),
    processed_at TEXT NOT NULL CHECK (length(trim(processed_at)) > 0),
    PRIMARY KEY (import_id, record_digest)
);
CREATE INDEX IF NOT EXISTS idx_content_import_records_kind
    ON content_import_records(import_id, kind, action);
""".strip()


class ImportJournalError(RuntimeError):
    """journal 契约违规或身份不匹配;fail-closed。"""


@dataclass(frozen=True)
class ImportEntry:
    import_id: str
    snapshot_digest: str
    target_generation: str
    plan_digest: str
    request_digest: str
    mode: str
    status: str
    target_db_path: str
    target_token: str
    target_storage: str
    started_at: str
    completed_at: str | None
    summary: dict


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class ContentImportJournal:
    """独立 journal 文件的读写门面;用原始 sqlite3 连接,不经 shared/db.py facade。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._conn = sqlite3.connect(self.path, timeout=30)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(_SCHEMA_SQL)
        except sqlite3.DatabaseError as exc:
            # 裸 sqlite 错误对运维没有处置线索:journal 损坏时要说清是哪个文件、怎么办。
            raise ImportJournalError(
                f"journal {self.path} 无法打开或已损坏: {exc}; "
                "确认该路径是本工具的 journal 文件;若确已损坏,归档后删除该文件重跑一次全新导入"
            ) from exc
        version = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
        if version == 0:
            self._conn.execute(f"PRAGMA user_version={JOURNAL_USER_VERSION}")
        elif version == 2:
            # v2 条目可继续列出供审计,但缺少请求/存储身份,不得被新代码续跑。
            # 空字符串作为 legacy 哨兵,begin 会 fail-closed 要求新 generation。
            with self._conn:
                self._conn.execute(
                    "ALTER TABLE content_imports ADD COLUMN request_digest TEXT NOT NULL DEFAULT ''"
                )
                self._conn.execute(
                    "ALTER TABLE content_imports ADD COLUMN target_storage TEXT NOT NULL DEFAULT ''"
                )
                self._conn.execute(f"PRAGMA user_version={JOURNAL_USER_VERSION}")
        elif version != JOURNAL_USER_VERSION:
            self._conn.close()
            raise ImportJournalError(
                f"journal {self.path} 版本 v{version} 与本程序 v{JOURNAL_USER_VERSION} 不符"
            )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "ContentImportJournal":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


    def begin(
        self,
        *,
        import_id: str,
        snapshot_digest: str,
        target_generation: str,
        plan_digest: str,
        request_digest: str,
        target_db_path: str = "",
        target_token: str = "",
        mode: str = "empty",
    ) -> ImportEntry:
        """登记一次导入(阶段1);同 (snapshot, generation) 已存在则返回既有条目。

        既有条目的 plan_digest 不同意味着计划变了,resume 不能沿用旧进度,
        直接拒绝而不是悄悄接着跑。
        """
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            existing = self.find(snapshot_digest, target_generation)
            if existing is not None:
                if not existing.request_digest:
                    raise ImportJournalError(
                        f"journal 已有 legacy 导入 {existing.import_id},但没有目标请求身份;"
                        "使用新的 target_generation 重跑,旧条目保留供审计"
                    )
                if existing.request_digest != request_digest or existing.mode != mode:
                    raise ImportJournalError(
                        f"journal 已有同 snapshot/generation 导入 {existing.import_id},"
                        "但数据库、存储目标、模式或策略不同;"
                        "请使用新的 target_generation"
                    )
                if existing.plan_digest != plan_digest:
                    raise ImportJournalError(
                        f"journal 已有同目标导入 {existing.import_id},但 plan_digest 不同;"
                        "请使用新的 target_generation"
                    )
                self._conn.commit()
                return existing
            self._conn.execute(
                """INSERT INTO content_imports
                   (import_id, snapshot_digest, target_generation, plan_digest,
                    request_digest, target_db_path, target_token, target_storage,
                    mode, status, started_at, summary)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,'{}')""",
                (
                    import_id, snapshot_digest, target_generation, plan_digest,
                    request_digest, target_db_path, target_token, "", mode,
                    STATUS_PREPARING, _now(),
                ),
            )
            self._conn.commit()
            return self.get(import_id)
        except BaseException:
            if self._conn.in_transaction:
                self._conn.rollback()
            raise

    def get(self, import_id: str) -> ImportEntry:
        row = self._conn.execute(
            "SELECT * FROM content_imports WHERE import_id=?", (import_id,),
        ).fetchone()
        if row is None:
            raise ImportJournalError(f"journal 无此导入: {import_id}")
        return _entry(row)

    def list_all(self) -> list[ImportEntry]:
        """全部导入条目,按开始时间升序;供 --list-imports 在崩溃后排查。"""
        return [
            _entry(row) for row in self._conn.execute(
                "SELECT * FROM content_imports ORDER BY started_at, import_id"
            )
        ]

    def find(self, snapshot_digest: str, target_generation: str) -> ImportEntry | None:
        row = self._conn.execute(
            """SELECT * FROM content_imports
               WHERE snapshot_digest=? AND target_generation=?""",
            (snapshot_digest, target_generation),
        ).fetchone()
        return _entry(row) if row is not None else None

    def bind_target(
        self, import_id: str, target_db_path: str, token: str,
        target_storage: str,
    ) -> None:
        """把导入条目绑定到具体目标库;token 变了即代表库被换过/重建过。"""
        with self._conn:
            row = self._conn.execute(
                "SELECT target_db_path, target_token, target_storage FROM content_imports "
                "WHERE import_id=?", (import_id,),
            ).fetchone()
            if row is None:
                raise ImportJournalError(f"journal 无此导入: {import_id}")
            previous = tuple(str(row[key] or "") for key in (
                "target_db_path", "target_token", "target_storage",
            ))
            requested = (target_db_path, token, target_storage)
            if any(previous) and previous != requested:
                raise ImportJournalError(
                    f"journal 导入 {import_id} 已绑定另一目标;"
                    "拒绝改写数据库或存储身份"
                )
            self._conn.execute(
                """UPDATE content_imports
                   SET target_db_path=?, target_token=?, target_storage=?
                   WHERE import_id=?""",
                (*requested, import_id),
            )

    def set_status(
        self, import_id: str, status: str, *, summary: dict | None = None,
    ) -> None:
        if status not in _STATUSES:
            raise ImportJournalError(f"未知导入状态: {status}")
        completed = _now() if status in (STATUS_COMPLETE, STATUS_FAILED) else None
        with self._conn:
            if summary is None:
                self._conn.execute(
                    "UPDATE content_imports SET status=?, completed_at=? WHERE import_id=?",
                    (status, completed, import_id),
                )
            else:
                self._conn.execute(
                    """UPDATE content_imports
                       SET status=?, completed_at=?, summary=? WHERE import_id=?""",
                    (
                        status, completed,
                        json.dumps(summary, ensure_ascii=False, sort_keys=True),
                        import_id,
                    ),
                )


    def record_processed(
        self, import_id: str, *, record_digest: str, kind: str,
        natural_key: str, action: str,
    ) -> None:
        """登记一条已处理 record;重复登记幂等(resume 会重放同一条)。"""
        if action not in _ACTIONS:
            raise ImportJournalError(f"未知导入动作: {action}")
        with self._conn:
            self._conn.execute(
                """INSERT INTO content_import_records
                   (import_id, record_digest, kind, natural_key, action, processed_at)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(import_id, record_digest) DO UPDATE SET
                     kind=excluded.kind, natural_key=excluded.natural_key,
                     action=excluded.action, processed_at=excluded.processed_at""",
                (import_id, record_digest, kind, natural_key, action, _now()),
            )

    def processed_digests(self, import_id: str, kind: str | None = None) -> set[str]:
        """已处理 record digest 集合;resume 据此跳过已验证内容,不重复复制大文件。"""
        if kind is None:
            rows = self._conn.execute(
                "SELECT record_digest FROM content_import_records WHERE import_id=?",
                (import_id,),
            )
        else:
            rows = self._conn.execute(
                """SELECT record_digest FROM content_import_records
                   WHERE import_id=? AND kind=?""",
                (import_id, kind),
            )
        return {row[0] for row in rows}

    def processed_actions(self, import_id: str) -> dict[str, str]:
        """返回已处理 record 的动作;续跑校验必须区分 insert/noop/skip。"""
        return {
            str(row[0]): str(row[1])
            for row in self._conn.execute(
                "SELECT record_digest, action FROM content_import_records WHERE import_id=?",
                (import_id,),
            )
        }

    def action_counts(self, import_id: str) -> dict[str, int]:
        rows = self._conn.execute(
            """SELECT action, COUNT(*) FROM content_import_records
               WHERE import_id=? GROUP BY action""",
            (import_id,),
        )
        return {row[0]: int(row[1]) for row in rows}

    def clear_records(self, import_id: str) -> None:
        """丢弃本次进度(计划变更或显式重来时用);导入条目本身保留供审计。"""
        with self._conn:
            self._conn.execute(
                "DELETE FROM content_import_records WHERE import_id=?", (import_id,),
            )


def _entry(row: sqlite3.Row) -> ImportEntry:
    return ImportEntry(
        import_id=row["import_id"],
        snapshot_digest=row["snapshot_digest"],
        target_generation=row["target_generation"],
        plan_digest=row["plan_digest"],
        request_digest=row["request_digest"],
        mode=row["mode"],
        status=row["status"],
        target_db_path=row["target_db_path"],
        target_token=row["target_token"],
        target_storage=row["target_storage"],
        started_at=row["started_at"],
        completed_at=row["completed_at"],
        summary=json.loads(row["summary"]),
    )
