"""验证完整灾备快照的校验、回滚与空环境恢复边界."""

from __future__ import annotations

import importlib.util
import hashlib
import io
import json
import sqlite3
import subprocess
import tarfile
from pathlib import Path

import pytest


_MODULE_PATH = Path(__file__).parents[1] / "scripts" / "dr_snapshot.py"
_SPEC = importlib.util.spec_from_file_location("flori_dr_snapshot", _MODULE_PATH)
assert _SPEC and _SPEC.loader
dr = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(dr)


def _fixture_roots(root: Path, *, user_version: int = 0) -> dict[str, Path]:
    roots = {name: root / name for name in ("data", "redis", "minio", "config")}
    for path in roots.values():
        path.mkdir(parents=True)
    db_path = roots["data"] / "db" / "analyzer.db"
    db_path.parent.mkdir(parents=True)
    connection = sqlite3.connect(db_path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute(f"PRAGMA user_version={user_version}")
    connection.execute("CREATE TABLE jobs(id TEXT PRIMARY KEY, title TEXT NOT NULL)")
    connection.execute("INSERT INTO jobs VALUES('jobs_test', '灾备测试')")
    connection.commit()
    connection.close()
    (roots["data"] / "jobs" / "jobs_test").mkdir(parents=True)
    (roots["data"] / "jobs" / "jobs_test" / "note.md").write_text("笔记\n", encoding="utf-8")
    (roots["data"] / "prompts" / "profiles").mkdir(parents=True)
    (roots["data"] / "prompts" / "profiles" / "general.yaml").write_text("role: test\n", encoding="utf-8")
    (roots["redis"] / "dump.rdb").write_bytes(b"REDIS-RDB")
    (roots["redis"] / "appendonlydir").mkdir()
    (roots["redis"] / "appendonlydir" / "appendonly.aof").write_bytes(b"AOF")
    (roots["minio"] / "flori" / "jobs_test").mkdir(parents=True)
    (roots["minio"] / "flori" / "jobs_test" / "artifact.bin").write_bytes(b"OBJECT")
    (roots["config"] / "pipelines.yaml").write_text("pipelines: {}\n", encoding="utf-8")
    return roots


def _create(root: Path, *, user_version: int = 0, redis_mode: str = "offline-volume") -> tuple[Path, dict]:
    sources = _fixture_roots(root / "source", user_version=user_version)
    archive = root / "backups" / "snapshot.tar.gz"
    result = dr.create_snapshot(
        data_root=sources["data"],
        redis_root=sources["redis"],
        minio_root=sources["minio"],
        config_root=sources["config"],
        output=archive,
        generation="test-generation",
        app_version="test",
        redis_mode=redis_mode,
    )
    return archive, result


def _target_roots(root: Path) -> dict[str, Path]:
    targets = {name: root / name for name in ("data", "redis", "minio", "config")}
    for path in targets.values():
        path.mkdir(parents=True)
    return targets


def _inventory(root: Path) -> list[tuple[str, bytes]]:
    return sorted(
        (path.relative_to(root).as_posix(), path.read_bytes())
        for path in root.rglob("*")
        if path.is_file()
    )


def test_snapshot_covers_all_persistent_assets_and_restores_empty_environment(tmp_path: Path):
    archive, backup = _create(tmp_path)

    manifest = dr.validate_archive(archive)
    assert backup["status"] == "success"
    assert backup["archive_sha256"]
    assert archive.with_suffix(archive.suffix + ".sha256").is_file()
    assert manifest["sqlite"]["integrity_check"] == "ok"
    assert manifest["assets"]["data"]["included"] is True
    assert manifest["assets"]["redis"]["included"] is True
    assert manifest["assets"]["minio"]["included"] is True
    assert manifest["assets"]["config"]["included"] is True
    declared = set(manifest["files"])
    assert "assets/data/jobs/jobs_test/note.md" in declared
    assert "assets/data/prompts/profiles/general.yaml" in declared
    assert "assets/data/db/analyzer.db" in declared
    assert "assets/redis/dump.rdb" in declared
    assert "assets/redis/appendonlydir/appendonly.aof" in declared
    assert "assets/minio/flori/jobs_test/artifact.bin" in declared
    assert "assets/config/pipelines.yaml" in declared

    targets = _target_roots(tmp_path / "empty-target")
    result = dr.restore_snapshot(archive_path=archive, targets=targets)

    assert result["status"] == "success"
    assert result["restored_assets"] == ["data", "redis", "minio", "config"]
    assert result["checks"]["atomic_switch"] == "ok"
    connection = sqlite3.connect(targets["data"] / "db" / "analyzer.db")
    assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    assert connection.execute("SELECT title FROM jobs WHERE id='jobs_test'").fetchone() == ("灾备测试",)
    connection.close()
    assert (targets["data"] / "jobs" / "jobs_test" / "note.md").read_text(encoding="utf-8") == "笔记\n"
    assert (targets["data"] / "prompts" / "profiles" / "general.yaml").is_file()
    assert (targets["redis"] / "dump.rdb").read_bytes() == b"REDIS-RDB"
    assert (targets["minio"] / "flori" / "jobs_test" / "artifact.bin").read_bytes() == b"OBJECT"
    assert (targets["config"] / "pipelines.yaml").is_file()
    assert not list((tmp_path / "empty-target").rglob(".flori-dr-*"))


def test_running_redis_mode_archives_materialized_rdb_and_aof(tmp_path: Path):
    archive, _ = _create(tmp_path, redis_mode="materialized-rdb-aof")

    manifest = dr.validate_archive(archive)

    assert manifest["assets"]["redis"]["capture_mode"] == "materialized-rdb-aof"
    redis_files = sorted(path for path in manifest["files"] if path.startswith("assets/redis/"))
    assert redis_files == [
        "assets/redis/appendonlydir/appendonly.aof",
        "assets/redis/dump.rdb",
    ]


def test_cli_accepts_materialized_redis_mode():
    args = dr._parser().parse_args([
        "create",
        "--data", "/data",
        "--redis", "/redis",
        "--output", "/output/backup.tar.gz",
        "--generation", "test-generation",
        "--redis-mode", "materialized-rdb-aof",
    ])

    assert args.redis_mode == "materialized-rdb-aof"


def test_nested_minio_mount_is_not_duplicated_or_removed_by_data_switch(tmp_path: Path):
    sources = _fixture_roots(tmp_path / "source")
    nested_minio = sources["data"] / "minio"
    (nested_minio / "flori" / "nested").mkdir(parents=True)
    (nested_minio / "flori" / "nested" / "object.bin").write_bytes(b"NESTED")
    archive = tmp_path / "backups" / "nested.tar.gz"
    dr.create_snapshot(
        data_root=sources["data"],
        redis_root=sources["redis"],
        minio_root=nested_minio,
        config_root=sources["config"],
        output=archive,
        generation="nested-generation",
    )
    manifest = dr.validate_archive(archive)
    assert manifest["assets"]["data"]["excluded_external_subtrees"] == ["minio"]
    assert not [path for path in manifest["files"] if path.startswith("assets/data/minio/")]

    data_target = tmp_path / "target" / "data"
    targets = {
        "data": data_target,
        "redis": tmp_path / "target" / "redis",
        "minio": data_target / "minio",
        "config": tmp_path / "target" / "config",
    }
    for path in targets.values():
        path.mkdir(parents=True, exist_ok=True)
    (targets["minio"] / "old-object").write_bytes(b"OLD")

    result = dr.restore_snapshot(archive_path=archive, targets=targets)

    assert result["preserved_target_entries"] == {"data": ["minio"]}
    assert (targets["data"] / "db" / "analyzer.db").is_file()
    assert not (targets["minio"] / "old-object").exists()
    assert (targets["minio"] / "flori" / "nested" / "object.bin").read_bytes() == b"NESTED"


def test_corrupt_archive_is_fail_closed_before_target_change(tmp_path: Path):
    archive, _ = _create(tmp_path)
    targets = _target_roots(tmp_path / "target")
    sentinel = targets["data"] / "sentinel"
    sentinel.write_bytes(b"current")
    corrupt = tmp_path / "corrupt.tar.gz"
    raw = archive.read_bytes()
    corrupt.write_bytes(raw[: len(raw) // 2])

    with pytest.raises(dr.SnapshotError):
        dr.restore_snapshot(archive_path=corrupt, targets=targets)

    assert sentinel.read_bytes() == b"current"
    assert _inventory(targets["redis"]) == []
    assert not list((tmp_path / "target").rglob(".flori-dr-*"))


def test_cross_asset_failure_rolls_back_every_committed_target(tmp_path: Path):
    archive, _ = _create(tmp_path)
    targets = _target_roots(tmp_path / "target")
    for name, path in targets.items():
        (path / "current.txt").write_text(name, encoding="utf-8")
    before = {name: _inventory(path) for name, path in targets.items()}

    with pytest.raises(dr.SnapshotError, match="故障注入"):
        dr.restore_snapshot(archive_path=archive, targets=targets, fail_after_commits=2)

    assert {name: _inventory(path) for name, path in targets.items()} == before
    for path in targets.values():
        assert not (path / dr.TRANSACTION_FILE).exists()
        assert not [child for child in path.iterdir() if child.name.startswith(dr.STAGE_PREFIX)]


def test_interrupted_transaction_recovery_uses_global_commit_decision(tmp_path: Path):
    archive, _ = _create(tmp_path)
    extracted = tmp_path / "extracted"
    extracted.mkdir()
    dr._extract_archive(archive, extracted)
    manifest = dr.validate_extracted(extracted)
    generation = manifest["generation"]

    rollback_targets = _target_roots(tmp_path / "rollback-target")
    for name in ("data", "redis"):
        (rollback_targets[name] / "current.txt").write_text(name, encoding="utf-8")
        dr._prepare_target(extracted / "assets" / name, rollback_targets[name], generation, name)
    dr._commit_target(rollback_targets["data"])

    dr._recover_target_set([rollback_targets["data"], rollback_targets["redis"]])

    assert (rollback_targets["data"] / "current.txt").read_text(encoding="utf-8") == "data"
    assert (rollback_targets["redis"] / "current.txt").read_text(encoding="utf-8") == "redis"

    accepted_targets = _target_roots(tmp_path / "accepted-target")
    for name in ("data", "redis"):
        (accepted_targets[name] / "current.txt").write_text(name, encoding="utf-8")
        dr._prepare_target(extracted / "assets" / name, accepted_targets[name], generation, name)
        dr._commit_target(accepted_targets[name])
    dr._accept_target(accepted_targets["data"])

    dr._recover_target_set([accepted_targets["data"], accepted_targets["redis"]])

    assert (accepted_targets["data"] / "db" / "analyzer.db").is_file()
    assert (accepted_targets["redis"] / "dump.rdb").is_file()
    for name in ("data", "redis"):
        assert not (accepted_targets[name] / dr.TRANSACTION_FILE).exists()
        assert not [child for child in accepted_targets[name].iterdir() if child.name.startswith(dr.STAGE_PREFIX)]


def test_unsupported_sqlite_version_fails_compatibility_gate(tmp_path: Path):
    archive, _ = _create(tmp_path, user_version=7)

    with pytest.raises(dr.SnapshotError, match="超出"):
        dr.validate_archive(archive, max_db_user_version=6)


def test_archive_path_traversal_is_rejected(tmp_path: Path):
    archive = tmp_path / "escape.tar.gz"
    with tarfile.open(archive, "w:gz") as stream:
        payload = b"escape"
        member = tarfile.TarInfo("../escape")
        member.size = len(payload)
        stream.addfile(member, io.BytesIO(payload))
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix(archive.suffix + ".sha256").write_text(
        f"{digest}  {archive.name}\n", encoding="utf-8"
    )

    with pytest.raises(dr.SnapshotError, match="路径非法"):
        dr.validate_archive(archive)

    assert not (tmp_path.parent / "escape").exists()


def test_existing_archive_is_never_overwritten(tmp_path: Path):
    sources = _fixture_roots(tmp_path / "source")
    archive = tmp_path / "snapshot.tar.gz"
    archive.write_bytes(b"known-good")

    with pytest.raises(dr.SnapshotError, match="拒绝覆盖"):
        dr.create_snapshot(
            data_root=sources["data"],
            redis_root=sources["redis"],
            output=archive,
            generation="same-generation",
        )

    assert archive.read_bytes() == b"known-good"


def test_empty_environment_drill_writes_machine_readable_rpo_rto(tmp_path: Path):
    result_path = tmp_path / "drill-result.json"

    result = dr.run_empty_environment_drill(result_path)

    stored = json.loads(result_path.read_text(encoding="utf-8"))
    assert result == stored
    assert stored["status"] == "success"
    assert stored["rpo_seconds"] >= 0
    assert stored["rto_seconds"] >= 0
    assert stored["checks"] == {
        "backup_atomic_publish": "ok",
        "corrupt_snapshot_fail_closed": "ok",
        "cross_asset_rollback": "ok",
        "empty_environment_restore": "ok",
    }


def test_operational_shell_wrappers_are_valid_bash():
    for name in ("backup.sh", "restore.sh", "dr-drill.sh"):
        completed = subprocess.run(
            ["bash", "-n", str(_MODULE_PATH.parent / name)],
            check=False,
            capture_output=True,
            text=True,
        )
        assert completed.returncode == 0, completed.stderr
