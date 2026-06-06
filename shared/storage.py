"""StorageBackend：统一文件访问接口。

LocalStorage：数据在本机，pull/push 为 no-op(work_dir 即真实 job 目录)。
RemoteStorage：对象存储(MinIO/S3)，让任意机器都能当 worker——
  pull 把该 job 现有产物下载到本机临时 work_dir，步骤照常读写本地路径，
  push 把本步新增/改动的文件回传对象存储(只增量上传、不删，避免并行分支互相覆盖)。
"""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path
from typing import Protocol


class StorageBackend(Protocol):
    async def pull(self, job_id: str, step: str) -> Path: ...
    async def push(self, job_id: str, step: str, work_dir: Path) -> None: ...
    async def cleanup(self, job_id: str, step: str, work_dir: Path) -> None: ...
    # 供 api 按需取单个产物(笔记/日志等);找不到返回 None。
    async def read_file(self, job_id: str, rel_path: str) -> bytes | None: ...


class LocalStorage:
    """本地部署：数据就在本机，pull/push 都是 no-op。"""

    def __init__(self, jobs_dir: Path):
        self.jobs_dir = jobs_dir

    async def pull(self, job_id: str, step: str) -> Path:
        return self.jobs_dir / job_id

    async def push(self, job_id: str, step: str, work_dir: Path) -> None:
        pass

    async def cleanup(self, job_id: str, step: str, work_dir: Path) -> None:
        pass

    async def read_file(self, job_id: str, rel_path: str) -> bytes | None:
        path = self.jobs_dir / job_id / rel_path
        if not path.is_file():
            return None
        return await asyncio.to_thread(path.read_bytes)


class RemoteStorage:
    """对象存储后端：worker 在任意机器拉取/回传 job 产物。

    对象键 = ``{job_id}/{相对路径}``。pull 下载整个 job 前缀到本机临时目录，
    push 只上传相对 pull 快照新增或改动的文件(不删除)，因此同一 job 的并行
    步骤各自只写自己的产物，互不覆盖。
    """

    def __init__(
        self,
        endpoint: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        secure: bool,
        tmp_root: Path,
    ):
        self._endpoint = endpoint
        self._access_key = access_key
        self._secret_key = secret_key
        self._bucket = bucket
        self._secure = secure
        self._tmp_root = tmp_root
        # pull 时记录每个 work_dir 的文件快照(relpath -> (size, mtime))，供 push 算增量。
        self._snapshots: dict[str, dict[str, tuple[int, float]]] = {}
        self._client_obj = None

    def _client(self):
        # 延迟连接:构造时不导入 minio、不连服务器(便于选型与单测),首次用到才建。
        if self._client_obj is None:
            from minio import Minio

            c = Minio(
                self._endpoint, access_key=self._access_key,
                secret_key=self._secret_key, secure=self._secure,
            )
            if not c.bucket_exists(self._bucket):
                c.make_bucket(self._bucket)
            self._tmp_root.mkdir(parents=True, exist_ok=True)
            self._client_obj = c
        return self._client_obj

    async def pull(self, job_id: str, step: str) -> Path:
        return await asyncio.to_thread(self._pull_sync, job_id)

    def _pull_sync(self, job_id: str) -> Path:
        work_dir = self._tmp_root / job_id
        work_dir.mkdir(parents=True, exist_ok=True)
        snapshot: dict[str, tuple[int, float]] = {}
        prefix = f"{job_id}/"
        for obj in self._client().list_objects(self._bucket, prefix=prefix, recursive=True):
            rel = obj.object_name[len(prefix):]
            if not rel:
                continue
            dest = work_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            self._client().fget_object(self._bucket, obj.object_name, str(dest))
            st = dest.stat()
            snapshot[rel] = (st.st_size, st.st_mtime)
        self._snapshots[str(work_dir)] = snapshot
        return work_dir

    async def push(self, job_id: str, step: str, work_dir: Path) -> None:
        await asyncio.to_thread(self._push_sync, job_id, work_dir)

    def _push_sync(self, job_id: str, work_dir: Path) -> None:
        snapshot = self._snapshots.get(str(work_dir), {})
        for path in work_dir.rglob("*"):
            if not path.is_file():
                continue
            rel = str(path.relative_to(work_dir))
            st = path.stat()
            prev = snapshot.get(rel)
            if prev is not None and prev == (st.st_size, st.st_mtime):
                continue  # 未改动，跳过
            self._client().fput_object(self._bucket, f"{job_id}/{rel}", str(path))

    async def cleanup(self, job_id: str, step: str, work_dir: Path) -> None:
        await asyncio.to_thread(self._cleanup_sync, work_dir)

    def _cleanup_sync(self, work_dir: Path) -> None:
        self._snapshots.pop(str(work_dir), None)
        shutil.rmtree(work_dir, ignore_errors=True)

    async def read_file(self, job_id: str, rel_path: str) -> bytes | None:
        return await asyncio.to_thread(self._read_file_sync, job_id, rel_path)

    def _read_file_sync(self, job_id: str, rel_path: str) -> bytes | None:
        from minio.error import S3Error

        resp = None
        try:
            resp = self._client().get_object(self._bucket, f"{job_id}/{rel_path}")
            return resp.read()
        except S3Error:
            return None
        finally:
            if resp is not None:
                resp.close()
                resp.release_conn()


def create_storage(jobs_dir: Path) -> StorageBackend:
    """工厂函数。设了 MINIO_URL 用对象存储(分布式 worker)，否则本地。"""
    endpoint = os.environ.get("MINIO_URL")
    if endpoint:
        return RemoteStorage(
            endpoint=endpoint,
            access_key=os.environ.get("MINIO_ACCESS_KEY", ""),
            secret_key=os.environ.get("MINIO_SECRET_KEY", ""),
            bucket=os.environ.get("MINIO_BUCKET", "mnemo"),
            secure=os.environ.get("MINIO_SECURE", "0") == "1",
            tmp_root=Path(os.environ.get("WORK_DIR", "/tmp/mnemo-work")),
        )
    return LocalStorage(jobs_dir)
