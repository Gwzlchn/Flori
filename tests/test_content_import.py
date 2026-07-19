"""content import 测试:backup->import 端到端往返、幂等、resume、投影与验收。

输入一律用 P2a 的 run_backup 产出的真快照,不手搓假仓库:仓库契约变化必须
在这里同步暴露。
"""

import asyncio
import errno
import json
import shutil
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from shared.content_import import (
    ContentImportError,
    _verify_source_root_identities,
    build_plan,
    main,
    run_import,
)
from shared.content_repository import ContentRepository
from shared.db import SCHEMA_VERSION
from shared.content_import_journal import (
    STATUS_COMPLETE,
    STATUS_MATERIALIZING,
    ContentImportJournal,
)
from shared.storage import LocalStorage
from tests.test_content_backup import (
    MEDIA_ONE,
    MEDIA_THREE,
    MEDIA_TWO,
    do_backup,
    insert_ai_usage,
    insert_collection,
    insert_job,
    insert_part,
    insert_step,
    commit_step,
    db_exec,
    ensure_job_json,
    seed_video_job,
    sha,
    T_CREATED,
    T_FINISHED,
    T_STARTED,
)


# 投影阶段要按仓库真实 pipelines.yaml 展开步骤,不能落到容器默认 /data/configs。
_CONFIGS_DIR = Path(__file__).parent.parent / "configs"


def test_source_root_identity_detects_directory_swap(tmp_path) -> None:
    target = tmp_path / "source-target"
    target.mkdir()
    info = target.stat()
    expected = {"nas-main": f"{info.st_dev}:{info.st_ino}"}
    assert _verify_source_root_identities(
        {"nas-main": target}, expected,
    ) == expected

    target.rename(tmp_path / "displaced-source-target")
    target.mkdir()
    with pytest.raises(ContentImportError, match="changed before container validation"):
        _verify_source_root_identities({"nas-main": target}, expected)


@pytest.fixture
def source(tmp_path, current_schema_db_template):
    """备份侧环境:真 schema 库 + LocalStorage + 便携仓库。"""
    db_path = tmp_path / "src" / "flori.db"
    db_path.parent.mkdir(parents=True)
    shutil.copy(current_schema_db_template, db_path)
    return SimpleNamespace(
        db=db_path,
        jobs_dir=tmp_path / "src" / "jobs",
        storage=LocalStorage(tmp_path / "src" / "jobs"),
        repo=ContentRepository.create(tmp_path / "repo"),
        tmp=tmp_path,
        parts={},
    )


@pytest.fixture
def target(tmp_path):
    """导入侧空环境:目标库路径尚不存在,对象根为空目录。"""
    root = tmp_path / "dst"
    root.mkdir()
    return SimpleNamespace(
        db=root / "db" / "analyzer.db",
        jobs_dir=root / "jobs",
        storage=LocalStorage(root / "jobs"),
        journal=root / "journal.sqlite3",
        root=root,
    )


async def do_import(source, target, *, generation="gen-1", **kwargs):
    kwargs.setdefault("config_dir", _CONFIGS_DIR)
    return await run_import(
        repository=source.repo,
        snapshot=kwargs.pop("snapshot", "latest"),
        target_db_path=target.db,
        storage=target.storage,
        journal_path=target.journal,
        target_generation=generation,
        **kwargs,
    )


def query(db_path: Path, sql: str, params: tuple = ()):
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in connection.execute(sql, params)]
    finally:
        connection.close()


def count_objects(root: Path) -> int:
    return sum(1 for path in root.rglob("*") if path.is_file())


def current_definition_digest(source, job_id: str, step: str, *, part_id: str | None = None):
    """算出与当前 configs 匹配的真 definition_digest,让投影能真正判 done。

    domain/style_tags 取自 jobs 行,与 rebuild_projection 的取值一致;
    错一个字段就会得到不同摘要,那正是 stale_manifest 分支要覆盖的情况。
    """
    from shared.config import load_config
    from shared.models import JobPart
    from shared.pipeline_scope import expand_pipeline_steps
    from shared.step_completion import step_definition_digest_for
    from shared.step_scope import execution_step_key

    config = load_config(_CONFIGS_DIR)
    [row] = query(source.db, "SELECT pipeline, domain, style_tags FROM jobs WHERE id=?", (job_id,))
    parts = [
        JobPart(id=item["id"], job_id=job_id, part_index=item["part_index"])
        for item in query(
            source.db,
            "SELECT id, part_index FROM job_parts WHERE job_id=? ORDER BY part_index",
            (job_id,),
        )
    ]
    expanded = expand_pipeline_steps(
        config.pipelines[row["pipeline"]].get("steps", []), parts,
    )
    key = execution_step_key(f"part:{part_id}", step) if part_id else step
    return step_definition_digest_for(
        row["pipeline"], expanded[key], config=config,
        domain=row["domain"] or "", style_tags=json.loads(row["style_tags"] or "[]"),
    )


async def seed_multi_job(source):
    """3 个 Job:多 Part 视频 + 单 Part 视频 + document;覆盖跨 Job 循环与多连接场景。"""
    await seed_video_job(source, "job_alpha")
    insert_job(source, "job_gamma")
    insert_part(source, "pt_gamma1", "job_gamma", 1)
    insert_step(source, "job_gamma", "part:pt_gamma1", "01_download", "done",
                started_at=T_STARTED, finished_at=T_FINISHED)
    await commit_step(
        source, "job_gamma", "part:pt_gamma1", "01_download",
        {"input/source.mp4": MEDIA_THREE}, part_index=1, exec_id="exec_gamma",
    )
    insert_job(source, "job_doc", content_type="document", document_kind="article")
    await ensure_job_json(source)


class TestMultiJobEndToEnd:
    async def test_three_jobs_import_without_lock_contention(self, source, target):
        """P0-1 回归:投影跨多个 Job 必须单连接,否则第 2 个 Job 起 database is locked。"""
        await seed_multi_job(source)
        await do_backup(source)
        result = await do_import(source, target)
        assert result.verification["counts"]["jobs"] == 3
        assert result.verification["counts"]["job_parts"] == 3
        # 每个 Job 都真的展开了步骤(不是静默 0 行)
        rows = query(
            target.db, "SELECT job_id, COUNT(*) c FROM job_steps GROUP BY job_id",
        )
        assert {row["job_id"] for row in rows} == {"job_alpha", "job_gamma", "job_doc"}
        for row in rows:
            assert row["c"] > 0
        assert result.projection["steps"] == sum(row["c"] for row in rows)

    async def test_ingested_items_are_materialized(self, source, target):
        """P1-4 回归:ingested_item 曾漏在 MATERIALIZE_ORDER,处理器写了却永不调用。"""
        insert_collection(source, "col_rss")
        db_exec(source.db, (
            "INSERT INTO ingested_items (collection_id, item_id, ingested_at)"
            " VALUES ('col_rss','item-1',?)"
        ), (T_CREATED,))
        db_exec(source.db, (
            "INSERT INTO ingested_items (collection_id, item_id, ingested_at)"
            " VALUES ('col_rss','item-2',?)"
        ), (T_CREATED,))
        await seed_multi_job(source)
        await do_backup(source)
        await do_import(source, target)
        rows = query(target.db, "SELECT item_id FROM ingested_items ORDER BY item_id")
        assert [row["item_id"] for row in rows] == ["item-1", "item-2"]

    def test_every_backup_kind_has_a_materializer(self):
        """构造性防漏:备份侧可能产出的每个 kind 都必须在 MATERIALIZE_ORDER 里。"""
        from shared.content_import import MATERIALIZE_ORDER
        from shared.content_policy import RECORD_KINDS

        assert RECORD_KINDS <= set(MATERIALIZE_ORDER), (
            f"未覆盖的 record kind: {sorted(RECORD_KINDS - set(MATERIALIZE_ORDER))}"
        )


class TestProjectionBranches:
    async def test_matching_definition_projects_done(self, source, target):
        """真 definition_digest → 投影判 done(此前所有用例全 waiting,分支零覆盖)。"""
        job_id = "job_real"
        insert_job(source, job_id)
        insert_part(source, "pt_r1", job_id, 1)
        insert_step(source, job_id, "part:pt_r1", "01_download", "done",
                    started_at=T_STARTED, finished_at=T_FINISHED)
        digest = current_definition_digest(source, job_id, "01_download", part_id="pt_r1")
        await commit_step(
            source, job_id, "part:pt_r1", "01_download",
            {"input/source.mp4": MEDIA_ONE}, part_index=1,
            definition_digest=digest,
        )
        await do_backup(source)
        result = await do_import(source, target)
        assert result.projection["done"] >= 1
        [row] = query(
            target.db,
            "SELECT status FROM job_steps WHERE job_id=? AND step='01_download'",
            (job_id,),
        )
        assert row["status"] == "done"

    async def test_deterministic_skip_projects_skipped(self, source, target):
        job_id = "job_skip"
        insert_job(source, job_id)
        insert_part(source, "pt_s1", job_id, 1)
        insert_step(source, job_id, "part:pt_s1", "01_download", "skipped")
        digest = current_definition_digest(source, job_id, "01_download", part_id="pt_s1")
        await commit_step(
            source, job_id, "part:pt_s1", "01_download", {}, part_index=1,
            outcome="skipped", skip_reason="rule_false", definition_digest=digest,
        )
        await do_backup(source)
        result = await do_import(source, target)
        assert result.projection["skipped"] >= 1
        [row] = query(
            target.db,
            "SELECT status FROM job_steps WHERE job_id=? AND step='01_download'",
            (job_id,),
        )
        assert row["status"] == "skipped"

    async def test_tampered_output_bytes_block_done(self, source, target):
        """P2-12:同名同长度但字节不同的产物不得被判 done。"""
        job_id = "job_tamper"
        insert_job(source, job_id)
        insert_part(source, "pt_t1", job_id, 1)
        insert_step(source, job_id, "part:pt_t1", "01_download", "done")
        digest = current_definition_digest(source, job_id, "01_download", part_id="pt_t1")
        await commit_step(
            source, job_id, "part:pt_t1", "01_download",
            {"input/source.mp4": MEDIA_ONE}, part_index=1, definition_digest=digest,
        )
        await do_backup(source)
        await do_import(source, target)
        # 导入后把目标对象换成等长不同字节,再单独跑一次投影
        restored = (
            target.jobs_dir / job_id / "parts" / "pt_t1" / "input" / "source.mp4"
        )
        restored.write_bytes(bytes(len(MEDIA_ONE)))
        from shared.config import load_config
        from shared.content_import import rebuild_projection

        connection = sqlite3.connect(target.db)
        connection.row_factory = sqlite3.Row
        try:
            projection = await rebuild_projection(
                connection=connection, storage=target.storage,
                config=load_config(_CONFIGS_DIR),
            )
        finally:
            connection.close()
        assert projection["done"] == 0
        assert projection["reasons"].get("output_mismatch", 0) >= 1

    async def test_waiting_upstream_suppresses_downstream(self, source, target):
        """P1-6:上游未完成时下游即便 manifest 有效也必须 waiting。"""
        job_id = "job_dag"
        insert_job(source, job_id)
        insert_part(source, "pt_d1", job_id, 1)
        # 只给下游 02_whisper 有效 manifest,上游 01_download 完全缺失
        insert_step(source, job_id, "part:pt_d1", "02_whisper", "done")
        digest = current_definition_digest(source, job_id, "02_whisper", part_id="pt_d1")
        await commit_step(
            source, job_id, "part:pt_d1", "02_whisper",
            {"intermediate/transcript.json": b"{}"}, part_index=1,
            definition_digest=digest,
        )
        await do_backup(source, allow_unknown=True)
        result = await do_import(source, target)
        [row] = query(
            target.db,
            "SELECT status, meta FROM job_steps WHERE job_id=? AND step='02_whisper'",
            (job_id,),
        )
        assert row["status"] == "waiting"
        assert "upstream_invalid" in row["meta"]
        assert result.projection["reasons"].get("upstream_invalid", 0) >= 1

    async def test_structurally_invalid_manifest_is_not_done(self, source, target):
        """P1-7:manifest 结构非法时不得走到函数尾部的 DONE 出口。"""
        await seed_video_job(source)
        await do_backup(source)
        await do_import(source, target)
        manifest_path = (
            target.jobs_dir / "job_alpha" / "parts" / "pt_alpha1"
            / ".flori" / "steps" / "01_download" / "manifest.json"
        )
        manifest_path.write_text(json.dumps({"format": "flori-step-manifest"}))
        from shared.config import load_config
        from shared.content_import import rebuild_projection

        connection = sqlite3.connect(target.db)
        connection.row_factory = sqlite3.Row
        try:
            projection = await rebuild_projection(
                connection=connection, storage=target.storage,
                config=load_config(_CONFIGS_DIR),
            )
        finally:
            connection.close()
        assert projection["done"] == 0

    async def test_unknown_pipeline_is_reported(self, source, target):
        """P2-19:快照 pipeline 不在当前配置 → plan 冲突,不静默 pending。"""
        insert_job(source, "job_ghost", pipeline="pipeline_that_does_not_exist",
                   content_type="document", document_kind="article")
        await do_backup(source)
        from shared.config import load_config
        from shared.content_import import build_plan

        plan, _b, _r = build_plan(
            repository=source.repo, snapshot="latest", target_db_path=target.db,
            config=load_config(_CONFIGS_DIR),
        )
        assert not plan.ok
        assert any("pipelines missing" in item for item in plan.conflicts)


class TestPlan:
    def test_v2_readiness_consumes_producer_completeness_shape(self):
        import shared.content_import as module

        completeness = {
            "terminal_steps": 8,
            "manifests_seen": 8,
            "manifests_missing": 0,
            "manifests_excluded": 0,
            "ai_config_complete": True,
            "user_config_complete": True,
            "secret_scan_complete": True,
            "media_self_contained": True,
            "external_media_roots": [],
            "portable_ready": True,
            "readiness_reasons": [],
        }

        ready, reason, reasons, projected = module._snapshot_readiness({
            "format": "flori-portable-snapshot/v2",
            "completeness": completeness,
        })

        assert ready is True and reason is None and reasons == []
        assert projected == completeness

    async def test_plan_is_readonly_and_machine_readable(self, source, target):
        await seed_video_job(source)
        await do_backup(source)
        plan, body, records = build_plan(
            repository=source.repo, snapshot="latest", target_db_path=target.db,
        )
        assert plan.ok and not plan.conflicts
        assert plan.counts["blobs"] == 2
        assert plan.bytes_to_write == len(MEDIA_ONE) + len(MEDIA_TWO)
        assert plan.plan_digest.startswith("sha256:")
        assert not target.db.exists(), "--plan 不得写目标库"
        assert len(records) == plan.counts["insert"]

    async def test_partial_snapshot_needs_explicit_optin(self, source, target):
        await seed_video_job(source)
        insert_job(source, "job_solo", content_type="document", document_kind="article")
        await do_backup(source, "run_full")
        await do_backup(source, "run_part", job_ids=["job_alpha"], ref="only-alpha")
        plan, _b, _r = build_plan(
            repository=source.repo, snapshot="only-alpha", target_db_path=target.db,
        )
        assert not plan.ok
        assert any("partial" in item for item in plan.conflicts)
        allowed, _b, _r = build_plan(
            repository=source.repo, snapshot="only-alpha",
            target_db_path=target.db, allow_partial=True,
        )
        assert allowed.ok and allowed.partial

    async def test_non_empty_target_is_conflict(self, source, target, current_schema_db_template):
        await seed_video_job(source)
        await do_backup(source)
        target.db.parent.mkdir(parents=True)
        shutil.copy(current_schema_db_template, target.db)
        db_exec(target.db, (
            "INSERT INTO jobs (id, content_type, document_kind, pipeline, status,"
            " created_at, updated_at) VALUES ('squatter','document','article','p',"
            "'pending',?,?)"
        ), (T_CREATED, T_CREATED))
        plan, _b, _r = build_plan(
            repository=source.repo, snapshot="latest", target_db_path=target.db,
        )
        assert not plan.ok
        assert any("not empty" in item for item in plan.conflicts)

    async def test_incomplete_snapshot_live_write_requires_double_risk_acceptance(
        self, source, target, tmp_path, monkeypatch,
    ):
        await seed_video_job(source)
        await do_backup(source)
        monkeypatch.delenv("FLORI_ACCEPT_INCOMPLETE_PORTABLE", raising=False)

        with pytest.raises(
            ContentImportError,
            match="--allow-incomplete-portable-snapshot.*FLORI_ACCEPT_INCOMPLETE_PORTABLE",
        ):
            await do_import(
                source,
                target,
                into_live=True,
                allow_incomplete_portable_snapshot=True,
                config_root=tmp_path / "live-prompts",
            )
        assert not target.db.exists()

        monkeypatch.setenv("FLORI_ACCEPT_INCOMPLETE_PORTABLE", "1")
        result = await do_import(
            source,
            target,
            into_live=True,
            allow_incomplete_portable_snapshot=True,
            config_root=tmp_path / "live-prompts",
        )
        assert result.plan.portable_ready is False
        assert result.plan.readiness_reason == "user_config_incomplete"

    def test_v1_snapshot_is_readable_but_never_inferred_portable_ready(self):
        from shared.content_import import _snapshot_readiness

        ready, reason, reasons, completeness = _snapshot_readiness({
            "format": "flori-portable-snapshot/v1",
        })
        assert ready is False
        assert reason == "legacy_snapshot_without_completeness"
        assert reasons == ["legacy_snapshot_without_completeness"]
        assert completeness == {}

    async def test_isolated_import_rejects_real_live_prompts_root(self, source, target):
        from shared.content_import import DEFAULT_LIVE_CONFIG_ROOT

        await seed_video_job(source)
        await do_backup(source)

        with pytest.raises(ContentImportError, match="active prompts root"):
            await do_import(
                source,
                target,
                config_root=Path(DEFAULT_LIVE_CONFIG_ROOT),
            )
        assert not target.db.exists()

    async def test_freshly_migrated_empty_target_is_accepted(
        self, source, target, current_schema_db_template,
    ):
        await seed_video_job(source)
        await do_backup(source)
        target.db.parent.mkdir(parents=True)
        shutil.copy(current_schema_db_template, target.db)
        plan, _b, _r = build_plan(
            repository=source.repo, snapshot="latest", target_db_path=target.db,
        )
        assert plan.ok

    async def test_corrupt_blob_blocks_plan(self, source, target):
        await seed_video_job(source)
        await do_backup(source)
        blob = source.repo.blob_path(sha(MEDIA_ONE))
        data = bytearray(blob.read_bytes())
        data[-1] ^= 0x01
        blob.write_bytes(bytes(data))
        with pytest.raises(ContentImportError, match="blob chain verification failed"):
            build_plan(
                repository=source.repo, snapshot="latest", target_db_path=target.db,
            )


class TestEmptyImport:
    async def test_portable_v2_ready_snapshot_roundtrips_without_override(
        self, source, target, tmp_path,
    ):
        await seed_video_job(source)
        prompts = tmp_path / "prompts"
        prompts.mkdir()
        backup = await do_backup(source, user_config_dir=prompts)
        snapshot = source.repo.get_snapshot(backup.snapshot_digest)
        assert snapshot["format"] == "flori-portable-snapshot/v2"
        assert snapshot["completeness"]["portable_ready"] is True

        result = await do_import(
            source,
            target,
            config_root=tmp_path / "restored-prompts",
        )
        assert result.plan.portable_ready is True
        assert result.plan.readiness_reasons == []
        assert result.verification["schema_version"] == SCHEMA_VERSION

    async def test_full_roundtrip_rebuilds_current_schema(self, source, target):
        """§5.2.12:空库导入,FK/integrity/migration validator 全过。"""
        insert_collection(source, "col_1")
        await seed_video_job(source)
        db_exec(source.db, "UPDATE jobs SET collection_id='col_1' WHERE id='job_alpha'")
        insert_ai_usage(source, "exec_ai_1", "job_alpha", "part:pt_alpha1::01_download")
        await do_backup(source)

        result = await do_import(source, target)
        assert result.verification["schema_version"] == SCHEMA_VERSION
        assert result.verification["counts"]["jobs"] == 1
        assert result.verification["counts"]["job_parts"] == 2
        assert result.verification["counts"]["ai_usage"] == 1
        # 业务事实按 allowlist 还原
        [job] = query(target.db, "SELECT * FROM jobs")
        assert job["id"] == "job_alpha"
        assert job["collection_id"] == "col_1"
        assert job["url"] == "https://www.bilibili.com/video/BV1xx411c7mD"
        parts = query(target.db, "SELECT * FROM job_parts ORDER BY part_index")
        assert [row["part_index"] for row in parts] == [1, 2]
        # 产物字节逐个还原
        for part_id, media in (("pt_alpha1", MEDIA_ONE), ("pt_alpha2", MEDIA_TWO)):
            restored = target.jobs_dir / "job_alpha" / "parts" / part_id / "input" / "source.mp4"
            assert restored.read_bytes() == media
        # collections.job_count 由重建得到,不来自快照
        [collection] = query(target.db, "SELECT * FROM collections")
        assert collection["job_count"] == 1

    async def test_status_is_reprojected_not_copied(self, source, target):
        """§2.9:备份时 failed 的 Job,只要 manifest 兼容,导入后不复刻 failed。"""
        await seed_video_job(source)
        db_exec(source.db, "UPDATE jobs SET status='failed', error='boom' WHERE id='job_alpha'")
        await do_backup(source)
        await do_import(source, target)
        [job] = query(target.db, "SELECT status, error, progress_pct FROM jobs")
        assert job["status"] != "failed"
        assert job["error"] is None
        # 步骤状态来自当前 pipeline 展开,不是快照里的行
        statuses = query(target.db, "SELECT status, COUNT(*) c FROM job_steps GROUP BY status")
        assert statuses, "必须重建出 job_steps"

    async def test_manifest_published_last(self, source, target):
        await seed_video_job(source)
        await do_backup(source)
        await do_import(source, target)
        for part_id in ("pt_alpha1", "pt_alpha2"):
            manifest = (
                target.jobs_dir / "job_alpha" / "parts" / part_id
                / ".flori" / "steps" / "01_download" / "manifest.json"
            )
            assert manifest.is_file()
            payload = json.loads(manifest.read_text())
            assert payload["outcome"] == "done"

    async def test_failure_events_do_not_become_active_state(self, source, target):
        """§2.4B:失败审计不恢复成活动失败状态。"""
        job_id = "job_fail"
        insert_job(source, job_id)
        insert_part(source, "pt_f1", job_id, 1)
        insert_step(source, job_id, "part:pt_f1", "01_download", "failed",
                    error="boom", started_at=T_STARTED, finished_at=T_FINISHED)
        await do_backup(source)
        await do_import(source, target)
        [job] = query(target.db, "SELECT status FROM jobs")
        assert job["status"] != "failed"
        assert query(target.db, "SELECT * FROM job_steps WHERE status='failed'") == []


class TestIdempotency:
    async def test_second_import_is_refused_as_already_done(self, source, target):
        """§5.2.13:同 snapshot 同 generation 再导入即 no-op,不重复写业务行。"""
        await seed_video_job(source)
        await do_backup(source)
        await do_import(source, target)
        rows_before = query(target.db, "SELECT COUNT(*) c FROM jobs")[0]["c"]
        objects_before = count_objects(target.jobs_dir)
        with pytest.raises(ContentImportError, match="already imported"):
            await do_import(source, target)
        assert query(target.db, "SELECT COUNT(*) c FROM jobs")[0]["c"] == rows_before
        assert count_objects(target.jobs_dir) == objects_before

    async def test_reimport_into_fresh_generation_reuses_objects(self, source, target):
        """§5.2.14:清 DB 保留对象后再导入,对象不重传但逐个 hash 核验。"""
        await seed_video_job(source)
        await do_backup(source)
        first = await do_import(source, target)
        assert first.materialized["objects_written"] == 2

        # 清库保留对象:模拟"清空 SQLite,MinIO 原样"
        target.db.unlink()
        second = await do_import(source, target, generation="gen-2")
        assert second.materialized["objects_written"] == 0
        assert second.materialized["objects_reused"] == 2
        # DB 重建结果相同
        assert query(target.db, "SELECT id FROM jobs") == [{"id": "job_alpha"}]

    async def test_existing_object_with_different_bytes_is_refused(self, source, target):
        await seed_video_job(source)
        await do_backup(source)
        target_path = (
            target.jobs_dir / "job_alpha" / "parts" / "pt_alpha1" / "input" / "source.mp4"
        )
        target_path.parent.mkdir(parents=True)
        target_path.write_bytes(b"SQUATTER")
        with pytest.raises(ContentImportError, match="refuses to overwrite"):
            await do_import(source, target)
        assert target_path.read_bytes() == b"SQUATTER", "拒绝时不得覆盖既有对象"

    async def test_request_identity_rejects_same_generation_with_new_storage(
        self, source, target,
    ):
        await seed_video_job(source)
        await do_backup(source)
        await do_import(source, target)
        other_storage = LocalStorage(target.root / "other-jobs")

        with pytest.raises(ContentImportError, match="different database, storage"):
            await run_import(
                repository=source.repo,
                snapshot="latest",
                target_db_path=target.db,
                storage=other_storage,
                journal_path=target.journal,
                target_generation="gen-1",
                config_dir=_CONFIGS_DIR,
            )


def test_journal_claim_is_atomic_across_process_connections(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    path = tmp_path / "claim.sqlite3"
    barrier = Barrier(2)
    digest = "sha256:" + "a" * 64
    plan = "sha256:" + "b" * 64
    request = "sha256:" + "c" * 64

    def claim(import_id):
        with ContentImportJournal(path) as journal:
            barrier.wait()
            return journal.begin(
                import_id=import_id,
                snapshot_digest=digest,
                target_generation="gen-atomic",
                plan_digest=plan,
                request_digest=request,
            ).import_id

    with ThreadPoolExecutor(max_workers=2) as executor:
        claimed = list(executor.map(claim, ("imp_first", "imp_second")))

    assert len(set(claimed)) == 1
    with ContentImportJournal(path) as journal:
        assert len(journal.list_all()) == 1


class TestMergeSafety:
    async def test_part_core_conflict_compares_full_identity(self, source, target):
        await seed_video_job(source)
        await do_backup(source, "run-before")
        await do_import(source, target)
        db_exec(
            source.db,
            "UPDATE job_parts SET title='changed title' WHERE id='pt_alpha1'",
        )
        await do_backup(source, "run-after")

        result = await do_import(
            source, target, generation="gen-merge", mode="merge",
        )

        conflicts = result.merge_report["conflicts"]
        assert any(
            item["kind"] == "part_core" and item["conflict"] == "job_identity"
            for item in conflicts
        )
        [part] = query(
            target.db, "SELECT title FROM job_parts WHERE id='pt_alpha1'",
        )
        assert part["title"] != "changed title"

    async def test_duplicate_ai_audit_identity_is_not_treated_as_noop(
        self, source, target,
    ):
        insert_job(source, "job_audit", content_type="document", document_kind="article")
        db_exec(source.db, (
            "INSERT INTO ai_task_logs "
            "(task_id, exec_id, step_name, domain, provider, model, ok, created_at) "
            "VALUES ('task-1','exec-1','notes','ml','claude','model',1,?)"
        ), (T_CREATED,))
        await do_backup(source)
        await do_import(source, target)
        db_exec(target.db, (
            "INSERT INTO ai_task_logs "
            "(task_id, exec_id, step_name, domain, provider, model, ok, created_at) "
            "SELECT task_id, exec_id, step_name, domain, provider, model, ok, created_at "
            "FROM ai_task_logs LIMIT 1"
        ))

        with pytest.raises(ContentImportError, match="immutable ledger conflicts"):
            await do_import(
                source, target, generation="gen-merge-audit", mode="merge",
            )

    async def test_projection_failure_rolls_back_merge_database(
        self, source, target, monkeypatch,
    ):
        insert_job(source, "job_existing", content_type="document", document_kind="article")
        await do_backup(source, "run-base")
        await do_import(source, target)
        insert_job(source, "job_new", content_type="document", document_kind="article")
        await do_backup(source, "run-new")
        import shared.content_import as module

        original = module.verify_target
        monkeypatch.setattr(
            module,
            "verify_target",
            lambda **kwargs: (_ for _ in ()).throw(_Kill("merge verification")),
        )
        with pytest.raises(_Kill):
            await do_import(
                source, target, generation="gen-merge-rollback", mode="merge",
            )
        monkeypatch.setattr(module, "verify_target", original)

        assert query(target.db, "SELECT id FROM jobs ORDER BY id") == [
            {"id": "job_existing"},
        ]
        result = await do_import(
            source, target, generation="gen-merge-rollback", mode="merge",
        )
        # journal 中的 insert 记录会先被全量闭包校验发现 DB 已回滚,随后清进度重放。
        assert result.resumed is False
        assert query(target.db, "SELECT id FROM jobs ORDER BY id") == [
            {"id": "job_existing"}, {"id": "job_new"},
        ]


class TestResume:
    async def test_resume_skips_already_materialized_records(
        self, source, target, monkeypatch,
    ):
        """§5.2.17:中断后按 journal resume,不从零复制已验证内容。"""
        await seed_video_job(source)
        await do_backup(source)

        # 第一次跑到一半崩:验收阶段抛错,journal 已记下物化进度
        import shared.content_import as module

        original = module.verify_target
        monkeypatch.setattr(
            module, "verify_target", lambda **kw: (_ for _ in ()).throw(_Kill("x")),
        )
        with pytest.raises(_Kill):
            await do_import(source, target)
        monkeypatch.setattr(module, "verify_target", original)
        with ContentImportJournal(target.journal) as journal:
            entry = journal.find(source.repo.get_ref("latest"), "gen-1")
            assert entry is not None
            processed = journal.processed_digests(entry.import_id)
        assert processed, "崩溃前的进度必须已登记"

        # resume:同 generation 重跑,已登记 record 不再重复物化
        result = await do_import(source, target)
        assert result.resumed is True
        assert result.materialized["records_resumed"] == len(processed)
        assert result.materialized["objects_written"] == 0
        assert query(target.db, "SELECT id FROM jobs") == [{"id": "job_alpha"}]

    async def test_journal_binds_to_target_generation(self, source, target):
        await seed_video_job(source)
        await do_backup(source)
        await do_import(source, target, generation="gen-a")
        with ContentImportJournal(target.journal) as journal:
            digest = source.repo.get_ref("latest")
            assert journal.find(digest, "gen-a").status == STATUS_COMPLETE
            # 另一个 generation 是另一次导入,不复用旧进度
            assert journal.find(digest, "gen-b") is None

    async def test_failed_import_leaves_journal_evidence(
        self, source, target, monkeypatch,
    ):
        await seed_video_job(source)
        await do_backup(source)
        import shared.content_import as module

        monkeypatch.setattr(
            module, "verify_target", lambda **kw: (_ for _ in ()).throw(_Kill("x")),
        )
        with pytest.raises(_Kill):
            await do_import(source, target)
        # journal 独立于被丢弃的目标库存活(§2.10 阶段5)
        assert target.journal.is_file()
        with ContentImportJournal(target.journal) as journal:
            entry = journal.find(source.repo.get_ref("latest"), "gen-1")
            assert entry.status == "failed"
            assert "error" in entry.summary


class _Kill(RuntimeError):
    """定点注入的模拟崩溃。"""


class TestCrashInjection:
    """在四个边界注入崩溃,断言 resume 幂等且不撞 UNIQUE(P1-5/P0-2)。"""

    async def _crash_then_resume(self, source, target, monkeypatch, patch):
        await seed_multi_job(source)
        await do_backup(source)
        undo = patch()
        with pytest.raises(_Kill):
            await do_import(source, target)
        undo()
        result = await do_import(source, target)
        # 无论崩在哪,续跑都必须得到同一套业务事实
        assert result.verification["counts"]["jobs"] == 3
        assert query(target.db, "SELECT COUNT(*) c FROM job_parts")[0]["c"] == 3
        return result

    async def test_crash_midway_through_materializing(self, source, target, monkeypatch):
        import shared.content_import as module

        original = module._Materializer._put_part_core
        state = {"n": 0}

        def boom(self, body):
            state["n"] += 1
            if state["n"] == 2:
                raise _Kill("materializing")
            return original(self, body)

        def patch():
            monkeypatch.setattr(module._Materializer, "_put_part_core", boom)
            return lambda: monkeypatch.setattr(
                module._Materializer, "_put_part_core", original,
            )

        await self._crash_then_resume(source, target, monkeypatch, patch)

    async def test_crash_between_db_commit_and_journal_mark(
        self, source, target, monkeypatch,
    ):
        """最狠的窗口:库已提交、journal 未登记 → 重放必须撞不上 UNIQUE。"""
        import shared.content_import as module

        original = module._Materializer._mark
        state = {"n": 0}

        def boom(self, kind, digest, natural_key, action):
            state["n"] += 1
            if state["n"] == 3:
                raise _Kill("between commit and journal")
            return original(self, kind, digest, natural_key, action)

        def patch():
            monkeypatch.setattr(module._Materializer, "_mark", boom)
            return lambda: monkeypatch.setattr(module._Materializer, "_mark", original)

        await self._crash_then_resume(source, target, monkeypatch, patch)

    async def test_crash_during_projection(self, source, target, monkeypatch):
        import shared.content_import as module

        original = module._upsert_step_row
        state = {"n": 0}

        def boom(*args, **kwargs):
            state["n"] += 1
            if state["n"] == 2:
                raise _Kill("projecting")
            return original(*args, **kwargs)

        def patch():
            monkeypatch.setattr(module, "_upsert_step_row", boom)
            return lambda: monkeypatch.setattr(module, "_upsert_step_row", original)

        await self._crash_then_resume(source, target, monkeypatch, patch)

    async def test_crash_just_before_complete(self, source, target, monkeypatch):
        import shared.content_import as module

        original = module.verify_target

        def boom(**kwargs):
            raise _Kill("before complete")

        def patch():
            monkeypatch.setattr(module, "verify_target", boom)
            return lambda: monkeypatch.setattr(module, "verify_target", original)

        await self._crash_then_resume(source, target, monkeypatch, patch)


class TestStageFiveDiscard:
    async def test_discarded_target_must_not_report_success(
        self, source, target, monkeypatch,
    ):
        """P0-2 回归门:阶段5 丢弃新库后同 generation 重跑,绝不能报成功。"""
        import shared.content_import as module

        await seed_multi_job(source)
        await do_backup(source)
        original = module.verify_target
        monkeypatch.setattr(
            module, "verify_target", lambda **kwargs: (_ for _ in ()).throw(_Kill("x")),
        )
        with pytest.raises(_Kill):
            await do_import(source, target)
        monkeypatch.setattr(module, "verify_target", original)

        # 阶段5:丢弃新库(journal 独立存活)
        shutil.rmtree(target.db.parent)
        assert target.journal.is_file(), "journal 必须活过目标库被丢弃"

        with pytest.raises(ContentImportError, match="different target database"):
            await do_import(source, target)
        # 换 generation 才是正确出路,且必须真的重建出内容
        result = await do_import(source, target, generation="gen-2")
        assert result.verification["counts"]["jobs"] == 3

    async def test_journal_default_is_outside_target_dir(self):
        """P1-9:默认 journal 不能落在目标库目录内,否则阶段5 一删就没证据。"""
        from shared.content_import import DEFAULT_JOURNAL_PATH

        assert not DEFAULT_JOURNAL_PATH.startswith("/data/db/")
        assert DEFAULT_JOURNAL_PATH.startswith("/data/")

    async def test_idempotent_rerun_keeps_complete_status(self, source, target):
        """P0-3:幂等重跑不得把 status=complete 改写成 failed。"""
        await seed_multi_job(source)
        await do_backup(source)
        await do_import(source, target)
        digest = source.repo.get_ref("latest")
        for _attempt in range(2):
            with pytest.raises(ContentImportError, match="already imported"):
                await do_import(source, target)
            with ContentImportJournal(target.journal) as journal:
                assert journal.find(digest, "gen-1").status == STATUS_COMPLETE


class TestProjection:
    async def test_incomplete_restore_waits_for_explicit_activation(self, source, target):
        await seed_video_job(source)
        await do_backup(source)

        result = await do_import(source, target)

        assert result.projection["waiting"] > 0
        [job] = query(target.db, "SELECT status, error FROM jobs WHERE id='job_alpha'")
        assert job == {"status": "pending_activation", "error": None}

    async def test_incompatible_definition_becomes_waiting(self, source, target, tmp_path):
        """§5.2.16:旧 definition manifest 导入新配置一律 waiting,不伪装 done。"""
        await seed_video_job(source)
        await do_backup(source)
        result = await do_import(source, target)
        # fixture manifest 的 definition_digest 是合成值,与当前配置必然不符
        reasons = result.projection["reasons"]
        assert reasons.get("stale_manifest", 0) + reasons.get("missing_manifest", 0) > 0
        assert result.projection["done"] == 0
        # 仓库历史结果仍可审计
        body = source.repo.get_snapshot(source.repo.get_ref("latest"))
        assert len(body["records"]["step_results"]) == 2

    async def test_projection_reason_recorded_on_step(self, source, target):
        await seed_video_job(source)
        await do_backup(source)
        await do_import(source, target)
        rows = query(target.db, "SELECT step, status, meta FROM job_steps")
        assert rows, "必须展开出步骤"
        for row in rows:
            assert row["status"] == "waiting"
            assert "projection_reason" in (row["meta"] or "")

    async def test_no_auto_enqueue(self, source, target):
        """普通导入不自动 enqueue:没有 ready/running 步骤。"""
        await seed_video_job(source)
        await do_backup(source)
        await do_import(source, target)
        active = query(
            target.db, "SELECT COUNT(*) c FROM job_steps WHERE status IN ('ready','running')",
        )
        assert active[0]["c"] == 0


class TestImmutableLedgers:
    async def test_ai_usage_imported_once_without_recharge(self, source, target):
        """§5.2.20:不可变账本导入不重复、不改序、不重新计费。"""
        job_id = "job_ai"
        insert_job(source, job_id, content_type="document", document_kind="article")
        for index in range(3):
            db_exec(source.db, (
                "INSERT INTO ai_usage (exec_id, job_id, step, provider, model,"
                " input_tokens, output_tokens, cost_usd, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)"
            ), (
                f"exec_{index}", job_id, "05_notes", "claude", "claude-x",
                100 + index, 50 + index, 0.25, T_CREATED,
            ))
        await do_backup(source)
        await do_import(source, target)
        rows = query(
            target.db,
            "SELECT exec_id, input_tokens, cost_usd FROM ai_usage ORDER BY exec_id",
        )
        assert [row["exec_id"] for row in rows] == ["exec_0", "exec_1", "exec_2"]
        assert [row["input_tokens"] for row in rows] == [100, 101, 102]
        assert sum(row["cost_usd"] for row in rows) == pytest.approx(0.75)

    async def test_study_ledger_rows_roundtrip(self, source, target):
        insert_job(source, "job_s", content_type="document", document_kind="article")
        db_exec(source.db, (
            "INSERT INTO study_cards (card_id, domain, job_id, concept_term, card_type,"
            " front, back, status, source, revision, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
        ), (
            "card_1", "ml", "job_s", "attention", "qa", "Q", "A",
            "active", "manual", 1, T_CREATED, T_CREATED,
        ))
        await do_backup(source)
        await do_import(source, target)
        [card] = query(target.db, "SELECT * FROM study_cards")
        assert card["card_id"] == "card_1" and card["front"] == "Q"

    async def test_glossary_and_definition_history_roundtrip(self, source, target):
        db_exec(source.db, (
            "INSERT INTO glossary (domain, term, definition, status, created_at, updated_at)"
            " VALUES ('ml','cnn','卷积网络','active',?,?)"
        ), (T_CREATED, T_CREATED))
        await do_backup(source)
        await do_import(source, target)
        [row] = query(target.db, "SELECT domain, term, definition FROM glossary")
        assert row == {"domain": "ml", "term": "cnn", "definition": "卷积网络"}

    async def test_collection_terms_json_roundtrips_byte_for_byte(self, source, target):
        """集合术语表是人工维护的领域配置,不是 glossary 的派生物,必须逐字节回来。

        漏掉它不会报错:恢复后 _export_term_map 读到空值就安静退回"只用 glossary",
        那本书的译名少一截且没有任何信号。
        """
        insert_collection(source, "col_book", name="深度学习")
        payload = json.dumps(
            {"attention": "注意力", "embedding": "嵌入"}, ensure_ascii=False, indent=1,
        ).encode("utf-8")
        await source.storage.write_file("collections/col_book", "terms.json", payload)

        await do_backup(source)
        await do_import(source, target)

        restored = await target.storage.read_file("collections/col_book", "terms.json")
        assert restored == payload

    async def test_collection_terms_json_is_claimed_by_the_snapshot(self, source, target):
        """回归钉:terms.json 必须以 user_config 进 snapshot,而不是被当成未知残留跳过。"""
        insert_collection(source, "col_book", name="深度学习")
        await source.storage.write_file(
            "collections/col_book", "terms.json", b'{"attention": "\\u6ce8\\u610f\\u529b"}',
        )
        result = await do_backup(source)

        body = source.repo.get_snapshot(result.snapshot_digest)
        configs = [
            source.repo.get_record("user_config", digest)
            for digest in body["records"]["business_ledgers"]
            if source.repo.has_record("user_config", digest)
        ]
        assert [item["path"] for item in configs] == ["collections/col_book/terms.json"]
        assert configs[0]["kind"] == "domain_config"
        # blob 必须同时进可达性闭包,否则 GC 会把字节扫掉只留记录。
        assert configs[0]["blob"] in body["blob_refs"]


class TestSearchRebuild:
    async def test_fts_rebuilt_from_restored_notes(self, source, target):
        """§5.2.19:笔记索引由已恢复文件重建,不从快照复制投影。"""
        await seed_video_job(source)
        await do_backup(source)
        result = await do_import(source, target)
        search = result.projection["search"]
        # 索引归属交还 scheduler:导入侧绝不能先填 notes_fts5,否则
        # list_unindexed_done_jobs 谓词永远为假,canonical_evidence 永久为空
        assert search["notes_indexed"] == 0
        assert search["owned_by"].endswith("reconcile_completion_effects")
        assert search["deferred"] == []
        assert "concept_occurrences" in search["note"]
        assert query(target.db, "SELECT COUNT(*) c FROM notes_fts5")[0]["c"] == 0
        assert query(target.db, "SELECT COUNT(*) c FROM note_chunks")[0]["c"] == 0


def _unit_materializer(tmp_path, *, source_roots=None):
    import shared.content_import as module

    repository = ContentRepository.create(tmp_path / "unit-repo")
    connection = sqlite3.connect(":memory:")
    storage = LocalStorage(tmp_path / "unit-jobs")
    journal = SimpleNamespace(record_processed=lambda *args, **kwargs: None)
    materializer = module._Materializer(
        repository=repository,
        connection=connection,
        storage=storage,
        journal=journal,
        import_id="imp_unit",
        processed=set(),
        config_root=tmp_path / "prompts",
        source_roots=source_roots or {},
    )
    return materializer, repository, storage, connection


class TestPortableV2WriteSurfaces:
    @pytest.mark.parametrize(("kind", "record_path", "target_rel"), [
        ("prompts", "prompts/semantic.md", "semantic.md"),
        ("profiles", "prompts/profiles/general.yaml", "profiles/general.yaml"),
        ("styles", "prompts/styles/academic.yaml", "styles/academic.yaml"),
        ("templates", "prompts/templates/review.md", "templates/review.md"),
    ])
    async def test_global_config_paths_are_restored_under_prompts_root(
        self, tmp_path, kind, record_path, target_rel,
    ):
        materializer, repository, _storage, connection = _unit_materializer(tmp_path)
        try:
            payload = f"{kind}-config".encode()
            blob = repository.put_blob_bytes(payload)
            body = {
                "kind": kind,
                "path": record_path,
                "blob": blob.digest,
                "size_bytes": len(payload),
            }

            await materializer._put_user_config(body)
            await materializer._put_user_config(body)

            assert (tmp_path / "prompts" / target_rel).read_bytes() == payload
            assert materializer.stats["objects_written"] == 1
            assert materializer.stats["objects_reused"] == 1
        finally:
            connection.close()

    async def test_global_config_rejects_symlink_parent_and_conflicting_file(
        self, tmp_path,
    ):
        materializer, repository, _storage, connection = _unit_materializer(tmp_path)
        outside = tmp_path / "outside"
        outside.mkdir()
        (tmp_path / "prompts").mkdir()
        (tmp_path / "prompts" / "profiles").symlink_to(
            outside, target_is_directory=True,
        )
        payload = b"profile"
        blob = repository.put_blob_bytes(payload)
        body = {
            "kind": "profiles",
            "path": "prompts/profiles/general.yaml",
            "blob": blob.digest,
            "size_bytes": len(payload),
        }
        try:
            with pytest.raises(ContentImportError, match="unsafe|unavailable"):
                await materializer._put_user_config(body)
            assert not (outside / "general.yaml").exists()

            (tmp_path / "prompts" / "profiles").unlink()
            (tmp_path / "prompts" / "profiles").mkdir()
            (tmp_path / "prompts" / "profiles" / "general.yaml").write_bytes(b"other")
            with pytest.raises(ContentImportError, match="conflicts"):
                await materializer._put_user_config(body)
        finally:
            connection.close()

    async def test_global_config_parent_swap_cannot_redirect_write(
        self, tmp_path, monkeypatch,
    ):
        materializer, repository, _storage, connection = _unit_materializer(tmp_path)
        profiles = tmp_path / "prompts" / "profiles"
        profiles.mkdir(parents=True)
        outside = tmp_path / "swap-outside"
        outside.mkdir()
        payload = b"profile"
        blob = repository.put_blob_bytes(payload)
        real_link = __import__("os").link
        swapped = False

        def swap_then_link(*args, **kwargs):
            nonlocal swapped
            if not swapped:
                swapped = True
                profiles.rename(profiles.with_name("profiles-pinned"))
                profiles.symlink_to(outside, target_is_directory=True)
            return real_link(*args, **kwargs)

        monkeypatch.setattr("shared.content_import.os.link", swap_then_link)
        try:
            with pytest.raises(ContentImportError, match="moved|changed|repository"):
                await materializer._put_user_config({
                    "kind": "profiles",
                    "path": "prompts/profiles/general.yaml",
                    "blob": blob.digest,
                    "size_bytes": len(payload),
                })
            assert not (outside / "general.yaml").exists()
            assert not (
                tmp_path / "prompts" / "profiles-pinned" / "general.yaml"
            ).exists()
        finally:
            connection.close()

    async def test_global_config_parent_moved_into_repository_is_rolled_back(
        self, tmp_path, monkeypatch,
    ):
        materializer, repository, _storage, connection = _unit_materializer(tmp_path)
        profiles = tmp_path / "prompts" / "profiles"
        profiles.mkdir(parents=True)
        stolen = repository.root / "stolen-profiles"
        payload = b"profile"
        blob = repository.put_blob_bytes(payload)
        real_link = __import__("os").link
        moved = False

        def move_into_repository_then_link(*args, **kwargs):
            nonlocal moved
            if not moved:
                moved = True
                profiles.rename(stolen)
                profiles.mkdir()
            return real_link(*args, **kwargs)

        monkeypatch.setattr(
            "shared.content_import.os.link", move_into_repository_then_link,
        )
        try:
            with pytest.raises(ContentImportError, match="moved|repository"):
                await materializer._put_user_config({
                    "kind": "profiles",
                    "path": "prompts/profiles/general.yaml",
                    "blob": blob.digest,
                    "size_bytes": len(payload),
                })
            assert not (stolen / "general.yaml").exists()
            assert not (profiles / "general.yaml").exists()
        finally:
            connection.close()

    async def test_job_ai_config_accepts_provider_map_without_overwriting(
        self, tmp_path,
    ):
        materializer, repository, storage, connection = _unit_materializer(tmp_path)
        job_id = "job_ai_restore"
        await storage.write_file(job_id, "job.json", b'{"id":"job_ai_restore"}')
        payload = json.dumps({
            "ai_overrides": {"11_smart": "claude-cli"},
            "prompt_overrides": {
                "11_smart": {"content": "prompt", "version": 1},
            },
        }).encode()
        blob = repository.put_blob_bytes(payload)
        body = {
            "kind": "job_ai_config",
            "path": f"jobs/{job_id}/ai-config.json",
            "blob": blob.digest,
            "size_bytes": len(payload),
        }
        try:
            await materializer._put_user_config(body)
            await materializer._put_user_config(body)
            restored = json.loads(await storage.read_file(job_id, "job.json"))
            assert restored["id"] == job_id
            assert restored["ai_overrides"] == {"11_smart": "claude-cli"}
        finally:
            connection.close()

    async def test_source_blob_requires_mapping_and_rejects_symlink_parent(
        self, tmp_path,
    ):
        media = b"vendored-media"
        source_root = tmp_path / "source-root"
        materializer, repository, _storage, connection = _unit_materializer(
            tmp_path, source_roots={"archive": source_root},
        )
        blob = repository.put_blob_bytes(media)
        try:
            await materializer._publish_local_blob(
                source_root,
                "videos/course.mp4",
                blob.digest,
                len(media),
                label="source root archive",
            )
            assert (source_root / "videos" / "course.mp4").read_bytes() == media

            shutil.rmtree(source_root)
            outside = tmp_path / "source-outside"
            outside.mkdir()
            source_root.mkdir()
            (source_root / "videos").symlink_to(outside, target_is_directory=True)
            with pytest.raises(ContentImportError, match="unsafe|unavailable"):
                await materializer._publish_local_blob(
                    source_root,
                    "videos/course.mp4",
                    blob.digest,
                    len(media),
                    label="source root archive",
                )
            assert not (outside / "course.mp4").exists()
        finally:
            connection.close()

    async def test_source_root_moved_into_repository_is_rolled_back(
        self, tmp_path, monkeypatch,
    ):
        media = b"vendored-media"
        source_root = tmp_path / "source-root"
        source_root.mkdir()
        materializer, repository, _storage, connection = _unit_materializer(
            tmp_path, source_roots={"archive": source_root},
        )
        blob = repository.put_blob_bytes(media)
        stolen = repository.root / "stolen-source-root"
        real_link = __import__("os").link
        moved = False

        def move_into_repository_then_link(*args, **kwargs):
            nonlocal moved
            if not moved:
                moved = True
                source_root.rename(stolen)
                source_root.mkdir()
            return real_link(*args, **kwargs)

        monkeypatch.setattr(
            "shared.content_import.os.link", move_into_repository_then_link,
        )
        try:
            with pytest.raises(ContentImportError, match="changed|repository"):
                await materializer._publish_local_blob(
                    source_root,
                    "videos/course.mp4",
                    blob.digest,
                    len(media),
                    label="source root archive",
                )
            assert not (stolen / "videos" / "course.mp4").exists()
            assert not (source_root / "videos" / "course.mp4").exists()
        finally:
            connection.close()


class TestCli:
    def test_plan_mode_outputs_json_and_writes_nothing(
        self, source, target, tmp_path, monkeypatch,
    ):
        monkeypatch.delenv("MINIO_URL", raising=False)
        asyncio.run(_seed_and_backup(source))
        result_file = tmp_path / "plan.json"
        code = main([
            "--repo", str(source.repo.root),
            "--db", str(target.db),
            "--jobs-dir", str(target.jobs_dir),
            "--journal", str(target.journal),
            "--config-dir", str(_CONFIGS_DIR),
            "--plan",
            "--result-file", str(result_file),
        ])
        assert code == 0
        payload = json.loads(result_file.read_text())
        assert payload["ok"] is True and payload["mode"] == "plan"
        assert payload["counts"]["blobs"] == 2
        assert not target.db.exists()

    def test_cli_refuses_object_store_import_without_an_isolated_bucket(
        self, source, target, tmp_path, monkeypatch,
    ):
        """P0-1:设了 MINIO_URL 却没给隔离桶时,默认导入必须拒绝而不是写生产桶。

        旧实现里 create_storage 在对象模式下丢掉 jobs_dir,于是"默认写隔离
        staging"这条安全属性在生产后端上完全不成立,而全部既有用例都
        monkeypatch 掉了 MINIO_URL,永远看不见。
        """
        asyncio.run(_seed_and_backup(source))
        monkeypatch.setenv("MINIO_URL", "minio:9000")
        monkeypatch.setenv("MINIO_BUCKET", "flori")
        result_file = tmp_path / "refused.json"
        code = main([
            "--repo", str(source.repo.root),
            "--db", str(target.db),
            "--jobs-dir", str(target.jobs_dir),
            "--journal", str(target.journal),
            "--config-dir", str(_CONFIGS_DIR),
            "--result-file", str(result_file),
        ])
        assert code == 2
        payload = json.loads(result_file.read_text())
        assert payload["ok"] is False
        assert "--object-bucket" in payload["error"]
        assert not target.db.exists()

    def test_cli_refuses_live_database_without_into_live(
        self, source, target, tmp_path, monkeypatch,
    ):
        """P0-2:把关看目标身份,不看 --into-live 有没有被用来挑默认 jobs-dir。"""
        asyncio.run(_seed_and_backup(source))
        monkeypatch.delenv("MINIO_URL", raising=False)
        monkeypatch.setenv("FLORI_LIVE_DB_PATH", str(target.db))
        result_file = tmp_path / "refused.json"
        code = main([
            "--repo", str(source.repo.root),
            "--db", str(target.db),
            "--jobs-dir", str(target.jobs_dir),
            "--journal", str(target.journal),
            "--config-dir", str(_CONFIGS_DIR),
            "--result-file", str(result_file),
        ])
        assert code == 2
        payload = json.loads(result_file.read_text())
        assert "没有 --into-live" in payload["error"]
        assert not target.db.exists()

    def test_cli_plan_is_not_blocked_by_the_live_write_gate(
        self, source, target, tmp_path, monkeypatch,
    ):
        """恢复流程第 1 步就是对着线上库出计划,只读路径不能被写入门拦死。"""
        asyncio.run(_seed_and_backup(source))
        monkeypatch.delenv("MINIO_URL", raising=False)
        monkeypatch.setenv("FLORI_LIVE_DB_PATH", str(target.db))
        assert main([
            "--repo", str(source.repo.root),
            "--db", str(target.db),
            "--jobs-dir", str(target.jobs_dir),
            "--journal", str(target.journal),
            "--config-dir", str(_CONFIGS_DIR),
            "--plan",
        ]) == 0

    def test_cli_reports_conflicts(self, source, target, tmp_path, monkeypatch):
        monkeypatch.delenv("MINIO_URL", raising=False)
        asyncio.run(_seed_and_backup(source, partial=True))
        result_file = tmp_path / "conflict.json"
        code = main([
            "--repo", str(source.repo.root),
            "--db", str(target.db),
            "--jobs-dir", str(target.jobs_dir),
            "--snapshot", "only-alpha",
            "--journal", str(target.journal),
            "--config-dir", str(_CONFIGS_DIR),
            "--plan",
            "--result-file", str(result_file),
        ])
        assert code == 1
        payload = json.loads(result_file.read_text())
        assert payload["ok"] is False
        assert any("partial" in item for item in payload["conflicts"])


    def test_cli_import_by_digest_succeeds_on_readonly_repo(
        self, source, target, tmp_path, monkeypatch,
    ):
        """按 digest 导入必须走得通,即使仓库只读(P4 演练 D2)。

        run_import 会为裸 digest 挂保活 ref 防并发 GC;仓库只读时 set_ref 抛的是
        OSError 而不是 RepositoryError,早期版本接不住 -> CLI 直接崩。
        receipt 给的就是 digest,用 digest 锁定恢复点是恢复期的正常操作。
        """
        monkeypatch.delenv("MINIO_URL", raising=False)
        asyncio.run(_seed_and_backup(source))
        digest = source.repo.get_ref("latest")
        assert digest.startswith("sha256:")
        # 容器内测试以 root 跑,chmod 挡不住 root;直接让 set_ref 抛 :ro 挂载真正会抛的
        # errno,精确复现 scripts/content-import.sh 的 -v repo:/content-repo:ro。
        from shared.content_repository import ContentRepository

        def _readonly_set_ref(self, name, snapshot_digest):
            raise OSError(
                errno.EROFS, "Read-only file system",
                str(self.root / "tmp" / f"ref-{name}"),
            )

        monkeypatch.setattr(ContentRepository, "set_ref", _readonly_set_ref)
        result_file = tmp_path / "digest.json"
        code = main([
            "--repo", str(source.repo.root),
            "--db", str(target.db),
            "--jobs-dir", str(target.jobs_dir),
            "--journal", str(target.journal),
            "--config-dir", str(_CONFIGS_DIR),
            "--snapshot", digest,
            "--target-generation", "cli-digest",
            "--result-file", str(result_file),
        ])
        assert code == 0
        payload = json.loads(result_file.read_text())
        assert payload["ok"] is True
        assert payload["plan"]["snapshot_digest"] == digest
        # 降级必须可见,不能只躺在日志里
        assert payload["snapshot_guard"]["held"] is False
        assert payload["snapshot_guard"]["error"]
        assert query(target.db, "SELECT id FROM jobs")

    def test_cli_import_by_digest_releases_guard_ref(
        self, source, target, tmp_path, monkeypatch,
    ):
        """可写仓库下保活 ref 必须挂上并在结束时摘掉,否则 GC 永远回收不了被导入过的快照。"""
        monkeypatch.delenv("MINIO_URL", raising=False)
        asyncio.run(_seed_and_backup(source))
        digest = source.repo.get_ref("latest")
        before = set(source.repo.list_refs())
        code = main([
            "--repo", str(source.repo.root),
            "--db", str(target.db),
            "--jobs-dir", str(target.jobs_dir),
            "--journal", str(target.journal),
            "--config-dir", str(_CONFIGS_DIR),
            "--snapshot", digest,
            "--target-generation", "cli-digest-guard",
            "--result-file", str(tmp_path / "guard.json"),
        ])
        assert code == 0
        payload = json.loads((tmp_path / "guard.json").read_text())
        assert payload["snapshot_guard"]["held"] is True
        assert set(source.repo.list_refs()) == before, "保活 ref 没摘干净"

    def test_cli_repeated_import_is_benign_noop(
        self, source, target, tmp_path, monkeypatch,
    ):
        """重复导入是 no-op 而不是失败:退出码 0 + already_imported(P4 演练 D3)。"""
        monkeypatch.delenv("MINIO_URL", raising=False)
        asyncio.run(_seed_and_backup(source))
        argv = [
            "--repo", str(source.repo.root),
            "--db", str(target.db),
            "--jobs-dir", str(target.jobs_dir),
            "--journal", str(target.journal),
            "--config-dir", str(_CONFIGS_DIR),
            "--target-generation", "cli-repeat",
            "--result-file", str(tmp_path / "again.json"),
        ]
        assert main(argv) == 0
        rows_first = query(target.db, "SELECT id FROM jobs")
        assert main(argv) == 0, "良性重放不得报非零退出码"
        payload = json.loads((tmp_path / "again.json").read_text())
        assert payload["ok"] is True
        assert payload["already_imported"] is True
        assert query(target.db, "SELECT id FROM jobs") == rows_first

    def test_plan_reports_real_bytes_without_rehashing(
        self, source, target, tmp_path, monkeypatch,
    ):
        """--plan 必须报真实字节数,否则紧邻的磁盘余量门永远不触发(P4 演练 D1)。"""
        monkeypatch.delenv("MINIO_URL", raising=False)
        asyncio.run(_seed_and_backup(source))
        result_file = tmp_path / "plan-bytes.json"
        assert main([
            "--repo", str(source.repo.root),
            "--db", str(target.db),
            "--jobs-dir", str(target.jobs_dir),
            "--journal", str(target.journal),
            "--config-dir", str(_CONFIGS_DIR),
            "--plan",
            "--result-file", str(result_file),
        ]) == 0
        payload = json.loads(result_file.read_text())
        expected = sum(
            source.repo.blob_path(digest).stat().st_size
            for digest in _snapshot_blob_refs(source.repo)
        )
        assert expected > 0
        assert payload["bytes_to_write"] == expected


def _snapshot_blob_refs(repo) -> list[str]:
    return repo.get_snapshot(repo.get_ref("latest"))["blob_refs"]


async def _seed_and_backup(source, *, partial: bool = False) -> None:
    await seed_video_job(source)
    await do_backup(source, "run_full")
    if partial:
        await do_backup(source, "run_part", job_ids=["job_alpha"], ref="only-alpha")
