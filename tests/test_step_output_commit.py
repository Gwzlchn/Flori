"""step 输出提交协议测试:输出展开、commit fence、staging→promote→manifest-last 与三后端等价。

覆盖设计稿 §5.2 条 3(逐故障点注入)、条 4(旧 exec 新 generation 后无法 promote/发布)、
条 7(Part 并发隔离)、条 11(失败只发布诊断)、条 14(Local/Remote/Gateway 协议等价)。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from tests.conftest import make_fakeredis
from api.main import create_app
from shared.config import AppConfig
from shared.models import Step, StepStatus
from shared.step_manifest import manifest_relative_path, validate_manifest
from shared.step_output_commit import (
    StepOutput,
    StepOutputError,
    build_candidate_record,
    build_commit_record,
    build_step_manifest,
    candidate_filename,
    collect_step_outputs,
    diagnostics_globs,
    expand_step_outputs,
    load_candidate_record,
    stale_output_paths,
)
from shared.step_scope import execution_step_key, part_scope
from shared.storage import (
    LocalStorage,
    RemoteStorage,
    StepCommitFenceRejected,
    StepCommitIntegrityError,
)
from tests.current_schema_db import clone_current_schema_database
from tests.test_worker import (
    activate_claim,
    lifecycle_payloads,
    make_claim,
    make_job,
)
from worker.transport import RedisTransport
from worker.worker import Worker


# 纯逻辑:输出展开与 candidate


def _write(root: Path, rel: str, data: bytes = b"x") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


class TestExpandStepOutputs:
    def test_fnmatch_semantics_and_utf8_sort(self, tmp_path):
        _write(tmp_path, "assets/nested/frame.png")
        _write(tmp_path, "assets/a.png")
        _write(tmp_path, "output/notes.md")
        _write(tmp_path, "logs/A.log")
        result = expand_step_outputs(
            tmp_path, ["assets/*", "output/notes.md"], scope_key="job",
        )
        # fnmatch 的 * 跨 '/'(与前端分组/provenance 校验一致),结果按 UTF-8 升序。
        assert result == ["assets/a.png", "assets/nested/frame.png", "output/notes.md"]

    def test_symlink_rejected_unless_excluded(self, tmp_path):
        target = _write(tmp_path, "real.bin")
        link = tmp_path / "input" / "source.mp4"
        link.parent.mkdir(parents=True)
        link.symlink_to(target)
        with pytest.raises(StepOutputError, match="symlink"):
            expand_step_outputs(tmp_path, ["input/*"], scope_key="job")
        assert expand_step_outputs(
            tmp_path, ["input/*"], scope_key="job",
            exclude_paths={"input/source.mp4"},
        ) == []

    def test_credential_sidecar_rejected(self, tmp_path):
        _write(tmp_path, "input/.credentials.json")
        with pytest.raises(StepOutputError, match="credential"):
            expand_step_outputs(tmp_path, ["input/*"], scope_key="job")

    def test_job_scope_skips_part_territory_and_internal(self, tmp_path):
        _write(tmp_path, "parts/pt_x/output/a.md")
        _write(tmp_path, ".flori/steps/A/manifest.json")
        _write(tmp_path, ".A.done")
        assert expand_step_outputs(tmp_path, ["*"], scope_key="job") == []

    def test_collect_path_filter_exempts_no_push_sources(self, tmp_path):
        # gateway NO_PUSH 源文件中心无副本:manifest 不声明,也不为其付哈希成本。
        _write(tmp_path, "input/source.mp4", b"big video")
        _write(tmp_path, "input/metadata.json", b"{}")
        outputs = collect_step_outputs(
            tmp_path, ["input/*"], scope_key=part_scope("pt_a"),
            path_filter=lambda job_rel: not job_rel.endswith("source.mp4"),
        )
        assert [o.job_rel for o in outputs] == ["parts/pt_a/input/metadata.json"]

    def test_collect_hashes_stream(self, tmp_path):
        _write(tmp_path, "out/a.json", b"hello")
        outputs = collect_step_outputs(
            tmp_path, ["out/*"], scope_key=part_scope("pt_a"),
        )
        assert outputs == [StepOutput(
            path="out/a.json", job_rel="parts/pt_a/out/a.json",
            size_bytes=5,
            sha256=f"sha256:{hashlib.sha256(b'hello').hexdigest()}",
            media_type="application/json",
        )]


class TestCandidateRecord:
    def test_roundtrip(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STEP_EXEC_ID", "w:1")
        record = build_candidate_record("A", {"input": "sha256:" + "0" * 64})
        (tmp_path / candidate_filename("A")).write_text(json.dumps(record))
        loaded = load_candidate_record(tmp_path, "A")
        assert loaded is not None
        assert loaded["exec_id"] == "w:1"
        assert loaded["input_fingerprints"] == {"input": "sha256:" + "0" * 64}

    def test_corrupt_or_mismatched_returns_none(self, tmp_path):
        assert load_candidate_record(tmp_path, "A") is None
        (tmp_path / candidate_filename("A")).write_text("not json")
        assert load_candidate_record(tmp_path, "A") is None
        record = build_candidate_record("B", {})
        (tmp_path / candidate_filename("A")).write_text(json.dumps(record))
        assert load_candidate_record(tmp_path, "A") is None

    def test_secret_fingerprint_fails_closed(self):
        with pytest.raises(Exception):
            build_candidate_record("A", {"api_key": "sk-" + "a" * 24})


_SHA = "sha256:" + "f" * 64

# 真实 pipeline 步骤 input_hashes 的逐字段形态(含空串值语义"该输入不存在"):
# 空串曾被 validate_input_fingerprints fail-closed 拒绝(P0 审查发现),会让
# mark_done() 之后的候选采集抛错、把已成功步骤打失败。此矩阵按实现逐步锁形态。
_REAL_STEP_FINGERPRINTS = {
    # steps/video/step_11_smart.py:28 — evidence=""(非案例类)、provider=""(无覆盖)
    "video_11_smart": {
        "mechanical": _SHA, "prompt": _SHA, "template": _SHA, "profile": _SHA,
        "styles": "{}", "template_vision": _SHA, "evidence": "",
        "source_segments": _SHA, "provider": "",
    },
    # steps/video/step_12_review.py:24 — smart=""/evidence=""/provider=""
    "video_12_review": {
        "smart": "", "mechanical": _SHA, "evidence": "", "provider": "",
        "template": _SHA,
    },
    # steps/document/step_08_review.py:20
    "document_08_review": {
        "smart": "", "document": _SHA, "quality": _SHA, "provider": "",
        "template": _SHA,
    },
    # steps/audio/step_05_review.py:20
    "audio_05_review": {
        "smart": "", "transcript": _SHA, "provider": "", "template": _SHA,
    },
    # steps/video/step_evidence.py:40 — 非案例类恒 {"skip": "non-case"};案例类 mechanical 可空
    "video_10_evidence_noncase": {"skip": "non-case"},
    "video_10_evidence_case": {"mechanical": "", "provider": "", "template": _SHA},
    # steps/video/step_08_punctuate.py:29 — 无字幕返回 {};有字幕以文件名作键
    "video_08_punctuate_empty": {},
    "video_08_punctuate": {
        "subtitle.srt": _SHA, "mode": "zh", "metadata": _SHA,
        "source_media": _SHA, "ocr": _SHA, "template": _SHA,
    },
    # steps/common/step_concepts.py:114 — 哨兵字符串值(none/missing)与相对路径值
    "common_concepts": {
        "source": "note", "source_hash": _SHA,
        "source_path": "output/versions/notes_smart_20260718.md",
        "evidence_note_type": "none", "source_manifest_hash": "missing",
        "provenance_hash": "missing", "prompt": _SHA, "styles": "{}",
    },
    # steps/common/step_01_download.py:65
    "common_01_download": {"job": _SHA},
}


class TestRealStepFingerprintShapes:
    """P0 回归:真实步骤 input_hashes 形态必须能通过候选采集与 input_digest 全链路。"""

    @pytest.mark.parametrize(
        "name", sorted(_REAL_STEP_FINGERPRINTS), ids=sorted(_REAL_STEP_FINGERPRINTS),
    )
    def test_candidate_roundtrip_accepts_real_shapes(self, tmp_path, name):
        from shared.step_manifest import compute_input_digest

        fingerprints = _REAL_STEP_FINGERPRINTS[name]
        record = build_candidate_record("A", fingerprints)
        (tmp_path / candidate_filename("A")).write_text(json.dumps(record))
        loaded = load_candidate_record(tmp_path, "A")
        assert loaded is not None
        assert loaded["input_fingerprints"] == fingerprints
        # 空串参与摘要且与非空区分(与 .done input_hashes 比较语义一致)。
        digest = compute_input_digest(loaded["input_fingerprints"])
        assert digest.startswith("sha256:")
        if any(value == "" for value in fingerprints.values()):
            mutated = {
                key: (_SHA if value == "" else value)
                for key, value in fingerprints.items()
            }
            assert compute_input_digest(mutated) != digest

    def test_manifest_accepts_empty_fingerprint_values(self):
        outputs = [StepOutput("out/a.json", "out/a.json", 1, DIGEST, None)]
        manifest, _bytes, _digest = build_step_manifest(
            job_id="j1", scope_key="job", step="12_review", part_index=None,
            exec_id="w:1", job_generation=1, attempt=1,
            started_at="2026-07-18T00:00:00Z", committed_at="2026-07-18T00:01:00Z",
            duration_sec=1.0,
            input_fingerprints=_REAL_STEP_FINGERPRINTS["video_12_review"],
            definition_digest="sha256:" + "c" * 64,
            outputs=outputs,
            producer={
                "flori_version": "2.1.1", "build_sha": None, "worker_id": "w",
                "runner": "subprocess", "image": "flori/step-base",
                "image_digest": None, "tool_versions": {},
            },
        )
        validate_manifest(manifest)


# commit fence(fakeredis;§5.2 条 4)


DIGEST = "sha256:" + "a" * 64


async def _activate_execution(
    redis, job_id="j1", step="A", exec_id="w:1", worker="w", generation=1,
):
    await redis.init_job(job_id, "test", {})
    await redis.r.hset(f"job:{job_id}", "lifecycle_generation", str(generation))
    await redis.set_step_status(job_id, step, "running")
    await redis.set_step_worker(job_id, step, worker)
    await redis.set_step_exec_id(job_id, step, exec_id)
    await redis.r.hset(f"job:{job_id}:step_generation", step, str(generation))
    await redis.create_task_lease(worker, job_id, step, exec_id, "cpu")


@pytest.fixture
async def fence_redis():
    client = make_fakeredis()
    yield client
    await client.close()


class TestCommitFence:
    @pytest.mark.asyncio
    async def test_begin_issues_one_time_token(self, fence_redis):
        await _activate_execution(fence_redis)
        token, reason = await fence_redis.begin_step_commit(
            job_id="j1", step="A", exec_id="w:1", generation=1,
            candidate_digest=DIGEST, worker_id="w",
        )
        assert reason == "issued" and token is not None
        assert token["exec_id"] == "w:1" and token["candidate_digest"] == DIGEST
        assert await fence_redis.validate_step_commit("j1", "A", token)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("mutate", "reason"),
        [
            (lambda r: r.r.hset("job:j1", "lifecycle_generation", "2"), "stale_generation"),
            (lambda r: r.set_step_exec_id("j1", "A", "w:other"), "stale_exec"),
            (lambda r: r.set_step_status("j1", "A", "done"), "not_running"),
            (lambda r: r.revoke_task_lease("w:1"), "lease_invalid"),
        ],
    )
    async def test_begin_rejects_stale_execution(self, fence_redis, mutate, reason):
        await _activate_execution(fence_redis)
        await mutate(fence_redis)
        token, got = await fence_redis.begin_step_commit(
            job_id="j1", step="A", exec_id="w:1", generation=1,
            candidate_digest=DIGEST, worker_id="w",
        )
        assert token is None and got == reason

    @pytest.mark.asyncio
    async def test_old_exec_cannot_promote_or_publish_after_new_generation(
        self, fence_redis,
    ):
        # §5.2 条 4:token 签发后 rerun 换代,promote 前校验与 manifest 发布全部拒绝。
        await _activate_execution(fence_redis)
        token, _ = await fence_redis.begin_step_commit(
            job_id="j1", step="A", exec_id="w:1", generation=1,
            candidate_digest=DIGEST, worker_id="w",
        )
        await fence_redis.r.hset("job:j1", "lifecycle_generation", "2")
        assert not await fence_redis.validate_step_commit("j1", "A", token)
        assert not await fence_redis.validate_step_commit(
            "j1", "A", token, phase="manifest_published",
        )
        assert not await fence_redis.finish_step_commit("j1", "A", token)

    @pytest.mark.asyncio
    async def test_token_rotation_invalidates_old(self, fence_redis):
        await _activate_execution(fence_redis)
        first, _ = await fence_redis.begin_step_commit(
            job_id="j1", step="A", exec_id="w:1", generation=1,
            candidate_digest=DIGEST, worker_id="w",
        )
        second, _ = await fence_redis.begin_step_commit(
            job_id="j1", step="A", exec_id="w:1", generation=1,
            candidate_digest=DIGEST, worker_id="w",
        )
        assert not await fence_redis.validate_step_commit("j1", "A", first)
        assert await fence_redis.validate_step_commit("j1", "A", second)

    @pytest.mark.asyncio
    async def test_finish_requires_manifest_published_and_consumes_once(
        self, fence_redis,
    ):
        await _activate_execution(fence_redis)
        token, _ = await fence_redis.begin_step_commit(
            job_id="j1", step="A", exec_id="w:1", generation=1,
            candidate_digest=DIGEST, worker_id="w",
        )
        assert not await fence_redis.finish_step_commit("j1", "A", token)
        assert await fence_redis.validate_step_commit(
            "j1", "A", token, phase="manifest_published",
        )
        assert await fence_redis.finish_step_commit("j1", "A", token)
        assert not await fence_redis.finish_step_commit("j1", "A", token)
        assert await fence_redis.get_step_commit("j1", "A") is None


# Local 后端提交协议(§5.2 条 3 故障点注入)


def _build_committed_manifest(
    job_id, scope_key, step, outputs, generation=1, exec_id="w:1",
):
    return build_step_manifest(
        job_id=job_id, scope_key=scope_key, step=step,
        part_index=1 if scope_key != "job" else None,
        exec_id=exec_id, job_generation=generation, attempt=1,
        started_at="2026-07-18T00:00:00Z", committed_at="2026-07-18T00:01:00Z",
        duration_sec=1.0,
        input_fingerprints={"input": "sha256:" + "b" * 64},
        definition_digest="sha256:" + "c" * 64,
        outputs=outputs,
        producer={
            "flori_version": "2.1.1", "build_sha": None, "worker_id": "w",
            "runner": "subprocess", "image": "flori/step-base",
            "image_digest": None, "tool_versions": {},
        },
    )


class _CountingVerifier:
    """verify_token 替身:第 fail_at 次调用返回 False(1-based);None=永真。"""

    def __init__(self, fail_at: int | None = None):
        self.calls = 0
        self.fail_at = fail_at
        self.phases: list[str] = []

    async def __call__(self, phase: str = "") -> bool:
        self.calls += 1
        self.phases.append(phase)
        return self.fail_at is None or self.calls != self.fail_at


async def _local_commit(
    storage: LocalStorage, tmp_path: Path, *, verifier=None, sha_override=None,
    token_digest=None, stale_paths=None, monkeypatch=None, break_manifest=False,
    commit_exec_id="w:1",
):
    job_id = "j_local"
    work = tmp_path / "jobs" / job_id
    _write(work, "out/a.json", b"alpha")
    _write(work, "out/b.json", b"beta")
    outputs = collect_step_outputs(work, ["out/*"], scope_key="job")
    if sha_override:
        outputs = [
            StepOutput(o.path, o.job_rel, o.size_bytes, sha_override, o.media_type)
            for o in outputs
        ]
    manifest, _bytes, digest = _build_committed_manifest(job_id, "job", "A", outputs)
    token = {
        "token_id": "t1", "exec_id": "w:1", "job_generation": 1,
        "candidate_digest": token_digest or digest,
    }
    for entry in outputs:
        await storage.stage_step_output(
            job_id, commit_exec_id, entry.job_rel, work / entry.path,
            size_bytes=entry.size_bytes, sha256=entry.sha256,
        )
    verifier = verifier or _CountingVerifier()
    if break_manifest:
        import shared.storage as storage_module

        original = storage_module.write_path_atomic

        def _boom(path, data):
            if path.name == "manifest.json":
                raise OSError("disk full")
            original(path, data)

        monkeypatch.setattr(storage_module, "write_path_atomic", _boom)
    record = build_commit_record(
        job_id=job_id, execution_step="A", exec_id="w:1", token=token,
        manifest_digest=digest, output_job_rels=[o.job_rel for o in outputs],
    )
    await storage.commit_step_outputs(
        job_id, "A", commit_exec_id,
        outputs=[
            {"path": o.job_rel, "size_bytes": o.size_bytes, "sha256": o.sha256}
            for o in outputs
        ],
        manifest=manifest, manifest_rel=manifest_relative_path("job", "A"),
        stale_paths=stale_paths or [], token=token, commit_record=record,
        verify_token=verifier,
    )
    return work, verifier


class TestLocalCommitProtocol:
    @pytest.mark.asyncio
    async def test_happy_path_manifest_last_and_cleanup(self, tmp_path):
        storage = LocalStorage(tmp_path / "jobs")
        work, verifier = await _local_commit(storage, tmp_path)
        manifest_path = work / ".flori" / "steps" / "A" / "manifest.json"
        assert manifest_path.is_file()
        validate_manifest(json.loads(manifest_path.read_text()))
        # commit 记录(promote_started 持久证据)在执行 staging namespace。
        record = (
            tmp_path / "jobs" / ".flori" / "staging" / "j_local" / "w:1"
            / ".commit.json"
        )
        assert record.is_file()
        assert json.loads(record.read_text())["promote_started"] is True
        # 最后一次围栏调用是 manifest_published 阶段推进。
        assert verifier.phases[-1] == "manifest_published"
        await storage.cleanup_execution_staging("j_local", "w:1")
        assert not record.parent.exists()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("fail_at", [1, 2, 3, 4, 5])
    async def test_fence_rejection_at_each_checkpoint_blocks_manifest(
        self, tmp_path, fail_at,
    ):
        # 2 个输出 → 前 4 次是 promote 前后校验,第 5 次是 manifest 发布前校验。
        storage = LocalStorage(tmp_path / "jobs")
        with pytest.raises(StepCommitFenceRejected):
            await _local_commit(
                storage, tmp_path, verifier=_CountingVerifier(fail_at=fail_at),
            )
        assert not (
            tmp_path / "jobs" / "j_local" / ".flori" / "steps" / "A" / "manifest.json"
        ).exists()

    @pytest.mark.asyncio
    async def test_read_back_mismatch_blocks_manifest(self, tmp_path):
        storage = LocalStorage(tmp_path / "jobs")
        with pytest.raises(StepCommitIntegrityError, match="read-back"):
            await _local_commit(
                storage, tmp_path, sha_override="sha256:" + "d" * 64,
            )
        assert not (
            tmp_path / "jobs" / "j_local" / ".flori" / "steps" / "A" / "manifest.json"
        ).exists()

    @pytest.mark.asyncio
    async def test_token_binding_mismatch_rejected_before_promote(self, tmp_path):
        storage = LocalStorage(tmp_path / "jobs")
        with pytest.raises(StepCommitIntegrityError, match="candidate_digest"):
            await _local_commit(
                storage, tmp_path, token_digest="sha256:" + "e" * 64,
            )

    @pytest.mark.asyncio
    async def test_manifest_put_failure_leaves_no_partial_manifest(
        self, tmp_path, monkeypatch,
    ):
        storage = LocalStorage(tmp_path / "jobs")
        with pytest.raises(OSError, match="disk full"):
            await _local_commit(
                storage, tmp_path, monkeypatch=monkeypatch, break_manifest=True,
            )
        manifest_dir = tmp_path / "jobs" / "j_local" / ".flori" / "steps" / "A"
        assert not manifest_dir.exists() or not any(manifest_dir.iterdir())

    @pytest.mark.asyncio
    async def test_stale_outputs_deleted_exactly(self, tmp_path):
        storage = LocalStorage(tmp_path / "jobs")
        stale = _write(tmp_path / "jobs" / "j_local", "old/stale.json", b"old")
        keep = _write(tmp_path / "jobs" / "j_local", "unrelated.txt", b"keep")
        await _local_commit(storage, tmp_path, stale_paths=["old/stale.json"])
        assert not stale.exists()
        assert keep.exists()

    @pytest.mark.asyncio
    async def test_stale_computation_from_previous_manifest(self, tmp_path):
        new = [StepOutput("out/a.json", "out/a.json", 1, DIGEST, None)]
        previous, _, _ = _build_committed_manifest(
            "j_local", "job", "A",
            [
                StepOutput("out/a.json", "out/a.json", 1, DIGEST, None),
                StepOutput("out/old.json", "out/old.json", 1, DIGEST, None),
            ],
        )
        assert stale_output_paths(previous, new) == ["out/old.json"]


# Remote(MinIO 替身)协议等价(§5.2 条 14)


class _FakeMinio:
    """内存版 minio client:仅覆盖提交协议用到的调用面。"""

    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def stat_object(self, bucket, key):
        from minio.error import S3Error

        if key not in self.objects:
            raise S3Error(
                MagicMock(status=404), "NoSuchKey", "not found", key, "req", "host",
            )
        return MagicMock(size=len(self.objects[key]))

    def copy_object(self, bucket, target, source):
        self.objects[target] = self.objects[source.object_name]

    def compose_object(self, bucket, target, sources):
        self.objects[target] = self.objects[sources[0].object_name]

    def fput_object(self, bucket, key, path):
        self.objects[key] = Path(path).read_bytes()

    def put_object(self, bucket, key, stream, length, **kwargs):
        self.objects[key] = stream.read()

    def get_object(self, bucket, key, **kwargs):
        from minio.error import S3Error

        if key not in self.objects:
            raise S3Error(
                MagicMock(status=404), "NoSuchKey", "not found", key, "req", "host",
            )
        data = self.objects[key]
        resp = MagicMock()
        buffer = {"offset": 0}

        def _read(size=-1):
            start = buffer["offset"]
            if size is None or size < 0:
                chunk = data[start:]
            else:
                chunk = data[start:start + size]
            buffer["offset"] = start + len(chunk)
            return chunk

        resp.read.side_effect = _read
        return resp

    def remove_object(self, bucket, key):
        self.objects.pop(key, None)

    def list_objects(self, bucket, prefix="", recursive=True):
        for key in sorted(self.objects):
            if key.startswith(prefix):
                yield MagicMock(object_name=key)

    def remove_objects(self, bucket, objs):
        for obj in objs:
            name = getattr(obj, "name", None) or getattr(obj, "_name", None)
            self.objects.pop(name, None)
        return iter(())


class TestRemoteCommitProtocol:
    @pytest.mark.asyncio
    async def test_protocol_equivalent_to_local(self, tmp_path):
        rs = RemoteStorage("h:9000", "k", "s", "b", False, tmp_root=tmp_path / "tmp")
        fake = _FakeMinio()
        rs._client = lambda: fake
        job_id = "j_remote"
        work = tmp_path / "work"
        _write(work, "out/a.json", b"alpha")
        # push 已上传 canonical 的对象走服务端复制进 staging(不二次上行)。
        fake.objects[f"{job_id}/out/a.json"] = b"alpha"
        outputs = collect_step_outputs(work, ["out/*"], scope_key="job")
        manifest, _bytes, digest = _build_committed_manifest(
            job_id, "job", "A", outputs,
        )
        token = {
            "token_id": "t1", "exec_id": "w:1", "job_generation": 1,
            "candidate_digest": digest,
        }
        for entry in outputs:
            await rs.stage_step_output(
                job_id, "w:1", entry.job_rel, work / entry.path,
                size_bytes=entry.size_bytes, sha256=entry.sha256,
            )
        assert f".flori/staging/{job_id}/w:1/out/a.json" in fake.objects
        record = build_commit_record(
            job_id=job_id, execution_step="A", exec_id="w:1", token=token,
            manifest_digest=digest, output_job_rels=["out/a.json"],
        )
        fake.objects[f"{job_id}/out/stale.json"] = b"old"
        verifier = _CountingVerifier()
        await rs.commit_step_outputs(
            job_id, "A", "w:1",
            outputs=[
                {"path": o.job_rel, "size_bytes": o.size_bytes, "sha256": o.sha256}
                for o in outputs
            ],
            manifest=manifest, manifest_rel=manifest_relative_path("job", "A"),
            stale_paths=["out/stale.json"], token=token, commit_record=record,
            verify_token=verifier,
        )
        manifest_key = f"{job_id}/.flori/steps/A/manifest.json"
        assert manifest_key in fake.objects
        validate_manifest(json.loads(fake.objects[manifest_key]))
        assert f"{job_id}/out/stale.json" not in fake.objects
        assert verifier.phases[-1] == "manifest_published"
        # 内部命名空间不作为业务产物往返:list_files 不含 manifest/staging。
        assert await rs.list_files(job_id) == ["out/a.json"]
        await rs.cleanup_execution_staging(job_id, "w:1")
        assert not any(
            key.startswith(f".flori/staging/{job_id}/") for key in fake.objects
        )

    @pytest.mark.asyncio
    async def test_fence_rejection_blocks_manifest(self, tmp_path):
        rs = RemoteStorage("h:9000", "k", "s", "b", False, tmp_root=tmp_path / "tmp")
        fake = _FakeMinio()
        rs._client = lambda: fake
        work = tmp_path / "work"
        _write(work, "out/a.json", b"alpha")
        outputs = collect_step_outputs(work, ["out/*"], scope_key="job")
        manifest, _bytes, digest = _build_committed_manifest(
            "j_remote", "job", "A", outputs,
        )
        token = {
            "token_id": "t1", "exec_id": "w:1", "job_generation": 1,
            "candidate_digest": digest,
        }
        await rs.stage_step_output(
            "j_remote", "w:1", "out/a.json", work / "out/a.json",
            size_bytes=5, sha256=outputs[0].sha256,
        )
        with pytest.raises(StepCommitFenceRejected):
            await rs.commit_step_outputs(
                "j_remote", "A", "w:1",
                outputs=[{
                    "path": "out/a.json", "size_bytes": 5,
                    "sha256": outputs[0].sha256,
                }],
                manifest=manifest, manifest_rel=manifest_relative_path("job", "A"),
                stale_paths=[], token=token,
                commit_record=b"{}",
                verify_token=_CountingVerifier(fail_at=1),
            )
        assert "j_remote/.flori/steps/A/manifest.json" not in fake.objects
        assert "j_remote/out/a.json" not in fake.objects


# Part 并发隔离(§5.2 条 7)


class TestPartConcurrentIsolation:
    @pytest.mark.asyncio
    async def test_two_parts_commit_concurrently_without_crosstalk(self, tmp_path):
        storage = LocalStorage(tmp_path / "jobs")
        job_id = "j_parts"
        results = {}

        async def _commit(part_id: str, exec_id: str, payload: bytes):
            scope = part_scope(part_id)
            work = tmp_path / "jobs" / job_id / "parts" / part_id
            _write(work, "out/a.json", payload)
            outputs = collect_step_outputs(work, ["out/*"], scope_key=scope)
            manifest, _b, digest = _build_committed_manifest(
                job_id, scope, "01_download", outputs, exec_id=exec_id,
            )
            token = {
                "token_id": exec_id, "exec_id": exec_id, "job_generation": 1,
                "candidate_digest": digest,
            }
            for entry in outputs:
                await storage.stage_step_output(
                    job_id, exec_id, entry.job_rel, work / entry.path,
                    size_bytes=entry.size_bytes, sha256=entry.sha256,
                )
            record = build_commit_record(
                job_id=job_id,
                execution_step=execution_step_key(scope, "01_download"),
                exec_id=exec_id, token=token, manifest_digest=digest,
                output_job_rels=[o.job_rel for o in outputs],
            )
            await storage.commit_step_outputs(
                job_id, execution_step_key(scope, "01_download"), exec_id,
                outputs=[
                    {"path": o.job_rel, "size_bytes": o.size_bytes, "sha256": o.sha256}
                    for o in outputs
                ],
                manifest=manifest,
                manifest_rel=manifest_relative_path(scope, "01_download"),
                stale_paths=[], token=token, commit_record=record,
                verify_token=_CountingVerifier(),
            )
            results[part_id] = manifest

        await asyncio.gather(
            _commit("pt_a", "w:a", b"part-a"),
            _commit("pt_b", "w:b", b"part-b"),
        )
        for part_id, payload in (("pt_a", b"part-a"), ("pt_b", b"part-b")):
            manifest_path = (
                tmp_path / "jobs" / job_id / "parts" / part_id / ".flori"
                / "steps" / "01_download" / "manifest.json"
            )
            data = json.loads(manifest_path.read_text())
            validate_manifest(data)
            assert data["scope"]["part_id"] == part_id
            assert (
                tmp_path / "jobs" / job_id / "parts" / part_id / "out" / "a.json"
            ).read_bytes() == payload
        # 执行 staging namespace 按 exec 隔离。
        staging = tmp_path / "jobs" / ".flori" / "staging" / job_id
        assert sorted(p.name for p in staging.iterdir()) == ["w:a", "w:b"]


# Gateway 端点等价(§5.2 条 14):worker 经 runner 端点,中心 LocalStorage + 真围栏 Lua。


REG_TOKEN = "flw-registration-secret"


@pytest.fixture
async def real_redis():
    rc = make_fakeredis()
    await rc.set_registration_token(REG_TOKEN)
    yield rc
    await rc.close()


@pytest.fixture
def gateway_app(db, test_config, real_redis):
    return create_app(db=db, redis=real_redis, config=test_config)


@pytest.fixture
async def gateway_client(gateway_app):
    transport = ASGITransport(app=gateway_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _register_worker(client):
    resp = await client.post(
        "/api/runner/register",
        json={"type": "cpu", "pools": ["cpu", "io"], "tags": [], "reject_tags": []},
        headers={"Authorization": f"Bearer {REG_TOKEN}"},
    )
    body = resp.json()
    return body["worker_id"], body["worker_token"]


def _lease_headers(token, job_id, step, exec_id):
    return {
        "Authorization": f"Bearer {token}",
        "X-Flori-Lease-Job": job_id,
        "X-Flori-Lease-Step": step,
        "X-Flori-Lease-Exec": exec_id,
    }


class TestGatewayCommitEndpoints:
    @pytest.mark.asyncio
    async def test_full_commit_flow_over_gateway(
        self, gateway_client, real_redis, test_config,
    ):
        worker_id, token = await _register_worker(gateway_client)
        job_id, step, exec_id = "j_gw", "A", f"{worker_id}:1"
        await _activate_execution(
            real_redis, job_id=job_id, step=step, exec_id=exec_id, worker=worker_id,
        )
        # canonical 已有 push 上传的输出(双写阶段现实):staging/copy 服务端复制。
        job_dir = test_config.jobs_dir / job_id
        _write(job_dir, "out/a.json", b"alpha")
        outputs = collect_step_outputs(job_dir, ["out/*"], scope_key="job")
        manifest, _b, digest = _build_committed_manifest(
            job_id, "job", step, outputs, exec_id=exec_id,
        )
        headers = _lease_headers(token, job_id, step, exec_id)

        resp = await gateway_client.post(
            f"/api/runner/jobs/{job_id}/staging/copy",
            json={"path": "out/a.json", "size_bytes": 5, "sha256": outputs[0].sha256},
            headers=headers,
        )
        assert resp.status_code == 200 and resp.json()["staged"] is True

        resp = await gateway_client.post(
            f"/api/runner/jobs/{job_id}/steps/{step}/commit/begin",
            json={"candidate_digest": digest}, headers=headers,
        )
        assert resp.status_code == 200
        wire_token = resp.json()["token"]
        assert wire_token["candidate_digest"] == digest

        resp = await gateway_client.post(
            f"/api/runner/jobs/{job_id}/steps/{step}/commit",
            json={
                "token": wire_token,
                "outputs": [
                    {"path": o.job_rel, "size_bytes": o.size_bytes, "sha256": o.sha256}
                    for o in outputs
                ],
                "manifest": manifest,
                "manifest_rel": manifest_relative_path("job", step),
                "stale_paths": [],
            },
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        manifest_path = job_dir / ".flori" / "steps" / step / "manifest.json"
        validate_manifest(json.loads(manifest_path.read_text()))

        # done 与 manifest 同 token;完成后 token 消费,不接受第二次。
        resp = await gateway_client.post(
            f"/api/runner/jobs/{job_id}/steps/{step}/complete",
            json={
                "pool": "cpu", "exec_id": exec_id, "duration": 1.0,
                "started_at": 0.0, "commit_token": wire_token,
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200 and resp.json()["ok"] is True
        assert await real_redis.get_step_commit(job_id, step) is None

        resp = await gateway_client.request(
            "DELETE", f"/api/runner/jobs/{job_id}/staging", headers=headers,
        )
        assert resp.status_code == 200
        assert not (
            test_config.jobs_dir / ".flori" / "staging" / job_id / exec_id
        ).exists()

    @pytest.mark.asyncio
    async def test_stale_execution_cannot_begin_after_new_generation(
        self, gateway_client, real_redis,
    ):
        # §5.2 条 4(gateway 面):换代后 begin 409,旧 token 的 commit 也 409。
        worker_id, token = await _register_worker(gateway_client)
        job_id, step, exec_id = "j_gw2", "A", f"{worker_id}:1"
        await _activate_execution(
            real_redis, job_id=job_id, step=step, exec_id=exec_id, worker=worker_id,
        )
        headers = _lease_headers(token, job_id, step, exec_id)
        resp = await gateway_client.post(
            f"/api/runner/jobs/{job_id}/steps/{step}/commit/begin",
            json={"candidate_digest": DIGEST}, headers=headers,
        )
        wire_token = resp.json()["token"]
        await real_redis.r.hset(f"job:{job_id}", "lifecycle_generation", "2")
        resp = await gateway_client.post(
            f"/api/runner/jobs/{job_id}/steps/{step}/commit/begin",
            json={"candidate_digest": DIGEST}, headers=headers,
        )
        assert resp.status_code == 409
        resp = await gateway_client.post(
            f"/api/runner/jobs/{job_id}/steps/{step}/commit",
            json={
                "token": wire_token, "outputs": [],
                "manifest": {}, "manifest_rel": "", "stale_paths": [],
            },
            headers=headers,
        )
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_internal_namespace_writes_rejected(
        self, gateway_client, real_redis,
    ):
        worker_id, token = await _register_worker(gateway_client)
        job_id, step, exec_id = "j_gw3", "A", f"{worker_id}:1"
        await _activate_execution(
            real_redis, job_id=job_id, step=step, exec_id=exec_id, worker=worker_id,
        )
        headers = _lease_headers(token, job_id, step, exec_id)
        resp = await gateway_client.put(
            f"/api/runner/jobs/{job_id}/artifacts/.flori/steps/A/manifest.json",
            content=b"{}", headers=headers,
        )
        assert resp.status_code == 403
        resp = await gateway_client.put(
            f"/api/runner/jobs/{job_id}/staging/.flori/x",
            content=b"{}", headers=headers,
        )
        assert resp.status_code == 403


# Worker 端到端(条 4/11 + 双写保守序)


@pytest.fixture
def tmp_jobs_dir(tmp_path):
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    return jobs


@pytest.fixture
def worker_db(tmp_path, current_schema_db_template):
    d = clone_current_schema_database(
        current_schema_db_template, tmp_path / "worker.db",
    )
    yield d
    d.close()


@pytest.fixture
async def worker_redis():
    client = make_fakeredis()
    yield client
    await client.close()


@pytest.fixture
def worker_config(tmp_path, tmp_jobs_dir, configs_dir):
    return AppConfig(
        data_dir=tmp_path,
        db_path=tmp_path / "worker.db",
        jobs_dir=tmp_jobs_dir,
        config_dir=configs_dir,
        prompts_dir=tmp_path / "prompts",
        pipelines={
            "test": {
                "steps": [
                    {"name": "A", "pool": "cpu", "depends_on": [], "retries": 2,
                     "module": "steps.test_a", "timeout_sec": 60,
                     "outputs": ["out/*.json"]},
                ]
            }
        },
        pools={"pools": {"cpu": {"limit": 3}, "io": {"limit": 999}}},
        providers={},
    )


@pytest.fixture
def manifest_worker(worker_redis, worker_db, worker_config, tmp_jobs_dir):
    return Worker(
        transport=RedisTransport(worker_redis, worker_db),
        config=worker_config,
        storage=LocalStorage(tmp_jobs_dir),
        worker_type="cpu", pools=["cpu"], tags=set(), reject_tags=set(),
    )


async def _prime_step(worker_redis, worker_db, claim, worker_id):
    if worker_db.get_job(claim["job_id"]) is None:
        worker_db.create_job(make_job())
    worker_db.upsert_step(Step(
        job_id=claim["job_id"], name=claim["step"],
        status=StepStatus.READY, pool="cpu",
    ))
    await worker_redis.try_acquire_slot("cpu", 3, claim["exec_id"])
    await activate_claim(worker_redis, claim, worker_id)
    await worker_redis.create_task_lease(
        worker_id, claim["job_id"], claim["step"], claim["exec_id"], "cpu",
    )


def _fake_success_runner(worker: Worker, *, mutate=None):
    async def run_step(ctx, on_progress, on_tick):
        _write(ctx.work_dir, "out/a.json", b"payload")
        record = build_candidate_record(
            ctx.step, {"input": "sha256:" + "0" * 64},
        )
        (ctx.work_dir / candidate_filename(ctx.step)).write_text(json.dumps(record))
        if mutate is not None:
            await mutate()
        return 0, ""

    worker.runner.run_step = run_step


class TestWorkerManifestFlow:
    @pytest.mark.asyncio
    async def test_success_publishes_manifest_then_done(
        self, manifest_worker, worker_redis, worker_db, tmp_jobs_dir,
    ):
        await manifest_worker.register()
        claim = make_claim(exec_id="w_test:1")
        await _prime_step(worker_redis, worker_db, claim, manifest_worker.worker_id)
        _fake_success_runner(manifest_worker)

        await manifest_worker.execute(claim)

        manifest_path = (
            tmp_jobs_dir / claim["job_id"] / ".flori" / "steps" / "A" / "manifest.json"
        )
        data = json.loads(manifest_path.read_text())
        validate_manifest(data)
        assert data["outputs"][0]["path"] == "out/a.json"
        assert data["execution"]["exec_id"] == claim["exec_id"]
        events = await lifecycle_payloads(worker_redis, "step_completed")
        assert len(events) == 1
        # token 已随 done 消费;staging 已清理。
        assert await worker_redis.get_step_commit(claim["job_id"], "A") is None
        assert not (
            tmp_jobs_dir / ".flori" / "staging" / claim["job_id"]
        ).exists()

    @pytest.mark.asyncio
    async def test_new_generation_blocks_old_exec_manifest_and_done(
        self, manifest_worker, worker_redis, worker_db, tmp_jobs_dir,
    ):
        # §5.2 条 4 端到端:子进程结束后 rerun 换代,旧执行既不能发布 manifest 也不能报 done。
        await manifest_worker.register()
        claim = make_claim(exec_id="w_test:1")
        await _prime_step(worker_redis, worker_db, claim, manifest_worker.worker_id)

        async def advance_generation():
            await worker_redis.r.hset(
                f"job:{claim['job_id']}", "lifecycle_generation", "2",
            )

        _fake_success_runner(manifest_worker, mutate=advance_generation)

        await manifest_worker.execute(claim)

        assert not (
            tmp_jobs_dir / claim["job_id"] / ".flori" / "steps" / "A" / "manifest.json"
        ).exists()
        assert await lifecycle_payloads(worker_redis, "step_completed") == []
        assert await lifecycle_payloads(worker_redis, "step_failed") == []

    @pytest.mark.asyncio
    async def test_commit_failure_reports_failed_not_done(
        self, manifest_worker, worker_redis, worker_db,
    ):
        await manifest_worker.register()
        claim = make_claim(exec_id="w_test:1")
        await _prime_step(worker_redis, worker_db, claim, manifest_worker.worker_id)
        _fake_success_runner(manifest_worker)

        async def boom(*args, **kwargs):
            raise RuntimeError("promote blew up")

        manifest_worker.storage.commit_step_outputs = boom

        await manifest_worker.execute(claim)

        assert await lifecycle_payloads(worker_redis, "step_completed") == []
        failed = await lifecycle_payloads(worker_redis, "step_failed")
        assert len(failed) == 1
        assert "manifest commit failed" in failed[0]["error"]
        # 审查 P1-6:失败分支也 best-effort 清理执行 staging,孤儿不留到 TTL。
        jobs_root = Path(manifest_worker.storage.jobs_dir)
        assert not (jobs_root / ".flori" / "staging" / claim["job_id"]).exists()

    @pytest.mark.asyncio
    async def test_reused_candidate_skips_republish_when_manifest_current(
        self, manifest_worker, worker_redis, worker_db, tmp_jobs_dir,
    ):
        # 审查 P3-7:幂等跳过 + 中心 manifest 与当前 digest 一致 → 不再重发;
        # 删掉 manifest 后重跑 → 自愈重发。
        await manifest_worker.register()
        claim = make_claim(exec_id="w_test:1")
        await _prime_step(worker_redis, worker_db, claim, manifest_worker.worker_id)
        _fake_success_runner(manifest_worker)
        await manifest_worker.execute(claim)
        manifest_path = (
            tmp_jobs_dir / claim["job_id"] / ".flori" / "steps" / "A" / "manifest.json"
        )
        assert manifest_path.is_file()

        commits = []
        original_commit = manifest_worker.storage.commit_step_outputs

        async def counting_commit(*args, **kwargs):
            commits.append(True)
            return await original_commit(*args, **kwargs)

        manifest_worker.storage.commit_step_outputs = counting_commit

        def _reused_runner():
            async def run_step(ctx, on_progress, on_tick):
                record = build_candidate_record(
                    ctx.step, {"input": "sha256:" + "0" * 64}, reused=True,
                )
                (ctx.work_dir / candidate_filename(ctx.step)).write_text(
                    json.dumps(record),
                )
                return 0, ""

            manifest_worker.runner.run_step = run_step

        _reused_runner()
        claim2 = make_claim(exec_id="w_test:2")
        await _prime_step(worker_redis, worker_db, claim2, manifest_worker.worker_id)
        await manifest_worker.execute(claim2)
        assert commits == []  # manifest 一致:未重发
        assert len(await lifecycle_payloads(worker_redis, "step_completed")) == 2

        manifest_path.unlink()
        claim3 = make_claim(exec_id="w_test:3")
        await _prime_step(worker_redis, worker_db, claim3, manifest_worker.worker_id)
        await manifest_worker.execute(claim3)
        assert commits == [True]  # 缺 manifest:自愈重发
        assert manifest_path.is_file()

    @pytest.mark.asyncio
    async def test_no_outputs_declaration_keeps_legacy_done(
        self, manifest_worker, worker_redis, worker_db, worker_config, tmp_jobs_dir,
    ):
        # dual 保守序:无 outputs 声明的步骤完全走既有 done 语义,不发布 manifest。
        worker_config.pipelines["test"]["steps"][0].pop("outputs")
        await manifest_worker.register()
        claim = make_claim(exec_id="w_test:1")
        await _prime_step(worker_redis, worker_db, claim, manifest_worker.worker_id)
        _fake_success_runner(manifest_worker)

        await manifest_worker.execute(claim)

        assert len(await lifecycle_payloads(worker_redis, "step_completed")) == 1
        assert not (
            tmp_jobs_dir / claim["job_id"] / ".flori"
        ).exists()

    @pytest.mark.asyncio
    async def test_failure_pushes_only_diagnostics_whitelist(
        self, manifest_worker, worker_redis, worker_db,
    ):
        # §5.2 条 11:失败路径只回传诊断白名单,不推业务输出、不发布 manifest。
        await manifest_worker.register()
        claim = make_claim(exec_id="w_test:1")
        await _prime_step(worker_redis, worker_db, claim, manifest_worker.worker_id)
        pushes = []

        async def run_step(ctx, on_progress, on_tick):
            _write(ctx.work_dir, "out/a.json", b"partial business output")
            _write(ctx.work_dir, "logs/A.log", b"log line")
            return 1, "boom"

        async def capture_push(job_id, step, work_dir, *, exclude_paths=None,
                               only_globs=None):
            pushes.append(only_globs)

        manifest_worker.runner.run_step = run_step
        manifest_worker.storage.push = capture_push

        await manifest_worker.execute(claim)

        assert len(pushes) == 1
        globs = pushes[0]
        assert globs == diagnostics_globs("A", "job")
        assert any(pattern.startswith("logs/") for pattern in globs)
        assert not any("out" == pattern.split("/", 1)[0] for pattern in globs)
        assert len(await lifecycle_payloads(worker_redis, "step_failed")) == 1


# 审查修复回归:TTL 续期 / stale 越权 / 幽灵输出 / 超限豁免 / 混跑窗口


class TestCommitFenceTTLRenewal:
    @pytest.mark.asyncio
    async def test_validate_renews_ttl_for_long_promotes(self, fence_redis):
        # 审查 P2-4:长 promote 期间每次围栏校验续期 TTL,token 不被饿死;身份不变。
        await _activate_execution(fence_redis)
        token, _ = await fence_redis.begin_step_commit(
            job_id="j1", step="A", exec_id="w:1", generation=1,
            candidate_digest=DIGEST, worker_id="w",
        )
        key = fence_redis._step_commit_key("j1", "A")
        await fence_redis.r.expire(key, 3)
        assert await fence_redis.r.ttl(key) <= 3
        assert await fence_redis.validate_step_commit("j1", "A", token)
        assert await fence_redis.r.ttl(key) > 100
        # 换代后校验失败,且不再续期(围栏不因续期放宽)。
        await fence_redis.r.expire(key, 3)
        await fence_redis.r.hset("job:j1", "lifecycle_generation", "2")
        assert not await fence_redis.validate_step_commit("j1", "A", token)
        assert await fence_redis.r.ttl(key) <= 3


class TestStalePathAuthorization:
    @pytest.mark.asyncio
    async def test_cross_part_stale_rejected_before_any_side_effect(self, tmp_path):
        # 审查 P1:Part A 的执行不得经 stale_paths 删除 Part B 的产物;零删除零 promote。
        storage = LocalStorage(tmp_path / "jobs")
        job_id = "j_auth"
        victim = _write(
            tmp_path / "jobs" / job_id, "parts/pt_b/out/a.json", b"victim",
        )
        scope = part_scope("pt_a")
        work = tmp_path / "jobs" / job_id / "parts" / "pt_a"
        _write(work, "out/a.json", b"mine")
        outputs = collect_step_outputs(work, ["out/*"], scope_key=scope)
        manifest, _b, digest = _build_committed_manifest(
            job_id, scope, "01_download", outputs,
        )
        token = {
            "token_id": "t1", "exec_id": "w:1", "job_generation": 1,
            "candidate_digest": digest,
        }
        execution_step = execution_step_key(scope, "01_download")
        for entry in outputs:
            await storage.stage_step_output(
                job_id, "w:1", entry.job_rel, work / entry.path,
                size_bytes=entry.size_bytes, sha256=entry.sha256,
            )
        with pytest.raises(StepCommitFenceRejected, match="stale"):
            await storage.commit_step_outputs(
                job_id, execution_step, "w:1",
                outputs=[
                    {"path": o.job_rel, "size_bytes": o.size_bytes, "sha256": o.sha256}
                    for o in outputs
                ],
                manifest=manifest,
                manifest_rel=manifest_relative_path(scope, "01_download"),
                stale_paths=["parts/pt_b/out/a.json"],
                token=token, commit_record=b"{}",
                verify_token=_CountingVerifier(),
            )
        assert victim.read_bytes() == b"victim"
        # 越权在任何副作用前拒绝:自己的输出也未 promote,manifest 未发布。
        assert not (
            tmp_path / "jobs" / job_id / "parts" / "pt_a" / ".flori"
        ).exists()

    @pytest.mark.asyncio
    async def test_ghost_manifest_outputs_rejected(self, tmp_path):
        # 审查 P2-3:manifest 声明集与实际提交集不一致(幽灵输出)即拒绝发布。
        storage = LocalStorage(tmp_path / "jobs")
        job_id = "j_ghost"
        work = tmp_path / "jobs" / job_id
        _write(work, "out/a.json", b"alpha")
        outputs = collect_step_outputs(work, ["out/*"], scope_key="job")
        ghost = outputs + [StepOutput("out/ghost.json", "out/ghost.json", 5, DIGEST, None)]
        manifest, _b, digest = _build_committed_manifest(job_id, "job", "A", ghost)
        token = {
            "token_id": "t1", "exec_id": "w:1", "job_generation": 1,
            "candidate_digest": digest,
        }
        await storage.stage_step_output(
            job_id, "w:1", "out/a.json", work / "out/a.json",
            size_bytes=outputs[0].size_bytes, sha256=outputs[0].sha256,
        )
        with pytest.raises(StepCommitIntegrityError, match="do not match"):
            await storage.commit_step_outputs(
                job_id, "A", "w:1",
                outputs=[
                    {"path": o.job_rel, "size_bytes": o.size_bytes, "sha256": o.sha256}
                    for o in outputs
                ],
                manifest=manifest, manifest_rel=manifest_relative_path("job", "A"),
                stale_paths=[], token=token, commit_record=b"{}",
                verify_token=_CountingVerifier(),
            )
        assert not (work / ".flori" / "steps" / "A" / "manifest.json").exists()

    @pytest.mark.asyncio
    async def test_manifest_identity_must_match_execution(self, tmp_path):
        # manifest 的 exec_id 与提交执行不一致即拒绝(身份=租约执行身份)。
        storage = LocalStorage(tmp_path / "jobs")
        with pytest.raises(StepCommitIntegrityError, match="exec_id"):
            await _local_commit(storage, tmp_path, commit_exec_id="w:other")


class TestGatewayStalePathEndpoint:
    @pytest.mark.asyncio
    async def test_cross_part_stale_paths_rejected_with_zero_deletion(
        self, gateway_client, real_redis, test_config,
    ):
        worker_id, token = await _register_worker(gateway_client)
        job_id = "j_gw_auth"
        scope = part_scope("pt_a")
        step = execution_step_key(scope, "01_download")
        exec_id = f"{worker_id}:1"
        await _activate_execution(
            real_redis, job_id=job_id, step=step, exec_id=exec_id, worker=worker_id,
        )
        victim = _write(
            test_config.jobs_dir / job_id, "parts/pt_b/out/a.json", b"victim",
        )
        work = test_config.jobs_dir / job_id / "parts" / "pt_a"
        _write(work, "out/a.json", b"mine")
        outputs = collect_step_outputs(work, ["out/*"], scope_key=scope)
        manifest, _b, digest = _build_committed_manifest(
            job_id, scope, "01_download", outputs, exec_id=exec_id,
        )
        headers = _lease_headers(token, job_id, step, exec_id)
        resp = await gateway_client.post(
            f"/api/runner/jobs/{job_id}/steps/{step}/commit/begin",
            json={"candidate_digest": digest}, headers=headers,
        )
        wire_token = resp.json()["token"]
        resp = await gateway_client.post(
            f"/api/runner/jobs/{job_id}/steps/{step}/commit",
            json={
                "token": wire_token,
                "outputs": [
                    {"path": o.job_rel, "size_bytes": o.size_bytes, "sha256": o.sha256}
                    for o in outputs
                ],
                "manifest": manifest,
                "manifest_rel": manifest_relative_path(scope, "01_download"),
                "stale_paths": ["parts/pt_b/out/a.json"],
            },
            headers=headers,
        )
        assert resp.status_code == 403
        assert victim.read_bytes() == b"victim"

    @pytest.mark.asyncio
    async def test_job_scope_stale_cannot_reach_part_territory(
        self, gateway_client, real_redis, test_config,
    ):
        worker_id, token = await _register_worker(gateway_client)
        job_id, step, exec_id = "j_gw_auth2", "A", f"{worker_id}:1"
        await _activate_execution(
            real_redis, job_id=job_id, step=step, exec_id=exec_id, worker=worker_id,
        )
        victim = _write(
            test_config.jobs_dir / job_id, "parts/pt_x/out/a.json", b"victim",
        )
        headers = _lease_headers(token, job_id, step, exec_id)
        resp = await gateway_client.post(
            f"/api/runner/jobs/{job_id}/steps/{step}/commit/begin",
            json={"candidate_digest": DIGEST}, headers=headers,
        )
        wire_token = resp.json()["token"]
        resp = await gateway_client.post(
            f"/api/runner/jobs/{job_id}/steps/{step}/commit",
            json={
                "token": wire_token, "outputs": [],
                "manifest": {}, "manifest_rel": "",
                "stale_paths": ["parts/pt_x/out/a.json"],
            },
            headers=headers,
        )
        assert resp.status_code == 403
        assert victim.read_bytes() == b"victim"


class TestOversizeExemption:
    def test_oversize_output_exempted_not_failed(self, tmp_path, monkeypatch):
        # 审查 P2-5:超 10GiB 硬上限按 NO_PUSH 同款豁免,绝不把成功步骤打失败。
        import shared.step_output_commit as module

        monkeypatch.setattr(module, "MAX_OUTPUT_FILE_BYTES", 1024)
        _write(tmp_path, "out/huge.bin", b"x" * 2048)
        _write(tmp_path, "out/a.json", b"{}")
        assert expand_step_outputs(tmp_path, ["out/*"], scope_key="job") == [
            "out/a.json",
        ]

    @pytest.mark.asyncio
    async def test_worker_step_with_oversize_output_still_done(
        self, manifest_worker, worker_redis, worker_db, tmp_jobs_dir, monkeypatch,
    ):
        import shared.step_output_commit as module

        monkeypatch.setattr(module, "MAX_OUTPUT_FILE_BYTES", 1024)
        await manifest_worker.register()
        claim = make_claim(exec_id="w_test:1")
        await _prime_step(worker_redis, worker_db, claim, manifest_worker.worker_id)

        async def run_step(ctx, on_progress, on_tick):
            _write(ctx.work_dir, "out/a.json", b"payload")
            _write(ctx.work_dir, "out/huge.json", b"x" * 4096)
            record = build_candidate_record(ctx.step, {"input": _SHA})
            (ctx.work_dir / candidate_filename(ctx.step)).write_text(
                json.dumps(record),
            )
            return 0, ""

        manifest_worker.runner.run_step = run_step
        await manifest_worker.execute(claim)

        assert len(await lifecycle_payloads(worker_redis, "step_completed")) == 1
        manifest_path = (
            tmp_jobs_dir / claim["job_id"] / ".flori" / "steps" / "A" / "manifest.json"
        )
        data = json.loads(manifest_path.read_text())
        assert [entry["path"] for entry in data["outputs"]] == ["out/a.json"]


class TestGatewayMixedVersionWindow:
    def _transport(self, tmp_path, resp):
        from worker.gateway_transport import GatewayTransport

        class _FakeClient:
            def __init__(self, response):
                self._response = response

            async def post(self, *args, **kwargs):
                return self._response

        gt = GatewayTransport(
            "http://gw", registration_token="reg",
            id_file=str(tmp_path / "worker.id"),
        )
        gt._client = _FakeClient(resp)
        gt._worker_token = "flwt-x"
        return gt

    class _Resp:
        def __init__(self, status_code, body=None):
            self.status_code = status_code
            self._body = body if body is not None else {}

        def json(self):
            return self._body

        def raise_for_status(self):
            import httpx

            if self.status_code >= 400:
                raise httpx.HTTPStatusError(
                    "boom", request=MagicMock(), response=MagicMock(
                        status_code=self.status_code,
                    ),
                )

    @pytest.mark.asyncio
    async def test_begin_404_means_center_unsupported_returns_none(self, tmp_path):
        # 审查 P3-8:旧中心无端点 → None,worker 保守跳过 manifest 走既有 done。
        gt = self._transport(tmp_path, self._Resp(404))
        claim = {"job_id": "j1", "step": "A", "exec_id": "w:1", "generation": 1}
        assert await gt.begin_step_commit(claim, DIGEST) is None

    @pytest.mark.asyncio
    async def test_begin_409_raises_stale(self, tmp_path):
        from shared.step_output_commit import StaleCommitError

        gt = self._transport(tmp_path, self._Resp(409))
        claim = {"job_id": "j1", "step": "A", "exec_id": "w:1", "generation": 1}
        with pytest.raises(StaleCommitError):
            await gt.begin_step_commit(claim, DIGEST)

    @pytest.mark.asyncio
    async def test_confirm_404_returns_false(self, tmp_path):
        gt = self._transport(tmp_path, self._Resp(404))
        claim = {"job_id": "j1", "step": "A", "exec_id": "w:1", "generation": 1}
        assert await gt.confirm_step_commit(claim, {"token_id": "t"}) is False


# LocalStorage 隔离 attempt(§2.6-1,opt-in)


class TestLocalAttemptIsolation:
    @pytest.mark.asyncio
    async def test_pull_copies_committed_view_and_push_writes_back(
        self, tmp_path, monkeypatch,
    ):
        monkeypatch.setenv("FLORI_LOCAL_ATTEMPT_ISOLATION", "1")
        storage = LocalStorage(tmp_path / "jobs")
        canonical = tmp_path / "jobs" / "j_iso"
        _write(canonical, "input/a.txt", b"committed")
        _write(canonical, ".flori/steps/A/manifest.json", b"{}")

        work_dir = await storage.pull("j_iso", "A")
        assert work_dir != canonical
        assert (work_dir / "input/a.txt").read_bytes() == b"committed"
        # 内部命名空间不进 attempt 视图;写 attempt 不击穿 canonical(真实复制)。
        assert not (work_dir / ".flori").exists()
        (work_dir / "input/a.txt").write_bytes(b"mutated")
        assert canonical.joinpath("input/a.txt").read_bytes() == b"committed"

        _write(work_dir, "out/new.json", b"fresh")
        await storage.push("j_iso", "A", work_dir)
        assert (canonical / "out/new.json").read_bytes() == b"fresh"
        assert (canonical / "input/a.txt").read_bytes() == b"mutated"

        await storage.cleanup("j_iso", "A", work_dir)
        assert not work_dir.exists()

    @pytest.mark.asyncio
    async def test_push_respects_diagnostics_whitelist(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FLORI_LOCAL_ATTEMPT_ISOLATION", "1")
        storage = LocalStorage(tmp_path / "jobs")
        canonical = tmp_path / "jobs" / "j_iso2"
        canonical.mkdir(parents=True)
        work_dir = await storage.pull("j_iso2", "A")
        _write(work_dir, "out/business.json", b"partial")
        _write(work_dir, "logs/A.log", b"log")
        await storage.push(
            "j_iso2", "A", work_dir, only_globs=diagnostics_globs("A", "job"),
        )
        assert (canonical / "logs/A.log").is_file()
        assert not (canonical / "out/business.json").exists()

    @pytest.mark.asyncio
    async def test_default_mode_stays_in_place(self, tmp_path, monkeypatch):
        monkeypatch.delenv("FLORI_LOCAL_ATTEMPT_ISOLATION", raising=False)
        storage = LocalStorage(tmp_path / "jobs")
        (tmp_path / "jobs" / "j_flat").mkdir(parents=True)
        assert await storage.pull("j_flat", "A") == tmp_path / "jobs" / "j_flat"


# 诊断白名单纯逻辑(条 11)


class TestDiagnosticsGlobs:
    def test_audit_globs_merged_with_scope_prefix(self):
        globs = diagnostics_globs(
            "10_evidence", part_scope("pt_a"),
            audit_globs=["output/evidence_audit/*"],
        )
        assert "parts/pt_a/output/evidence_audit/*" in globs

    def test_part_scope_prefixes(self):
        globs = diagnostics_globs("01_download", part_scope("pt_a"))
        assert "parts/pt_a/logs/*" in globs
        assert "parts/pt_a/output/ai_logs/*" in globs
        assert "parts/pt_a/.01_download.error.json" in globs
        assert all(g.startswith("parts/pt_a/") for g in globs)
