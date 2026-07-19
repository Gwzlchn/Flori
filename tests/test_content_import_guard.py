"""导入侧线上面隔离门(shared/content_import_guard)。

这些用例存在的理由:全部既有 merge/import 测试都 monkeypatch 掉 MINIO_URL,于是
"默认写隔离 staging"这条安全属性只在 LocalStorage 上被验证过,而生产 100% 跑
对象存储——那条路径上 jobs_dir 根本不参与寻址,默认导入写的是生产桶。因此这里
每条断言都显式覆盖设了 MINIO_URL 的形态。
"""

from __future__ import annotations

import json
import hashlib
from datetime import datetime, timedelta, timezone

import pytest

from shared import content_import_guard as guard
from shared.content_import_guard import LiveTargetError


@pytest.fixture
def live_layout(tmp_path, monkeypatch):
    """把线上面锚到 tmp,避免测试依赖容器内的 /data 真实存在。"""
    live_db = tmp_path / "data" / "db" / "analyzer.db"
    live_jobs = tmp_path / "data" / "jobs"
    live_config = tmp_path / "data" / "prompts"
    live_jobs.mkdir(parents=True)
    live_config.mkdir(parents=True)
    live_db.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv(guard.LIVE_DB_PATH_ENV, str(live_db))
    monkeypatch.setenv(guard.LIVE_JOBS_DIR_ENV, str(live_jobs))
    monkeypatch.setenv(guard.LIVE_CONFIG_ROOT_ENV, str(live_config))
    monkeypatch.setenv(guard.LIVE_DATA_ROOT_ENV, str(tmp_path / "data"))
    monkeypatch.setenv(guard.DEPLOYMENT_ID_ENV, "test-deployment")
    monkeypatch.delenv("MINIO_URL", raising=False)
    monkeypatch.delenv("MINIO_BUCKET", raising=False)
    monkeypatch.delenv(guard.REMOTE_QUIESCE_ENV, raising=False)
    monkeypatch.delenv(guard.DR_MAX_AGE_ENV, raising=False)
    return {
        "db": live_db,
        "jobs": live_jobs,
        "config": live_config,
        "staging_db": tmp_path / "staging" / "new.db",
        "staging_jobs": tmp_path / "staging" / "jobs",
    }


def _dr_receipt(path, *, age_seconds: int = 60, **overrides) -> str:
    created = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    archive = path.parent / "flori-backup-gen.tar.gz"
    archive.write_text(
        f"exact-dr-archive|gen-20260717T000000Z|{created.isoformat()}",
        encoding="utf-8",
    )
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix(archive.suffix + ".sha256").write_text(
        f"{digest}  {archive.name}\n", encoding="utf-8",
    )
    body = {
        "status": "success",
        "operation": "backup",
        "generation": "gen-20260717T000000Z",
        "archive": f"/output/{archive.name}",
        "archive_sha256": digest,
        "manifest": {
            "format": "flori-disaster-recovery",
            "format_version": 2,
            "generation": "gen-20260717T000000Z",
            "created_at": created.isoformat(),
            "deployment": {"id": "test-deployment"},
            "assets": {
                "data": {
                    "included": True,
                    "excluded_external_subtrees": [],
                },
                "redis": {"included": True},
                "minio": {
                    "included": True,
                    "excluded_external_subtrees": [],
                },
                "config": {"included": True},
            },
        },
    }
    body.update(overrides)
    path.write_text(json.dumps(body), encoding="utf-8")
    return str(path)


@pytest.fixture(autouse=True)
def _stub_full_dr_validation(monkeypatch):
    """归档内部全链由 backup/restore 测试覆盖;本文件聚焦 receipt 与真实字节绑定。"""
    def validate(archive):
        _prefix, generation, created_at = archive.read_text(encoding="utf-8").split("|", 2)
        return {
            "status": "success",
            "operation": "validate",
            "format": guard.DR_FORMAT_NAME,
            "format_version": 2,
            "generation": generation,
            "created_at": created_at,
            "deployment_id": "test-deployment",
            "assets": {
                "data": {
                    "included": True,
                    "excluded_external_subtrees": [],
                },
                "redis": {"included": True},
                "minio": {
                    "included": True,
                    "excluded_external_subtrees": [],
                },
                "config": {"included": True},
            },
            "checks": {"members": "ok", "checksums": "ok"},
        }

    monkeypatch.setattr(guard, "_validate_dr_archive", validate)


class TestLiveTargetResolution:
    def test_staging_target_is_not_live(self, live_layout) -> None:
        assert guard.resolve_live_targets(
            db_path=live_layout["staging_db"], jobs_dir=live_layout["staging_jobs"],
            object_bucket=None,
        ) == []

    def test_live_database_is_detected_even_with_staging_artifacts(self, live_layout) -> None:
        assert guard.resolve_live_targets(
            db_path=live_layout["db"], jobs_dir=live_layout["staging_jobs"],
            object_bucket=None,
        ) == [guard.TARGET_DATABASE]

    def test_explicit_live_jobs_dir_is_detected(self, live_layout) -> None:
        """--jobs-dir 显式指到线上根:旧实现只在 --into-live 挑默认值时才当线上。"""
        assert guard.resolve_live_targets(
            db_path=live_layout["staging_db"], jobs_dir=live_layout["jobs"],
            object_bucket=None,
        ) == [guard.TARGET_ARTIFACT_ROOT]

    def test_subdirectory_of_live_root_is_still_live(self, live_layout) -> None:
        assert guard.resolve_live_targets(
            db_path=live_layout["staging_db"], jobs_dir=live_layout["jobs"] / "sub",
            object_bucket=None,
        ) == [guard.TARGET_ARTIFACT_ROOT]

    def test_live_config_root_is_detected_with_other_targets_isolated(
        self, live_layout,
    ) -> None:
        assert guard.resolve_live_targets(
            db_path=live_layout["staging_db"],
            jobs_dir=live_layout["staging_jobs"],
            object_bucket=None,
            config_root=live_layout["config"],
        ) == [guard.TARGET_CONFIG_ROOT]

    def test_config_alias_to_live_jobs_is_classified_as_artifact_target(
        self, live_layout,
    ) -> None:
        assert guard.resolve_live_targets(
            db_path=live_layout["staging_db"],
            jobs_dir=live_layout["staging_jobs"],
            object_bucket=None,
            config_root=live_layout["jobs"],
        ) == [guard.TARGET_ARTIFACT_ROOT]

    def test_source_alias_to_live_jobs_is_classified_as_artifact_target(
        self, live_layout,
    ) -> None:
        assert guard.resolve_live_targets(
            db_path=live_layout["staging_db"],
            jobs_dir=live_layout["staging_jobs"],
            object_bucket=None,
            source_roots=[live_layout["jobs"]],
        ) == [guard.TARGET_ARTIFACT_ROOT]

    def test_database_inside_live_jobs_cannot_be_disguised_as_staging(
        self, live_layout,
    ) -> None:
        assert guard.resolve_live_targets(
            db_path=live_layout["jobs"] / "hidden.db",
            jobs_dir=live_layout["staging_jobs"],
            object_bucket=None,
        ) == [guard.TARGET_ARTIFACT_ROOT]

    def test_symlink_alias_to_live_database_is_rejected(
        self, live_layout, tmp_path,
    ) -> None:
        live_layout["db"].touch()
        alias = tmp_path / "db-alias"
        alias.symlink_to(live_layout["db"])
        with pytest.raises(LiveTargetError, match="符号链接"):
            guard.resolve_live_targets(
                db_path=alias, jobs_dir=live_layout["staging_jobs"], object_bucket=None,
            )

    def test_symlink_parent_to_live_jobs_is_rejected(
        self, live_layout, tmp_path,
    ) -> None:
        alias = tmp_path / "jobs-alias"
        alias.symlink_to(live_layout["jobs"], target_is_directory=True)
        with pytest.raises(LiveTargetError, match="符号链接"):
            guard.resolve_live_targets(
                db_path=live_layout["staging_db"], jobs_dir=alias / "child",
                object_bucket=None,
            )

    def test_hardlink_alias_to_live_database_is_detected(
        self, live_layout, tmp_path,
    ) -> None:
        live_layout["db"].touch()
        alias = tmp_path / "db-hardlink"
        alias.hardlink_to(live_layout["db"])
        assert guard.resolve_live_targets(
            db_path=alias, jobs_dir=live_layout["staging_jobs"], object_bucket=None,
        ) == [guard.TARGET_DATABASE]

    def test_object_mode_without_explicit_bucket_is_live(
        self, live_layout, monkeypatch,
    ) -> None:
        """P0-1 的核心:对象存储下 staging jobs_dir 完全不构成隔离。"""
        monkeypatch.setenv("MINIO_URL", "minio:9000")
        monkeypatch.setenv("MINIO_BUCKET", "flori")
        assert guard.resolve_live_targets(
            db_path=live_layout["staging_db"], jobs_dir=live_layout["staging_jobs"],
            object_bucket=None,
        ) == [guard.TARGET_OBJECT_STORE]

    def test_object_mode_with_production_bucket_name_is_live(
        self, live_layout, monkeypatch,
    ) -> None:
        monkeypatch.setenv("MINIO_URL", "minio:9000")
        monkeypatch.setenv("MINIO_BUCKET", "flori")
        assert guard.resolve_live_targets(
            db_path=live_layout["staging_db"], jobs_dir=live_layout["staging_jobs"],
            object_bucket="flori",
        ) == [guard.TARGET_OBJECT_STORE]

    def test_object_mode_with_distinct_bucket_is_isolated(
        self, live_layout, monkeypatch,
    ) -> None:
        monkeypatch.setenv("MINIO_URL", "minio:9000")
        monkeypatch.setenv("MINIO_BUCKET", "flori")
        assert guard.resolve_live_targets(
            db_path=live_layout["staging_db"], jobs_dir=live_layout["staging_jobs"],
            object_bucket="flori-import-staging",
        ) == []

    def test_object_mode_ignores_live_jobs_dir(self, live_layout, monkeypatch) -> None:
        """对象模式下本地 jobs_dir 不参与寻址,不该因为它是 /data/jobs 就误报。"""
        monkeypatch.setenv("MINIO_URL", "minio:9000")
        monkeypatch.setenv("MINIO_BUCKET", "flori")
        assert guard.resolve_live_targets(
            db_path=live_layout["staging_db"], jobs_dir=live_layout["jobs"],
            object_bucket="isolated",
        ) == []


class TestWriteAuthorization:
    def _authorize(self, layout, **kwargs):
        params = {
            "db_path": layout["staging_db"], "jobs_dir": layout["staging_jobs"],
            "object_bucket": None, "into_live": False, "dr_receipt": None,
        }
        params.update(kwargs)
        return guard.assert_write_authorized(**params)

    def test_isolated_write_needs_nothing(self, live_layout) -> None:
        assert self._authorize(live_layout)["live_targets"] == []

    def test_live_write_without_flag_is_refused(self, live_layout) -> None:
        with pytest.raises(LiveTargetError, match="没有 --into-live"):
            self._authorize(live_layout, db_path=live_layout["db"])

    def test_live_config_write_also_requires_full_live_authorization(
        self, live_layout,
    ) -> None:
        with pytest.raises(LiveTargetError, match="没有 --into-live"):
            self._authorize(live_layout, config_root=live_layout["config"])

    def test_object_mode_default_import_is_refused(
        self, live_layout, monkeypatch,
    ) -> None:
        """默认的、不加任何 flag 的隔离导入,在生产后端上必须失败而不是写生产桶。"""
        monkeypatch.setenv("MINIO_URL", "minio:9000")
        monkeypatch.setenv("MINIO_BUCKET", "flori")
        with pytest.raises(LiveTargetError, match="--object-bucket"):
            self._authorize(live_layout)

    def test_live_write_requires_remote_worker_attestation(
        self, live_layout, tmp_path,
    ) -> None:
        with pytest.raises(LiveTargetError, match=guard.REMOTE_QUIESCE_ENV):
            self._authorize(
                live_layout, db_path=live_layout["db"], into_live=True,
                dr_receipt=_dr_receipt(tmp_path / "dr.json"),
            )

    def test_live_write_requires_dr_receipt(self, live_layout, monkeypatch) -> None:
        monkeypatch.setenv(guard.REMOTE_QUIESCE_ENV, "1")
        with pytest.raises(LiveTargetError, match="exact DR receipt"):
            self._authorize(live_layout, db_path=live_layout["db"], into_live=True)

    def test_live_write_passes_with_all_three_gates(
        self, live_layout, monkeypatch, tmp_path,
    ) -> None:
        monkeypatch.setenv(guard.REMOTE_QUIESCE_ENV, "1")
        report = self._authorize(
            live_layout, db_path=live_layout["db"], into_live=True,
            dr_receipt=_dr_receipt(tmp_path / "dr.json"),
        )
        assert report["live_targets"] == [guard.TARGET_DATABASE]
        assert len(report["dr_receipt"]["archive_sha256"]) == 64
        assert report["dr_receipt"]["validation"]["checksums"] == "ok"
        assert report["dr_receipt"]["deployment_id"] == "test-deployment"
        assert report["dr_receipt"]["coverage"]["covered_targets"] == [
            guard.TARGET_DATABASE,
        ]

    def test_live_write_requires_persistent_deployment_id(
        self, live_layout, monkeypatch, tmp_path,
    ) -> None:
        monkeypatch.setenv(guard.REMOTE_QUIESCE_ENV, "1")
        monkeypatch.delenv(guard.DEPLOYMENT_ID_ENV)
        with pytest.raises(LiveTargetError, match=guard.DEPLOYMENT_ID_ENV):
            self._authorize(
                live_layout, db_path=live_layout["db"], into_live=True,
                dr_receipt=_dr_receipt(tmp_path / "dr.json"),
            )

    def test_live_write_rejects_unbound_deployment_id(
        self, live_layout, monkeypatch, tmp_path,
    ) -> None:
        monkeypatch.setenv(guard.REMOTE_QUIESCE_ENV, "1")
        monkeypatch.setenv(guard.DEPLOYMENT_ID_ENV, "unbound")
        with pytest.raises(LiveTargetError, match="非unbound"):
            self._authorize(
                live_layout, db_path=live_layout["db"], into_live=True,
                dr_receipt=_dr_receipt(tmp_path / "dr.json"),
            )

    def test_live_write_rejects_another_deployment_archive(
        self, live_layout, monkeypatch, tmp_path,
    ) -> None:
        monkeypatch.setenv(guard.REMOTE_QUIESCE_ENV, "1")
        monkeypatch.setenv(guard.DEPLOYMENT_ID_ENV, "another-deployment")
        with pytest.raises(LiveTargetError, match="deployment id"):
            self._authorize(
                live_layout, db_path=live_layout["db"], into_live=True,
                dr_receipt=_dr_receipt(tmp_path / "dr.json"),
            )

    def test_dr_coverage_rejects_excluded_live_jobs(self, live_layout) -> None:
        manifest = {
            "assets": {
                "data": {
                    "included": True,
                    "excluded_external_subtrees": ["jobs"],
                },
            },
        }
        with pytest.raises(LiveTargetError, match="排除了"):
            guard._dr_asset_coverage(manifest, [guard.TARGET_ARTIFACT_ROOT])

    def test_dr_coverage_rejects_excluded_live_database(self, live_layout) -> None:
        manifest = {
            "assets": {
                "data": {
                    "included": True,
                    "excluded_external_subtrees": ["db"],
                },
            },
        }
        with pytest.raises(LiveTargetError, match="排除了"):
            guard._dr_asset_coverage(manifest, [guard.TARGET_DATABASE])

    def test_dr_coverage_rejects_missing_minio_asset(
        self, live_layout, monkeypatch,
    ) -> None:
        monkeypatch.setenv("MINIO_URL", "minio:9000")
        manifest = {
            "assets": {
                "data": {"included": True, "excluded_external_subtrees": []},
                "minio": {"included": False, "reason": "not-configured"},
            },
        }
        with pytest.raises(LiveTargetError, match="minio"):
            guard._dr_asset_coverage(manifest, [guard.TARGET_OBJECT_STORE])

    def test_into_live_against_isolated_target_is_refused(self, live_layout) -> None:
        """开关与目标不符时不静默放行:那通常意味着操作者以为自己在写别的地方。"""
        with pytest.raises(LiveTargetError, match="全在隔离区"):
            self._authorize(live_layout, into_live=True)


class TestDrReceiptVerification:
    def test_arbitrary_file_is_rejected(self, tmp_path) -> None:
        """FLORI_DR_RECEIPT=/etc/hostname 之类必须过不去。"""
        bogus = tmp_path / "hostname"
        bogus.write_text("some-host\n", encoding="utf-8")
        with pytest.raises(LiveTargetError, match="不是可读 JSON"):
            guard.verify_dr_receipt(bogus)

    def test_missing_file_is_rejected(self, tmp_path) -> None:
        with pytest.raises(LiveTargetError, match="不存在"):
            guard.verify_dr_receipt(tmp_path / "nope.json")

    def test_failed_backup_receipt_is_rejected(self, tmp_path) -> None:
        path = tmp_path / "dr.json"
        _dr_receipt(path, status="failed")
        with pytest.raises(LiveTargetError, match="成功的 exact DR"):
            guard.verify_dr_receipt(path)

    def test_missing_archive_is_rejected(self, tmp_path) -> None:
        path = tmp_path / "dr.json"
        _dr_receipt(path)
        (tmp_path / "flori-backup-gen.tar.gz").unlink()
        with pytest.raises(LiveTargetError, match="归档不存在"):
            guard.verify_dr_receipt(path)

    def test_archive_sha_must_match_receipt(self, tmp_path) -> None:
        path = tmp_path / "dr.json"
        _dr_receipt(path)
        archive = tmp_path / "flori-backup-gen.tar.gz"
        archive.write_bytes(b"tampered")
        with pytest.raises(LiveTargetError, match="SHA 与 receipt"):
            guard.verify_dr_receipt(path)

    def test_missing_archive_sidecar_is_rejected(self, tmp_path) -> None:
        path = tmp_path / "dr.json"
        _dr_receipt(path)
        (tmp_path / "flori-backup-gen.tar.gz.sha256").unlink()
        with pytest.raises(LiveTargetError, match="缺少 sha256 sidecar"):
            guard.verify_dr_receipt(path)

    def test_tampered_archive_sidecar_is_rejected(self, tmp_path) -> None:
        path = tmp_path / "dr.json"
        _dr_receipt(path)
        sidecar = tmp_path / "flori-backup-gen.tar.gz.sha256"
        sidecar.write_text(f"{'0' * 64}  flori-backup-gen.tar.gz\n", encoding="utf-8")
        with pytest.raises(LiveTargetError, match="sidecar"):
            guard.verify_dr_receipt(path)

    def test_receipt_cannot_make_an_old_archive_look_fresh(self, tmp_path) -> None:
        path = tmp_path / "dr.json"
        _dr_receipt(path, age_seconds=guard.DEFAULT_DR_MAX_AGE_SEC + 600)
        body = json.loads(path.read_text(encoding="utf-8"))
        body["manifest"]["created_at"] = datetime.now(timezone.utc).isoformat()
        path.write_text(json.dumps(body), encoding="utf-8")
        with pytest.raises(LiveTargetError, match="created_at"):
            guard.verify_dr_receipt(path)

    def test_symlink_archive_is_rejected(self, tmp_path) -> None:
        path = tmp_path / "dr.json"
        _dr_receipt(path)
        archive = tmp_path / "flori-backup-gen.tar.gz"
        actual = tmp_path / "elsewhere.tar.gz"
        archive.rename(actual)
        archive.symlink_to(actual)
        with pytest.raises(LiveTargetError, match="符号链接"):
            guard.verify_dr_receipt(path)

    def test_validator_generation_must_match_receipt(
        self, tmp_path, monkeypatch,
    ) -> None:
        path = tmp_path / "dr.json"
        _dr_receipt(path)
        monkeypatch.setattr(guard, "_validate_dr_archive", lambda _archive: {
            "status": "success", "operation": "validate",
            "generation": "different", "created_at": datetime.now(timezone.utc).isoformat(),
            "checks": {},
        })
        with pytest.raises(LiveTargetError, match="generation"):
            guard.verify_dr_receipt(path)

    def test_wrong_manifest_format_is_rejected(self, tmp_path) -> None:
        path = tmp_path / "dr.json"
        body = json.loads(_dr_receipt(path) and path.read_text(encoding="utf-8"))
        body["manifest"]["format"] = "something-else"
        path.write_text(json.dumps(body), encoding="utf-8")
        with pytest.raises(LiveTargetError, match="manifest.format"):
            guard.verify_dr_receipt(path)

    def test_freshness_comes_from_the_receipt_not_the_mtime(self, tmp_path) -> None:
        """touch 一下就能骗过 mtime;新鲜度必须取自 manifest.created_at。"""
        path = tmp_path / "dr.json"
        _dr_receipt(path, age_seconds=guard.DEFAULT_DR_MAX_AGE_SEC + 600)
        path.touch()  # mtime 是刚刚
        with pytest.raises(LiveTargetError, match="已过期"):
            guard.verify_dr_receipt(path)

    def test_future_timestamp_is_rejected(self, tmp_path) -> None:
        path = tmp_path / "dr.json"
        _dr_receipt(path, age_seconds=-3600)
        with pytest.raises(LiveTargetError, match="未来"):
            guard.verify_dr_receipt(path)

    def test_max_age_override_is_capped(self, tmp_path, monkeypatch) -> None:
        """无上限的 env 覆盖等于把这道门交给调用者关掉。"""
        monkeypatch.setenv(guard.DR_MAX_AGE_ENV, str(guard.DR_MAX_AGE_CEILING_SEC + 1))
        path = tmp_path / "dr.json"
        _dr_receipt(path)
        with pytest.raises(LiveTargetError, match="硬上限"):
            guard.verify_dr_receipt(path)

    def test_max_age_override_within_ceiling_is_honored(
        self, tmp_path, monkeypatch,
    ) -> None:
        monkeypatch.setenv(guard.DR_MAX_AGE_ENV, "172800")
        path = tmp_path / "dr.json"
        _dr_receipt(path, age_seconds=100_000)
        assert guard.verify_dr_receipt(path)["age_seconds"] > 86_400


class TestImportStorageConstruction:
    def test_local_mode_uses_the_given_jobs_dir(self, tmp_path, monkeypatch) -> None:
        monkeypatch.delenv("MINIO_URL", raising=False)
        storage = guard.create_import_storage(tmp_path / "jobs", object_bucket=None)
        assert str(getattr(storage, "jobs_dir")) == str(tmp_path / "jobs")

    def test_object_mode_binds_the_requested_bucket(self, tmp_path, monkeypatch) -> None:
        """构造不连服务器(minio 客户端是惰性的),只验证寻址绑到了隔离桶。"""
        monkeypatch.setenv("MINIO_URL", "minio:9000")
        monkeypatch.setenv("MINIO_BUCKET", "flori")
        storage = guard.create_import_storage(
            tmp_path / "ignored", object_bucket="flori-import-staging",
        )
        assert storage.bucket == "flori-import-staging"
        assert not hasattr(storage, "jobs_dir")

    def test_object_mode_without_bucket_falls_back_to_production(
        self, tmp_path, monkeypatch,
    ) -> None:
        """构造层如实反映"没给桶就是生产桶";拦截由 assert_write_authorized 负责。"""
        monkeypatch.setenv("MINIO_URL", "minio:9000")
        monkeypatch.setenv("MINIO_BUCKET", "flori")
        storage = guard.create_import_storage(tmp_path / "ignored", object_bucket=None)
        assert storage.bucket == "flori"


class _ObjectStorageStub:
    """只暴露 bucket 的对象存储替身:merge 的根一致性判定只需要这一个属性。"""

    def __init__(self, bucket: str) -> None:
        self.bucket = bucket


class TestMergeStorageRootOnObjectStore:
    """旧实现对对象存储直接 return,等于 merge 在生产后端上完全不设防。"""

    def _assert(self, db_path, bucket):
        from shared.content_import import _assert_merge_storage_root

        return _assert_merge_storage_root(db_path, _ObjectStorageStub(bucket))

    def test_live_database_with_production_bucket_is_consistent(
        self, live_layout, monkeypatch,
    ) -> None:
        monkeypatch.setenv("MINIO_BUCKET", "flori")
        self._assert(live_layout["db"], "flori")

    def test_isolated_database_with_isolated_bucket_is_consistent(
        self, live_layout, monkeypatch,
    ) -> None:
        monkeypatch.setenv("MINIO_BUCKET", "flori")
        self._assert(live_layout["staging_db"], "flori-staging")

    def test_live_database_with_isolated_bucket_is_refused(
        self, live_layout, monkeypatch,
    ) -> None:
        """规则 4 靠读目标侧 manifest 判冲突;对着空 staging 桶分类会静默盖掉本地结果。"""
        from shared.content_import import ImportError_

        monkeypatch.setenv("MINIO_BUCKET", "flori")
        with pytest.raises(ImportError_, match="target's own artifact root"):
            self._assert(live_layout["db"], "flori-staging")

    def test_isolated_database_with_production_bucket_is_refused(
        self, live_layout, monkeypatch,
    ) -> None:
        from shared.content_import import ImportError_

        monkeypatch.setenv("MINIO_BUCKET", "flori")
        with pytest.raises(ImportError_, match="target's own artifact root"):
            self._assert(live_layout["staging_db"], "flori")

    def test_storage_without_any_root_is_refused(self, live_layout) -> None:
        from shared.content_import import ImportError_, _assert_merge_storage_root

        with pytest.raises(ImportError_, match="refusing to classify blind"):
            _assert_merge_storage_root(live_layout["db"], object())
