"""运维脚本使用的受控只读数据库投影。"""

from __future__ import annotations


class MaintenanceRepository:
    """替代运维脚本越过 façade 直访 SQLite 连接。"""

    @staticmethod
    def glossary_rows(database) -> list[dict]:
        rows = database._conn.execute(
            "SELECT domain, term, zh_name, definition, aliases, occurrences, "
            "status, created_at FROM glossary ORDER BY domain, term"
        ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def glossary_zh_name(
        database, domain: str, term: str
    ) -> dict | None:
        row = database._conn.execute(
            "SELECT zh_name FROM glossary WHERE domain=? AND term=?",
            (domain, term),
        ).fetchone()
        return dict(row) if row is not None else None

    @staticmethod
    def credential_keys(database) -> list[str]:
        rows = database._conn.execute(
            "SELECT key FROM app_credentials ORDER BY key"
        ).fetchall()
        return [str(row["key"]) for row in rows]
