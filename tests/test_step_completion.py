"""manifest 消费侧测试:dual 读端、对账、rerun 失效边界、skipped 恢复与 backfill。

覆盖设计稿 §5.2 条 5(manifest 已发布 DB 未 done 重启修复不重复副作用)、条 6
(DB done 但 manifest 缺/损降 waiting 失效下游)、条 8(Part rerun 只保兄弟 Part
manifests 并失效 Job reduce)、条 9(AI 与 CPU 变化同一 stale 算法)、条 10
(deterministic skip 可恢复;no_worker 新环境变 waiting)、条 12(backfill 全覆盖
与 fail-closed)。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.conftest import make_fakeredis
from shared.config import AppConfig
from shared.models import Job, JobPart, Step, StepStatus
from shared.step_base import StepBase, file_hash
from shared.step_completion import (
    build_skipped_manifest,
    read_valid_manifest,
    step_definition_digest_for,
)
from shared.step_manifest import ManifestError, manifest_relative_path, validate_manifest
from shared.step_output_commit import StepOutput, build_step_manifest
from shared.step_scope import execution_step_key, part_scope
from shared.storage import LocalStorage
from scheduler.scheduler import Scheduler
from tests.current_schema_db import clone_current_schema_database


@pytest.fixture
def tmp_jobs_dir(tmp_path):
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    return jobs


@pytest.fixture
def db(tmp_path, current_schema_db_template):
    d = clone_current_schema_database(
        current_schema_db_template, tmp_path / "test.db",
    )
    yield d
    d.close()


@pytest.fixture
async def redis():
    client = make_fakeredis()
    yield client
    await client.close()


@pytest.fixture
def config(tmp_path, tmp_jobs_dir, configs_dir):
    return AppConfig(
        data_dir=tmp_path,
        db_path=tmp_path / "test.db",
        jobs_dir=tmp_jobs_dir,
        config_dir=configs_dir,
        prompts_dir=tmp_path / "prompts",
        pipelines={
            "test": {
                "steps": [
                    {"name": "A", "pool": "cpu", "depends_on": [], "retries": 1,
                     "outputs": ["out/*.json"]},
                    {"name": "B", "pool": "cpu", "depends_on": ["A"], "retries": 1,
                     "outputs": ["final/*.json"]},
                ]
            },
            "multi": {
                "steps": [
                    {"name": "01_dl", "pool": "io", "depends_on": [],
                     "scope": "part", "outputs": ["out/*.json"]},
                    {"name": "09_merge", "pool": "io", "depends_on": [],
                     "fan_in": ["01_dl"], "outputs": ["merged/*.json"]},
                ]
            },
        },
        pools={"pools": {"cpu": {"limit": 3}, "io": {"limit": 999}}},
        providers={},
    )


@pytest.fixture
def scheduler(redis, db, config, tmp_jobs_dir):
    return Scheduler(redis, db, config, storage=LocalStorage(tmp_jobs_dir))


def _write(root: Path, rel: str, data: bytes = b"{}") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _publish_done_manifest(
    jobs_dir: Path, job_id: str, scope_key: str, step: str, *,
    outputs: list[tuple[str, bytes]], exec_id: str = "w:1",
    fingerprints: dict | None = None,
) -> dict:
    """在 LocalStorage 布局下发布一份有效 done manifest + 对应输出文件。"""
    import hashlib

    prefix = f"parts/{scope_key.split(':', 1)[1]}/" if scope_key != "job" else ""
    entries = []
    for rel, data in outputs:
        _write(jobs_dir / job_id, f"{prefix}{rel}", data)
        entries.append(StepOutput(
            path=rel, job_rel=f"{prefix}{rel}", size_bytes=len(data),
            sha256=f"sha256:{hashlib.sha256(data).hexdigest()}", media_type=None,
        ))
    manifest, encoded, _digest = build_step_manifest(
        job_id=job_id, scope_key=scope_key, step=step,
        part_index=1 if scope_key != "job" else None,
        exec_id=exec_id, job_generation=1, attempt=1,
        started_at="2026-07-18T00:00:00Z", committed_at="2026-07-18T00:01:00Z",
        duration_sec=1.0,
        input_fingerprints=(
            fingerprints if fingerprints is not None
            else {"input": "sha256:" + "b" * 64}
        ),
        definition_digest="sha256:" + "c" * 64,
        outputs=entries,
        producer={
            "flori_version": "2.1.1", "build_sha": None, "worker_id": "w",
            "runner": "subprocess", "image": "flori/step-base",
            "image_digest": None, "tool_versions": {},
        },
    )
    _write(jobs_dir / job_id, manifest_relative_path(scope_key, step), encoded)
    return manifest


async def _seed_job(scheduler, job_id="j1", pipeline="test", parts=None):
    scheduler.db.create_job(
        Job(id=job_id, content_type="video", pipeline=pipeline, domain="general"),
        parts,
    )
    await scheduler.redis.init_job(job_id, pipeline, {
        "domain": "general", "style_tags": "[]",
    })
    await scheduler.redis.add_active_job(job_id)


# 条 5:manifest 已发布、DB/Redis 投影落后 → 重启对账修复,副作用幂等不重复


class TestReconcileRepairsProjection:
    @pytest.mark.asyncio
    async def test_manifest_published_db_waiting_repaired_idempotently(
        self, scheduler, tmp_jobs_dir,
    ):
        await _seed_job(scheduler)
        scheduler.db.upsert_step(Step(
            job_id="j1", name="A", status=StepStatus.WAITING, pool="cpu",
        ))
        await scheduler.redis.set_step_status("j1", "A", "waiting")
        _publish_done_manifest(
            tmp_jobs_dir, "j1", "job", "A", outputs=[("out/a.json", b"payload")],
        )

        await scheduler.reconcile_step_manifests("j1")

        assert await scheduler.redis.get_step_status("j1", "A") == "done"
        assert scheduler.db.get_steps("j1")[0].status == StepStatus.DONE
        # 第二次对账幂等:投影不再变化,不重复副作用(effects 重放本身幂等)。
        await scheduler.reconcile_step_manifests("j1")
        assert await scheduler.redis.get_step_status("j1", "A") == "done"

    @pytest.mark.asyncio
    async def test_repair_replays_effects_once_not_doubled(
        self, scheduler, tmp_jobs_dir, config,
    ):
        # 审查 A6:带 on_complete 的步骤修复后,两次 reconcile 只重放一次副作用。
        config.pipelines["test"]["steps"][0]["on_complete"] = [
            {"action": "sync_metadata"},
        ]
        await _seed_job(scheduler)
        await scheduler.redis.set_step_status("j1", "A", "waiting")
        scheduler.db.upsert_step(Step(
            job_id="j1", name="A", status=StepStatus.WAITING, pool="cpu",
        ))
        _publish_done_manifest(
            tmp_jobs_dir, "j1", "job", "A", outputs=[("out/a.json", b"payload")],
        )
        effect_calls = []

        async def counting_effects(job_id, step, effects):
            effect_calls.append((job_id, step))
            return True

        scheduler._run_completion_effects = counting_effects
        await scheduler.reconcile_step_manifests("j1")
        await scheduler.reconcile_step_manifests("j1")
        assert effect_calls == [("j1", "A")]

    @pytest.mark.asyncio
    async def test_running_step_never_flipped_by_reconcile(
        self, scheduler, tmp_jobs_dir,
    ):
        await _seed_job(scheduler)
        await scheduler.redis.set_step_status("j1", "A", "running")
        _publish_done_manifest(
            tmp_jobs_dir, "j1", "job", "A", outputs=[("out/a.json", b"payload")],
        )
        await scheduler.reconcile_step_manifests("j1")
        assert await scheduler.redis.get_step_status("j1", "A") == "running"


# 条 6:DB done 但 manifest 缺失/损坏 → manifest-only 降 waiting 并失效下游


class TestManifestMissingDemotion:
    @pytest.mark.asyncio
    async def test_manifest_only_demotes_and_invalidates_downstream(
        self, scheduler, tmp_jobs_dir, monkeypatch,
    ):
        monkeypatch.setenv("STEP_COMPLETION_MODE", "manifest-only")
        await _seed_job(scheduler)
        for name in ("A", "B"):
            scheduler.db.upsert_step(Step(
                job_id="j1", name=name, status=StepStatus.DONE, pool="cpu",
            ))
            await scheduler.redis.set_step_status("j1", name, "done")
        # A 无 manifest(人工 DB done);B 有有效 manifest(但上游已不可信)。
        _publish_done_manifest(
            tmp_jobs_dir, "j1", "job", "B", outputs=[("final/b.json", b"x")],
        )

        await scheduler.reconcile_step_manifests("j1")

        assert await scheduler.redis.get_step_status("j1", "A") == "waiting"
        assert await scheduler.redis.get_step_status("j1", "B") == "waiting"
        # 下游 manifest 一并删除,防下次对账把投影翻回 done。
        assert not (
            tmp_jobs_dir / "j1" / ".flori" / "steps" / "B" / "manifest.json"
        ).exists()

    @pytest.mark.asyncio
    async def test_downstream_manifest_deleted_regardless_of_status(
        self, scheduler, tmp_jobs_dir, monkeypatch,
    ):
        # 审查 A1 复现:A 缺 manifest 触发 demote 时,failed 状态的下游 B 若不删
        # manifest,同轮稍后会被翻回 done。修复后 B 的 manifest 必删、状态不被翻转。
        monkeypatch.setenv("STEP_COMPLETION_MODE", "manifest-only")
        await _seed_job(scheduler)
        scheduler.db.upsert_step(Step(
            job_id="j1", name="A", status=StepStatus.DONE, pool="cpu",
        ))
        scheduler.db.upsert_step(Step(
            job_id="j1", name="B", status=StepStatus.FAILED, pool="cpu",
        ))
        await scheduler.redis.set_step_status("j1", "A", "done")
        await scheduler.redis.set_step_status("j1", "B", "failed")
        _publish_done_manifest(
            tmp_jobs_dir, "j1", "job", "B", outputs=[("final/b.json", b"x")],
        )

        await scheduler.reconcile_step_manifests("j1")

        assert await scheduler.redis.get_step_status("j1", "A") == "waiting"
        # B 不被同轮据下游 manifest 翻回 done;manifest 已删。
        assert await scheduler.redis.get_step_status("j1", "B") == "failed"
        assert not (
            tmp_jobs_dir / "j1" / ".flori" / "steps" / "B" / "manifest.json"
        ).exists()

    @pytest.mark.asyncio
    async def test_corrupt_manifest_demotes_in_manifest_only(
        self, scheduler, tmp_jobs_dir, monkeypatch,
    ):
        monkeypatch.setenv("STEP_COMPLETION_MODE", "manifest-only")
        await _seed_job(scheduler)
        scheduler.db.upsert_step(Step(
            job_id="j1", name="A", status=StepStatus.DONE, pool="cpu",
        ))
        await scheduler.redis.set_step_status("j1", "A", "done")
        _write(tmp_jobs_dir / "j1", ".flori/steps/A/manifest.json", b"not json")
        await scheduler.reconcile_step_manifests("j1")
        assert await scheduler.redis.get_step_status("j1", "A") == "waiting"

    @pytest.mark.asyncio
    async def test_dual_mode_keeps_done_when_manifest_missing(
        self, scheduler, monkeypatch,
    ):
        monkeypatch.delenv("STEP_COMPLETION_MODE", raising=False)
        await _seed_job(scheduler)
        scheduler.db.upsert_step(Step(
            job_id="j1", name="A", status=StepStatus.DONE, pool="cpu",
        ))
        await scheduler.redis.set_step_status("j1", "A", "done")
        await scheduler.reconcile_step_manifests("j1")
        # dual:.done 仍是权威,manifest 缺失不降级。
        assert await scheduler.redis.get_step_status("j1", "A") == "done"


# 条 8:P02 rerun 只保 P01/P03 manifests 并失效 Job reduce


class TestPartRerunInvalidationBoundary:
    @pytest.mark.asyncio
    async def test_part_rerun_keeps_siblings_invalidates_reduce(
        self, scheduler, tmp_jobs_dir,
    ):
        parts = [
            JobPart(f"pt_{index}", "j_parts", index) for index in (1, 2, 3)
        ]
        await _seed_job(scheduler, job_id="j_parts", pipeline="multi", parts=parts)
        for part in parts:
            scope = part_scope(part.id)
            scheduler.db.upsert_step(Step(
                job_id="j_parts", name="01_dl", scope_key=scope,
                status=StepStatus.DONE, pool="io",
            ))
            name = execution_step_key(scope, "01_dl")
            await scheduler.redis.set_step_status("j_parts", name, "done")
            _publish_done_manifest(
                tmp_jobs_dir, "j_parts", scope, "01_dl",
                outputs=[("out/a.json", part.id.encode())],
            )
        scheduler.db.upsert_step(Step(
            job_id="j_parts", name="09_merge", status=StepStatus.DONE, pool="io",
        ))
        await scheduler.redis.set_step_status("j_parts", "09_merge", "done")
        _publish_done_manifest(
            tmp_jobs_dir, "j_parts", "job", "09_merge",
            outputs=[("merged/all.json", b"merged")],
        )

        reset = await scheduler.rerun(
            "j_parts", execution_step_key(part_scope("pt_2"), "01_dl"),
        )

        assert set(reset) == {
            execution_step_key(part_scope("pt_2"), "01_dl"), "09_merge",
        }
        manifest_of = lambda scope, step: (
            tmp_jobs_dir / "j_parts" / manifest_relative_path(scope, step)
        )
        assert not manifest_of(part_scope("pt_2"), "01_dl").exists()
        assert not manifest_of("job", "09_merge").exists()
        assert manifest_of(part_scope("pt_1"), "01_dl").exists()
        assert manifest_of(part_scope("pt_3"), "01_dl").exists()
        # 旧输出按旧 manifest 精确删除;兄弟 Part 输出保持。
        assert not (
            tmp_jobs_dir / "j_parts" / "parts" / "pt_2" / "out" / "a.json"
        ).exists()
        assert (
            tmp_jobs_dir / "j_parts" / "parts" / "pt_1" / "out" / "a.json"
        ).exists()


# 审查 A2:rerun 的 manifest/输出删除失败不吞,整个命令失败交 PEL 重投


class TestRerunDeleteFailurePropagates:
    @pytest.mark.asyncio
    async def test_rerun_manifest_delete_failure_propagates_for_retry(
        self, scheduler, tmp_jobs_dir,
    ):
        await _seed_job(scheduler)
        scheduler.db.upsert_step(Step(
            job_id="j1", name="A", status=StepStatus.DONE, pool="cpu",
        ))
        await scheduler.redis.set_step_status("j1", "A", "done")
        _publish_done_manifest(
            tmp_jobs_dir, "j1", "job", "A", outputs=[("out/a.json", b"x")],
        )

        real_delete = scheduler.storage.delete_file
        calls = {"n": 0}

        async def flaky_delete(job_id, rel):
            calls["n"] += 1
            raise RuntimeError("central storage down")

        scheduler.storage.delete_file = flaky_delete
        with pytest.raises(RuntimeError, match="central storage down"):
            await scheduler.rerun("j1", "A", idempotency_key="op-1")
        assert calls["n"] >= 1

        # 同 idempotency key 重放:删除恢复后 rerun 完整重执行(幂等重试)。
        scheduler.storage.delete_file = real_delete
        reset = await scheduler.rerun("j1", "A", idempotency_key="op-1")
        assert "A" in reset
        assert not (
            tmp_jobs_dir / "j1" / ".flori" / "steps" / "A" / "manifest.json"
        ).exists()
        assert not (tmp_jobs_dir / "j1" / "out" / "a.json").exists()


# 条 9:AI 模型/Prompt 变化与 CPU version/config 变化使用同一 stale 算法


class TestUnifiedStaleAlgorithm:
    def test_ai_and_cpu_changes_move_same_digest(self, config):
        base_ai = {
            "name": "11_smart", "pool": "ai", "depends_on": [], "outputs": ["o/*"],
            "version": "5",
            "ai": {"primary": {"provider": "claude-cli", "model": "m1"}},
        }
        base_cpu = {
            "name": "06_ocr", "pool": "cpu", "depends_on": [], "outputs": ["o/*"],
            "version": "2",
        }
        digest = lambda cfg: step_definition_digest_for(
            "video", cfg, config=config, domain="general", style_tags=[],
        )
        ai_before = digest(base_ai)
        ai_after = digest({
            **base_ai,
            "ai": {"primary": {"provider": "claude-cli", "model": "m2"}},
        })
        cpu_before = digest(base_cpu)
        cpu_after = digest({**base_cpu, "version": "3"})
        # 同一函数、同一字段(compatibility.definition_digest):任一语义变化都换摘要。
        assert ai_before != ai_after
        assert cpu_before != cpu_after
        # 纯运行字段不动摘要(AI/CPU 同规则)。
        assert digest({**base_ai, "timeout_sec": 999}) == ai_before
        assert digest({**base_cpu, "pool": "io"}) == cpu_before

    def test_prompt_template_binding_moves_digest(self, config):
        base = {
            "name": "11_smart", "pool": "ai", "depends_on": [], "outputs": ["o/*"],
            "ai": {"primary": {"provider": "claude-cli", "model": "m1"}},
        }
        digest = lambda cfg: step_definition_digest_for(
            "video", cfg, config=config, domain="general", style_tags=[],
        )
        assert digest(base) != digest({**base, "prompt_template": "05_concepts"})


# 条 10:deterministic skip 可恢复;no_worker skip 新环境变 waiting


class TestSkipRecovery:
    @pytest.mark.asyncio
    async def test_environmental_skip_resets_to_waiting(self, scheduler):
        await _seed_job(scheduler)
        scheduler.db.upsert_step(Step(
            job_id="j1", name="A", status=StepStatus.SKIPPED, pool="cpu",
        ))
        # 无 manifest、非条件步、非 mechanical_only:无法确定性重推导 = 环境性 skip。
        await scheduler.redis.set_step_status("j1", "A", "skipped")
        await scheduler.reconcile_step_manifests("j1")
        assert await scheduler.redis.get_step_status("j1", "A") == "waiting"

    @pytest.mark.asyncio
    async def test_deterministic_rule_skip_is_kept(self, scheduler, config):
        config.pipelines["test"]["steps"][0]["rules"] = [
            {"exists": "input/*.srt", "when": "on"}, {"when": "skip"},
        ]
        await _seed_job(scheduler)
        await scheduler.redis.set_step_status("j1", "A", "skipped")
        await scheduler.reconcile_step_manifests("j1")
        # rules 对当前证据仍确定性求值为 skip → 保持。
        assert await scheduler.redis.get_step_status("j1", "A") == "skipped"

    @pytest.mark.asyncio
    async def test_skipped_manifest_survives_reconcile(self, scheduler, tmp_jobs_dir):
        await _seed_job(scheduler)
        definition = step_definition_digest_for(
            "test", scheduler.config.pipelines["test"]["steps"][0],
            config=scheduler.config, domain="general", style_tags=[],
        )
        _manifest, encoded = build_skipped_manifest(
            job_id="j1", scope_key="job", step="A", part_index=None,
            job_generation=1, reason_code="rule_false",
            definition_digest=definition, flori_version="2.1.1",
        )
        _write(tmp_jobs_dir / "j1", manifest_relative_path("job", "A"), encoded)
        await scheduler.redis.set_step_status("j1", "A", "skipped")
        await scheduler.reconcile_step_manifests("j1")
        assert await scheduler.redis.get_step_status("j1", "A") == "skipped"

    def test_no_worker_never_becomes_durable_manifest(self):
        with pytest.raises(ManifestError):
            build_skipped_manifest(
                job_id="j1", scope_key="job", step="A", part_index=None,
                job_generation=1, reason_code="no_worker",
                definition_digest="sha256:" + "c" * 64,
            )

    @pytest.mark.asyncio
    async def test_dag_planner_publishes_deterministic_skip_manifest(
        self, scheduler, tmp_jobs_dir,
    ):
        cfg = {
            "name": "A", "template_step": "A", "scope_key": "job",
            "pool": "ai", "depends_on": [], "outputs": ["out/*.json"],
        }
        await _seed_job(scheduler)
        await scheduler._dag_planner._publish_skipped_manifest(
            "j1", cfg, "mechanical_only",
        )
        manifest = await read_valid_manifest(
            scheduler.storage, "j1", "job", "A",
        )
        assert manifest is not None and manifest["outcome"] == "skipped"
        assert manifest["skip"]["reason_code"] == "mechanical_only"
        assert manifest["producer"]["kind"] == "scheduler_skip"
        # 审查 A4:mechanical_only 以 flags 摘要充 condition 证据。
        assert manifest["skip"]["condition_digest"] is not None

    @pytest.mark.asyncio
    async def test_rule_false_skip_manifest_carries_evidence_digests(
        self, scheduler, tmp_jobs_dir,
    ):
        cfg = {
            "name": "A", "template_step": "A", "scope_key": "job",
            "pool": "cpu", "depends_on": [], "outputs": ["out/*.json"],
            "rules": [{"exists": "input/*.srt", "when": "on"}, {"when": "skip"}],
        }
        await _seed_job(scheduler)
        await scheduler._dag_planner._publish_skipped_manifest("j1", cfg, "rule_false")
        manifest = await read_valid_manifest(scheduler.storage, "j1", "job", "A")
        assert manifest is not None
        assert manifest["skip"]["reason_code"] == "rule_false"
        assert manifest["skip"]["rule_digest"] is not None
        assert manifest["skip"]["condition_digest"] is not None


# 审查 B2:clone 重签发不盲信父声明,按子 job 实际文件重算


class TestCloneReissue:
    async def _reissue(self, jobs_dir, tamper=False):
        import shutil

        from api.routes.jobs import _reissue_step_manifests

        _publish_done_manifest(
            jobs_dir, "j_parent", "job", "A", outputs=[("out/a.json", b"original")],
        )
        # 模拟 storage.clone(排除 .flori):只拷业务文件到子 job。
        _write(jobs_dir / "j_child", "out/a.json", b"original")
        if tamper:
            (jobs_dir / "j_child" / "out" / "a.json").write_bytes(b"tampered")  # 同长度
        storage = LocalStorage(jobs_dir)
        await _reissue_step_manifests(
            storage, parent_id="j_parent", new_id="j_child",
            by_name={"A": {"name": "A", "scope": "job"}},
            reset_steps=set(), part_ids=[],
        )
        return jobs_dir / "j_child" / ".flori" / "steps" / "A" / "manifest.json"

    @pytest.mark.asyncio
    async def test_clone_reissue_new_identity(self, tmp_jobs_dir):
        manifest_path = await self._reissue(tmp_jobs_dir)
        manifest = json.loads(manifest_path.read_text())
        validate_manifest(manifest)
        assert manifest["job_id"] == "j_child"
        assert manifest["producer"]["kind"] == "clone_reissue"
        assert manifest["execution"]["exec_id"].startswith("clone:j_parent:")
        # 输出哈希与父声明一致(等价 manifest)。
        assert manifest["outputs"][0]["sha256"].startswith("sha256:")

    @pytest.mark.asyncio
    async def test_clone_reissue_refuses_tampered_outputs(self, tmp_jobs_dir):
        # 同长度篡改:size 相同、SHA 不同 → 拒绝重签发,子 job 无该步 manifest。
        manifest_path = await self._reissue(tmp_jobs_dir, tamper=True)
        assert not manifest_path.exists()


# should_run 读端切换(dual:manifest 优先,.done fallback)


class _ProbeStep(StepBase):
    def __init__(self, job_dir, config=None):
        super().__init__("A", job_dir, config or {})

    def execute(self):
        return {}

    def input_hashes(self):
        source = self.job_dir / "input" / "data.json"
        return {"data": file_hash(source)} if source.exists() else {}


class TestShouldRunManifestFirst:
    def _publish(self, job_dir: Path, *, input_digest: str, definition: str) -> None:
        manifest = {
            "format": "flori-step-manifest", "format_version": 1,
            "job_id": "j1",
            "scope": {"kind": "job", "scope_key": "job", "part_id": None,
                      "part_index": None},
            "step": "A", "outcome": "done",
            "execution": {"exec_id": "w:1", "job_generation": 1, "attempt": 1,
                          "started_at": "2026-07-18T00:00:00Z",
                          "committed_at": "2026-07-18T00:00:01Z",
                          "duration_sec": 0.1},
            "compatibility": {
                "input_fingerprints": {}, "input_digest": input_digest,
                "definition_digest": definition,
            },
            "producer": {"flori_version": "1", "build_sha": None, "worker_id": None,
                         "runner": "subprocess", "image": None,
                         "image_digest": None, "tool_versions": {}},
            "outputs": [], "skip": None,
        }
        # input_digest 需与 fingerprints 匹配才能过 schema。
        from shared.step_manifest import compute_input_digest

        manifest["compatibility"]["input_digest"] = compute_input_digest({})
        validate_manifest(manifest)
        path = job_dir / ".flori" / "steps" / "A" / "manifest.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(manifest))

    def test_manifest_match_skips_run(self, tmp_path):
        definition = "sha256:" + "c" * 64
        self._publish(tmp_path, input_digest="", definition=definition)
        step = _ProbeStep(
            tmp_path, {"step": {"name": "A", "definition_digest": definition}},
        )
        assert step.should_run() is False

    def test_manifest_definition_mismatch_forces_run_even_with_done(self, tmp_path):
        self._publish(
            tmp_path, input_digest="", definition="sha256:" + "c" * 64,
        )
        step = _ProbeStep(
            tmp_path,
            {"step": {"name": "A", "definition_digest": "sha256:" + "d" * 64}},
        )
        # 兼容的 .done 在场也不回退:manifest 权威优先。
        step.artifacts.write_done({
            "step": "A", "input_hashes": {}, "def_digest": step._def_digest(),
            "finished_at": "2026-07-18T00:00:00+00:00",
        })
        assert step.should_run() is True

    def test_nas_source_fingerprints_symmetry(self, tmp_path, tmp_jobs_dir):
        # 审查 C1:manifest 的 input_fingerprints 含 source_*(worker 生产端并入);
        # should_run 侧经 step config 注入同样的 source_* 后计算同一 current。
        from shared.step_manifest import compute_input_digest

        source = {
            "source_ref": "zg-library/P01.mkv",
            "source_digest": "sha256:" + "a" * 64,
            "source_size_bytes": "1024",
        }
        _publish_done_manifest(
            tmp_jobs_dir, "j_nas", "job", "A", outputs=[("out/a.json", b"x")],
            fingerprints=dict(source),
        )
        job_dir = tmp_jobs_dir / "j_nas"
        step = _ProbeStep(job_dir, {"step": {
            "name": "A", "source_fingerprints": dict(source),
        }})
        assert step.should_run() is False
        # 源 digest 变化 → current 变 → 必跑。
        changed = {**source, "source_digest": "sha256:" + "f" * 64}
        step = _ProbeStep(job_dir, {"step": {
            "name": "A", "source_fingerprints": changed,
        }})
        assert step.should_run() is True

    def test_manifest_missing_falls_back_to_done_in_dual(self, tmp_path, monkeypatch):
        monkeypatch.delenv("STEP_COMPLETION_MODE", raising=False)
        step = _ProbeStep(tmp_path)
        step.artifacts.write_done({
            "step": "A", "input_hashes": {}, "def_digest": step._def_digest(),
            "finished_at": "2026-07-18T00:00:00+00:00",
        })
        assert step.should_run() is False
        monkeypatch.setenv("STEP_COMPLETION_MODE", "manifest-only")
        assert step.should_run() is True


# 条 12:backfill report/backfill/verify 与 fail-closed


class TestBackfill:
    def _seed(self, db, jobs_dir, *, with_def_digest=True, marker=True,
              corrupt=False, def_digest=None, step_status=StepStatus.DONE,
              flags=None, pool="cpu"):
        from shared.step_base import def_digest_for

        job = Job(
            id="j_bf", content_type="video", pipeline="test", domain="general",
            meta={"flags": flags} if flags else {},
        )
        db.create_job(job)
        db.upsert_step(Step(
            job_id="j_bf", name="A", status=step_status, pool=pool,
        ))
        _write(jobs_dir / "j_bf", "out/a.json", b"backfilled")
        if marker:
            payload = {
                "step": "A",
                "input_hashes": {"data": "sha256:" + "b" * 64, "provider": ""},
                "finished_at": "2026-07-18T00:00:00+00:00",
            }
            if with_def_digest:
                # 缺省与当前定义一致(等价 should_run 门通过);drift 用例显式覆盖。
                payload["def_digest"] = def_digest or def_digest_for(None, None)
            data = b"not json" if corrupt else json.dumps(payload).encode()
            _write(jobs_dir / "j_bf", ".A.done", data)
        return job

    async def _run(self, db, config, jobs_dir, command="report", accept=None):
        from shared.step_manifest_migration import run_migration

        return await run_migration(
            db=db, storage=LocalStorage(jobs_dir), config=config,
            command=command, accept_legacy_definition=accept, job_ids=["j_bf"],
        )

    @pytest.mark.asyncio
    async def test_report_then_backfill_then_idempotent(
        self, db, config, tmp_jobs_dir,
    ):
        self._seed(db, tmp_jobs_dir)
        report = await self._run(db, config, tmp_jobs_dir, "report")
        assert report.eligible == 1 and report.issued == 0
        assert not (
            tmp_jobs_dir / "j_bf" / ".flori" / "steps" / "A" / "manifest.json"
        ).exists()

        report = await self._run(db, config, tmp_jobs_dir, "backfill")
        assert report.issued == 1
        manifest = json.loads(
            (tmp_jobs_dir / "j_bf" / ".flori" / "steps" / "A" / "manifest.json")
            .read_text()
        )
        validate_manifest(manifest)
        assert manifest["producer"]["kind"] == "legacy_done_backfill"
        assert manifest["execution"]["exec_id"].startswith("legacy:")
        assert manifest["outputs"][0]["path"] == "out/a.json"
        # 空串指纹值(provider="")原样保留。
        assert manifest["compatibility"]["input_fingerprints"]["provider"] == ""

        report = await self._run(db, config, tmp_jobs_dir, "backfill")
        assert report.already_present == 1 and report.issued == 0

        report = await self._run(db, config, tmp_jobs_dir, "verify")
        assert report.verified == 1 and report.verify_failures == []

    @pytest.mark.asyncio
    async def test_missing_and_corrupt_marker_fail_closed(
        self, db, config, tmp_jobs_dir,
    ):
        self._seed(db, tmp_jobs_dir, marker=False)
        report = await self._run(db, config, tmp_jobs_dir, "backfill")
        assert report.issued == 0
        assert report.inconsistent[0]["reason"] == "done_marker_missing"

        _write(tmp_jobs_dir / "j_bf", ".A.done", b"not json")
        report = await self._run(db, config, tmp_jobs_dir, "backfill")
        assert report.issued == 0
        assert report.inconsistent[0]["reason"] == "done_marker_corrupt"

    @pytest.mark.asyncio
    async def test_legacy_definition_requires_explicit_accept(
        self, db, config, tmp_jobs_dir,
    ):
        self._seed(db, tmp_jobs_dir, with_def_digest=False)
        report = await self._run(db, config, tmp_jobs_dir, "backfill")
        assert report.issued == 0 and report.legacy_definition_unverified == 1

        report = await self._run(
            db, config, tmp_jobs_dir, "backfill", accept="current",
        )
        assert report.issued == 1

    @pytest.mark.asyncio
    async def test_verify_detects_tampered_output(self, db, config, tmp_jobs_dir):
        self._seed(db, tmp_jobs_dir)
        await self._run(db, config, tmp_jobs_dir, "backfill")
        _write(tmp_jobs_dir / "j_bf", "out/a.json", b"tampered!!")
        report = await self._run(db, config, tmp_jobs_dir, "verify")
        assert report.verified == 0
        assert "output_mismatch" in report.verify_failures[0]["reason"]

    @pytest.mark.asyncio
    async def test_backfill_rejects_definition_drift(self, db, config, tmp_jobs_dir):
        # 审查 B1 攻击复现:.done 的 def_digest 属旧定义,签发会给旧产物披上
        # 当前定义 manifest;必须 definition_drift 拒签,accept 开关不豁免此情形。
        self._seed(db, tmp_jobs_dir, def_digest="sha256:" + "e" * 64)
        report = await self._run(db, config, tmp_jobs_dir, "backfill")
        assert report.issued == 0
        assert report.inconsistent[0]["reason"] == "definition_drift"
        report = await self._run(
            db, config, tmp_jobs_dir, "backfill", accept="current",
        )
        assert report.issued == 0
        assert report.inconsistent[0]["reason"] == "definition_drift"

    @pytest.mark.asyncio
    async def test_skipped_mechanical_only_rederived_and_issued(
        self, db, config, tmp_jobs_dir,
    ):
        # 审查 A3:DB skipped 用与 dag_planner 同源判定重推导,通过者签发
        # legacy_skip_backfill;verify 认可。
        config.pipelines["test"]["steps"][0]["pool"] = "ai"  # 同源判定看 pipeline cfg 的 pool
        self._seed(
            db, tmp_jobs_dir, step_status=StepStatus.SKIPPED,
            flags={"mechanical_only": True}, pool="ai", marker=False,
        )
        report = await self._run(db, config, tmp_jobs_dir, "backfill")
        assert report.issued == 1
        manifest = json.loads(
            (tmp_jobs_dir / "j_bf" / ".flori" / "steps" / "A" / "manifest.json")
            .read_text()
        )
        validate_manifest(manifest)
        assert manifest["outcome"] == "skipped"
        assert manifest["skip"]["reason_code"] == "mechanical_only"
        assert manifest["producer"]["kind"] == "legacy_skip_backfill"
        report = await self._run(db, config, tmp_jobs_dir, "verify")
        assert report.verify_failures == []

    @pytest.mark.asyncio
    async def test_skipped_not_rederivable_reported(self, db, config, tmp_jobs_dir):
        # no_worker 等环境性 skip 无法重推导:只进报告,不签发。
        self._seed(db, tmp_jobs_dir, step_status=StepStatus.SKIPPED, marker=False)
        report = await self._run(db, config, tmp_jobs_dir, "backfill")
        assert report.issued == 0
        assert report.inconsistent[0]["reason"] == "skip_not_rederivable"

    @pytest.mark.asyncio
    async def test_verify_bidirectional_catches_nonterminal_manifest(
        self, db, config, tmp_jobs_dir,
    ):
        # 审查 D1:manifest 在而 DB 非终态 → verify 失败;孤儿 manifest 检出。
        self._seed(db, tmp_jobs_dir, step_status=StepStatus.WAITING)
        _publish_done_manifest(
            tmp_jobs_dir, "j_bf", "job", "A", outputs=[("out/a.json", b"backfilled")],
        )
        _write(
            tmp_jobs_dir / "j_bf", ".flori/steps/GHOST/manifest.json", b"{}",
        )
        report = await self._run(db, config, tmp_jobs_dir, "verify")
        reasons = {item["reason"] for item in report.verify_failures}
        assert "db_not_terminal:waiting" in reasons
        assert "orphan_manifest_outside_pipeline" in reasons

    @pytest.mark.asyncio
    async def test_cleanup_removes_only_done_marker(self, db, config, tmp_jobs_dir):
        self._seed(db, tmp_jobs_dir)
        await self._run(db, config, tmp_jobs_dir, "backfill")
        report = await self._run(db, config, tmp_jobs_dir, "cleanup")
        assert report.cleaned == 1
        assert not (tmp_jobs_dir / "j_bf" / ".A.done").exists()
        assert (
            tmp_jobs_dir / "j_bf" / ".flori" / "steps" / "A" / "manifest.json"
        ).exists()
        assert (tmp_jobs_dir / "j_bf" / "out" / "a.json").exists()
