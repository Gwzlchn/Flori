"""持有 Database 唯一 SQLite 连接和可重入锁。"""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import stat
import threading
import uuid
from collections.abc import Callable
from pathlib import Path

from ..migrations import (
    Migration,
    UnsupportedSchemaVersionError,
    assert_schema_compatible,
    run_migrations,
    validate_registry,
)
from ..study_suggestions import content_fingerprint, knowledge_fingerprint


class DatabaseRuntime:
    """创建并独占一个 Database 实例的连接、锁和 PRAGMA 生命周期。"""

    def __init__(
        self,
        db_path: Path | str,
        *,
        schema_version: int,
        migration_steps: Callable,
        probe_schema_version: Callable[[Path], int | None],
    ) -> None:
        validate_registry(migration_steps())
        self.schema_limit = schema_version
        self._migration_steps_provider = migration_steps
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.parent / f".{self.path.name}.migration.lock"
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            probed_version = probe_schema_version(self.path)
            if probed_version is not None and probed_version > schema_version:
                raise UnsupportedSchemaVersionError(
                    f"SQLite user_version={probed_version} 高于当前程序上限 "
                    f"{schema_version}，已在连接前拒绝"
                )
            self.connection = sqlite3.connect(
                str(self.path), check_same_thread=False
            )
            self.connection.row_factory = sqlite3.Row
            self.connection.create_function(
                "flori_study_knowledge_fingerprint",
                2,
                lambda domain, key: knowledge_fingerprint(str(domain), str(key)),
                deterministic=True,
            )
            self.connection.create_function(
                "flori_study_content_fingerprint",
                5,
                lambda domain, card_type, front, back, explanation: content_fingerprint(
                    domain=str(domain),
                    card_type=str(card_type),
                    front=str(front),
                    back=str(back),
                    explanation=str(explanation or ""),
                ),
                deterministic=True,
            )
            try:
                assert_schema_compatible(
                    self.connection,
                    minimum_version=0,
                    maximum_version=schema_version,
                )
            except BaseException:
                self.connection.close()
                raise
            self.connection.execute("PRAGMA busy_timeout=5000")
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.execute("PRAGMA foreign_keys=ON")
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
        self.lock = threading.RLock()

    def init_schema(self, owner) -> None:
        """在跨进程锁内执行备份和完整迁移；owner 保留故障注入 seam。"""
        lock_path = self.path.parent / f".{self.path.name}.migration.lock"
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            with self.lock:
                before = owner.schema_version()
                self._require_offline_migration_ready(before)
                if before < self.schema_limit and owner._has_user_schema():
                    owner._create_migration_backup(before)
                run_migrations(self.connection, owner._migration_steps())
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)

    def _require_offline_migration_ready(self, before: int) -> None:
        """生产v7视频库必须先完成对象stage,避免服务启动时只迁DB。"""
        if (
            os.environ.get("FLORI_REQUIRE_OFFLINE_MIGRATIONS") != "1"
            or self.schema_limit < 8
        ):
            return
        configured = os.environ.get("FLORI_MULTIPART_V8_READY_FILE")
        marker_path = (
            Path(configured) if configured
            else self.path.parent / "multipart-v8.ready.json"
        )
        if before == 8 and marker_path.exists():
            try:
                marker = json.loads(marker_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError(
                    "multipart v8 migration marker is unreadable"
                ) from exc
            if marker.get("state") not in {"committed", "verified"}:
                raise RuntimeError(
                    "multipart v8 database commit is incomplete"
                )
            return
        if before != 7:
            return
        video_count = int(self.connection.execute(
            "SELECT COUNT(*) FROM jobs WHERE content_type='video'",
        ).fetchone()[0])
        if video_count == 0:
            return
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                "multipart v8 object stage is required before database migration"
            ) from exc
        expected_path = str(self.path.resolve())
        if (
            marker.get("state") != "staged"
            or marker.get("schema_from") != 7
            or marker.get("schema_to") != 8
            or marker.get("video_jobs") != video_count
            or marker.get("db_path") != expected_path
        ):
            raise RuntimeError("multipart v8 object stage marker does not match database")

    def migration_steps(self) -> tuple[Migration, ...]:
        return self._migration_steps_provider()

    def has_user_schema(self) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' "
            "AND name != 'schema_migrations' LIMIT 1"
        ).fetchone()
        return row is not None

    def create_migration_backup(self, from_version: int) -> Path:
        """为非空库保留升级前一致快照，同版迁移重试时原子刷新。"""
        backup_dir = self.path.parent / "migration-backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        target = backup_dir / (
            f"{self.path.stem}.pre-v{from_version}-to-v{self.schema_limit}.db"
        )
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        destination: sqlite3.Connection | None = None
        try:
            destination = sqlite3.connect(str(temporary))
            self.connection.backup(destination, pages=256, sleep=0.01)
            destination.commit()
            integrity = destination.execute("PRAGMA integrity_check").fetchone()
            copied_version = int(
                destination.execute("PRAGMA user_version").fetchone()[0]
            )
            if integrity != ("ok",) or copied_version != from_version:
                raise sqlite3.DatabaseError(
                    f"迁移前快照校验失败: integrity={integrity}, "
                    f"user_version={copied_version}"
                )
            destination.close()
            destination = None
            source_mode = stat.S_IMODE(self.path.stat().st_mode)
            os.chmod(temporary, source_mode)
            with temporary.open("rb") as stream:
                os.fsync(stream.fileno())
            os.replace(temporary, target)
            directory_fd = os.open(backup_dir, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except BaseException:
            if destination is not None:
                destination.close()
            temporary.unlink(missing_ok=True)
            raise
        return target

    def schema_version(self) -> int:
        return self.connection.execute("PRAGMA user_version").fetchone()[0]

    def run_transaction(
        self,
        owner,
        operation,
        args: tuple,
        kwargs: dict,
        *,
        begin_immediate: bool,
        commit_on_success: bool,
        commit_if_false: bool,
        rollback_on_error: bool,
    ):
        """为单领域写提供唯一锁和提交边界；repository 只执行 in-tx SQL。"""
        with self.lock:
            nested = self.connection.in_transaction
            try:
                if begin_immediate and not nested:
                    self.connection.execute("BEGIN IMMEDIATE")
                result = operation(owner, self.connection, *args, **kwargs)
                if (
                    commit_on_success
                    and not nested
                    and (commit_if_false or result is not False)
                ):
                    self.connection.commit()
                return result
            except BaseException:
                if rollback_on_error and not nested and self.connection.in_transaction:
                    self.connection.rollback()
                raise

    def close(self) -> None:
        with self.lock:
            self.connection.close()
