"""shared/subscriptions/local_dir.py(本地目录订阅适配器)单测。"""

from __future__ import annotations

import os

import pytest

from shared.subscriptions import local_dir
from shared.subscriptions.base import SourceContext, SourceItem


def _write(p, content=b"x"):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    return p


class TestScanDir:
    def test_content_type_by_extension(self, tmp_path):
        _write(tmp_path / "paper.pdf")
        _write(tmp_path / "clip.mp4")
        _write(tmp_path / "talk.mkv")
        _write(tmp_path / "movie.webm")
        _write(tmp_path / "rec.mov")
        _write(tmp_path / "ep.mp3")
        _write(tmp_path / "ep.m4a")
        _write(tmp_path / "ep.wav")
        _write(tmp_path / "ep.flac")
        _write(tmp_path / "note.md")
        _write(tmp_path / "readme.txt")
        _write(tmp_path / "page.html")

        items = local_dir.scan_dir(str(tmp_path))
        by_name = {
            it.title: (it.content_type, it.document_kind) for it in items
        }
        assert by_name == {
            "paper.pdf": ("document", "unknown"),
            "clip.mp4": ("video", None),
            "talk.mkv": ("video", None),
            "movie.webm": ("video", None),
            "rec.mov": ("video", None),
            "ep.mp3": ("audio", None),
            "ep.m4a": ("audio", None),
            "ep.wav": ("audio", None),
            "ep.flac": ("audio", None),
            "note.md": ("document", "unknown"),
            "readme.txt": ("document", "unknown"),
            "page.html": ("document", "unknown"),
        }

    def test_unsupported_extensions_ignored(self, tmp_path):
        _write(tmp_path / "keep.pdf")
        _write(tmp_path / "skip.zip")
        _write(tmp_path / "skip.docx")
        _write(tmp_path / "skip")  # no extension
        items = local_dir.scan_dir(str(tmp_path))
        assert [it.title for it in items] == ["keep.pdf"]

    def test_recursive_scan(self, tmp_path):
        _write(tmp_path / "top.pdf")
        _write(tmp_path / "sub" / "nested.mp4")
        _write(tmp_path / "sub" / "deep" / "deeper.mp3")
        items = local_dir.scan_dir(str(tmp_path))
        titles = sorted(it.title for it in items)
        assert titles == ["deeper.mp3", "nested.mp4", "top.pdf"]

    def test_url_is_file_scheme_absolute(self, tmp_path):
        f = _write(tmp_path / "doc.pdf")
        items = local_dir.scan_dir(str(tmp_path))
        assert len(items) == 1
        assert items[0].url == f"file://{f.resolve()}"
        assert items[0].url.startswith("file://")

    def test_item_id_format_rel_size_mtime(self, tmp_path):
        f = _write(tmp_path / "sub" / "a.pdf", b"hello")
        st = f.stat()
        items = local_dir.scan_dir(str(tmp_path))
        assert len(items) == 1
        expected = f"{os.path.join('sub', 'a.pdf')}|{st.st_size}|{int(st.st_mtime)}"
        assert items[0].item_id == expected

    def test_item_id_stable_when_unchanged(self, tmp_path):
        _write(tmp_path / "a.pdf", b"same")
        first = local_dir.scan_dir(str(tmp_path))
        second = local_dir.scan_dir(str(tmp_path))
        # 同一未改动文件多次枚举给同一 item_id → sync 层据此去重,不重复建 job。
        assert [it.item_id for it in first] == [it.item_id for it in second]

    def test_item_id_changes_when_file_modified(self, tmp_path):
        f = _write(tmp_path / "a.pdf", b"v1")
        first = local_dir.scan_dir(str(tmp_path))[0].item_id
        # 改大小 + mtime → item_id 变化(取最新内容重新入库)。
        os.utime(f, (f.stat().st_atime, f.stat().st_mtime + 10))
        f.write_bytes(b"v2-bigger-content")
        second = local_dir.scan_dir(str(tmp_path))[0].item_id
        assert first != second

    def test_missing_dir_returns_empty(self, tmp_path):
        assert local_dir.scan_dir(str(tmp_path / "nope")) == []

    def test_path_to_file_not_dir_returns_empty(self, tmp_path):
        f = _write(tmp_path / "a.pdf")
        assert local_dir.scan_dir(str(f)) == []


class TestEnumerateLocalDir:
    @pytest.mark.asyncio
    async def test_returns_basename_title_and_items(self, tmp_path):
        d = tmp_path / "inbox"
        _write(d / "a.pdf")
        _write(d / "b.mp4")
        title, items = await local_dir.enumerate_local_dir(str(d), SourceContext())
        assert title == "inbox"
        assert sorted(it.title for it in items) == ["a.pdf", "b.mp4"]
        assert all(isinstance(it, SourceItem) for it in items)

    @pytest.mark.asyncio
    async def test_trailing_slash_basename(self, tmp_path):
        d = tmp_path / "inbox"
        d.mkdir()
        title, _items = await local_dir.enumerate_local_dir(str(d) + "/", SourceContext())
        assert title == "inbox"

    @pytest.mark.asyncio
    async def test_calls_scan_dir_via_module_attr(self, tmp_path, monkeypatch):
        # 经模块属性调用 → monkeypatch 生效(契约要求,便于 mock 外部枚举)。
        sentinel = [SourceItem(item_id="x|1|1", title="x.pdf", url="file:///x.pdf",
                               content_type="document", document_kind="research_paper")]
        monkeypatch.setattr("shared.subscriptions.local_dir.scan_dir", lambda root: sentinel)
        _title, items = await local_dir.enumerate_local_dir(str(tmp_path), SourceContext())
        assert items is sentinel

    @pytest.mark.asyncio
    async def test_registered_under_local_dir(self):
        from shared.subscriptions.base import SOURCE_ADAPTERS, source_label
        # 注册靠 import 副作用,不依赖别处 eager-import;此处显式 import 触发注册。
        import shared.subscriptions.local_dir  # noqa: F401
        assert SOURCE_ADAPTERS.get("local_dir") is local_dir.enumerate_local_dir
        assert source_label("local_dir") == "local"
