"""Step 01: 下载。各内容类型(视频/论文/文章/音频)共用,按来源分派 yutto/yt-dlp/arXiv/curl/本地复制。"""

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

        # 本地目录订阅(local_dir 适配器)枚举出的 file:// url 不走网络下载:
        # 把宿主本地文件复制进 job 的 input/,被监听目录已挂进容器,再走正常校验。
        # create_job_core 用 detect_source 给本地 url 的 source 记 "other",故这里
        # 直接按 url 前缀判定,优先于 source 分派。
        if url.startswith("file://"):
            self.log.info("local_file_mode", content_type=content_type)
            source = "local"
            self._copy_local_file(url, content_type)
        elif source == "upload":
            self.log.info("upload_mode", content_type=content_type)
        elif content_type == "audio" and source not in ("bilibili", "youtube"):
            # 显式音频任务:无论 URL 是音频直链(podcast 源)还是播客页面,都走音频下载——
            # 否则页面 URL 被 detect_source 判成 http_article 走文章分支,whisper 无音源会挂。
            # bilibili/youtube 留给各自带凭证/字幕的下载器(那类应作 video,不在此拦)。
            self._download_audio(url)
        elif source == "bilibili":
            self._download_bilibili(url)
        elif source == "youtube":
            self._download_youtube(url)
        elif source == "arxiv":
            self._download_arxiv(url)
        elif source == "pdf":
            self._download_pdf(url)
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
            "-q", "80",   # 1080P 上限:平衡主视觉清晰度与 NAS 到 ECS 的隧道/MinIO 带宽
        ]

        # SESSDATA 取值优先级见 _resolve_sessdata。侧载凭证由扫码登录入库,只存在于同机
        # LocalStorage,绝不下发远端 worker,见 shared/storage.is_credential_file。
        # 皆取不到则匿名下载,降级 480P。
        # yutto 的 -c 要的是 SESSDATA 值,不是文件路径;传路径会静默失去登录态:
        # 匿名下载、无字幕(字幕需登录)、清晰度降 480P。
        sessdata = self._resolve_sessdata()
        if sessdata:
            cmd.extend(["-c", sessdata])
        else:
            self.log.warn("no_bilibili_cookies", msg="降级 480P")

        # yutto 主力,失败转 yt-dlp 兜底,最后 ffprobe 验收挡坏下载。
        try:
            self.run_subprocess(cmd, timeout=self.config["step"]["timeout_sec"])
            self._rename_downloaded_video(input_dir)
            self._prune_subtitles_danmaku(input_dir)
        except Exception as e:
            self.log.warn("yutto_failed_ytdlp_fallback", error=str(e)[:200])
            self._download_bili_ytdlp(target_url, input_dir, sessdata)
        self._verify_download(input_dir / "source.mp4")

    def _resolve_sessdata(self) -> str | None:
        """B站 SESSDATA 取值优先级:env BILI_SESSDATA、本机侧载 input/.credentials.json、
        本地 cookie 文件 /data/cookies/bilibili.txt。env 方式适合无状态 worker:
        凭证随 env 注入,不落本地文件。"""
        return (
            os.environ.get("BILI_SESSDATA", "").strip()
            or self._read_sessdata()
            or self._read_cookie_file_sessdata()
        )

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
        """从 /data/cookies/bilibili.txt 取 SESSDATA 值(Netscape cookie 文件或裸值均可)。
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
            # 避开 AV1:处理镜像 ffmpeg 解不了 av01,场景检测/抽帧会拿到 0 结果(场景/关键帧全空)。
            # 优先非-av1 视频,-S 再偏好 H.264;实在只有 av1 才回退,保证总能下到东西。
            "-f", "bestvideo[height<=1080][vcodec!*=av01]+bestaudio/"
                  "bestvideo[height<=1080]+bestaudio/best[height<=1080]",
            "-S", "vcodec:h264",
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

        # 先抓 arxiv API 元数据(标题/作者/摘要/发布日):PDF 解析抓不准,标题常成左边距 arXiv 戳、作者空。
        self._fetch_arxiv_meta(arxiv_id)

        pdf_url = f"https://arxiv.org/pdf/{arxiv_id}.pdf"
        cmd = ["curl", "-fSL", "-o", str(input_dir / "source.pdf"), pdf_url]
        self.run_subprocess(cmd, timeout=120)

        # HTML 源(论文源头重做):arxiv 官方/ar5iv 的 LaTeXML 渲染结构+公式无损,原文/翻译/笔记
        # 全吃它(pymupdf 逆向 PDF 断词、公式丢,已废)。PDF 仍保留(下载入口 + 无 HTML 论文兜底)。
        self._fetch_arxiv_html(arxiv_id)

    def _fetch_arxiv_html(self, arxiv_id: str) -> None:
        """抓 arxiv HTML 源 → input/source.html;页内图片下载到 job 根 assets/(与 article/前端
        `/api/jobs/{id}/assets/` 约定一致),并把 HTML 里的 src 重写为 assets/<名>。
        先官方 https://arxiv.org/html/<id>(新论文原生),404 再 ar5iv;都失败 = 无 HTML 源
        (老 LaTeX 编译失败/纯扫描件),不写 source.html → 02 步按 pdf-only 处理。best-effort。"""
        html = None
        for base in (f"https://arxiv.org/html/{arxiv_id}",
                     f"https://ar5iv.labs.arxiv.org/html/{arxiv_id}"):
            html = self._fetch_html(base, timeout=60)
            # ar5iv 对无 HTML 的论文回 200 落地页(含 ar5iv 提示语),官方 404 → None;
            # 粗判:LaTeXML 产物必有 ltx_ 标记。
            if html and "ltx_" in html:
                self._arxiv_html_base = base
                break
            html = None
        if not html:
            self.log.info("arxiv_html_unavailable", arxiv_id=arxiv_id)
            return
        html = self._localize_html_images(html, self._arxiv_html_base)
        self.write_output("input/source.html", html)
        self.log.info("arxiv_html_fetched", arxiv_id=arxiv_id, base=self._arxiv_html_base,
                      bytes=len(html))

    def _localize_html_images(self, html: str, base_url: str) -> str:
        """下载 HTML 内 <img src> 到 job 根 assets/,src 重写为 assets/<扁平名>。
        单图失败保留原引用(绝对化,前端在线渲染兜底),不失败整体。"""
        from urllib.parse import urljoin

        assets = self.job_dir / "assets"
        srcs = dict.fromkeys(re.findall(r'<img[^>]+src="([^"]+)"', html))
        n_ok = 0
        for src in srcs:
            if src.startswith("data:"):
                continue
            absolute = urljoin(base_url + "/", src)
            fname = re.sub(r"[^A-Za-z0-9._-]", "_", src.split("?")[0].strip("/"))[-80:]
            try:
                assets.mkdir(parents=True, exist_ok=True)
                self.run_subprocess(
                    ["curl", "-fsSL", "-o", str(assets / fname), "--", absolute], timeout=60)
                html = html.replace(f'src="{src}"', f'src="assets/{fname}"')
                n_ok += 1
            except Exception as e:
                # 失败:引用绝对化留痕(相对路径离开 arxiv 域必坏,绝对 URL 至少可在线渲染)。
                html = html.replace(f'src="{src}"', f'src="{absolute}"')
                self.log.warning("arxiv_html_image_failed", src=src[:120], error=str(e)[:120])
        if n_ok:
            self.log.info("arxiv_html_images_localized", count=n_ok, total=len(srcs))
        return html

    def _fetch_arxiv_meta(self, arxiv_id: str) -> None:
        """arxiv API 取权威元数据 → stash self._arxiv_meta(由 _extract_metadata 并入 metadata.json)。
        标准库 ElementTree 解析 Atom,零运行时依赖(曾用 feedparser,worker 镜像没装它,宽 except 把
        ModuleNotFoundError 当网络错误静默吞 → 所有 arxiv 论文标题/作者丢失、UI 显示 job_id)。
        best-effort 只兜【网络/坏响应】(curl 失败/超时/坏 XML → 回退 PDF 启发);编程错误照常上抛。"""
        import xml.etree.ElementTree as ET

        from shared.step_base import SubprocessFailed

        api = f"http://export.arxiv.org/api/query?id_list={arxiv_id}"
        try:
            r = self.run_subprocess(["curl", "-fsSL", api], timeout=30)
            meta = self._parse_arxiv_atom(r.stdout)
        except (SubprocessFailed, subprocess.TimeoutExpired, ET.ParseError) as ex:
            self.log.warning("arxiv_meta_fetch_failed", arxiv_id=arxiv_id, error=str(ex)[:200])
            return
        if not meta:
            self.log.warning("arxiv_meta_empty", arxiv_id=arxiv_id)
            return
        self._arxiv_meta = meta
        self.log.info("arxiv_meta_fetched", arxiv_id=arxiv_id,
                      title=meta.get("title"), authors=len(meta.get("authors", [])))

    @staticmethod
    def _parse_arxiv_atom(xml_text: str) -> dict:
        """arxiv Atom → {title, authors, abstract, published_at};无 entry 返回 {}。
        坏 XML 抛 ParseError,由调用方按网络类失败兜底。"""
        import xml.etree.ElementTree as ET
        ns = {"a": "http://www.w3.org/2005/Atom"}
        entry = ET.fromstring(xml_text).find("a:entry", ns)
        if entry is None:
            return {}
        meta: dict = {}
        title = entry.findtext("a:title", "", ns)
        if title.strip():
            meta["title"] = " ".join(title.split())                 # 去 arxiv title 里的换行
        authors = [a for a in (el.findtext("a:name", "", ns).strip()
                               for el in entry.findall("a:author", ns)) if a]
        if authors:
            meta["authors"] = authors
        summary = entry.findtext("a:summary", "", ns)
        if summary.strip():
            meta["abstract"] = " ".join(summary.split())
        published = entry.findtext("a:published", "", ns)
        if published:
            meta["published_at"] = published[:10]                   # ISO → YYYY-MM-DD
        return meta

    def _download_pdf(self, url: str) -> None:
        """非 arxiv 的直链 PDF(OSDI/usenix/会议/期刊等)→ input/source.pdf,供 02_pdf_parse 消费。"""
        from shared.net import assert_public_url

        assert_public_url(url)   # 抓取前挡内网/回环目标(SSRF),与 _download_article 一致
        input_dir = self.job_dir / "input"
        input_dir.mkdir(parents=True, exist_ok=True)
        cmd = ["curl", "-fSL", "-o", str(input_dir / "source.pdf"), url]
        self.run_subprocess(cmd, timeout=120)

    def _download_article(self, url: str) -> None:
        """抓 HTML 原文写 input/source.html;同时用 trafilatura 抽正文/标题供后续解析。
        抓取用 urllib(尊重 HTTP(S)_PROXY env)——trafilatura.fetch_url 内部 urllib3 不读代理 env,
        必须走代理的站(HF 等)直连必败、把退避整轮白烧;trafilatura 只负责解析不再负责下载。"""
        import trafilatura

        from shared.net import assert_public_url

        assert_public_url(url)  # 抓取前挡内网/回环目标(SSRF)
        input_dir = self.job_dir / "input"
        input_dir.mkdir(parents=True, exist_ok=True)

        # 慢站首拍常超时 → 指数退避重试:超时 30→60→120→240→480s。
        # 步超时(pipelines.yaml article 01_download)须 ≥ 各拍之和 ~930s,否则退避被腰斩。
        html = None
        timeout = 30
        for _ in range(5):
            html = self._fetch_html(url, timeout)
            if html:
                break
            timeout *= 2
        if not html:
            from shared.errors import InputInvalidError
            raise InputInvalidError(f"Cannot fetch article: {url} (5 次退避重试后仍失败)")
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

    @staticmethod
    def _fetch_html(url: str, timeout: int) -> str | None:
        """urllib 抓单页(尊重代理 env),失败返 None(调用方退避重试)。
        解码链:HTTP header charset → utf-8 → gb18030(GBK/GB2312 中文站) → utf-8 宽容兜底。"""
        import urllib.request
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                charset = r.headers.get_content_charset()
        except Exception:
            return None
        if not raw:
            return None
        for enc in filter(None, (charset, "utf-8", "gb18030")):
            try:
                return raw.decode(enc)
            except (UnicodeDecodeError, LookupError):
                continue
        return raw.decode("utf-8", errors="replace")

    def _download_audio(self, url: str) -> None:
        """音频任务下载 → input/source.mp3,后续复制为 source.mp4 供 whisper;ffmpeg 按内容
        嗅探解码,扩展名不影响转写。支持音频直链(mp3/m4a/wav/aac/flac)与播客页面 URL,
        后者 best-effort 从页面解析音频真链。下载后 ffprobe 校验:挡住 404/HTML 存成 mp3
        拖到 whisper 才报晦涩 ffmpeg 错。"""
        from shared.errors import InputInvalidError
        from shared.net import assert_public_url
        from shared.source_detect import detect_source, extract_audio_enclosure

        assert_public_url(url)  # 下载前挡内网/回环目标(SSRF)
        input_dir = self.job_dir / "input"
        input_dir.mkdir(parents=True, exist_ok=True)
        dest = input_dir / "source.mp3"

        media_url = url
        # 给的是网页(非音频直链)→ 先抓页面解析出音频真链再下。
        if detect_source(url) != "podcast":
            resolved = self._resolve_audio_from_page(url)
            if resolved:
                assert_public_url(resolved)
                self.log.info("audio_enclosure_resolved", src=resolved[:200])
                media_url = resolved

        self._curl_to(media_url, dest)

        if not self._verify_audio(dest):
            # 直链回来的可能是落地页 HTML(部分 CDN 对裸 UA 返回页面)→ 从内容里再解析真链重试一次。
            try:
                content = dest.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                content = ""
            recovered = extract_audio_enclosure(content, base_url=media_url)
            if recovered and recovered != media_url:
                assert_public_url(recovered)
                self.log.info("audio_enclosure_recovered", src=recovered[:200])
                self._curl_to(recovered, dest)
            if not self._verify_audio(dest):
                raise InputInvalidError(
                    f"audio download is not a playable media file (HTML/404?): {url}"
                )

    def _curl_to(self, url: str, dest: Path) -> None:
        """curl 下载到 dest。带浏览器 UA:部分 CDN 对裸 curl UA 返回落地页而非音频文件。"""
        cmd = ["curl", "-fSL", "-A", "Mozilla/5.0", "-o", str(dest), "--", url]
        self.run_subprocess(cmd, timeout=self.config["step"]["timeout_sec"])

    def _resolve_audio_from_page(self, page_url: str) -> str | None:
        """抓播客页面 HTML,解析出音频直链(og:audio/<audio>/<enclosure>/<a *.mp3>)。
        best-effort:网络/解析失败返回 None,由调用方按原 URL 继续(再失败则校验拦下)。"""
        from shared.source_detect import extract_audio_enclosure
        try:
            r = self.run_subprocess(
                ["curl", "-fsSL", "-A", "Mozilla/5.0", "--", page_url], timeout=60,
            )
            return extract_audio_enclosure(r.stdout or "", base_url=page_url)
        except Exception as e:
            self.log.warning("audio_page_resolve_failed", error=str(e)[:160])
            return None

    def _verify_audio(self, path: Path) -> bool:
        """音频下载验收:文件存在 + >2KB + ffprobe 读得出时长(>0.5s)。
        HTML 错误页/404 体没有可解码时长,ffprobe 失败即返回 False。"""
        if not path.exists() or path.stat().st_size < 2048:
            return False
        dur = self._get_video_duration(path)  # ffprobe format=duration,音频同样适用
        return bool(dur and dur > 0.5)

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

        assert_public_url(url)  # 下载前挡内网/回环目标(SSRF):generic 接任意用户 URL
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
        """yutto 下载的视频文件名不固定,重命名为 source.mp4。"""
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
        from steps.utils.srt_parser import _looks_chinese, CHINESE_SUBTITLE_KEYWORDS

        srts = sorted(input_dir.glob("*.srt"))
        zh = [f for f in srts if _looks_chinese(f)]
        if zh:
            marked = [f for f in zh if any(k in f.name.lower() for k in CHINESE_SUBTITLE_KEYWORDS)]
            keep = (marked or zh)[0]
            for f in srts:
                if f != keep:
                    f.unlink()

        asses = sorted(input_dir.glob("*.ass"))
        if asses:
            target = input_dir / "danmaku.ass"
            # 已存在 danmaku.ass 则以它为准,不要把字母序首个 rename 覆盖掉它。
            keep = target if target.exists() else asses[0]
            if keep.name != "danmaku.ass":
                keep = keep.rename(target)
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

    def _probe_codec_info(self, video_file: Path) -> dict:
        """ffprobe 取视频/音频编码、码率、帧率等基本信息(供前端「元信息」展示)。尽力而为,失败返回空。"""
        import json as _json
        import subprocess
        try:
            out = subprocess.run(
                ["ffprobe", "-v", "error", "-print_format", "json",
                 "-show_streams", "-show_format", str(video_file)],
                capture_output=True, text=True, timeout=30,
            )
            d = _json.loads(out.stdout or "{}")
        except Exception:
            return {}
        info: dict = {}
        streams = d.get("streams", [])
        vs = next((s for s in streams if s.get("codec_type") == "video"), None)
        aud = next((s for s in streams if s.get("codec_type") == "audio"), None)
        fmt = d.get("format", {})

        def _kbps(v) -> int | None:
            return round(int(v) / 1000) if v and str(v).isdigit() else None

        if vs:
            info["video_codec"] = vs.get("codec_name")
            fr = vs.get("avg_frame_rate") or vs.get("r_frame_rate") or ""
            if "/" in fr:
                a, b = fr.split("/", 1)
                try:
                    if float(b):
                        info["fps"] = round(float(a) / float(b), 2)
                except (ValueError, ZeroDivisionError):
                    pass
            vb = _kbps(vs.get("bit_rate"))
            if vb is not None:
                info["video_bitrate_kbps"] = vb
        if aud:
            info["audio_codec"] = aud.get("codec_name")
            ab = _kbps(aud.get("bit_rate"))
            if ab is not None:
                info["audio_bitrate_kbps"] = ab
        tb = _kbps(fmt.get("bit_rate"))
        if tb is not None:
            info["bitrate_kbps"] = tb  # 总码率(视频流缺 bit_rate 时也能从容器拿到)
        return info

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
            metadata.update(self._probe_codec_info(video_file))  # 编码/码率/帧率等基本信息

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
        # 并入 arxiv API 元数据(title/authors/abstract/published_at),作权威来源,优先于 PDF 启发。
        metadata.update(getattr(self, "_arxiv_meta", {}) or {})
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
