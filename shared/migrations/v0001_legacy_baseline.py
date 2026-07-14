"""冻结旧库到首个可追踪 schema 的完整迁移。"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

VERSION = 1
NAME = "legacy-baseline"


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    content_type TEXT NOT NULL,
    pipeline TEXT NOT NULL,
    collection_id TEXT,
    url TEXT,
    title TEXT,
    domain TEXT NOT NULL DEFAULT 'general',
    source TEXT,
    style_tags TEXT DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'pending',
    progress_pct INTEGER DEFAULT 0,
    meta TEXT DEFAULT '{}',
    published_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    error TEXT,
    lineage_key TEXT,
    is_current INTEGER NOT NULL DEFAULT 1,
    source_digest TEXT,
    pipeline_digest TEXT,
    parent_job_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_lineage ON jobs(lineage_key);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_collection ON jobs(collection_id);

CREATE TABLE IF NOT EXISTS job_steps (
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    step TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'waiting',
    pool TEXT NOT NULL DEFAULT '',
    input_hash TEXT,
    worker_id TEXT,
    started_at TEXT,
    finished_at TEXT,
    duration_sec REAL,
    meta TEXT,
    error TEXT,
    retries INTEGER DEFAULT 0,
    PRIMARY KEY (job_id, step)
);

CREATE TABLE IF NOT EXISTS workers (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    pools TEXT NOT NULL DEFAULT '[]',
    tags TEXT NOT NULL DEFAULT '[]',
    reject_tags TEXT NOT NULL DEFAULT '[]',
    hostname TEXT,
    gpu_name TEXT,
    gpu_memory_mb INTEGER,
    concurrency INTEGER NOT NULL DEFAULT 1,
    remote_addr TEXT,
    status TEXT NOT NULL DEFAULT 'offline',
    admin_status TEXT NOT NULL DEFAULT '',
    current_job TEXT,
    current_step TEXT,
    tasks_completed INTEGER DEFAULT 0,
    tasks_failed INTEGER DEFAULT 0,
    total_duration_sec REAL DEFAULT 0,
    desired_config TEXT,
    cfg_rev INTEGER NOT NULL DEFAULT 0,
    first_seen TEXT NOT NULL,
    started_at TEXT,
    last_heartbeat TEXT,
    admin_note TEXT
);

CREATE TABLE IF NOT EXISTS ai_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    exec_id TEXT NOT NULL UNIQUE,
    job_id TEXT,
    step TEXT,
    worker_id TEXT,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_creation_input_tokens INTEGER DEFAULT 0,
    cache_read_input_tokens INTEGER DEFAULT 0,
    cost_usd REAL DEFAULT 0,
    duration_sec REAL DEFAULT 0,
    num_turns INTEGER DEFAULT 0,
    cached INTEGER DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ai_usage_job ON ai_usage(job_id);
CREATE INDEX IF NOT EXISTS idx_ai_usage_provider ON ai_usage(provider);

CREATE TABLE IF NOT EXISTS ai_task_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    exec_id TEXT,
    step_name TEXT,
    domain TEXT,
    provider TEXT,
    model TEXT,
    ok INTEGER DEFAULT 1,
    error TEXT,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_creation_input_tokens INTEGER DEFAULT 0,
    cache_read_input_tokens INTEGER DEFAULT 0,
    cost_usd REAL DEFAULT 0,
    duration_sec REAL DEFAULT 0,
    num_turns INTEGER DEFAULT 0,
    record_json TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ai_task_logs_task ON ai_task_logs(task_id);

CREATE TABLE IF NOT EXISTS prompt_overrides (
    scope TEXT NOT NULL DEFAULT 'global',
    domain TEXT NOT NULL DEFAULT '',
    pipeline TEXT NOT NULL,
    step TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (scope, domain, pipeline, step)
);

CREATE TABLE IF NOT EXISTS prompt_override_versions (
    scope TEXT NOT NULL DEFAULT 'global',
    domain TEXT NOT NULL DEFAULT '',
    pipeline TEXT NOT NULL,
    step TEXT NOT NULL,
    version INTEGER NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    PRIMARY KEY (scope, domain, pipeline, step, version)
);

CREATE TABLE IF NOT EXISTS collections (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    domain TEXT NOT NULL,
    description TEXT DEFAULT '',
    tags TEXT DEFAULT '[]',
    job_count INTEGER DEFAULT 0,
    source_type TEXT,
    source_id TEXT,
    sync_enabled INTEGER NOT NULL DEFAULT 1,
    last_synced_at TEXT,
    last_sync_status TEXT,
    last_sync_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS worker_tokens (
    token_hash TEXT PRIMARY KEY,
    worker_id TEXT NOT NULL,
    pools TEXT NOT NULL DEFAULT '[]',
    tags TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    last_used TEXT,
    revoked INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_worker_tokens_worker ON worker_tokens(worker_id);

CREATE TABLE IF NOT EXISTS app_credentials (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS ingested_items (
    collection_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    PRIMARY KEY (collection_id, item_id)
);

CREATE TABLE IF NOT EXISTS glossary (
    domain TEXT NOT NULL,
    term TEXT NOT NULL,
    definition TEXT DEFAULT '',
    zh_name TEXT DEFAULT '',
    aliases TEXT DEFAULT '[]',
    occurrences TEXT DEFAULT '[]',
    related TEXT DEFAULT '[]',
    status TEXT DEFAULT 'accepted',
    watched INTEGER DEFAULT 0,
    is_topic INTEGER DEFAULT 0,
    definition_locked INTEGER DEFAULT 0,
    created_at TEXT,
    updated_at TEXT,
    PRIMARY KEY (domain, term)
);
CREATE INDEX IF NOT EXISTS idx_glossary_domain_status ON glossary(domain, status);

CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts5 USING fts5(
    job_id UNINDEXED,
    content_type UNINDEXED,
    note_type UNINDEXED,
    collection_id UNINDEXED,
    domain UNINDEXED,
    title,
    body,
    tokenize='trigram'
);

CREATE TABLE IF NOT EXISTS note_chunks (
    chunk_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    note_type TEXT NOT NULL,
    content_type TEXT NOT NULL DEFAULT '',
    collection_id TEXT NOT NULL DEFAULT '',
    domain TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    section TEXT NOT NULL DEFAULT '',
    chunk_index INTEGER NOT NULL,
    char_start INTEGER NOT NULL DEFAULT 0,
    char_end INTEGER NOT NULL DEFAULT 0,
    body TEXT NOT NULL DEFAULT '',
    evidence_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(job_id, note_type, chunk_index)
);
CREATE INDEX IF NOT EXISTS idx_note_chunks_job ON note_chunks(job_id);
CREATE INDEX IF NOT EXISTS idx_note_chunks_domain ON note_chunks(domain);

CREATE VIRTUAL TABLE IF NOT EXISTS note_chunks_fts5 USING fts5(
    chunk_id UNINDEXED,
    job_id UNINDEXED,
    note_type UNINDEXED,
    content_type UNINDEXED,
    collection_id UNINDEXED,
    domain UNINDEXED,
    title,
    section,
    body,
    evidence_json UNINDEXED,
    tokenize='trigram'
);

CREATE TABLE IF NOT EXISTS study_cards (
    card_id TEXT PRIMARY KEY,
    domain TEXT NOT NULL DEFAULT 'general',
    job_id TEXT,
    concept_term TEXT,
    card_type TEXT NOT NULL DEFAULT 'basic',
    front TEXT NOT NULL,
    back TEXT NOT NULL,
    explanation TEXT NOT NULL DEFAULT '',
    evidence_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'active',
    source TEXT NOT NULL DEFAULT 'manual',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_study_cards_domain ON study_cards(domain);
CREATE INDEX IF NOT EXISTS idx_study_cards_status ON study_cards(status);
CREATE INDEX IF NOT EXISTS idx_study_cards_job ON study_cards(job_id);

CREATE TABLE IF NOT EXISTS study_reviews (
    card_id TEXT PRIMARY KEY REFERENCES study_cards(card_id) ON DELETE CASCADE,
    due_at TEXT NOT NULL,
    interval_days REAL NOT NULL DEFAULT 0,
    ease REAL NOT NULL DEFAULT 2.5,
    repetitions INTEGER NOT NULL DEFAULT 0,
    lapses INTEGER NOT NULL DEFAULT 0,
    last_grade TEXT,
    last_reviewed_at TEXT,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_study_reviews_due ON study_reviews(due_at);

CREATE TABLE IF NOT EXISTS study_review_logs (
    id TEXT PRIMARY KEY,
    card_id TEXT NOT NULL REFERENCES study_cards(card_id) ON DELETE CASCADE,
    grade TEXT NOT NULL,
    reviewed_at TEXT NOT NULL,
    response_ms INTEGER,
    scheduled_due_at TEXT,
    next_due_at TEXT NOT NULL,
    interval_days REAL NOT NULL,
    ease REAL NOT NULL,
    repetitions INTEGER NOT NULL,
    lapses INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_study_review_logs_card ON study_review_logs(card_id);
"""


EXPECTED_COLUMNS: dict[str, dict[str, str]] = {
    "jobs": {
        "collection_id": "collection_id TEXT",
        "source": "source TEXT",
        "published_at": "published_at TEXT",
        "lineage_key": "lineage_key TEXT",
        "is_current": "is_current INTEGER NOT NULL DEFAULT 1",
        "source_digest": "source_digest TEXT",
        "pipeline_digest": "pipeline_digest TEXT",
        "parent_job_id": "parent_job_id TEXT",
    },
    "job_steps": {"retries": "retries INTEGER DEFAULT 0"},
    "ai_usage": {
        "worker_id": "worker_id TEXT",
        "cache_creation_input_tokens": "cache_creation_input_tokens INTEGER DEFAULT 0",
        "cache_read_input_tokens": "cache_read_input_tokens INTEGER DEFAULT 0",
        "num_turns": "num_turns INTEGER DEFAULT 0",
    },
    "workers": {
        "reject_tags": "reject_tags TEXT NOT NULL DEFAULT '[]'",
        "admin_note": "admin_note TEXT",
        "admin_status": "admin_status TEXT NOT NULL DEFAULT ''",
        "concurrency": "concurrency INTEGER NOT NULL DEFAULT 1",
        "remote_addr": "remote_addr TEXT",
        "desired_config": "desired_config TEXT",
        "cfg_rev": "cfg_rev INTEGER NOT NULL DEFAULT 0",
    },
    "collections": {
        "source_type": "source_type TEXT",
        "source_id": "source_id TEXT",
        "sync_enabled": "sync_enabled INTEGER NOT NULL DEFAULT 1",
        "last_synced_at": "last_synced_at TEXT",
        "last_sync_status": "last_sync_status TEXT",
        "last_sync_error": "last_sync_error TEXT",
    },
    "glossary": {
        "occurrences": "occurrences TEXT DEFAULT '[]'",
        "zh_name": "zh_name TEXT DEFAULT ''",
        "aliases": "aliases TEXT DEFAULT '[]'",
        "watched": "watched INTEGER DEFAULT 0",
        "is_topic": "is_topic INTEGER DEFAULT 0",
        "definition_locked": "definition_locked INTEGER DEFAULT 0",
    },
    "prompt_overrides": {"version": "version INTEGER NOT NULL DEFAULT 1"},
}


LEGACY_PRESERVED_TABLES = {
    "glossary_bak_clean_20260617": (
        "CREATE TABLE glossary_bak_clean_20260617("
        "domain TEXT, term TEXT, definition TEXT, related TEXT, status TEXT, "
        "created_at TEXT, updated_at TEXT, occurrences TEXT, is_topic INT, "
        "definition_locked INT)"
    ),
}

_IGNORED_SQLITE_STAT_TABLES = frozenset({"sqlite_stat1", "sqlite_stat4"})


def source_payload() -> str:
    """整个版本模块即不可变 payload，SQL 或回填算法改动都会改 checksum。"""
    return Path(__file__).read_text(encoding="utf-8")


def _short_hash(value: str | None) -> str:
    return hashlib.sha1((value or "").encode()).hexdigest()[:10]


_FROZEN_SOURCE_RULES = (
    (
        "bilibili",
        (r"bilibili\.com", r"^BV[a-zA-Z0-9]{10}$", r"b23\.tv"),
        (),
    ),
    ("youtube", (r"youtube\.com", r"youtu\.be"), ()),
    ("arxiv", (r"arxiv\.org",), ()),
    ("pdf", (), (".pdf",)),
    ("podcast", (), (".mp3", ".m4a", ".wav", ".aac", ".flac")),
    ("local_file", (r"^file://",), ()),
    ("http_article", (r"^https?://",), ()),
)


def _frozen_detect_source(value: str) -> str:
    """冻结执行基线的 registry 顺序与 pattern/suffix 识别语义。"""
    suffix_target = value.lower().split("?", 1)[0]
    for name, patterns, suffixes in _FROZEN_SOURCE_RULES:
        if any(re.search(pattern, value, re.I) for pattern in patterns):
            return name
        if any(suffix_target.endswith(suffix) for suffix in suffixes):
            return name
    return "other"


def _frozen_lineage_key(
    url: str, content_type: str | None, source: str | None
) -> str:
    """固化 v1 时的 URL 归一逻辑，不依赖会继续演进的 ids/source registry。"""
    detected = source or _frozen_detect_source(url)
    if detected == "bilibili":
        match = re.search(r"(BV[A-Za-z0-9]{10})", url)
        return f"jobs_bili_{match.group(1) if match else _short_hash(url)}"
    if detected == "youtube":
        match = re.search(
            r"(?:v=|youtu\.be/|/shorts/|/embed/)([A-Za-z0-9_-]{11})", url
        )
        return f"jobs_yt_{match.group(1) if match else _short_hash(url)}"
    if detected == "arxiv":
        match = re.search(
            r"(?:arxiv\.org/(?:abs|pdf)/)?"
            r"(\d+\.\d+(?:v\d+)?|[a-z-]+(?:\.[A-Za-z]{2})?/\d{7}(?:v\d+)?)",
            url,
            re.I,
        )
        return f"jobs_arxiv_{match.group(1) if match else _short_hash(url)}"
    if detected == "podcast":
        return f"jobs_audio_{_short_hash(url)}"
    if detected == "http_article":
        return f"jobs_article_{_short_hash(url)}"
    prefix = {
        "video": "video",
        "article": "article",
        "paper": "paper",
        "audio": "audio",
    }.get(content_type or "", content_type or "x")
    return f"jobs_{prefix}_{_short_hash(url)}"


def _execute_sql_script(connection: sqlite3.Connection, script: str) -> None:
    pending = ""
    for line in script.splitlines(keepends=True):
        pending += line
        if not sqlite3.complete_statement(pending):
            continue
        statement = pending.strip()
        pending = ""
        if statement:
            connection.execute(statement)
    if pending.strip():
        raise sqlite3.OperationalError("迁移 SQL 存在未完整语句")


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _normalize_sql(value: str | None) -> str:
    """归一 SQL token 与标点空白,移除注释并保留 quoted 内容。"""
    source = (value or "").strip()
    normalized: list[str] = []
    pending_space = False
    index = 0
    while index < len(source):
        char = source[index]
        if char.isspace():
            pending_space = True
            index += 1
            continue
        if source.startswith("--", index):
            newline = source.find("\n", index + 2)
            index = len(source) if newline < 0 else newline + 1
            pending_space = True
            continue
        if source.startswith("/*", index):
            closing_comment = source.find("*/", index + 2)
            index = len(source) if closing_comment < 0 else closing_comment + 2
            pending_space = True
            continue
        if char in {"(", ")", ","}:
            if normalized and normalized[-1] == " ":
                normalized.pop()
            normalized.append(char)
            pending_space = False
            index += 1
            continue
        if (
            pending_space
            and normalized
            and normalized[-1] not in {"(", ","}
        ):
            normalized.append(" ")
        pending_space = False
        if char not in {"'", '"', "`", "["}:
            normalized.append(char.casefold())
            index += 1
            continue
        closing = "]" if char == "[" else char
        normalized.append(char)
        index += 1
        while index < len(source):
            quoted = source[index]
            normalized.append(quoted)
            index += 1
            if quoted != closing:
                continue
            if index < len(source) and source[index] == closing:
                normalized.append(source[index])
                index += 1
                continue
            break
    return "".join(normalized)


def _mask_quoted_sql(value: str) -> str:
    """保留索引位置并遮蔽 quoted token,供结构 marker 只看真实 SQL token。"""
    masked = list(value)
    index = 0
    while index < len(value):
        opening = value[index]
        if opening not in {"'", '"', "`", "["}:
            index += 1
            continue
        closing = "]" if opening == "[" else opening
        masked[index] = " "
        index += 1
        while index < len(value):
            quoted = value[index]
            masked[index] = " "
            index += 1
            if quoted != closing:
                continue
            if index < len(value) and value[index] == closing:
                masked[index] = " "
                index += 1
                continue
            break
    return "".join(masked)


def _sql_tokens(value: str) -> tuple[tuple[str, str], ...]:
    """返回无注释 canonical SQL token,quoted token 不参与关键字匹配。"""
    normalized = _normalize_sql(value)
    tokens: list[tuple[str, str]] = []
    index = 0
    while index < len(normalized):
        char = normalized[index]
        if char.isspace():
            index += 1
            continue
        if char in {"'", '"', "`", "["}:
            start = index
            closing = "]" if char == "[" else char
            index += 1
            while index < len(normalized):
                quoted = normalized[index]
                index += 1
                if quoted != closing:
                    continue
                if index < len(normalized) and normalized[index] == closing:
                    index += 1
                    continue
                break
            kind = "literal" if char == "'" else "identifier"
            tokens.append((kind, normalized[start:index]))
            continue
        if char.isalnum() or char == "_":
            start = index
            while index < len(normalized) and (
                normalized[index].isalnum() or normalized[index] == "_"
            ):
                index += 1
            tokens.append(("word", normalized[start:index]))
            continue
        tokens.append(("symbol", char))
        index += 1
    return tuple(tokens)


def _normalize_default(value: object) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    while normalized.startswith("(") and normalized.endswith(")"):
        normalized = normalized[1:-1].strip()
    return _normalize_sql(normalized)


def _declared_tables(schema_sql: str) -> tuple[str, ...]:
    matches = re.finditer(
        r"CREATE\s+(?:VIRTUAL\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
        r"([A-Za-z0-9_]+)",
        schema_sql,
        re.I,
    )
    return tuple(match.group(1) for match in matches)


def _table_sql(connection: sqlite3.Connection, table: str) -> str | None:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return None if row is None else row[0]


def _column_semantics(
    connection: sqlite3.Connection, table: str
) -> dict[str, tuple[str, int, str | None, int, int]]:
    rows = connection.execute(
        f"PRAGMA table_xinfo({_quote_identifier(table)})"
    ).fetchall()
    return {
        str(row[1]): (
            re.sub(r"\s+", " ", str(row[2] or "").strip()).upper(),
            int(row[3]),
            _normalize_default(row[4]),
            int(row[5]),
            int(row[6]),
        )
        for row in rows
    }


def _table_flags(
    connection: sqlite3.Connection, table: str
) -> tuple[str, int, int]:
    rows = connection.execute("PRAGMA table_list").fetchall()
    for row in rows:
        if str(row[0]) == "main" and str(row[1]) == table:
            return str(row[2]), int(row[4]), int(row[5])
    raise sqlite3.DatabaseError(f"迁移后缺少 table_list 记录: {table}")


def _parenthesized_constraints(sql: str, keyword: str) -> tuple[str, ...]:
    normalized = _normalize_sql(sql)
    searchable = _mask_quoted_sql(normalized)
    values: list[str] = []
    pattern = re.compile(rf"\b{re.escape(keyword)}\s*\(", re.I)
    for match in pattern.finditer(searchable):
        opening = searchable.find("(", match.start())
        depth = 0
        index = opening
        while index < len(searchable):
            char = searchable[index]
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    values.append(normalized[opening + 1 : index])
                    break
            index += 1
        else:
            raise sqlite3.DatabaseError(f"{keyword} 约束括号不完整")
    return tuple(values)


def _write_semantic_markers(sql: str) -> tuple[object, ...]:
    tokens = _sql_tokens(sql)
    conflicts: list[str] = []
    collations: list[str] = []
    deferred: list[str] = []
    autoincrement = 0
    index = 0
    while index < len(tokens):
        kind, value = tokens[index]
        next_one = tokens[index + 1] if index + 1 < len(tokens) else None
        next_two = tokens[index + 2] if index + 2 < len(tokens) else None
        if (
            (kind, value) == ("word", "on")
            and next_one == ("word", "conflict")
            and next_two is not None
            and next_two[0] == "word"
            and next_two[1] in {"rollback", "abort", "fail", "ignore", "replace"}
        ):
            conflicts.append(next_two[1])
        if (
            (kind, value) == ("word", "collate")
            and next_one is not None
            and next_one[0] in {"word", "identifier"}
        ):
            collations.append(next_one[1])
        if (
            (kind, value) == ("word", "not")
            and next_one == ("word", "deferrable")
        ):
            deferred.append("not deferrable")
            index += 1
        elif (kind, value) == ("word", "deferrable"):
            deferred.append("deferrable")
        elif (
            (kind, value) == ("word", "initially")
            and next_one is not None
            and next_one[0] == "word"
            and next_one[1] in {"deferred", "immediate"}
        ):
            deferred.append(f"initially {next_one[1]}")
            index += 1
        if (kind, value) == ("word", "autoincrement"):
            autoincrement += 1
        index += 1
    return (
        _parenthesized_constraints(sql, "CHECK"),
        tuple(conflicts),
        tuple(collations),
        tuple(deferred),
        autoincrement,
    )


def _foreign_key_semantics(
    connection: sqlite3.Connection, table: str
) -> tuple[tuple[object, ...], ...]:
    rows = connection.execute(
        f"PRAGMA foreign_key_list({_quote_identifier(table)})"
    ).fetchall()
    return tuple(
        (
            int(row[0]),
            int(row[1]),
            str(row[2]),
            str(row[3]),
            None if row[4] is None else str(row[4]),
            str(row[5]).upper(),
            str(row[6]).upper(),
            str(row[7]).upper(),
        )
        for row in rows
    )


def _index_signature(
    connection: sqlite3.Connection, row: sqlite3.Row | tuple
) -> tuple[object, ...]:
    name = str(row[1])
    columns = connection.execute(
        f"PRAGMA index_xinfo({_quote_identifier(name)})"
    ).fetchall()
    ordered = tuple(
        (
            None if column[2] is None else str(column[2]),
            int(column[3]),
            str(column[4]).upper(),
            int(column[5]),
        )
        for column in columns
    )
    return int(row[2]), str(row[3]), int(row[4]), ordered


def _index_rows(connection: sqlite3.Connection, table: str) -> list:
    return list(
        connection.execute(
            f"PRAGMA index_list({_quote_identifier(table)})"
        ).fetchall()
    )


def _assert_table_semantics(
    connection: sqlite3.Connection,
    expected: sqlite3.Connection,
    table: str,
    *,
    exact_sql: bool = False,
) -> None:
    expected_sql = _table_sql(expected, table)
    actual_sql = _table_sql(connection, table)
    if expected_sql is None or actual_sql is None:
        raise sqlite3.DatabaseError(f"迁移后缺少表: {table}")
    expected_virtual = _normalize_sql(expected_sql).startswith(
        "create virtual table"
    )
    actual_virtual = _normalize_sql(actual_sql).startswith(
        "create virtual table"
    )
    if expected_virtual != actual_virtual:
        raise sqlite3.DatabaseError(f"{table} 虚表身份不匹配")
    if (expected_virtual or exact_sql) and _normalize_sql(actual_sql) != _normalize_sql(
        expected_sql
    ):
        raise sqlite3.DatabaseError(f"{table} 建表约束或虚表配置不匹配")
    if _table_flags(connection, table) != _table_flags(expected, table):
        raise sqlite3.DatabaseError(f"{table} table flags 不匹配")
    if _write_semantic_markers(actual_sql) != _write_semantic_markers(
        expected_sql
    ):
        raise sqlite3.DatabaseError(f"{table} 写语义约束不匹配")

    expected_columns = _column_semantics(expected, table)
    actual_columns = _column_semantics(connection, table)
    for column, semantics in expected_columns.items():
        actual = actual_columns.get(column)
        if actual is None:
            raise sqlite3.DatabaseError(f"{table} 缺少列: {column}")
        if actual != semantics:
            labels = ("type", "notnull", "default", "pk", "hidden")
            differences = [
                label
                for label, wanted, found in zip(labels, semantics, actual)
                if wanted != found
            ]
            raise sqlite3.DatabaseError(
                f"{table}.{column} 列语义不匹配: {','.join(differences)}"
            )
    unexpected_columns = set(actual_columns) - set(expected_columns)
    if unexpected_columns:
        raise sqlite3.DatabaseError(
            f"{table} 含冻结 schema 未声明列: {sorted(unexpected_columns)}"
        )

    expected_fks = _foreign_key_semantics(expected, table)
    actual_fks = _foreign_key_semantics(connection, table)
    if actual_fks != expected_fks:
        raise sqlite3.DatabaseError(f"{table} 外键定义不匹配")


def _assert_index_semantics(
    connection: sqlite3.Connection,
    expected: sqlite3.Connection,
    table: str,
) -> None:
    expected_rows = _index_rows(expected, table)
    actual_rows = _index_rows(connection, table)
    expected_explicit = {
        str(row[1]) for row in expected_rows if str(row[3]) == "c"
    }
    actual_explicit = {
        str(row[1]) for row in actual_rows if str(row[3]) == "c"
    }
    if actual_explicit != expected_explicit:
        raise sqlite3.DatabaseError(
            f"{table} 显式索引集合不匹配: "
            f"missing={sorted(expected_explicit - actual_explicit)}, "
            f"extra={sorted(actual_explicit - expected_explicit)}"
        )
    expected_automatic = Counter(
        _index_signature(expected, row)
        for row in expected_rows
        if str(row[3]) != "c"
    )
    actual_automatic = Counter(
        _index_signature(connection, row)
        for row in actual_rows
        if str(row[3]) != "c"
    )
    if actual_automatic != expected_automatic:
        raise sqlite3.DatabaseError(f"{table} UNIQUE/PRIMARY KEY 索引集合不匹配")
    actual_by_name = {str(row[1]): row for row in actual_rows}

    for expected_row in expected_rows:
        expected_name = str(expected_row[1])
        expected_signature = _index_signature(expected, expected_row)
        if str(expected_row[3]) == "c":
            actual_row = actual_by_name.get(expected_name)
            if actual_row is None:
                raise sqlite3.DatabaseError(f"缺少索引: {expected_name}")
            if _index_signature(connection, actual_row) != expected_signature:
                raise sqlite3.DatabaseError(f"索引定义不匹配: {expected_name}")
            expected_sql_row = expected.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
                (expected_name,),
            ).fetchone()
            actual_sql_row = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
                (expected_name,),
            ).fetchone()
            if (
                expected_sql_row is None
                or actual_sql_row is None
                or _normalize_sql(actual_sql_row[0])
                != _normalize_sql(expected_sql_row[0])
            ):
                raise sqlite3.DatabaseError(f"索引 SQL 不匹配: {expected_name}")
            continue


def _expected_schema(schema_sql: str) -> sqlite3.Connection:
    expected = sqlite3.connect(":memory:")
    _execute_sql_script(expected, schema_sql)
    return expected


def _ignore_schema_object(object_type: str, name: str) -> bool:
    if object_type == "index" and name.startswith("sqlite_autoindex_"):
        return True
    return object_type == "table" and name in _IGNORED_SQLITE_STAT_TABLES


def _schema_objects(
    connection: sqlite3.Connection,
) -> set[tuple[str, str, str]]:
    return {
        (str(row[0]), str(row[1]), str(row[2]))
        for row in connection.execute(
            "SELECT type, name, tbl_name FROM sqlite_master"
        ).fetchall()
        if not _ignore_schema_object(str(row[0]), str(row[1]))
    }


def _table_names(connection: sqlite3.Connection) -> tuple[str, ...]:
    return tuple(
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        if str(row[0]) not in _IGNORED_SQLITE_STAT_TABLES
    )


def _exact_object_sql(
    connection: sqlite3.Connection,
) -> dict[tuple[str, str, str], str]:
    return {
        (str(row[0]), str(row[1]), str(row[2])): _normalize_sql(row[3])
        for row in connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE type IN ('trigger', 'view')"
        ).fetchall()
    }


def _assert_safe_sqlite_statistics(connection: sqlite3.Connection) -> None:
    present = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        if str(row[0]) in _IGNORED_SQLITE_STAT_TABLES
    }
    if not present:
        return
    if "sqlite_stat4" in present and "sqlite_stat1" not in present:
        raise sqlite3.DatabaseError("sqlite_stat4 缺少配套 sqlite_stat1")
    expected = sqlite3.connect(":memory:")
    try:
        expected.execute("CREATE TABLE flori_stat_probe(value TEXT)")
        expected.execute(
            "CREATE INDEX flori_stat_probe_idx ON flori_stat_probe(value)"
        )
        expected.execute("ANALYZE")
        available = {
            str(row[0])
            for row in expected.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        for table in sorted(present):
            if table not in available:
                raise sqlite3.DatabaseError(
                    f"当前 SQLite 不支持统计表: {table}"
                )
            _assert_table_semantics(
                connection,
                expected,
                table,
                exact_sql=True,
            )
            _assert_index_semantics(connection, expected, table)
    finally:
        expected.close()


def _assert_legacy_preserved_table(
    connection: sqlite3.Connection,
    table: str,
    schema_sql: str,
) -> None:
    """只允许冻结形状且与托管 schema 完全隔离的历史表原样存续。"""
    if table.startswith("sqlite_") or table.endswith(
        ("_config", "_content", "_data", "_docsize", "_idx")
    ):
        raise sqlite3.DatabaseError(f"历史保留表名称非法: {table}")
    expected = sqlite3.connect(":memory:")
    try:
        expected.execute(schema_sql)
        try:
            _assert_table_semantics(
                connection,
                expected,
                table,
                exact_sql=True,
            )
            _assert_index_semantics(connection, expected, table)
        except sqlite3.DatabaseError as exc:
            raise sqlite3.DatabaseError(
                f"历史保留表形状不匹配: {table}: {exc}"
            ) from exc
    finally:
        expected.close()
    if _table_flags(connection, table)[0] != "table":
        raise sqlite3.DatabaseError(f"历史保留表必须是普通表: {table}")
    for candidate in _table_names(connection):
        for foreign_key in _foreign_key_semantics(connection, candidate):
            if str(foreign_key[2]).casefold() == table.casefold():
                raise sqlite3.DatabaseError(
                    f"历史保留表被外键引用: {candidate} -> {table}"
                )
    reference = re.compile(
        rf"(?<![A-Za-z0-9_]){re.escape(table)}(?![A-Za-z0-9_])",
        re.I,
    )
    for object_type, name, sql in connection.execute(
        "SELECT type, name, COALESCE(sql, '') FROM sqlite_master "
        "WHERE type IN ('trigger', 'view')"
    ).fetchall():
        if reference.search(str(sql)):
            raise sqlite3.DatabaseError(
                f"历史保留表被 {object_type} {name} 引用: {table}"
            )


def _validate_complete_schema(
    connection: sqlite3.Connection,
    schema_sql: str,
) -> None:
    """校验一个版本完整 current schema；不得留下未声明对象或写约束。"""
    expected = _expected_schema(schema_sql)
    try:
        _assert_safe_sqlite_statistics(connection)
        expected_objects = _schema_objects(expected)
        actual_objects = _schema_objects(connection)
        missing_objects = expected_objects - actual_objects
        extra_objects = actual_objects - expected_objects
        for table, preserved_sql in LEGACY_PRESERVED_TABLES.items():
            preserved_object = ("table", table, table)
            if preserved_object not in extra_objects:
                continue
            _assert_legacy_preserved_table(connection, table, preserved_sql)
            extra_objects.remove(preserved_object)
        if missing_objects or extra_objects:
            raise sqlite3.DatabaseError(
                "冻结 schema 对象集合不匹配: "
                f"missing={sorted(missing_objects)}, "
                f"extra={sorted(extra_objects)}"
            )
        if _exact_object_sql(connection) != _exact_object_sql(expected):
            raise sqlite3.DatabaseError("冻结 schema trigger/view SQL 不匹配")
        declared_tables = set(_declared_tables(schema_sql))
        for table in _table_names(expected):
            _assert_table_semantics(
                connection,
                expected,
                table,
                exact_sql=table not in declared_tables,
            )
            _assert_index_semantics(connection, expected, table)
    finally:
        expected.close()
    foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_key_errors:
        raise sqlite3.IntegrityError(
            f"迁移后 foreign_key_check 失败: {len(foreign_key_errors)}"
        )
    integrity = connection.execute("PRAGMA integrity_check").fetchone()
    if not integrity or integrity[0] != "ok":
        raise sqlite3.DatabaseError(f"迁移后 integrity_check 失败: {integrity}")


def _ensure_columns(connection: sqlite3.Connection) -> None:
    for table, columns in EXPECTED_COLUMNS.items():
        existing = {
            row["name"]
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if not existing:
            continue
        for column, ddl in columns.items():
            if column not in existing:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def _backfill_lineage(connection: sqlite3.Connection) -> None:
    columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
    }
    if "lineage_key" not in columns:
        return
    has_url = "url" in columns
    has_content_type = "content_type" in columns
    has_source = "source" in columns
    selected = "id"
    selected += ", url" if has_url else ""
    selected += ", content_type" if has_content_type else ""
    selected += ", source" if has_source else ""
    rows = connection.execute(
        f"SELECT {selected} FROM jobs WHERE lineage_key IS NULL"
    ).fetchall()
    for row in rows:
        url = row["url"] if has_url else None
        value = (
            _frozen_lineage_key(
                url,
                row["content_type"] if has_content_type else None,
                row["source"] if has_source else None,
            )
            if url
            else row["id"]
        )
        connection.execute(
            "UPDATE jobs SET lineage_key=? WHERE id=?", (value, row["id"])
        )
    if not rows:
        return
    connection.execute("UPDATE jobs SET is_current=0 WHERE lineage_key IS NOT NULL")
    connection.execute(
        """UPDATE jobs SET is_current=1 WHERE id IN (
             SELECT id FROM jobs j WHERE j.created_at = (
               SELECT MAX(created_at) FROM jobs j2 WHERE j2.lineage_key = j.lineage_key
             ) GROUP BY j.lineage_key
           )"""
    )


def _backfill_prompt_versions(connection: sqlite3.Connection) -> None:
    tables = {
        row["name"]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    if "prompt_overrides" not in tables or "prompt_override_versions" not in tables:
        return
    rows = connection.execute(
        "SELECT scope, domain, pipeline, step, content FROM prompt_overrides"
    ).fetchall()
    now = datetime.now(timezone.utc).isoformat()
    for row in rows:
        key = (row["scope"], row["domain"], row["pipeline"], row["step"])
        connection.execute(
            "UPDATE prompt_overrides SET version=1 WHERE scope=? AND domain=? "
            "AND pipeline=? AND step=? AND (version IS NULL OR version<1)",
            key,
        )
        has_history = connection.execute(
            "SELECT 1 FROM prompt_override_versions WHERE scope=? AND domain=? "
            "AND pipeline=? AND step=? LIMIT 1",
            key,
        ).fetchone()
        if has_history or not (row["content"] or "").strip():
            continue
        connection.execute(
            """INSERT OR IGNORE INTO prompt_override_versions
               (scope, domain, pipeline, step, version, content, note, created_at)
               VALUES (?,?,?,?,1,?,?,?)""",
            (*key, row["content"], "初始版本", now),
        )


def apply(connection: sqlite3.Connection) -> None:
    _ensure_columns(connection)
    _execute_sql_script(connection, SCHEMA_SQL)
    _backfill_lineage(connection)
    _backfill_prompt_versions(connection)


def validate(connection: sqlite3.Connection) -> None:
    """按冻结 DDL 校验 v1 完整 current schema。"""
    _validate_complete_schema(connection, SCHEMA_SQL)
