"""tests for shared/storage.py"""

import os

import pytest

from shared.storage import LocalStorage, RemoteStorage, create_storage


class TestLocalStorage:
    @pytest.fixture
    def storage(self, tmp_path):
        return LocalStorage(tmp_path)

    @pytest.mark.asyncio
    async def test_pull_returns_path(self, storage, tmp_path):
        path = await storage.pull("j_xxx", "01_scene")
        assert path == tmp_path / "j_xxx"

    @pytest.mark.asyncio
    async def test_push_noop(self, storage, tmp_path):
        job_dir = tmp_path / "j_xxx"
        job_dir.mkdir(parents=True)
        (job_dir / "test.txt").write_text("hello")
        await storage.push("j_xxx", "01_scene", job_dir)
        # LocalStorage.push is a no-op — files should remain unchanged
        assert (job_dir / "test.txt").read_text() == "hello"

    @pytest.mark.asyncio
    async def test_cleanup_noop(self, storage, tmp_path):
        job_dir = tmp_path / "j_xxx"
        job_dir.mkdir(parents=True)
        (job_dir / "test.txt").write_text("data")
        await storage.cleanup("j_xxx", "01_scene", job_dir)
        # LocalStorage.cleanup is a no-op — directory should still exist
        assert job_dir.exists()
        assert (job_dir / "test.txt").read_text() == "data"


    @pytest.mark.asyncio
    async def test_pull_missing_dir(self, storage, tmp_path):
        path = await storage.pull("nonexistent", "01_scene")
        assert path == tmp_path / "nonexistent"

    @pytest.mark.asyncio
    async def test_read_file(self, storage, tmp_path):
        out = tmp_path / "j_xxx" / "output"
        out.mkdir(parents=True)
        (out / "notes_smart.md").write_text("note")
        assert await storage.read_file("j_xxx", "output/notes_smart.md") == b"note"
        assert await storage.read_file("j_xxx", "output/missing.md") is None


class TestCreateStorage:
    def test_default_local(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MINIO_URL", raising=False)
        s = create_storage(tmp_path)
        assert isinstance(s, LocalStorage)

    def test_minio_selects_remote(self, tmp_path, monkeypatch):
        # 设了 MINIO_URL 选 RemoteStorage(延迟连接,构造不需 minio 服务)。
        monkeypatch.setenv("MINIO_URL", "minio:9000")
        s = create_storage(tmp_path)
        assert isinstance(s, RemoteStorage)
