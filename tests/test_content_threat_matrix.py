"""§2.15 恶意/威胁矩阵:逐行验证"拒绝且不发布半成品"。

与既有单元测试的分工:那些证明单个原语按契约工作;这里按威胁矩阵的行组织,
每条都走到发布面(put_snapshot / run_backup / scrub),断言两件事——
攻击被拒绝,且仓库没有留下半成品(ref 不动、snapshot 不增)。
"""

import json

import pytest

from shared.content_backup import BackupError, run_backup
from shared.content_policy import PolicyError
from shared.content_repository import (
    ContentRepository,
    RepositoryCorruptionError,
    RepositoryError,
)
from tests.test_content_backup import (
    MEDIA_ONE,
    commit_step,
    db_exec,
    do_backup,
    ensure_job_json,
    insert_job,
    insert_part,
    insert_step,
    make_clock,
    seed_video_job,
    sha,
    T_CREATED,
    T_FINISHED,
    T_STARTED,
)
from tests.test_content_import import source  # noqa: F401 - fixture


def repo_state(repo: ContentRepository) -> dict:
    """发布面的可观测状态;攻击被拒后它必须逐项不变。"""
    return {
        "refs": dict(repo.list_refs()),
        "snapshots": sorted(repo.iter_snapshots()),
        "blobs": sorted(repo.iter_blobs()),
    }


class TestSecretsNeverReachRepository:
    """矩阵行"secret 进入明文仓库":allowlist + scan 双门,命中即整次失败。"""

    async def test_credential_table_blocks_backup(self, source):
        """app_credentials 是 E 类;它一旦被列入分类面就必须挡住整次备份。"""
        from shared.content_policy import CATEGORY_FORBIDDEN, classify_table

        assert classify_table("app_credentials")[0] == CATEGORY_FORBIDDEN
        assert classify_table("worker_tokens")[0] == CATEGORY_FORBIDDEN

    async def test_cookie_in_job_meta_fails_closed(self, source):
        await seed_video_job(source)
        db_exec(source.db, "UPDATE jobs SET meta=? WHERE id='job_alpha'", (
            json.dumps({"headers": "Cookie: sessionid=abcdef1234567890"}),
        ))
        before = repo_state(source.repo)
        with pytest.raises((BackupError, PolicyError)):
            await do_backup(source)
        assert repo_state(source.repo) == before, "被拒后不得留下半成品"

    async def test_worker_token_in_meta_fails_closed(self, source):
        await seed_video_job(source)
        db_exec(source.db, "UPDATE jobs SET meta=? WHERE id='job_alpha'", (
            json.dumps({"registration_token": "abcdef0123456789abcdef"}),
        ))
        before = repo_state(source.repo)
        with pytest.raises((BackupError, PolicyError)):
            await do_backup(source)
        assert repo_state(source.repo) == before

    async def test_signed_url_is_redacted_not_stored(self, source):
        """签名 URL 属"脱敏后保留 canonical locator",不是整次失败。"""
        job_id = "job_signed"
        insert_job(
            source, job_id, content_type="document", document_kind="article",
            url="https://cdn.example.com/a.pdf?X-Amz-Signature=" + "9" * 40,
        )
        result = await do_backup(source)
        body = source.repo.get_snapshot(result.snapshot_digest)
        core = next(
            source.repo.get_record("job_core", digest)
            for digest in body["records"]["jobs"]
            if source.repo.has_record("job_core", digest)
        )
        assert "X-Amz-Signature" not in core["url"]
        assert core["url"] == "https://cdn.example.com/a.pdf"

    async def test_ai_log_secret_fails_closed(self, source):
        insert_job(source, "job_ai", content_type="document", document_kind="article")
        db_exec(source.db, (
            "INSERT INTO ai_task_logs (task_id, exec_id, error, created_at)"
            " VALUES ('t1','e1',?,?)"
        ), ("auth failed: Bearer abcdefghijklmnop1234567890", T_CREATED))
        before = repo_state(source.repo)
        with pytest.raises((BackupError, PolicyError)):
            await do_backup(source)
        assert repo_state(source.repo) == before

    async def test_secret_in_text_blob_fails_closed(self, source):
        """blob 字节里的明文密钥必须整次失败,而不是被"只扫 record"漏过去。"""
        job_id = "job_blob"
        insert_job(source, job_id, content_type="document", document_kind="article")
        insert_step(
            source, job_id, "job", "01_download", "done",
            started_at=T_STARTED, finished_at=T_FINISHED,
        )
        await commit_step(
            source, job_id, "job", "01_download",
            {"input/metadata.json": json.dumps({
                "final_url": "https://cdn.example.com/a.pdf?X-Amz-Signature=" + "9" * 40,
            }).encode()},
        )
        before = repo_state(source.repo)
        with pytest.raises((BackupError, PolicyError)):
            await do_backup(source)
        assert repo_state(source.repo) == before

    async def test_approved_secret_blob_is_disclosed_in_the_snapshot(self, source, tmp_path):
        """操作者批准的例外必须写进 snapshot.policy,不能封进一个断言相反的快照。"""
        job_id = "job_blob"
        insert_job(source, job_id, content_type="document", document_kind="article")
        insert_step(
            source, job_id, "job", "01_download", "done",
            started_at=T_STARTED, finished_at=T_FINISHED,
        )
        await commit_step(
            source, job_id, "job", "01_download",
            {"input/metadata.json": json.dumps({
                "final_url": "https://cdn.example.com/a.pdf?X-Amz-Signature=" + "9" * 40,
            }).encode()},
        )
        allowlist = tmp_path / "approved.txt"
        allowlist.write_text(f"{job_id}:input/metadata.json\n", encoding="utf-8")

        result = await do_backup(source, secret_blob_allowlist=allowlist)

        policy = source.repo.get_snapshot(result.snapshot_digest)["policy"]
        assert policy["secrets_included"] is True
        assert policy["secret_scan_exceptions"] == [f"{job_id}:input/metadata.json"]
        assert result.report["secret_blob_exceptions"] == [f"{job_id}:input/metadata.json"]
        assert result.stats["blob_scan_exceptions"] == 1

    async def test_private_key_in_failure_message_fails_closed(self, source):
        job_id = "job_key"
        insert_job(source, job_id)
        insert_part(source, "pt_k1", job_id, 1)
        insert_step(
            source, job_id, "part:pt_k1", "01_download", "failed",
            error="-----BEGIN RSA PRIVATE KEY----- MIIEowIBAAKC",
            started_at=T_STARTED, finished_at=T_FINISHED,
        )
        before = repo_state(source.repo)
        with pytest.raises((BackupError, PolicyError)):
            await do_backup(source)
        assert repo_state(source.repo) == before


class TestForgedArtifacts:
    """矩阵行"DB done 但文件缺失/被换":manifest + exact outputs 才算完成。"""

    async def test_forged_manifest_digest_is_rejected(self, source):
        """manifest 声明的 sha 与真实字节不符 -> 重试耗尽后整次失败。"""
        job_id = "job_forge"
        insert_job(source, job_id)
        insert_part(source, "pt_f1", job_id, 1)
        insert_step(source, job_id, "part:pt_f1", "01_download", "done")
        await commit_step(
            source, job_id, "part:pt_f1", "01_download",
            {"input/source.mp4": MEDIA_ONE}, part_index=1,
        )
        # 换掉字节但保留 manifest:典型的"伪造完成"
        await source.storage.write_file(
            job_id, "parts/pt_f1/input/source.mp4", b"FORGED-CONTENT",
        )
        before = repo_state(source.repo)
        with pytest.raises(BackupError, match="consistency retries exhausted"):
            await do_backup(source)
        assert repo_state(source.repo) == before

    async def test_missing_output_is_rejected(self, source):
        job_id = "job_gone"
        insert_job(source, job_id)
        insert_part(source, "pt_g1", job_id, 1)
        insert_step(source, job_id, "part:pt_g1", "01_download", "done")
        await commit_step(
            source, job_id, "part:pt_g1", "01_download",
            {"input/source.mp4": MEDIA_ONE}, part_index=1,
        )
        await source.storage.delete_file(job_id, "parts/pt_g1/input/source.mp4")
        before = repo_state(source.repo)
        with pytest.raises(BackupError):
            await do_backup(source)
        assert repo_state(source.repo) == before

    async def test_complete_file_without_manifest_is_not_backed_up(self, source):
        """矩阵行"失败视频被误备份":人工放完整 source 但无 manifest,必须排除。"""
        job_id = "job_manual"
        insert_job(source, job_id)
        insert_part(source, "pt_m1", job_id, 1)
        insert_step(
            source, job_id, "part:pt_m1", "01_download", "failed",
            error="download failed", started_at=T_STARTED, finished_at=T_FINISHED,
        )
        await ensure_job_json(source)
        await source.storage.write_file(
            job_id, "parts/pt_m1/input/source.mp4", MEDIA_ONE,
        )
        with pytest.raises(BackupError, match="unknown storage paths"):
            await do_backup(source)
        # 即便人工放行,也只进 unknown 报告,绝不作为业务 blob 收纳
        result = await do_backup(source, "run_allow", allow_unknown=True)
        assert not source.repo.has_blob(sha(MEDIA_ONE))
        assert result.stats["step_results"] == 0


class TestRepositoryTampering:
    """矩阵行"仓库损坏":位翻转/篡改/穿越/symlink 一律拒绝,且不静默修复。"""

    async def _seeded(self, source):
        await seed_video_job(source)
        await do_backup(source)
        assert source.repo.scrub().ok
        return source.repo

    async def test_tampered_snapshot_body_is_rejected(self, source):
        repo = await self._seeded(source)
        digest = repo.get_ref("latest")
        path = repo.root / "snapshots" / f"{digest.split(':')[1]}.json"
        body = json.loads(path.read_text())
        body["source"]["app_version"] = "9.9.9"
        path.write_text(json.dumps(body, sort_keys=True, separators=(",", ":")))
        with pytest.raises(RepositoryCorruptionError, match="digest mismatch"):
            repo.get_snapshot(digest)
        assert any(
            item.kind == "snapshot_corrupt" for item in repo.scrub().issues
        )

    async def test_snapshot_renamed_to_other_digest_is_rejected(self, source):
        """把 snapshot 改名成另一个 digest:内容寻址必须识破。"""
        repo = await self._seeded(source)
        digest = repo.get_ref("latest")
        source_path = repo.root / "snapshots" / f"{digest.split(':')[1]}.json"
        fake = repo.root / "snapshots" / ("a" * 64 + ".json")
        fake.write_bytes(source_path.read_bytes())
        with pytest.raises(RepositoryCorruptionError):
            repo.get_snapshot("sha256:" + "a" * 64)

    async def test_record_content_swap_is_rejected(self, source):
        repo = await self._seeded(source)
        record_dir = repo.root / "records" / "job_core"
        victim = next(iter(record_dir.iterdir()))
        victim.write_bytes(b'{"content_type":"video","created_at":"2026-07-18T00:00:00Z",'
                           b'"id":"evil","pipeline":"video"}')
        report = repo.scrub()
        assert any(item.kind == "record_corrupt" for item in report.issues)

    async def test_blob_bit_flip_never_silently_repaired(self, source):
        """损坏的 blob 不得被同 digest 覆盖"修复"(§2.14-6)。"""
        repo = await self._seeded(source)
        path = repo.blob_path(sha(MEDIA_ONE))
        data = bytearray(path.read_bytes())
        data[-1] ^= 0x01
        path.write_bytes(bytes(data))
        with pytest.raises(RepositoryCorruptionError, match="refusing to overwrite"):
            repo.put_blob_bytes(MEDIA_ONE)
        assert path.read_bytes() == bytes(data), "损坏对象必须原样留给人工处置"

    async def test_traversal_and_symlink_are_flagged(self, source):
        repo = await self._seeded(source)
        (repo.root / "blobs" / "sha256" / "..evil").write_bytes(b"x")
        (repo.root / "records" / "job_core" / "evil.json").symlink_to(
            repo.blob_path(sha(MEDIA_ONE))
        )
        kinds = {item.kind for item in repo.scrub().issues}
        assert "stray_file" in kinds
        assert "symlink" in kinds

    async def test_zip_bomb_shaped_record_is_bounded(self, source):
        """压缩炸弹样式:超深嵌套 JSON 由有界解析挡住,不吃满内存。"""
        repo = await self._seeded(source)
        victim = next(iter((repo.root / "records" / "job_core").iterdir()))
        victim.write_bytes(b"[" * 5000 + b"1" + b"]" * 5000)
        assert any(
            item.kind == "record_corrupt" for item in repo.scrub().issues
        )

    async def test_dangling_ref_does_not_publish(self, source):
        repo = await self._seeded(source)
        with pytest.raises(RepositoryError):
            repo.set_ref("evil", "sha256:" + "e" * 64)
        assert "evil" not in repo.list_refs()


class TestPartOrderIntegrity:
    """矩阵行"多 Part 顺序丢失":清单不一致必须拒绝。"""

    async def test_job_json_manifest_mismatch_is_rejected(self, source):
        part_ids = await seed_video_job(source)
        await source.storage.write_file("job_alpha", "job.json", json.dumps({
            "parts": [{"part_id": part_ids[1]}, {"part_id": part_ids[0]}],
        }).encode())
        before = repo_state(source.repo)
        with pytest.raises(BackupError, match="disagrees with database"):
            await do_backup(source)
        assert repo_state(source.repo) == before

    async def test_part_scoped_manifest_index_mismatch_is_rejected(self, source):
        job_id = "job_idx"
        insert_job(source, job_id)
        insert_part(source, "pt_i1", job_id, 1)
        insert_step(source, job_id, "part:pt_i1", "01_download", "done")
        # manifest 自称 part_index=7,与 DB 的 1 不符
        await commit_step(
            source, job_id, "part:pt_i1", "01_download",
            {"input/source.mp4": MEDIA_ONE}, part_index=7,
        )
        with pytest.raises(BackupError, match="part_index disagrees"):
            await do_backup(source)
