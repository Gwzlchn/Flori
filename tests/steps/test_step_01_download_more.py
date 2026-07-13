"""额外覆盖 steps/common/step_01_download.py 的下载分派/凭证/重命名/元数据逻辑。

所有 subprocess / yt-dlp / yutto / curl / urllib / trafilatura / ffprobe 均被 mock,
绝不触网、不真实下载。与既有 test_step_01_download.py 互补(不重复其用例)。
"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from steps.common.step_01_download import DownloadStep
from tests.steps.conftest import make_step_config


def _make_job_dir(tmp_path):
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    for d in ["input", "intermediate", "output", "assets", "logs"]:
        (job_dir / d).mkdir()
    return job_dir


def _make_step(job_dir, tmp_path, url="https://example.com/x", source=None, content_type="video"):
    job_data = {"url": url, "content_type": content_type}
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
        with patch.object(step, "run_subprocess") as run, \
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
        with patch.object(step, "run_subprocess", side_effect=_capture), \
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
        with patch.object(step, "run_subprocess", side_effect=_boom), \
             pytest.raises(RuntimeError):
            step._download_youtube("https://youtu.be/abc")
        assert not Path(seen["path"]).exists()


# _download_arxiv

class TestDownloadArxiv:
    def test_builds_pdf_url(self, tmp_path):
        job_dir = _make_job_dir(tmp_path)
        step = _make_step(job_dir, tmp_path, source="arxiv", content_type="paper")
        # 元数据 curl 返回空响应(ParseError → best-effort 兜底);HTML 抓取 patch 掉(不碰网络)。
        with patch.object(step, "run_subprocess", return_value=SimpleNamespace(stdout="")) as run, \
                patch.object(step, "_fetch_html", return_value=(None, None)):
            step._download_arxiv("https://arxiv.org/abs/2301.00001")
            cmd = run.call_args[0][0]
            assert cmd[0] == "curl"
            assert "https://arxiv.org/pdf/2301.00001.pdf" in cmd

    def test_bad_url_raises(self, tmp_path):
        from shared.errors import InputInvalidError
        job_dir = _make_job_dir(tmp_path)
        step = _make_step(job_dir, tmp_path, source="arxiv", content_type="paper")
        with patch.object(step, "run_subprocess") as run:
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
             patch.object(step, "run_subprocess") as run:
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
             patch.object(step, "run_subprocess") as run:
            with pytest.raises(InputInvalidError):
                step._download_audio("http://127.0.0.1/secret.mp3")
            run.assert_not_called()


# _download_generic

class TestDownloadGeneric:
    def test_runs_ytdlp_with_separator(self, tmp_path):
        job_dir = _make_job_dir(tmp_path)
        step = _make_step(job_dir, tmp_path)
        with patch("shared.net.assert_public_url"), \
             patch.object(step, "run_subprocess") as run, \
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
             patch.object(step, "run_subprocess") as run:
            with pytest.raises(InputInvalidError):
                step._download_generic("http://10.0.0.1/x")
            run.assert_not_called()


# _download_bilibili (主力 + 兜底)

class TestDownloadBilibili:
    def test_yutto_primary_with_sessdata(self, tmp_path):
        job_dir = _make_job_dir(tmp_path)
        (job_dir / "input" / ".credentials.json").write_text(json.dumps({"sessdata": "TOK"}))
        step = _make_step(job_dir, tmp_path, source="bilibili")
        with patch.object(step, "run_subprocess") as run, \
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
        with patch.object(step, "run_subprocess") as run, \
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
        with patch.object(step, "run_subprocess") as run, \
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
        with patch.object(step, "run_subprocess", side_effect=RuntimeError("yutto boom")), \
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
        with patch.object(step, "run_subprocess") as run, \
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
        with patch.object(step, "run_subprocess") as run, \
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
        with patch.object(step, "run_subprocess") as run, \
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
        step = _make_step(job_dir, tmp_path, url="https://blog.example.com/post", content_type="article")
        with patch.object(step, "_download_article") as dl:
            result = step.execute()
            dl.assert_called_once_with("https://blog.example.com/post")
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
        """抓取走 step._fetch_html(urllib,尊重代理 env)→ 直接 patch 它;
        trafilatura 只剩解析(extract_metadata),假模块随之瘦身。"""
        from steps.common.step_01_download import DownloadStep
        fetch = MagicMock()
        if fetch_side_effect is not None:
            # 便捷:元素可为 html|None(自动包成 (html, url) tuple)或现成 tuple
            fetch.side_effect = [e if isinstance(e, tuple) else (e, "https://final.example/p" if e else None)
                                 for e in fetch_side_effect]
        else:
            fetch.return_value = (html, "https://final.example/p")
        monkeypatch.setattr(DownloadStep, "_fetch_html", staticmethod(fetch))
        mod = MagicMock()
        mod.extract_metadata.return_value = meta
        monkeypatch.setitem(sys.modules, "trafilatura", mod)
        return fetch, mod

    def test_writes_html_and_meta(self, tmp_path, monkeypatch):
        job_dir = _make_job_dir(tmp_path)
        step = _make_step(job_dir, tmp_path, url="https://blog.example.com/p", content_type="article")
        meta = SimpleNamespace(title="T", author="A", sitename="S", date="2024-01-01")
        self._fake_fetch(monkeypatch, meta=meta)
        with patch("shared.net.assert_public_url") as ap:
            step._download_article("https://blog.example.com/p")
            ap.assert_called_once_with("https://blog.example.com/p")
        assert (job_dir / "input" / "source.html").read_text() == "<html>body</html>"
        am = json.loads((job_dir / "input" / "article_meta.json").read_text())
        assert am["url"] == "https://blog.example.com/p"
        assert am["title"] == "T"
        assert am["author"] == "A"

    def test_fetch_returns_none_raises_after_backoff(self, tmp_path, monkeypatch):
        # 5 拍退避全空才判失败;每拍 use_config 设超时递增(30→480)。
        from shared.errors import InputInvalidError
        job_dir = _make_job_dir(tmp_path)
        step = _make_step(job_dir, tmp_path, url="https://blog.example.com/p", content_type="article")
        fetch, _ = self._fake_fetch(monkeypatch, html=None)
        with patch("shared.net.assert_public_url"):
            with pytest.raises(InputInvalidError):
                step._download_article("https://blog.example.com/p")
        assert fetch.call_count == 5

    def test_fetch_transient_fail_recovers_on_retry(self, tmp_path, monkeypatch):
        # 首拍超时返 None、次拍成功 → 不判失败(退避的意义)。
        job_dir = _make_job_dir(tmp_path)
        step = _make_step(job_dir, tmp_path, url="https://blog.example.com/p", content_type="article")
        fetch, _ = self._fake_fetch(monkeypatch, fetch_side_effect=[None, "<html>slow</html>"])
        with patch("shared.net.assert_public_url"):
            step._download_article("https://blog.example.com/p")
        assert fetch.call_count == 2
        assert (job_dir / "input" / "source.html").read_text() == "<html>slow</html>"

    def test_meta_extraction_exception_swallowed(self, tmp_path, monkeypatch):
        """extract_metadata 抛错时仍写 article_meta.json(只含 url),不冒泡。"""
        job_dir = _make_job_dir(tmp_path)
        step = _make_step(job_dir, tmp_path, url="https://blog.example.com/p", content_type="article")
        _, mod = self._fake_fetch(monkeypatch, html="<html>x</html>")
        mod.extract_metadata.side_effect = RuntimeError("parse boom")
        with patch("shared.net.assert_public_url"):
            step._download_article("https://blog.example.com/p")
        am = json.loads((job_dir / "input" / "article_meta.json").read_text())
        assert am == {"url": "https://blog.example.com/p"}

    def test_ssrf_blocked_no_fetch(self, tmp_path, monkeypatch):
        from shared.errors import InputInvalidError
        job_dir = _make_job_dir(tmp_path)
        step = _make_step(job_dir, tmp_path, url="http://127.0.0.1/p", content_type="article")
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
    def test_fetch_and_localize(self, tmp_path):
        job_dir = _make_job_dir(tmp_path)
        step = _make_step(job_dir, tmp_path, source="arxiv", content_type="paper")
        html = '<div class="ltx_page_main"><img src="x1v2/x1.png"></div>'
        with patch.object(step, "_fetch_html", return_value=(html, "https://arxiv.org/html/x1")), \
                patch.object(step, "run_subprocess", return_value=SimpleNamespace(stdout="")) as run:
            step._fetch_arxiv_html("1810.04805")
        saved = (job_dir / "input" / "source.html").read_text(encoding="utf-8")
        assert 'src="assets/x1v2_x1.png"' in saved        # 引用重写为本地 assets(扁平名含相对目录段)
        curl_cmd = run.call_args[0][0]
        assert "https://arxiv.org/html/x1v2/x1.png" in curl_cmd  # RFC3986:base 末段丢弃(浏览器语义,无手拼斜杠)

    def test_ar5iv_fallback_then_unavailable(self, tmp_path):
        job_dir = _make_job_dir(tmp_path)
        step = _make_step(job_dir, tmp_path, source="arxiv", content_type="paper")
        # 官方与 ar5iv 都无 LaTeXML 产物(None / 落地页无 ltx_)→ 不写 source.html。
        with patch.object(step, "_fetch_html", side_effect=[(None, None), ("<html>no latexml</html>", "u")]):
            step._fetch_arxiv_html("9901.00001")
        assert not (job_dir / "input" / "source.html").exists()

    def test_image_download_failure_keeps_absolute_url(self, tmp_path):
        job_dir = _make_job_dir(tmp_path)
        step = _make_step(job_dir, tmp_path, source="arxiv", content_type="paper")
        html = '<div class="ltx_page_main"><img src="x1v2/x1.png"></div>'
        with patch.object(step, "_fetch_html", return_value=(html, "https://arxiv.org/html/x1")), \
                patch.object(step, "run_subprocess", side_effect=RuntimeError("curl fail")):
            step._fetch_arxiv_html("1810.04805")
        saved = (job_dir / "input" / "source.html").read_text(encoding="utf-8")
        assert 'src="https://arxiv.org/html/x1v2/x1.png"' in saved  # 失败留绝对 URL(RFC3986 语义拼)
