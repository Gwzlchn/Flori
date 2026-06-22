"""Step 01: 下载。视频+论文复用。来源识别 → yutto/yt-dlp/arXiv/upload。"""

from __future__ import annotations

import json
import os
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

        # 本地目录订阅(local_dir 适配器)枚举出的 file:// url:不走网络下载,
        # 把宿主本地文件复制进 job 的 input/(被监听目录已挂进容器),再走正常校验。
        # 注:create_job_core 用 detect_source 给本地 url 的 source 记 "other",故这里
        # 直接按 url 前缀判定,优先于 source 分派。
        if url.startswith("file://"):
            self.log.info("local_file_mode", content_type=content_type)
            source = "local"
            self._copy_local_file(url, content_type)
        elif source == "upload":
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
        if source == "bilibili":
            pub = self._bili_published_at(url)
            if pub:
                metadata["published_at"] = pub   # 源视频在 B 站的发布时间(供前端「上传于」)
        elif source == "youtube":
            # 标题/上传日期取自 yt-dlp 写的 source.info.json(--write-info-json)。
            t, pub = self._youtube_title_published()
            if t and not metadata.get("title"):
                metadata["title"] = t
            if pub:
                metadata["published_at"] = pub
        self.write_output("input/metadata.json", metadata)
        return {"source": source, "duration_sec": metadata.get("duration_sec")}

    def _youtube_title_published(self) -> tuple[str | None, str | None]:
        """从 source.info.json 读 YouTube 标题与上传日期(YYYYMMDD→ISO)。失败返回 (None, None)。"""
        import json as _json
        from datetime import datetime, timezone
        info = self.job_dir / "input" / "source.info.json"
        if not info.is_file():
            return None, None
        try:
            d = _json.loads(info.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None, None
        title = (d.get("title") or d.get("fulltitle") or "").strip() or None
        pub = None
        ud = d.get("upload_date") or d.get("release_date")  # YYYYMMDD
        if ud and len(str(ud)) == 8:
            try:
                pub = datetime.strptime(str(ud), "%Y%m%d").replace(tzinfo=timezone.utc).isoformat()
            except ValueError:
                pass
        return title, pub

    def _bili_published_at(self, url: str) -> str | None:
        """取 B 站视频发布时间(pubdate)→ ISO 字符串。尽力而为,失败返回 None,不影响下载。"""
        bvid = extract_bilibili_bvid(url)
        if not bvid:
            return None
        try:
            import json as _json
            import urllib.request
            from datetime import datetime, timezone

            req = urllib.request.Request(
                f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}",
                headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com/"},
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                d = _json.loads(r.read().decode("utf-8"))
            ts = d.get("data", {}).get("pubdate") if d.get("code") == 0 else None
            if ts:
                return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
        except Exception as e:
            self.log.warn("bili_pubdate_failed", error=str(e)[:120])
        return None

    def _download_bilibili(self, url: str) -> None:
        bvid = extract_bilibili_bvid(url)
        target_url = f"https://www.bilibili.com/video/{bvid}" if bvid else url
        input_dir = self.job_dir / "input"
        input_dir.mkdir(parents=True, exist_ok=True)

        cmd = [
            "yutto", target_url,
            "-d", str(input_dir),
            "-tp", "{title}",
            "-q", "80",   # 1080P 上限:平衡主视觉清晰度与 NAS↔ECS 隧道/MinIO 带宽
        ]

        # 优先用本机侧载凭证文件 input/.credentials.json 里的 SESSDATA(扫码登录入库;
        # 该文件只存在于同机 LocalStorage,绝不下发远端 worker——见 shared/storage.is_credential_file);
        # 否则回退本地 cookie 文件;两者皆无则匿名下载,降级 480P。
        # 取值优先级:本机侧载 → 本地 cookie 文件。注意 yutto 的 -c 要的是 SESSDATA「值」,
        # 不是文件路径——此前回退误把 bilibili.txt 路径当值传给 -c,致登录态失效:匿名下载、
        # 无字幕(字幕需登录)、清晰度降 480P。
        sessdata = self._read_sessdata() or self._read_cookie_file_sessdata()
        if sessdata:
            cmd.extend(["-c", sessdata])
        else:
            self.log.warn("no_bilibili_cookies", msg="降级 480P")

        # yutto 主力,失败转 yt-dlp 兜底(移植老原型双引擎),最后 ffprobe 验收挡坏下载。
        try:
            self.run_subprocess(cmd, timeout=self.config["step"]["timeout_sec"])
            self._rename_downloaded_video(input_dir)
            self._prune_subtitles_danmaku(input_dir)
        except Exception as e:
            self.log.warn("yutto_failed_ytdlp_fallback", error=str(e)[:200])
            self._download_bili_ytdlp(target_url, input_dir, sessdata)
        self._verify_download(input_dir / "source.mp4")

    def _read_sessdata(self) -> str | None:
        """从本机侧载凭证文件读 SESSDATA(只在同机 LocalStorage 存在;远端 worker 取不到)。
        文件缺失/损坏/无字段均返回 None,由调用方回退本地 cookie 文件。"""
        import json as _json
        cred = self.job_dir / "input" / ".credentials.json"
        if not cred.is_file():
            return None
        try:
            return _json.loads(cred.read_text(encoding="utf-8")).get("sessdata") or None
        except (OSError, ValueError):
            return None

    def _read_cookie_file_sessdata(self) -> str | None:
        """从 /data/cookies/bilibili.txt 取 SESSDATA「值」(Netscape cookie 文件或裸值均可)。
        MinIO 部署下侧载凭证不下发远端 worker,此文件是登录态来源(无则匿名、无字幕)。"""
        p = Path(os.environ.get("DATA_DIR", "/data")) / "cookies" / "bilibili.txt"
        if not p.is_file():
            return None
        try:
            txt = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return None
        m = re.search(r"SESSDATA[\s=]+([^\s;]+)", txt)  # Netscape 行 或 cookie 串
        if m:
            return m.group(1)
        s = txt.strip()
        return s or None

    def _download_bili_ytdlp(self, url: str, input_dir: Path, sessdata: str | None) -> None:
        """yutto 失败时的兜底引擎。"""
        cmd = [
            "yt-dlp",
            "-o", str(input_dir / "source.%(ext)s"),
            "-f", "bestvideo[height<=1080]+bestaudio/best[height<=1080]",
            "--merge-output-format", "mp4",
            "--referer", "https://www.bilibili.com/",
        ]
        if sessdata:
            cmd += ["--add-header", f"Cookie:SESSDATA={sessdata}"]
        cmd += ["--", url]
        self.run_subprocess(cmd, timeout=self.config["step"]["timeout_sec"])
        self._rename_to_source_mp4(input_dir)

    def _verify_download(self, mp4: Path) -> None:
        """ffprobe 验收:文件存在 + >1MB + 可读出时长,挡半截/无源的坏下载污染下游。"""
        from shared.errors import InputInvalidError
        if not mp4.exists() or mp4.stat().st_size < 1_000_000:
            raise InputInvalidError(f"download missing or too small: {mp4.name}")
        duration = self._get_video_duration(mp4)
        if not duration or duration < 1:
            raise InputInvalidError(f"download has no playable duration: {mp4.name}")

    def _download_youtube(self, url: str) -> None:
        input_dir = self.job_dir / "input"
        input_dir.mkdir(parents=True, exist_ok=True)

        cmd = [
            "yt-dlp",
            "-o", str(input_dir / "source.%(ext)s"),
            "--write-info-json",   # 写 source.info.json(供元信息取标题/上传日期)
            "--write-sub", "--sub-lang", "en,zh-Hans",
            "--convert-subs", "srt",
            "-f", "bestvideo[height<=1080]+bestaudio/best[height<=1080]",
            "--merge-output-format", "mp4",
        ]
        # 上传的 YouTube cookies(/data/cookies/youtube.txt,Netscape 格式)用于受限/年龄限制
        # 视频;缺失则匿名下载。写入方见 api/routes/auth.py:/youtube/cookies。
        cookies = Path(os.environ.get("DATA_DIR", "/data")) / "cookies" / "youtube.txt"
        if cookies.exists():
            cmd += ["--cookies", str(cookies)]
        cmd += ["--", url]  # -- 分隔:挡以 "-" 开头的 url 被当作 yt-dlp 选项注入
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

        # 顺手抽一份标题等元数据,正文解析仍由 02_parse_article 负责(trafilatura)。
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

    def _copy_local_file(self, url: str, content_type: str) -> None:
        """把 file:// url 指向的宿主本地文件复制进 input/(按 content_type 命名为 source.*)。

        被监听目录(local_dir 订阅源)已挂进 worker 容器,故路径在容器内可达。
        不走网络;复制后视频类走 ffprobe 校验,挡空/坏文件污染下游。"""
        import shutil
        from urllib.parse import unquote, urlparse

        from shared.errors import InputInvalidError

        src = Path(unquote(urlparse(url).path))
        if not src.is_file():
            raise InputInvalidError(f"local file not found: {src}")

        input_dir = self.job_dir / "input"
        input_dir.mkdir(parents=True, exist_ok=True)

        # 按 content_type 落成下游约定的 source.* 名(沿用原扩展名,音频另由
        # _link_audio_for_whisper 备一份 source.mp4)。
        ext = src.suffix.lower()
        name_by_type = {
            "paper": "source.pdf",
            "video": "source.mp4",
            "article": f"source{ext or '.html'}",
            "audio": f"source{ext or '.mp3'}",
        }
        dest = input_dir / name_by_type.get(content_type, f"source{ext}")
        shutil.copyfile(src, dest)

        if content_type == "video":
            self._verify_download(dest)

    def _download_generic(self, url: str) -> None:
        from shared.net import assert_public_url

        assert_public_url(url)  # 下载前挡内网/回环目标(SSRF):generic 接任意用户 URL,此前无校验
        input_dir = self.job_dir / "input"
        input_dir.mkdir(parents=True, exist_ok=True)

        cmd = [
            "yt-dlp",
            "-o", str(input_dir / "source.%(ext)s"),
            "-f", "bestvideo[height<=1080]+bestaudio/best[height<=1080]",
            "--merge-output-format", "mp4",
            "--", url,  # -- 分隔:挡以 "-" 开头的 url 被当作 yt-dlp 选项注入
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

    def _prune_subtitles_danmaku(self, input_dir: Path) -> None:
        """精简下载产物,避免冗余:
        - 字幕:原生中文视频只留一份中文字幕(删 B 站 AI 翻译的其它语种,机械/智能版用不到);
          外文视频保留全部 srt,交 08 选原生语种并翻译。
        - 弹幕:多份 .ass(yutto 常同时落 danmaku.ass 与 <标题>.ass)只留一份 danmaku.ass。"""
        from steps.utils.srt_parser import _looks_chinese

        srts = sorted(input_dir.glob("*.srt"))
        zh = [f for f in srts if _looks_chinese(f)]
        if zh:
            marked = [f for f in zh if any(k in f.name.lower() for k in ("中文", "zh", "chs", "cn"))]
            keep = (marked or zh)[0]
            for f in srts:
                if f != keep:
                    f.unlink()

        asses = sorted(input_dir.glob("*.ass"))
        if asses:
            keep = asses[0]
            if keep.name != "danmaku.ass":
                keep = keep.rename(input_dir / "danmaku.ass")
            for f in asses:
                if f != keep and f.exists():
                    f.unlink()

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

        def _set_size(p: Path) -> None:
            # 原始文件大小:存精确字节(前端转 KB/MB/GB)+ 兼容旧 file_size_mb。
            b = p.stat().st_size
            metadata["file_size_bytes"] = b
            metadata["file_size_mb"] = round(b / 1048576, 1)

        video_file = input_dir / "source.mp4"
        if video_file.exists():
            metadata["duration_sec"] = self._get_video_duration(video_file)
            _set_size(video_file)
            w, h = self._get_video_resolution(video_file)
            if w and h:
                metadata["width"], metadata["height"] = w, h
                metadata["resolution"] = f"{w}x{h}"

        pdf_file = input_dir / "source.pdf"
        if pdf_file.exists():
            _set_size(pdf_file)

        html_file = input_dir / "source.html"
        if html_file.exists():
            _set_size(html_file)

        # 音频:对原始音频文件(非复制出的 source.mp4)取时长与大小。
        for ext in (".mp3", ".m4a", ".wav", ".aac"):
            audio_file = input_dir / f"source{ext}"
            if audio_file.exists():
                metadata["duration_sec"] = self._get_video_duration(audio_file)
                _set_size(audio_file)
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

    def _get_video_resolution(self, video_path: Path) -> tuple[int | None, int | None]:
        """ffprobe 取视频首个视频流的宽高(像素)。失败返回 (None, None)。"""
        try:
            result = subprocess.run(
                [
                    "ffprobe", "-v", "quiet",
                    "-select_streams", "v:0",
                    "-show_entries", "stream=width,height",
                    "-of", "csv=s=x:p=0",
                    str(video_path),
                ],
                capture_output=True, text=True, timeout=30,
            )
            out = result.stdout.strip()
            if result.returncode == 0 and "x" in out:
                w, h = out.split("x")[:2]
                return int(w), int(h)
        except (subprocess.TimeoutExpired, ValueError):
            pass
        return None, None


if __name__ == "__main__":
    DownloadStep.cli_main("01_download")
