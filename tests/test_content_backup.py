"""content backup 编排测试:选择/幂等/一致性重读/失败审计/未知项门/CLI。"""

import hashlib
import json
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Sequence

import pytest

import shared.content_backup as content_backup
from shared.content_backup import (
    BackupError,
    main,
    run_backup,
)
from shared.content_repository import ContentRepository
from shared.step_manifest import compute_input_digest, manifest_relative_path
from shared.storage import LocalStorage


HEX_A = "sha256:" + "a" * 64
HEX_D = "sha256:" + "d" * 64
MEDIA_ONE = b"media-bytes-one-" + b"1" * 64
MEDIA_TWO = b"media-bytes-two-" + b"2" * 64
MEDIA_THREE = b"media-bytes-three-" + b"3" * 64

T_CREATED = "2026-07-18T06:00:00+00:00"
T_STARTED = "2026-07-18T06:10:00+00:00"
T_FINISHED = "2026-07-18T06:20:00+00:00"


def fixed_now() -> str:
    return "2026-07-18T08:00:00Z"


def sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


@pytest.fixture
def env(tmp_path, current_schema_db_template):
    db_path = tmp_path / "flori.db"
    shutil.copy(current_schema_db_template, db_path)
    return SimpleNamespace(
        db=db_path,
        jobs_dir=tmp_path / "jobs",
        storage=LocalStorage(tmp_path / "jobs"),
        repo=ContentRepository.create(tmp_path / "repo"),
        tmp=tmp_path,
        parts={},
    )


def db_exec(db_path: Path, sql: str, params: tuple = ()) -> None:
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(sql, params)
        connection.commit()
    finally:
        connection.close()


def insert_job(
    env, job_id: str, *, url: str | None = None,
    collection_id: str | None = None, content_type: str = "video",
    document_kind: str = "", pipeline: str | None = None,
):
    # pipeline 名必须是 configs/pipelines.yaml 里的真名:导入侧投影按它展开步骤,
    # 用虚构名会让 job_steps 静默为空,掩盖投影 bug。
    db_exec(env.db, (
        "INSERT INTO jobs (id, content_type, document_kind, pipeline, url,"
        " collection_id, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)"
    ), (
        job_id, content_type, document_kind,
        pipeline or ("document" if content_type == "document" else "video"), url,
        collection_id, "processing", T_CREATED, T_CREATED,
    ))


def insert_part(
    env, part_id: str, job_id: str, index: int, *,
    source_url: str | None = None, source_ref: str | None = None,
    source_digest: str | None = None, size_bytes: int | None = None,
):
    db_exec(env.db, (
        "INSERT INTO job_parts (id, job_id, part_index, source_url, source_ref,"
        " source_digest, size_bytes, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)"
    ), (
        part_id, job_id, index, source_url, source_ref, source_digest, size_bytes,
        T_CREATED, T_CREATED,
    ))
    env.parts.setdefault(job_id, []).append((index, part_id))


async def ensure_job_json(env) -> None:
    """给还没有 job.json 的多 Part Job 补默认清单;已存在的不覆盖(B3 用例自己写)。"""
    for job_id, entries in env.parts.items():
        if await env.storage.read_file(job_id, "job.json") is None:
            ordered = [part_id for _index, part_id in sorted(entries)]
            await write_job_json(env, job_id, ordered)


async def write_job_json(env, job_id: str, part_ids: Sequence[str], **extra) -> None:
    """写根 job.json 的 Part 清单;DB 有 Part 时它是必需产物(B3)。"""
    root = {"job_id": job_id, "parts": [{"part_id": pid} for pid in part_ids]}
    root.update(extra)
    await env.storage.write_file(job_id, "job.json", json.dumps(root).encode())


def insert_step(
    env, job_id: str, scope_key: str, step: str, status: str, *,
    error: str | None = None, retries: int = 0, pool: str = "cpu",
    started_at: str | None = None, finished_at: str | None = None,
):
    db_exec(env.db, (
        "INSERT INTO job_steps (job_id, scope_key, step, status, pool, retries,"
        " started_at, finished_at, error) VALUES (?,?,?,?,?,?,?,?,?)"
    ), (job_id, scope_key, step, status, pool, retries, started_at, finished_at, error))


def insert_collection(env, collection_id: str, name: str = "论文集"):
    db_exec(env.db, (
        "INSERT INTO collections (id, name, domain, created_at, updated_at)"
        " VALUES (?,?,?,?,?)"
    ), (collection_id, name, "general", T_CREATED, T_CREATED))


def insert_ai_usage(env, exec_id: str, job_id: str, step: str):
    db_exec(env.db, (
        "INSERT INTO ai_usage (exec_id, job_id, step, provider, model, created_at)"
        " VALUES (?,?,?,?,?,?)"
    ), (exec_id, job_id, step, "claude", "claude-x", T_CREATED))


def build_manifest(
    job_id: str, scope_key: str, step: str, outputs: dict[str, bytes], *,
    part_index: int | None = None, exec_id: str = "exec_1",
    outcome: str = "done", skip_reason: str | None = None,
    definition_digest: str = HEX_D,
) -> dict:
    part_id = scope_key.split(":", 1)[1] if scope_key != "job" else None
    fingerprints = {"src": HEX_A}
    return {
        "format": "flori-step-manifest",
        "format_version": 1,
        "job_id": job_id,
        "scope": {
            "kind": "part" if part_id else "job",
            "scope_key": scope_key,
            "part_id": part_id,
            "part_index": part_index if part_id else None,
        },
        "step": step,
        "outcome": outcome,
        "execution": {
            "exec_id": exec_id,
            "job_generation": 1,
            "attempt": 1,
            "started_at": "2026-07-18T06:10:00Z",
            "committed_at": "2026-07-18T06:20:00Z",
            "duration_sec": 600,
        },
        "compatibility": {
            "input_fingerprints": fingerprints,
            "input_digest": compute_input_digest(fingerprints),
            "definition_digest": definition_digest,
        },
        "producer": {
            "flori_version": "2.2.0",
            "build_sha": None,
            "worker_id": "w1",
            "runner": "subprocess",
            "image": None,
            "image_digest": None,
            "tool_versions": {},
        },
        "outputs": [
            {
                "path": path,
                "size_bytes": len(data),
                "sha256": sha(data),
                "media_type": None,
            }
            for path, data in sorted(outputs.items())
        ] if outcome == "done" else [],
        "skip": None if outcome == "done" else {
            "reason_code": skip_reason or "rule_false",
            "rule_digest": None,
            "condition_digest": None,
        },
    }


async def commit_step(
    env, job_id: str, scope_key: str, step: str, outputs: dict[str, bytes], **kwargs,
) -> dict:
    """写产物文件 + 发布 manifest(测试直写,绕过 commit fence)。"""
    part_id = scope_key.split(":", 1)[1] if scope_key != "job" else None
    prefix = f"parts/{part_id}/" if part_id else ""
    for rel, data in outputs.items():
        await env.storage.write_file(job_id, prefix + rel, data)
    manifest = build_manifest(job_id, scope_key, step, outputs, **kwargs)
    await env.storage.write_file(
        job_id, manifest_relative_path(scope_key, step),
        json.dumps(manifest).encode(),
    )
    return manifest


async def seed_video_job(env, job_id: str = "job_alpha") -> list[str]:
    """两 Part 全 done 的标准视频 Job;返回 part id 列表。"""
    insert_job(env, job_id, url="https://www.bilibili.com/video/BV1xx411c7mD")
    part_ids = [f"pt_{job_id[-5:]}{index}" for index in (1, 2)]
    for index, part_id in enumerate(part_ids, start=1):
        insert_part(env, part_id, job_id, index)
        scope = f"part:{part_id}"
        insert_step(
            env, job_id, scope, "01_download", "done",
            started_at=T_STARTED, finished_at=T_FINISHED,
        )
        media = MEDIA_ONE if index == 1 else MEDIA_TWO
        await commit_step(
            env, job_id, scope, "01_download",
            {"input/source.mp4": media}, part_index=index, exec_id=f"exec_{part_id}",
        )
    await write_job_json(env, job_id, part_ids)
    return part_ids


async def do_backup(env, run_id: str = "run_a", **kwargs):
    await ensure_job_json(env)
    kwargs.setdefault("now_fn", make_clock())
    return await run_backup(
        db_path=env.db,
        storage=env.storage,
        repository=env.repo,
        run_id=run_id,
        app_version="2.2.0",
        **kwargs,
    )


def make_clock():
    """每次调用步进一秒:同 run 的多条 receipt 需要可排序的不同时刻。"""
    state = {"tick": 0}

    def _now() -> str:
        state["tick"] += 1
        moment = datetime(2026, 7, 18, 8, 0, tzinfo=timezone.utc) + timedelta(
            seconds=state["tick"]
        )
        return moment.strftime("%Y-%m-%dT%H:%M:%SZ")

    return _now


def count_files(root: Path, subdir: str) -> int:
    base = root / subdir
    return sum(1 for path in base.rglob("*") if path.is_file()) if base.is_dir() else 0


class TestFullBackup:
    async def test_tmp_spool_symlink_cannot_truncate_external_target(self, env, monkeypatch):
        await seed_video_job(env)
        target = env.tmp / "do-not-touch"
        target.write_bytes(b"protected")
        spool_nonce = "f" * 32
        real_clean = env.repo.clean_tmp
        real_token_hex = content_backup.secrets.token_hex

        def clean_then_occupy() -> int:
            removed = real_clean()
            (env.repo.tmp_dir / f"spool-{spool_nonce}").symlink_to(target)
            return removed

        monkeypatch.setattr(env.repo, "clean_tmp", clean_then_occupy)
        monkeypatch.setattr(
            content_backup.secrets, "token_hex",
            lambda size: spool_nonce if size == 16 else real_token_hex(size),
        )
        with pytest.raises(BackupError, match="occupied before exclusive create"):
            await do_backup(env)
        assert target.read_bytes() == b"protected"

    async def test_backup_and_repeat_zero_growth(self, env):
        insert_collection(env, "col_1")
        await seed_video_job(env)
        db_exec(env.db, "UPDATE jobs SET collection_id='col_1' WHERE id='job_alpha'")

        first = await do_backup(env, "run_a")
        assert env.repo.get_ref("latest") == first.snapshot_digest
        assert not first.hit_existing_snapshot
        assert first.stats["jobs"] == 1
        assert first.stats["parts"] == 2
        assert first.stats["step_results"] == 2
        assert first.stats["blobs_created"] == 2
        assert first.stats["unknown_paths"] == 0

        before = {
            sub: count_files(env.repo.root, sub)
            for sub in ("blobs", "records", "snapshots")
        }
        second = await do_backup(env, "run_b")
        assert second.snapshot_digest == first.snapshot_digest
        assert second.hit_existing_snapshot and not second.reused_run
        assert second.stats["blobs_created"] == 0
        # 第二轮走内容寻址增量:不再重读产物字节(C2)
        assert second.stats["step_results_incremental"] == 2
        assert second.stats["blob_bytes_rehashed"] == 0
        after = {
            sub: count_files(env.repo.root, sub)
            for sub in ("blobs", "records", "snapshots")
        }
        assert after == before
        # 每次运行两条 receipt(in_progress + success),快照与 blob 不增长
        assert len(env.repo.list_receipts()) == 4

    async def test_same_run_id_short_circuits(self, env):
        await seed_video_job(env)
        first = await do_backup(env, "run_a")
        again = await do_backup(env, "run_a")
        assert again.reused_run and again.snapshot_digest == first.snapshot_digest
        # 短路不重跑,因此不再新增 receipt(首轮的 in_progress + success 共两条)
        assert len(env.repo.list_receipts()) == 2

    async def test_empty_database_backup(self, env):
        result = await do_backup(env)
        assert result.stats["jobs"] == 0
        assert env.repo.get_ref("latest") == result.snapshot_digest

    async def test_snapshot_contents_pass_repository_verification(self, env):
        await seed_video_job(env)
        result = await do_backup(env)
        # 独立实例全量重验(闭包含 blob 等值),证明发布物自洽
        fresh = ContentRepository.open(env.repo.root)
        body = fresh.get_snapshot(result.snapshot_digest)
        assert len(body["records"]["step_results"]) == 2
        assert sorted(body["blob_refs"]) == sorted([sha(MEDIA_ONE), sha(MEDIA_TWO)])


class TestPartialSuccess:
    async def test_failed_part_media_not_collected(self, env):
        """§2.5.3/§5.2.5/§5.2.10:P1/P3 备份,P2 只留审计;同名完整文件零采集。"""
        job_id = "job_beta"
        insert_job(env, job_id)
        for index, part_id in enumerate(("pt_b1", "pt_b2", "pt_b3"), start=1):
            insert_part(env, part_id, job_id, index)
        for part_id, index, media in (("pt_b1", 1, MEDIA_ONE), ("pt_b3", 3, MEDIA_THREE)):
            scope = f"part:{part_id}"
            insert_step(env, job_id, scope, "01_download", "done",
                        started_at=T_STARTED, finished_at=T_FINISHED)
            await commit_step(env, job_id, scope, "01_download",
                              {"input/source.mp4": media}, part_index=index,
                              exec_id=f"exec_{part_id}")
        # P2 失败:留下半成品文件;同时放一个与 P1 字节相同的"完整"文件
        insert_step(env, job_id, "part:pt_b2", "01_download", "failed",
                    error="yt-dlp exited with code 1", retries=2,
                    started_at=T_STARTED, finished_at=T_FINISHED)
        await env.storage.write_file(job_id, "parts/pt_b2/input/source.mp4.part", b"partial")
        await env.storage.write_file(job_id, "parts/pt_b2/input/source.mp4", MEDIA_ONE)
        insert_step(env, job_id, "job", "09_merge_parts", "waiting")
        insert_ai_usage(env, "exec_ai_b2", job_id, "part:pt_b2::01_download")

        # 无 manifest 的完整 source.mp4 不是半成品命名,必须走 unknown 门(A1)
        with pytest.raises(BackupError, match="unknown storage paths"):
            await do_backup(env)
        allowlist = env.tmp / "approved.txt"
        allowlist.write_text(f"# reviewed\n{job_id}:parts/pt_b2/input/source.mp4\n")
        result = await do_backup(env, "run_ok", unknown_allowlist=allowlist)

        assert count_files(env.repo.root, "blobs") == 2
        assert env.repo.has_blob(sha(MEDIA_ONE)) and env.repo.has_blob(sha(MEDIA_THREE))
        assert not env.repo.has_blob(sha(b"partial"))
        assert result.stats["step_results"] == 2
        assert result.stats["failure_events"] == 1

        body = env.repo.get_snapshot(result.snapshot_digest)
        [failure_digest] = body["records"]["failures"]
        event = env.repo.get_record("failure_event", failure_digest)
        assert event["partial_outputs_discarded"] is True
        # 只有 .part 半成品进摘要,被审批放行的完整文件不冒充 partial
        assert [entry["path"] for entry in event["partial_outputs"]] == \
            ["input/source.mp4.part"]
        assert event["sanitized_message"] == "yt-dlp exited with code 1"
        assert event["attempt"] == 3
        assert event["ai_usage_refs"], "关联 ai_usage 审计必须挂上"
        for ref in event["ai_usage_refs"]:
            assert env.repo.get_record("ai_usage", ref)["exec_id"] == "exec_ai_b2"

    async def test_business_file_in_failed_scope_is_unknown_not_swallowed(self, env):
        """A1 复现:失败 scope 内的正常业务文件必须进 unknown,不得被吞成 partial。"""
        job_id = "job_swallow"
        insert_job(env, job_id)
        insert_part(env, "pt_w1", job_id, 1)
        insert_step(env, job_id, "part:pt_w1", "01_download", "failed",
                    error="boom", started_at=T_STARTED, finished_at=T_FINISHED)
        await env.storage.write_file(job_id, "parts/pt_w1/output/notes.md", b"# real notes")
        with pytest.raises(BackupError, match="unknown storage paths"):
            await do_backup(env)
        result = await do_backup(env, "run_allow", allow_unknown=True)
        assert result.report["jobs"][job_id]["unknown_paths"] == [
            "parts/pt_w1/output/notes.md",
        ]
        body = env.repo.get_snapshot(result.snapshot_digest)
        [failure_digest] = body["records"]["failures"]
        event = env.repo.get_record("failure_event", failure_digest)
        assert "partial_outputs" not in event

    async def test_runtime_sidecars_are_not_unknown(self, env):
        """A2 复现:.{step}.done/.meta 等生命周期 dotfile 不算未知业务产物。"""
        await seed_video_job(env)
        for part_id in ("pt_alpha1", "pt_alpha2"):
            for name in (".01_download.done", ".01_download.meta", ".01_download.progress"):
                await env.storage.write_file("job_alpha", f"parts/{part_id}/{name}", b"{}")
        await env.storage.write_file("job_alpha", ".09_merge_parts.done", b"{}")
        result = await do_backup(env)
        assert result.stats["unknown_paths"] == 0

    async def test_failure_event_identity_covers_all_fields(self, env):
        """A6:exec_id 与 record 内容一一对应,任一字段变化都得到新事件。"""
        job_id = "job_ident"
        insert_job(env, job_id)
        insert_part(env, "pt_i1", job_id, 1)
        insert_step(env, job_id, "part:pt_i1", "01_download", "failed",
                    error="boom", retries=1, pool="cpu",
                    started_at=T_STARTED, finished_at=T_FINISHED)
        first = await do_backup(env, "run_1")
        first_body = env.repo.get_snapshot(first.snapshot_digest)
        [digest_a] = first_body["records"]["failures"]
        event_a = env.repo.get_record("failure_event", digest_a)

        # 只改 retries:record digest 与 exec_id 都必须随之变化
        db_exec(env.db, "UPDATE job_steps SET retries=5 WHERE job_id=?", (job_id,))
        second = await do_backup(env, "run_2", ref="second")
        second_body = env.repo.get_snapshot(second.snapshot_digest)
        [digest_b] = second_body["records"]["failures"]
        event_b = env.repo.get_record("failure_event", digest_b)
        assert digest_a != digest_b
        assert event_a["exec_id"] != event_b["exec_id"]

    async def test_scope_partials_attach_to_last_failure_only(self, env):
        """B6:同 scope 多步失败时,残留清单只挂最后一次失败。"""
        job_id = "job_multi"
        insert_job(env, job_id)
        insert_part(env, "pt_m1", job_id, 1)
        insert_step(env, job_id, "part:pt_m1", "01_download", "failed",
                    error="first", started_at=T_STARTED,
                    finished_at="2026-07-18T06:20:00+00:00")
        insert_step(env, job_id, "part:pt_m1", "02_transcribe", "failed",
                    error="second", started_at=T_STARTED,
                    finished_at="2026-07-18T07:20:00+00:00")
        await env.storage.write_file(job_id, "parts/pt_m1/input/source.mp4.part", b"x")
        result = await do_backup(env)
        body = env.repo.get_snapshot(result.snapshot_digest)
        events = [
            env.repo.get_record("failure_event", digest)
            for digest in body["records"]["failures"]
        ]
        carriers = [event["step"] for event in events if "partial_outputs" in event]
        assert carriers == ["02_transcribe"]


class TestConsistencyProtocol:
    class ScriptedStorage:
        """代理 LocalStorage,按脚本替换特定 manifest 的 read_file 返回值。"""

        def __init__(self, inner, rel: str, script: list[bytes]):
            self._inner = inner
            self._rel = rel
            self._script = list(script)
            self.reads = 0

        def __getattr__(self, name):
            return getattr(self._inner, name)

        async def read_file(self, job_id, rel_path):
            if rel_path == self._rel:
                self.reads += 1
                if len(self._script) > 1:
                    return self._script.pop(0)
                return self._script[0]
            return await self._inner.read_file(job_id, rel_path)

        async def list_file_sizes(self, job_id):
            return await self._inner.list_file_sizes(job_id)

    async def test_manifest_replaced_midway_retries_to_stable(self, env):
        """§5.2.9:M1 != M2 触发重读,第二轮稳定后收取新 manifest。"""
        job_id = "job_race"
        insert_job(env, job_id)
        insert_part(env, "pt_r1", job_id, 1)
        scope = "part:pt_r1"
        insert_step(env, job_id, scope, "01_download", "done")
        old = await commit_step(env, job_id, scope, "01_download",
                                {"input/source.mp4": MEDIA_ONE},
                                part_index=1, exec_id="exec_old")
        new = build_manifest(job_id, scope, "01_download",
                             {"input/source.mp4": MEDIA_ONE},
                             part_index=1, exec_id="exec_new")
        rel = manifest_relative_path(scope, "01_download")
        old_raw, new_raw = json.dumps(old).encode(), json.dumps(new).encode()
        # attempt1: M1=old, confirm=new -> 重试; attempt2 起稳定为 new
        await ensure_job_json(env)
        storage = self.ScriptedStorage(env.storage, rel, [old_raw, new_raw])
        result = await run_backup(
            db_path=env.db, storage=storage, repository=env.repo,
            run_id="run_race", app_version="2.2.0", now_fn=make_clock(),
        )
        body = env.repo.get_snapshot(result.snapshot_digest)
        [sr_digest] = body["records"]["step_results"]
        record = env.repo.get_record("step_result", sr_digest)
        assert record["manifest"]["execution"]["exec_id"] == "exec_new"

    async def test_manifest_vanishing_midway_fails_closed(self, env):
        """B2:采集途中 manifest 消失是并发 rerun/delete,不能静默丢步。"""
        job_id = "job_vanish"
        insert_job(env, job_id)
        insert_part(env, "pt_v1", job_id, 1)
        scope = "part:pt_v1"
        insert_step(env, job_id, scope, "01_download", "done")
        manifest = await commit_step(env, job_id, scope, "01_download",
                                     {"input/source.mp4": MEDIA_ONE}, part_index=1)
        rel = manifest_relative_path(scope, "01_download")
        raw = json.dumps(manifest).encode()
        await ensure_job_json(env)
        storage = self.ScriptedStorage(env.storage, rel, [raw, None])
        with pytest.raises(BackupError, match="vanished during read"):
            await run_backup(
                db_path=env.db, storage=storage, repository=env.repo,
                run_id="run_vanish", app_version="2.2.0", now_fn=make_clock(),
            )

    async def test_missing_manifest_for_done_step_is_counted(self, env):
        """B2 覆盖率:DB 说 done 但从无 manifest 的步骤计入 missing,分母是终态步数。"""
        job_id = "job_cover"
        insert_job(env, job_id)
        insert_part(env, "pt_c1", job_id, 1)
        insert_step(env, job_id, "part:pt_c1", "01_download", "done")
        result = await do_backup(env)
        assert result.stats["manifests_missing"] == 1
        assert result.stats["terminal_steps"] == 1
        assert result.report["jobs"][job_id]["missing_manifests"] == [
            "part:pt_c1::01_download",
        ]

    async def test_endless_replacement_fails_closed(self, env):
        job_id = "job_flap"
        insert_job(env, job_id)
        insert_part(env, "pt_f1", job_id, 1)
        scope = "part:pt_f1"
        insert_step(env, job_id, scope, "01_download", "done")
        one = await commit_step(env, job_id, scope, "01_download",
                                {"input/source.mp4": MEDIA_ONE},
                                part_index=1, exec_id="exec_one")
        two = build_manifest(job_id, scope, "01_download",
                             {"input/source.mp4": MEDIA_ONE},
                             part_index=1, exec_id="exec_two")
        rel = manifest_relative_path(scope, "01_download")
        flip = [json.dumps(one).encode(), json.dumps(two).encode()] * 12
        await ensure_job_json(env)
        storage = self.ScriptedStorage(env.storage, rel, flip)
        with pytest.raises(BackupError, match="consistency retries exhausted"):
            await run_backup(
                db_path=env.db, storage=storage, repository=env.repo,
                run_id="run_flap", app_version="2.2.0", now_fn=make_clock(),
            )

    async def test_tampered_output_fails_closed(self, env):
        """§5.2.8:manifest 声明与实际字节不符,重试耗尽后整次失败。"""
        job_id = "job_tamper"
        insert_job(env, job_id)
        insert_part(env, "pt_t1", job_id, 1)
        scope = "part:pt_t1"
        insert_step(env, job_id, scope, "01_download", "done")
        await commit_step(env, job_id, scope, "01_download",
                          {"input/source.mp4": MEDIA_ONE}, part_index=1)
        await env.storage.write_file(job_id, "parts/pt_t1/input/source.mp4", b"EVIL-BYTES")
        with pytest.raises(BackupError, match="consistency retries exhausted"):
            await do_backup(env)


class TestSelectionGates:
    async def test_unknown_paths_fail_closed_then_allowed(self, env):
        """§5.2.23:非 manifest 产物且非失败残留的路径必须为 0,或显式放行。"""
        await seed_video_job(env)
        await env.storage.write_file("job_alpha", "notes/orphan.md", b"# stray")
        with pytest.raises(BackupError, match="unknown storage paths"):
            await do_backup(env)
        # refs 未被动过
        with pytest.raises(Exception):
            env.repo.get_ref("latest")
        result = await do_backup(env, "run_allow", allow_unknown=True)
        assert result.stats["unknown_paths"] == 1
        assert result.report["jobs"]["job_alpha"]["unknown_paths"] == ["notes/orphan.md"]
        snapshot = env.repo.get_snapshot(result.snapshot_digest)
        assert snapshot["completeness"]["portable_ready"] is False
        assert "unknown_artifacts_omitted" in snapshot["completeness"]["readiness_reasons"]

    async def test_alien_part_dir_is_unknown(self, env):
        await seed_video_job(env)
        await env.storage.write_file(
            "job_alpha", "parts/pt_alien/input/source.mp4", b"ALIEN",
        )
        with pytest.raises(BackupError, match="unknown storage paths"):
            await do_backup(env)

    async def test_part_index_gap_rejected(self, env):
        # v8 schema validator 在库级先拦(§2.7-2),编排层同名检查作纵深防御
        job_id = "job_gap"
        insert_job(env, job_id)
        insert_part(env, "pt_g1", job_id, 1)
        insert_part(env, "pt_g3", job_id, 3)
        with pytest.raises(BackupError, match="not contiguous|sequence broken"):
            await do_backup(env)

    async def test_job_json_part_list_mismatch_rejected(self, env):
        await seed_video_job(env)
        await env.storage.write_file(
            "job_alpha", "job.json",
            json.dumps({"parts": [{"part_id": "pt_wrong"}]}).encode(),
        )
        with pytest.raises(BackupError, match="job.json parts manifest disagrees"):
            await do_backup(env)

    async def test_non_deterministic_skip_excluded(self, env):
        job_id = "job_skip"
        insert_job(env, job_id)
        insert_part(env, "pt_s1", job_id, 1)
        scope = "part:pt_s1"
        insert_step(env, job_id, scope, "01_download", "done")
        await commit_step(env, job_id, scope, "01_download", {},
                          part_index=1, outcome="skipped",
                          skip_reason="capacity_probe")
        result = await do_backup(env)
        assert result.stats["step_results"] == 0
        assert result.stats["excluded_reasons"] == {"non_deterministic_skip": 1}

    async def test_deterministic_skip_collected(self, env):
        job_id = "job_dskip"
        insert_job(env, job_id)
        insert_part(env, "pt_d1", job_id, 1)
        scope = "part:pt_d1"
        insert_step(env, job_id, scope, "01_download", "done")
        await commit_step(env, job_id, scope, "01_download", {},
                          part_index=1, outcome="skipped", skip_reason="rule_false")
        result = await do_backup(env)
        assert result.stats["step_results"] == 1

    async def test_wrong_schema_version_rejected(self, env):
        db_exec(env.db, "PRAGMA user_version=7")
        with pytest.raises(BackupError, match="schema v7"):
            await do_backup(env)

    async def test_ai_task_log_without_exec_id_rejected(self, env):
        db_exec(env.db, (
            "INSERT INTO ai_task_logs (task_id, exec_id, created_at)"
            " VALUES ('task_x', NULL, ?)"
        ), (T_CREATED,))
        with pytest.raises(BackupError, match="exec_id is required"):
            await do_backup(env)


class TestRedactionAndExternalSources:
    async def test_file_urls_are_omitted_from_portable_records(self, env):
        host_path = str(env.tmp / "private" / "video.mp4")
        insert_job(env, "job_file", url=f"file://{host_path}")
        insert_part(env, "pt_file", "job_file", 1, source_url=f"file://{host_path}")
        result = await do_backup(env)
        body = env.repo.get_snapshot(result.snapshot_digest)
        job = next(
            env.repo.get_record("job_core", digest)
            for digest in body["records"]["jobs"]
            if env.repo.has_record("job_core", digest)
        )
        part = env.repo.get_record("part_core", body["records"]["parts"][0])
        assert "url" not in job
        assert "source_url" not in part
        assert host_path not in json.dumps(body, ensure_ascii=False)

    async def test_urls_redacted_in_records(self, env):
        job_id = "job_url"
        insert_job(
            env, job_id,
            url="https://cdn.example.com/v.mp4?id=1&X-Amz-Signature=" + "5" * 40,
        )
        insert_part(
            env, "pt_u1", job_id, 1,
            source_url="https://u:p@example.com/watch?v=1&sig=deadbeef1234",
        )
        result = await do_backup(env)
        body = env.repo.get_snapshot(result.snapshot_digest)
        record = next(
            env.repo.get_record("job_core", digest)
            for digest in body["records"]["jobs"]
            if env.repo.has_record("job_core", digest)
        )
        assert record["url"] == "https://cdn.example.com/v.mp4?id=1"
        [part_digest] = body["records"]["parts"]
        part = env.repo.get_record("part_core", part_digest)
        assert part["source_url"] == "https://example.com/watch?v=1"

    async def test_nas_source_ref_parts_counted(self, env):
        job_id = "job_nas"
        insert_job(env, job_id)
        insert_part(env, "pt_n1", job_id, 1, source_ref="nas://media-a/videos/lecture.mp4")
        scope = "part:pt_n1"
        insert_step(env, job_id, scope, "01_download", "done")
        # NAS 引用 Part:manifest 只有元数据输出,不含 source media
        await commit_step(env, job_id, scope, "01_download",
                          {"input/metadata.json": b"{}"}, part_index=1)
        result = await do_backup(env)
        assert result.stats["external_source_parts"] == 1
        assert result.stats["nas_source_roots"] == ["media-a"]
        assert result.stats["blobs_created"] == 1  # 只有 metadata blob
        snapshot = env.repo.get_snapshot(result.snapshot_digest)
        assert snapshot["completeness"]["media_self_contained"] is False
        assert snapshot["completeness"]["external_media_roots"] == ["media-a"]

    async def test_vendor_media_stores_verified_source_in_cas(self, env, monkeypatch):
        source_root = env.tmp / "source-media"
        source = source_root / "videos" / "lecture.mp4"
        source.parent.mkdir(parents=True)
        source.write_bytes(MEDIA_ONE)
        monkeypatch.setenv(
            "FLORI_SOURCE_ROOTS_JSON", json.dumps({"media-a": str(source_root)}),
        )
        insert_job(env, "job_vendor")
        insert_part(
            env, "pt_vendor", "job_vendor", 1,
            source_ref="nas://media-a/videos/lecture.mp4",
            source_digest=sha(MEDIA_ONE), size_bytes=len(MEDIA_ONE),
        )
        result = await do_backup(env, vendor_media=True)
        snapshot = env.repo.get_snapshot(result.snapshot_digest)
        part = env.repo.get_record("part_core", snapshot["records"]["parts"][0])
        assert part["source_blob"] == sha(MEDIA_ONE)
        assert env.repo.read_blob(part["source_blob"]) == MEDIA_ONE
        assert snapshot["completeness"]["media_self_contained"] is True
        assert snapshot["completeness"]["external_media_roots"] == []

    async def test_invalid_source_ref_rejected(self, env):
        job_id = "job_badref"
        insert_job(env, job_id)
        insert_part(env, "pt_x1", job_id, 1, source_ref="nas://../escape.mp4")
        with pytest.raises(BackupError, match="invalid source_ref"):
            await do_backup(env)


class TestFailureIsolation:
    async def test_refs_untouched_and_failure_receipt_on_error(self, env):
        await seed_video_job(env)
        first = await do_backup(env, "run_ok")
        await env.storage.write_file("job_alpha", "notes/orphan.md", b"stray")
        snapshots_before = count_files(env.repo.root, "snapshots")
        with pytest.raises(BackupError):
            await do_backup(env, "run_bad")
        assert env.repo.get_ref("latest") == first.snapshot_digest
        assert count_files(env.repo.root, "snapshots") == snapshots_before
        failed = [
            body for _rid, body in env.repo.find_receipts("run_bad")
            if body["outcome"] == "failed"
        ]
        assert len(failed) == 1 and "unknown storage paths" in failed[0]["error"]
        # 失败后同 run_id 重试(修复问题后)仍可成功
        await env.storage.delete_file("job_alpha", "notes/orphan.md")
        retried = await do_backup(env, "run_bad")
        assert retried.snapshot_digest == first.snapshot_digest


class TestPartialSnapshots:
    async def test_job_filter_must_not_write_latest(self, env):
        """A5 复现:局部快照不代表系统全貌,禁止覆盖默认 latest。"""
        await seed_video_job(env)
        with pytest.raises(BackupError, match="must not write the default 'latest'"):
            await do_backup(env, job_ids=["job_alpha"])

    async def test_partial_snapshot_is_marked_and_distinct(self, env):
        await seed_video_job(env)
        insert_job(env, "job_solo", content_type="document", document_kind="article")
        full = await do_backup(env, "run_full")
        partial = await do_backup(
            env, "run_partial", job_ids=["job_alpha"], ref="only-alpha",
        )
        assert partial.snapshot_digest != full.snapshot_digest
        assert partial.stats["partial_snapshot"] is True
        body = env.repo.get_snapshot(partial.snapshot_digest)
        assert body["selector"] == {"partial": True, "job_ids": ["job_alpha"]}
        assert env.repo.get_ref("latest") == full.snapshot_digest
        assert env.repo.get_ref("only-alpha") == partial.snapshot_digest
        # 全量快照的 selector 必须是空集形态
        assert env.repo.get_snapshot(full.snapshot_digest)["selector"] == {
            "partial": False, "job_ids": [],
        }

    async def test_invalid_ref_rejected_before_lock(self, env):
        with pytest.raises(BackupError, match="ref name"):
            await do_backup(env, ref="../escape")
        assert env.repo.write_lock_holder() is None


class TestRunIdRecovery:
    async def test_successful_run_id_is_bound_to_canonical_request(self, env):
        await seed_video_job(env)
        await do_backup(env, "run_bound", ref="named")
        with pytest.raises(BackupError, match="identical canonical request"):
            await do_backup(env, "run_bound", ref="named", full_rehash=True)

    async def test_reused_run_requires_ref_in_place(self, env):
        """B1 复现:上次成功但 ref 没落位时,同 run_id 重跑必须补设而非空转。"""
        await seed_video_job(env)
        first = await do_backup(env, "run_x", ref="monthly-2026-07")
        env.repo.delete_ref("monthly-2026-07")
        again = await do_backup(env, "run_x", ref="monthly-2026-07")
        assert again.reused_run is False
        assert env.repo.get_ref("monthly-2026-07") == first.snapshot_digest

    async def test_reused_run_short_circuits_when_ref_matches(self, env):
        await seed_video_job(env)
        first = await do_backup(env, "run_y", ref="named")
        again = await do_backup(env, "run_y", ref="named")
        assert again.reused_run is True
        assert again.snapshot_digest == first.snapshot_digest

    async def test_in_progress_receipt_written(self, env):
        """B4:三态 receipt——拿锁后先落 in_progress,终态另写一条。"""
        insert_job(env, "job_ip", content_type="document", document_kind="article")
        await do_backup(env, "run_ip")
        outcomes = [body["outcome"] for _rid, body in env.repo.find_receipts("run_ip")]
        assert outcomes == ["in_progress", "success"]

    async def test_failed_run_leaves_in_progress_then_failed(self, env):
        await seed_video_job(env)
        await env.storage.write_file("job_alpha", "notes/orphan.md", b"stray")
        with pytest.raises(BackupError):
            await do_backup(env, "run_bad")
        outcomes = [body["outcome"] for _rid, body in env.repo.find_receipts("run_bad")]
        assert outcomes == ["in_progress", "failed"]


class TestIncrementalRehash:
    async def test_second_run_skips_byte_reads(self, env):
        """C2 复现:record + blob 齐备时走内容寻址增量,不再重读产物字节。"""
        await seed_video_job(env)
        first = await do_backup(env, "run_1")
        assert first.stats["step_results_incremental"] == 0
        assert first.stats["blob_bytes_rehashed"] > 0

        second = await do_backup(env, "run_2")
        assert second.snapshot_digest == first.snapshot_digest
        assert second.stats["step_results_incremental"] == 2
        assert second.stats["blob_bytes_rehashed"] == 0

        third = await do_backup(env, "run_3", full_rehash=True)
        assert third.stats["step_results_incremental"] == 0
        assert third.stats["blob_bytes_rehashed"] == first.stats["blob_bytes_rehashed"]

    async def test_missing_blob_forces_full_path(self, env):
        await seed_video_job(env)
        await do_backup(env, "run_1")
        # 仓库里 blob 被清掉:增量判定必须回落到重读并重新发布
        import os

        os.unlink(env.repo.blob_path(sha(MEDIA_ONE)))
        second = await do_backup(env, "run_2")
        assert second.stats["step_results_incremental"] == 1
        assert second.stats["blobs_created"] == 1
        assert env.repo.has_blob(sha(MEDIA_ONE))

    async def test_incremental_still_rehashes_every_text_output(self, env):
        insert_job(env, "job_text", content_type="document", document_kind="article")
        insert_step(
            env, "job_text", "job", "02_parse", "done",
            started_at=T_STARTED, finished_at=T_FINISHED,
        )
        text = b"# durable note\ntext is rescanned on every backup\n"
        await commit_step(
            env, "job_text", "job", "02_parse", {"output/note.md": text},
        )
        first = await do_backup(env, "run_text_1")
        second = await do_backup(env, "run_text_2")
        assert first.stats["blob_bytes_rehashed"] == len(text)
        assert second.stats["step_results_incremental"] == 1
        assert second.stats["blob_bytes_rehashed"] == len(text)


class TestRelationRecords:
    async def test_per_job_relation_record(self, env):
        """C4:每 Job 一条 relation record,P3 可按 Job diff 定位冲突。"""
        insert_collection(env, "col_1")
        await seed_video_job(env)
        db_exec(env.db, "UPDATE jobs SET collection_id='col_1' WHERE id='job_alpha'")
        result = await do_backup(env)
        body = env.repo.get_snapshot(result.snapshot_digest)
        relations = [
            env.repo.get_record("job_relation", digest)
            for digest in body["records"]["jobs"]
            if env.repo.has_record("job_relation", digest)
        ]
        assert len(relations) == 1
        relation = relations[0]
        assert relation["job_id"] == "job_alpha"
        assert len(relation["parts"]) == 2
        assert set(relation["step_results"]) == {
            "part:pt_alpha1::01_download", "part:pt_alpha2::01_download",
        }
        assert "user_state" in relation
        # relation 的每条边都必须落在快照对应分组内(闭包已在 put_snapshot 验过)
        assert relation["core"] in body["records"]["jobs"]
        for digest in relation["parts"]:
            assert digest in body["records"]["parts"]

    async def test_dangling_relation_edge_rejected(self, env):
        """job_relation 的悬空引用由 P1 闭包 fail-closed。"""
        from shared.content_repository import RepositoryError

        job = env.repo.put_record("job_core", {
            "id": "job_x", "content_type": "video", "pipeline": "p",
            "created_at": "2026-07-18T00:00:00Z",
        })
        relation = env.repo.put_record("job_relation", {
            "job_id": "job_x", "core": job.digest, "parts": [HEX_A],
            "step_results": {}, "failures": [],
        })
        from tests.test_content_repository import build_snapshot

        with pytest.raises(RepositoryError, match="not listed in records.parts"):
            env.repo.put_snapshot(build_snapshot(jobs=[job.digest, relation.digest]))


class TestTimestampsAndRedaction:
    async def test_naive_timestamp_normalized_not_fatal(self, env):
        """A3 复现:旧库 naive 串按 db._parse_dt 约定补 UTC,不中止全备。"""
        job_id = "job_naive"
        insert_job(env, job_id)
        insert_part(env, "pt_n1", job_id, 1)
        insert_step(env, job_id, "part:pt_n1", "01_download", "failed",
                    error="boom", started_at="2026-07-18 06:10:00",
                    finished_at="2026-07-18 06:20:00")
        result = await do_backup(env)
        assert result.report["normalized_naive_timestamps"] == 2
        body = env.repo.get_snapshot(result.snapshot_digest)
        [digest] = body["records"]["failures"]
        event = env.repo.get_record("failure_event", digest)
        assert event["failed_at"] == "2026-07-18T06:20:00+00:00"

    async def test_unparsable_timestamp_skips_event_only(self, env):
        job_id = "job_badts"
        insert_job(env, job_id)
        insert_part(env, "pt_b1", job_id, 1)
        insert_step(env, job_id, "part:pt_b1", "01_download", "failed",
                    error="boom", started_at="not-a-time", finished_at="also-bad")
        result = await do_backup(env)
        assert result.stats["failure_events"] == 0
        assert result.report["jobs"][job_id]["failure_rows_skipped"] == [
            {"scope_key": "part:pt_b1", "step": "01_download",
             "reason": "unparsable_timestamp"},
        ]

    async def test_embedded_url_in_meta_redacted(self, env):
        """A4 复现:meta 里嵌的带 auth_token URL 必须脱敏,不得原样入库。"""
        job_id = "job_meta"
        insert_job(env, job_id, content_type="document", document_kind="article")
        db_exec(env.db, "UPDATE jobs SET meta=? WHERE id=?", (
            json.dumps({
                "origin": "https://cdn.example.com/v.mp4?auth_token=deadbeef01&id=7",
                "nested": {"cover": "https://img.example.com/c.jpg?sig=abcdef123456"},
            }),
            job_id,
        ))
        result = await do_backup(env)
        body = env.repo.get_snapshot(result.snapshot_digest)
        core = next(
            env.repo.get_record("job_core", digest)
            for digest in body["records"]["jobs"]
            if env.repo.has_record("job_core", digest)
        )
        assert core["meta"]["origin"] == "https://cdn.example.com/v.mp4?id=7"
        assert core["meta"]["nested"]["cover"] == "https://img.example.com/c.jpg"
        assert result.report["url_redactions"]["job_core"]

    async def test_collection_source_id_redacted_by_type(self, env):
        db_exec(env.db, (
            "INSERT INTO collections (id, name, domain, source_type, source_id,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?,?)"
        ), (
            "col_rss", "订阅", "general", "rss",
            "https://feeds.example.com/f.xml?api_key=abcdef123456",
            T_CREATED, T_CREATED,
        ))
        result = await do_backup(env)
        body = env.repo.get_snapshot(result.snapshot_digest)
        collection = next(
            env.repo.get_record("collection", digest)
            for digest in body["records"]["business_ledgers"]
            if env.repo.has_record("collection", digest)
        )
        assert collection["source_id"] == "https://feeds.example.com/f.xml"


class TestReportingExtras:
    async def test_global_user_configs_are_collected_from_explicit_root(self, env):
        root = env.tmp / "prompts"
        (root / "profiles").mkdir(parents=True)
        (root / "styles").mkdir()
        (root / "templates").mkdir()
        (root / "hot.md").write_text("当前 prompt\n", encoding="utf-8")
        (root / "profiles" / "fast.yaml").write_text("model: fast\n", encoding="utf-8")
        result = await do_backup(env, user_config_dir=root)
        snapshot = env.repo.get_snapshot(result.snapshot_digest)
        records = [
            env.repo.get_record("user_config", digest)
            for digest in snapshot["records"]["business_ledgers"]
            if env.repo.has_record("user_config", digest)
        ]
        assert {(item["path"], item["kind"]) for item in records} == {
            ("prompts/hot.md", "prompts"),
            ("prompts/profiles/fast.yaml", "profiles"),
        }
        assert snapshot["completeness"]["user_config_complete"] is True

    async def test_unknown_user_config_path_fails_closed(self, env):
        root = env.tmp / "prompts"
        root.mkdir()
        (root / "credentials.json").write_text("{}", encoding="utf-8")
        with pytest.raises(BackupError, match="unknown user config path"):
            await do_backup(env, user_config_dir=root)

    async def test_job_json_ai_override_flagged(self, env):
        """job.json 只归档 AI 配置子集,不复制整个 runtime sidecar。"""
        part_ids = await seed_video_job(env)
        await write_job_json(
            env, "job_alpha", part_ids,
            ai_overrides={"05_vision": "claude"},
            prompt_overrides={"06_summary": {"version": 3, "content": "hot"}},
        )
        result = await do_backup(env)
        assert result.report["jobs_with_job_ai_config"] == ["job_alpha"]
        snapshot = env.repo.get_snapshot(result.snapshot_digest)
        configs = [
            env.repo.get_record("user_config", digest)
            for digest in snapshot["records"]["business_ledgers"]
            if env.repo.has_record("user_config", digest)
        ]
        config = next(item for item in configs if item["kind"] == "job_ai_config")
        assert config["path"] == "jobs/job_alpha/ai-config.json"
        assert json.loads(env.repo.read_blob(config["blob"])) == {
            "ai_overrides": {"05_vision": "claude"},
            "prompt_overrides": {
                "06_summary": {"content": "hot", "version": 3},
            },
        }

    async def test_secret_split_across_storage_chunk_is_rejected(self, env):
        """签名参数跨 1 MiB chunk 边界也不能逃过完整扫描。"""
        job_id = "job_big"
        insert_job(env, job_id, content_type="document", document_kind="article")
        insert_step(
            env, job_id, "job", "01_download", "done",
            started_at=T_STARTED, finished_at=T_FINISHED,
        )
        prefix = b"n" * (1024 * 1024 - len(b"https://x.example/v?sig="))
        data = prefix + b"https://x.example/v?sig=" + b"abcdef123456"
        await commit_step(env, job_id, "job", "01_download", {"output/notes.md": data})
        with pytest.raises(BackupError, match="contains a secret-shaped value"):
            await do_backup(env)

    async def test_secret_after_four_megabytes_is_rejected(self, env):
        job_id = "job_tail"
        insert_job(env, job_id, content_type="document", document_kind="article")
        insert_step(env, job_id, "job", "01_download", "done")
        data = b"n" * (4 * 1024 * 1024 + 37) + b"?auth_token=abcdef123456"
        await commit_step(env, job_id, "job", "01_download", {"output/notes.md": data})
        with pytest.raises(BackupError, match="contains a secret-shaped value"):
            await do_backup(env)

    async def test_fully_scanned_blob_is_not_reported_as_truncated(self, env):
        await seed_video_job(env)
        result = await do_backup(env)
        assert "blob_scans_truncated" not in result.report
        assert result.stats["blob_scans_truncated"] == 0

    async def test_unknown_candidates_listed_for_approval(self, env):
        """C3:失败时报告直接给出可粘贴的候选清单。"""
        await seed_video_job(env)
        await env.storage.write_file("job_alpha", "notes/a.md", b"x")
        await env.storage.write_file("job_alpha", "notes/b.md", b"y")
        with pytest.raises(BackupError, match="--allow-unknown-file"):
            await do_backup(env)
        allowlist = env.tmp / "approved.txt"
        allowlist.write_text("job_alpha:notes/a.md\njob_alpha:notes/b.md\n")
        result = await do_backup(env, "run_ok", unknown_allowlist=allowlist)
        assert result.stats["unknown_paths"] == 2
        assert result.report["jobs"]["job_alpha"]["approved_unknown_paths"] == [
            "notes/a.md", "notes/b.md",
        ]
        snapshot = env.repo.get_snapshot(result.snapshot_digest)
        assert snapshot["completeness"]["portable_ready"] is False
        assert "unknown_artifacts_omitted" in snapshot["completeness"]["readiness_reasons"]

    async def test_allowlist_entry_is_exact_match(self, env):
        await seed_video_job(env)
        await env.storage.write_file("job_alpha", "notes/a.md", b"x")
        allowlist = env.tmp / "approved.txt"
        allowlist.write_text("job_other:notes/a.md\n")
        with pytest.raises(BackupError, match="unknown storage paths"):
            await do_backup(env, unknown_allowlist=allowlist)

    async def test_malformed_allowlist_rejected(self, env):
        allowlist = env.tmp / "bad.txt"
        allowlist.write_text("no-separator-here\n")
        with pytest.raises(BackupError, match="must be"):
            await do_backup(env, unknown_allowlist=allowlist)


class TestLegacyArchiveChunking:
    async def test_large_legacy_table_is_chunked(self, env, monkeypatch):
        """C5:超限 legacy 归档分片而非中止整次备份。"""
        import shared.content_backup as module

        monkeypatch.setattr(module, "LEGACY_ARCHIVE_CHUNK_ROWS", 2)
        # 形状必须与 v0001 的 LEGACY_PRESERVED_TABLES 冻结 DDL 逐字一致,
        # 否则 schema validator 先于备份逻辑拒绝。
        db_exec(env.db, (
            "CREATE TABLE glossary_bak_clean_20260617("
            "domain TEXT, term TEXT, definition TEXT, related TEXT, status TEXT, "
            "created_at TEXT, updated_at TEXT, occurrences TEXT, is_topic INT, "
            "definition_locked INT)"
        ))
        for index in range(5):
            db_exec(env.db, (
                "INSERT INTO glossary_bak_clean_20260617 (domain, term, definition)"
                " VALUES (?,?,?)"
            ), ("general", f"term_{index}", f"定义 {index}"))
        result = await do_backup(env)
        body = env.repo.get_snapshot(result.snapshot_digest)
        chunks = [
            env.repo.get_record("legacy_archive", digest)
            for digest in body["records"]["business_ledgers"]
            if env.repo.has_record("legacy_archive", digest)
        ]
        assert len(chunks) == 3
        assert {chunk["chunk_total"] for chunk in chunks} == {3}
        assert sorted(chunk["chunk_index"] for chunk in chunks) == [0, 1, 2]
        assert sum(len(chunk["rows"]) for chunk in chunks) == 5


class TestCli:
    def test_backup_and_verify_roundtrip(self, env, tmp_path, monkeypatch):
        monkeypatch.delenv("MINIO_URL", raising=False)
        # 非 video 类型:v8 validator 要求 video job 必有 Part
        insert_job(env, "job_cli", content_type="document", document_kind="article")
        result_file = tmp_path / "out" / "result.json"
        code = main([
            "backup",
            "--repo", str(env.repo.root),
            "--db", str(env.db),
            "--jobs-dir", str(env.jobs_dir),
            "--run-id", "run_cli",
            "--app-version", "2.2.0",
            "--result-file", str(result_file),
        ])
        assert code == 0
        payload = json.loads(result_file.read_text())
        assert payload["ok"] is True
        assert payload["stats"]["jobs"] == 1
        assert payload["snapshot_digest"].startswith("sha256:")

        verify_file = tmp_path / "out" / "verify.json"
        code = main([
            "verify", "--repo", str(env.repo.root),
            "--result-file", str(verify_file),
        ])
        assert code == 0
        verdict = json.loads(verify_file.read_text())
        assert verdict["ok"] is True and verdict["issues"] == []
        # C8:--verify 只证仓库自洽,payload 必须如实声明边界
        assert "does NOT prove" in verdict["scope"]

    def test_cli_rejects_partial_snapshot_to_latest(self, env, tmp_path, monkeypatch):
        """A5 复现(CLI 侧):--job 搭 --ref latest 直接报错。"""
        monkeypatch.delenv("MINIO_URL", raising=False)
        result_file = tmp_path / "partial.json"
        code = main([
            "backup",
            "--repo", str(env.repo.root),
            "--db", str(env.db),
            "--jobs-dir", str(env.jobs_dir),
            "--job", "job_any",
            "--result-file", str(result_file),
        ])
        assert code == 2
        payload = json.loads(result_file.read_text())
        assert payload["ok"] is False and "explicit --ref" in payload["error"]

    def test_cli_reports_missing_database(self, env, tmp_path, monkeypatch):
        """C1:存在性 preflight 在容器内做,给出机器可读错误而不是栈回溯。"""
        monkeypatch.delenv("MINIO_URL", raising=False)
        result_file = tmp_path / "nodb.json"
        code = main([
            "backup",
            "--repo", str(env.repo.root),
            "--db", str(tmp_path / "missing.db"),
            "--jobs-dir", str(env.jobs_dir),
            "--result-file", str(result_file),
        ])
        assert code == 1
        assert "database not found" in json.loads(result_file.read_text())["error"]

    def test_cli_reports_failure(self, env, tmp_path, monkeypatch):
        monkeypatch.delenv("MINIO_URL", raising=False)
        db_exec(env.db, "PRAGMA user_version=7")
        result_file = tmp_path / "fail.json"
        code = main([
            "backup",
            "--repo", str(env.repo.root),
            "--db", str(env.db),
            "--jobs-dir", str(env.jobs_dir),
            "--run-id", "run_fail",
            "--result-file", str(result_file),
        ])
        assert code == 1
        payload = json.loads(result_file.read_text())
        assert payload["ok"] is False and "schema v7" in payload["error"]
