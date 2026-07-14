"""credentials 领域的显式数据库边界。"""

from __future__ import annotations

from .seams import db as _db

from ..db import (
    _fernet,
    _now_iso,
    _warn_plaintext_credentials_once,
)


class CredentialsRepository:
    """由 Database façade 调用，不持有独立连接或锁。"""

    def get_credential(self, key: str) -> str | None:
        """读一条凭证值,未命中返回 None。

        有 Fernet key 时尝试解密;遇 InvalidToken(历史明文行,或换了 key 的旧 token)
        透传原始串(legacy passthrough)。无 key 则直接返回原始串。任何情况都不因坏值崩。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM app_credentials WHERE key=?", (key,)
            ).fetchone()
        if row is None:
            return None
        raw = row["value"]
        if raw is None:
            return None
        f = _fernet()
        if f is None:
            return raw
        try:
            from cryptography.fernet import InvalidToken
            return f.decrypt(raw.encode()).decode()
        except InvalidToken:
            return raw  # 明文遗留行 / 异 key 的 token:原样透传
        except Exception:
            return raw  # 任何意外都不让读凭证崩

    def set_credential_in_tx(self, connection, key: str, value: str) -> None:
        """存/覆盖一条应用级凭证(如 B站 cookie JSON),按 key 幂等 upsert。

        设了 FLORI_SECRET_KEY 时以 Fernet token 加密落库;未设则存明文(向后兼容)
        并一次性告警(建议设 key 以 at-rest 加密)。"""
        f = _fernet()
        if f is not None:
            stored = f.encrypt(value.encode()).decode()
        else:
            _warn_plaintext_credentials_once()
            stored = value
        connection.execute(
            """INSERT OR REPLACE INTO app_credentials (key, value, updated_at)
               VALUES (?,?,?)""",
            (key, stored, _db._now_iso()),
        )

    def delete_credential_in_tx(self, connection, key: str) -> None:
        """删一条凭证(如登出清除 B站 cookie)。"""
        connection.execute("DELETE FROM app_credentials WHERE key=?", (key,))
