"""shared/storage.py 的单测。"""

import os
import socket
import threading
import time
import tracemalloc
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from shared.errors import WorkerAuthRejected
from shared.runner_ops import TaskLease, bind_task_lease, clear_task_lease
from shared.storage import (
    ArtifactTooLarge,
    GatewayStorage,
    LocalStorage,
    RemoteStorage,
    _parse_minio_version,
    create_storage,
    is_credential_file,
    publish_content_addressed_path,
    read_file_bounded,
    read_path_bounded,
)


def _rss_bytes() -> int:
    with open("/proc/self/status", encoding="utf-8") as status:
        for line in status:
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    raise RuntimeError("VmRSS unavailable")


class TestParseMinioVersion:
    def test_from_servers_array(self):
        # 实测 MinioAdmin.info():顶层无 version,版本在 servers[].version。
        info = {"servers": [{"endpoint": "minio:9000", "version": "2025-09-07T16:13:09Z"}]}
        assert _parse_minio_version(info) == "2025-09-07T16:13:09Z"

    def test_release_style_version(self):
        info = {"servers": [{"version": "RELEASE.2024-01-01T00-00-00Z"}]}
        assert _parse_minio_version(info) == "RELEASE.2024-01-01T00-00-00Z"

    def test_prefers_top_level(self):
        info = {"version": "top-v", "servers": [{"version": "srv-v"}]}
        assert _parse_minio_version(info) == "top-v"

    def test_missing_returns_none(self):
        assert _parse_minio_version({}) is None
        assert _parse_minio_version({"servers": []}) is None
        assert _parse_minio_version({"servers": [{}]}) is None
        assert _parse_minio_version("not-a-dict") is None


class TestIsCredentialFile:
    def test_matches_sidecar(self):
        assert is_credential_file("input/.credentials.json")
        assert is_credential_file(".credentials.json")
        assert is_credential_file("input\\.credentials.json")  # windows 分隔符

    def test_rejects_others(self):
        assert not is_credential_file("job.json")
        assert not is_credential_file("output/notes.md")
        assert not is_credential_file("input/source.mp4")


class TestReadFileBounded:
    class ClosingStream:
        def __init__(self, chunks):
            self._chunks = iter(chunks)
            self.closed = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self._chunks)
            except StopIteration as exc:
                raise StopAsyncIteration from exc

        async def aclose(self):
            self.closed = True

    @pytest.mark.asyncio
    async def test_closes_stream_when_limit_sentinel_is_reached(self):
        stream = self.ClosingStream([b"abcd", b"efgh"])

        class Storage:
            async def file_size(self, job_id, rel_path):
                return None

            async def open_stream(self, job_id, rel_path, **kwargs):
                assert kwargs["length"] == 6
                return stream

        assert await read_file_bounded(Storage(), "j1", "a", 5) == b"abcdef"
        assert stream.closed is True

    @pytest.mark.asyncio
    async def test_closes_stream_when_stream_contract_is_invalid(self):
        stream = self.ClosingStream([b"ok", "not-bytes"])

        class Storage:
            async def file_size(self, job_id, rel_path):
                return None

            async def open_stream(self, job_id, rel_path, **kwargs):
                return stream

        with pytest.raises(ValueError, match="non-bytes"):
            await read_file_bounded(Storage(), "j1", "a", 5)
        assert stream.closed is True

    @pytest.mark.asyncio
    async def test_gateway_bounded_read_never_uses_full_get_for_size(self, tmp_path):
        class Response:
            def __init__(self, *, headers, chunks=()):
                self.status_code = 206
                self.headers = headers
                self._chunks = chunks

            def raise_for_status(self):
                return None

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def aiter_bytes(self, chunk_size):
                for chunk in self._chunks:
                    yield chunk

        class Client:
            def __init__(self):
                self.stream_calls = 0
                self.ranges = []

            async def get(self, *args, **kwargs):
                raise AssertionError("bounded read must not issue a full GET for file_size")

            def stream(self, *args, **kwargs):
                self.stream_calls += 1
                self.ranges.append(kwargs["headers"]["Range"])
                if self.stream_calls == 1:
                    return Response(headers={"Content-Range": "bytes 0-0/8"})
                return Response(headers={"Content-Range": "bytes 0-5/8"}, chunks=[b"abcdefgh"])

        storage = GatewayStorage("https://example.invalid", lambda: "token", tmp_path)
        client = Client()
        storage._client_obj = client

        assert await read_file_bounded(storage, "j1", "large.bin", 5) == b"\0" * 6
        assert client.stream_calls == 1
        assert client.ranges == ["bytes=0-0"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("status", "headers", "expected"), [
        (206, {"Content-Range": "bytes 0-0/123"}, 123),
        (200, {"Content-Length": "9"}, 9),
        (404, {}, None),
        (416, {"Content-Range": "bytes */0"}, 0),
    ])
    async def test_gateway_file_size_uses_metadata_without_touching_body(
        self, tmp_path, status, headers, expected,
    ):
        class Response:
            status_code = status

            def __init__(self):
                self.headers = headers

            @property
            def content(self):
                raise AssertionError("metadata probe must not materialize the body")

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            def raise_for_status(self):
                if self.status_code >= 400:
                    raise RuntimeError(f"HTTP {self.status_code}")

        class Client:
            def stream(self, method, endpoint, *, headers):
                assert method == "GET"
                assert headers["Range"] == "bytes=0-0"
                return Response()

        storage = GatewayStorage("https://example.invalid", lambda: "token", tmp_path)
        storage._client_obj = Client()
        assert await storage.file_size("j1", "artifact.bin") == expected


class TestReadPathBounded:
    def test_rejects_parent_component_symlink(self, tmp_path):
        root = tmp_path / "root"
        real = root / "real"
        real.mkdir(parents=True)
        (real / "note.md").write_text("safe")
        (root / "alias").symlink_to(real, target_is_directory=True)

        with pytest.raises(OSError):
            read_path_bounded(root / "alias/note.md", 10, trusted_root=root)

    def test_rejects_parent_replacement_during_read(self, tmp_path, monkeypatch):
        from shared import storage as storage_module

        root = tmp_path / "root"
        source_dir = root / "source"
        source_dir.mkdir(parents=True)
        target = source_dir / "note.md"
        target.write_bytes(b"inside")
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "note.md").write_bytes(b"outside")
        real_read = storage_module.os.read
        replaced = False

        def replace_parent(fd, size):
            nonlocal replaced
            data = real_read(fd, size)
            if not replaced:
                replaced = True
                source_dir.rename(root / "source-old")
                source_dir.symlink_to(outside, target_is_directory=True)
            return data

        monkeypatch.setattr(storage_module.os, "read", replace_parent)
        with pytest.raises(OSError, match="changed|link"):
            read_path_bounded(target, 10, trusted_root=root)

    def test_rejects_growth_during_read(self, tmp_path, monkeypatch):
        from shared import storage as storage_module

        target = tmp_path / "note.md"
        target.write_bytes(b"inside")
        real_read = storage_module.os.read
        grown = False

        def grow_file(fd, size):
            nonlocal grown
            data = real_read(fd, size)
            if not grown:
                grown = True
                with target.open("ab") as handle:
                    handle.write(b"growth")
            return data

        monkeypatch.setattr(storage_module.os, "read", grow_file)
        with pytest.raises(OSError, match="changed"):
            read_path_bounded(target, 20, trusted_root=tmp_path)

    def test_content_addressed_publish_is_no_clobber_and_cleans_staging(self, tmp_path):
        target = tmp_path / "sources/source.md"
        publish_content_addressed_path(target, b"stable")
        publish_content_addressed_path(target, b"stable")
        with pytest.raises(ValueError, match="collision"):
            publish_content_addressed_path(target, b"forged")

        assert target.read_bytes() == b"stable"
        assert list(target.parent.glob("*.flori-part-*")) == []


class TestLocalStorage:
    @pytest.fixture
    def storage(self, tmp_path):
        return LocalStorage(tmp_path)

    @pytest.mark.asyncio
    async def test_pull_returns_path(self, storage, tmp_path):
        path = await storage.pull("j_xxx", "03_scene")
        assert path == tmp_path / "j_xxx"

    @pytest.mark.asyncio
    async def test_push_noop(self, storage, tmp_path):
        job_dir = tmp_path / "j_xxx"
        job_dir.mkdir(parents=True)
        (job_dir / "test.txt").write_text("hello")
        await storage.push("j_xxx", "03_scene", job_dir)
        # LocalStorage.push is a no-op — files should remain unchanged
        assert (job_dir / "test.txt").read_text() == "hello"

    @pytest.mark.asyncio
    async def test_cleanup_noop(self, storage, tmp_path):
        job_dir = tmp_path / "j_xxx"
        job_dir.mkdir(parents=True)
        (job_dir / "test.txt").write_text("data")
        await storage.cleanup("j_xxx", "03_scene", job_dir)
        # LocalStorage.cleanup is a no-op — directory should still exist
        assert job_dir.exists()
        assert (job_dir / "test.txt").read_text() == "data"

    @pytest.mark.asyncio
    async def test_object_version_changes_after_same_size_replacement(
        self, storage, tmp_path,
    ):
        await storage.write_file("j1", "source.bin", b"first")
        before = await storage.object_version("j1", "source.bin")
        assert before is not None
        assert before.size == 5
        assert before.namespace == f"local:{tmp_path.resolve()}"

        path = tmp_path / "j1/source.bin"
        previous_stat = path.stat()
        path.write_bytes(b"other")
        os.utime(path, ns=(
            previous_stat.st_atime_ns,
            previous_stat.st_mtime_ns + 1_000_000_000,
        ))
        after = await storage.object_version("j1", "source.bin")
        assert after is not None and after != before

        path.unlink()
        assert await storage.object_version("j1", "source.bin") is None

    @pytest.mark.asyncio
    async def test_stream_roundtrip_has_bounded_python_memory(self, storage):
        chunk = b"x" * (1024 * 1024)
        base_rss = _rss_bytes()
        peak_rss = base_rss

        async def source():
            nonlocal peak_rss
            for _ in range(32):
                peak_rss = max(peak_rss, _rss_bytes())
                yield chunk

        tracemalloc.start()
        tracemalloc.reset_peak()
        result = await storage.write_stream("j1", "large.bin", source())
        _, upload_peak = tracemalloc.get_traced_memory()
        assert result["size"] == 32 * 1024 * 1024
        assert upload_peak < 8 * 1024 * 1024
        assert peak_rss - base_rss < 12 * 1024 * 1024

        stream = await storage.open_stream("j1", "large.bin", chunk_size=1024 * 1024)
        assert stream is not None
        tracemalloc.reset_peak()
        total = 0
        async for part in stream:
            total += len(part)
            peak_rss = max(peak_rss, _rss_bytes())
        _, download_peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        assert total == result["size"]
        assert download_peak < 8 * 1024 * 1024
        assert peak_rss - base_rss < 12 * 1024 * 1024

    @pytest.mark.asyncio
    async def test_concurrent_large_uploads_are_bounded(self, storage):
        import asyncio

        chunk = b"z" * (1024 * 1024)

        async def upload(index: int):
            async def source():
                for _ in range(8):
                    yield chunk

            return await storage.write_stream(f"j{index}", "large.bin", source())

        tracemalloc.start()
        tracemalloc.reset_peak()
        results = await asyncio.gather(*(upload(i) for i in range(4)))
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        assert [item["size"] for item in results] == [8 * 1024 * 1024] * 4
        assert peak < 16 * 1024 * 1024

    @pytest.mark.asyncio
    async def test_stream_failure_cleans_staging_and_preserves_target(self, storage, tmp_path):
        await storage.write_file("j1", "a.bin", b"old")

        async def broken():
            yield b"partial"
            raise ConnectionError("client disconnected")

        with pytest.raises(ConnectionError):
            await storage.write_stream("j1", "a.bin", broken())
        assert await storage.read_file("j1", "a.bin") == b"old"
        assert await storage.list_files("j1") == ["a.bin"]
        assert not (tmp_path / ".flori-staging").exists()

    @pytest.mark.asyncio
    async def test_stream_size_and_checksum_mismatch_are_atomic(self, storage):
        async def source():
            yield b"new"

        await storage.write_file("j1", "a.bin", b"old")
        with pytest.raises(ValueError, match="size mismatch"):
            await storage.write_stream("j1", "a.bin", source(), expected_size=4)
        assert await storage.read_file("j1", "a.bin") == b"old"

        with pytest.raises(ValueError, match="checksum mismatch"):
            await storage.write_stream(
                "j1", "a.bin", source(), expected_sha256="0" * 64,
            )
        assert await storage.read_file("j1", "a.bin") == b"old"

    @pytest.mark.asyncio
    async def test_stream_limit_plus_one_is_atomic_and_cleans_staging(
        self, storage, tmp_path,
    ):
        await storage.write_file("j1", "a.bin", b"old")

        async def source():
            yield b"1234"
            yield b"567"

        with pytest.raises(ArtifactTooLarge, match="6 bytes"):
            await storage.write_stream("j1", "a.bin", source(), max_bytes=6)
        assert await storage.read_file("j1", "a.bin") == b"old"
        assert not (tmp_path / ".flori-staging").exists()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("invalid", ["text", bytearray(b"x"), memoryview(b"x")])
    async def test_stream_rejects_non_bytes_without_publishing(
        self, storage, tmp_path, invalid,
    ):
        await storage.write_file("j1", "a.bin", b"old")

        async def source():
            yield invalid

        with pytest.raises(TypeError, match="non-bytes"):
            await storage.write_stream("j1", "a.bin", source())
        assert await storage.read_file("j1", "a.bin") == b"old"
        assert not (tmp_path / ".flori-staging").exists()

    @pytest.mark.asyncio
    async def test_delete_propagates_local_io_failure(
        self, storage, tmp_path, monkeypatch,
    ):
        from shared import storage as storage_module

        await storage.write_file("j1", "source.bin", b"source")
        real_rmtree = storage_module.shutil.rmtree

        def deny_job(path, *args, **kwargs):
            if path == tmp_path / "j1":
                raise PermissionError("delete denied")
            return real_rmtree(path, *args, **kwargs)

        monkeypatch.setattr(storage_module.shutil, "rmtree", deny_job)
        with pytest.raises(PermissionError, match="delete denied"):
            await storage.delete("j1")
        assert (tmp_path / "j1/source.bin").read_bytes() == b"source"

    @pytest.mark.asyncio
    async def test_initialization_marker_is_not_worker_artifact(
        self, storage, tmp_path,
    ):
        await storage.write_file("j1", "job.json", b"{}")
        await storage.write_file("j1", ".flori-initializing.json", b"marker")

        assert await storage.list_initialization_markers() == ["j1"]
        assert await storage.list_files("j1") == ["job.json"]
        assert await storage.list_file_sizes("j1") == {"job.json": 2}
        work_dir = await storage.pull("j1", "01_download")
        assert not (work_dir / ".flori-initializing.json").exists()
        assert (
            tmp_path / ".flori-initializing/j1/marker.json"
        ).read_bytes() == b"marker"

    @pytest.mark.asyncio
    async def test_pull_missing_dir(self, storage, tmp_path):
        path = await storage.pull("nonexistent", "03_scene")
        assert path == tmp_path / "nonexistent"

    @pytest.mark.asyncio
    async def test_clone_copies_products_and_done_excludes_credentials(self, storage, tmp_path):
        # 父 job:产物 + .done dotfile + 凭证侧载文件
        src = tmp_path / "j_parent"
        (src / "output").mkdir(parents=True)
        (src / "input").mkdir(parents=True)
        (src / "output" / "sections.json").write_text("[]")
        (src / ".04_smart.done").write_text('{"def_digest":"sha256:x"}')
        (src / "input" / ".credentials.json").write_text("{}")  # 内容无关:is_credential_file 按文件名判,clone 须按名排除
        await storage.clone("j_parent", "j_child")
        child = tmp_path / "j_child"
        assert (child / "output" / "sections.json").read_text() == "[]"
        assert (child / ".04_smart.done").exists()             # .done 被播种(供 fork 跳过未变步)
        assert not (child / "input" / ".credentials.json").exists()  # 凭证不克隆

    @pytest.mark.asyncio
    async def test_clone_missing_src_noop(self, storage):
        await storage.clone("nope", "dst")   # 源不存在=no-op,不抛

    @pytest.mark.asyncio
    async def test_read_file(self, storage, tmp_path):
        out = tmp_path / "j_xxx" / "output"
        out.mkdir(parents=True)
        (out / "notes_smart.md").write_text("note")
        assert await storage.read_file("j_xxx", "output/notes_smart.md") == b"note"
        assert await storage.read_file("j_xxx", "output/missing.md") is None

    @pytest.mark.asyncio
    async def test_write_file_roundtrip(self, storage, tmp_path):
        await storage.write_file("j_w", "job.json", b'{"id":"j_w"}')
        assert (tmp_path / "j_w" / "job.json").read_bytes() == b'{"id":"j_w"}'
        assert await storage.read_file("j_w", "job.json") == b'{"id":"j_w"}'

    @pytest.mark.asyncio
    async def test_list_files(self, storage, tmp_path):
        job = tmp_path / "j_l"
        (job / "output").mkdir(parents=True)
        (job / "job.json").write_text("{}")
        (job / "output" / "notes.md").write_text("note")
        (job / "logs").mkdir()  # 空目录不计入
        files = sorted(await storage.list_files("j_l"))
        assert files == ["job.json", "output/notes.md"]  # rel,"/" 分隔,跳过目录

    @pytest.mark.asyncio
    async def test_list_files_missing_job(self, storage):
        assert await storage.list_files("nope") == []

    @pytest.mark.asyncio
    async def test_delete_file_removes_single_and_is_idempotent(self, storage, tmp_path):
        # rerun 清中心 .done 用:删单文件、不碰其他产物;不存在即 no-op。
        job = tmp_path / "j_df"
        job.mkdir()
        (job / ".02_parse.done").write_text("{}")
        (job / "job.json").write_text("{}")
        await storage.delete_file("j_df", ".02_parse.done")
        assert not (job / ".02_parse.done").exists()
        assert (job / "job.json").exists()
        await storage.delete_file("j_df", ".02_parse.done")   # 幂等

    @pytest.mark.asyncio
    async def test_delete_removes_job_dir(self, storage, tmp_path):
        job = tmp_path / "j_del"
        (job / "output").mkdir(parents=True)
        (job / "output" / "notes.md").write_text("n")
        await storage.delete("j_del")
        assert not job.exists()

    @pytest.mark.asyncio
    async def test_delete_missing_is_noop(self, storage):
        await storage.delete("nope")  # 幂等:目录不存在不报错

    @pytest.mark.asyncio
    async def test_delete_traversal_blocked(self, storage, tmp_path):
        # job_id 含 ".." 不得逃出 jobs_dir 删外部数据
        outside = tmp_path.parent / "keep.txt"
        outside.write_text("keep")
        with pytest.raises(ValueError):
            await storage.delete("..")
        assert outside.read_text() == "keep"

    @pytest.mark.asyncio
    async def test_traversal_via_job_id_blocked(self, storage, tmp_path):
        # job_id 含 ".." 逃出 jobs_dir → 拒绝(兜底防穿越,挡持 token 者读写中心数据)
        outside = tmp_path.parent / "outside.txt"
        outside.write_text("untouchable")
        with pytest.raises(ValueError):
            await storage.read_file("..", "outside.txt")
        with pytest.raises(ValueError):
            await storage.write_file("..", "outside.txt", b"pwned")
        with pytest.raises(ValueError):
            await storage.list_files("../")
        assert outside.read_text() == "untouchable"  # 未被覆盖

    @pytest.mark.asyncio
    async def test_traversal_via_rel_blocked(self, storage):
        with pytest.raises(ValueError):
            await storage.read_file("j_ok", "../../etc/passwd")
        with pytest.raises(ValueError):
            await storage.write_file("j_ok", "../escape.txt", b"x")

    @pytest.mark.asyncio
    async def test_null_byte_in_path_blocked(self, storage):
        # null byte 会让 pathlib.resolve() 抛 ValueError,裸传即 500;_safe_path 在源头拦成 ValueError。
        with pytest.raises(ValueError):
            await storage.read_file("j_ok", "assets/x\x00.jpg")
        with pytest.raises(ValueError):
            await storage.read_file("j\x00", "f.txt")
        with pytest.raises(ValueError):
            await storage.write_file("j_ok", "a\x00b", b"x")


class TestRemoteListFiles:
    @pytest.mark.asyncio
    async def test_list_objects_under_prefix_strips_prefix(self, monkeypatch):
        rs = RemoteStorage("h:9000", "k", "s", "b", False, tmp_root=None)
        objs = [
            MagicMock(object_name="j1/job.json"),
            MagicMock(object_name="j1/output/notes.md"),
            MagicMock(object_name="j1/"),  # 前缀本身应跳过
        ]
        client = MagicMock()
        client.list_objects.return_value = objs
        monkeypatch.setattr(rs, "_client", lambda: client)

        files = await rs.list_files("j1")
        assert files == ["job.json", "output/notes.md"]
        client.list_objects.assert_called_once_with("b", prefix="j1/", recursive=True)

    @pytest.mark.asyncio
    async def test_delete_removes_prefix_objects(self, tmp_path):
        # 删 job:列 {job_id}/ 前缀全部对象 → remove_objects 批量删,否则 MinIO 留孤儿。
        rs = RemoteStorage("h:9000", "k", "s", "b", False, tmp_root=tmp_path)
        objs = [MagicMock(object_name="j1/job.json"), MagicMock(object_name="j1/out/n.md")]
        client = MagicMock()
        client.list_objects.side_effect = [objs, [], []]
        client.remove_objects.return_value = []  # 无删除错误
        rs._client = lambda: client

        await rs.delete("j1")

        assert [call.kwargs["prefix"] for call in client.list_objects.call_args_list] == [
            "j1/", ".flori-staging/j1/", ".flori-initializing/j1/",
        ]
        bucket, deletes = client.remove_objects.call_args.args
        assert bucket == "b"
        assert len(list(deletes)) == 2  # 每个对象键一个 DeleteObject

    @pytest.mark.asyncio
    async def test_delete_no_objects_is_noop(self, tmp_path):
        rs = RemoteStorage("h:9000", "k", "s", "b", False, tmp_root=tmp_path)
        client = MagicMock()
        client.list_objects.return_value = []
        rs._client = lambda: client
        await rs.delete("none")  # 幂等:无对象不调 remove_objects
        client.remove_objects.assert_not_called()

    @pytest.mark.asyncio
    async def test_cancel_after_delete_request_keeps_strict_delete_task(self, tmp_path):
        import asyncio

        rs = RemoteStorage("h:9000", "k", "s", "b", False, tmp_root=tmp_path)
        client = MagicMock()
        release_delete = threading.Event()
        delete_scan_started = threading.Event()
        request_registered = asyncio.Event()

        def list_objects(*_args, **_kwargs):
            delete_scan_started.set()
            release_delete.wait(2)
            return []

        client.list_objects.side_effect = list_objects
        rs._client = lambda: client
        original_start = rs._maybe_start_delete

        def cancel_after_start(job_id):
            task = original_start(job_id)
            request_registered.set()
            asyncio.current_task().cancel()
            return task

        rs._maybe_start_delete = cancel_after_start
        delete_request = asyncio.create_task(rs.delete("j1"))
        await asyncio.wait_for(request_registered.wait(), timeout=1)
        done, _ = await asyncio.wait({delete_request}, timeout=0.2)
        assert delete_request in done
        with pytest.raises(asyncio.CancelledError):
            await delete_request
        assert "j1" in rs._delete_requested
        assert "j1" in rs._delete_tasks

        release_delete.set()
        await asyncio.wait_for(rs.wait_for_finalizers(), timeout=1)

        assert await asyncio.to_thread(delete_scan_started.wait, 1)
        assert len(client.list_objects.call_args_list) == 3
        assert "j1" not in rs._delete_requested
        assert "j1" not in rs._delete_tasks
        assert "j1" not in rs._job_locks

    @pytest.mark.asyncio
    async def test_delete_partial_error_is_consumed_and_raised(self, tmp_path):
        rs = RemoteStorage("h:9000", "k", "s", "b", False, tmp_root=tmp_path)
        client = MagicMock()
        client.list_objects.side_effect = [
            [MagicMock(object_name="j1/job.json")],
            [],
            [],
        ]
        consumed = []

        def errors():
            consumed.append(True)
            yield MagicMock(code="AccessDenied", object_name="j1/job.json")

        client.remove_objects.return_value = errors()
        rs._client = lambda: client

        with pytest.raises(OSError, match="AccessDenied"):
            await rs.delete("j1")
        assert consumed == [True]


class TestRemoteStreaming:
    @pytest.mark.asyncio
    async def test_object_version_uses_minio_stat_identity(self, tmp_path):
        rs = RemoteStorage("h:9000", "k", "s", "b", False, tmp_root=tmp_path)
        client = MagicMock()
        client.stat_object.return_value = MagicMock(
            size=42,
            etag="etag-1",
            version_id="version-1",
            last_modified="2026-07-14T00:00:00Z",
        )
        rs._client = lambda: client

        version = await rs.object_version("j1", "source.pdf")
        assert version is not None
        assert version.namespace == "minio:http://h:9000/b"
        assert version.size == 42
        assert version.token == (
            "etag-1:version-1:2026-07-14T00:00:00Z"
        )
        client.stat_object.assert_called_once_with("b", "j1/source.pdf")

    @pytest.mark.asyncio
    async def test_write_stream_stages_then_atomically_copies(self, tmp_path):
        import hashlib

        rs = RemoteStorage("h:9000", "k", "s", "b", False, tmp_root=tmp_path)
        client = MagicMock()
        uploaded = []

        def consume_stream(_bucket, _key, data, **_kwargs):
            uploaded.append(b"".join(iter(lambda: data.read(3), b"")))

        client.put_object.side_effect = consume_stream
        rs._client = lambda: client

        async def source():
            yield b"abc"
            yield b"def"

        result = await rs.write_stream(
            "j1", "out/a.bin", source(),
            expected_size=6, expected_sha256=hashlib.sha256(b"abcdef").hexdigest(),
        )
        assert result == {"size": 6, "sha256": hashlib.sha256(b"abcdef").hexdigest()}
        staging_key = client.put_object.call_args.args[1]
        assert staging_key.startswith(".flori-staging/j1/")
        assert uploaded == [b"abcdef"]
        assert client.put_object.call_args.kwargs["length"] == -1
        assert client.put_object.call_args.kwargs["part_size"] == 5 * 1024 * 1024
        copy_source = client.copy_object.call_args.args[2]
        assert client.copy_object.call_args.args[:2] == ("b", "j1/out/a.bin")
        assert copy_source.object_name == staging_key
        client.remove_object.assert_called_once_with("b", staging_key)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("status", "code"),
        [(403, "AccessDenied"), (500, "InternalError")],
    )
    async def test_final_stat_failure_never_starts_publish(
        self, tmp_path, status, code,
    ):
        from minio.error import S3Error

        rs = RemoteStorage("h:9000", "k", "s", "b", False, tmp_root=tmp_path)
        client = MagicMock()

        def consume_stream(_bucket, _key, data, **_kwargs):
            while data.read(3):
                pass

        client.put_object.side_effect = consume_stream
        client.stat_object.side_effect = S3Error(
            MagicMock(status=status), code, "stat failed", "resource", "req", "host",
        )
        rs._client = lambda: client

        async def source():
            yield b"new"

        with pytest.raises(S3Error) as raised:
            await rs.write_stream("j1", "out/a.bin", source())
        assert raised.value.code == code
        client.copy_object.assert_not_called()
        staging_key = client.put_object.call_args.args[1]
        client.remove_object.assert_called_once_with("b", staging_key)

    @pytest.mark.asyncio
    async def test_remote_marker_uses_global_enumerable_prefix(self, tmp_path):
        rs = RemoteStorage("h:9000", "k", "s", "b", False, tmp_root=tmp_path)
        client = MagicMock()
        client.list_objects.return_value = [
            MagicMock(object_name=".flori-initializing/j1/marker.json"),
            MagicMock(object_name=".flori-initializing/j2/not-marker.json"),
        ]
        rs._client = lambda: client

        await rs.write_file("j1", ".flori-initializing.json", b"marker")
        jobs = await rs.list_initialization_markers()

        assert client.put_object.call_args.args[:2] == (
            "b", ".flori-initializing/j1/marker.json",
        )
        assert jobs == ["j1"]
        client.list_objects.assert_called_once_with(
            "b", prefix=".flori-initializing/", recursive=True,
        )

    @pytest.mark.asyncio
    async def test_remote_marker_permission_errors_fail_closed(self, tmp_path):
        from minio.error import S3Error

        rs = RemoteStorage("h:9000", "k", "s", "b", False, tmp_root=tmp_path)
        client = MagicMock()
        denied = S3Error(
            MagicMock(status=403), "AccessDenied", "denied",
            "resource", "req", "host",
        )
        client.get_object.side_effect = denied
        client.remove_object.side_effect = denied
        rs._client = lambda: client

        with pytest.raises(S3Error, match="AccessDenied"):
            await rs.read_file("j1", ".flori-initializing.json")
        with pytest.raises(S3Error, match="AccessDenied"):
            await rs.delete_file("j1", ".flori-initializing.json")

    @pytest.mark.asyncio
    async def test_staging_lifecycle_merges_existing_rules_and_filters_objects(
        self, tmp_path,
    ):
        rs = RemoteStorage("h:9000", "k", "s", "b", False, tmp_root=tmp_path)
        client = MagicMock()
        existing = MagicMock(rule_id="keep-existing")
        client.get_bucket_lifecycle.return_value = MagicMock(rules=[existing])
        old = datetime.now(timezone.utc) - timedelta(days=2)
        fresh = datetime.now(timezone.utc)
        client.list_objects.return_value = [
            MagicMock(object_name=".flori-staging/stale/token-a", last_modified=old),
            MagicMock(object_name=".flori-staging/active/token-b", last_modified=old),
            MagicMock(object_name=".flori-staging/protected/token-c", last_modified=old),
            MagicMock(object_name=".flori-staging/fresh/token-d", last_modified=fresh),
        ]
        rs._client = lambda: client

        removed = await rs.cleanup_stale_staging(
            active_tokens={("active", "token-b")},
            protected_job_ids={"protected"},
            stale_before_epoch=(fresh - timedelta(hours=1)).timestamp(),
        )

        assert removed == 1
        config = client.set_bucket_lifecycle.call_args.args[1]
        assert config.rules[0] is existing
        assert [rule.rule_id for rule in config.rules] == [
            "keep-existing", "flori-staging-recovery",
        ]
        client.remove_object.assert_called_once_with(
            "b", ".flori-staging/stale/token-a",
        )

    @pytest.mark.asyncio
    async def test_missing_lifecycle_configuration_installs_rule(self, tmp_path):
        from minio.error import S3Error

        rs = RemoteStorage("h:9000", "k", "s", "b", False, tmp_root=tmp_path)
        client = MagicMock()
        client.get_bucket_lifecycle.side_effect = S3Error(
            MagicMock(status=404), "NoSuchLifecycleConfiguration", "missing",
            "resource", "req", "host",
        )
        client.list_objects.return_value = []
        rs._client = lambda: client

        assert await rs.cleanup_stale_staging(
            active_tokens=set(), protected_job_ids=set(), stale_before_epoch=0,
        ) == 0
        config = client.set_bucket_lifecycle.call_args.args[1]
        assert [rule.rule_id for rule in config.rules] == [
            "flori-staging-recovery",
        ]

    @pytest.mark.asyncio
    async def test_invalid_named_lifecycle_rule_is_replaced(self, tmp_path):
        rs = RemoteStorage("h:9000", "k", "s", "b", False, tmp_root=tmp_path)
        client = MagicMock()
        invalid = MagicMock(
            rule_id="flori-staging-recovery",
            status="Disabled",
        )
        client.get_bucket_lifecycle.return_value = MagicMock(rules=[invalid])
        client.list_objects.return_value = []
        rs._client = lambda: client

        await rs.cleanup_stale_staging(
            active_tokens=set(), protected_job_ids=set(), stale_before_epoch=0,
        )

        config = client.set_bucket_lifecycle.call_args.args[1]
        assert len(config.rules) == 1
        assert config.rules[0] is not invalid
        assert config.rules[0].rule_id == "flori-staging-recovery"
        assert config.rules[0].status == "Enabled"

    @pytest.mark.asyncio
    async def test_lifecycle_set_failure_aborts_cleanup(self, tmp_path):
        rs = RemoteStorage("h:9000", "k", "s", "b", False, tmp_root=tmp_path)
        client = MagicMock()
        client.get_bucket_lifecycle.return_value = None
        client.set_bucket_lifecycle.side_effect = PermissionError("lifecycle denied")
        rs._client = lambda: client

        with pytest.raises(PermissionError, match="lifecycle denied"):
            await rs.cleanup_stale_staging(
                active_tokens=set(), protected_job_ids=set(), stale_before_epoch=0,
            )
        client.list_objects.assert_not_called()

    @pytest.mark.asyncio
    async def test_open_stream_reads_chunks_and_closes_response(self, tmp_path):
        rs = RemoteStorage("h:9000", "k", "s", "b", False, tmp_root=tmp_path)
        response = MagicMock()
        response.read.side_effect = [b"abc", b"def", b""]
        client = MagicMock()
        client.get_object.return_value = response
        rs._client = lambda: client

        stream = await rs.open_stream("j1", "out/a.bin", chunk_size=3)
        assert stream is not None
        assert b"".join([part async for part in stream]) == b"abcdef"
        response.close.assert_called_once()
        response.release_conn.assert_called_once()

    @pytest.mark.asyncio
    async def test_publish_failure_removes_staging_object(self, tmp_path):
        rs = RemoteStorage("h:9000", "k", "s", "b", False, tmp_root=tmp_path)
        client = MagicMock()
        final = {"value": b"old"}

        def fail_copy(*_args, **_kwargs):
            raise RuntimeError("copy failed")

        client.copy_object.side_effect = fail_copy

        def consume_stream(_bucket, _key, data, **_kwargs):
            while data.read(3):
                pass

        client.put_object.side_effect = consume_stream
        rs._client = lambda: client

        async def source():
            yield b"partial"

        with pytest.raises(RuntimeError, match="copy failed"):
            await rs.write_stream("j1", "out/a.bin", source())
        staging_key = client.put_object.call_args.args[1]
        client.remove_object.assert_called_once_with("b", staging_key)
        assert final["value"] == b"old"

    @pytest.mark.asyncio
    async def test_upload_failure_still_attempts_staging_cleanup(self, tmp_path):
        rs = RemoteStorage("h:9000", "k", "s", "b", False, tmp_root=tmp_path)
        client = MagicMock()
        client.put_object.side_effect = ConnectionError("upload interrupted")
        rs._client = lambda: client

        async def source():
            yield b"partial"

        with pytest.raises(ConnectionError, match="upload interrupted"):
            await rs.write_stream("j1", "out/a.bin", source())
        staging_key = client.put_object.call_args.args[1]
        client.remove_object.assert_called_once_with("b", staging_key)

    @pytest.mark.asyncio
    async def test_stream_limit_aborts_multipart_and_never_publishes(self, tmp_path):
        rs = RemoteStorage("h:9000", "k", "s", "b", False, tmp_root=tmp_path)
        client = MagicMock()

        def consume_stream(_bucket, _key, data, **_kwargs):
            while data.read(3):
                pass

        client.put_object.side_effect = consume_stream
        rs._client = lambda: client

        async def source():
            yield b"1234"
            yield b"567"

        with pytest.raises(ArtifactTooLarge, match="6 bytes"):
            await rs.write_stream("j1", "out/a.bin", source(), max_bytes=6)
        client.copy_object.assert_not_called()
        staging_key = client.put_object.call_args.args[1]
        client.remove_object.assert_called_once_with("b", staging_key)

    @pytest.mark.asyncio
    async def test_source_disconnect_aborts_multipart_and_cleans_staging(self, tmp_path):
        rs = RemoteStorage("h:9000", "k", "s", "b", False, tmp_root=tmp_path)
        client = MagicMock()

        def consume_stream(_bucket, _key, data, **_kwargs):
            while data.read(3):
                pass

        client.put_object.side_effect = consume_stream
        rs._client = lambda: client

        async def source():
            yield b"partial"
            raise ConnectionError("client disconnected")

        with pytest.raises(ConnectionError, match="client disconnected"):
            await rs.write_stream("j1", "out/a.bin", source())
        client.copy_object.assert_not_called()
        staging_key = client.put_object.call_args.args[1]
        client.remove_object.assert_called_once_with("b", staging_key)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "invalid", ["", "not-bytes", bytearray(b"x"), memoryview(b"x")],
    )
    async def test_non_bytes_source_aborts_without_publishing(
        self, tmp_path, invalid,
    ):
        rs = RemoteStorage("h:9000", "k", "s", "b", False, tmp_root=tmp_path)
        client = MagicMock()

        def consume_stream(_bucket, _key, data, **_kwargs):
            while data.read(3):
                pass

        client.put_object.side_effect = consume_stream
        rs._client = lambda: client

        async def source():
            yield invalid

        with pytest.raises(TypeError, match="non-bytes"):
            await rs.write_stream("j1", "out/a.bin", source())
        client.copy_object.assert_not_called()
        staging_key = client.put_object.call_args.args[1]
        client.remove_object.assert_called_once_with("b", staging_key)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("kwargs", "error"),
        [
            ({"expected_size": 4}, "size mismatch"),
            ({"expected_sha256": "0" * 64}, "checksum mismatch"),
        ],
    )
    async def test_validation_failure_does_not_replace_existing_final(
        self, tmp_path, kwargs, error,
    ):
        rs = RemoteStorage("h:9000", "k", "s", "b", False, tmp_root=tmp_path)
        client = MagicMock()
        final = {"value": b"old"}

        def consume_stream(_bucket, _key, data, **_kwargs):
            while data.read(3):
                pass

        def replace_final(*_args, **_kwargs):
            final["value"] = b"new"

        client.put_object.side_effect = consume_stream
        client.copy_object.side_effect = replace_final
        rs._client = lambda: client

        async def source():
            yield b"new"

        with pytest.raises(ValueError, match=error):
            await rs.write_stream("j1", "out/a.bin", source(), **kwargs)
        client.copy_object.assert_not_called()
        assert final["value"] == b"old"
        staging_key = client.put_object.call_args.args[1]
        client.remove_object.assert_called_once_with("b", staging_key)

    @pytest.mark.asyncio
    async def test_cancellation_aborts_reader_without_deadlock(self, tmp_path):
        import asyncio

        rs = RemoteStorage("h:9000", "k", "s", "b", False, tmp_root=tmp_path)
        client = MagicMock()
        reader_started = threading.Event()
        hold_source = asyncio.Event()

        def consume_stream(_bucket, _key, data, **_kwargs):
            reader_started.set()
            while data.read(3):
                pass

        client.put_object.side_effect = consume_stream
        rs._client = lambda: client

        async def source():
            yield b"partial"
            await hold_source.wait()

        task = asyncio.create_task(rs.write_stream("j1", "out/a.bin", source()))
        assert await asyncio.to_thread(reader_started.wait, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)
        await asyncio.wait_for(rs.wait_for_finalizers(), timeout=1)
        client.copy_object.assert_not_called()
        staging_key = client.put_object.call_args.args[1]
        client.remove_object.assert_called_once_with("b", staging_key)

    @pytest.mark.asyncio
    async def test_cancellation_returns_before_blocked_minio_and_finalizes_later(
        self, tmp_path,
    ):
        import asyncio

        rs = RemoteStorage("h:9000", "k", "s", "b", False, tmp_root=tmp_path)
        client = MagicMock()
        reader_started = threading.Event()
        release_minio = threading.Event()
        hold_source = asyncio.Event()

        def blocked_upload(_bucket, _key, data, **_kwargs):
            reader_started.set()
            try:
                while data.read(3):
                    pass
            except OSError:
                release_minio.wait(2)
                raise

        client.put_object.side_effect = blocked_upload
        rs._client = lambda: client

        async def source():
            yield b"partial"
            await hold_source.wait()

        task = asyncio.create_task(rs.write_stream("j1", "out/a.bin", source()))
        assert await asyncio.to_thread(reader_started.wait, 1)
        task.cancel()
        done, _ = await asyncio.wait({task}, timeout=0.2)
        try:
            assert task in done
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            release_minio.set()
        await asyncio.wait_for(rs.wait_for_finalizers(), timeout=1)
        client.copy_object.assert_not_called()
        staging_key = client.put_object.call_args.args[1]
        client.remove_object.assert_called_once_with("b", staging_key)

    @pytest.mark.asyncio
    async def test_cancel_during_publish_removes_new_final(self, tmp_path):
        import asyncio
        from minio.error import S3Error

        rs = RemoteStorage("h:9000", "k", "s", "b", False, tmp_root=tmp_path)
        client = MagicMock()
        publish_started = threading.Event()
        release_publish = threading.Event()

        def consume_stream(_bucket, _key, data, **_kwargs):
            while data.read(3):
                pass

        def block_publish(*_args, **_kwargs):
            publish_started.set()
            release_publish.wait(2)

        client.put_object.side_effect = consume_stream
        client.stat_object.side_effect = S3Error(
            MagicMock(status=404), "NoSuchKey", "missing",
            "resource", "req", "host",
        )
        client.copy_object.side_effect = block_publish
        rs._client = lambda: client

        async def source():
            yield b"new"

        task = asyncio.create_task(rs.write_stream("j1", "out/a.bin", source()))
        assert await asyncio.to_thread(publish_started.wait, 1)
        task.cancel()
        done, _ = await asyncio.wait({task}, timeout=0.2)
        assert task in done
        with pytest.raises(asyncio.CancelledError):
            await task
        release_publish.set()
        await asyncio.wait_for(rs.wait_for_finalizers(), timeout=1)
        client.remove_object.assert_any_call("b", "j1/out/a.bin")

    @pytest.mark.asyncio
    async def test_cancel_during_overwrite_restores_previous_final(self, tmp_path):
        import asyncio

        rs = RemoteStorage("h:9000", "k", "s", "b", False, tmp_root=tmp_path)
        client = MagicMock()
        publish_started = threading.Event()
        release_publish = threading.Event()
        copy_targets: list[str] = []

        def consume_stream(_bucket, _key, data, **_kwargs):
            while data.read(3):
                pass

        def copy(_bucket, target, _source):
            copy_targets.append(target)
            if target == "j1/out/a.bin" and len(copy_targets) == 2:
                publish_started.set()
                release_publish.wait(2)

        client.put_object.side_effect = consume_stream
        client.stat_object.return_value = MagicMock(size=3)
        client.copy_object.side_effect = copy
        rs._client = lambda: client

        async def source():
            yield b"new"

        task = asyncio.create_task(rs.write_stream("j1", "out/a.bin", source()))
        assert await asyncio.to_thread(publish_started.wait, 1)
        task.cancel()
        done, _ = await asyncio.wait({task}, timeout=0.2)
        assert task in done
        with pytest.raises(asyncio.CancelledError):
            await task
        release_publish.set()
        await asyncio.wait_for(rs.wait_for_finalizers(), timeout=1)

        assert copy_targets[0].endswith(".backup")
        assert copy_targets[1:] == ["j1/out/a.bin", "j1/out/a.bin"]

    @pytest.mark.asyncio
    async def test_cancel_in_post_publish_barrier_removes_new_final(self, tmp_path):
        import asyncio
        from minio.error import S3Error

        rs = RemoteStorage("h:9000", "k", "s", "b", False, tmp_root=tmp_path)
        client = MagicMock()
        objects: dict[str, bytes] = {}
        post_publish = asyncio.Event()
        ensure_calls = 0

        def consume_stream(_bucket, key, data, **_kwargs):
            objects[key] = b"".join(iter(lambda: data.read(3), b""))

        def stat_object(_bucket, key):
            if key not in objects:
                raise S3Error(
                    MagicMock(status=404), "NoSuchKey", "missing",
                    "resource", "req", "host",
                )
            return MagicMock(size=len(objects[key]))

        def copy_object(_bucket, target, _source):
            staging_key = client.put_object.call_args.args[1]
            objects[target] = objects[staging_key]

        def remove_object(_bucket, key):
            objects.pop(key, None)

        client.put_object.side_effect = consume_stream
        client.stat_object.side_effect = stat_object
        client.copy_object.side_effect = copy_object
        client.remove_object.side_effect = remove_object
        rs._client = lambda: client
        original_ensure = rs._ensure_publish_allowed

        async def hold_post_publish(job_id):
            nonlocal ensure_calls
            ensure_calls += 1
            if ensure_calls == 3:
                post_publish.set()
                await asyncio.Event().wait()
            await original_ensure(job_id)

        rs._ensure_publish_allowed = hold_post_publish

        async def source():
            yield b"new"

        writer = asyncio.create_task(rs.write_stream("j1", "out/a.bin", source()))
        await asyncio.wait_for(post_publish.wait(), timeout=1)
        assert objects["j1/out/a.bin"] == b"new"
        writer.cancel()
        done, _ = await asyncio.wait({writer}, timeout=0.2)
        assert writer in done
        with pytest.raises(asyncio.CancelledError):
            await writer
        await asyncio.wait_for(rs.wait_for_finalizers(), timeout=1)

        assert "j1/out/a.bin" not in objects
        assert not any(key.startswith(".flori-staging/j1/") for key in objects)
        assert not rs._finalizer_tasks

    @pytest.mark.asyncio
    async def test_cancel_in_post_publish_barrier_restores_existing_final(
        self, tmp_path,
    ):
        import asyncio

        rs = RemoteStorage("h:9000", "k", "s", "b", False, tmp_root=tmp_path)
        client = MagicMock()
        objects = {"j1/out/a.bin": b"old"}
        post_publish = asyncio.Event()
        ensure_calls = 0
        final_copies = 0

        def consume_stream(_bucket, key, data, **_kwargs):
            objects[key] = b"".join(iter(lambda: data.read(3), b""))

        def copy_object(_bucket, target, _source):
            nonlocal final_copies
            if target.endswith(".backup"):
                objects[target] = objects["j1/out/a.bin"]
            elif target == "j1/out/a.bin":
                final_copies += 1
                if final_copies == 1:
                    staging_key = client.put_object.call_args.args[1]
                    objects[target] = objects[staging_key]
                else:
                    backup_key = next(
                        key for key in objects if key.endswith(".backup")
                    )
                    objects[target] = objects[backup_key]

        def remove_object(_bucket, key):
            objects.pop(key, None)

        client.put_object.side_effect = consume_stream
        client.stat_object.side_effect = lambda _bucket, key: MagicMock(
            size=len(objects[key]),
        )
        client.copy_object.side_effect = copy_object
        client.remove_object.side_effect = remove_object
        rs._client = lambda: client
        original_ensure = rs._ensure_publish_allowed

        async def hold_post_publish(job_id):
            nonlocal ensure_calls
            ensure_calls += 1
            if ensure_calls == 4:
                post_publish.set()
                await asyncio.Event().wait()
            await original_ensure(job_id)

        rs._ensure_publish_allowed = hold_post_publish

        async def source():
            yield b"new"

        writer = asyncio.create_task(rs.write_stream("j1", "out/a.bin", source()))
        await asyncio.wait_for(post_publish.wait(), timeout=1)
        assert objects["j1/out/a.bin"] == b"new"
        writer.cancel()
        done, _ = await asyncio.wait({writer}, timeout=0.2)
        assert writer in done
        with pytest.raises(asyncio.CancelledError):
            await writer
        await asyncio.wait_for(rs.wait_for_finalizers(), timeout=1)

        assert objects == {"j1/out/a.bin": b"old"}
        assert final_copies == 2
        assert not rs._finalizer_tasks

    @pytest.mark.asyncio
    @pytest.mark.parametrize("has_existing_final", [False, True])
    async def test_cancel_in_success_cleanup_rolls_back_and_job_is_reusable(
        self, tmp_path, has_existing_final,
    ):
        import asyncio
        from minio.error import S3Error

        rs = RemoteStorage("h:9000", "k", "s", "b", False, tmp_root=tmp_path)
        client = MagicMock()
        objects = {"j1/out/a.bin": b"old"} if has_existing_final else {}
        cleanup_started = threading.Event()
        release_cleanup = threading.Event()
        staging_calls = 0
        backup_calls = 0

        def consume_stream(_bucket, key, data, **_kwargs):
            objects[key] = b"".join(iter(lambda: data.read(3), b""))

        def stat_object(_bucket, key):
            if key not in objects:
                raise S3Error(
                    MagicMock(status=404), "NoSuchKey", "missing",
                    "resource", "req", "host",
                )
            return MagicMock(size=len(objects[key]))

        def copy_object(_bucket, target, source):
            objects[target] = objects[source.object_name]

        def remove_object(_bucket, key):
            nonlocal backup_calls, staging_calls
            if key.endswith(".backup"):
                backup_calls += 1
            elif key.startswith(".flori-staging/"):
                staging_calls += 1
                cleanup_started.set()
                release_cleanup.wait(2)
            objects.pop(key, None)

        client.put_object.side_effect = consume_stream
        client.stat_object.side_effect = stat_object
        client.copy_object.side_effect = copy_object
        client.remove_object.side_effect = remove_object
        rs._client = lambda: client

        async def source(body):
            yield body

        writer = asyncio.create_task(rs.write_stream(
            "j1", "out/a.bin", source(b"new"),
        ))
        assert await asyncio.to_thread(cleanup_started.wait, 1)
        writer.cancel()
        done, _ = await asyncio.wait({writer}, timeout=0.2)
        assert writer in done
        with pytest.raises(asyncio.CancelledError):
            await writer

        drain = asyncio.create_task(rs.wait_for_finalizers())
        await asyncio.sleep(0)
        assert not drain.done()
        assert staging_calls == 1
        release_cleanup.set()
        await asyncio.wait_for(drain, timeout=1)

        expected = {"j1/out/a.bin": b"old"} if has_existing_final else {}
        assert objects == expected
        assert "j1" not in rs._active_writers
        assert "j1" not in rs._job_locks
        assert not rs._finalizer_tasks
        assert staging_calls == 1
        assert backup_calls == (1 if has_existing_final else 0)

        result = await rs.write_stream("j1", "out/a.bin", source(b"reused"))
        assert result["size"] == 6
        await asyncio.wait_for(rs.wait_for_finalizers(), timeout=1)
        assert objects == {"j1/out/a.bin": b"reused"}
        assert "j1" not in rs._job_locks

    @pytest.mark.asyncio
    @pytest.mark.parametrize("has_existing_final", [False, True])
    async def test_cleanup_commit_wins_when_cancel_callback_runs_before_wakeup(
        self, tmp_path, has_existing_final,
    ):
        import asyncio
        from minio.error import S3Error

        rs = RemoteStorage("h:9000", "k", "s", "b", False, tmp_root=tmp_path)
        client = MagicMock()
        objects = {"j1/out/a.bin": b"old"} if has_existing_final else {}

        def consume_stream(_bucket, key, data, **_kwargs):
            objects[key] = b"".join(iter(lambda: data.read(3), b""))

        def stat_object(_bucket, key):
            if key not in objects:
                raise S3Error(
                    MagicMock(status=404), "NoSuchKey", "missing",
                    "resource", "req", "host",
                )
            return MagicMock(size=len(objects[key]))

        def copy_object(_bucket, target, source):
            objects[target] = objects[source.object_name]

        def remove_object(_bucket, key):
            objects.pop(key, None)

        client.put_object.side_effect = consume_stream
        client.stat_object.side_effect = stat_object
        client.copy_object.side_effect = copy_object
        client.remove_object.side_effect = remove_object
        rs._client = lambda: client
        original_cleanup = rs._remove_staging_keys
        cancel_scheduled = False
        writer = None

        async def cleanup_then_cancel_writer(*keys):
            nonlocal cancel_scheduled
            result = await original_cleanup(*keys)
            if not cancel_scheduled and not keys[0].endswith(".backup"):
                cancel_scheduled = True
                asyncio.get_running_loop().call_soon(writer.cancel)
            return result

        rs._remove_staging_keys = cleanup_then_cancel_writer

        async def source():
            yield b"new"

        writer = asyncio.create_task(rs.write_stream("j1", "out/a.bin", source()))
        result = await writer
        await asyncio.wait_for(rs.wait_for_finalizers(), timeout=1)

        assert cancel_scheduled
        assert result["size"] == 3
        assert objects == {"j1/out/a.bin": b"new"}
        assert "j1" not in rs._job_locks

    @pytest.mark.asyncio
    async def test_success_cleanup_failure_is_not_a_commit_point(self, tmp_path):
        import asyncio
        from minio.error import S3Error

        rs = RemoteStorage("h:9000", "k", "s", "b", False, tmp_root=tmp_path)
        client = MagicMock()
        objects: dict[str, bytes] = {}
        staging_calls = 0

        def consume_stream(_bucket, key, data, **_kwargs):
            objects[key] = b"".join(iter(lambda: data.read(3), b""))

        def stat_object(_bucket, key):
            if key not in objects:
                raise S3Error(
                    MagicMock(status=404), "NoSuchKey", "missing",
                    "resource", "req", "host",
                )
            return MagicMock(size=len(objects[key]))

        def copy_object(_bucket, target, source):
            objects[target] = objects[source.object_name]

        def remove_object(_bucket, key):
            nonlocal staging_calls
            if key.startswith(".flori-staging/") and not key.endswith(".backup"):
                staging_calls += 1
                if staging_calls == 1:
                    raise RuntimeError("cleanup denied")
            objects.pop(key, None)

        client.put_object.side_effect = consume_stream
        client.stat_object.side_effect = stat_object
        client.copy_object.side_effect = copy_object
        client.remove_object.side_effect = remove_object
        rs._client = lambda: client

        async def source():
            yield b"new"

        with pytest.raises(OSError, match="staging cleanup failed"):
            await rs.write_stream("j1", "out/a.bin", source())
        await asyncio.wait_for(rs.wait_for_finalizers(), timeout=1)

        assert objects == {}
        assert staging_calls == 2
        assert "j1" not in rs._job_locks
        assert not rs._finalizer_tasks

    @pytest.mark.asyncio
    async def test_cancel_during_failed_publish_backup_cleanup_is_drained(
        self, tmp_path,
    ):
        import asyncio

        rs = RemoteStorage("h:9000", "k", "s", "b", False, tmp_root=tmp_path)
        client = MagicMock()
        objects = {"j1/out/a.bin": b"old"}
        backup_cleanup_started = threading.Event()
        release_backup_cleanup = threading.Event()

        def consume_stream(_bucket, key, data, **_kwargs):
            objects[key] = b"".join(iter(lambda: data.read(3), b""))

        def copy_object(_bucket, target, source):
            if target == "j1/out/a.bin":
                raise RuntimeError("publish failed")
            objects[target] = objects[source.object_name]

        def remove_object(_bucket, key):
            if key.endswith(".backup"):
                backup_cleanup_started.set()
                release_backup_cleanup.wait(2)
            objects.pop(key, None)

        client.put_object.side_effect = consume_stream
        client.stat_object.return_value = MagicMock(size=3)
        client.copy_object.side_effect = copy_object
        client.remove_object.side_effect = remove_object
        rs._client = lambda: client

        async def source():
            yield b"new"

        writer = asyncio.create_task(rs.write_stream("j1", "out/a.bin", source()))
        assert await asyncio.to_thread(backup_cleanup_started.wait, 1)
        writer.cancel()
        done, _ = await asyncio.wait({writer}, timeout=0.2)
        assert writer in done
        with pytest.raises(asyncio.CancelledError):
            await writer
        drain = asyncio.create_task(rs.wait_for_finalizers())
        await asyncio.sleep(0)
        assert not drain.done()
        release_backup_cleanup.set()
        await asyncio.wait_for(drain, timeout=1)

        assert objects == {"j1/out/a.bin": b"old"}
        assert "j1" not in rs._job_locks
        assert not rs._finalizer_tasks

    @pytest.mark.asyncio
    async def test_failed_backup_cleanup_does_not_replace_publish_error(self, tmp_path):
        rs = RemoteStorage("h:9000", "k", "s", "b", False, tmp_root=tmp_path)
        client = MagicMock()
        objects = {"j1/out/a.bin": b"old"}

        def consume_stream(_bucket, key, data, **_kwargs):
            objects[key] = b"".join(iter(lambda: data.read(3), b""))

        def copy_object(_bucket, target, source):
            if target == "j1/out/a.bin":
                raise RuntimeError("publish failed")
            objects[target] = objects[source.object_name]

        def remove_object(_bucket, key):
            if key.endswith(".backup"):
                raise OSError("backup cleanup denied")
            objects.pop(key, None)

        client.put_object.side_effect = consume_stream
        client.stat_object.return_value = MagicMock(size=3)
        client.copy_object.side_effect = copy_object
        client.remove_object.side_effect = remove_object
        rs._client = lambda: client

        async def source():
            yield b"new"

        with pytest.raises(RuntimeError, match="publish failed"):
            await rs.write_stream("j1", "out/a.bin", source())

        assert objects["j1/out/a.bin"] == b"old"
        assert not any(
            key.startswith(".flori-staging/") and not key.endswith(".backup")
            for key in objects
        )

    @pytest.mark.asyncio
    async def test_delete_wins_when_cancelled_during_success_cleanup(self, tmp_path):
        import asyncio

        rs = RemoteStorage("h:9000", "k", "s", "b", False, tmp_root=tmp_path)
        client = MagicMock()
        objects = {"j1/out/a.bin": b"old"}
        cleanup_entered = threading.Event()
        release_cleanup = threading.Event()
        staging_calls = 0
        backup_calls = 0

        def consume_stream(_bucket, key, data, **_kwargs):
            objects[key] = b"".join(iter(lambda: data.read(3), b""))

        def copy_object(_bucket, target, source):
            objects[target] = objects[source.object_name]

        def remove_object(_bucket, key):
            nonlocal backup_calls, staging_calls
            if key.endswith(".backup"):
                backup_calls += 1
            elif key.startswith(".flori-staging/"):
                staging_calls += 1
                cleanup_entered.set()
                release_cleanup.wait(2)
            objects.pop(key, None)

        def list_objects(_bucket, *, prefix, recursive):
            assert recursive is True
            return [
                MagicMock(object_name=key)
                for key in list(objects)
                if key.startswith(prefix)
            ]

        def remove_objects(_bucket, _deletes):
            objects.clear()
            return []

        client.put_object.side_effect = consume_stream
        client.stat_object.side_effect = lambda _bucket, key: MagicMock(
            size=len(objects[key]),
        )
        client.copy_object.side_effect = copy_object
        client.remove_object.side_effect = remove_object
        client.list_objects.side_effect = list_objects
        client.remove_objects.side_effect = remove_objects
        rs._client = lambda: client

        async def source():
            yield b"new"

        writer = asyncio.create_task(rs.write_stream("j1", "out/a.bin", source()))
        assert await asyncio.to_thread(cleanup_entered.wait, 1)
        assert objects["j1/out/a.bin"] == b"new"
        await rs.delete("j1", defer_if_busy=True)
        delete_waiter = asyncio.create_task(rs.delete("j1"))
        writer.cancel()
        done, _ = await asyncio.wait({writer}, timeout=0.2)
        assert writer in done
        with pytest.raises(asyncio.CancelledError):
            await writer
        assert not delete_waiter.done()
        assert staging_calls == 1
        release_cleanup.set()
        await asyncio.wait_for(delete_waiter, timeout=1)
        await asyncio.wait_for(rs.wait_for_finalizers(), timeout=1)

        assert objects == {}
        assert "j1" not in rs._delete_requested
        assert "j1" not in rs._job_locks
        assert not rs._finalizer_tasks
        assert staging_calls == 1
        assert backup_calls == 1

    @pytest.mark.asyncio
    async def test_cancel_at_writer_register_return_seam_releases_state(
        self, tmp_path,
    ):
        import asyncio

        rs = RemoteStorage("h:9000", "k", "s", "b", False, tmp_root=tmp_path)
        client = MagicMock()
        rs._client = lambda: client
        original_register = rs._register_writer
        registered = asyncio.Event()

        async def cancel_after_registration(job_id):
            await original_register(job_id)
            registered.set()
            asyncio.current_task().cancel()
            await asyncio.sleep(0)

        rs._register_writer = cancel_after_registration

        async def source():
            yield b"unreachable"

        writer = asyncio.create_task(rs.write_stream("j1", "out/a.bin", source()))
        await asyncio.wait_for(registered.wait(), timeout=1)
        done, _ = await asyncio.wait({writer}, timeout=0.2)
        assert writer in done
        with pytest.raises(asyncio.CancelledError):
            await writer

        client.put_object.assert_not_called()
        assert "j1" not in rs._active_writers
        assert "j1" not in rs._job_locks

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("cancel_phase", "has_existing_final"),
        [("upload", False), ("backup", True), ("publish", True)],
    )
    async def test_cancel_at_operation_start_return_seam_has_no_orphan(
        self, tmp_path, cancel_phase, has_existing_final,
    ):
        import asyncio
        from minio.error import S3Error

        rs = RemoteStorage("h:9000", "k", "s", "b", False, tmp_root=tmp_path)
        client = MagicMock()
        objects = {"j1/out/a.bin": b"old"} if has_existing_final else {}
        cancelled_at_seam = asyncio.Event()
        final_copies = 0

        def consume_stream(_bucket, key, data, **_kwargs):
            body = bytearray()
            while chunk := data.read(3):
                body.extend(chunk)
            objects[key] = bytes(body)

        def stat_object(_bucket, key):
            if key not in objects:
                raise S3Error(
                    MagicMock(status=404), "NoSuchKey", "missing",
                    "resource", "req", "host",
                )
            return MagicMock(size=len(objects[key]))

        def copy_object(_bucket, target, _source):
            nonlocal final_copies
            if target.endswith(".backup"):
                objects[target] = objects["j1/out/a.bin"]
            elif target == "j1/out/a.bin":
                final_copies += 1
                if final_copies == 1:
                    staging_key = client.put_object.call_args.args[1]
                    objects[target] = objects[staging_key]
                else:
                    backup_key = next(
                        key for key in objects if key.endswith(".backup")
                    )
                    objects[target] = objects[backup_key]

        def remove_object(_bucket, key):
            objects.pop(key, None)

        client.put_object.side_effect = consume_stream
        client.stat_object.side_effect = stat_object
        client.copy_object.side_effect = copy_object
        client.remove_object.side_effect = remove_object
        rs._client = lambda: client
        original_start = rs._start_publish_operation

        async def cancel_after_registration(
            job_id, operations, phase, callback, *args,
        ):
            task = await original_start(
                job_id, operations, phase, callback, *args,
            )
            if phase == cancel_phase:
                cancelled_at_seam.set()
                asyncio.current_task().cancel()
                await asyncio.sleep(0)
            return task

        rs._start_publish_operation = cancel_after_registration

        async def source():
            yield b"new"

        writer = asyncio.create_task(rs.write_stream("j1", "out/a.bin", source()))
        await asyncio.wait_for(cancelled_at_seam.wait(), timeout=1)
        done, _ = await asyncio.wait({writer}, timeout=0.2)
        assert writer in done
        with pytest.raises(asyncio.CancelledError):
            await writer
        await asyncio.wait_for(rs.wait_for_finalizers(), timeout=1)

        if has_existing_final:
            assert objects == {"j1/out/a.bin": b"old"}
        else:
            assert objects == {}
        assert not rs._finalizer_tasks
        assert "j1" not in rs._active_writers

    @pytest.mark.asyncio
    async def test_delete_wins_over_post_publish_cancelled_rollback(self, tmp_path):
        import asyncio

        rs = RemoteStorage("h:9000", "k", "s", "b", False, tmp_root=tmp_path)
        client = MagicMock()
        objects = {"j1/out/a.bin": b"old"}
        post_publish = asyncio.Event()
        ensure_calls = 0
        final_copies = 0

        def consume_stream(_bucket, key, data, **_kwargs):
            objects[key] = b"".join(iter(lambda: data.read(3), b""))

        def copy_object(_bucket, target, _source):
            nonlocal final_copies
            if target.endswith(".backup"):
                objects[target] = objects["j1/out/a.bin"]
            elif target == "j1/out/a.bin":
                final_copies += 1
                staging_key = client.put_object.call_args.args[1]
                objects[target] = objects[staging_key]

        def remove_object(_bucket, key):
            objects.pop(key, None)

        def list_objects(_bucket, *, prefix, recursive):
            assert recursive is True
            return [
                MagicMock(object_name=key)
                for key in list(objects)
                if key.startswith(prefix)
            ]

        def remove_objects(_bucket, _deletes):
            objects.clear()
            return []

        client.put_object.side_effect = consume_stream
        client.stat_object.side_effect = lambda _bucket, key: MagicMock(
            size=len(objects[key]),
        )
        client.copy_object.side_effect = copy_object
        client.remove_object.side_effect = remove_object
        client.list_objects.side_effect = list_objects
        client.remove_objects.side_effect = remove_objects
        rs._client = lambda: client
        original_ensure = rs._ensure_publish_allowed

        async def hold_post_publish(job_id):
            nonlocal ensure_calls
            ensure_calls += 1
            if ensure_calls == 4:
                post_publish.set()
                await asyncio.Event().wait()
            await original_ensure(job_id)

        rs._ensure_publish_allowed = hold_post_publish

        async def source():
            yield b"new"

        writer = asyncio.create_task(rs.write_stream("j1", "out/a.bin", source()))
        await asyncio.wait_for(post_publish.wait(), timeout=1)
        assert objects["j1/out/a.bin"] == b"new"
        await rs.delete("j1", defer_if_busy=True)
        delete_waiter = asyncio.create_task(rs.delete("j1"))
        writer.cancel()
        done, _ = await asyncio.wait({writer}, timeout=0.2)
        assert writer in done
        with pytest.raises(asyncio.CancelledError):
            await writer
        await asyncio.wait_for(delete_waiter, timeout=1)
        await asyncio.wait_for(rs.wait_for_finalizers(), timeout=1)

        assert objects == {}
        assert final_copies == 1
        assert "j1" not in rs._delete_requested
        assert "j1" not in rs._job_locks
        assert not rs._finalizer_tasks

    @pytest.mark.asyncio
    async def test_delete_barrier_waits_for_all_cancelled_writers_inverse_order(
        self, tmp_path,
    ):
        import asyncio

        rs = RemoteStorage("h:9000", "k", "s", "b", False, tmp_root=tmp_path)
        client = MagicMock()
        started = {name: threading.Event() for name in ("first", "second")}
        release = {name: threading.Event() for name in ("first", "second")}
        cleaned = {name: threading.Event() for name in ("first", "second")}
        holds = {name: asyncio.Event() for name in ("first", "second")}

        def blocked_upload(_bucket, key, data, **_kwargs):
            token = key.rsplit("/", 1)[-1]
            started[token].set()
            try:
                while data.read(3):
                    pass
            except OSError:
                release[token].wait(2)
                raise

        def remove(_bucket, key):
            token = key.rsplit("/", 1)[-1].split(".", 1)[0]
            if token in cleaned:
                cleaned[token].set()

        client.put_object.side_effect = blocked_upload
        client.remove_object.side_effect = remove
        client.list_objects.return_value = []
        rs._client = lambda: client

        async def source(name):
            yield name.encode()
            await holds[name].wait()

        first = asyncio.create_task(rs.write_stream(
            "j1", "out/first.bin", source("first"), staging_token="first",
        ))
        second = asyncio.create_task(rs.write_stream(
            "j1", "out/second.bin", source("second"), staging_token="second",
        ))
        assert await asyncio.to_thread(started["first"].wait, 1)
        assert await asyncio.to_thread(started["second"].wait, 1)
        first.cancel()
        second.cancel()
        for task in (first, second):
            with pytest.raises(asyncio.CancelledError):
                await task

        await rs.delete("j1", defer_if_busy=True)
        await rs.delete("j1", defer_if_busy=True)
        delete_waiter = asyncio.create_task(rs.delete("j1"))
        client.list_objects.assert_not_called()
        release["second"].set()
        assert await asyncio.to_thread(cleaned["second"].wait, 1)
        assert not cleaned["first"].is_set()
        client.list_objects.assert_not_called()
        release["first"].set()
        await asyncio.wait_for(delete_waiter, timeout=1)
        await asyncio.wait_for(rs.wait_for_finalizers(), timeout=1)

        assert len(client.list_objects.call_args_list) == 3
        assert "j1" not in rs._delete_requested
        assert "j1" not in rs._job_locks

    @pytest.mark.asyncio
    async def test_existing_final_cancel_then_delete_never_restores(self, tmp_path):
        import asyncio

        rs = RemoteStorage("h:9000", "k", "s", "b", False, tmp_root=tmp_path)
        client = MagicMock()
        publish_started = threading.Event()
        release_publish = threading.Event()
        copy_targets: list[str] = []

        def consume_stream(_bucket, _key, data, **_kwargs):
            while data.read(3):
                pass

        def copy(_bucket, target, _source):
            copy_targets.append(target)
            if target == "j1/out/a.bin":
                publish_started.set()
                release_publish.wait(2)

        client.put_object.side_effect = consume_stream
        client.stat_object.return_value = MagicMock(size=3)
        client.copy_object.side_effect = copy
        client.list_objects.return_value = []
        rs._client = lambda: client

        async def source():
            yield b"new"

        task = asyncio.create_task(rs.write_stream("j1", "out/a.bin", source()))
        assert await asyncio.to_thread(publish_started.wait, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await rs.delete("j1", defer_if_busy=True)
        delete_waiter = asyncio.create_task(rs.delete("j1"))
        release_publish.set()
        await asyncio.wait_for(delete_waiter, timeout=1)
        await asyncio.wait_for(rs.wait_for_finalizers(), timeout=1)

        assert len(copy_targets) == 2
        assert copy_targets[0].endswith(".backup")
        assert copy_targets[1] == "j1/out/a.bin"
        assert len(client.list_objects.call_args_list) == 3

    @pytest.mark.asyncio
    async def test_delete_barrier_blocks_normal_writer_publish(self, tmp_path):
        import asyncio

        rs = RemoteStorage("h:9000", "k", "s", "b", False, tmp_root=tmp_path)
        client = MagicMock()
        reader_started = threading.Event()
        release_source = asyncio.Event()

        def consume_stream(_bucket, _key, data, **_kwargs):
            reader_started.set()
            while data.read(3):
                pass

        client.put_object.side_effect = consume_stream
        client.list_objects.return_value = []
        rs._client = lambda: client

        async def source():
            yield b"partial"
            await release_source.wait()

        task = asyncio.create_task(rs.write_stream("j1", "out/a.bin", source()))
        assert await asyncio.to_thread(reader_started.wait, 1)
        await rs.delete("j1", defer_if_busy=True)
        delete_waiter = asyncio.create_task(rs.delete("j1"))
        client.list_objects.assert_not_called()
        release_source.set()
        with pytest.raises(OSError, match="deletion"):
            await task
        await asyncio.wait_for(delete_waiter, timeout=1)
        await asyncio.wait_for(rs.wait_for_finalizers(), timeout=1)

        client.copy_object.assert_not_called()
        assert len(client.list_objects.call_args_list) == 3

    @pytest.mark.asyncio
    async def test_delete_during_normal_publish_waits_then_removes_job(self, tmp_path):
        import asyncio
        from minio.error import S3Error

        rs = RemoteStorage("h:9000", "k", "s", "b", False, tmp_root=tmp_path)
        client = MagicMock()
        publish_started = threading.Event()
        release_publish = threading.Event()
        order: list[str] = []

        def consume_stream(_bucket, _key, data, **_kwargs):
            while data.read(3):
                pass

        def publish(*_args, **_kwargs):
            order.append("publish_started")
            publish_started.set()
            release_publish.wait(2)
            order.append("publish_done")

        def list_objects(*_args, **_kwargs):
            order.append("delete_scan")
            return []

        client.put_object.side_effect = consume_stream
        client.stat_object.side_effect = S3Error(
            MagicMock(status=404), "NoSuchKey", "missing",
            "resource", "req", "host",
        )
        client.copy_object.side_effect = publish
        client.list_objects.side_effect = list_objects
        rs._client = lambda: client

        async def source():
            yield b"new"

        writer = asyncio.create_task(rs.write_stream("j1", "out/a.bin", source()))
        assert await asyncio.to_thread(publish_started.wait, 1)
        await rs.delete("j1", defer_if_busy=True)
        delete_waiter = asyncio.create_task(rs.delete("j1"))
        assert "delete_scan" not in order
        release_publish.set()
        with pytest.raises(OSError, match="deletion"):
            await writer
        await asyncio.wait_for(delete_waiter, timeout=1)
        await asyncio.wait_for(rs.wait_for_finalizers(), timeout=1)

        assert order.index("publish_done") < order.index("delete_scan")
        assert order.count("delete_scan") == 3

    @pytest.mark.asyncio
    async def test_successful_delete_releases_state_for_reused_job_id(self, tmp_path):
        rs = RemoteStorage("h:9000", "k", "s", "b", False, tmp_root=tmp_path)
        client = MagicMock()
        client.list_objects.return_value = []

        def consume_stream(_bucket, _key, data, **_kwargs):
            while data.read(3):
                pass

        client.put_object.side_effect = consume_stream
        rs._client = lambda: client

        await rs.delete("same-job")
        assert "same-job" not in rs._delete_requested
        assert "same-job" not in rs._job_locks

        async def source():
            yield b"reused"

        result = await rs.write_stream("same-job", "out/a.bin", source())
        assert result["size"] == 6
        client.copy_object.assert_called_once()
        assert "same-job" not in rs._job_locks

    @pytest.mark.asyncio
    async def test_failed_delete_keeps_barrier_until_explicit_retry(self, tmp_path):
        rs = RemoteStorage("h:9000", "k", "s", "b", False, tmp_root=tmp_path)
        client = MagicMock()
        client.list_objects.side_effect = [
            [MagicMock(object_name="j1/source.bin")], [], [],
        ]
        client.remove_objects.return_value = [
            MagicMock(code="AccessDenied", object_name="j1/source.bin"),
        ]
        rs._client = lambda: client

        with pytest.raises(OSError, match="AccessDenied"):
            await rs.delete("j1")
        assert "j1" in rs._delete_requested

        async def source():
            yield b"blocked"

        with pytest.raises(OSError, match="deletion"):
            await rs.write_stream("j1", "out/a.bin", source())

        client.list_objects.side_effect = [[], [], []]
        client.remove_objects.return_value = []
        await rs.delete("j1")
        assert "j1" not in rs._delete_requested
        assert "j1" not in rs._job_locks


class TestRemoteHealthVersion:
    @pytest.mark.asyncio
    async def test_readiness_probe_puts_and_deletes_canary(self, tmp_path, monkeypatch):
        rs = RemoteStorage("h:9000", "k", "s", "b", False, tmp_root=tmp_path)
        client = MagicMock()
        monkeypatch.setattr(rs, "_readiness_client", lambda _timeout: client)
        rs._server_version = "RELEASE.test"

        result = await rs.readiness_probe()

        bucket, key, stream, length = client.put_object.call_args.args
        assert bucket == "b"
        assert key.startswith(".flori-readiness/") and key.endswith(".canary")
        assert stream.read() == b"flori-readiness"
        assert length == len(b"flori-readiness")
        client.remove_object.assert_called_once_with("b", key)
        assert result["status"] == "up"
        assert result["version"] == "RELEASE.test"

    @pytest.mark.asyncio
    async def test_readiness_collects_version_with_same_bounded_budget(
        self, tmp_path, monkeypatch,
    ):
        rs = RemoteStorage("h:9000", "k", "s", "b", False, tmp_root=tmp_path)
        client = MagicMock()
        monkeypatch.setattr(rs, "_readiness_client", lambda _timeout: client)
        version_probe = MagicMock(return_value="RELEASE.bounded")
        monkeypatch.setattr(rs, "_server_version_sync", version_probe)

        result = await rs.readiness_probe(timeout_sec=1)

        assert result["version"] == "RELEASE.bounded"
        version_probe.assert_called_once()
        assert 0 < version_probe.call_args.kwargs["timeout_sec"] <= 1

    @pytest.mark.asyncio
    async def test_readiness_delete_failure_fails_closed_and_retries_cleanup(
        self, tmp_path, monkeypatch,
    ):
        rs = RemoteStorage("h:9000", "k", "s", "b", False, tmp_root=tmp_path)
        client = MagicMock()
        client.remove_object.side_effect = [RuntimeError("delete denied"), None]
        monkeypatch.setattr(rs, "_readiness_client", lambda _timeout: client)

        with pytest.raises(RuntimeError, match="delete denied"):
            await rs.readiness_probe()
        assert client.remove_object.call_count == 2

    @pytest.mark.asyncio
    async def test_readiness_blackhole_io_is_bounded_across_repeated_probes(self, tmp_path):
        """本地黑洞 TCP 接受请求但不回 HTTP,验证 SDK read timeout 真正收回线程."""
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.settimeout(0.05)
        stop = threading.Event()
        active = 0
        lock = threading.Lock()

        def serve_blackhole():
            nonlocal active
            while not stop.is_set():
                try:
                    connection, _address = listener.accept()
                except (socket.timeout, OSError):
                    continue
                with lock:
                    active += 1
                connection.settimeout(0.05)
                try:
                    while not stop.is_set():
                        try:
                            if not connection.recv(65536):
                                break
                        except socket.timeout:
                            continue
                finally:
                    connection.close()
                    with lock:
                        active -= 1

        thread = threading.Thread(target=serve_blackhole, daemon=True)
        thread.start()
        endpoint = f"127.0.0.1:{listener.getsockname()[1]}"
        storage = RemoteStorage(endpoint, "k", "s", "b", False, tmp_root=tmp_path)
        started = time.monotonic()
        try:
            for _ in range(3):
                with pytest.raises(Exception):
                    await storage.readiness_probe(timeout_sec=0.3)
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                with lock:
                    if active == 0:
                        break
                import asyncio
                await asyncio.sleep(0.02)
            with lock:
                assert active == 0
            assert time.monotonic() - started < 2
        finally:
            stop.set()
            listener.close()
            thread.join(timeout=1)

    @pytest.mark.asyncio
    async def test_health_includes_server_version(self, monkeypatch):
        # health 把 MinIO 服务端版本(经 MinioAdmin.info)填进 version 字段。
        rs = RemoteStorage("h:9000", "k", "s", "b", False, tmp_root=None)
        client = MagicMock()
        client.bucket_exists.return_value = True
        monkeypatch.setattr(rs, "_client", lambda: client)
        monkeypatch.setattr(rs, "_server_version_sync", lambda: "RELEASE.2025-09-07T16-13-09Z")

        h = await rs.health()
        assert h["status"] == "up"
        assert h["version"] == "RELEASE.2025-09-07T16-13-09Z"

    @pytest.mark.asyncio
    async def test_health_version_none_on_admin_failure(self, monkeypatch):
        # MinioAdmin 取版本失败绝不让 health 报错/变慢:version 回 None,探活照常返回。
        import minio

        rs = RemoteStorage("h:9000", "k", "s", "b", False, tmp_root=None)
        client = MagicMock()
        client.bucket_exists.return_value = True
        monkeypatch.setattr(rs, "_client", lambda: client)

        # 构造即抛(模拟连不上/凭证错):_server_version_sync 内 try/except 吞掉 → version None。
        monkeypatch.setattr(minio, "MinioAdmin", MagicMock(side_effect=RuntimeError("admin unreachable")))
        h = await rs.health()
        assert h["status"] == "up"
        assert h["version"] is None


class _GatewayStorageHelpers:
    """GatewayStorage 测试共用 mock builder;下划线前缀让 pytest 不收集为测试类。"""

    def _gw(self, tmp_path):
        gw = GatewayStorage(
            "https://gw.example", token_getter=lambda: "wt", work_dir=tmp_path / "work",
        )
        client = MagicMock()
        client.get = AsyncMock()
        client.put = AsyncMock()
        client.stream = MagicMock()   # 流式下载:同步返回 async-CM,再 await __aenter__
        gw._client_obj = client
        return gw, client

    def _resp(self, status_code=200, content=b"", json_data=None):
        r = MagicMock()
        r.status_code = status_code
        r.content = content
        r.json.return_value = json_data if json_data is not None else {}
        r.raise_for_status = MagicMock()
        return r

    def _stream_cm(self, content=b"", status_code=200):
        """模拟 httpx client.stream(...) 返回的 async context manager(resp 有 aiter_bytes)。"""
        resp = MagicMock()
        resp.status_code = status_code
        resp.raise_for_status = MagicMock()

        async def _aiter(chunk_size=65536):
            yield content

        resp.aiter_bytes = _aiter
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=resp)
        cm.__aexit__ = AsyncMock(return_value=False)
        return cm


class TestGatewayStorage(_GatewayStorageHelpers):

    @pytest.mark.asyncio
    async def test_artifact_requests_carry_current_task_lease(self, tmp_path):
        gw, client = self._gw(tmp_path)
        client.get.return_value = self._resp(json_data={"files": []})
        bind_task_lease(TaskLease("worker-1", "j1", "A", "exec-1"))
        try:
            assert await gw.list_files("j1") == []
        finally:
            clear_task_lease()
        headers = client.get.call_args.kwargs["headers"]
        assert headers["X-Flori-Lease-Job"] == "j1"
        assert headers["X-Flori-Lease-Step"] == "A"
        assert headers["X-Flori-Lease-Exec"] == "exec-1"

    @pytest.mark.asyncio
    async def test_pull_downloads_manifest_and_objects_and_snapshots(self, tmp_path):
        gw, client = self._gw(tmp_path)
        # 清单走 .get(返回 json),逐个产物走流式 .stream 下载到磁盘
        client.get.return_value = self._resp(json_data={"files": ["job.json", "out/n.md"]})

        def _stream(method, url, headers=None):
            return self._stream_cm(b"J" if url.endswith("job.json") else b"NOTE")

        client.stream.side_effect = _stream

        work_dir = await gw.pull("j1", "01")
        assert work_dir == tmp_path / "work" / "j1"
        assert (work_dir / "job.json").read_bytes() == b"J"
        assert (work_dir / "out" / "n.md").read_bytes() == b"NOTE"
        # 清单调用带 token_getter 的认证头
        assert client.get.call_args_list[0].kwargs["headers"]["Authorization"] == "Bearer wt"
        # 流式下载也带认证头
        assert client.stream.call_args.kwargs["headers"]["Authorization"] == "Bearer wt"
        # 快照记下,供 push 算增量
        snap = gw._snapshots[str(work_dir)]
        assert set(snap) == {"job.json", "out/n.md"}

    @pytest.mark.asyncio
    async def test_pull_disconnect_preserves_target_and_cleans_staging(self, tmp_path):
        gw, client = self._gw(tmp_path)
        client.get.return_value = self._resp(json_data={"files": ["job.json"]})
        work_dir = tmp_path / "work" / "j1"
        work_dir.mkdir(parents=True)
        target = work_dir / "job.json"
        target.write_bytes(b"old")

        resp = MagicMock(status_code=200)
        resp.raise_for_status = MagicMock()

        async def _broken(chunk_size=65536):
            yield b"partial"
            raise ConnectionError("download interrupted")

        resp.aiter_bytes = _broken
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=resp)
        cm.__aexit__ = AsyncMock(return_value=False)
        client.stream.return_value = cm

        with pytest.raises(ConnectionError, match="download interrupted"):
            await gw.pull("j1", "01")
        assert target.read_bytes() == b"old"
        assert not list(work_dir.glob(".*.flori-part-*"))

    @pytest.mark.asyncio
    async def test_push_uploads_only_changed(self, tmp_path):
        gw, client = self._gw(tmp_path)
        client.put.return_value = self._resp()
        work_dir = tmp_path / "work" / "j1"
        (work_dir / "out").mkdir(parents=True)
        unchanged = work_dir / "job.json"
        unchanged.write_bytes(b"J")
        new = work_dir / "out" / "n.md"
        new.write_bytes(b"NOTE")
        # 快照只含 job.json 当前指纹 → 仅 out/n.md 视为新增
        st = unchanged.stat()
        gw._snapshots[str(work_dir)] = {"job.json": (st.st_size, st.st_mtime)}

        await gw.push("j1", "01", work_dir)

        put_urls = [c.args[0] for c in client.put.call_args_list]
        assert put_urls == ["/api/runner/jobs/j1/artifacts/out/n.md"]
        content = client.put.call_args.kwargs["content"]
        # 大文件流式上传:content 是 async 生成器,消费后应还原完整字节
        assert b"".join([c async for c in content]) == b"NOTE"

    @pytest.mark.asyncio
    async def test_read_file_404_returns_none(self, tmp_path):
        gw, client = self._gw(tmp_path)
        client.get.return_value = self._resp(status_code=404)
        assert await gw.read_file("j1", "missing.md") is None

    @pytest.mark.asyncio
    async def test_read_file_returns_bytes(self, tmp_path):
        gw, client = self._gw(tmp_path)
        client.get.return_value = self._resp(content=b"data")
        assert await gw.read_file("j1", "job.json") == b"data"

    @pytest.mark.asyncio
    async def test_write_file_puts(self, tmp_path):
        gw, client = self._gw(tmp_path)
        client.put.return_value = self._resp()
        await gw.write_file("j1", "job.json", b"X")
        assert client.put.call_args.args[0] == "/api/runner/jobs/j1/artifacts/job.json"
        assert client.put.call_args.kwargs["content"] == b"X"

    @pytest.mark.asyncio
    async def test_list_files_auth_rejected_raises(self, tmp_path):
        gw, client = self._gw(tmp_path)
        client.get.return_value = self._resp(status_code=401)
        with pytest.raises(WorkerAuthRejected):
            await gw.list_files("j1")

    @pytest.mark.asyncio
    async def test_list_files_auth_throttled_raises(self, tmp_path):
        gw, client = self._gw(tmp_path)
        client.get.return_value = self._resp(status_code=429)
        with pytest.raises(WorkerAuthRejected):
            await gw.list_files("j1")

    @pytest.mark.asyncio
    async def test_read_file_auth_rejected_raises(self, tmp_path):
        gw, client = self._gw(tmp_path)
        client.get.return_value = self._resp(status_code=403)
        with pytest.raises(WorkerAuthRejected):
            await gw.read_file("j1", "job.json")

    @pytest.mark.asyncio
    async def test_write_file_auth_rejected_raises(self, tmp_path):
        gw, client = self._gw(tmp_path)
        client.put.return_value = self._resp(status_code=401)
        with pytest.raises(WorkerAuthRejected):
            await gw.write_file("j1", "job.json", b"X")

    @pytest.mark.asyncio
    async def test_pull_object_auth_rejected_raises(self, tmp_path):
        gw, client = self._gw(tmp_path)
        client.get.return_value = self._resp(json_data={"files": ["job.json"]})
        client.stream.return_value = self._stream_cm(status_code=403)
        with pytest.raises(WorkerAuthRejected):
            await gw.pull("j1", "01")

    @pytest.mark.asyncio
    async def test_push_auth_rejected_raises(self, tmp_path):
        gw, client = self._gw(tmp_path)
        client.put.return_value = self._resp(status_code=403)
        work_dir = tmp_path / "work" / "j1"
        work_dir.mkdir(parents=True)
        (work_dir / "job.json").write_bytes(b"J")
        gw._snapshots[str(work_dir)] = {}

        with pytest.raises(WorkerAuthRejected):
            await gw.push("j1", "01", work_dir)

    @pytest.mark.asyncio
    async def test_cleanup_rmtree(self, tmp_path):
        gw, _ = self._gw(tmp_path)
        work_dir = tmp_path / "work" / "j1"
        work_dir.mkdir(parents=True)
        (work_dir / "f").write_text("x")
        gw._snapshots[str(work_dir)] = {"f": (1, 1.0)}

        await gw.cleanup("j1", "01", work_dir)
        assert not work_dir.exists()
        assert str(work_dir) not in gw._snapshots

    @pytest.mark.asyncio
    async def test_delete_removes_workdir(self, tmp_path):
        # gateway 侧不删中心产物(API 端 Local/Remote 负责),仅清本机留存的 job 工作目录+快照。
        gw, _ = self._gw(tmp_path)
        work_dir = tmp_path / "work" / "j1"
        work_dir.mkdir(parents=True)
        (work_dir / "f").write_text("x")
        gw._snapshots[str(work_dir)] = {"f": (1, 1.0)}

        await gw.delete("j1")
        assert not work_dir.exists()
        assert str(work_dir) not in gw._snapshots


class TestGatewayStorageReuse(_GatewayStorageHelpers):
    """STORAGE_WORKDIR_REUSE + STORAGE_NO_PUSH_GLOBS:大源文件留本机、不走慢链路。"""

    @pytest.mark.asyncio
    async def test_no_push_skips_matching_glob(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STORAGE_NO_PUSH_GLOBS", "input/source.mp4,input/source.mp3")
        gw, client = self._gw(tmp_path)
        client.put.return_value = self._resp()
        work_dir = tmp_path / "work" / "j1"
        (work_dir / "input").mkdir(parents=True)
        (work_dir / "out").mkdir(parents=True)
        (work_dir / "input" / "source.mp4").write_bytes(b"BIGVIDEO")
        (work_dir / "out" / "frame.jpg").write_bytes(b"IMG")
        gw._snapshots[str(work_dir)] = {}  # 都视为新增

        await gw.push("j1", "02", work_dir)

        put_urls = [c.args[0] for c in client.put.call_args_list]
        # 帧图回传,source.mp4 被挡(留本机)
        assert "/api/runner/jobs/j1/artifacts/out/frame.jpg" in put_urls
        assert "/api/runner/jobs/j1/artifacts/input/source.mp4" not in put_urls

    @pytest.mark.asyncio
    async def test_reuse_pull_skips_locally_present(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STORAGE_WORKDIR_REUSE", "1")
        gw, client = self._gw(tmp_path)
        # 上一步留下的 source.mp4 已在本机
        work_dir = tmp_path / "work" / "j1"
        (work_dir / "input").mkdir(parents=True)
        (work_dir / "input" / "source.mp4").write_bytes(b"LOCAL")

        client.get.return_value = self._resp(
            json_data={"files": ["input/source.mp4", "job.json"]})

        def _stream(method, url, headers=None):
            if url.endswith("job.json"):
                return self._stream_cm(b"J")
            raise AssertionError(f"unexpected stream {url}")  # 不该重拉 source.mp4

        client.stream.side_effect = _stream

        out = await gw.pull("j1", "02")
        streamed = [c.args[1] for c in client.stream.call_args_list]
        assert "/api/runner/jobs/j1/artifacts/input/source.mp4" not in streamed
        assert (out / "input" / "source.mp4").read_bytes() == b"LOCAL"  # 本机原样保留
        # 快照覆盖全部本机文件(含留下的 mp4),push 才不会误传
        assert set(gw._snapshots[str(out)]) == {"input/source.mp4", "job.json"}

    @pytest.mark.asyncio
    async def test_reuse_cleanup_keeps_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STORAGE_WORKDIR_REUSE", "1")
        gw, _ = self._gw(tmp_path)
        work_dir = tmp_path / "work" / "j1"
        work_dir.mkdir(parents=True)
        (work_dir / "f").write_text("x")
        gw._snapshots[str(work_dir)] = {"f": (1, 1.0)}

        await gw.cleanup("j1", "02", work_dir)
        assert work_dir.exists()  # 复用:目录留住给下一步
        assert str(work_dir) not in gw._snapshots  # 快照仍清掉

    @pytest.mark.asyncio
    async def test_reuse_gc_removes_stale_sibling(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STORAGE_WORKDIR_REUSE", "1")
        monkeypatch.setenv("STORAGE_WORKDIR_GC_TTL_SEC", "100")
        gw, client = self._gw(tmp_path)
        work_root = tmp_path / "work"
        stale = work_root / "old_job"
        stale.mkdir(parents=True)
        (stale / "input").mkdir()
        (stale / "input" / "source.mp4").write_bytes(b"OLD")
        os.utime(stale, (0, 0))  # 远早于 TTL

        client.get.side_effect = lambda url, headers=None: self._resp(json_data={"files": []})

        await gw.pull("j2", "00")
        assert not stale.exists()  # 过期兄弟目录被回收
        assert (work_root / "j2").exists()  # 当前 job 目录保留

        monkeypatch.delenv("MINIO_URL", raising=False)
        s = create_storage(tmp_path)
        assert isinstance(s, LocalStorage)

    def test_minio_selects_remote(self, tmp_path, monkeypatch):
        # 设了 MINIO_URL 选 RemoteStorage(延迟连接,构造不需 minio 服务)。
        monkeypatch.setenv("MINIO_URL", "minio:9000")
        s = create_storage(tmp_path)
        assert isinstance(s, RemoteStorage)


class TestStorageHealth:
    @pytest.mark.asyncio
    async def test_local_health_is_local_mode(self, tmp_path):
        h = await LocalStorage(tmp_path).health()
        assert h["mode"] == "local" and h["status"] == "unknown"
        assert h["bucket"] is None

    @pytest.mark.asyncio
    async def test_remote_health_bucket_exists_up(self, tmp_path):
        rs = RemoteStorage("h:9000", "k", "s", "flori", False, tmp_root=tmp_path)
        client = MagicMock()
        client.bucket_exists.return_value = True
        rs._client = lambda: client
        h = await rs.health()
        assert h["status"] == "up" and h["mode"] == "remote"
        assert h["bucket"] == "flori" and h["bucket_exists"] is True
        assert isinstance(h["probe_ms"], float)
        client.bucket_exists.assert_called_once_with("flori")

    @pytest.mark.asyncio
    async def test_remote_health_missing_bucket_degraded(self, tmp_path):
        rs = RemoteStorage("h:9000", "k", "s", "flori", False, tmp_root=tmp_path)
        client = MagicMock()
        client.bucket_exists.return_value = False
        rs._client = lambda: client
        h = await rs.health()
        assert h["status"] == "degraded" and h["bucket_exists"] is False
        assert "flori" in h["detail"]

    @pytest.mark.asyncio
    async def test_gateway_health_stub(self, tmp_path):
        gw = GatewayStorage("https://gw", lambda: "tok", tmp_path)
        h = await gw.health()
        assert h["mode"] == "gateway" and h["status"] == "unknown"


class TestStorageCapacity:
    @pytest.mark.asyncio
    async def test_remote_capacity_sums_objects_and_bytes(self, tmp_path):
        # 全量 list bucket(无前缀,recursive)求对象数 + 总字节。
        rs = RemoteStorage("h:9000", "k", "s", "b", False, tmp_root=tmp_path)
        objs = [
            MagicMock(size=100), MagicMock(size=250), MagicMock(size=0),
        ]
        client = MagicMock()
        client.list_objects.return_value = objs
        rs._client = lambda: client
        cap = await rs.capacity()
        assert cap == {"objects": 3, "bytes": 350}
        client.list_objects.assert_called_once_with("b", recursive=True)

    @pytest.mark.asyncio
    async def test_remote_capacity_handles_none_size(self, tmp_path):
        # obj.size 为 None(某些 SDK 列举返回)按 0 计,不抛。
        rs = RemoteStorage("h:9000", "k", "s", "b", False, tmp_root=tmp_path)
        client = MagicMock()
        client.list_objects.return_value = [MagicMock(size=None), MagicMock(size=10)]
        rs._client = lambda: client
        cap = await rs.capacity()
        assert cap == {"objects": 2, "bytes": 10}

    @pytest.mark.asyncio
    async def test_remote_capacity_empty_bucket(self, tmp_path):
        rs = RemoteStorage("h:9000", "k", "s", "b", False, tmp_root=tmp_path)
        client = MagicMock()
        client.list_objects.return_value = []
        rs._client = lambda: client
        assert await rs.capacity() == {"objects": 0, "bytes": 0}

    @pytest.mark.asyncio
    async def test_local_capacity_walks_jobs_dir(self, tmp_path):
        job = tmp_path / "j_a" / "output"
        job.mkdir(parents=True)
        (job / "notes.md").write_bytes(b"hello")          # 5
        (tmp_path / "j_a" / "job.json").write_bytes(b"{}")  # 2
        cap = await LocalStorage(tmp_path).capacity()
        assert cap == {"objects": 2, "bytes": 7}

    @pytest.mark.asyncio
    async def test_local_capacity_missing_dir_is_zero(self, tmp_path):
        cap = await LocalStorage(tmp_path / "nope").capacity()
        assert cap == {"objects": 0, "bytes": 0}

    @pytest.mark.asyncio
    async def test_gateway_capacity_is_none(self, tmp_path):
        gw = GatewayStorage("https://gw", lambda: "tok", tmp_path)
        assert await gw.capacity() is None
