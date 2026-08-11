"""额外覆盖 steps/common/step_01_download.py 的下载分派/凭证/重命名/元数据逻辑。

所有 subprocess / yt-dlp / yutto / curl / urllib / trafilatura / ffprobe 均被 mock,
绝不触网、不真实下载。与既有 test_step_01_download.py 互补(不重复其用例)。
"""

import json
import socket
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pytest

from steps.common import step_01_download as download_module
from steps.common.step_01_download import DownloadStep, HttpFetchResult
from tests.steps.conftest import make_step_config


def _make_job_dir(tmp_path):
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    for d in ["input", "intermediate", "output", "assets", "logs"]:
        (job_dir / d).mkdir()
    return job_dir


def _make_step(job_dir, tmp_path, url="https://example.com/x", source=None,
               content_type="video", document_kind=None):
    job_data = {"url": url, "content_type": content_type}
    if document_kind:
        job_data["document_kind"] = document_kind
    if source:
        job_data["source"] = source
    (job_dir / "job.json").write_text(json.dumps(job_data))
    config = make_step_config(tmp_path, step_name="01_download", pool="io")
    return DownloadStep("01_download", job_dir, config)


# input_hashes

class TestInputHashes:
    def test_input_hashes_uses_job_json(self, tmp_path):
        job_dir = _make_job_dir(tmp_path)
        step = _make_step(job_dir, tmp_path, source="upload")
        h = step.input_hashes()
        assert set(h) == {"job"}
        assert h["job"].startswith("sha256:")

    def test_input_hashes_changes_with_job_content(self, tmp_path):
        job_dir = _make_job_dir(tmp_path)
        step = _make_step(job_dir, tmp_path, url="https://a.example/1", source="upload")
        first = step.input_hashes()["job"]
        (job_dir / "job.json").write_text(json.dumps({"url": "https://a.example/2", "source": "upload"}))
        assert step.input_hashes()["job"] != first


class TestNasSourceMode:
    def test_execute_reads_materialized_source_without_copy_or_download(self, tmp_path):
        job_dir = _make_job_dir(tmp_path)
        source = job_dir / "input" / "source.mp4"
        source.write_bytes(b"materialized")
        step = _make_step(
            job_dir, tmp_path, url="", source="nas_source", content_type="video",
        )
        step._verify_download = MagicMock()
        step._extract_metadata = MagicMock(return_value={"duration_sec": 12})
        step._download_generic = MagicMock()
        step._copy_local_file = MagicMock()

        result = step.execute()

        step._verify_download.assert_called_once_with(source)
        step._download_generic.assert_not_called()
        step._copy_local_file.assert_not_called()
        assert result == {"source": "nas_source", "duration_sec": 12}


# _read_sessdata

class TestReadSessdata:
    def test_missing_credentials_returns_none(self, tmp_path):
        job_dir = _make_job_dir(tmp_path)
        step = _make_step(job_dir, tmp_path)
        assert step._read_sessdata() is None

    def test_valid_credentials_returns_value(self, tmp_path):
        job_dir = _make_job_dir(tmp_path)
        (job_dir / "input" / ".credentials.json").write_text(json.dumps({"sessdata": "ABC123"}))
        step = _make_step(job_dir, tmp_path)
        assert step._read_sessdata() == "ABC123"

    def test_credentials_without_field_returns_none(self, tmp_path):
        job_dir = _make_job_dir(tmp_path)
        (job_dir / "input" / ".credentials.json").write_text(json.dumps({"other": "x"}))
        step = _make_step(job_dir, tmp_path)
        assert step._read_sessdata() is None

    def test_corrupt_credentials_returns_none(self, tmp_path):
        job_dir = _make_job_dir(tmp_path)
        (job_dir / "input" / ".credentials.json").write_text("not json {{{")
        step = _make_step(job_dir, tmp_path)
        assert step._read_sessdata() is None


# _resolve_sessdata 优先级(env > 侧载;文件回退已废除)

class TestResolveSessdata:
    def test_env_takes_priority_over_sideload(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BILI_SESSDATA", "ENVVAL")
        job_dir = _make_job_dir(tmp_path)
        (job_dir / "input" / ".credentials.json").write_text(json.dumps({"sessdata": "SIDELOAD"}))
        step = _make_step(job_dir, tmp_path)
        assert step._resolve_sessdata() == "ENVVAL"

    def test_falls_back_to_sideload_without_env(self, tmp_path, monkeypatch):
        monkeypatch.delenv("BILI_SESSDATA", raising=False)
        job_dir = _make_job_dir(tmp_path)
        (job_dir / "input" / ".credentials.json").write_text(json.dumps({"sessdata": "SIDELOAD"}))
        step = _make_step(job_dir, tmp_path)
        assert step._resolve_sessdata() == "SIDELOAD"

    def test_none_when_no_env_no_sideload(self, tmp_path, monkeypatch):
        monkeypatch.delenv("BILI_SESSDATA", raising=False)
        job_dir = _make_job_dir(tmp_path)
        step = _make_step(job_dir, tmp_path)
        assert step._resolve_sessdata() is None


# _verify_download

class TestVerifyDownload:
    def test_missing_file_raises(self, tmp_path):
        from shared.errors import InputInvalidError
        job_dir = _make_job_dir(tmp_path)
        step = _make_step(job_dir, tmp_path)
        with pytest.raises(InputInvalidError):
            step._verify_download(job_dir / "input" / "nope.mp4")

    def test_too_small_raises(self, tmp_path):
        from shared.errors import InputInvalidError
        job_dir = _make_job_dir(tmp_path)
        mp4 = job_dir / "input" / "source.mp4"
        mp4.write_bytes(b"\x00" * 1024)  # < 1MB
        step = _make_step(job_dir, tmp_path)
        with pytest.raises(InputInvalidError):
            step._verify_download(mp4)

    def test_no_duration_raises(self, tmp_path):
        from shared.errors import InputInvalidError
        job_dir = _make_job_dir(tmp_path)
        mp4 = job_dir / "input" / "source.mp4"
        mp4.write_bytes(b"\x00" * (1024 * 1024 * 2))
        step = _make_step(job_dir, tmp_path)
        with patch.object(step, "_get_video_duration", return_value=None):
            with pytest.raises(InputInvalidError):
                step._verify_download(mp4)

    def test_zero_duration_raises(self, tmp_path):
        from shared.errors import InputInvalidError
        job_dir = _make_job_dir(tmp_path)
        mp4 = job_dir / "input" / "source.mp4"
        mp4.write_bytes(b"\x00" * (1024 * 1024 * 2))
        step = _make_step(job_dir, tmp_path)
        with patch.object(step, "_get_video_duration", return_value=0.0):
            with pytest.raises(InputInvalidError):
                step._verify_download(mp4)

    def test_valid_passes(self, tmp_path):
        job_dir = _make_job_dir(tmp_path)
        mp4 = job_dir / "input" / "source.mp4"
        mp4.write_bytes(b"\x00" * (1024 * 1024 * 2))
        step = _make_step(job_dir, tmp_path)
        with patch.object(step, "_get_video_duration", return_value=42.0):
            step._verify_download(mp4)  # should not raise


# _get_video_duration (ffprobe mocked)

class TestGetVideoDuration:
    def test_ok(self, tmp_path):
        job_dir = _make_job_dir(tmp_path)
        step = _make_step(job_dir, tmp_path)
        fake = SimpleNamespace(returncode=0, stdout="123.456\n", stderr="")
        with patch("steps.common.step_01_download.subprocess.run", return_value=fake) as run:
            assert step._get_video_duration(job_dir / "input" / "source.mp4") == 123.5
        assert run.call_args[0][0][0] == "ffprobe"

    def test_nonzero_returncode_returns_none(self, tmp_path):
        job_dir = _make_job_dir(tmp_path)
        step = _make_step(job_dir, tmp_path)
        fake = SimpleNamespace(returncode=1, stdout="", stderr="boom")
        with patch("steps.common.step_01_download.subprocess.run", return_value=fake):
            assert step._get_video_duration(job_dir / "input" / "source.mp4") is None

    def test_empty_stdout_returns_none(self, tmp_path):
        job_dir = _make_job_dir(tmp_path)
        step = _make_step(job_dir, tmp_path)
        fake = SimpleNamespace(returncode=0, stdout="   \n", stderr="")
        with patch("steps.common.step_01_download.subprocess.run", return_value=fake):
            assert step._get_video_duration(job_dir / "input" / "source.mp4") is None

    def test_unparsable_value_returns_none(self, tmp_path):
        job_dir = _make_job_dir(tmp_path)
        step = _make_step(job_dir, tmp_path)
        fake = SimpleNamespace(returncode=0, stdout="N/A\n", stderr="")
        with patch("steps.common.step_01_download.subprocess.run", return_value=fake):
            assert step._get_video_duration(job_dir / "input" / "source.mp4") is None

    def test_timeout_returns_none(self, tmp_path):
        import subprocess
        job_dir = _make_job_dir(tmp_path)
        step = _make_step(job_dir, tmp_path)
        with patch("steps.common.step_01_download.subprocess.run",
                   side_effect=subprocess.TimeoutExpired("ffprobe", 30)):
            assert step._get_video_duration(job_dir / "input" / "source.mp4") is None


# _rename_to_source_mp4

class TestRenameToSourceMp4:
    def test_renames_mkv_to_source_mp4(self, tmp_path):
        job_dir = _make_job_dir(tmp_path)
        input_dir = job_dir / "input"
        (input_dir / "source.mkv").write_bytes(b"x")
        step = _make_step(job_dir, tmp_path)
        step._rename_to_source_mp4(input_dir)
        assert (input_dir / "source.mp4").exists()
        assert not (input_dir / "source.mkv").exists()

    def test_renames_mov_upload_to_source_mp4(self, tmp_path):
        job_dir = _make_job_dir(tmp_path)
        input_dir = job_dir / "input"
        (input_dir / "source.mov").write_bytes(b"x")
        step = _make_step(job_dir, tmp_path)
        step._rename_to_source_mp4(input_dir)
        assert (input_dir / "source.mp4").exists()

    def test_already_mp4_left_alone(self, tmp_path):
        job_dir = _make_job_dir(tmp_path)
        input_dir = job_dir / "input"
        (input_dir / "source.mp4").write_bytes(b"already")
        step = _make_step(job_dir, tmp_path)
        step._rename_to_source_mp4(input_dir)
        assert (input_dir / "source.mp4").read_bytes() == b"already"

    def test_no_video_noop(self, tmp_path):
        job_dir = _make_job_dir(tmp_path)
        input_dir = job_dir / "input"
        step = _make_step(job_dir, tmp_path)
        step._rename_to_source_mp4(input_dir)  # nothing to do
        assert not (input_dir / "source.mp4").exists()


# _rename_downloaded_video (flv branch)

class TestRenameDownloadedVideo:
    def test_flv_renamed(self, tmp_path):
        job_dir = _make_job_dir(tmp_path)
        input_dir = job_dir / "input"
        (input_dir / "clip.flv").write_bytes(b"flv")
        step = _make_step(job_dir, tmp_path)
        step._rename_downloaded_video(input_dir)
        assert (input_dir / "source.mp4").exists()


# _link_audio_for_whisper

class TestLinkAudioForWhisper:
    def test_copies_mp3_to_mp4(self, tmp_path):
        job_dir = _make_job_dir(tmp_path)
        input_dir = job_dir / "input"
        (input_dir / "source.mp3").write_bytes(b"audiobytes")
        step = _make_step(job_dir, tmp_path)
        step._link_audio_for_whisper(input_dir)
        assert (input_dir / "source.mp4").read_bytes() == b"audiobytes"

    def test_existing_mp4_not_overwritten(self, tmp_path):
        job_dir = _make_job_dir(tmp_path)
        input_dir = job_dir / "input"
        (input_dir / "source.mp4").write_bytes(b"video")
        (input_dir / "source.mp3").write_bytes(b"audio")
        step = _make_step(job_dir, tmp_path)
        step._link_audio_for_whisper(input_dir)
        assert (input_dir / "source.mp4").read_bytes() == b"video"

    def test_no_audio_noop(self, tmp_path):
        job_dir = _make_job_dir(tmp_path)
        input_dir = job_dir / "input"
        step = _make_step(job_dir, tmp_path)
        step._link_audio_for_whisper(input_dir)
        assert not (input_dir / "source.mp4").exists()


# _download_youtube

class TestDownloadYoutube:
    def test_anonymous_no_cookies(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
        job_dir = _make_job_dir(tmp_path)
        step = _make_step(job_dir, tmp_path, source="youtube")
        monkeypatch.delenv("FLORI_YT_COOKIES", raising=False)
        with patch.object(step.commands, "run") as run, \
             patch.object(step, "_rename_to_source_mp4") as rn:
            step._download_youtube("https://youtu.be/abc")
            cmd = run.call_args[0][0]
            assert cmd[0] == "yt-dlp"
            assert "--cookies" not in cmd
            assert cmd[-1] == "https://youtu.be/abc"
            assert cmd[-2] == "--"
            rn.assert_called_once()

    def test_with_cookies_env_tempfile_cleaned(self, tmp_path, monkeypatch):
        # 中心分发注入 env FLORI_YT_COOKIES → 写临时文件传 --cookies,用毕即删(凭证不留盘)。
        monkeypatch.setenv("FLORI_YT_COOKIES", "# netscape cookies")
        job_dir = _make_job_dir(tmp_path)
        step = _make_step(job_dir, tmp_path, source="youtube")
        seen = {}
        def _capture(cmd, timeout=0):
            i = cmd.index("--cookies")
            seen["path"] = cmd[i + 1]
            seen["content"] = Path(cmd[i + 1]).read_text(encoding="utf-8")
        with patch.object(step.commands, "run", side_effect=_capture), \
             patch.object(step, "_rename_to_source_mp4"):
            step._download_youtube("https://youtu.be/abc")
        assert "netscape cookies" in seen["content"]
        assert not Path(seen["path"]).exists()   # finally 已删

    def test_with_cookies_tempfile_cleaned_on_failure(self, tmp_path, monkeypatch):
        # 下载失败同样清理临时 cookie 文件。
        monkeypatch.setenv("FLORI_YT_COOKIES", "cookiez")
        job_dir = _make_job_dir(tmp_path)
        step = _make_step(job_dir, tmp_path, source="youtube")
        seen = {}
        def _boom(cmd, timeout=0):
            seen["path"] = cmd[cmd.index("--cookies") + 1]
            raise RuntimeError("network down")
        with patch.object(step.commands, "run", side_effect=_boom), \
             pytest.raises(RuntimeError):
            step._download_youtube("https://youtu.be/abc")
        assert not Path(seen["path"]).exists()


# _download_arxiv

class TestDownloadArxiv:
    def test_builds_pdf_url(self, tmp_path):
        job_dir = _make_job_dir(tmp_path)
        step = _make_step(job_dir, tmp_path, source="arxiv", content_type="document",
                          document_kind="research_paper")
        # 元数据 curl 返回空响应(ParseError → best-effort 兜底);HTML 抓取 patch 掉(不碰网络)。
        with patch.object(step.commands, "run", return_value=SimpleNamespace(stdout="")) as run, \
                patch.object(step, "_fetch_scholarly_html", return_value=(None, None)):
            step._download_arxiv("https://arxiv.org/abs/2301.00001")
            cmd = run.call_args[0][0]
            assert cmd[0] == "curl"
            assert "https://arxiv.org/pdf/2301.00001.pdf" in cmd

    def test_bad_url_raises(self, tmp_path):
        from shared.errors import InputInvalidError
        job_dir = _make_job_dir(tmp_path)
        step = _make_step(job_dir, tmp_path, source="arxiv", content_type="document",
                          document_kind="research_paper")
        with patch.object(step.commands, "run") as run:
            with pytest.raises(InputInvalidError):
                step._download_arxiv("https://arxiv.org/notapaper")
            run.assert_not_called()


# _download_audio

class TestDownloadAudio:
    def test_downloads_to_mp3(self, tmp_path):
        job_dir = _make_job_dir(tmp_path)
        step = _make_step(job_dir, tmp_path, source="podcast", content_type="audio")
        with patch("shared.net.assert_public_url") as ap, \
             patch.object(step, "_verify_audio", return_value=True), \
             patch.object(step.commands, "run") as run:
            step._download_audio("https://cdn.example.com/ep/1.mp3")
            ap.assert_called_once_with("https://cdn.example.com/ep/1.mp3")
            cmd = run.call_args[0][0]
            assert cmd[0] == "curl"
            assert str(job_dir / "input" / "source.mp3") in cmd
            assert cmd[-1] == "https://cdn.example.com/ep/1.mp3"

    def test_ssrf_blocked_no_download(self, tmp_path):
        from shared.errors import InputInvalidError
        job_dir = _make_job_dir(tmp_path)
        step = _make_step(job_dir, tmp_path, source="podcast", content_type="audio")
        with patch("shared.net.assert_public_url",
                   side_effect=InputInvalidError("internal")) as ap, \
             patch.object(step.commands, "run") as run:
            with pytest.raises(InputInvalidError):
                step._download_audio("http://127.0.0.1/secret.mp3")
            run.assert_not_called()


# _download_generic

class TestDownloadGeneric:
    def test_runs_ytdlp_with_separator(self, tmp_path):
        job_dir = _make_job_dir(tmp_path)
        step = _make_step(job_dir, tmp_path)
        with patch("shared.net.assert_public_url"), \
             patch.object(step.commands, "run") as run, \
             patch.object(step, "_rename_to_source_mp4") as rn:
            step._download_generic("https://vid.example.com/x")
            cmd = run.call_args[0][0]
            assert cmd[0] == "yt-dlp"
            assert "--" in cmd
            assert cmd[-1] == "https://vid.example.com/x"
            rn.assert_called_once()

    def test_ssrf_blocked(self, tmp_path):
        from shared.errors import InputInvalidError
        job_dir = _make_job_dir(tmp_path)
        step = _make_step(job_dir, tmp_path)
        with patch("shared.net.assert_public_url",
                   side_effect=InputInvalidError("internal")), \
             patch.object(step.commands, "run") as run:
            with pytest.raises(InputInvalidError):
                step._download_generic("http://10.0.0.1/x")
            run.assert_not_called()


# _download_bilibili (主力 + 兜底)

class TestDownloadBilibili:
    def test_yutto_primary_with_sessdata(self, tmp_path):
        job_dir = _make_job_dir(tmp_path)
        (job_dir / "input" / ".credentials.json").write_text(json.dumps({"sessdata": "TOK"}))
        step = _make_step(job_dir, tmp_path, source="bilibili")
        with patch.object(step.commands, "run") as run, \
             patch.object(step, "_rename_downloaded_video"), \
             patch.object(step, "_prune_subtitles_danmaku"), \
             patch.object(step, "_verify_download"):
            step._download_bilibili("https://www.bilibili.com/video/BV1xx411c7mD")
            cmd = run.call_args[0][0]
            assert cmd[0] == "yutto"
            assert "-c" in cmd
            assert "TOK" in cmd

    def test_yutto_anonymous_no_sessdata(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path / "nope"))
        job_dir = _make_job_dir(tmp_path)
        step = _make_step(job_dir, tmp_path, source="bilibili")
        with patch.object(step.commands, "run") as run, \
             patch.object(step, "_rename_downloaded_video"), \
             patch.object(step, "_prune_subtitles_danmaku"), \
             patch.object(step, "_verify_download"):
            step._download_bilibili("https://www.bilibili.com/video/BV1xx411c7mD")
            cmd = run.call_args[0][0]
            assert "-c" not in cmd

    def test_yutto_env_sessdata_injected(self, tmp_path, monkeypatch):
        """中心分发注入 env BILI_SESSDATA(worker 认领时下发)→ yutto -c 收到该值;
        cookie 文件回退已废除(docs/03 §1.7.1)。"""
        monkeypatch.setenv("BILI_SESSDATA", "dispatched-token")
        job_dir = _make_job_dir(tmp_path)   # 无 .credentials.json → 只能来自 env
        step = _make_step(job_dir, tmp_path, source="bilibili")
        with patch.object(step.commands, "run") as run, \
             patch.object(step, "_rename_downloaded_video"), \
             patch.object(step, "_prune_subtitles_danmaku"), \
             patch.object(step, "_verify_download"):
            step._download_bilibili("https://www.bilibili.com/video/BV1xx411c7mD")
            cmd = run.call_args[0][0]
            assert cmd[cmd.index("-c") + 1] == "dispatched-token"

    def test_yutto_fails_falls_back_to_ytdlp(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path / "nope"))
        job_dir = _make_job_dir(tmp_path)
        step = _make_step(job_dir, tmp_path, source="bilibili")
        with patch.object(step.commands, "run", side_effect=RuntimeError("yutto boom")), \
             patch.object(step, "_download_bili_ytdlp") as fallback, \
             patch.object(step, "_verify_download"):
            step._download_bilibili("https://www.bilibili.com/video/BV1xx411c7mD")
            # 兜底须拿到归一后的 target_url + input_dir + sessdata,此处无凭证故 None。
            # 不只验"被调过",防回退时丢参或错传参。签名为 url, input_dir, sessdata。
            fallback.assert_called_once_with(
                "https://www.bilibili.com/video/BV1xx411c7mD",
                job_dir / "input",
                None,
            )

    def test_non_bvid_url_passes_through(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path / "nope"))
        job_dir = _make_job_dir(tmp_path)
        step = _make_step(job_dir, tmp_path, source="bilibili")
        with patch.object(step.commands, "run") as run, \
             patch.object(step, "_rename_downloaded_video"), \
             patch.object(step, "_prune_subtitles_danmaku"), \
             patch.object(step, "_verify_download"):
            step._download_bilibili("https://b23.tv/shortcode")
            cmd = run.call_args[0][0]
            # 无法抽 bvid 时直接用原 url
            assert "https://b23.tv/shortcode" in cmd


# _download_bili_ytdlp

class TestDownloadBiliYtdlp:
    def test_with_sessdata_adds_cookie_header(self, tmp_path):
        job_dir = _make_job_dir(tmp_path)
        step = _make_step(job_dir, tmp_path)
        with patch.object(step.commands, "run") as run, \
             patch.object(step, "_rename_to_source_mp4") as rn:
            step._download_bili_ytdlp("https://www.bilibili.com/video/BVx", job_dir / "input", "SESS")
            cmd = run.call_args[0][0]
            assert cmd[0] == "yt-dlp"
            assert "--add-header" in cmd
            assert any("SESSDATA=SESS" in c for c in cmd)
            rn.assert_called_once()

    def test_without_sessdata_no_cookie_header(self, tmp_path):
        job_dir = _make_job_dir(tmp_path)
        step = _make_step(job_dir, tmp_path)
        with patch.object(step.commands, "run") as run, \
             patch.object(step, "_rename_to_source_mp4"):
            step._download_bili_ytdlp("https://www.bilibili.com/video/BVx", job_dir / "input", None)
            cmd = run.call_args[0][0]
            assert "--add-header" not in cmd


# _bili_published_at (urllib mocked)

class TestBiliPublishedAt:
    def test_no_bvid_returns_none(self, tmp_path):
        job_dir = _make_job_dir(tmp_path)
        step = _make_step(job_dir, tmp_path)
        assert step._bili_published_at("https://example.com/notbili") is None

    def test_success_returns_iso(self, tmp_path):
        job_dir = _make_job_dir(tmp_path)
        step = _make_step(job_dir, tmp_path)
        payload = json.dumps({"code": 0, "data": {"pubdate": 1_600_000_000}}).encode("utf-8")

        class FakeResp:
            def read(self):
                return payload

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        with patch("urllib.request.urlopen", return_value=FakeResp()):
            iso = step._bili_published_at("https://www.bilibili.com/video/BV1xx411c7mD")
        assert iso is not None
        assert iso.startswith("2020-")
        assert "T" in iso

    def test_api_error_code_returns_none(self, tmp_path):
        job_dir = _make_job_dir(tmp_path)
        step = _make_step(job_dir, tmp_path)
        payload = json.dumps({"code": -404, "data": {}}).encode("utf-8")

        class FakeResp:
            def read(self):
                return payload

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        with patch("urllib.request.urlopen", return_value=FakeResp()):
            assert step._bili_published_at("https://www.bilibili.com/video/BV1xx411c7mD") is None

    def test_network_exception_returns_none(self, tmp_path):
        job_dir = _make_job_dir(tmp_path)
        step = _make_step(job_dir, tmp_path)
        with patch("urllib.request.urlopen", side_effect=OSError("net down")):
            assert step._bili_published_at("https://www.bilibili.com/video/BV1xx411c7mD") is None


# execute() 分派分支

class TestExecuteDispatch:
    def test_http_article_branch(self, tmp_path):
        """execute 走 http_article 分支:_download_article 被调,metadata 落盘。"""
        job_dir = _make_job_dir(tmp_path)
        step = _make_step(job_dir, tmp_path, url="https://blog.example.com/post",
                          content_type="document", document_kind="article")
        with patch.object(step, "_download_article") as dl:
            result = step.execute()
            dl.assert_called_once_with(
                "https://blog.example.com/post", document_kind="article",
            )
        assert result["source"] == "http_article"
        assert (job_dir / "input" / "metadata.json").exists()

    def test_podcast_branch_links_audio(self, tmp_path):
        """audio content_type:_download_audio 后 _link_audio_for_whisper 备 source.mp4。"""
        job_dir = _make_job_dir(tmp_path)
        step = _make_step(job_dir, tmp_path, url="https://cdn.example.com/ep/1.mp3", content_type="audio")

        def fake_audio(url):
            (job_dir / "input" / "source.mp3").write_bytes(b"audio")

        with patch.object(step, "_download_audio", side_effect=fake_audio) as dl, \
             patch.object(step, "_get_video_duration", return_value=12.0):
            result = step.execute()
            dl.assert_called_once()
        assert result["source"] == "podcast"
        assert (job_dir / "input" / "source.mp4").read_bytes() == b"audio"

    def test_generic_branch(self, tmp_path):
        """非已知来源 → _download_generic 兜底。"""
        job_dir = _make_job_dir(tmp_path)
        step = _make_step(job_dir, tmp_path, url="https://vid.example.com/clip", source="other")
        with patch.object(step, "_download_generic") as dl:
            result = step.execute()
            dl.assert_called_once_with("https://vid.example.com/clip")
        assert result["source"] == "other"

    def test_bilibili_branch_merges_published_at(self, tmp_path):
        """bilibili 来源:成功取到 pubdate → metadata 带 published_at。"""
        job_dir = _make_job_dir(tmp_path)
        url = "https://www.bilibili.com/video/BV1xx411c7mD"
        step = _make_step(job_dir, tmp_path, url=url, source="bilibili")
        with patch.object(step, "_download_bilibili") as dl, \
             patch.object(step, "_bili_published_at", return_value="2020-09-13T12:26:40+00:00"):
            result = step.execute()
            dl.assert_called_once_with(url)
        assert result["source"] == "bilibili"
        meta = json.loads((job_dir / "input" / "metadata.json").read_text())
        assert meta["published_at"] == "2020-09-13T12:26:40+00:00"

    def test_bilibili_branch_no_published_at(self, tmp_path):
        """bilibili 来源:取不到 pubdate → metadata 不带 published_at(不报错)。"""
        job_dir = _make_job_dir(tmp_path)
        url = "https://www.bilibili.com/video/BV1xx411c7mD"
        step = _make_step(job_dir, tmp_path, url=url, source="bilibili")
        with patch.object(step, "_download_bilibili"), \
             patch.object(step, "_bili_published_at", return_value=None):
            step.execute()
        meta = json.loads((job_dir / "input" / "metadata.json").read_text())
        assert "published_at" not in meta


# _download_article (trafilatura mocked via sys.modules)

class TestDownloadArticle:
    def _fake_fetch(self, monkeypatch, html="<html>body</html>", meta=None,
                    fetch_side_effect=None):
        """抓取走 step._fetch_response;trafilatura 只保留元数据解析."""
        from steps.common.step_01_download import DownloadStep

        def result(value):
            if isinstance(value, HttpFetchResult):
                return value
            final_url = "https://final.example/p"
            if isinstance(value, tuple):
                value, final_url = value
            if value is None:
                return HttpFetchResult(
                    body=b"", final_url=final_url or "https://blog.example.com/p",
                    status_code=None, content_type="", error="URLError:mock",
                )
            body = value.encode() if isinstance(value, str) else value
            return HttpFetchResult(
                body=body, final_url=final_url, status_code=200,
                content_type="text/html; charset=utf-8", error=None,
            )

        fetch = MagicMock()
        if fetch_side_effect is not None:
            fetch.side_effect = [result(value) for value in fetch_side_effect]
        else:
            fetch.return_value = result(html)
        monkeypatch.setattr(DownloadStep, "_fetch_response", staticmethod(fetch))
        mod = MagicMock()
        mod.extract_metadata.return_value = meta
        monkeypatch.setitem(sys.modules, "trafilatura", mod)
        return fetch, mod

    def test_writes_html_and_meta(self, tmp_path, monkeypatch):
        job_dir = _make_job_dir(tmp_path)
        step = _make_step(job_dir, tmp_path, url="https://blog.example.com/p",
                          content_type="document", document_kind="article")
        meta = SimpleNamespace(title="T", author="A", sitename="S", date="2024-01-01")
        self._fake_fetch(monkeypatch, meta=meta)
        with patch("shared.net.assert_public_url") as ap:
            step._download_article("https://blog.example.com/p")
            assert ap.call_args_list == [
                call("https://blog.example.com/p"),
                call("https://final.example/p"),
            ]
        assert (job_dir / "input" / "source.html").read_text() == "<html>body</html>"
        assert step._document_source_meta == {
            "source_url": "https://blog.example.com/p",
            "final_url": "https://final.example/p",
            "title": "T", "author": "A", "sitename": "S",
            "published_at": "2024-01-01",
        }
        assert "date" not in step._document_source_meta
        assert not (job_dir / "input" / "article_meta.json").exists()

    def test_fetch_returns_none_raises_after_backoff(self, tmp_path, monkeypatch):
        # 5 拍退避全空才判失败;每拍 use_config 设超时递增(30→480)。
        from shared.errors import InputInvalidError
        job_dir = _make_job_dir(tmp_path)
        step = _make_step(job_dir, tmp_path, url="https://blog.example.com/p",
                          content_type="document", document_kind="article")
        fetch, _ = self._fake_fetch(monkeypatch, html=None)
        with patch("shared.net.assert_public_url"):
            with pytest.raises(InputInvalidError):
                step._download_article("https://blog.example.com/p")
        assert fetch.call_count == 5

    def test_fetch_transient_fail_recovers_on_retry(self, tmp_path, monkeypatch):
        # 首拍超时返 None、次拍成功 → 不判失败(退避的意义)。
        job_dir = _make_job_dir(tmp_path)
        step = _make_step(job_dir, tmp_path, url="https://blog.example.com/p",
                          content_type="document", document_kind="article")
        fetch, _ = self._fake_fetch(monkeypatch, fetch_side_effect=[None, "<html>slow</html>"])
        with patch("shared.net.assert_public_url"):
            step._download_article("https://blog.example.com/p")
        assert fetch.call_count == 2
        assert (job_dir / "input" / "source.html").read_text() == "<html>slow</html>"

    def test_meta_extraction_exception_swallowed(self, tmp_path, monkeypatch):
        """extract_metadata 抛错时仍保留统一来源 URL 元数据，不冒泡。"""
        job_dir = _make_job_dir(tmp_path)
        step = _make_step(job_dir, tmp_path, url="https://blog.example.com/p",
                          content_type="document", document_kind="article")
        _, mod = self._fake_fetch(monkeypatch, html="<html>x</html>")
        mod.extract_metadata.side_effect = RuntimeError("parse boom")
        with patch("shared.net.assert_public_url"):
            step._download_article("https://blog.example.com/p")
        assert step._document_source_meta == {
            "source_url": "https://blog.example.com/p",
            "final_url": "https://final.example/p",
        }
        assert not (job_dir / "input" / "article_meta.json").exists()

    def test_ssrf_blocked_no_fetch(self, tmp_path, monkeypatch):
        from shared.errors import InputInvalidError
        job_dir = _make_job_dir(tmp_path)
        step = _make_step(job_dir, tmp_path, url="http://127.0.0.1/p",
                          content_type="document", document_kind="article")
        fetch, _ = self._fake_fetch(monkeypatch)
        with patch("shared.net.assert_public_url",
                   side_effect=InputInvalidError("internal")):
            with pytest.raises(InputInvalidError):
                step._download_article("http://127.0.0.1/p")
        fetch.assert_not_called()


# _extract_metadata (其它内容类型)

class TestExtractMetadataTypes:
    def test_pdf_size(self, tmp_path):
        job_dir = _make_job_dir(tmp_path)
        (job_dir / "input" / "source.pdf").write_bytes(b"%PDF" + b"\x00" * 2048)
        step = _make_step(job_dir, tmp_path)
        meta = step._extract_metadata("arxiv", "paper")
        assert meta["file_size_mb"] >= 0
        assert meta["source"] == "arxiv"
        assert meta["content_type"] == "paper"

    def test_html_size(self, tmp_path):
        job_dir = _make_job_dir(tmp_path)
        (job_dir / "input" / "source.html").write_text("<html>" + "x" * 4096 + "</html>")
        step = _make_step(job_dir, tmp_path)
        meta = step._extract_metadata("http_article", "article")
        assert "file_size_mb" in meta
        assert meta["has_subtitle"] is False
        assert meta["has_danmaku"] is False

    def test_audio_duration_and_size(self, tmp_path):
        job_dir = _make_job_dir(tmp_path)
        (job_dir / "input" / "source.mp3").write_bytes(b"\x00" * (1024 * 100))
        step = _make_step(job_dir, tmp_path)
        with patch.object(step, "_get_video_duration", return_value=88.0):
            meta = step._extract_metadata("podcast", "audio")
        assert meta["duration_sec"] == 88.0
        assert meta["file_size_mb"] > 0

    def test_video_duration_and_danmaku_flag(self, tmp_path):
        job_dir = _make_job_dir(tmp_path)
        (job_dir / "input" / "source.mp4").write_bytes(b"\x00" * (1024 * 1024))
        (job_dir / "input" / "danmaku.ass").write_text("[Script Info]")
        step = _make_step(job_dir, tmp_path)
        with patch.object(step, "_get_video_duration", return_value=300.0):
            meta = step._extract_metadata("bilibili", "video")
        assert meta["duration_sec"] == 300.0
        assert meta["has_danmaku"] is True


# _fetch_arxiv_html(HTML 源:官方 → ar5iv 回退;图片本地化)

class TestFetchArxivHtml:
    def test_static_reference_parser_ignores_script_pseudotags_and_reads_unquoted_attrs(self):
        styles, images = DownloadStep._html_static_references(
            '<script>"<img src=evil.png>"</script>'
            '<link rel=stylesheet href=paper.css><img src=figure.png>'
        )

        assert styles == [("link", "paper.css")]
        assert images == ["figure.png"]
        assert 'src="input/html_assets/figure.png"' in DownloadStep._rewrite_html_images(
            '<img src=figure.png>',
            {"figure.png": "input/html_assets/figure.png"},
        )

    @pytest.mark.parametrize(
        "url",
        [
            "http://cdn.example/paper.css",
            "https://user:secret@cdn.example/paper.css",
            "https://cdn.example/paper.woff2#font",
        ],
    )
    def test_snapshot_resource_rejects_unsafe_url_before_network(self, tmp_path, url):
        job_dir = _make_job_dir(tmp_path)
        step = _make_step(job_dir, tmp_path)
        with patch.object(download_module.socket, "getaddrinfo") as resolve:
            with pytest.raises(ValueError, match="uncredentialed HTTPS"):
                step._snapshot_fetch(url, max_bytes=1024)
        resolve.assert_not_called()

    @pytest.mark.parametrize("address", ["100.64.0.1", "192.0.0.8"])
    def test_snapshot_resource_rejects_resolved_non_global_address(
        self, address,
    ):
        info = (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, 443))
        with patch.object(download_module.socket, "getaddrinfo", return_value=[info]):
            with pytest.raises(ValueError, match="non-public address"):
                download_module._public_https_target("https://cdn.example/paper.css")

    def test_snapshot_resource_accepts_resolved_global_address(self):
        info = (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", 443))
        with patch.object(download_module.socket, "getaddrinfo", return_value=[info]):
            assert download_module._public_https_target(
                "https://cdn.example/paper.css",
            ) == ("cdn.example", 443, ("8.8.8.8",))

    def test_snapshot_resource_rejects_https_to_http_redirect(self, tmp_path):
        job_dir = _make_job_dir(tmp_path)
        step = _make_step(job_dir, tmp_path)

        class RedirectResponse:
            status = 302

            @staticmethod
            def getheader(name, default=None):
                return {
                    "Content-Type": "text/plain",
                    "Location": "http://internal.example/resource",
                }.get(name, default)

            @staticmethod
            def close():
                return None

        class FakeConnection:
            def __init__(self, host, port, pinned_ip, *, timeout):
                assert (host, port, pinned_ip, timeout) == (
                    "cdn.example", 443, "203.0.113.8", 60,
                )

            def request(self, *args, **kwargs):
                return None

            @staticmethod
            def getresponse():
                return RedirectResponse()

            @staticmethod
            def close():
                return None

        def resolve(url):
            if url.startswith("http://"):
                raise ValueError("snapshot resource URL must be uncredentialed HTTPS")
            return "cdn.example", 443, ("203.0.113.8",)

        with patch.object(download_module, "_public_https_target", side_effect=resolve), \
                patch.object(download_module, "_PinnedHTTPSConnection", FakeConnection):
            with pytest.raises(ValueError, match="uncredentialed HTTPS"):
                step._snapshot_fetch(
                    "https://cdn.example/paper.css", max_bytes=1024,
                )

    def test_snapshot_https_connection_receives_validated_numeric_ip(self):
        class OkResponse:
            status = 200

            @staticmethod
            def getheader(name, default=None):
                return "text/css" if name == "Content-Type" else default

            @staticmethod
            def read(size):
                assert size == 1025
                return b".paper{color:#123}"

            @staticmethod
            def close():
                return None

        observed = {}

        class FakeConnection:
            def __init__(self, host, port, pinned_ip, *, timeout):
                observed["target"] = (host, port, pinned_ip, timeout)

            def request(self, method, target, *, headers):
                observed["request"] = (method, target, headers["Host"] if "Host" in headers else None)

            @staticmethod
            def getresponse():
                return OkResponse()

            @staticmethod
            def close():
                return None

        with patch.object(
            download_module, "_public_https_target",
            return_value=("cdn.example", 443, ("198.51.100.8",)),
        ), patch.object(download_module, "_PinnedHTTPSConnection", FakeConnection):
            response = DownloadStep._fetch_pinned_https(
                "https://cdn.example/assets/paper.css?rev=1",
                timeout=60,
                max_bytes=1024,
            )

        assert observed["target"] == ("cdn.example", 443, "198.51.100.8", 60)
        assert observed["request"][:2] == (
            "GET", "/assets/paper.css?rev=1",
        )
        assert response.body == b".paper{color:#123}"

    def test_scholarly_root_uses_provider_allowlist_and_validates_final_url(self, tmp_path):
        job_dir = _make_job_dir(tmp_path)
        step = _make_step(
            job_dir, tmp_path, source="arxiv", content_type="document",
            document_kind="research_paper",
        )
        body = b"<html><body><article class='ltx_document'>paper</article></body></html>"
        with patch.object(
            step, "_fetch_pinned_https",
            return_value=HttpFetchResult(
                body, "https://arxiv.org/html/2305.14314v1", 200,
                "text/html; charset=utf-8", None,
            ),
        ) as fetch:
            html, final_url = step._fetch_scholarly_html(
                "https://arxiv.org/html/2305.14314", provider="arxiv",
            )

        assert "ltx_document" in html
        assert final_url == "https://arxiv.org/html/2305.14314v1"
        fetch.assert_called_once_with(
            "https://arxiv.org/html/2305.14314",
            timeout=60,
            max_bytes=download_module.SCHOLARLY_HTML_SOURCE_MAX_BYTES,
            allowed_hosts=frozenset({"arxiv.org", "www.arxiv.org"}),
        )

        with patch.object(
            step, "_fetch_pinned_https",
            return_value=HttpFetchResult(
                body, "https://evil.example/html/2305.14314", 200,
                "text/html", None,
            ),
        ), pytest.raises(ValueError, match="provider host mismatch"):
            step._fetch_scholarly_html(
                "https://arxiv.org/html/2305.14314", provider="arxiv",
            )

    def test_pinned_root_redirect_does_not_request_unapproved_host(self):
        class RedirectResponse:
            status = 302

            @staticmethod
            def getheader(name, default=None):
                return {
                    "Content-Type": "text/html",
                    "Location": "https://evil.example/html/paper",
                }.get(name, default)

            @staticmethod
            def close():
                return None

        class FakeConnection:
            def __init__(self, host, port, pinned_ip, *, timeout):
                assert host == "arxiv.org"

            def request(self, *args, **kwargs):
                return None

            @staticmethod
            def getresponse():
                return RedirectResponse()

            @staticmethod
            def close():
                return None

        resolve = MagicMock(return_value=("arxiv.org", 443, ("198.51.100.8",)))
        with patch.object(download_module, "_public_https_target", resolve), \
                patch.object(download_module, "_PinnedHTTPSConnection", FakeConnection):
            response = DownloadStep._fetch_pinned_https(
                "https://arxiv.org/html/2305.14314",
                timeout=60,
                max_bytes=1024,
                allowed_hosts=frozenset({"arxiv.org"}),
            )

        assert response.error == "provider_host_not_allowed"
        assert resolve.call_count == 1

    def test_static_reference_parser_bounds_chunks_and_reference_events(self, monkeypatch):
        monkeypatch.setattr(download_module, "SCHOLARLY_HTML_CSS_MAX_BYTES", 8)
        with pytest.raises(ValueError, match="stylesheet exceeds"):
            DownloadStep._html_static_references(
                "<style>123456789</style>",
            )

        monkeypatch.setattr(download_module, "SCHOLARLY_HTML_MAX_REFERENCE_EVENTS", 2)
        with pytest.raises(ValueError, match="reference count"):
            DownloadStep._html_static_references(
                '<img src="same.png"><img src="same.png"><img src="same.png">',
            )

        attributes = " ".join(f'data-{index}="x"' for index in range(257))
        with pytest.raises(ValueError, match="attribute count"):
            DownloadStep._html_static_references(
                f'<img src="figure.png" {attributes}>',
            )

    def test_fetch_and_localize(self, tmp_path):
        job_dir = _make_job_dir(tmp_path)
        step = _make_step(job_dir, tmp_path, source="arxiv", content_type="document",
                          document_kind="research_paper")
        html = (
            '<html><body><div class="ltx_page_main">'
            '<img src="x1v2/x1.png"></div></body></html>'
        )
        image = b"\x89PNG\r\n\x1a\n" + b"x" * 16
        with patch.object(step, "_fetch_scholarly_html", return_value=(html, "https://arxiv.org/html/x1")), \
                patch.object(step, "_snapshot_fetch", return_value=HttpFetchResult(
                    image, "https://arxiv.org/html/x1v2/x1.png", 200, "image/png", None,
                )):
            step._fetch_arxiv_html("1810.04805")
        saved = (job_dir / "input" / "source.html").read_text(encoding="utf-8")
        assert 'src="input/html_assets/image-' in saved
        snapshot = json.loads((job_dir / "input/html_snapshot.json").read_text())
        assert snapshot["provider"] == "arxiv"
        assert snapshot["resources"][0]["request_url"] == "https://arxiv.org/html/x1v2/x1.png"
        assert snapshot["resources"][0]["sha256"].startswith("sha256:")

    def test_ar5iv_fallback_then_unavailable(self, tmp_path):
        job_dir = _make_job_dir(tmp_path)
        step = _make_step(job_dir, tmp_path, source="arxiv", content_type="document",
                          document_kind="research_paper")
        # 官方与 ar5iv 都无 LaTeXML 产物(None / 落地页无 ltx_)→ 不写 source.html。
        with patch.object(step, "_fetch_scholarly_html", side_effect=[(None, None), ("<html>no latexml</html>", "u")]):
            step._fetch_arxiv_html("9901.00001")
        assert not (job_dir / "input" / "source.html").exists()

    def test_incomplete_official_html_falls_back_to_complete_ar5iv(self, tmp_path):
        job_dir = _make_job_dir(tmp_path)
        step = _make_step(job_dir, tmp_path, source="arxiv", content_type="document",
                          document_kind="research_paper")
        truncated = '<html><body><div class="ltx_page_main"><math><mi>x</mi>'
        complete = '<html><body><div class="ltx_page_main">ok</div></body></html>'
        with patch.object(
            step, "_fetch_scholarly_html",
            side_effect=[
                (truncated, "https://arxiv.org/html/2205.14135v2"),
                (complete, "https://ar5iv.labs.arxiv.org/html/2205.14135"),
            ],
        ):
            step._fetch_arxiv_html("2205.14135")

        assert (job_dir / "input/source.html").read_text() == complete
        assert step._arxiv_html_base == "https://ar5iv.labs.arxiv.org/html/2205.14135"

    def test_all_incomplete_arxiv_html_sources_leave_pdf_as_primary(self, tmp_path):
        job_dir = _make_job_dir(tmp_path)
        step = _make_step(job_dir, tmp_path, source="arxiv", content_type="document",
                          document_kind="research_paper")
        truncated = '<html><body><div class="ltx_page_main"><math><mi>x</mi>'
        with patch.object(
            step, "_fetch_scholarly_html",
            side_effect=[(truncated, "official"), (truncated, "ar5iv")],
        ):
            step._fetch_arxiv_html("2205.14135")

        assert not (job_dir / "input/source.html").exists()

    def test_image_download_failure_rejects_incomplete_html_snapshot(self, tmp_path):
        job_dir = _make_job_dir(tmp_path)
        step = _make_step(job_dir, tmp_path, source="arxiv", content_type="document",
                          document_kind="research_paper")
        html = (
            '<html><body><div class="ltx_page_main">'
            '<img src="x1v2/x1.png"></div></body></html>'
        )
        with patch.object(
            step, "_fetch_scholarly_html",
            side_effect=[
                (html, "https://arxiv.org/html/x1"),
                (html, "https://ar5iv.labs.arxiv.org/html/x1"),
            ],
        ), \
                patch.object(step, "_snapshot_fetch", side_effect=ValueError("fetch fail")):
            step._fetch_arxiv_html("1810.04805")
        assert not (job_dir / "input/source.html").exists()
        assert not (job_dir / "input/html_snapshot.json").exists()

    def test_failed_refetch_cannot_reissue_old_snapshot_outputs(self, tmp_path):
        job_dir = _make_job_dir(tmp_path)
        step = _make_step(
            job_dir, tmp_path, source="arxiv", content_type="document",
            document_kind="research_paper",
        )
        (job_dir / "input/source.html").write_text("<html>old</html>")
        (job_dir / "input/html_snapshot.json").write_text('{"old":true}')
        (job_dir / "input/html_assets").mkdir()
        (job_dir / "input/html_assets/old.css").write_text(".old{}")

        with patch.object(step, "_fetch_scholarly_html", return_value=(None, None)):
            step._fetch_arxiv_html("1810.04805")

        assert not (job_dir / "input/source.html").exists()
        assert not (job_dir / "input/html_snapshot.json").exists()
        assert not (job_dir / "input/html_assets").exists()

    def test_unexpected_root_fetch_error_preserves_old_snapshot_outputs(self, tmp_path):
        job_dir = _make_job_dir(tmp_path)
        step = _make_step(
            job_dir, tmp_path, source="arxiv", content_type="document",
            document_kind="research_paper",
        )
        (job_dir / "input/source.html").write_bytes(b"old-html")
        (job_dir / "input/html_snapshot.json").write_bytes(b'{"old":true}')
        (job_dir / "input/html_assets").mkdir()
        (job_dir / "input/html_assets/old.css").write_bytes(b".old{}")

        with patch.object(
            step, "_fetch_scholarly_html", side_effect=RuntimeError("unexpected"),
        ), pytest.raises(RuntimeError, match="unexpected"):
            step._fetch_arxiv_html("1810.04805")

        assert (job_dir / "input/source.html").read_bytes() == b"old-html"
        assert (job_dir / "input/html_snapshot.json").read_bytes() == b'{"old":true}'
        assert (job_dir / "input/html_assets/old.css").read_bytes() == b".old{}"

    def test_snapshot_cleanup_unlinks_asset_symlink_without_touching_target(self, tmp_path):
        job_dir = _make_job_dir(tmp_path)
        step = _make_step(job_dir, tmp_path)
        outside = tmp_path / "outside"
        outside.mkdir()
        protected = outside / "protected.css"
        protected.write_text(".protected{}")
        (job_dir / "input/html_assets").symlink_to(outside, target_is_directory=True)

        step._clear_scholarly_html_snapshot()

        assert protected.read_text() == ".protected{}"
        assert not (job_dir / "input/html_assets").exists()

    def test_oversized_snapshot_json_rejects_provider_before_publish(
        self, tmp_path, monkeypatch,
    ):
        job_dir = _make_job_dir(tmp_path)
        step = _make_step(
            job_dir, tmp_path, source="arxiv", content_type="document",
            document_kind="research_paper",
        )
        html = '<html><body><article class="ltx_document">paper</article></body></html>'
        monkeypatch.setattr(download_module, "SCHOLARLY_HTML_SNAPSHOT_MAX_BYTES", 1)

        with patch.object(
            step, "_fetch_scholarly_html",
            side_effect=[
                (html, "https://arxiv.org/html/2305.14314"),
                (html, "https://ar5iv.labs.arxiv.org/html/2305.14314"),
            ],
        ):
            step._fetch_arxiv_html("2305.14314")

        assert not (job_dir / "input/source.html").exists()
        assert not (job_dir / "input/html_snapshot.json").exists()

    def test_snapshots_imported_css_fonts_and_images_in_dependency_order(self, tmp_path):
        job_dir = _make_job_dir(tmp_path)
        step = _make_step(
            job_dir, tmp_path, source="arxiv", content_type="document",
            document_kind="research_paper",
        )
        html = """<html><head><link rel="stylesheet" href="/assets/main.css"></head>
        <body><article class="ltx_document"><img src="figures/x.png"></article></body></html>"""
        main_css = b"@import url(fonts.css);.ltx_document{font-family:Paper}"
        font_css = b"@font-face{font-family:Paper;src:url(paper.woff2)}"
        font = b"wOF2" + b"f" * 32
        image = b"\x89PNG\r\n\x1a\n" + b"i" * 32
        responses = [
            HttpFetchResult(main_css, "https://arxiv.org/assets/main.css", 200, "text/css", None),
            HttpFetchResult(font_css, "https://arxiv.org/assets/fonts.css", 200, "text/css", None),
            HttpFetchResult(font, "https://arxiv.org/assets/paper.woff2", 200, "font/woff2", None),
            HttpFetchResult(image, "https://arxiv.org/figures/x.png", 200, "image/png", None),
        ]

        with patch.object(step, "_fetch_scholarly_html", return_value=(html, "https://arxiv.org/html/2305.14314")), \
                patch.object(step, "_snapshot_fetch", side_effect=responses):
            step._fetch_arxiv_html("2305.14314")

        snapshot = json.loads((job_dir / "input/html_snapshot.json").read_text())
        by_kind = {}
        for record in snapshot["resources"]:
            by_kind.setdefault(record["kind"], []).append(record)
            assert (job_dir / record["path"]).read_bytes()
        assert [
            next(record for record in snapshot["resources"] if record["path"] == path)["request_url"]
            for path in snapshot["stylesheets"]
        ] == [
            "https://arxiv.org/assets/fonts.css",
            "https://arxiv.org/assets/main.css",
        ]
        assert len(by_kind["font"]) == 1
        assert len(by_kind["image"]) == 1
        saved = (job_dir / "input/source.html").read_text()
        assert "https://arxiv.org/figures/x.png" not in saved
        assert "input/html_assets/image-" in saved

    def test_snapshot_preserves_link_and_inline_stylesheet_order(self, tmp_path):
        job_dir = _make_job_dir(tmp_path)
        step = _make_step(
            job_dir, tmp_path, source="arxiv", content_type="document",
            document_kind="research_paper",
        )
        html = (
            '<html><head><link rel="stylesheet" href="a.css">'
            '<style>.inline{color:red}</style>'
            '<link rel="stylesheet" href="b.css"></head>'
            '<body><article class="ltx_document">paper</article></body></html>'
        )
        responses = [
            HttpFetchResult(
                b".a{color:blue}", "https://arxiv.org/html/a.css",
                200, "text/css", None,
            ),
            HttpFetchResult(
                b".b{color:green}", "https://arxiv.org/html/b.css",
                200, "text/css", None,
            ),
        ]

        with patch.object(
            step, "_fetch_scholarly_html",
            return_value=(html, "https://arxiv.org/html/2305.14314"),
        ), patch.object(step, "_snapshot_fetch", side_effect=responses):
            step._fetch_arxiv_html("2305.14314")

        snapshot = json.loads((job_dir / "input/html_snapshot.json").read_text())
        by_path = {record["path"]: record for record in snapshot["resources"]}
        assert [by_path[path]["request_url"] for path in snapshot["stylesheets"]] == [
            "https://arxiv.org/html/a.css",
            "https://arxiv.org/html/2305.14314#flori-inline-style-1",
            "https://arxiv.org/html/b.css",
        ]
