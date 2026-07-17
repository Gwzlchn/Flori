"""把v7视频对象和SQLite一次性切换到多Part终态。"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from shared.db import Database
from shared.step_scope import stable_part_id
from shared.storage import write_path_atomic


FORMAT = "flori-multipart-v8-migration"
PART_STEPS = {
    "01_download", "02_whisper", "03_scene", "04_frames",
    "05_dedup", "06_ocr", "07_danmaku", "08_punctuate",
}
PART_INTERMEDIATE = {
    "candidates.json", "danmaku.json", "dedup.json", "ocr.json",
    "scenes.json", "source_segments.json",
}


class MultipartMigrationError(RuntimeError):
    """迁移不变量未满足。"""


@dataclass(frozen=True)
class ObjectStat:
    size: int
    token: str


class ObjectStore(Protocol):
    def list_job(self, job_id: str) -> dict[str, ObjectStat]: ...
    def stat(self, job_id: str, rel_path: str) -> ObjectStat | None: ...
    def copy(
        self, job_id: str, src_rel: str, dst_rel: str,
    ) -> tuple[ObjectStat, ObjectStat]: ...
    def read(self, job_id: str, rel_path: str) -> bytes | None: ...
    def write(self, job_id: str, rel_path: str, data: bytes) -> None: ...


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(4 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _stat_document(value: ObjectStat) -> dict[str, int | str]:
    return {"size": value.size, "token": value.token}


def _document_stat(value: object) -> ObjectStat:
    if not isinstance(value, dict):
        raise MultipartMigrationError("migration journal object stat is invalid")
    size = value.get("size")
    token = value.get("token")
    if not isinstance(size, int) or size < 0 or not isinstance(token, str) or not token:
        raise MultipartMigrationError("migration journal object stat is invalid")
    return ObjectStat(size=size, token=token)


class LocalObjectStore:
    """本地jobs目录迁移后端,复制和校验均限制在单个Job根内。"""

    def __init__(self, jobs_dir: Path | str):
        self.jobs_dir = Path(jobs_dir).resolve()

    def _path(self, job_id: str, rel_path: str = "") -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", job_id):
            raise MultipartMigrationError(f"invalid job id: {job_id!r}")
        root = (self.jobs_dir / job_id).resolve()
        path = (root / rel_path).resolve()
        if path != root and root not in path.parents:
            raise MultipartMigrationError("object path escapes job root")
        return path

    def list_job(self, job_id: str) -> dict[str, ObjectStat]:
        root = self._path(job_id)
        if not root.is_dir():
            return {}
        result: dict[str, ObjectStat] = {}
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(root).as_posix()
            value = path.stat()
            result[rel] = ObjectStat(
                value.st_size,
                f"local:{value.st_mtime_ns}:{value.st_ctime_ns}",
            )
        return result

    def stat(self, job_id: str, rel_path: str) -> ObjectStat | None:
        path = self._path(job_id, rel_path)
        if not path.is_file():
            return None
        value = path.stat()
        return ObjectStat(value.st_size, f"sha256:{_sha256_path(path)}")

    def copy(
        self, job_id: str, src_rel: str, dst_rel: str,
    ) -> tuple[ObjectStat, ObjectStat]:
        source = self._path(job_id, src_rel)
        target = self._path(job_id, dst_rel)
        if not source.is_file():
            raise MultipartMigrationError(f"source object missing: {job_id}/{src_rel}")
        source_stat = ObjectStat(source.stat().st_size, f"sha256:{_sha256_path(source)}")
        existing = self.stat(job_id, dst_rel)
        if existing is not None:
            if existing != source_stat:
                raise MultipartMigrationError(
                    f"staged object conflicts: {job_id}/{dst_rel}"
                )
            return source_stat, existing
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.multipart-v8-", dir=target.parent,
        )
        os.close(fd)
        temporary = Path(temporary_name)
        try:
            shutil.copyfile(source, temporary)
            with temporary.open("rb") as stream:
                os.fsync(stream.fileno())
            copied = ObjectStat(
                temporary.stat().st_size,
                f"sha256:{_sha256_path(temporary)}",
            )
            if copied != source_stat:
                raise MultipartMigrationError(
                    f"copied object checksum mismatch: {job_id}/{src_rel}"
                )
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        target_stat = self.stat(job_id, dst_rel)
        if target_stat != source_stat:
            raise MultipartMigrationError(
                f"copied object checksum mismatch: {job_id}/{src_rel}"
            )
        return source_stat, target_stat

    def read(self, job_id: str, rel_path: str) -> bytes | None:
        path = self._path(job_id, rel_path)
        return path.read_bytes() if path.is_file() else None

    def write(self, job_id: str, rel_path: str, data: bytes) -> None:
        write_path_atomic(self._path(job_id, rel_path), data)


class MinioObjectStore:
    """MinIO服务端复制后端,避免大媒体经迁移进程往返传输。"""

    def __init__(
        self, endpoint: str, access_key: str, secret_key: str,
        bucket: str, secure: bool,
    ):
        from minio import Minio

        self.bucket = bucket
        self.client = Minio(
            endpoint, access_key=access_key, secret_key=secret_key, secure=secure,
        )
        if not self.client.bucket_exists(bucket):
            raise MultipartMigrationError(f"MinIO bucket does not exist: {bucket}")

    @staticmethod
    def _key(job_id: str, rel_path: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", job_id):
            raise MultipartMigrationError(f"invalid job id: {job_id!r}")
        if rel_path.startswith("/") or ".." in Path(rel_path).parts or "\x00" in rel_path:
            raise MultipartMigrationError("invalid object path")
        return f"{job_id}/{rel_path}"

    @staticmethod
    def _stat_value(value) -> ObjectStat:
        return ObjectStat(int(value.size or 0), f"etag:{value.etag or ''}")

    def list_job(self, job_id: str) -> dict[str, ObjectStat]:
        prefix = f"{job_id}/"
        return {
            item.object_name[len(prefix):]: self._stat_value(item)
            for item in self.client.list_objects(
                self.bucket, prefix=prefix, recursive=True,
            )
            if item.object_name != prefix
        }

    def stat(self, job_id: str, rel_path: str) -> ObjectStat | None:
        from minio.error import S3Error

        try:
            return self._stat_value(
                self.client.stat_object(self.bucket, self._key(job_id, rel_path)),
            )
        except S3Error as exc:
            if exc.code in {"NoSuchKey", "NoSuchObject", "NoSuchVersion"}:
                return None
            raise

    def copy(
        self, job_id: str, src_rel: str, dst_rel: str,
    ) -> tuple[ObjectStat, ObjectStat]:
        from minio.commonconfig import CopySource

        source = self.stat(job_id, src_rel)
        if source is None:
            raise MultipartMigrationError(f"source object missing: {job_id}/{src_rel}")
        existing = self.stat(job_id, dst_rel)
        if existing == source:
            return source, existing
        source_etag = source.token.removeprefix("etag:")
        self.client.copy_object(
            self.bucket,
            self._key(job_id, dst_rel),
            CopySource(
                self.bucket,
                self._key(job_id, src_rel),
                match_etag=source_etag,
            ),
        )
        copied = self.stat(job_id, dst_rel)
        current_source = self.stat(job_id, src_rel)
        if copied is None or copied.size != source.size or current_source != source:
            raise MultipartMigrationError(
                f"server-side copy mismatch: {job_id}/{src_rel}"
            )
        return source, copied

    def read(self, job_id: str, rel_path: str) -> bytes | None:
        from minio.error import S3Error

        response = None
        try:
            response = self.client.get_object(
                self.bucket, self._key(job_id, rel_path),
            )
            return response.read()
        except S3Error as exc:
            if exc.code in {"NoSuchKey", "NoSuchObject", "NoSuchVersion"}:
                return None
            raise
        finally:
            if response is not None:
                response.close()
                response.release_conn()

    def write(self, job_id: str, rel_path: str, data: bytes) -> None:
        self.client.put_object(
            self.bucket, self._key(job_id, rel_path), io.BytesIO(data), len(data),
        )


def is_part_artifact(rel_path: str) -> bool:
    """识别v7中由01-08产生且必须复制进P01的对象。"""
    rel = rel_path.replace("\\", "/")
    if rel.startswith("parts/") or rel == "job.json":
        return False
    if rel.startswith("input/") or rel.startswith("assets/"):
        return True
    if rel.startswith("intermediate/"):
        return rel.split("/", 1)[1] in PART_INTERMEDIATE
    if rel in {"output/transcript.md", "output/provenance/transcript.json"}:
        return True
    if rel.startswith("output/ai_logs/"):
        return rel.split("/", 2)[-1].startswith("08_punctuate")
    if rel.startswith("logs/"):
        basename = rel.rsplit("/", 1)[-1].lstrip(".")
        return any(basename.startswith(step) for step in PART_STEPS)
    if rel.startswith("."):
        basename = rel[1:]
        return any(basename.startswith(f"{step}.") for step in PART_STEPS)
    return False


def _read_v7_video_jobs(connection: sqlite3.Connection) -> list[dict]:
    connection.row_factory = sqlite3.Row
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if version not in {7, 8}:
        raise MultipartMigrationError(f"expected SQLite v7 or v8, got v{version}")
    rows = connection.execute(
        """SELECT id, url, source, title, domain, style_tags, meta
           FROM jobs WHERE content_type='video' ORDER BY id""",
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        for key in ("style_tags", "meta"):
            raw = item.get(key)
            try:
                item[key] = json.loads(raw) if isinstance(raw, str) and raw else ([] if key == "style_tags" else {})
            except json.JSONDecodeError:
                item[key] = [] if key == "style_tags" else {}
        result.append(item)
    return result


def _database_fingerprint(connection: sqlite3.Connection) -> str:
    """固定stage看到的完整逻辑库;维护窗口内任意写入都会阻止commit。"""
    digest = hashlib.sha256()
    for statement in connection.iterdump():
        digest.update(statement.encode("utf-8"))
        digest.update(b"\n")
    return f"sha256:{digest.hexdigest()}"


def _job_documents(job: dict, original: bytes) -> tuple[dict, dict]:
    try:
        root = json.loads(original.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MultipartMigrationError(f"invalid job.json for {job['id']}") from exc
    part_id = stable_part_id(job["id"], 1)
    url = job.get("url") or root.get("url")
    part = {
        "job_id": job["id"],
        "part_id": part_id,
        "part_index": 1,
        "title": None,
        "url": url,
        "source": job.get("source") or root.get("source"),
        "content_type": "video",
        "domain": job.get("domain") or root.get("domain") or "general",
        "style_tags": job.get("style_tags") or root.get("style_tags") or [],
        "flags": root.get("flags") or (job.get("meta") or {}).get("flags") or {},
    }
    if root.get("prompt_overrides"):
        part["prompt_overrides"] = root["prompt_overrides"]
    root["url"] = None
    root["parts"] = [part]
    return root, part


def _atomic_json(path: Path, payload: dict) -> None:
    write_path_atomic(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8"),
    )


def _load_journal(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MultipartMigrationError(f"migration journal unavailable: {path}") from exc
    if payload.get("format") != FORMAT:
        raise MultipartMigrationError("migration journal format mismatch")
    return payload


def _load_ready_marker(db_path: Path) -> tuple[Path, dict]:
    marker_path = db_path.parent / "multipart-v8.ready.json"
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MultipartMigrationError(
            f"migration ready marker unavailable: {marker_path}"
        ) from exc
    return marker_path, marker


def _validate_ready_marker(
    db_path: Path,
    journal_path: Path,
    journal: dict,
    *,
    schema_version: int,
) -> tuple[Path, dict]:
    marker_path, marker = _load_ready_marker(db_path)
    expected = {
        "format": FORMAT,
        "schema_from": 7,
        "schema_to": 8,
        "db_path": str(db_path.resolve()),
        "video_jobs": journal.get("video_jobs"),
    }
    if any(marker.get(key) != value for key, value in expected.items()):
        raise MultipartMigrationError("migration ready marker does not match journal")
    allowed_states = {"staged"} if schema_version == 7 else {
        "staged", "committed", "verified",
    }
    if marker.get("state") not in allowed_states:
        raise MultipartMigrationError(
            f"invalid migration ready state: {marker.get('state')!r}"
        )
    if journal.get("state") == "staged":
        current_hash = hashlib.sha256(journal_path.read_bytes()).hexdigest()
        if marker.get("state") != "staged" or marker.get("journal_sha256") != current_hash:
            raise MultipartMigrationError("staged migration journal checksum mismatch")
    return marker_path, marker


def _verify_staged_objects(
    store: ObjectStore,
    journal: dict,
    *,
    expect_original_root: bool,
) -> None:
    completed = journal.get("completed_jobs") or []
    jobs = journal.get("jobs") or {}
    if (
        len(completed) != journal.get("video_jobs")
        or set(completed) != set(jobs)
    ):
        raise MultipartMigrationError("migration journal has incomplete job coverage")
    for job_id in completed:
        expected = jobs[job_id]
        part_id = expected.get("part_id")
        part_doc = store.read(job_id, f"parts/{part_id}/job.json")
        if part_doc is None:
            raise MultipartMigrationError(f"part manifest missing: {job_id}")
        try:
            parsed_part = json.loads(part_doc.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MultipartMigrationError(f"invalid part manifest: {job_id}") from exc
        if parsed_part.get("part_id") != part_id or parsed_part.get("job_id") != job_id:
            raise MultipartMigrationError(f"part manifest identity mismatch: {job_id}")
        for item in expected.get("objects") or []:
            if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                raise MultipartMigrationError("migration journal object entry is invalid")
            source_rel = item["path"]
            source = store.stat(job_id, source_rel)
            target = store.stat(job_id, f"parts/{part_id}/{source_rel}")
            if (
                source != _document_stat(item.get("source"))
                or target != _document_stat(item.get("target"))
            ):
                raise MultipartMigrationError(
                    f"staged object changed before commit: {job_id}/{source_rel}"
                )
        root = store.read(job_id, "job.json")
        if root is None:
            raise MultipartMigrationError(f"job.json missing: {job_id}")
        if expect_original_root:
            if hashlib.sha256(root).hexdigest() != expected.get("original_sha256"):
                raise MultipartMigrationError(f"job.json changed before commit: {job_id}")
        else:
            try:
                parsed_root = json.loads(root.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise MultipartMigrationError(f"invalid migrated job.json: {job_id}") from exc
            if (
                parsed_root.get("url") is not None
                or [item.get("part_id") for item in parsed_root.get("parts", [])]
                != [part_id]
            ):
                raise MultipartMigrationError(f"migrated job manifest mismatch: {job_id}")


async def assert_redis_quiescent(redis_url: str) -> dict:
    """拒绝在队列、租约或资源槽仍活跃时切换执行身份。"""
    from redis.asyncio import Redis

    redis = Redis.from_url(redis_url, decode_responses=True)
    try:
        active = int(await redis.scard("active_jobs"))
        queued = 0
        async for key in redis.scan_iter(match="queue:*"):
            if await redis.type(key) == "zset":
                queued += int(await redis.zcard(key))
        holders = 0
        for pattern in ("pool:*:holders", "res:*:holders"):
            async for key in redis.scan_iter(match=pattern):
                holders += int(await redis.scard(key))
        leases = 0
        async for _ in redis.scan_iter(match="runner:lease:*"):
            leases += 1
        state = {
            "active_jobs": active, "queued": queued,
            "holders": holders, "leases": leases,
        }
        if any(state.values()):
            raise MultipartMigrationError(f"Redis is not quiescent: {state}")
        return state
    finally:
        await redis.aclose()


async def clear_runtime_redis(redis_url: str) -> int:
    """清旧执行身份;知识索引、配置和Worker注册不在删除范围。"""
    from redis.asyncio import Redis

    redis = Redis.from_url(redis_url, decode_responses=True)
    patterns = (
        "job:*", "queue:*", "runner:lease:*", "runner:released:*",
        "pool:*:holders", "res:*:holders", "flori:lifecycle*",
    )
    keys = {"active_jobs"}
    try:
        for pattern in patterns:
            async for key in redis.scan_iter(match=pattern):
                keys.add(str(key))
        if keys:
            return int(await redis.delete(*sorted(keys)))
        return 0
    finally:
        await redis.aclose()


def create_object_store(jobs_dir: Path | str) -> ObjectStore:
    endpoint = os.environ.get("MINIO_URL")
    if not endpoint:
        return LocalObjectStore(jobs_dir)
    return MinioObjectStore(
        endpoint,
        os.environ.get("MINIO_ACCESS_KEY", ""),
        os.environ.get("MINIO_SECRET_KEY", ""),
        os.environ.get("MINIO_BUCKET", "flori"),
        os.environ.get("MINIO_SECURE") == "1",
    )


def audit(db_path: Path, store: ObjectStore) -> dict:
    with sqlite3.connect(db_path) as connection:
        jobs = _read_v7_video_jobs(connection)
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    objects = 0
    bytes_total = 0
    part_objects = 0
    part_bytes = 0
    for job in jobs:
        files = store.list_job(job["id"])
        objects += len(files)
        bytes_total += sum(item.size for item in files.values())
        selected = {path: item for path, item in files.items() if is_part_artifact(path)}
        part_objects += len(selected)
        part_bytes += sum(item.size for item in selected.values())
    return {
        "schema_version": version,
        "video_jobs": len(jobs),
        "objects": objects,
        "bytes": bytes_total,
        "part_objects": part_objects,
        "part_bytes": part_bytes,
    }


def stage(db_path: Path, store: ObjectStore, journal_path: Path) -> dict:
    """复制P01对象并写Part文档;根job.json和SQLite保持v7。"""
    with sqlite3.connect(db_path) as connection:
        jobs = _read_v7_video_jobs(connection)
        database_fingerprint = _database_fingerprint(connection)
        if int(connection.execute("PRAGMA user_version").fetchone()[0]) != 7:
            raise MultipartMigrationError("stage requires SQLite v7")
    journal_root = journal_path.parent / "multipart-v8"
    originals = journal_root / "original-job-json"
    originals.mkdir(parents=True, exist_ok=True)
    journal = {
        "format": FORMAT,
        "state": "staging",
        "schema_from": 7,
        "schema_to": 8,
        "db_path": str(db_path.resolve()),
        "database_fingerprint": database_fingerprint,
        "video_jobs": len(jobs),
        "completed_jobs": [],
        "jobs": {},
    }
    if journal_path.exists():
        previous = _load_journal(journal_path)
        if (
            previous.get("db_path") != journal["db_path"]
            or previous.get("video_jobs") != len(jobs)
            or previous.get("schema_from") != 7
            or previous.get("database_fingerprint") != database_fingerprint
        ):
            raise MultipartMigrationError("existing journal does not match database")
        journal = previous
        if journal.get("state") in {"staged", "committed", "verified"}:
            return journal
    completed = set(journal.get("completed_jobs") or [])
    for job in jobs:
        job_id = job["id"]
        original = store.read(job_id, "job.json")
        if original is None:
            raise MultipartMigrationError(f"job.json missing: {job_id}")
        backup = originals / f"{job_id}.json"
        if not backup.exists():
            write_path_atomic(backup, original)
        root_doc, part_doc = _job_documents(job, original)
        part_id = part_doc["part_id"]
        files = store.list_job(job_id)
        selected = sorted(path for path in files if is_part_artifact(path))
        copied_objects = []
        for source_rel in selected:
            source_stat, target_stat = store.copy(
                job_id, source_rel, f"parts/{part_id}/{source_rel}",
            )
            copied_objects.append({
                "path": source_rel,
                "source": _stat_document(source_stat),
                "target": _stat_document(target_stat),
            })
        store.write(
            job_id,
            f"parts/{part_id}/job.json",
            json.dumps(part_doc, ensure_ascii=False, indent=2).encode("utf-8"),
        )
        journal["jobs"][job_id] = {
            "part_id": part_id,
            "objects": copied_objects,
            "bytes": sum(item["source"]["size"] for item in copied_objects),
            "root_job": root_doc,
            "original_sha256": hashlib.sha256(original).hexdigest(),
        }
        completed.add(job_id)
        journal["completed_jobs"] = sorted(completed)
        _atomic_json(journal_path, journal)
    if len(completed) != len(jobs):
        raise MultipartMigrationError("not every video job completed object stage")
    journal["state"] = "staged"
    _atomic_json(journal_path, journal)
    marker_path = db_path.parent / "multipart-v8.ready.json"
    _atomic_json(marker_path, {
        "format": FORMAT,
        "state": "staged",
        "schema_from": 7,
        "schema_to": 8,
        "db_path": str(db_path.resolve()),
        "video_jobs": len(jobs),
        "journal_sha256": hashlib.sha256(journal_path.read_bytes()).hexdigest(),
    })
    return journal


def _restore_root_documents(store: ObjectStore, journal_path: Path, journal: dict) -> None:
    originals = journal_path.parent / "multipart-v8" / "original-job-json"
    for job_id in journal.get("completed_jobs") or []:
        original = (originals / f"{job_id}.json").read_bytes()
        store.write(job_id, "job.json", original)


async def commit(
    db_path: Path,
    store: ObjectStore,
    journal_path: Path,
    *,
    redis_url: str | None,
) -> dict:
    """发布根manifest、原子迁SQLite并清除旧运行态Redis。"""
    journal = _load_journal(journal_path)
    if journal.get("state") not in {"staged", "committed", "verified"}:
        raise MultipartMigrationError("object stage is incomplete")
    if redis_url:
        await assert_redis_quiescent(redis_url)
    with sqlite3.connect(db_path) as connection:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version == 7 and _database_fingerprint(connection) != journal.get(
            "database_fingerprint"
        ):
            raise MultipartMigrationError("database changed after object stage")
    marker_path, marker = _validate_ready_marker(
        db_path, journal_path, journal, schema_version=version,
    )
    _verify_staged_objects(
        store, journal, expect_original_root=version == 7,
    )
    if version == 7:
        try:
            for job_id in journal["completed_jobs"]:
                root_doc = journal["jobs"][job_id]["root_job"]
                store.write(
                    job_id,
                    "job.json",
                    json.dumps(root_doc, ensure_ascii=False, indent=2).encode("utf-8"),
                )
            old_gate = os.environ.get("FLORI_REQUIRE_OFFLINE_MIGRATIONS")
            os.environ["FLORI_REQUIRE_OFFLINE_MIGRATIONS"] = "1"
            try:
                database = Database(db_path)
                try:
                    database.init_schema()
                finally:
                    database.close()
            finally:
                if old_gate is None:
                    os.environ.pop("FLORI_REQUIRE_OFFLINE_MIGRATIONS", None)
                else:
                    os.environ["FLORI_REQUIRE_OFFLINE_MIGRATIONS"] = old_gate
        except BaseException:
            with sqlite3.connect(db_path) as connection:
                failed_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if failed_version == 7:
                _restore_root_documents(store, journal_path, journal)
            raise
    elif version != 8:
        raise MultipartMigrationError(f"commit expected SQLite v7/v8, got v{version}")
    deleted = await clear_runtime_redis(redis_url) if redis_url else 0
    journal["state"] = "committed"
    journal["redis_keys_deleted"] = deleted
    _atomic_json(journal_path, journal)
    marker["state"] = "committed"
    _atomic_json(marker_path, marker)
    return journal


def verify(db_path: Path, store: ObjectStore, journal_path: Path) -> dict:
    """逐Job核对DB Part、根manifest和全部stage对象。"""
    journal = _load_journal(journal_path)
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version != 8:
            raise MultipartMigrationError(f"verify requires SQLite v8, got v{version}")
        video_jobs = int(connection.execute(
            "SELECT COUNT(*) FROM jobs WHERE content_type='video'",
        ).fetchone()[0])
        parts = int(connection.execute("SELECT COUNT(*) FROM job_parts").fetchone()[0])
        top_urls = int(connection.execute(
            "SELECT COUNT(*) FROM jobs WHERE content_type='video' AND url IS NOT NULL",
        ).fetchone()[0])
        if video_jobs != journal["video_jobs"] or parts != video_jobs or top_urls:
            raise MultipartMigrationError(
                f"database verification failed: jobs={video_jobs}, parts={parts}, urls={top_urls}"
            )
        rows = connection.execute(
            "SELECT job_id,id FROM job_parts ORDER BY job_id,part_index",
        ).fetchall()
    for row in rows:
        job_id = str(row["job_id"])
        part_id = str(row["id"])
        expected = journal["jobs"].get(job_id)
        if expected is None or expected["part_id"] != part_id:
            raise MultipartMigrationError(f"part identity mismatch: {job_id}")
        part_doc = store.read(job_id, f"parts/{part_id}/job.json")
        root_doc = store.read(job_id, "job.json")
        if part_doc is None or root_doc is None:
            raise MultipartMigrationError(f"manifest object missing: {job_id}")
        root = json.loads(root_doc.decode("utf-8"))
        if root.get("url") is not None or [p.get("part_id") for p in root.get("parts", [])] != [part_id]:
            raise MultipartMigrationError(f"root manifest mismatch: {job_id}")
        for item in expected["objects"]:
            source_rel = item["path"]
            source = store.stat(job_id, source_rel)
            target = store.stat(job_id, f"parts/{part_id}/{source_rel}")
            if (
                source != _document_stat(item.get("source"))
                or target != _document_stat(item.get("target"))
            ):
                raise MultipartMigrationError(
                    f"staged object verification failed: {job_id}/{source_rel}"
                )
    journal["state"] = "verified"
    journal["verified_jobs"] = len(rows)
    _atomic_json(journal_path, journal)
    marker_path = db_path.parent / "multipart-v8.ready.json"
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    marker["state"] = "verified"
    _atomic_json(marker_path, marker)
    return {
        "schema_version": 8,
        "video_jobs": video_jobs,
        "parts": parts,
        "verified_objects": sum(len(item["objects"]) for item in journal["jobs"].values()),
        "redis_keys_deleted": journal.get("redis_keys_deleted", 0),
    }


async def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("audit", "stage", "commit", "verify", "all"))
    parser.add_argument("--data-dir", default=os.environ.get("DATA_DIR", "/data"))
    parser.add_argument("--db", default=None)
    parser.add_argument("--journal", default=None)
    parser.add_argument("--redis-url", default=os.environ.get("REDIS_URL"))
    parser.add_argument("--ack-maintenance-window", action="store_true")
    args = parser.parse_args()
    data_dir = Path(args.data_dir)
    db_path = Path(args.db) if args.db else data_dir / "db" / "analyzer.db"
    journal_path = Path(args.journal) if args.journal else data_dir / "db" / "multipart-v8-journal.json"
    store = create_object_store(data_dir / "jobs")
    if args.command == "audit":
        result = audit(db_path, store)
    else:
        if args.command in {"commit", "all"} and not args.ack_maintenance_window:
            raise MultipartMigrationError("commit requires --ack-maintenance-window")
        if args.redis_url:
            await assert_redis_quiescent(args.redis_url)
        result = {}
        if args.command in {"stage", "all"}:
            result = stage(db_path, store, journal_path)
        if args.command in {"commit", "all"}:
            result = await commit(
                db_path, store, journal_path, redis_url=args.redis_url,
            )
        if args.command in {"verify", "all"}:
            result = verify(db_path, store, journal_path)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def main() -> int:
    return asyncio.run(_main())


if __name__ == "__main__":
    raise SystemExit(main())
