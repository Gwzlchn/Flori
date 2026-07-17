"""steps/common/step_01_download.py 的测试。"""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from steps.common.step_01_download import DownloadStep
from tests.steps.conftest import make_step_config


class TestDownloadStep:
    def _make(self, job_dir, tmp_path, url="https://example.com/v.mp4", source=None,
              content_type="video", document_kind=None):
        job_data = {"url": url, "content_type": content_type}
        if document_kind:
            job_data["document_kind"] = document_kind
        if source:
            job_data["source"] = source
        (job_dir / "job.json").write_text(json.dumps(job_data))
        config = make_step_config(tmp_path, step_name="01_download", pool="io")
        return DownloadStep("01_download", job_dir, config)

    def test_validate_inputs_missing(self, tmp_path):
        job_dir = tmp_path / "job"
        job_dir.mkdir()
        config = make_step_config(tmp_path, step_name="01_download")
        step = DownloadStep("01_download", job_dir, config)
        assert step.validate_inputs() == ["job.json"]

    def test_validate_inputs_present(self, tmp_path):
        job_dir = tmp_path / "job"
        job_dir.mkdir()
        (job_dir / "job.json").write_text('{"url": "test"}')
        config = make_step_config(tmp_path, step_name="01_download")
        step = DownloadStep("01_download", job_dir, config)
        assert step.validate_inputs() == []

    def test_source_detection_bilibili(self, tmp_path):
        job_dir = tmp_path / "job"
        job_dir.mkdir()
        for d in ["input", "intermediate", "output", "assets", "logs"]:
            (job_dir / d).mkdir()
        step = self._make(job_dir, tmp_path, url="https://www.bilibili.com/video/BV1xx411c7mD")
        with patch.object(step, "_download_bilibili") as mock_dl:
            result = step.execute()
            mock_dl.assert_called_once_with("https://www.bilibili.com/video/BV1xx411c7mD")
            assert result["source"] == "bilibili"

    def test_source_detection_youtube(self, tmp_path):
        job_dir = tmp_path / "job"
        job_dir.mkdir()
        for d in ["input", "intermediate", "output", "assets", "logs"]:
            (job_dir / d).mkdir()
        step = self._make(job_dir, tmp_path, url="https://www.youtube.com/watch?v=abc")
        with patch.object(step, "_download_youtube") as mock_dl:
            result = step.execute()
            mock_dl.assert_called_once_with("https://www.youtube.com/watch?v=abc")
            assert result["source"] == "youtube"

    def test_source_detection_arxiv(self, tmp_path):
        job_dir = tmp_path / "job"
        job_dir.mkdir()
        for d in ["input", "intermediate", "output", "assets", "logs"]:
            (job_dir / d).mkdir()
        step = self._make(job_dir, tmp_path, url="https://arxiv.org/abs/2301.00001",
                          content_type="document", document_kind="research_paper")
        with patch.object(step, "_download_arxiv") as mock_dl:
            result = step.execute()
            mock_dl.assert_called_once_with("https://arxiv.org/abs/2301.00001")
            assert result["source"] == "arxiv"

    def test_source_detection_pdf(self, tmp_path):
        # 非 arxiv 直链 PDF(OSDI/usenix 等)→ source=pdf,走 _download_pdf 存成 source.pdf。
        job_dir = tmp_path / "job"
        job_dir.mkdir()
        for d in ["input", "intermediate", "output", "assets", "logs"]:
            (job_dir / d).mkdir()
        url = "https://www.usenix.org/system/files/osdi23-li-zhuohan.pdf"
        step = self._make(job_dir, tmp_path, url=url, content_type="document",
                          document_kind="research_paper")
        with patch.object(step, "_download_pdf") as mock_dl:
            result = step.execute()
            mock_dl.assert_called_once_with(url)
            assert result["source"] == "pdf"

    def _mk_dirs(self, tmp_path):
        job_dir = tmp_path / "job"
        job_dir.mkdir()
        for d in ["input", "intermediate", "output", "assets", "logs"]:
            (job_dir / d).mkdir()
        return job_dir

    def test_audio_page_url_routes_to_download_audio(self, tmp_path):
        # content_type=audio + 播客页面 URL(无音频后缀)→ 走 _download_audio,不走 _download_article。
        job_dir = self._mk_dirs(tmp_path)
        url = "https://www.econtalk.org/some-episode/"
        step = self._make(job_dir, tmp_path, url=url, content_type="audio")
        with patch.object(step, "_download_audio") as mock_audio, \
                patch.object(step, "_download_article") as mock_article:
            step.execute()
            mock_audio.assert_called_once_with(url)
            mock_article.assert_not_called()

    def test_audio_direct_link_routes_to_download_audio(self, tmp_path):
        job_dir = self._mk_dirs(tmp_path)
        url = "https://cdn.example.com/ep42.mp3"
        step = self._make(job_dir, tmp_path, url=url, content_type="audio")
        with patch.object(step, "_download_audio") as mock_audio:
            result = step.execute()
            mock_audio.assert_called_once_with(url)
            assert result["source"] == "podcast"

    def test_verify_audio(self, tmp_path):
        job_dir = self._mk_dirs(tmp_path)
        step = self._make(job_dir, tmp_path, content_type="audio")
        p = job_dir / "input" / "source.mp3"
        p.write_bytes(b"\x00" * 100)                       # <2KB → False
        assert step._verify_audio(p) is False
        p.write_bytes(b"\x00" * 4096)
        with patch.object(step, "_get_video_duration", return_value=42.0):
            assert step._verify_audio(p) is True            # 有时长 → True
        with patch.object(step, "_get_video_duration", return_value=None):
            assert step._verify_audio(p) is False           # ffprobe 读不出时长(HTML/404)→ False

    def test_download_audio_recovers_from_landing_page(self, tmp_path, monkeypatch):
        # 直链回来的是落地页 HTML(部分 CDN 行为)→ 从内容解析音频真链重下一次。
        monkeypatch.setattr("shared.net.assert_public_url", lambda u: None)
        job_dir = self._mk_dirs(tmp_path)
        url = "https://cdn.example.com/ep.mp3"             # detect_source=podcast,跳过预解析
        step = self._make(job_dir, tmp_path, url=url, content_type="audio")
        html = '<audio src="https://cdn.example.com/real.mp3"></audio>'
        seq = []

        def fake_curl(u, dest):
            seq.append(u)
            dest.write_text(html) if u == url else dest.write_bytes(b"\x00" * 4096)

        monkeypatch.setattr(step, "_curl_to", fake_curl)
        monkeypatch.setattr(step, "_verify_audio", lambda p: p.read_bytes()[:1] == b"\x00")
        step._download_audio(url)
        assert seq == [url, "https://cdn.example.com/real.mp3"]

    def test_download_audio_raises_on_unplayable(self, tmp_path, monkeypatch):
        from shared.errors import InputInvalidError
        monkeypatch.setattr("shared.net.assert_public_url", lambda u: None)
        job_dir = self._mk_dirs(tmp_path)
        url = "https://cdn.example.com/ep.mp3"
        step = self._make(job_dir, tmp_path, url=url, content_type="audio")
        monkeypatch.setattr(step, "_curl_to", lambda u, dest: dest.write_text("<html>404</html>"))
        monkeypatch.setattr(step, "_verify_audio", lambda p: False)
        with pytest.raises(InputInvalidError):
            step._download_audio(url)

    def test_download_audio_resolves_page_then_downloads(self, tmp_path, monkeypatch):
        # 给的是播客页面(非直链)→ 先 _resolve_audio_from_page 取真链,再下载该真链。
        monkeypatch.setattr("shared.net.assert_public_url", lambda u: None)
        job_dir = self._mk_dirs(tmp_path)
        page = "https://pod.example.com/ep/7"
        step = self._make(job_dir, tmp_path, url=page, content_type="audio")
        monkeypatch.setattr(step, "_resolve_audio_from_page",
                            lambda u: "https://cdn.example.com/ep7.mp3")
        got = []
        monkeypatch.setattr(step, "_curl_to", lambda u, dest: got.append(u) or dest.write_bytes(b"\x00" * 4096))
        monkeypatch.setattr(step, "_verify_audio", lambda p: True)
        step._download_audio(page)
        assert got == ["https://cdn.example.com/ep7.mp3"]

    def test_upload_mode_skips_download(self, tmp_path):
        job_dir = tmp_path / "job"
        job_dir.mkdir()
        for d in ["input", "intermediate", "output", "assets", "logs"]:
            (job_dir / d).mkdir()
        (job_dir / "input" / "source.mp4").write_bytes(b"\x00" * 1024)
        step = self._make(job_dir, tmp_path, source="upload")
        result = step.execute()
        assert result["source"] == "upload"
        assert (job_dir / "input" / "metadata.json").exists()

    def test_document_upload_accepts_null_url(self, tmp_path):
        job_dir = self._mk_dirs(tmp_path)
        (job_dir / "input" / "source.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
        step = self._make(
            job_dir, tmp_path, url=None, source="upload",
            content_type="document", document_kind="research_paper",
        )

        result = step.execute()

        assert result["source"] == "upload"
        assert (job_dir / "input" / "metadata.json").exists()

    def test_metadata_extraction(self, tmp_path):
        job_dir = tmp_path / "job"
        job_dir.mkdir()
        input_dir = job_dir / "input"
        input_dir.mkdir()
        (input_dir / "source.mp4").write_bytes(b"\x00" * 1048576 * 2)
        (input_dir / "subtitle.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nhi\n")

        config = make_step_config(tmp_path, step_name="01_download")
        step = DownloadStep("01_download", job_dir, config)
        # mock 时长提取:去掉对宿主 ffprobe 的隐式依赖(假 mp4 喂 ffprobe 本就读不出时长),
        # 同时把 duration_sec 这条派生字段钉成确定值。
        with patch.object(step, "_get_video_duration", return_value=42.0):
            meta = step._extract_metadata("bilibili", "video")
        assert meta["source"] == "bilibili"
        assert meta["has_subtitle"] is True
        assert meta["file_size_mb"] > 0
        assert meta["duration_sec"] == 42.0

    _ARXIV_ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>BERT: Pre-training of Deep
      Bidirectional Transformers</title>
    <summary>We introduce BERT.</summary>
    <published>2018-10-11T17:08:54Z</published>
    <author><name>Jacob Devlin</name></author>
    <author><name>Ming-Wei Chang</name></author>
  </entry>
</feed>"""

    def test_fetch_arxiv_meta_and_merge(self, tmp_path):
        # arxiv API 元数据解析(标题去换行/作者/摘要/发布日),并入 _extract_metadata 时 API 为权威、优先于 PDF 解析。
        from types import SimpleNamespace
        job_dir = tmp_path / "job"; job_dir.mkdir(); (job_dir / "input").mkdir()
        step = self._make(job_dir, tmp_path, url="https://arxiv.org/abs/1810.04805",
                          content_type="document", document_kind="research_paper")
        with patch.object(step.commands, "run", return_value=SimpleNamespace(stdout=self._ARXIV_ATOM)):
            step._fetch_arxiv_meta("1810.04805")
        m = step._arxiv_meta
        assert m["title"] == "BERT: Pre-training of Deep Bidirectional Transformers"
        assert m["authors"] == ["Jacob Devlin", "Ming-Wei Chang"]
        assert m["abstract"] == "We introduce BERT."
        assert m["published_at"] == "2018-10-11"
        (job_dir / "input" / "source.pdf").write_bytes(b"%PDF-1.4")
        meta = step._extract_metadata("arxiv", "paper")
        assert meta["title"].startswith("BERT") and meta["authors"] == ["Jacob Devlin", "Ming-Wei Chang"]

    def test_fetch_arxiv_meta_network_failure_is_graceful(self, tmp_path):
        # 网络类失败(curl 挂)→ 不抛、不 stash;_extract_metadata 正常返回(回退 PDF 解析)。
        from shared.step_subprocess import SubprocessFailed
        job_dir = tmp_path / "job"; job_dir.mkdir(); (job_dir / "input").mkdir()
        step = self._make(job_dir, tmp_path, url="https://arxiv.org/abs/1810.04805",
                          content_type="document", document_kind="research_paper")
        with patch.object(step.commands, "run",
                          side_effect=SubprocessFailed(22, ["curl"], output="", stderr="timeout")):
            step._fetch_arxiv_meta("1810.04805")
        assert getattr(step, "_arxiv_meta", {}) == {}
        (job_dir / "input" / "source.pdf").write_bytes(b"%PDF-1.4")
        meta = step._extract_metadata("arxiv", "paper")
        assert "title" not in meta

    def test_fetch_arxiv_meta_bad_xml_is_graceful(self, tmp_path):
        # arxiv 返回坏响应(半截 HTML 等)→ ParseError 按网络类失败兜底,不抛不 stash。
        from types import SimpleNamespace
        job_dir = tmp_path / "job"; job_dir.mkdir(); (job_dir / "input").mkdir()
        step = self._make(job_dir, tmp_path, url="https://arxiv.org/abs/1810.04805",
                          content_type="document", document_kind="research_paper")
        with patch.object(step.commands, "run", return_value=SimpleNamespace(stdout="<html>oops")):
            step._fetch_arxiv_meta("1810.04805")
        assert getattr(step, "_arxiv_meta", {}) == {}

    def test_fetch_arxiv_meta_empty_feed_is_graceful(self, tmp_path):
        # 合法 Atom 但无 entry(id 不存在)→ 不 stash。
        from types import SimpleNamespace
        empty = '<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"></feed>'
        job_dir = tmp_path / "job"; job_dir.mkdir(); (job_dir / "input").mkdir()
        step = self._make(job_dir, tmp_path, url="https://arxiv.org/abs/1810.04805",
                          content_type="document", document_kind="research_paper")
        with patch.object(step.commands, "run", return_value=SimpleNamespace(stdout=empty)):
            step._fetch_arxiv_meta("1810.04805")
        assert getattr(step, "_arxiv_meta", {}) == {}

    def test_fetch_arxiv_meta_programming_error_fails_loud(self, tmp_path):
        # 编程/打包类错误(如曾经的 ModuleNotFoundError)不许静默吞——否则所有 arxiv 标题丢了都没人知道。
        import pytest
        job_dir = tmp_path / "job"; job_dir.mkdir(); (job_dir / "input").mkdir()
        step = self._make(job_dir, tmp_path, url="https://arxiv.org/abs/1810.04805",
                          content_type="document", document_kind="research_paper")
        with patch.object(step.commands, "run", side_effect=ModuleNotFoundError("feedparser")):
            with pytest.raises(ModuleNotFoundError):
                step._fetch_arxiv_meta("1810.04805")

    def test_idempotent(self, tmp_path):
        job_dir = tmp_path / "job"
        job_dir.mkdir()
        for d in ["input", "intermediate", "output", "assets", "logs"]:
            (job_dir / d).mkdir()
        step = self._make(job_dir, tmp_path, source="upload")
        step.execute()
        step.mark_done()
        step2 = self._make(job_dir, tmp_path, source="upload")
        assert step2.should_run() is False

    def test_rename_video_from_job_root(self, tmp_path):
        """yutto may download to job root instead of input/ — rename should find it."""
        job_dir = tmp_path / "job"
        job_dir.mkdir()
        input_dir = job_dir / "input"
        input_dir.mkdir()
        # yutto puts file in job root with video title as name
        (job_dir / "带你躺赢带你飞.mp4").write_bytes(b"\x00" * 2048)

        config = make_step_config(tmp_path, step_name="01_download")
        step = DownloadStep("01_download", job_dir, config)
        step._rename_downloaded_video(input_dir)

        assert (input_dir / "source.mp4").exists()
        assert not (job_dir / "带你躺赢带你飞.mp4").exists()

    def test_prune_danmaku_keeps_one(self, tmp_path):
        """多份 .ass(yutto 同时落 danmaku.ass 与 <标题>.ass)只保留一份 danmaku.ass。"""
        job_dir = tmp_path / "job"
        job_dir.mkdir()
        input_dir = job_dir / "input"
        input_dir.mkdir()
        (input_dir / "video_title.ass").write_text("[Script Info]")
        (input_dir / "danmaku.ass").write_text("[Script Info]")

        config = make_step_config(tmp_path, step_name="01_download")
        step = DownloadStep("01_download", job_dir, config)
        step._prune_subtitles_danmaku(input_dir)

        assert (input_dir / "danmaku.ass").exists()
        assert len(list(input_dir.glob("*.ass"))) == 1

    def test_local_file_copies_pdf(self, tmp_path):
        """file:// url(本地目录订阅)→ 复制宿主文件进 input/source.pdf,跳过网络下载。"""
        job_dir = tmp_path / "job"
        job_dir.mkdir()
        for d in ["input", "intermediate", "output", "assets", "logs"]:
            (job_dir / d).mkdir()
        src = tmp_path / "inbox" / "paper.pdf"
        src.parent.mkdir()
        src.write_bytes(b"%PDF-1.4 fake pdf content")

        step = self._make(job_dir, tmp_path, url=f"file://{src}", content_type="document",
                          document_kind="research_paper")
        result = step.execute()

        assert result["source"] == "local_file"
        copied = job_dir / "input" / "source.pdf"
        assert copied.exists()
        assert copied.read_bytes() == b"%PDF-1.4 fake pdf content"
        assert (job_dir / "input" / "metadata.json").exists()

    def test_local_file_video_runs_verify(self, tmp_path):
        """video 类本地文件复制后走 ffprobe 校验:坏文件(无时长)应被挡。"""
        from shared.errors import InputInvalidError

        job_dir = tmp_path / "job"
        job_dir.mkdir()
        for d in ["input", "intermediate", "output", "assets", "logs"]:
            (job_dir / d).mkdir()
        src = tmp_path / "inbox" / "clip.mp4"
        src.parent.mkdir()
        src.write_bytes(b"\x00" * 16)  # 太小且无时长 → _verify_download 抛错

        step = self._make(job_dir, tmp_path, url=f"file://{src}", content_type="video")
        with pytest.raises(InputInvalidError):
            step.execute()

    def test_local_file_missing_raises(self, tmp_path):
        """file:// 指向不存在的本地文件 → InputInvalidError(不静默成功)。"""
        from shared.errors import InputInvalidError

        job_dir = tmp_path / "job"
        job_dir.mkdir()
        for d in ["input", "intermediate", "output", "assets", "logs"]:
            (job_dir / d).mkdir()
        step = self._make(job_dir, tmp_path,
                          url=f"file://{tmp_path / 'inbox' / 'gone.pdf'}",
                          content_type="document", document_kind="research_paper")
        with pytest.raises(InputInvalidError):
            step.execute()

    def test_local_file_no_network_download_called(self, tmp_path):
        """file:// 分支绝不触发 generic/yt-dlp 网络下载。"""
        job_dir = tmp_path / "job"
        job_dir.mkdir()
        for d in ["input", "intermediate", "output", "assets", "logs"]:
            (job_dir / d).mkdir()
        src = tmp_path / "inbox" / "note.txt"
        src.parent.mkdir()
        src.write_text("hello article body")

        step = self._make(job_dir, tmp_path, url=f"file://{src}", content_type="document",
                          document_kind="article")
        with patch.object(step, "_download_generic") as mock_generic:
            result = step.execute()
            mock_generic.assert_not_called()
        assert result["source"] == "local_file"
        html = (job_dir / "input" / "source.html").read_text()
        assert "hello article body" in html
        assert not (job_dir / "input" / "source.txt").exists()

    def test_article_markdown_is_normalized_and_escaped(self, tmp_path):
        input_dir = tmp_path / "input"
        input_dir.mkdir()
        (input_dir / "source.md").write_text("# title\n<script>bad</script>")

        DownloadStep._normalize_article_input(input_dir)

        html = (input_dir / "source.html").read_text()
        assert "# title" in html
        assert "&lt;script&gt;bad&lt;/script&gt;" in html
        assert not (input_dir / "source.md").exists()

    def test_flac_is_linked_for_whisper(self, tmp_path):
        input_dir = tmp_path / "input"
        input_dir.mkdir()
        (input_dir / "source.flac").write_bytes(b"flac-audio")
        step = self._make(tmp_path, tmp_path, url=None, content_type="audio")

        step._link_audio_for_whisper(input_dir)

        assert (input_dir / "source.mp4").read_bytes() == b"flac-audio"

    def test_prune_subtitle_keeps_chinese_only(self, tmp_path):
        """原生中文视频:只留中文字幕,删 B 站 AI 翻译的其它语种。"""
        job_dir = tmp_path / "job"
        job_dir.mkdir()
        input_dir = job_dir / "input"
        input_dir.mkdir()
        (input_dir / "video.中文.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\n大家好这是中文字幕内容\n")
        (input_dir / "video.English.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nhello english subtitle\n")

        config = make_step_config(tmp_path, step_name="01_download")
        step = DownloadStep("01_download", job_dir, config)
        step._prune_subtitles_danmaku(input_dir)

        srts = list(input_dir.glob("*.srt"))
        assert len(srts) == 1
        assert srts[0].name == "video.中文.srt"
