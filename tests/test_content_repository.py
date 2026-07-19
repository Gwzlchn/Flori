"""content repository 测试:CAS 幂等、snapshot 确定性/闭包、refs/receipts/锁、GC mark 与 scrub。"""

import hashlib
import os
from pathlib import Path

import pytest

import shared.content_repository as content_repository
from shared.content_policy import PolicyError
from shared.content_repository import (
    LEGACY_SNAPSHOT_FORMAT,
    REPOSITORY_FORMAT,
    SNAPSHOT_FORMAT,
    SOURCE_MANIFEST_FORMAT,
    ContentRepository,
    RepositoryCorruptionError,
    RepositoryError,
    RepositoryLockError,
)
from shared.step_manifest import canonical_digest, canonical_json_bytes, compute_input_digest


HEX_A = "sha256:" + "a" * 64
HEX_D = "sha256:" + "d" * 64

BLOB_SHARED = b"shared-video-bytes-" + b"S" * 64
BLOB_ONLY_1 = b"first-only-bytes-" + b"1" * 64
BLOB_ONLY_2 = b"second-only-bytes-" + b"2" * 64


def sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def build_step_result(job_id: str, part_id: str, data_map: dict[str, bytes]) -> dict:
    fingerprints = {"source": HEX_A}
    outputs = [
        {
            "path": path,
            "size_bytes": len(data),
            "sha256": sha(data),
            "media_type": None,
        }
        for path, data in sorted(data_map.items())
    ]
    manifest = {
        "format": "flori-step-manifest",
        "format_version": 1,
        "job_id": job_id,
        "scope": {
            "kind": "part",
            "scope_key": f"part:{part_id}",
            "part_id": part_id,
            "part_index": 1,
        },
        "step": "01_download",
        "outcome": "done",
        "execution": {
            "exec_id": f"exec_{part_id}",
            "job_generation": 1,
            "attempt": 1,
            "started_at": "2026-07-18T04:00:00Z",
            "committed_at": "2026-07-18T04:10:00Z",
            "duration_sec": 600,
        },
        "compatibility": {
            "input_fingerprints": fingerprints,
            "input_digest": compute_input_digest(fingerprints),
            "definition_digest": HEX_D,
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
        "outputs": outputs,
        "skip": None,
    }
    return {
        "job_id": job_id,
        "scope_key": f"part:{part_id}",
        "step": "01_download",
        "manifest": manifest,
        "output_blobs": {path: sha(data) for path, data in data_map.items()},
    }


def build_job_core(job_id: str) -> dict:
    return {
        "id": job_id,
        "content_type": "video",
        "pipeline": "video_v2",
        "created_at": "2026-07-18T03:00:00Z",
    }


def build_part_core(part_id: str, job_id: str) -> dict:
    return {
        "id": part_id,
        "job_id": job_id,
        "part_index": 1,
        "created_at": "2026-07-18T03:00:00Z",
    }


def build_ai_usage(exec_id: str) -> dict:
    return {
        "exec_id": exec_id,
        "created_at": "2026-07-18T05:00:00Z",
        "provider": "claude",
        "model": "claude-x",
        "input_tokens": 10,
        "output_tokens": 5,
    }


def build_failure_event(exec_id: str, *, usage_refs: list[str] | None = None) -> dict:
    body = {
        "job_id": "job_alpha",
        "scope_key": "part:pt_alpha1",
        "step": "01_download",
        "exec_id": exec_id,
        "failed_at": "2026-07-18T05:00:00Z",
    }
    if usage_refs is not None:
        body["ai_usage_refs"] = usage_refs
    return body


def build_snapshot(
    *, jobs=(), parts=(), step_results=(), failures=(), business_ledgers=(),
    blob_refs=(), job_ids=(),
) -> dict:
    return {
        "format": SNAPSHOT_FORMAT,
        "repository_format": REPOSITORY_FORMAT,
        "source": {
            "app_version": "2.2.0",
            "db_user_version": 8,
            "manifest_format": SOURCE_MANIFEST_FORMAT,
        },
        "selector": {
            "partial": bool(job_ids),
            "job_ids": sorted(set(job_ids)),
        },
        "records": {
            "jobs": sorted(jobs),
            "parts": sorted(parts),
            "step_results": sorted(step_results),
            "failures": sorted(failures),
            "business_ledgers": sorted(business_ledgers),
        },
        "blob_refs": sorted(blob_refs),
        "relations_digest": canonical_digest({"edges": []}),
        "policy": {
            "successful_artifacts_only": True,
            "secrets_included": False,
            "secret_scan_exceptions": [],
            "runtime_state_included": False,
        },
        "completeness": {
            "terminal_steps": 0,
            "manifests_seen": 0,
            "manifests_missing": 0,
            "manifests_excluded": 0,
            "ai_config_complete": True,
            "user_config_complete": True,
            "secret_scan_complete": True,
            "media_self_contained": True,
            "external_media_roots": [],
            "portable_ready": True,
            "readiness_reasons": [],
        },
    }


def seed_repository(root: Path) -> dict:
    """标准场景:一个 Job/Part、一个 source blob、一个 snapshot、ref latest。"""
    repo = ContentRepository.create(root)
    blob = repo.put_blob_bytes(BLOB_SHARED)
    job = repo.put_record("job_core", build_job_core("job_alpha"))
    part = repo.put_record("part_core", build_part_core("pt_alpha1", "job_alpha"))
    sr = repo.put_record(
        "step_result",
        build_step_result("job_alpha", "pt_alpha1", {"input/source.mp4": BLOB_SHARED}),
    )
    snapshot_body = build_snapshot(
        jobs=[job.digest], parts=[part.digest], step_results=[sr.digest],
        blob_refs=[blob.digest],
    )
    snapshot = repo.put_snapshot(snapshot_body)
    repo.set_ref("latest", snapshot.digest)
    return {
        "repo": repo,
        "blob": blob,
        "job": job,
        "part": part,
        "sr": sr,
        "snapshot": snapshot,
        "snapshot_body": snapshot_body,
    }


def count_files(root: Path, subdir: str) -> int:
    base = root / subdir
    return sum(1 for path in base.rglob("*") if path.is_file()) if base.is_dir() else 0


def flip_last_byte(path: Path) -> None:
    data = bytearray(path.read_bytes())
    data[-1] ^= 0x01
    path.write_bytes(bytes(data))


class TestLifecycle:
    def test_create_and_open(self, tmp_path):
        repo = ContentRepository.create(tmp_path / "repo")
        assert (repo.root / "repository.json").is_file()
        assert ContentRepository.open(repo.root).root == repo.root

    def test_open_non_repository_rejected(self, tmp_path):
        with pytest.raises(RepositoryError, match="not a portable"):
            ContentRepository.open(tmp_path)

    def test_create_refuses_nonempty_dir(self, tmp_path):
        (tmp_path / "existing.txt").write_text("x")
        with pytest.raises(RepositoryError, match="not empty"):
            ContentRepository.create(tmp_path)

    def test_unknown_format_version_rejected(self, tmp_path):
        repo = ContentRepository.create(tmp_path / "repo")
        (repo.root / "repository.json").write_bytes(
            canonical_json_bytes({"format": "flori-portable-repository/v2"})
        )
        with pytest.raises(RepositoryError, match="unsupported repository format"):
            ContentRepository.open(repo.root)

    def test_open_rejects_symlinked_repository_directory(self, tmp_path):
        repo = ContentRepository.create(tmp_path / "repo")
        real_tmp = repo.root / "tmp-real"
        (repo.root / "tmp").rename(real_tmp)
        (repo.root / "tmp").symlink_to(real_tmp, target_is_directory=True)
        with pytest.raises(RepositoryError, match="missing or unsafe"):
            ContentRepository.open(repo.root)

    def test_create_and_open_reject_symlinked_ancestor(self, tmp_path):
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real, target_is_directory=True)
        with pytest.raises(RepositoryError, match="symlink component"):
            ContentRepository.create(link / "new-repo")

        repo = ContentRepository.create(real / "existing-repo")
        with pytest.raises(RepositoryError, match="symlink component"):
            ContentRepository.open(link / repo.root.name)

    def test_clean_tmp_unlinks_symlink_without_touching_target(self, tmp_path):
        repo = ContentRepository.create(tmp_path / "repo")
        target = tmp_path / "target"
        target.write_bytes(b"protected")
        link = repo.tmp_dir / "spool-known"
        link.symlink_to(target)
        assert repo.clean_tmp() == 1
        assert not link.exists()
        assert target.read_bytes() == b"protected"

    def test_new_directories_fsync_their_parent(self, tmp_path, monkeypatch):
        seen: list[Path] = []
        real_fsync = content_repository._fsync_dir

        def observe(path: Path) -> None:
            seen.append(Path(path))
            real_fsync(path)

        monkeypatch.setattr(content_repository, "_fsync_dir", observe)
        root = tmp_path / "nested" / "repo"
        repo = ContentRepository.create(root)
        put = repo.put_blob_bytes(BLOB_SHARED)
        assert tmp_path in seen
        assert root in seen
        assert repo.blob_path(put.digest).parent.parent in seen


class TestBlobs:
    def test_put_idempotent_single_copy(self, tmp_path):
        repo = ContentRepository.create(tmp_path / "repo")
        first = repo.put_blob_bytes(BLOB_SHARED)
        second = repo.put_blob_bytes(BLOB_SHARED)
        assert first.created and not second.created
        assert first.digest == second.digest == sha(BLOB_SHARED)
        assert count_files(repo.root, "blobs") == 1
        assert count_files(repo.root, "tmp") == 0
        assert repo.read_blob(first.digest) == BLOB_SHARED

    def test_same_digest_different_bytes_is_corruption(self, tmp_path):
        repo = ContentRepository.create(tmp_path / "repo")
        put = repo.put_blob_bytes(BLOB_SHARED)
        blob_file = repo.blob_path(put.digest)
        blob_file.write_bytes(b"EVIL")
        with pytest.raises(RepositoryCorruptionError, match="refusing to overwrite"):
            repo.put_blob_bytes(BLOB_SHARED)
        # 损坏对象保持原样等待人工处理,不得被同 digest 覆盖修复
        assert blob_file.read_bytes() == b"EVIL"

    def test_bit_flip_detected(self, tmp_path):
        repo = ContentRepository.create(tmp_path / "repo")
        put = repo.put_blob_bytes(BLOB_SHARED)
        flip_last_byte(repo.blob_path(put.digest))
        with pytest.raises(RepositoryCorruptionError, match="hash mismatch"):
            repo.verify_blob(put.digest)
        with pytest.raises(RepositoryCorruptionError):
            repo.read_blob(put.digest)

    def test_put_file_streams_and_rejects_symlink(self, tmp_path):
        repo = ContentRepository.create(tmp_path / "repo")
        source = tmp_path / "media.mp4"
        source.write_bytes(BLOB_ONLY_1)
        put = repo.put_blob_file(source)
        assert put.digest == sha(BLOB_ONLY_1)
        assert put.size_bytes == len(BLOB_ONLY_1)
        assert not repo.put_blob_file(source).created
        link = tmp_path / "media-link.mp4"
        link.symlink_to(source)
        with pytest.raises(RepositoryError, match="symlink"):
            repo.put_blob_file(link)

    def test_adopt_blob_file_links_from_tmp(self, tmp_path):
        """C6:仓库 tmp/ 内的 spool 直接 link 成 blob,省第二遍拷贝。"""
        repo = ContentRepository.create(tmp_path / "repo")
        spool = repo.tmp_dir / "spool-x"
        spool.write_bytes(BLOB_ONLY_1)
        put = repo.adopt_blob_file(spool)
        assert put.created and put.digest == sha(BLOB_ONLY_1)
        assert put.size_bytes == len(BLOB_ONLY_1)
        assert not spool.exists(), "adopt 后 spool 必须被消费"
        assert repo.read_blob(put.digest) == BLOB_ONLY_1

    def test_adopt_blob_file_rejects_outside_tmp(self, tmp_path):
        repo = ContentRepository.create(tmp_path / "repo")
        outside = tmp_path / "outside.bin"
        outside.write_bytes(BLOB_ONLY_2)
        with pytest.raises(RepositoryError, match="must live in"):
            repo.adopt_blob_file(outside)
        assert outside.exists()

    def test_adopt_existing_digest_is_noop(self, tmp_path):
        repo = ContentRepository.create(tmp_path / "repo")
        first = repo.put_blob_bytes(BLOB_SHARED)
        spool = repo.tmp_dir / "spool-dup"
        spool.write_bytes(BLOB_SHARED)
        second = repo.adopt_blob_file(spool)
        assert second.digest == first.digest and not second.created
        assert not spool.exists()
        assert count_files(repo.root, "blobs") == 1

    def test_missing_blob_reported(self, tmp_path):
        repo = ContentRepository.create(tmp_path / "repo")
        with pytest.raises(RepositoryError, match="not found"):
            repo.read_blob(sha(b"never-stored"))
        with pytest.raises(RepositoryError, match="digest"):
            repo.blob_path("sha256:XYZ")

    def test_copy_blob_to_streams_and_verifies(self, tmp_path):
        repo = ContentRepository.create(tmp_path / "repo")
        put = repo.put_blob_bytes(BLOB_SHARED)
        dest = tmp_path / "restore" / "source.mp4"
        assert repo.copy_blob_to(put.digest, dest) == len(BLOB_SHARED)
        assert dest.read_bytes() == BLOB_SHARED
        # 边流边验:仓库内位翻转 -> 半成品目标被清除并报损坏
        flip_last_byte(repo.blob_path(put.digest))
        broken_dest = tmp_path / "restore" / "broken.mp4"
        with pytest.raises(RepositoryCorruptionError, match="hash mismatch"):
            repo.copy_blob_to(put.digest, broken_dest)
        assert not broken_dest.exists()

    def test_open_blob_stream_reads_raw_bytes(self, tmp_path):
        repo = ContentRepository.create(tmp_path / "repo")
        put = repo.put_blob_bytes(BLOB_ONLY_1)
        with repo.open_blob_stream(put.digest) as stream:
            assert stream.read() == BLOB_ONLY_1
        with pytest.raises(RepositoryError, match="not found"):
            repo.open_blob_stream(sha(b"never-stored"))


class TestRecords:
    def test_digest_stable_across_key_order(self, tmp_path):
        repo = ContentRepository.create(tmp_path / "repo")
        body = build_job_core("job_alpha")
        first = repo.put_record("job_core", body)
        second = repo.put_record("job_core", dict(reversed(list(body.items()))))
        assert first.digest == second.digest
        assert first.created and not second.created
        assert count_files(repo.root, "records") == 1
        assert repo.get_record("job_core", first.digest) == body

    def test_policy_gate_applies_on_put(self, tmp_path):
        repo = ContentRepository.create(tmp_path / "repo")
        body = build_job_core("job_alpha")
        body["status"] = "done"
        with pytest.raises(PolicyError, match="allowlist"):
            repo.put_record("job_core", body)
        assert count_files(repo.root, "records") == 0

    def test_tampered_record_detected(self, tmp_path):
        repo = ContentRepository.create(tmp_path / "repo")
        put = repo.put_record("job_core", build_job_core("job_alpha"))
        path = repo.root / "records" / "job_core" / f"{put.digest.split(':')[1]}.json"
        other = canonical_json_bytes(build_job_core("job_beta"))
        path.write_bytes(other)
        with pytest.raises(RepositoryCorruptionError, match="digest mismatch"):
            repo.get_record("job_core", put.digest)

    def test_non_canonical_stored_bytes_detected(self, tmp_path):
        repo = ContentRepository.create(tmp_path / "repo")
        put = repo.put_record("job_core", build_job_core("job_alpha"))
        path = repo.root / "records" / "job_core" / f"{put.digest.split(':')[1]}.json"
        import json

        path.write_bytes(json.dumps(build_job_core("job_alpha"), indent=2).encode())
        with pytest.raises(RepositoryCorruptionError, match="not canonical"):
            repo.get_record("job_core", put.digest)

    def test_stored_policy_violation_is_corruption(self, tmp_path):
        repo = ContentRepository.create(tmp_path / "repo")
        bad = build_job_core("job_alpha")
        bad["status"] = "done"
        encoded = canonical_json_bytes(bad)
        digest_hex = hashlib.sha256(encoded).hexdigest()
        target = repo.root / "records" / "job_core"
        target.mkdir(parents=True, exist_ok=True)
        (target / f"{digest_hex}.json").write_bytes(encoded)
        with pytest.raises(RepositoryCorruptionError, match="allowlist"):
            repo.get_record("job_core", f"sha256:{digest_hex}")

    def test_unknown_kind_rejected(self, tmp_path):
        repo = ContentRepository.create(tmp_path / "repo")
        with pytest.raises(PolicyError, match="not defined"):
            repo.put_record("nope", {})
        with pytest.raises(RepositoryError, match="not defined"):
            repo.has_record("nope", HEX_A)


class TestSnapshots:
    def test_legacy_v1_snapshot_is_readable_but_not_portable_ready(self, tmp_path):
        repo = ContentRepository.create(tmp_path / "repo")
        legacy = build_snapshot()
        legacy["format"] = LEGACY_SNAPSHOT_FORMAT
        del legacy["completeness"]
        raw = canonical_json_bytes(legacy)
        digest = sha(raw)
        target = repo._snapshot_path(digest)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        loaded = repo.get_snapshot(digest)
        assert loaded["format"] == LEGACY_SNAPSHOT_FORMAT
        assert loaded["completeness"]["portable_ready"] is False
        assert loaded["completeness"]["readiness_reasons"] == [
            "legacy_snapshot_without_completeness",
        ]

    def test_repeat_backup_zero_growth(self, tmp_path):
        ctx = seed_repository(tmp_path / "repo")
        repo = ctx["repo"]
        before = {sub: count_files(repo.root, sub) for sub in ("blobs", "records", "snapshots")}
        # 同一逻辑状态整轮重放:blob/record/snapshot 均 no-op
        assert not repo.put_blob_bytes(BLOB_SHARED).created
        assert not repo.put_record("job_core", build_job_core("job_alpha")).created
        again = repo.put_snapshot(ctx["snapshot_body"])
        assert not again.created and again.digest == ctx["snapshot"].digest
        after = {sub: count_files(repo.root, sub) for sub in ("blobs", "records", "snapshots")}
        assert after == before
        # 只有 receipt 可以增长
        repo.write_receipt({
            "run_id": "run_1", "observed_at": "2026-07-18T06:00:00Z",
            "outcome": "success", "snapshot_digest": ctx["snapshot"].digest,
            "hit_existing_snapshot": True,
        })
        assert count_files(repo.root, "receipts") == 1

    def test_same_bytes_two_references_single_blob(self, tmp_path):
        ctx = seed_repository(tmp_path / "repo")
        repo = ctx["repo"]
        job2 = repo.put_record("job_core", build_job_core("job_beta"))
        part2 = repo.put_record("part_core", build_part_core("pt_beta1", "job_beta"))
        sr2 = repo.put_record(
            "step_result",
            build_step_result("job_beta", "pt_beta1", {"input/source.mp4": BLOB_SHARED}),
        )
        snapshot = repo.put_snapshot(build_snapshot(
            jobs=sorted([ctx["job"].digest, job2.digest]),
            parts=sorted([ctx["part"].digest, part2.digest]),
            step_results=sorted([ctx["sr"].digest, sr2.digest]),
            blob_refs=[ctx["blob"].digest],
        ))
        assert count_files(repo.root, "blobs") == 1
        body = repo.get_snapshot(snapshot.digest)
        assert body["blob_refs"] == [ctx["blob"].digest]
        for digest in body["records"]["step_results"]:
            record = repo.get_record("step_result", digest)
            assert record["output_blobs"]["input/source.mp4"] == ctx["blob"].digest
        assert repo.read_blob(ctx["blob"].digest) == BLOB_SHARED

    def test_snapshot_digest_matches_put(self, tmp_path):
        ctx = seed_repository(tmp_path / "repo")
        assert ctx["repo"].snapshot_digest(ctx["snapshot_body"]) == ctx["snapshot"].digest

    def test_missing_record_ref_rejected(self, tmp_path):
        repo = ContentRepository.create(tmp_path / "repo")
        with pytest.raises(RepositoryError, match="record .* not found"):
            repo.put_snapshot(build_snapshot(jobs=[HEX_A]))
        assert count_files(repo.root, "snapshots") == 0

    def test_missing_blob_rejected(self, tmp_path):
        repo = ContentRepository.create(tmp_path / "repo")
        job = repo.put_record("job_core", build_job_core("job_alpha"))
        part = repo.put_record("part_core", build_part_core("pt_alpha1", "job_alpha"))
        sr = repo.put_record(
            "step_result",
            build_step_result("job_alpha", "pt_alpha1", {"input/source.mp4": BLOB_ONLY_1}),
        )
        with pytest.raises(RepositoryError, match="blob .* not found"):
            repo.put_snapshot(build_snapshot(
                jobs=[job.digest], parts=[part.digest], step_results=[sr.digest],
                blob_refs=[sha(BLOB_ONLY_1)],
            ))

    def test_blob_refs_must_equal_record_references(self, tmp_path):
        ctx = seed_repository(tmp_path / "repo")
        repo = ctx["repo"]
        extra = repo.put_blob_bytes(BLOB_ONLY_1)
        body = build_snapshot(
            jobs=[ctx["job"].digest], parts=[ctx["part"].digest],
            step_results=[ctx["sr"].digest],
            blob_refs=sorted([ctx["blob"].digest, extra.digest]),
        )
        with pytest.raises(RepositoryError, match="must equal record-referenced"):
            repo.put_snapshot(body)
        body = build_snapshot(
            jobs=[ctx["job"].digest], parts=[ctx["part"].digest],
            step_results=[ctx["sr"].digest], blob_refs=[],
        )
        with pytest.raises(RepositoryError, match="must equal record-referenced"):
            repo.put_snapshot(body)

    def test_determinism_gates(self, tmp_path):
        ctx = seed_repository(tmp_path / "repo")
        repo = ctx["repo"]
        with_time = dict(ctx["snapshot_body"])
        with_time["observed_at"] = "2026-07-18T06:00:00Z"
        with pytest.raises(RepositoryError, match="keys must be exactly"):
            repo.put_snapshot(with_time)

        unsorted_refs = dict(ctx["snapshot_body"])
        unsorted_refs["blob_refs"] = [ctx["blob"].digest, ctx["blob"].digest]
        with pytest.raises(RepositoryError, match="ascending"):
            repo.put_snapshot(unsorted_refs)

    def test_policy_flags_hard_gate(self, tmp_path):
        ctx = seed_repository(tmp_path / "repo")
        repo = ctx["repo"]
        leaked = dict(ctx["snapshot_body"])
        leaked["policy"] = dict(leaked["policy"], secrets_included=True)
        with pytest.raises(RepositoryError, match="snapshot.policy"):
            repo.put_snapshot(leaked)
        punned = dict(ctx["snapshot_body"])
        punned["policy"] = dict(punned["policy"], successful_artifacts_only=1)
        with pytest.raises(RepositoryError, match="snapshot.policy"):
            repo.put_snapshot(punned)

    def test_secret_exceptions_must_be_disclosed_not_asserted_away(self, tmp_path):
        """放行过密钥扫描却仍声称 secrets_included=false,正是这条门要挡的快照。"""
        ctx = seed_repository(tmp_path / "repo")
        repo = ctx["repo"]

        lying = dict(ctx["snapshot_body"])
        lying["policy"] = dict(
            lying["policy"], secret_scan_exceptions=["job_a:input/metadata.json"],
        )
        with pytest.raises(RepositoryError, match="secrets_included must be true"):
            repo.put_snapshot(lying)

        # 反向同样是谎:没放行任何东西却承认带了密钥。
        overclaiming = dict(ctx["snapshot_body"])
        overclaiming["policy"] = dict(overclaiming["policy"], secrets_included=True)
        with pytest.raises(RepositoryError, match="secrets_included must be true"):
            repo.put_snapshot(overclaiming)

        honest = dict(ctx["snapshot_body"])
        honest["policy"] = dict(
            honest["policy"],
            secrets_included=True,
            secret_scan_exceptions=["job_a:input/metadata.json"],
        )
        honest["completeness"] = dict(
            honest["completeness"],
            portable_ready=False,
            readiness_reasons=["secret_scan_exceptions"],
        )
        published = repo.put_snapshot(honest)
        assert repo.get_snapshot(published.digest)["policy"]["secret_scan_exceptions"] == [
            "job_a:input/metadata.json"
        ]
        # 清单进 digest:改动放行范围必须是另一个快照,不能就地翻供。
        assert published.digest != ctx["snapshot"].digest

    def test_secret_exception_list_must_be_sorted_and_unique(self, tmp_path):
        ctx = seed_repository(tmp_path / "repo")
        body = dict(ctx["snapshot_body"])
        body["policy"] = dict(
            body["policy"],
            secrets_included=True,
            secret_scan_exceptions=["job_b:o.json", "job_a:o.json"],
        )
        with pytest.raises(RepositoryError, match="strictly ascending"):
            ctx["repo"].put_snapshot(body)

    def test_selector_is_part_of_identity(self, tmp_path):
        """A5:同一记录集合的全量与局部快照必须是两个不同 digest。"""
        ctx = seed_repository(tmp_path / "repo")
        repo = ctx["repo"]
        partial_body = dict(ctx["snapshot_body"])
        partial_body["selector"] = {"partial": True, "job_ids": ["job_alpha"]}
        partial = repo.put_snapshot(partial_body)
        assert partial.digest != ctx["snapshot"].digest
        assert repo.get_snapshot(partial.digest)["selector"]["partial"] is True

    def test_selector_consistency_enforced(self, tmp_path):
        ctx = seed_repository(tmp_path / "repo")
        repo = ctx["repo"]
        body = dict(ctx["snapshot_body"])
        body["selector"] = {"partial": False, "job_ids": ["job_alpha"]}
        with pytest.raises(RepositoryError, match="partial must be true exactly"):
            repo.put_snapshot(body)
        body["selector"] = {"partial": True, "job_ids": []}
        with pytest.raises(RepositoryError, match="partial must be true exactly"):
            repo.put_snapshot(body)
        body["selector"] = {"partial": True, "job_ids": ["b_job", "a_job"]}
        with pytest.raises(RepositoryError, match="sorted and unique"):
            repo.put_snapshot(body)

    def test_manifest_format_pinned(self, tmp_path):
        ctx = seed_repository(tmp_path / "repo")
        body = dict(ctx["snapshot_body"])
        body["source"] = dict(body["source"], manifest_format="flori-step-manifest/v9")
        with pytest.raises(RepositoryError, match="manifest_format"):
            ctx["repo"].put_snapshot(body)

    def test_failure_event_refs_must_be_listed_in_ledgers(self, tmp_path):
        """failure_event 的审计引用是 record->record 边:悬空或未列入分组都拒绝。"""
        repo = ContentRepository.create(tmp_path / "repo")
        usage = repo.put_record("ai_usage", build_ai_usage("exec_fail_1"))
        fe = repo.put_record(
            "failure_event",
            build_failure_event("exec_fail_1", usage_refs=[usage.digest]),
        )
        # 引用的 ai_usage 存在但未列入 business_ledgers -> 拒绝
        with pytest.raises(RepositoryError, match="listed in business_ledgers"):
            repo.put_snapshot(build_snapshot(failures=[fe.digest]))
        # 列入后接受
        accepted = repo.put_snapshot(build_snapshot(
            failures=[fe.digest], business_ledgers=[usage.digest],
        ))
        assert repo.has_snapshot(accepted.digest)

    def test_failure_event_dangling_ref_rejected(self, tmp_path):
        repo = ContentRepository.create(tmp_path / "repo")
        dangling = sha(b"never-stored-usage-record")
        fe = repo.put_record(
            "failure_event",
            build_failure_event("exec_fail_2", usage_refs=[dangling]),
        )
        with pytest.raises(RepositoryError, match="listed in business_ledgers"):
            repo.put_snapshot(build_snapshot(failures=[fe.digest]))

    def test_failure_event_ref_wrong_kind_rejected(self, tmp_path):
        # 列入 business_ledgers 但不是 ai_usage record -> 仍拒绝
        repo = ContentRepository.create(tmp_path / "repo")
        glossary = repo.put_record("glossary", {"domain": "ml", "term": "cnn"})
        fe = repo.put_record(
            "failure_event",
            build_failure_event("exec_fail_3", usage_refs=[glossary.digest]),
        )
        with pytest.raises(RepositoryError, match="must be a ai_usage record"):
            repo.put_snapshot(build_snapshot(
                failures=[fe.digest], business_ledgers=[glossary.digest],
            ))


class TestRefs:
    def test_roundtrip_and_listing(self, tmp_path):
        ctx = seed_repository(tmp_path / "repo")
        repo = ctx["repo"]
        assert repo.get_ref("latest") == ctx["snapshot"].digest
        repo.set_ref("monthly-2026-07", ctx["snapshot"].digest)
        assert repo.list_refs() == {
            "latest": ctx["snapshot"].digest,
            "monthly-2026-07": ctx["snapshot"].digest,
        }
        repo.delete_ref("monthly-2026-07")
        assert "monthly-2026-07" not in repo.list_refs()

    def test_ref_not_updated_on_missing_snapshot(self, tmp_path):
        ctx = seed_repository(tmp_path / "repo")
        repo = ctx["repo"]
        with pytest.raises(RepositoryError, match="not found"):
            repo.set_ref("latest", "sha256:" + "f" * 64)
        assert repo.get_ref("latest") == ctx["snapshot"].digest

    def test_ref_not_updated_on_corrupt_snapshot(self, tmp_path):
        # 用新 open 的实例:同实例刚 put 过的 snapshot 走进程内信任通道
        ctx = seed_repository(tmp_path / "repo")
        empty = ctx["repo"].put_snapshot(build_snapshot())
        flip_last_byte(
            ctx["repo"].root / "snapshots" / f"{empty.digest.split(':')[1]}.json"
        )
        fresh = ContentRepository.open(ctx["repo"].root)
        with pytest.raises(RepositoryCorruptionError):
            fresh.set_ref("latest", empty.digest)
        assert fresh.get_ref("latest") == ctx["snapshot"].digest

    def test_set_ref_trusts_same_instance_verification(self, tmp_path):
        """put_snapshot 刚验完的闭包结论由 set_ref 复用;新实例无此信任,全量重验。"""
        ctx = seed_repository(tmp_path / "repo")
        repo = ctx["repo"]
        job2 = repo.put_record("job_core", build_job_core("job_beta"))
        snap2 = repo.put_snapshot(build_snapshot(jobs=[job2.digest]))
        os.unlink(
            repo.root / "records" / "job_core" / f"{job2.digest.split(':')[1]}.json"
        )
        # 同实例:信任通道生效,只查 snapshot 存在性
        repo.set_ref("candidate", snap2.digest)
        # 跨进程等价路径:闭包重验发现 record 缺失,fail-closed
        fresh = ContentRepository.open(repo.root)
        with pytest.raises(RepositoryCorruptionError):
            fresh.set_ref("candidate2", snap2.digest)

    @pytest.mark.parametrize("name", ["../x", "a/b", "", ".hidden", "a" * 200])
    def test_invalid_ref_names_rejected(self, tmp_path, name):
        ctx = seed_repository(tmp_path / "repo")
        with pytest.raises(RepositoryError, match="invalid"):
            ctx["repo"].set_ref(name, ctx["snapshot"].digest)


class TestReceipts:
    def test_write_read_find(self, tmp_path):
        ctx = seed_repository(tmp_path / "repo")
        repo = ctx["repo"]
        receipt_id = repo.write_receipt({
            "run_id": "run_alpha", "observed_at": "2026-07-18T06:00:00Z",
            "outcome": "success", "snapshot_digest": ctx["snapshot"].digest,
            "stats": {"jobs_seen": 1, "steps_done": 1},
            "source_instance": "nas-main",
        })
        assert repo.read_receipt(receipt_id)["run_id"] == "run_alpha"
        assert len(repo.find_receipts("run_alpha")) == 1
        assert repo.find_receipts("run_other") == []

    def test_success_requires_existing_snapshot(self, tmp_path):
        ctx = seed_repository(tmp_path / "repo")
        with pytest.raises(RepositoryError, match="requires snapshot_digest"):
            ctx["repo"].write_receipt({
                "run_id": "r1", "observed_at": "2026-07-18T06:00:00Z",
                "outcome": "success",
            })
        with pytest.raises(RepositoryError, match="not found"):
            ctx["repo"].write_receipt({
                "run_id": "r1", "observed_at": "2026-07-18T06:00:00Z",
                "outcome": "success", "snapshot_digest": "sha256:" + "e" * 64,
            })

    def test_unknown_key_and_secrets_rejected(self, tmp_path):
        ctx = seed_repository(tmp_path / "repo")
        with pytest.raises(RepositoryError, match="unknown"):
            ctx["repo"].write_receipt({
                "run_id": "r1", "observed_at": "2026-07-18T06:00:00Z",
                "outcome": "failed", "hostname": "nas",
            })
        with pytest.raises(RepositoryError, match="credential"):
            ctx["repo"].write_receipt({
                "run_id": "r1", "observed_at": "2026-07-18T06:00:00Z",
                "outcome": "failed", "stats": {"api_key": "x"},
            })

    def test_receipt_id_orders_by_true_time(self, tmp_path):
        """前缀是零填充 epoch 微秒:小数秒与 +00:00 写法都按真实时刻排序。"""
        repo = ContentRepository.create(tmp_path / "repo")
        observed = {
            "half": "2026-07-18T06:00:00.5Z",
            "early": "2026-07-18T05:59:59Z",
            "offset": "2026-07-18T06:00:00+00:00",
            "zulu": "2026-07-18T06:00:00Z",
        }
        ids = {
            key: repo.write_receipt({
                "run_id": f"run_{key}", "observed_at": value, "outcome": "failed",
            })
            for key, value in observed.items()  # 故意乱序写入
        }
        prefix = {key: value.split("-")[0] for key, value in ids.items()}
        # 同一时刻的两种写法归一到同一前缀
        assert prefix["offset"] == prefix["zulu"]
        assert prefix["early"] < prefix["zulu"] < prefix["half"]
        listed = repo.list_receipts()
        assert listed[0] == ids["early"] and listed[-1] == ids["half"]

    def test_receipt_readable_after_snapshot_gone(self, tmp_path):
        repo = ContentRepository.create(tmp_path / "repo")
        empty = repo.put_snapshot(build_snapshot())
        receipt_id = repo.write_receipt({
            "run_id": "r1", "observed_at": "2026-07-18T06:00:00Z",
            "outcome": "success", "snapshot_digest": empty.digest,
        })
        os.unlink(repo.root / "snapshots" / f"{empty.digest.split(':')[1]}.json")
        assert repo.read_receipt(receipt_id)["snapshot_digest"] == empty.digest


class TestWriteLock:
    def test_mutual_exclusion(self, tmp_path):
        repo = ContentRepository.create(tmp_path / "repo")
        with repo.write_lock("backup"):
            assert repo.write_lock_holder()["owner"] == "backup"
            with pytest.raises(RepositoryLockError, match="held by"):
                with repo.write_lock("gc"):
                    pass
        assert repo.write_lock_holder() is None
        with repo.write_lock("gc"):
            pass

    def test_released_on_exception(self, tmp_path):
        repo = ContentRepository.create(tmp_path / "repo")
        with pytest.raises(RuntimeError):
            with repo.write_lock("backup"):
                raise RuntimeError("boom")
        assert repo.write_lock_holder() is None

    def test_holder_payload_has_operator_fields(self, tmp_path):
        repo = ContentRepository.create(tmp_path / "repo")
        with repo.write_lock("backup"):
            holder = repo.write_lock_holder()
            assert set(holder) == {"owner", "pid", "host", "acquired_at", "token"}
            assert holder["owner"] == "backup"

    def test_release_failure_does_not_mask_original_exception(self, tmp_path):
        repo = ContentRepository.create(tmp_path / "repo")
        with pytest.raises(RuntimeError, match="boom"):
            with repo.write_lock("backup"):
                os.unlink(repo.root / "locks" / "write.lock")
                raise RuntimeError("boom")

    def test_success_path_release_failure_raises_lock_error(self, tmp_path):
        repo = ContentRepository.create(tmp_path / "repo")
        with pytest.raises(RepositoryLockError, match="disappeared"):
            with repo.write_lock("backup"):
                os.unlink(repo.root / "locks" / "write.lock")

    def test_break_write_lock(self, tmp_path):
        repo = ContentRepository.create(tmp_path / "repo")
        with pytest.raises(RepositoryLockError, match="not held"):
            repo.break_write_lock()
        fd = os.open(repo.root / "locks" / "write.lock", os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
        repo.break_write_lock()
        assert repo.write_lock_holder() is None


class TestGCMark:
    def build_two_snapshot_repo(self, root: Path) -> dict:
        """snap1 引 shared+only1,snap2 引 shared+only2;ref latest 只指 snap2。"""
        repo = ContentRepository.create(root)
        digests = {}
        for name, job_id, part_id, data in (
            ("one", "job_alpha", "pt_alpha1", {"input/a.mp4": BLOB_SHARED, "input/b.srt": BLOB_ONLY_1}),
            ("two", "job_beta", "pt_beta1", {"input/a.mp4": BLOB_SHARED, "input/b.srt": BLOB_ONLY_2}),
        ):
            for blob in set(data.values()):
                repo.put_blob_bytes(blob)
            job = repo.put_record("job_core", build_job_core(job_id))
            part = repo.put_record("part_core", build_part_core(part_id, job_id))
            sr = repo.put_record("step_result", build_step_result(job_id, part_id, data))
            snapshot = repo.put_snapshot(build_snapshot(
                jobs=[job.digest], parts=[part.digest], step_results=[sr.digest],
                blob_refs=sorted({sha(item) for item in data.values()}),
            ))
            digests[name] = {"snapshot": snapshot.digest, "records": {
                ("job_core", job.digest), ("part_core", part.digest),
                ("step_result", sr.digest),
            }}
        repo.set_ref("latest", digests["two"]["snapshot"])
        repo.write_receipt({
            "run_id": "run_one", "observed_at": "2026-07-18T05:00:00Z",
            "outcome": "success", "snapshot_digest": digests["one"]["snapshot"],
        })
        return {"repo": repo, **digests}

    def test_all_roots_reachable_by_default(self, tmp_path):
        ctx = self.build_two_snapshot_repo(tmp_path / "repo")
        plan = ctx["repo"].gc_mark()
        assert set(plan.reachable_snapshots) == {
            ctx["one"]["snapshot"], ctx["two"]["snapshot"],
        }
        assert plan.unreachable_snapshots == ()
        assert plan.unreachable_records == ()
        assert plan.unreachable_blobs == ()

    def test_dropping_receipt_roots_exposes_candidates(self, tmp_path):
        ctx = self.build_two_snapshot_repo(tmp_path / "repo")
        plan = ctx["repo"].gc_mark(receipt_root_limit=0)
        assert plan.reachable_snapshots == (ctx["two"]["snapshot"],)
        assert set(plan.unreachable_snapshots) == {ctx["one"]["snapshot"]}
        assert set(plan.unreachable_records) == ctx["one"]["records"]
        # 共享 blob 必须存活,只有 snap1 独占的 blob 成为候选
        assert plan.unreachable_blobs == (sha(BLOB_ONLY_1),)
        assert sha(BLOB_SHARED) in plan.reachable_blobs

    def test_dry_run_is_deterministic(self, tmp_path):
        ctx = self.build_two_snapshot_repo(tmp_path / "repo")
        assert ctx["repo"].gc_mark(receipt_root_limit=0) == \
            ctx["repo"].gc_mark(receipt_root_limit=0)

    def test_broken_ref_is_fatal(self, tmp_path):
        ctx = self.build_two_snapshot_repo(tmp_path / "repo")
        repo = ctx["repo"]
        os.unlink(repo.root / "snapshots" / f"{ctx['two']['snapshot'].split(':')[1]}.json")
        with pytest.raises(RepositoryCorruptionError, match="missing snapshot"):
            repo.gc_mark()

    def test_receipt_to_missing_snapshot_is_warning(self, tmp_path):
        repo = ContentRepository.create(tmp_path / "repo")
        empty = repo.put_snapshot(build_snapshot())
        repo.write_receipt({
            "run_id": "r1", "observed_at": "2026-07-18T05:00:00Z",
            "outcome": "success", "snapshot_digest": empty.digest,
        })
        os.unlink(repo.root / "snapshots" / f"{empty.digest.split(':')[1]}.json")
        plan = repo.gc_mark()
        assert plan.reachable_snapshots == ()
        assert any("missing snapshot" in warning for warning in plan.warnings)

    def test_receipt_retention_order_follows_true_time(self, tmp_path):
        """保留窗口按真实时刻裁剪:小数秒/+00:00 写法不得扰乱 GC 的最近 N 条。"""
        repo = ContentRepository.create(tmp_path / "repo")
        observed = [
            ("half", "2026-07-18T06:00:00.5Z"),
            ("early", "2026-07-18T05:59:59Z"),
            ("offset", "2026-07-18T06:00:00+00:00"),
            ("zulu", "2026-07-18T06:00:00Z"),
        ]  # 故意乱序写入
        snapshots = {}
        for key, moment in observed:
            body = build_snapshot()
            body["source"] = dict(body["source"], app_version=f"2.2.0-{key}")
            snapshots[key] = repo.put_snapshot(body).digest
            repo.write_receipt({
                "run_id": f"run_{key}", "observed_at": moment,
                "outcome": "success", "snapshot_digest": snapshots[key],
            })
        # 只留最近 1 条:必须是 06:00:00.5Z 那条,不受写入顺序影响
        plan = repo.gc_mark(receipt_root_limit=1)
        assert plan.reachable_snapshots == (snapshots["half"],)
        # 只留最近 3 条:唯一被裁掉的是 05:59:59Z
        plan = repo.gc_mark(receipt_root_limit=3)
        assert set(plan.unreachable_snapshots) == {snapshots["early"]}

    def test_gc_keeps_failure_audit_refs_reachable(self, tmp_path):
        repo = ContentRepository.create(tmp_path / "repo")
        usage = repo.put_record("ai_usage", build_ai_usage("exec_fail_1"))
        fe = repo.put_record(
            "failure_event",
            build_failure_event("exec_fail_1", usage_refs=[usage.digest]),
        )
        snapshot = repo.put_snapshot(build_snapshot(
            failures=[fe.digest], business_ledgers=[usage.digest],
        ))
        repo.set_ref("latest", snapshot.digest)
        plan = repo.gc_mark()
        assert ("ai_usage", usage.digest) in plan.reachable_records
        assert ("failure_event", fe.digest) in plan.reachable_records
        assert plan.unreachable_records == ()


class TestScrub:
    def test_clean_repository_passes(self, tmp_path):
        ctx = seed_repository(tmp_path / "repo")
        report = ctx["repo"].scrub()
        assert report.ok
        assert report.checked_blobs == 1
        assert report.checked_records == 3
        assert report.checked_snapshots == 1
        assert report.checked_refs == 1

    def test_detects_damage_without_modifying(self, tmp_path):
        ctx = seed_repository(tmp_path / "repo")
        repo = ctx["repo"]
        blob_file = repo.blob_path(ctx["blob"].digest)
        flip_last_byte(blob_file)
        (repo.root / "stray.txt").write_text("junk")
        (repo.root / "records" / "job_core" / "evil.txt").write_text("junk")
        (repo.root / "refs" / "broken").write_text("not-a-digest")
        (repo.root / "tmp" / "leftover.bin").write_bytes(b"tmp")
        link = repo.root / "records" / "job_core" / "link.json"
        link.symlink_to(blob_file)

        report = repo.scrub()
        kinds = {issue.kind for issue in report.issues}
        assert {"blob_corrupt", "stray_file", "broken_ref", "tmp_leftover", "symlink"} <= kinds
        # scrub 只读:损坏与杂散文件原样保留
        assert blob_file.exists()
        assert (repo.root / "stray.txt").exists()
        assert link.is_symlink()

    def test_detects_misplaced_blob(self, tmp_path):
        ctx = seed_repository(tmp_path / "repo")
        repo = ctx["repo"]
        wrong_dir = repo.root / "blobs" / "sha256" / "ff"
        wrong_dir.mkdir(parents=True, exist_ok=True)
        hex64 = "0" * 64
        (wrong_dir / hex64).write_bytes(b"misplaced")
        report = repo.scrub()
        assert any(
            issue.kind == "stray_file" and "invalid blob file name" in issue.detail
            for issue in report.issues
        )

    def test_detects_snapshot_with_vanished_record(self, tmp_path):
        ctx = seed_repository(tmp_path / "repo")
        repo = ctx["repo"]
        os.unlink(
            repo.root / "records" / "step_result"
            / f"{ctx['sr'].digest.split(':')[1]}.json"
        )
        report = repo.scrub()
        assert any(issue.kind == "snapshot_corrupt" for issue in report.issues)
        assert any(issue.kind == "broken_ref" for issue in report.issues) is False
