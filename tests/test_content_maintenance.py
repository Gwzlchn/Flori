"""在线服务与便携导入的真实 namespace 锁测试。"""

from __future__ import annotations

import os
import asyncio

import pytest

from shared import content_maintenance as maintenance
from shared.content_maintenance import MaintenanceLockError


@pytest.fixture(autouse=True)
def lock_root(tmp_path, monkeypatch):
    monkeypatch.setenv(maintenance.LOCK_DIR_ENV, str(tmp_path / "locks"))
    monkeypatch.delenv("MINIO_URL", raising=False)
    monkeypatch.delenv("MINIO_BUCKET", raising=False)


def _layout(tmp_path):
    db = tmp_path / "data" / "db" / "analyzer.db"
    jobs = tmp_path / "data" / "jobs"
    db.parent.mkdir(parents=True)
    jobs.mkdir(parents=True)
    db.touch()
    return db, jobs


def test_multiple_services_share_a_namespace_lease(tmp_path) -> None:
    db, jobs = _layout(tmp_path)
    first = maintenance.acquire_service_lease(
        db_path=db, jobs_dir=jobs, owner="api",
    )
    second = maintenance.acquire_service_lease(
        db_path=db, jobs_dir=jobs, owner="scheduler",
    )
    second.close()
    first.close()


def test_live_import_is_blocked_until_all_services_release(tmp_path) -> None:
    db, jobs = _layout(tmp_path)
    service = maintenance.acquire_service_lease(
        db_path=db, jobs_dir=jobs, owner="worker",
    )
    resources = maintenance.service_resources(db_path=db, jobs_dir=jobs)
    with pytest.raises(MaintenanceLockError, match="blocked"):
        maintenance.acquire_maintenance_lease(
            resources, exclusive=True, owner="content-import",
        )
    service.close()
    exclusive = maintenance.acquire_maintenance_lease(
        resources, exclusive=True, owner="content-import",
    )
    exclusive.close()


def test_import_lease_blocks_a_late_service_start(tmp_path) -> None:
    db, jobs = _layout(tmp_path)
    resources = maintenance.service_resources(db_path=db, jobs_dir=jobs)
    exclusive = maintenance.acquire_maintenance_lease(
        resources, exclusive=True, owner="content-import",
    )
    with pytest.raises(MaintenanceLockError, match="blocked"):
        maintenance.acquire_service_lease(
            db_path=db, jobs_dir=jobs, owner="api",
        )
    exclusive.close()


def test_hardlink_database_alias_has_the_same_resource_identity(tmp_path) -> None:
    db, jobs = _layout(tmp_path)
    alias = tmp_path / "db-alias"
    os.link(db, alias)
    alias_resources = set(maintenance.path_resources("database", alias))
    live_resources = set(maintenance.path_resources("database", db))
    assert any(item.startswith("physical:inode:") for item in alias_resources & live_resources)


def test_cross_kind_alias_uses_the_same_physical_resource_identity(tmp_path) -> None:
    _db, jobs = _layout(tmp_path)
    artifact = set(maintenance.path_resources("artifact-root", jobs))
    config = set(maintenance.path_resources("config-root", jobs))
    source = set(maintenance.path_resources("source-root", jobs))
    shared = artifact & config & source
    assert any(item.startswith("physical:path:") for item in shared)
    assert any(item.startswith("physical:inode:") for item in shared)

    lease = maintenance.acquire_maintenance_lease(
        artifact, exclusive=False, owner="api",
    )
    with pytest.raises(MaintenanceLockError, match="blocked"):
        maintenance.acquire_maintenance_lease(
            source, exclusive=True, owner="content-import",
        )
    lease.close()


def test_path_identity_does_not_change_when_database_is_created(tmp_path) -> None:
    db = tmp_path / "data" / "db" / "analyzer.db"
    db.parent.mkdir(parents=True)
    before = set(maintenance.path_resources("database", db))
    db.touch()
    after = set(maintenance.path_resources("database", db))
    assert before <= after


def test_live_artifact_subdirectory_locks_the_service_root(tmp_path) -> None:
    db, jobs = _layout(tmp_path)
    resources = maintenance.live_import_resources(
        targets=["artifact-root"],
        live_db_path=db,
        live_jobs_dir=jobs,
        production_bucket="flori",
    )
    assert resources == tuple(sorted(maintenance.path_resources("artifact-root", jobs)))


def test_live_config_import_locks_the_same_root_as_services(tmp_path) -> None:
    db, jobs = _layout(tmp_path)
    prompts = tmp_path / "data" / "prompts"
    prompts.mkdir()
    service = maintenance.acquire_service_lease(
        db_path=db, jobs_dir=jobs, config_root=prompts, owner="api",
    )
    resources = maintenance.live_import_resources(
        targets=["config-root"],
        live_db_path=tmp_path / "other.db",
        live_jobs_dir=tmp_path / "other-jobs",
        production_bucket="flori",
        live_config_root=prompts,
    )
    with pytest.raises(MaintenanceLockError, match="blocked"):
        maintenance.acquire_maintenance_lease(
            resources, exclusive=True, owner="content-import",
        )
    service.close()


def test_source_root_import_conflicts_with_local_worker_reader(tmp_path) -> None:
    db, jobs = _layout(tmp_path)
    source = tmp_path / "source-library"
    source.mkdir()
    service = maintenance.acquire_service_lease(
        db_path=db, jobs_dir=jobs, source_roots=[source], owner="worker",
    )
    with pytest.raises(MaintenanceLockError, match="blocked"):
        maintenance.acquire_maintenance_lease(
            maintenance.path_resources("source-root", source),
            exclusive=True,
            owner="content-import",
        )
    service.close()


def test_object_store_namespace_uses_the_actual_bucket(tmp_path, monkeypatch) -> None:
    db, jobs = _layout(tmp_path)
    monkeypatch.setenv("MINIO_URL", "minio:9000")
    monkeypatch.setenv("MINIO_BUCKET", "flori-production")
    resources = maintenance.service_resources(db_path=db, jobs_dir=jobs)
    assert maintenance.object_resource("flori-production") in resources
    assert all(not item.startswith("artifact-root:") for item in resources)


@pytest.mark.asyncio
async def test_api_lifespan_holds_the_shared_service_lease(app, monkeypatch) -> None:
    monkeypatch.delenv("MINIO_URL", raising=False)
    async with app.router.lifespan_context(app):
        resources = maintenance.service_resources(
            db_path=app.state.config.db_path,
            jobs_dir=app.state.config.jobs_dir,
        )
        with pytest.raises(MaintenanceLockError, match="blocked"):
            maintenance.acquire_maintenance_lease(
                resources, exclusive=True, owner="content-import",
            )


@pytest.mark.asyncio
async def test_api_lifespan_exception_releases_the_service_lease(
    app, monkeypatch,
) -> None:
    monkeypatch.delenv("MINIO_URL", raising=False)
    resources = maintenance.service_resources(
        db_path=app.state.config.db_path,
        jobs_dir=app.state.config.jobs_dir,
    )
    with pytest.raises(RuntimeError, match="application failed"):
        async with app.router.lifespan_context(app):
            raise RuntimeError("application failed")
    lease = maintenance.acquire_maintenance_lease(
        resources, exclusive=True, owner="content-import",
    )
    lease.close()


@pytest.mark.asyncio
async def test_two_import_calls_cannot_write_the_same_isolated_target(
    tmp_path, monkeypatch,
) -> None:
    from shared import content_import

    class Storage:
        jobs_dir = tmp_path / "isolated-jobs"

    Storage.jobs_dir.mkdir()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def hold_locked_import(**_kwargs):
        entered.set()
        await release.wait()
        return object()

    monkeypatch.setattr(content_import, "_run_import_locked", hold_locked_import)
    kwargs = {
        "repository": object(),
        "snapshot": "latest",
        "target_db_path": tmp_path / "isolated.db",
        "storage": Storage(),
        "journal_path": tmp_path / "journal.sqlite3",
        "target_generation": "gen-1",
    }
    first = asyncio.create_task(content_import.run_import(**kwargs))
    await entered.wait()
    with pytest.raises(MaintenanceLockError, match="blocked"):
        await content_import.run_import(**kwargs)
    release.set()
    await first


@pytest.mark.asyncio
async def test_live_subdirectory_import_conflicts_with_service_root_lease(
    tmp_path, monkeypatch,
) -> None:
    from shared import content_import

    db, jobs = _layout(tmp_path)
    service = maintenance.acquire_service_lease(
        db_path=db, jobs_dir=jobs, owner="api",
    )

    class Storage:
        jobs_dir = jobs / "subdir"

    Storage.jobs_dir.mkdir()
    authorization = {
        "maintenance_resources": list(maintenance.live_import_resources(
            targets=["artifact-root"],
            live_db_path=db,
            live_jobs_dir=jobs,
            production_bucket="flori",
        )),
    }
    monkeypatch.setattr(content_import, "_run_import_locked", lambda **_kwargs: None)
    with pytest.raises(MaintenanceLockError, match="blocked"):
        await content_import.run_import(
            repository=object(), snapshot="latest",
            target_db_path=tmp_path / "isolated.db",
            storage=Storage(), journal_path=tmp_path / "journal.sqlite3",
            target_generation="gen-live-subdir",
            live_authorization=authorization,
        )
    service.close()
