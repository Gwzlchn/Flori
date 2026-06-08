"""Step 00: 下载。视频+论文复用。来源识别 → yutto/yt-dlp/arXiv/upload。"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from shared.source_detect import detect_source, extract_arxiv_id, extract_bilibili_bvid
from shared.step_base import StepBase, file_hash


class DownloadStep(StepBase):
    def validate_inputs(self) -> list[str]:
        if not (self.job_dir / "job.json").exists():
            return ["job.json"]
        return []

    def input_hashes(self) -> dict[str, str]:
        return {
            "job": file_hash(self.job_dir / "job.json"),
        }

    def execute(self) -> dict | None:
        job = self.load_json("job.json")
        url = job.get("url", "")
        source = job.get("source") or detect_source(url)
        content_type = job.get("content_type", "video")

        if source == "upload":
            self.log.info("upload_mode", content_type=content_type)
        elif source == "bilibili":
            self._download_bilibili(url)
        elif source == "youtube":
            self._download_youtube(url)
        elif source == "arxiv":
            self._download_arxiv(url)
        elif source == "http_article":
            self._download_article(url)
        elif source == "podcast":
            self._download_audio(url)
        else:
            self._download_generic(url)

        # 音频任务(上传或单集 URL)统一备一份 source.mp4 供复用的 whisper 步消费。
        if content_type == "audio":
            self._link_audio_for_whisper(self.job_dir / "input")

        metadata = self._extract_metadata(source, content_type)
        self.write_output("input/metadata.json", metadata)
        return {"source": source, "duration_sec": metadata.get("duration_sec")}

    def _download_bilibili(self, url: str) -> None:
        bvid = extract_bilibili_bvid(url)
        target_url = f"https://www.bilibili.com/video/{bvid}" if bvid else url
        input_dir = self.job_dir / "input"
        input_dir.mkdir(parents=True, exist_ok=True)

        cmd = [
            "yutto", target_url,
            "-d", str(input_dir),
            "-tp", "{title}",
        ]

        # 优先用 job.json 注入的 SESSDATA(扫码登录入库,随任务下发到远程 worker);
        # 否则回退到本地 cookie 文件(现状);两者皆无则匿名下载,降级 480P。
        sessdata = self.load_json("job.json").get("sessdata")
        cookies = Path("/data/cookies/bilibili.txt")
        if sessdata:
            cmd.extend(["-c", sessdata])
        elif cookies.exists():
            cmd.extend(["-c", str(cookies)])
        else:
            self.log.warn("no_bilibili_cookies", msg="降级 480P")

        self.run_subprocess(cmd, timeout=self.config["step"]["timeout_sec"])
        self._rename_downloaded_video(input_dir)
        self._rename_downloaded_subtitle(input_dir)
        self._rename_downloaded_danmaku(input_dir)

    def _download_youtube(self, url: str) -> None:
        input_dir = self.job_dir / "input"
        input_dir.mkdir(parents=True, exist_ok=True)

        cmd = [
            "yt-dlp", url,
            "-o", str(input_dir / "source.%(ext)s"),
            "--write-sub", "--sub-lang", "en,zh-Hans",
            "--convert-subs", "srt",
            "-f", "bestvideo[height<=1080]+bestaudio/best[height<=1080]",
            "--merge-output-format", "mp4",
        ]
        self.run_subprocess(cmd, timeout=self.config["step"]["timeout_sec"])
        self._rename_to_source_mp4(input_dir)

    def _download_arxiv(self, url: str) -> None:
        input_dir = self.job_dir / "input"
        input_dir.mkdir(parents=True, exist_ok=True)

        arxiv_id = extract_arxiv_id(url)
        if not arxiv_id:
            from shared.errors import InputInvalidError
            raise InputInvalidError(f"Cannot extract arXiv ID from: {url}")

        pdf_url = f"https://arxiv.org/pdf/{arxiv_id}.pdf"
        cmd = ["curl", "-fSL", "-o", str(input_dir / "source.pdf"), pdf_url]
        self.run_subprocess(cmd, timeout=120)

    def _download_article(self, url: str) -> None:
        """抓 HTML 原文写 input/source.html;同时用 trafilatura 抽正文/标题供后续解析。"""
        import trafilatura

        from shared.net import assert_public_url

        assert_public_url(url)  # 抓取前挡内网/回环目标(SSRF)
        input_dir = self.job_dir / "input"
        input_dir.mkdir(parents=True, exist_ok=True)

        html = trafilatura.fetch_url(url)
        if not html:
            from shared.errors import InputInvalidError
            raise InputInvalidError(f"Cannot fetch article: {url}")
        self.write_output("input/source.html", html)

        # 顺手抽一份标题等元数据,正文解析仍由 16_parse_article 负责(trafilatura)。
        article_meta: dict = {"url": url}
        try:
            meta = trafilatura.extract_metadata(html)
            if meta:
                article_meta["title"] = meta.title or ""
                article_meta["author"] = meta.author or ""
                article_meta["sitename"] = meta.sitename or ""
                article_meta["date"] = meta.date or ""
        except Exception:
            pass
        self.write_output("input/article_meta.json", article_meta)

    def _download_audio(self, url: str) -> None:
        """单集音频 URL → 下载写 input/source.mp3。无 RSS,只取单文件。
        同时落一份 input/source.mp4(复用现有 whisper 步,其入参约定为 source.mp4;
        ffmpeg 按内容嗅探解码,扩展名不影响转写)。"""
        from shared.net import assert_public_url

        assert_public_url(url)  # 下载前挡内网/回环目标(SSRF)
        input_dir = self.job_dir / "input"
        input_dir.mkdir(parents=True, exist_ok=True)

        dest = input_dir / "source.mp3"
        cmd = ["curl", "-fSL", "-o", str(dest), "--", url]
        self.run_subprocess(cmd, timeout=self.config["step"]["timeout_sec"])

    def _link_audio_for_whisper(self, input_dir: Path) -> None:
        """把已下载/已上传的单集音频复制为 source.mp4,满足复用 whisper 步的入参约定。"""
        target = input_dir / "source.mp4"
        if target.exists():
            return
        for ext in (".mp3", ".m4a", ".wav", ".aac"):
            src = input_dir / f"source{ext}"
            if src.exists():
                import shutil
                shutil.copyfile(src, target)
                return

    def _download_generic(self, url: str) -> None:
        input_dir = self.job_dir / "input"
        input_dir.mkdir(parents=True, exist_ok=True)

        cmd = [
            "yt-dlp", url,
            "-o", str(input_dir / "source.%(ext)s"),
            "-f", "bestvideo[height<=1080]+bestaudio/best[height<=1080]",
            "--merge-output-format", "mp4",
        ]
        self.run_subprocess(cmd, timeout=self.config["step"]["timeout_sec"])
        self._rename_to_source_mp4(input_dir)

    def _rename_downloaded_video(self, input_dir: Path) -> None:
        """yutto 下载的视频文件名不固定，重命名为 source.mp4。"""
        search_dirs = [input_dir, self.job_dir]
        for d in search_dirs:
            for f in d.glob("*.mp4"):
                if f.name != "source.mp4":
                    f.rename(input_dir / "source.mp4")
                    return
            for f in d.glob("*.flv"):
                f.rename(input_dir / "source.mp4")
                return

    def _rename_downloaded_subtitle(self, input_dir: Path) -> None:
        search_dirs = [input_dir, self.job_dir]
        for d in search_dirs:
            for f in d.glob("*.srt"):
                if f.name != "subtitle.srt":
                    f.rename(input_dir / "subtitle.srt")
                    return

    def _rename_downloaded_danmaku(self, input_dir: Path) -> None:
        search_dirs = [input_dir, self.job_dir]
        for d in search_dirs:
            for f in d.glob("*.ass"):
                if f.name != "danmaku.ass":
                    f.rename(input_dir / "danmaku.ass")
                    return

    def _rename_to_source_mp4(self, input_dir: Path) -> None:
        """yt-dlp 下载后重命名为 source.mp4。"""
        for f in input_dir.glob("source.*"):
            if f.suffix in (".mp4", ".mkv", ".webm"):
                if f.name != "source.mp4":
                    f.rename(input_dir / "source.mp4")
                return

    def _extract_metadata(self, source: str, content_type: str) -> dict:
        input_dir = self.job_dir / "input"
        metadata: dict = {"source": source, "content_type": content_type}

        video_file = input_dir / "source.mp4"
        if video_file.exists():
            metadata["duration_sec"] = self._get_video_duration(video_file)
            metadata["file_size_mb"] = round(video_file.stat().st_size / 1048576, 1)

        pdf_file = input_dir / "source.pdf"
        if pdf_file.exists():
            metadata["file_size_mb"] = round(pdf_file.stat().st_size / 1048576, 1)

        html_file = input_dir / "source.html"
        if html_file.exists():
            metadata["file_size_mb"] = round(html_file.stat().st_size / 1048576, 1)

        # 音频:对原始音频文件(非复制出的 source.mp4)取时长与大小。
        for ext in (".mp3", ".m4a", ".wav", ".aac"):
            audio_file = input_dir / f"source{ext}"
            if audio_file.exists():
                metadata["duration_sec"] = self._get_video_duration(audio_file)
                metadata["file_size_mb"] = round(audio_file.stat().st_size / 1048576, 1)
                break

        metadata["has_subtitle"] = any(input_dir.glob("*.srt"))
        metadata["has_danmaku"] = any(input_dir.glob("*.ass"))
        return metadata

    def _get_video_duration(self, video_path: Path) -> float | None:
        try:
            result = subprocess.run(
                [
                    "ffprobe", "-v", "quiet",
                    "-show_entries", "format=duration",
                    "-of", "csv=p=0",
                    str(video_path),
                ],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode == 0 and result.stdout.strip():
                return round(float(result.stdout.strip()), 1)
        except (subprocess.TimeoutExpired, ValueError):
            pass
        return None


if __name__ == "__main__":
    DownloadStep.cli_main("00_download")
