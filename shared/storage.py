"""StorageBackend：统一文件访问接口。

LocalStorage：数据在本机，pull/push 为 no-op(work_dir 即真实 job 目录)。
RemoteStorage：对象存储(MinIO/S3)，让任意机器都能当 worker——
  pull 把该 job 现有产物下载到本机临时 work_dir，步骤照常读写本地路径，
  push 把本步新增/改动的文件回传对象存储(只增量上传、不删，避免并行分支互相覆盖)。
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import os
import shutil
import time
from pathlib import Path
from typing import Callable, Protocol


# B站 SESSDATA 等敏感凭证的本地侧载文件:只供同机(LocalStorage)下载步本地读取,
# 绝不入中心对象存储、绝不经 runner 网关下发给远端 worker(见 RemoteStorage / api/routes/runner.py)。
CREDENTIAL_REL = "input/.credentials.json"


def is_credential_file(rel: str) -> bool:
    """是否为敏感凭证侧载文件(按 basename 判,跨平台)。"""
    return rel.replace("\\", "/").rsplit("/", 1)[-1] == ".credentials.json"


def _parse_minio_version(info: dict) -> str | None:
    """从 MinioAdmin.info() 的 JSON 取服务端版本。
    实测响应无顶层 version,版本在 servers[].version(形如 RELEASE.xxx 或 2025-09-07T16:13:09Z)。
    优先顶层(兼容未来/其他实现),否则取首个带 version 的 server。取不到返回 None。"""
    if not isinstance(info, dict):
        return None
    top = info.get("version")
    if isinstance(top, str) and top:
        return top
    for srv in info.get("servers") or []:
        if isinstance(srv, dict):
            v = srv.get("version")
            if isinstance(v, str) and v:
                return v
    return None


class StorageBackend(Protocol):
    async def pull(self, job_id: str, step: str) -> Path: ...
    async def push(self, job_id: str, step: str, work_dir: Path) -> None: ...
    async def cleanup(self, job_id: str, step: str, work_dir: Path) -> None: ...
    # 删 job 时清掉该 job 的全部产物:LocalStorage 删 job 目录、RemoteStorage 删 {job_id}/ 前缀对象。
    # 幂等(无产物即 no-op),避免 MinIO/分布式部署删 job 后中心存储留孤儿产物。
    async def delete(self, job_id: str) -> None: ...
    # 供 api 按需取单个产物(笔记/日志等);找不到返回 None。
    async def read_file(self, job_id: str, rel_path: str) -> bytes | None: ...
    # 供 api 写入 job 初始文件(job.json、上传源文件等),worker 才能 pull 到。
    async def write_file(self, job_id: str, rel_path: str, data: bytes) -> None: ...
    # 列出某 job 的全部产物相对路径(供 gateway 产物清单端点 / GatewayStorage.pull 用)。
    async def list_files(self, job_id: str) -> list[str]: ...
    # 列产物相对路径 → 字节大小(供 /artifacts 透出每步/每 job 产物体积)。一次列举拿全,
    # 不对每文件逐个 stat(本地 rglob 自带 st_size、MinIO list_objects 自带 obj.size)。
    async def list_file_sizes(self, job_id: str) -> dict[str, int]: ...
    # 供 api range 流式播放视频/音频:取文件大小 + 读指定字节区间。找不到返回 None。
    async def file_size(self, job_id: str, rel_path: str) -> int | None: ...
    async def read_range(self, job_id: str, rel_path: str, start: int, length: int) -> bytes | None: ...
    # 健康探活(供 /api/status 的 minio 组件):返回 {status, mode, bucket, ...};不抛(异常由调用方包超时)。
    async def health(self) -> dict: ...
    # 容量统计(对象数 + 总字节):RemoteStorage 全量 list 求和(贵!),故 api 侧带缓存+后台刷新,
    # 绝不同步阻塞 /api/status。不支持(本地不强求)返回 None。
    async def capacity(self) -> dict | None: ...


class LocalStorage:
    """本地部署：数据就在本机，pull/push 都是 no-op。"""

    def __init__(self, jobs_dir: Path):
        self.jobs_dir = jobs_dir

    def _safe_path(self, job_id: str, rel_path: str = "") -> Path:
        # 兜底防穿越:job_id 不得逃出 jobs_dir、rel 不得逃出其 job 目录,
        # 挡持 token 者经 job_id/rel 里的 ".." 读写中心数据。
        # 空字节(null byte)会让 pathlib.resolve() / os 抛 ValueError(裸传即 500),在此与穿越一并拦成 ValueError。
        if "\x00" in job_id or "\x00" in rel_path:
            raise ValueError("null byte in path")
        root = self.jobs_dir.resolve()
        job_root = (root / job_id).resolve()
        if job_root != root and root not in job_root.parents:
            raise ValueError("path escapes jobs_dir")
        path = (job_root / rel_path).resolve()
        if path != job_root and job_root not in path.parents:
            raise ValueError("path escapes job dir")
        return path

    async def pull(self, job_id: str, step: str) -> Path:
        return self._safe_path(job_id)

    async def push(self, job_id: str, step: str, work_dir: Path) -> None:
        pass

    async def cleanup(self, job_id: str, step: str, work_dir: Path) -> None:
        pass

    async def delete(self, job_id: str) -> None:
        # _safe_path 兜底防穿越(job_id 不得逃出 jobs_dir);ignore_errors 保证幂等(目录不存在即 no-op)。
        root = self._safe_path(job_id)
        await asyncio.to_thread(shutil.rmtree, root, ignore_errors=True)

    async def read_file(self, job_id: str, rel_path: str) -> bytes | None:
        path = self._safe_path(job_id, rel_path)
        if not path.is_file():
            return None
        return await asyncio.to_thread(path.read_bytes)

    async def file_size(self, job_id: str, rel_path: str) -> int | None:
        path = self._safe_path(job_id, rel_path)
        return path.stat().st_size if path.is_file() else None

    async def read_range(self, job_id: str, rel_path: str, start: int, length: int) -> bytes | None:
        path = self._safe_path(job_id, rel_path)
        if not path.is_file():
            return None

        def _read() -> bytes:
            with open(path, "rb") as f:
                f.seek(start)
                return f.read(length)

        return await asyncio.to_thread(_read)

    async def write_file(self, job_id: str, rel_path: str, data: bytes) -> None:
        path = self._safe_path(job_id, rel_path)

        def _write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

        await asyncio.to_thread(_write)

    async def list_files(self, job_id: str) -> list[str]:
        return await asyncio.to_thread(self._list_files_sync, job_id)

    def _list_files_sync(self, job_id: str) -> list[str]:
        root = self._safe_path(job_id)
        if not root.is_dir():
            return []
        # 只收文件,相对 job 目录,统一用 "/" 分隔(跨平台/与对象键对齐)。
        return [
            p.relative_to(root).as_posix()
            for p in root.rglob("*") if p.is_file()
        ]

    async def list_file_sizes(self, job_id: str) -> dict[str, int]:
        return await asyncio.to_thread(self._list_file_sizes_sync, job_id)

    def _list_file_sizes_sync(self, job_id: str) -> dict[str, int]:
        root = self._safe_path(job_id)
        if not root.is_dir():
            return {}
        # rglob 已遍历到每个文件,顺手 stat().st_size,不再二次列举。
        return {
            p.relative_to(root).as_posix(): p.stat().st_size
            for p in root.rglob("*") if p.is_file()
        }

    async def health(self) -> dict:
        # 本地盘:无独立对象存储组件,前端按 mode=local 显"本地存储"灰点(unknown,非 down)。
        return {
            "status": "unknown", "mode": "local", "bucket": None,
            "version": None, "detail": "本地盘", "probe_ms": None,
        }

    async def capacity(self) -> dict | None:
        # 本地盘容量(os.walk 求和);to_thread 防阻塞。jobs_dir 不存在 → 零。
        return await asyncio.to_thread(self._capacity_sync)

    def _capacity_sync(self) -> dict:
        objects = 0
        total = 0
        root = self.jobs_dir
        if root.is_dir():
            for dirpath, _dirs, files in os.walk(root):
                for name in files:
                    try:
                        total += os.path.getsize(os.path.join(dirpath, name))
                        objects += 1
                    except OSError:
                        continue
        return {"objects": objects, "bytes": total}


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
            if is_credential_file(rel):
                continue  # 敏感凭证永不上行中心存储
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

    async def delete(self, job_id: str) -> None:
        await asyncio.to_thread(self._delete_sync, job_id)

    def _delete_sync(self, job_id: str) -> None:
        from minio.deleteobjects import DeleteObject

        client = self._client()
        prefix = f"{job_id}/"
        objs = [
            DeleteObject(o.object_name)
            for o in client.list_objects(self._bucket, prefix=prefix, recursive=True)
        ]
        if objs:
            # remove_objects 惰性返回错误迭代器,必须消费(list)才真正发起删除。
            errors = list(client.remove_objects(self._bucket, objs))
            if errors:
                import structlog
                structlog.get_logger().warning(
                    "storage_delete_partial", job_id=job_id,
                    errors=[str(e) for e in errors],
                )
        # 顺带清掉本机为该 job 留存的临时工作目录与快照(幂等)。
        work_dir = self._tmp_root / job_id
        self._snapshots.pop(str(work_dir), None)
        shutil.rmtree(work_dir, ignore_errors=True)

    async def write_file(self, job_id: str, rel_path: str, data: bytes) -> None:
        if is_credential_file(rel_path):
            return  # 敏感凭证不入中心对象存储(防下发到远端 worker);仅 LocalStorage 本机持有
        await asyncio.to_thread(self._write_file_sync, job_id, rel_path, data)

    def _write_file_sync(self, job_id: str, rel_path: str, data: bytes) -> None:
        import io

        self._client().put_object(
            self._bucket, f"{job_id}/{rel_path}", io.BytesIO(data), length=len(data),
        )

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

    async def file_size(self, job_id: str, rel_path: str) -> int | None:
        def _stat() -> int | None:
            from minio.error import S3Error
            try:
                return self._client().stat_object(self._bucket, f"{job_id}/{rel_path}").size
            except S3Error:
                return None
        return await asyncio.to_thread(_stat)

    async def read_range(self, job_id: str, rel_path: str, start: int, length: int) -> bytes | None:
        def _read() -> bytes | None:
            from minio.error import S3Error
            resp = None
            try:
                resp = self._client().get_object(
                    self._bucket, f"{job_id}/{rel_path}", offset=start, length=length,
                )
                return resp.read()
            except S3Error:
                return None
            finally:
                if resp is not None:
                    resp.close()
                    resp.release_conn()
        return await asyncio.to_thread(_read)

    async def list_files(self, job_id: str) -> list[str]:
        return await asyncio.to_thread(self._list_files_sync, job_id)

    def _list_files_sync(self, job_id: str) -> list[str]:
        prefix = f"{job_id}/"
        out: list[str] = []
        for obj in self._client().list_objects(self._bucket, prefix=prefix, recursive=True):
            rel = obj.object_name[len(prefix):]
            if rel:  # 跳过前缀本身/目录占位
                out.append(rel)
        return out

    async def list_file_sizes(self, job_id: str) -> dict[str, int]:
        return await asyncio.to_thread(self._list_file_sizes_sync, job_id)

    def _list_file_sizes_sync(self, job_id: str) -> dict[str, int]:
        prefix = f"{job_id}/"
        out: dict[str, int] = {}
        # list_objects 自带 obj.size,无需逐对象 stat_object。
        for obj in self._client().list_objects(self._bucket, prefix=prefix, recursive=True):
            rel = obj.object_name[len(prefix):]
            if rel:
                out[rel] = obj.size or 0
        return out

    async def health(self) -> dict:
        # bucket_exists 是 HEAD bucket(O(1)),勿用 list_objects(全量扫)。minio SDK 同步 → to_thread。
        # 容量统计(对象数/总字节)MinIO 无聚合 API,全量 list 才能求和 → 不在探活里做(设计 §5.4 标"未采集")。
        return await asyncio.to_thread(self._health_sync)

    def _health_sync(self) -> dict:
        t0 = time.perf_counter()
        exists = self._client().bucket_exists(self._bucket)
        probe_ms = round((time.perf_counter() - t0) * 1000, 1)
        return {
            "status": "up" if exists else "degraded",
            "mode": "remote", "version": self._server_version_sync(),
            "bucket": self._bucket, "bucket_exists": exists, "probe_ms": probe_ms,
            "detail": None if exists else f"bucket {self._bucket} 不存在",
        }

    def _server_version_sync(self) -> str | None:
        # 经 MinIO 管理 API(MinioAdmin.info)取服务端版本。版本近乎静态:首次成功即缓存到实例,
        # 后续 health 直接复用,不再每次新建 MinioAdmin/调 info()——避免管理 API RTT 反复挤占
        # health 的 3s 探活预算(否则可能拖超时致 minio 误报 down)。失败回 None 且不缓存,下次再试。
        cached = getattr(self, "_server_version", None)
        if cached:
            return cached
        try:
            from minio import MinioAdmin
            from minio.credentials import StaticProvider

            adm = MinioAdmin(
                endpoint=self._endpoint,
                credentials=StaticProvider(self._access_key, self._secret_key),
                secure=self._secure,
            )
            info = json.loads(adm.info())
            self._server_version = _parse_minio_version(info)
            return self._server_version
        except Exception:
            return None

    async def capacity(self) -> dict | None:
        # 全量遍历 bucket 求对象数 + 总字节(MinIO 无聚合 API)。贵! 故 api 侧带缓存+后台定时
        # 刷新,绝不在 /api/status 同步调。同步 minio 调用包 to_thread,不阻塞事件循环。
        return await asyncio.to_thread(self._capacity_sync)

    def _capacity_sync(self) -> dict:
        objects = 0
        total = 0
        for obj in self._client().list_objects(self._bucket, recursive=True):
            total += obj.size or 0
            objects += 1
        return {"objects": objects, "bytes": total}


class GatewayStorage:
    """gateway-PROXY 产物后端:纯出站 HTTPS,产物经 API 中转(worker 永不直连 minio)。

    pull 拉清单+逐个产物到本机临时 work_dir(并记快照),push 只回传相对快照
    新增/改动的文件(语义与 RemoteStorage 一致),read/write/list 直接打 API 端点。
    每个对象整体载入内存(与现有 read_file/write_file 一致);流式传输是后续优化。

    远端 worker 经慢链路(出站 HTTPS)连中心存储时,两个可选项把大源文件挡在链路外:
      · STORAGE_WORKDIR_REUSE=1:job 目录跨步骤复用(按 job_id 命名),pull 跳过本机
        已存在的文件、cleanup 不再逐步 rmtree(改由 pull 时按 TTL GC 兄弟目录)。
        于是 01_download 下载的 source.mp4 留在本机,后续 03/04/02 步直接读本地,不重拉。
      · STORAGE_NO_PUSH_GLOBS=input/source.mp4,...:匹配的文件不回传中心存储,只留本机。
        大源文件(视频/音频)因此永不上行慢链路;帧图/字幕/OCR 等小产物照常回传供 AI 步消费。
    二者默认关闭(空),不改变既有部署语义;远端重算 worker 才在 docker run 里开。
    NOTE:开了 NO_PUSH,依赖该文件的步骤必须落在持有它的同一 worker(中心存储无副本)。
    """

    def __init__(
        self,
        base_url: str,
        token_getter: Callable[[], str],
        work_dir: Path,
    ):
        self._base_url = base_url.rstrip("/")
        self._token_getter = token_getter
        self._work_root = work_dir
        # pull 时记录每个 work_dir 的文件快照(relpath -> (size, mtime))，供 push 算增量。
        self._snapshots: dict[str, dict[str, tuple[int, float]]] = {}
        self._client_obj = None
        # 跨步骤复用 job 目录(留住大源文件,免重拉);关时沿用逐步 rmtree 旧语义。
        self._reuse = os.environ.get("STORAGE_WORKDIR_REUSE", "") not in ("", "0", "false")
        # 复用模式下,pull 时回收超过 TTL 未活动的兄弟 job 目录(默认 2h),给磁盘兜底。
        self._gc_ttl = int(os.environ.get("STORAGE_WORKDIR_GC_TTL_SEC", "7200"))
        # 不回传中心存储的文件 glob(相对 work_dir,fnmatch);默认空=全推(旧语义)。
        self._no_push = [
            g.strip() for g in os.environ.get("STORAGE_NO_PUSH_GLOBS", "").split(",") if g.strip()
        ]

    def _client(self):
        # 延迟建 httpx.AsyncClient:构造不连接(便于选型/单测),首次用到才建。
        if self._client_obj is None:
            import httpx

            from shared.net import gateway_tls_verify

            self._client_obj = httpx.AsyncClient(
                base_url=self._base_url, timeout=60, verify=gateway_tls_verify(),
            )
        return self._client_obj

    def _auth(self) -> dict:
        return {"Authorization": f"Bearer {self._token_getter()}"}

    async def pull(self, job_id: str, step: str) -> Path:
        if self._reuse:
            await asyncio.to_thread(self._gc_stale, job_id)
        work_dir = self._work_root / job_id
        work_dir.mkdir(parents=True, exist_ok=True)
        rels = await self.list_files(job_id)
        for rel in rels:
            dest = work_dir / rel
            # 复用模式:本机已有同名文件就不重拉(留住的 source.mp4 不再走慢链路下行)。
            if self._reuse and dest.is_file():
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            # 流式下载到磁盘:大产物(未配 NO_PUSH 的源文件)不再整体载入内存(审计 #23)。
            async with self._client().stream(
                "GET", f"/api/runner/jobs/{job_id}/artifacts/{rel}", headers=self._auth(),
            ) as resp:
                resp.raise_for_status()
                with open(dest, "wb") as f:
                    async for chunk in resp.aiter_bytes(65536):
                        f.write(chunk)
        # 快照覆盖 work_dir 全部本机文件(含复用留下的),push 才能据此跳过未改动的文件。
        snapshot: dict[str, tuple[int, float]] = {}
        for path in work_dir.rglob("*"):
            if path.is_file():
                st = path.stat()
                snapshot[path.relative_to(work_dir).as_posix()] = (st.st_size, st.st_mtime)
        self._snapshots[str(work_dir)] = snapshot
        if self._reuse:
            await asyncio.to_thread(os.utime, work_dir, None)  # 标记活动时间,供 GC 判活
        return work_dir

    def _gc_stale(self, current_job_id: str) -> None:
        """复用模式回收:删超过 TTL 未活动的兄弟 job 目录,给磁盘兜底。失败不致命。"""
        if not self._work_root.exists():
            return
        cutoff = time.time() - self._gc_ttl
        for child in self._work_root.iterdir():
            if child.name == current_job_id or not child.is_dir():
                continue
            try:
                if child.stat().st_mtime < cutoff:
                    shutil.rmtree(child, ignore_errors=True)
            except OSError:
                continue

    async def push(self, job_id: str, step: str, work_dir: Path) -> None:
        snapshot = self._snapshots.get(str(work_dir), {})
        for path in work_dir.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(work_dir).as_posix()
            if is_credential_file(rel) or self._is_no_push(rel):
                continue  # 敏感凭证 / 大源文件等:不回传中心(凭证绝不上行;源文件配 NO_PUSH glob)
            st = path.stat()
            prev = snapshot.get(rel)
            if prev is not None and prev == (st.st_size, st.st_mtime):
                continue  # 未改动，跳过
            data = await asyncio.to_thread(path.read_bytes)
            resp = await self._client().put(
                f"/api/runner/jobs/{job_id}/artifacts/{rel}",
                headers=self._auth(), content=data,
            )
            resp.raise_for_status()

    def _is_no_push(self, rel: str) -> bool:
        return any(fnmatch.fnmatch(rel, pat) for pat in self._no_push)

    async def cleanup(self, job_id: str, step: str, work_dir: Path) -> None:
        self._snapshots.pop(str(work_dir), None)
        # 复用模式留住 job 目录(同 job 后续步直接读本地),由 pull 时 TTL GC 回收。
        if self._reuse:
            return
        await asyncio.to_thread(shutil.rmtree, work_dir, ignore_errors=True)

    async def delete(self, job_id: str) -> None:
        # worker 侧 gateway 不负责删中心产物(那是 API/中心存储的职责,删 job 在 API 端走 Local/Remote);
        # 这里仅清掉本机为该 job 留存的(复用)工作目录与快照,保证幂等。
        work_dir = self._work_root / job_id
        self._snapshots.pop(str(work_dir), None)
        await asyncio.to_thread(shutil.rmtree, work_dir, ignore_errors=True)

    async def read_file(self, job_id: str, rel_path: str) -> bytes | None:
        resp = await self._client().get(
            f"/api/runner/jobs/{job_id}/artifacts/{rel_path}", headers=self._auth(),
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.content

    async def write_file(self, job_id: str, rel_path: str, data: bytes) -> None:
        resp = await self._client().put(
            f"/api/runner/jobs/{job_id}/artifacts/{rel_path}",
            headers=self._auth(), content=data,
        )
        resp.raise_for_status()

    async def list_files(self, job_id: str) -> list[str]:
        resp = await self._client().get(
            f"/api/runner/jobs/{job_id}/artifacts", headers=self._auth(),
        )
        resp.raise_for_status()
        return resp.json().get("files", [])

    # gateway 仅供 worker 拉/回传产物,前端 /artifacts 体积透出走 API 端 Local/Remote;此处不参与 → 返回空。
    async def list_file_sizes(self, job_id: str) -> dict[str, int]:
        return {}

    # gateway 仅供远端 worker 拉产物,不用于给前端流式播放;range 用整文件回退即可。
    async def file_size(self, job_id: str, rel_path: str) -> int | None:
        data = await self.read_file(job_id, rel_path)
        return len(data) if data is not None else None

    async def read_range(self, job_id: str, rel_path: str, start: int, length: int) -> bytes | None:
        data = await self.read_file(job_id, rel_path)
        return data[start:start + length] if data is not None else None

    async def health(self) -> dict:
        # worker 侧网关存储,不参与 /api/status 的 minio 探活(那查的是 API 自己的中心存储)。
        # 仅满足 Protocol,标 unknown(gateway 中转)。
        return {"status": "unknown", "mode": "gateway", "bucket": None,
                "version": None, "detail": "gateway proxy", "probe_ms": None}

    async def capacity(self) -> dict | None:
        # worker 侧网关存储不查中心容量(那是 API 端 Remote/Local 的职责);仅满足 Protocol。
        return None

    async def close(self) -> None:
        if self._client_obj is not None:
            await self._client_obj.aclose()
            self._client_obj = None


def create_storage(jobs_dir: Path) -> StorageBackend:
    """工厂函数。设了 MINIO_URL 用对象存储(分布式 worker)，否则本地。"""
    endpoint = os.environ.get("MINIO_URL")
    if endpoint:
        return RemoteStorage(
            endpoint=endpoint,
            access_key=os.environ.get("MINIO_ACCESS_KEY", ""),
            secret_key=os.environ.get("MINIO_SECRET_KEY", ""),
            bucket=os.environ.get("MINIO_BUCKET", "flori"),
            secure=os.environ.get("MINIO_SECURE", "0") == "1",
            tmp_root=Path(os.environ.get("WORK_DIR", "/tmp/flori-work")),
        )
    return LocalStorage(jobs_dir)
