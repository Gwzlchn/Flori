"""shared/storage.py 的单测。"""

import os
import socket
import threading
import time
import tracemalloc
from unittest.mock import AsyncMock, MagicMock

import pytest

from shared.errors import WorkerAuthRejected
from shared.runner_ops import TaskLease, bind_task_lease, clear_task_lease
from shared.storage import (
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
        assert not list((tmp_path / "j1" / ".flori-upload").glob("*"))

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
        client.list_objects.return_value = objs
        client.remove_objects.return_value = []  # 无删除错误
        rs._client = lambda: client

        await rs.delete("j1")

        client.list_objects.assert_called_once_with("b", prefix="j1/", recursive=True)
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
        rs._client = lambda: client

        async def source():
            yield b"abc"
            yield b"def"

        result = await rs.write_stream(
            "j1", "out/a.bin", source(),
            expected_size=6, expected_sha256=hashlib.sha256(b"abcdef").hexdigest(),
        )
        assert result == {"size": 6, "sha256": hashlib.sha256(b"abcdef").hexdigest()}
        staging_key = client.fput_object.call_args.args[1]
        assert staging_key.startswith("j1/.flori-upload/")
        copy_source = client.copy_object.call_args.args[2]
        assert client.copy_object.call_args.args[:2] == ("b", "j1/out/a.bin")
        assert copy_source.object_name == staging_key
        client.remove_object.assert_called_once_with("b", staging_key)
        assert not list((tmp_path / ".flori-upload").glob("*"))

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
        client.copy_object.side_effect = RuntimeError("copy failed")
        rs._client = lambda: client

        async def source():
            yield b"partial"

        with pytest.raises(RuntimeError, match="copy failed"):
            await rs.write_stream("j1", "out/a.bin", source())
        staging_key = client.fput_object.call_args.args[1]
        client.remove_object.assert_called_once_with("b", staging_key)
        assert not list((tmp_path / ".flori-upload").glob("*"))

    @pytest.mark.asyncio
    async def test_upload_failure_still_attempts_staging_cleanup(self, tmp_path):
        rs = RemoteStorage("h:9000", "k", "s", "b", False, tmp_root=tmp_path)
        client = MagicMock()
        client.fput_object.side_effect = ConnectionError("upload interrupted")
        rs._client = lambda: client

        async def source():
            yield b"partial"

        with pytest.raises(ConnectionError, match="upload interrupted"):
            await rs.write_stream("j1", "out/a.bin", source())
        staging_key = client.fput_object.call_args.args[1]
        client.remove_object.assert_called_once_with("b", staging_key)
        assert not list((tmp_path / ".flori-upload").glob("*"))


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
