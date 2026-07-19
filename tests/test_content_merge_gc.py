"""P3b:merge 七条规则、冲突分类零修改、GC mark/sweep 与 scrub 拒收损坏。"""

import json
import shutil
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from shared.content_gc import main as gc_main
from shared.content_import import (
    ContentImportError,
    MODE_MERGE,
    classify_merge,
    run_import,
)
from shared.content_repository import ContentRepository, RepositoryCorruptionError
from shared.storage import LocalStorage
from tests.test_content_backup import (
    MEDIA_ONE,
    MEDIA_THREE,
    MEDIA_TWO,
    commit_step,
    db_exec,
    do_backup,
    ensure_job_json,
    insert_collection,
    insert_job,
    insert_part,
    insert_step,
    seed_video_job,
    sha,
    T_CREATED,
    T_FINISHED,
    T_STARTED,
)
from tests.test_content_import import (
    _CONFIGS_DIR,
    current_definition_digest,
    do_import,
    query,
    seed_multi_job,
    source,  # noqa: F401 - fixture
    target,  # noqa: F401 - fixture
)


async def do_merge(source, target, *, generation="merge-1", **kwargs):
    kwargs.setdefault("config_dir", _CONFIGS_DIR)
    kwargs.setdefault("mode", MODE_MERGE)
    return await run_import(
        repository=source.repo,
        snapshot=kwargs.pop("snapshot", "latest"),
        target_db_path=target.db,
        storage=target.storage,
        journal_path=target.journal,
        target_generation=generation,
        **kwargs,
    )


async def classify(source, target, *, apply_user_state=False):
    """在目标库上跑一次只读分类,返回 MergeClassification。"""
    from shared.content_import import _resolve_records_for_classification

    _body, records = _resolve_records_for_classification(
        source.repo, source.repo.get_ref("latest"),
    )
    connection = sqlite3.connect(target.db)
    connection.row_factory = sqlite3.Row
    try:
        return await classify_merge(
            connection=connection, storage=target.storage, records=records,
            apply_user_state=apply_user_state,
        )
    finally:
        connection.close()


async def seed_and_import_base(source, target):
    """先用 empty 模式建好目标库,后续 merge 都在它之上做。"""
    await seed_multi_job(source)
    await do_backup(source, "run_base")
    await do_import(source, target)


class TestMergeRules:
    async def test_same_snapshot_merge_is_all_noop(self, source, target):
        """§5.2.13 的 merge 版:同一 snapshot 再 merge 一次必须全 no-op。"""
        await seed_and_import_base(source, target)
        before = {
            table: query(target.db, f"SELECT COUNT(*) c FROM {table}")[0]["c"]
            for table in ("jobs", "job_parts", "collections", "ai_usage")
        }
        result = await do_merge(source, target)
        counts = result.merge_report["counts"]
        assert counts["insert"] == 0
        assert counts["conflict"] == 0
        assert counts["noop"] > 0
        after = {
            table: query(target.db, f"SELECT COUNT(*) c FROM {table}")[0]["c"]
            for table in ("jobs", "job_parts", "collections", "ai_usage")
        }
        assert after == before

    async def test_absent_natural_key_is_inserted(self, source, target):
        """规则 1:目标没有的自然键 -> 插入。"""
        await seed_and_import_base(source, target)
        # 源侧新增一个 Job,重新备份后 merge
        insert_job(source, "job_new", content_type="document", document_kind="article")
        await do_backup(source, "run_new")
        result = await do_merge(source, target, generation="merge-new")
        assert result.merge_report["counts"]["insert"] >= 1
        assert query(target.db, "SELECT id FROM jobs WHERE id='job_new'")

    async def test_monotonic_step_fill_is_allowed(self, source, target):
        """规则 3:目标缺的 (job,scope,step) manifest 允许单调补齐。"""
        await seed_and_import_base(source, target)
        # 把目标库里 pt_alpha2 的 manifest 删掉,模拟"目标只有部分 step"
        manifest = (
            target.jobs_dir / "job_alpha" / "parts" / "pt_alpha2"
            / ".flori" / "steps" / "01_download" / "manifest.json"
        )
        manifest.unlink()
        classification = await classify(source, target)
        step_actions = [
            classification.actions[digest]
            for kind, digest, body in _records_of(source, "step_result")
            if body["job_id"] == "job_alpha" and body["scope_key"] == "part:pt_alpha2"
        ]
        assert step_actions == ["insert"]
        result = await do_merge(source, target, generation="merge-fill")
        assert manifest.is_file(), "缺失的 step manifest 必须被补齐"
        assert result.merge_report["counts"]["conflict"] == 0

    async def test_different_manifest_for_same_step_is_conflict(self, source, target):
        """规则 4:同一 active step 已有不同 manifest -> 冲突且整单元零修改。"""
        await seed_and_import_base(source, target)
        # 目标库那一步换成另一份合法 manifest(不同 exec_id -> 不同 digest)
        manifest_path = (
            target.jobs_dir / "job_alpha" / "parts" / "pt_alpha1"
            / ".flori" / "steps" / "01_download" / "manifest.json"
        )
        payload = json.loads(manifest_path.read_text())
        payload["execution"]["exec_id"] = "exec_target_side"
        manifest_path.write_text(json.dumps(payload))

        rows_before = query(target.db, "SELECT id FROM job_parts ORDER BY id")
        classification = await classify(source, target)
        assert any(
            item.conflict == "step_manifest" for item in classification.conflicts
        )
        assert "job:job_alpha" in classification.conflicted_units
        # 该 Job 单元内没有任何 record 会被写入
        for kind, digest, body in _records_of(source, None):
            if body.get("job_id") == "job_alpha" or body.get("id") == "job_alpha":
                assert classification.actions[digest] != "insert"

        result = await do_merge(source, target, generation="merge-conflict")
        assert query(target.db, "SELECT id FROM job_parts ORDER BY id") == rows_before
        assert manifest_path.read_text() == json.dumps(payload), "冲突单元不得被覆盖"
        conflicts = result.merge_report["conflicts"]
        assert conflicts and conflicts[0]["unit"] == "job:job_alpha"
        assert conflicts[0]["target_digest"] != conflicts[0]["snapshot_digest"]

    async def test_different_part_list_is_identity_conflict(self, source, target):
        """规则 5:同 Job id 但有序 Part 清单不同 -> 身份冲突,整 Job 拒绝。"""
        await seed_and_import_base(source, target)
        db_exec(target.db, "DELETE FROM job_parts WHERE id='pt_alpha2'")
        classification = await classify(source, target)
        assert any(
            item.conflict == "job_identity" for item in classification.conflicts
        )
        assert "job:job_alpha" in classification.conflicted_units

    async def test_changed_job_core_is_identity_conflict(self, source, target):
        await seed_and_import_base(source, target)
        db_exec(
            target.db, "UPDATE jobs SET title='被本地改过' WHERE id='job_alpha'",
        )
        classification = await classify(source, target)
        identity = [
            item for item in classification.conflicts
            if item.conflict == "job_identity"
        ]
        assert identity and identity[0].natural_key == "job_alpha"
        assert "core differs" in identity[0].detail

    async def test_immutable_ledger_conflict(self, source, target):
        """规则 7:不可变账本同 key 不同内容 -> 拒绝。"""
        await seed_multi_job(source)
        db_exec(source.db, (
            "INSERT INTO ai_usage (exec_id, job_id, step, provider, model,"
            " input_tokens, created_at) VALUES ('exec_ai_1','job_alpha','05_notes',"
            "'claude','claude-x',100,?)"
        ), (T_CREATED,))
        await do_backup(source, "run_base")
        await do_import(source, target)
        db_exec(
            target.db,
            "UPDATE ai_usage SET input_tokens=999 WHERE exec_id='exec_ai_1'",
        )
        classification = await classify(source, target)
        ledger = [
            item for item in classification.conflicts
            if item.conflict == "immutable_ledger"
        ]
        assert ledger, "被改过的账本行必须报冲突"
        assert ledger[0].kind == "ai_usage"

    async def test_user_state_kept_by_default(self, source, target):
        """规则 6:用户状态不同默认保留目标并报告。"""
        insert_collection(source, "col_src")
        await seed_multi_job(source)
        db_exec(source.db, "UPDATE jobs SET collection_id='col_src' WHERE id='job_alpha'")
        await do_backup(source, "run_base")
        await do_import(source, target)
        # 目标侧把归类改掉
        db_exec(target.db, (
            "INSERT INTO collections (id, name, domain, created_at, updated_at)"
            " VALUES ('col_local','本地集','general',?,?)"
        ), (T_CREATED, T_CREATED))
        db_exec(target.db, "UPDATE jobs SET collection_id='col_local' WHERE id='job_alpha'")

        result = await do_merge(source, target, generation="merge-user")
        kept = result.merge_report["user_state_kept"]
        assert kept and kept[0]["job_id"] == "job_alpha"
        assert kept[0]["target_collection_id"] == "col_local"
        [row] = query(target.db, "SELECT collection_id FROM jobs WHERE id='job_alpha'")
        assert row["collection_id"] == "col_local", "默认不得覆盖用户状态"

    async def test_apply_user_state_refused_when_job_identity_conflicts(
        self, source, target,
    ):
        """前置摘要不匹配(Job 身份已冲突)时,--apply-user-state 也不得改。"""
        insert_collection(source, "col_src")
        await seed_multi_job(source)
        db_exec(source.db, "UPDATE jobs SET collection_id='col_src' WHERE id='job_alpha'")
        await do_backup(source, "run_base")
        await do_import(source, target)
        db_exec(target.db, "UPDATE jobs SET title='本地改过' WHERE id='job_alpha'")
        db_exec(target.db, "UPDATE jobs SET collection_id=NULL WHERE id='job_alpha'")

        result = await do_merge(
            source, target, generation="merge-apply2", apply_user_state=True,
        )
        assert "job:job_alpha" in result.merge_report["conflicted_units"]
        [row] = query(target.db, "SELECT collection_id, title FROM jobs WHERE id='job_alpha'")
        assert row["title"] == "本地改过", "身份冲突单元必须零修改"

    async def test_merge_into_empty_database_inserts_everything(self, source, target):
        await seed_multi_job(source)
        await do_backup(source)
        # 先用 empty 建 schema,再清空业务表,模拟"有 schema 无数据"
        await do_import(source, target)
        for table in ("job_steps", "job_parts", "jobs", "collections", "ai_usage"):
            db_exec(target.db, f"DELETE FROM {table}")
        result = await do_merge(source, target, generation="merge-empty")
        assert result.merge_report["counts"]["conflict"] == 0
        assert query(target.db, "SELECT COUNT(*) c FROM jobs")[0]["c"] == 3

    async def test_plan_reports_merge_counts_and_conflicts(self, source, target):
        """merge 感知的 plan:三个计数不再是 None,冲突清单可直接给人看。"""
        await seed_and_import_base(source, target)
        db_exec(target.db, "UPDATE jobs SET title='本地改过' WHERE id='job_alpha'")
        from shared.config import load_config
        from shared.content_import import build_plan

        classification = await classify(source, target)
        plan, _b, _r = build_plan(
            repository=source.repo, snapshot="latest", target_db_path=target.db,
            config=load_config(_CONFIGS_DIR), mode=MODE_MERGE,
            classification=classification, verify_blobs=False,
        )
        assert plan.counts["noop"] is not None
        assert plan.counts["conflict"] >= 1
        assert plan.counts["pending"] >= 0
        assert plan.merge_conflicts
        assert any("merge conflicts" in item for item in plan.conflicts)


class TestMergeViaCli:
    """出货入口级覆盖:merge 只有走 main() 才算真的可用(P0-1)。"""

    def test_cli_merge_applies_and_reports(self, source, target, tmp_path, monkeypatch):
        import asyncio

        from shared.content_import import main as import_main

        monkeypatch.delenv("MINIO_URL", raising=False)
        asyncio.run(_seed_base_then_add_job(source, target))
        result_file = tmp_path / "merge.json"
        code = import_main([
            "--repo", str(source.repo.root),
            "--db", str(target.db),
            "--jobs-dir", str(target.jobs_dir),
            "--journal", str(target.journal),
            "--config-dir", str(_CONFIGS_DIR),
            "--target", "merge",
            "--target-generation", "cli-merge",
            "--result-file", str(result_file),
        ])
        assert code == 0
        payload = json.loads(result_file.read_text())
        assert payload["ok"] is True
        # 真正跑了 merge,而不是悄悄落回 empty
        assert payload["merge"]["mode"] == "merge"
        assert payload["merge"]["counts"]["noop"] > 0
        assert query(target.db, "SELECT id FROM jobs WHERE id='job_new'")

    def test_cli_plan_target_merge_classifies(self, source, target, tmp_path, monkeypatch):
        import asyncio

        from shared.content_import import main as import_main

        monkeypatch.delenv("MINIO_URL", raising=False)
        asyncio.run(_seed_base_then_add_job(source, target))
        db_exec(target.db, "UPDATE jobs SET title='本地改过' WHERE id='job_alpha'")
        result_file = tmp_path / "plan-merge.json"
        code = import_main([
            "--repo", str(source.repo.root),
            "--db", str(target.db),
            "--jobs-dir", str(target.jobs_dir),
            "--journal", str(target.journal),
            "--config-dir", str(_CONFIGS_DIR),
            "--target", "merge", "--plan",
            "--result-file", str(result_file),
        ])
        payload = json.loads(result_file.read_text())
        assert payload["target_mode"] == "merge"
        # --plan 必须真的分类过:三个计数不再是 None,冲突清单可读
        assert payload["counts"]["noop"] is not None
        assert payload["merge_conflicts"]
        assert code == 1

    def test_cli_plan_digest_matches_real_import(self, source, target, tmp_path, monkeypatch):
        """P1-3:plan 与真实导入必须算出同一个 plan_digest,否则续跑判"计划变了"。"""
        import asyncio

        from shared.content_import import main as import_main

        monkeypatch.delenv("MINIO_URL", raising=False)
        asyncio.run(_seed_base_then_add_job(source, target))
        plan_file = tmp_path / "p.json"
        import_main([
            "--repo", str(source.repo.root), "--db", str(target.db),
            "--jobs-dir", str(target.jobs_dir), "--journal", str(target.journal),
            "--config-dir", str(_CONFIGS_DIR), "--target", "merge", "--plan",
            "--result-file", str(plan_file),
        ])
        planned = json.loads(plan_file.read_text())["plan_digest"]
        run_file = tmp_path / "r.json"
        import_main([
            "--repo", str(source.repo.root), "--db", str(target.db),
            "--jobs-dir", str(target.jobs_dir), "--journal", str(target.journal),
            "--config-dir", str(_CONFIGS_DIR), "--target", "merge",
            "--target-generation", "cli-same", "--result-file", str(run_file),
        ])
        assert json.loads(run_file.read_text())["plan"]["plan_digest"] == planned

    def test_repeated_merge_succeeds_and_keeps_audit(self, source, target):
        """P1-3:同一 snapshot 连续 merge 两次必须都成功,且账本不被毁。"""
        import asyncio

        asyncio.run(_seed_base_then_add_job(source, target))
        first = asyncio.run(do_merge(source, target, generation="repeat"))
        second = asyncio.run(do_merge(source, target, generation="repeat"))
        assert first.merge_report["mode"] == "merge"
        assert second.merge_report["counts"]["conflict"] == 0
        assert second.merge_report["counts"]["insert"] == 0

    def test_cli_merge_refuses_mismatched_jobs_root(
        self, source, target, tmp_path, monkeypatch,
    ):
        """P1-8:分类器读错产物根会让规则 4 永远判不出冲突,必须 fail-closed。"""
        import asyncio

        from shared.content_import import main as import_main

        monkeypatch.delenv("MINIO_URL", raising=False)
        asyncio.run(_seed_base_then_add_job(source, target))
        staging = tmp_path / "import-staging" / "jobs"
        staging.mkdir(parents=True)
        result_file = tmp_path / "root.json"
        code = import_main([
            "--repo", str(source.repo.root), "--db", str(target.db),
            "--jobs-dir", str(staging), "--journal", str(target.journal),
            "--config-dir", str(_CONFIGS_DIR), "--target", "merge",
            "--target-generation", "cli-root", "--result-file", str(result_file),
        ])
        assert code == 1
        assert "artifact root" in json.loads(result_file.read_text())["error"]


async def _seed_base_then_add_job(source, target):
    await seed_multi_job(source)
    await do_backup(source, "run_base")
    await do_import(source, target)
    insert_job(source, "job_new", content_type="document", document_kind="article")
    await do_backup(source, "run_add")


class TestMergeIsolation:
    async def test_unrelated_local_jobs_keep_runtime_state(self, source, target):
        """P0-2 回归:merge 不得重投影快照外的本地 Job,更不得清它们的运行态。"""
        await seed_multi_job(source)
        await do_backup(source, "run_base")
        await do_import(source, target)
        # 目标库里有一个与快照无关的本地 Job,带完整运行态
        db_exec(target.db, (
            "INSERT INTO jobs (id, content_type, document_kind, pipeline, status,"
            " progress_pct, error, created_at, updated_at)"
            " VALUES ('job_local','document','article','document','failed',42,"
            "'本地失败原因',?,?)"
        ), (T_CREATED, T_CREATED))
        db_exec(target.db, (
            "INSERT INTO job_steps (job_id, scope_key, step, status, pool)"
            " VALUES ('job_local','job','05_notes','running','ai')"
        ))
        insert_job(source, "job_new", content_type="document", document_kind="article")
        await do_backup(source, "run_add")

        await do_merge(source, target, generation="isolate")
        [local] = query(
            target.db,
            "SELECT status, progress_pct, error FROM jobs WHERE id='job_local'",
        )
        assert local["status"] == "failed"
        assert local["progress_pct"] == 42
        assert local["error"] == "本地失败原因"
        [step] = query(
            target.db,
            "SELECT status FROM job_steps WHERE job_id='job_local' AND step='05_notes'",
        )
        assert step["status"] == "running", "无关 Job 的运行中步骤不得被清成 waiting"

    async def test_conflicted_unit_rows_are_byte_identical(self, source, target):
        """P0-2/§2.9-4:冲突单元逐表行内容必须完全不变,而不只是"报了冲突"。"""
        await seed_multi_job(source)
        await do_backup(source, "run_base")
        await do_import(source, target)
        db_exec(target.db, "UPDATE jobs SET title='本地改过' WHERE id='job_alpha'")
        db_exec(
            target.db,
            "UPDATE job_steps SET status='running' WHERE job_id='job_alpha'",
        )
        before_job = query(target.db, "SELECT * FROM jobs WHERE id='job_alpha'")
        before_steps = query(
            target.db, "SELECT * FROM job_steps WHERE job_id='job_alpha' ORDER BY step",
        )
        before_parts = query(
            target.db, "SELECT * FROM job_parts WHERE job_id='job_alpha' ORDER BY id",
        )
        insert_job(source, "job_new", content_type="document", document_kind="article")
        await do_backup(source, "run_add")

        result = await do_merge(source, target, generation="conflict-frozen")
        assert "job:job_alpha" in result.merge_report["conflicted_units"]
        assert query(target.db, "SELECT * FROM jobs WHERE id='job_alpha'") == before_job
        assert query(
            target.db, "SELECT * FROM job_steps WHERE job_id='job_alpha' ORDER BY step",
        ) == before_steps
        assert query(
            target.db, "SELECT * FROM job_parts WHERE job_id='job_alpha' ORDER BY id",
        ) == before_parts
        # 干净单元照常补进来
        assert query(target.db, "SELECT id FROM jobs WHERE id='job_new'")


class TestUserStatePrecondition:
    async def test_apply_refused_when_target_changed_after_snapshot(
        self, source, target,
    ):
        """P2-10:目标在备份之后被改过,--apply-user-state 也必须拒绝。"""
        insert_collection(source, "col_src")
        await seed_multi_job(source)
        db_exec(source.db, "UPDATE jobs SET collection_id='col_src' WHERE id='job_alpha'")
        await do_backup(source, "run_base")
        await do_import(source, target)
        # 目标改到一个快照没见过的值 -> 前置摘要不匹配
        db_exec(target.db, (
            "INSERT INTO collections (id, name, domain, created_at, updated_at)"
            " VALUES ('col_local','本地集','general',?,?)"
        ), (T_CREATED, T_CREATED))
        db_exec(target.db, "UPDATE jobs SET collection_id='col_local' WHERE id='job_alpha'")

        result = await do_merge(
            source, target, generation="pre-1", apply_user_state=True,
        )
        conflicts = [
            item for item in result.merge_report["conflicts"]
            if item["conflict"] == "user_state"
        ]
        assert conflicts, "前置不匹配必须报 user_state 冲突"
        [row] = query(target.db, "SELECT collection_id FROM jobs WHERE id='job_alpha'")
        assert row["collection_id"] == "col_local"

    async def test_apply_allowed_when_target_matches_precondition(self, source, target):
        """目标现值就是快照的前置值时,--apply-user-state 允许更新。"""
        insert_collection(source, "col_src")
        await seed_multi_job(source)
        db_exec(source.db, "UPDATE jobs SET collection_id='col_src' WHERE id='job_alpha'")
        await do_backup(source, "run_base")
        await do_import(source, target)
        result = await do_merge(
            source, target, generation="pre-2", apply_user_state=True,
        )
        assert not [
            item for item in result.merge_report["conflicts"]
            if item["conflict"] == "user_state"
        ]


def _drop_anchors(repo):
    """只清当月锚点,保留 latest:让历史 snapshot 不可达,最新的仍受保护。"""
    for name in list(repo.list_refs()):
        if name.startswith("monthly-"):
            repo.delete_ref(name)


def _drop_all_refs(repo):
    for name in list(repo.list_refs()):
        repo.delete_ref(name)


def _records_of(source, kind: str | None):
    from shared.content_import import _resolve_records_for_classification

    _body, records = _resolve_records_for_classification(
        source.repo, source.repo.get_ref("latest"),
    )
    return [item for item in records if kind is None or item[0] == kind]


class TestGarbageCollection:
    async def _repo_with_two_snapshots(self, source):
        """两次备份产生两个 snapshot:第一个只被 receipt 引用,第二个被 latest 指。"""
        await seed_video_job(source)
        await do_backup(source, "run_one")
        first = source.repo.get_ref("latest")
        insert_job(source, "job_extra", content_type="document", document_kind="article")
        await do_backup(source, "run_two")
        second = source.repo.get_ref("latest")
        assert first != second
        return first, second

    async def test_reachable_media_never_collected(self, source):
        """§5.2.21:多 snapshot + named ref 下,任何可达视频都不得被清扫。"""
        first, second = await self._repo_with_two_snapshots(source)
        source.repo.set_ref("monthly-2026-07", first)
        plan = source.repo.gc_mark()
        assert set(plan.reachable_snapshots) == {first, second}
        assert plan.unreachable_blobs == ()
        assert sha(MEDIA_ONE) in plan.reachable_blobs
        assert sha(MEDIA_TWO) in plan.reachable_blobs

    async def test_dry_run_matches_actual_sweep(self, source):
        """§5.2.21:dry-run 清单必须与实删清单逐条一致。"""
        first, second = await self._repo_with_two_snapshots(source)
        # 丢掉对第一个 snapshot 的全部引用:refs(含当月锚点)+ receipts
        _drop_anchors(source.repo)
        plan = source.repo.gc_mark(receipt_root_limit=0)
        assert first in plan.unreachable_snapshots

        preview = source.repo.gc_sweep(plan, grace_seconds=0, dry_run=True)
        assert preview["dry_run"] is True
        assert preview["deleted"]["snapshots"] == [first]

        applied = source.repo.gc_sweep(plan, grace_seconds=0, dry_run=False)
        assert applied["deleted"] == preview["deleted"]
        assert not source.repo.has_snapshot(first)
        # 仍被 latest 指的 snapshot 与其 blob 分毫未动
        assert source.repo.has_snapshot(second)
        assert source.repo.has_blob(sha(MEDIA_ONE))

    async def test_grace_period_protects_fresh_objects(self, source):
        """grace period 内的对象一律不删:它们可能属于一次进行中的备份。"""
        first, _second = await self._repo_with_two_snapshots(source)
        _drop_anchors(source.repo)
        plan = source.repo.gc_mark(receipt_root_limit=0)
        outcome = source.repo.gc_sweep(plan, grace_seconds=86_400, dry_run=False)
        assert outcome["counts"]["snapshots"] == 0
        assert outcome["retained_within_grace"]
        assert source.repo.has_snapshot(first), "grace 内的对象必须留着"

    async def test_sweep_removes_orphan_blob(self, source):
        await seed_video_job(source)
        await do_backup(source)
        orphan = source.repo.put_blob_bytes(b"orphan-bytes-never-referenced")
        plan = source.repo.gc_mark()
        assert orphan.digest in plan.unreachable_blobs
        source.repo.gc_sweep(plan, grace_seconds=0, dry_run=False)
        assert not source.repo.has_blob(orphan.digest)
        assert source.repo.has_blob(sha(MEDIA_ONE))

    def test_gc_cli_mark_sweep_scrub(self, source, tmp_path, monkeypatch):
        import asyncio

        asyncio.run(_seed_for_cli(source))
        result_file = tmp_path / "gc.json"
        assert gc_main([
            "--repo", str(source.repo.root), "--mark",
            "--result-file", str(result_file),
        ]) == 0
        payload = json.loads(result_file.read_text())
        assert payload["ok"] and payload["mode"] == "mark"

        assert gc_main([
            "--repo", str(source.repo.root), "--sweep",
            "--result-file", str(result_file),
        ]) == 0
        assert json.loads(result_file.read_text())["sweep"]["dry_run"] is True

        assert gc_main([
            "--repo", str(source.repo.root), "--scrub",
            "--result-file", str(result_file),
        ]) == 0
        assert json.loads(result_file.read_text())["ok"] is True

    def test_gc_sweep_holds_write_lock(self, source, tmp_path):
        import asyncio

        asyncio.run(_seed_for_cli(source))
        with source.repo.write_lock("pretend-backup"):
            code = gc_main([
                "--repo", str(source.repo.root), "--sweep", "--apply",
                "--result-file", str(tmp_path / "blocked.json"),
            ])
        assert code == 1, "backup 持锁时 GC sweep 必须让路"
        assert "held by" in json.loads((tmp_path / "blocked.json").read_text())["error"]


async def _seed_for_cli(source):
    await seed_video_job(source)
    await do_backup(source)


class TestGcRegressions:
    async def test_grace_keeps_whole_reference_group(self, source):
        """P1-5:因 grace 留下的 snapshot,其 record/blob 必须一并保活。

        逐对象判 grace 会留下 snapshot 却删掉它引用的东西 —— GC 自己造损坏。
        """
        await seed_video_job(source)
        await do_backup(source, "run_one")
        first = source.repo.get_ref("latest")
        insert_job(source, "job_extra", content_type="document", document_kind="article")
        await do_backup(source, "run_two")

        _drop_anchors(source.repo)
        plan = source.repo.gc_mark(receipt_root_limit=0)
        assert first in plan.unreachable_snapshots
        # snapshot 仍在 grace 内(刚写),但它引用的 record/blob 假装已过期
        outcome = source.repo.gc_sweep(
            plan, grace_seconds=3600, dry_run=False,
            now=Path(source.repo.root).stat().st_mtime + 100_000,
        )
        # 要么整组删,要么整组留;绝不能留 snapshot 删 record
        if first not in outcome["deleted"]["snapshots"]:
            body = source.repo.get_snapshot(first, verify_closure=False)
            for blob in body["blob_refs"]:
                assert blob not in outcome["deleted"]["blobs"], (
                    "被 grace 留下的 snapshot,其 blob 不得被删"
                )
        assert source.repo.scrub().ok, "sweep 之后仓库必须仍然自洽"

    async def test_monthly_anchor_created_and_stable(self, source):
        """P1-6:备份后自动建当月锚点,当月后续备份不覆盖它。"""
        await seed_video_job(source)
        first = await do_backup(source, "run_one")
        anchors = [
            name for name in source.repo.list_refs() if name.startswith("monthly-")
        ]
        assert anchors, "备份必须建立当月保留锚点"
        anchor = anchors[0]
        assert source.repo.get_ref(anchor) == first.snapshot_digest

        insert_job(source, "job_extra", content_type="document", document_kind="article")
        second = await do_backup(source, "run_two")
        assert second.snapshot_digest != first.snapshot_digest
        assert source.repo.get_ref(anchor) == first.snapshot_digest, (
            "当月锚点钉住的是本月最早那个恢复点,不该被后续备份改写"
        )

    async def test_anchor_protects_old_snapshot_from_sweep(self, source):
        await seed_video_job(source)
        first = await do_backup(source, "run_one")
        insert_job(source, "job_extra", content_type="document", document_kind="article")
        await do_backup(source, "run_two")
        plan = source.repo.gc_mark(receipt_root_limit=0)
        assert first.snapshot_digest in plan.reachable_snapshots, (
            "有当月锚点时,旧恢复点不得进入可删清单"
        )

    async def test_receipt_window_counts_backups_not_rows(self, source):
        """P3-13:一次备份写两条 receipt,窗口应按"有结果的备份"数算。

        两次备份共用一个递增时钟:do_backup 默认每次新建时钟,会让两轮 receipt
        撞上同一时刻,窗口顺序退化成随机 tie-break。
        """
        from tests.test_content_backup import make_clock

        clock = make_clock()
        await seed_video_job(source)
        await do_backup(source, "run_one", now_fn=clock)
        insert_job(source, "job_extra", content_type="document", document_kind="article")
        second = await do_backup(source, "run_two", now_fn=clock)
        _drop_all_refs(source.repo)
        plan = source.repo.gc_mark(receipt_root_limit=1)
        assert plan.reachable_snapshots == (second.snapshot_digest,), (
            "keep-receipts=1 应保住最近一次备份,而不是被 in_progress 占掉名额"
        )

    def test_gc_cli_dry_run_on_readonly_repo(self, source, tmp_path):
        """P1-4:默认 dry-run 不得取写锁,只读仓库上也要出 JSON。"""
        import asyncio

        asyncio.run(_seed_for_cli(source))
        # 模拟只读挂载:去掉目录写权限
        lock_dir = source.repo.root / "locks"
        lock_dir.chmod(0o500)
        try:
            result_file = tmp_path / "ro.json"
            code = gc_main([
                "--repo", str(source.repo.root), "--sweep",
                "--result-file", str(result_file),
            ])
            payload = json.loads(result_file.read_text())
        finally:
            lock_dir.chmod(0o700)
        assert code == 0, "dry-run 在只读仓库上必须照常工作"
        assert payload["sweep"]["dry_run"] is True

    def test_gc_cli_break_lock_reports_holder(self, source, tmp_path):
        """P3-17:破锁前必须先把持锁者打出来供人工确认。"""
        import asyncio

        asyncio.run(_seed_for_cli(source))
        import os

        fd = os.open(source.repo.root / "locks" / "write.lock",
                     os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.write(fd, b'{"owner":"dead-backup","pid":1,"host":"h",'
                     b'"acquired_at":"2026-07-19T00:00:00+00:00","token":"t"}')
        os.close(fd)
        result_file = tmp_path / "brk.json"
        assert gc_main([
            "--repo", str(source.repo.root), "--break-lock",
            "--result-file", str(result_file),
        ]) == 0
        payload = json.loads(result_file.read_text())
        assert payload["held"] is True
        assert payload["holder"]["owner"] == "dead-backup"
        assert source.repo.write_lock_holder() is None


class TestScrubRejectsDamage:
    """§5.2.22:位翻转/缺 blob/错 ref/杂散与 symlink 全部由 scrub 拒收。"""

    async def _seeded(self, source):
        await seed_video_job(source)
        await do_backup(source)
        assert source.repo.scrub().ok
        return source.repo

    async def test_bit_flip_detected(self, source):
        repo = await self._seeded(source)
        path = repo.blob_path(sha(MEDIA_ONE))
        data = bytearray(path.read_bytes())
        data[-1] ^= 0x01
        path.write_bytes(bytes(data))
        report = repo.scrub()
        assert not report.ok
        assert any(item.kind == "blob_corrupt" for item in report.issues)

    async def test_missing_blob_detected(self, source):
        repo = await self._seeded(source)
        repo.blob_path(sha(MEDIA_ONE)).unlink()
        report = repo.scrub()
        assert any(item.kind == "snapshot_corrupt" for item in report.issues)

    async def test_broken_record_ref_detected(self, source):
        repo = await self._seeded(source)
        record_dir = repo.root / "records" / "step_result"
        next(iter(record_dir.iterdir())).unlink()
        report = repo.scrub()
        assert any(item.kind == "snapshot_corrupt" for item in report.issues)

    async def test_dangling_ref_detected(self, source):
        repo = await self._seeded(source)
        (repo.root / "refs" / "bogus").write_text("sha256:" + "f" * 64)
        report = repo.scrub()
        assert any(item.kind == "broken_ref" for item in report.issues)

    async def test_symlink_and_stray_file_detected(self, source):
        repo = await self._seeded(source)
        (repo.root / "records" / "job_core" / "link.json").symlink_to(
            repo.blob_path(sha(MEDIA_ONE))
        )
        (repo.root / "stray.bin").write_bytes(b"junk")
        report = repo.scrub()
        kinds = {item.kind for item in report.issues}
        assert "symlink" in kinds and "stray_file" in kinds

    async def test_traversal_named_object_is_stray(self, source):
        """路径穿越样式的文件名不会被当成合法对象。"""
        repo = await self._seeded(source)
        (repo.root / "snapshots" / "..evil.json").write_text("{}")
        report = repo.scrub()
        assert any(item.kind == "stray_file" for item in report.issues)

    async def test_oversized_record_is_rejected_on_read(self, source):
        """压缩炸弹样式:超限 JSON 在读回时被有界解析挡住。"""
        repo = await self._seeded(source)
        record_dir = repo.root / "records" / "job_core"
        victim = next(iter(record_dir.iterdir()))
        victim.write_bytes(b'{"a":' + b"[" * 200 + b"1" + b"]" * 200 + b"}")
        report = repo.scrub()
        assert any(item.kind == "record_corrupt" for item in report.issues)
