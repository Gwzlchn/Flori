"""验证固定历史归档经生产恢复入口后可被当前 Database 读取。"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from shared.db import Database, SCHEMA_VERSION


pytestmark = pytest.mark.integration

_REPO = Path(__file__).parents[2]
_DR_FIXTURES = Path(__file__).parents[1] / "fixtures" / "dr"
_RESTORE_ENTRY = _REPO / "scripts" / "dr_snapshot.py"
_SCHEMA_MANIFEST = _REPO / "shared" / "migrations" / "manifest.json"


@pytest.mark.parametrize(
    ("fixture_name", "archived_version", "expected_job_ids"),
    [
        ("legacy-format-v1", 0, {"legacy-v0-job", "jobs_test"}),
        ("format-v2-schema-v2", 2, {"jobs_test"}),
    ],
    ids=["legacy-format-v1", "format-v2-schema-v2"],
)
def test_fixed_archive_restores_through_production_cli_and_database(
    tmp_path: Path,
    fixture_name: str,
    archived_version: int,
    expected_job_ids: set[str],
) -> None:
    archive = tmp_path / f"{fixture_name}.tar.gz"
    archive.write_bytes(
        base64.b64decode(
            (_DR_FIXTURES / f"{fixture_name}.tar.gz.base64").read_text(
                encoding="ascii"
            )
        )
    )
    checksum_source = _DR_FIXTURES / f"{fixture_name}.tar.gz.sha256"
    expected_checksum = checksum_source.read_text(encoding="ascii").split()[0]
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == expected_checksum
    archive.with_suffix(archive.suffix + ".sha256").write_text(
        f"{expected_checksum}  {archive.name}\n",
        encoding="ascii",
    )

    targets = {
        name: tmp_path / "restored" / name
        for name in ("data", "redis", "minio", "config")
    }
    result_path = tmp_path / "restore-result.json"
    command = [
        sys.executable,
        str(_RESTORE_ENTRY),
        "restore",
        "--archive",
        str(archive),
        "--data-target",
        str(targets["data"]),
        "--redis-target",
        str(targets["redis"]),
        "--minio-target",
        str(targets["minio"]),
        "--config-target",
        str(targets["config"]),
        "--schema-manifest",
        str(_SCHEMA_MANIFEST),
        "--expected-deployment-id",
        "integration-restore-target",
        "--allow-cross-deployment",
        "--cross-deployment-confirmation",
        "REPLACE_OTHER_FLORI_DEPLOYMENT",
        "--result-file",
        str(result_path),
        "--owner-uid",
        str(os.getuid()),
        "--owner-gid",
        str(os.getgid()),
    ]
    completed = subprocess.run(
        command,
        cwd=_REPO,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["status"] == "success"
    assert result["deployment"]["current_id"] == "integration-restore-target"
    assert result["deployment"]["cross_deployment_override"] is True
    assert result["checks"] == {
        "archive_members": "ok",
        "checksums": "ok",
        "sqlite_integrity": "ok",
        "compatibility": "ok",
        "atomic_switch": "ok",
    }

    db_path = targets["data"] / "db" / "analyzer.db"
    raw = sqlite3.connect(db_path)
    try:
        assert raw.execute("PRAGMA user_version").fetchone() == (archived_version,)
    finally:
        raw.close()

    with Database(db_path) as database:
        database.init_schema()
        assert database.schema_version() == SCHEMA_VERSION
        restored = database.get_job("jobs_test")
        assert restored is not None
        assert restored.title == "灾备测试"
        total, jobs = database.list_jobs(current_only=False, limit=10)
        assert total == len(expected_job_ids)
        assert {job.id for job in jobs} == expected_job_ids
