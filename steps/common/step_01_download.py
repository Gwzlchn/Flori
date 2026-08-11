"""Step 01: 下载。各内容类型(视频/论文/文章/音频)共用,按来源分派 yutto/yt-dlp/arXiv/curl/本地复制。"""

from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

from shared.document_source_resolver import (
    DocumentSourceAlternative,
    detect_access_challenge,
    extract_research_pdf_url,
    resolve_document_source_alternative,
)
from shared.source_detect import detect_source, extract_arxiv_id, extract_bilibili_bvid
from shared.scholarly_html_snapshot import (
    SCHOLARLY_HTML_ASSET_PREFIX,
    SCHOLARLY_HTML_CSS_MAX_BYTES,
    SCHOLARLY_HTML_MAX_RESOURCES,
    SCHOLARLY_HTML_MAX_REFERENCE_EVENTS,
    SCHOLARLY_HTML_MAX_STYLE_CHUNKS,
    SCHOLARLY_HTML_MAX_STYLESHEETS,
    SCHOLARLY_HTML_RESOURCE_MAX_BYTES,
    SCHOLARLY_HTML_RESOURCE_TOTAL_MAX_BYTES,
    SCHOLARLY_HTML_SOURCE_MAX_BYTES,
    SCHOLARLY_HTML_SNAPSHOT_FORMAT,
    SCHOLARLY_HTML_SNAPSHOT_MAX_BYTES,
    SCHOLARLY_HTML_SNAPSHOT_PATH,
    SCHOLARLY_HTML_SNAPSHOT_VERSION,
    canonical_snapshot_bytes,
    iter_css_urls,
    scholarly_provider_hosts,
    sha256_digest,
    validate_scholarly_document_url,
    validate_scholarly_html_snapshot,
)
from shared.step_base import StepBase, file_hash


@dataclass(frozen=True)
class HttpFetchResult:
    """一次文档抓取的有界响应,保留失败诊断但不记录敏感响应头."""

    body: bytes
    final_url: str
    status_code: int | None
    content_type: str
    error: str | None


class _PublicUrlRedirectHandler(urllib.request.HTTPRedirectHandler):
    """每个 HTTP redirect 在连接前重做公网 URL 校验."""

    max_redirections: int = 5
    max_repeats: int = 2

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        from shared.net import assert_public_url

        target = urljoin(req.full_url, newurl)
        assert_public_url(target)
        return super().redirect_request(
            req, fp, code, msg, headers, target,
        )


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """连接已校验的数字IP,但TLS证书与SNI仍绑定原始域名。"""

    def __init__(
        self,
        host: str,
        port: int,
        pinned_ip: str,
        *,
        timeout: int,
    ) -> None:
        super().__init__(
            host,
            port=port,
            timeout=timeout,
            context=ssl.create_default_context(),
        )
        self._pinned_ip = pinned_ip

    def connect(self) -> None:
        original = self._create_connection

        def create_pinned(_address, timeout, source_address):
            ip = ipaddress.ip_address(self._pinned_ip)
            family = socket.AF_INET6 if ip.version == 6 else socket.AF_INET
            sock = socket.socket(family, socket.SOCK_STREAM)
            try:
                sock.settimeout(timeout)
                if source_address:
                    sock.bind(source_address)
                target = (
                    (self._pinned_ip, self.port, 0, 0)
                    if ip.version == 6
                    else (self._pinned_ip, self.port)
                )
                sock.connect(target)
                return sock
            except BaseException:
                sock.close()
                raise

        self._create_connection = create_pinned
        try:
            super().connect()
        finally:
            self._create_connection = original


def _public_https_target(url: str) -> tuple[str, int, tuple[str, ...]]:
    """解析并冻结一次公网HTTPS目标;返回值可直接用于固定IP连接。"""
    if len(url) > 8192:
        raise ValueError("snapshot resource URL exceeds limit")
    parsed = urlparse(url)
    try:
        port = parsed.port or 443
    except ValueError as exc:
        raise ValueError("snapshot resource URL has invalid port") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ValueError("snapshot resource URL must be uncredentialed HTTPS")
    try:
        infos = socket.getaddrinfo(
            parsed.hostname,
            port,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
    except socket.gaierror as exc:
        raise ValueError("snapshot resource host cannot be resolved") from exc
    addresses: list[str] = []
    for info in infos:
        address = str(ipaddress.ip_address(info[4][0]))
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            raise ValueError("snapshot resource resolves to non-public address")
        if address not in addresses:
            addresses.append(address)
    if not addresses:
        raise ValueError("snapshot resource host has no public address")
    return parsed.hostname, port, tuple(addresses)


class _StaticHtmlReferenceParser(HTMLParser):
    """只提取浏览器自动加载的论文样式与图片边,不解释脚本内伪标签。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.styles: list[tuple[str, str]] = []
        self.images: list[str] = []
        self._linked_styles: set[str] = set()
        self._image_sources: set[str] = set()
        self._style_chunks: list[str] | None = None
        self._style_bytes = 0
        self._inline_style_bytes = 0
        self._style_chunk_count = 0
        self._reference_events = 0

    @staticmethod
    def _attr_map(attrs: list[tuple[str, str | None]]) -> dict[str, str]:
        if len(attrs) > 256:
            raise ValueError("HTML static resource attribute count exceeds limit")
        total_bytes = 0
        for name, value in attrs:
            raw_value = value or ""
            if len(name) + len(raw_value) > 256 * 1024:
                raise ValueError("HTML static resource attributes exceed byte limit")
            total_bytes += len(name.encode("utf-8")) + len(raw_value.encode("utf-8"))
            if total_bytes > 256 * 1024:
                raise ValueError("HTML static resource attributes exceed byte limit")
        return {name.casefold(): value or "" for name, value in attrs}

    def _add_reference_event(self) -> None:
        self._reference_events += 1
        if self._reference_events > SCHOLARLY_HTML_MAX_REFERENCE_EVENTS:
            raise ValueError("HTML static reference count exceeds limit")

    def _add_style(self, kind: str, value: str) -> None:
        if len(self.styles) >= SCHOLARLY_HTML_MAX_STYLESHEETS:
            raise ValueError("HTML stylesheet count exceeds limit")
        self.styles.append((kind, value))

    def _append_style_chunk(self, value: str) -> None:
        if self._style_chunks is None:
            return
        self._style_chunk_count += 1
        if self._style_chunk_count > SCHOLARLY_HTML_MAX_STYLE_CHUNKS:
            raise ValueError("inline stylesheet chunk count exceeds limit")
        if len(value) > SCHOLARLY_HTML_CSS_MAX_BYTES:
            raise ValueError("inline stylesheet exceeds limit")
        size = len(value.encode("utf-8"))
        if self._style_bytes + size > SCHOLARLY_HTML_CSS_MAX_BYTES:
            raise ValueError("inline stylesheet exceeds limit")
        if self._inline_style_bytes + size > SCHOLARLY_HTML_RESOURCE_TOTAL_MAX_BYTES:
            raise ValueError("inline stylesheets exceed total byte limit")
        self._style_bytes += size
        self._inline_style_bytes += size
        self._style_chunks.append(value)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized = tag.casefold()
        if normalized not in {"link", "style", "img", "image"}:
            return
        values = self._attr_map(attrs) if normalized != "style" else {}
        if normalized == "link":
            rel = {token.casefold() for token in values.get("rel", "").split()}
            if "stylesheet" in rel and values.get("href"):
                self._add_reference_event()
                href = values["href"]
                if href not in self._linked_styles:
                    self._linked_styles.add(href)
                    self._add_style("link", href)
        elif normalized == "style":
            if self._style_chunks is not None:
                raise ValueError("nested style element is not allowed")
            if len(self.styles) >= SCHOLARLY_HTML_MAX_STYLESHEETS:
                raise ValueError("HTML stylesheet count exceeds limit")
            self._style_chunks = []
            self._style_bytes = 0
            self._style_chunk_count = 0
        elif normalized == "img" and values.get("src"):
            self._add_reference_event()
            source = values["src"]
            if source not in self._image_sources:
                if len(self.images) >= SCHOLARLY_HTML_MAX_RESOURCES:
                    raise ValueError("HTML image count exceeds limit")
                self._image_sources.add(source)
                self.images.append(source)
        elif normalized == "image":
            source = values.get("href") or values.get("xlink:href")
            if source:
                self._add_reference_event()
                if source not in self._image_sources:
                    if len(self.images) >= SCHOLARLY_HTML_MAX_RESOURCES:
                        raise ValueError("HTML image count exceeds limit")
                    self._image_sources.add(source)
                    self.images.append(source)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() != "style" or self._style_chunks is None:
            return
        self._add_style("inline", "".join(self._style_chunks))
        self._style_chunks = None

    def handle_data(self, data: str) -> None:
        self._append_style_chunk(data)

    def handle_entityref(self, name: str) -> None:
        self._append_style_chunk(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self._append_style_chunk(f"&#{name};")

    def finish(self) -> tuple[list[tuple[str, str]], list[str]]:
        if self._style_chunks is not None:
            raise ValueError("unclosed style element")
        return list(self.styles), list(self.images)


class DownloadStep(StepBase):
    def validate_inputs(self) -> list[str]:
        if not (self.job_dir / "job.json").exists():
            return ["job.json"]
        return []

    def step_input_hashes(self) -> dict[str, str]:
        return {
            "job": file_hash(self.job_dir / "job.json"),
        }

    def execute(self) -> dict | None:
        job = self.artifacts.load_json("job.json")
        # 上传任务把 url 持久化为 null,不能只依赖 dict.get 的缺省值。
        url = job.get("url") or ""
        source = job.get("source") or detect_source(url)
        content_type = job.get("content_type", "video")

        # 本地目录订阅(local_dir 适配器)枚举出的 file:// url 不走网络下载:
        # 把宿主本地文件复制进 job 的 input/,被监听目录已挂进容器,再走正常校验。
        # create_job_core 已把本地 url 记为 local_file;这里仍按 scheme 守住旧 job 兼容。
        if url.startswith("file://"):
            self.log.info("local_file_mode", content_type=content_type)
            source = "local_file"
            self._copy_local_file(url, content_type)
            if content_type == "document":
                self._normalize_document_input(self.job_dir / "input")
        elif source == "nas_source":
            # Worker已将通过full SHA-256重验的NAS原片以临时只读链接物化。
            # 本步只验证可播性和写metadata,不复制、改名或上传原片。
            if content_type != "video":
                raise ValueError("NAS source only supports video")
            self.log.info("nas_source_mode")
            self._verify_download(self.job_dir / "input" / "source.mp4")
        elif source == "upload":
            self.log.info("upload_mode", content_type=content_type)
            if content_type == "video":
                self._rename_to_source_mp4(self.job_dir / "input")
            elif content_type == "document":
                self._normalize_document_input(self.job_dir / "input")
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
            self._download_article(url, document_kind=job.get("document_kind"))
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
        self.artifacts.write("input/metadata.json", metadata)
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

        # SESSDATA 取值优先级见 _resolve_sessdata(worker 认领时中心分发注入 env,
        # docs/03 §1.7.1)。皆取不到则匿名下载,降级 480P。
        # yutto 的 -c 要的是 SESSDATA 值,不是文件路径;传路径会静默失去登录态:
        # 匿名下载、无字幕(字幕需登录)、清晰度降 480P。
        sessdata = self._resolve_sessdata()
        if sessdata:
            cmd.extend(["-c", sessdata])
        else:
            self.log.warn("no_bilibili_cookies", msg="降级 480P")

        # yutto 主力,失败转 yt-dlp 兜底,最后 ffprobe 验收挡坏下载。
        try:
            self.commands.run(cmd, timeout=self.config["step"]["timeout_sec"])
            self._rename_downloaded_video(input_dir)
            self._prune_subtitles_danmaku(input_dir)
        except Exception as e:
            self.log.warn("yutto_failed_ytdlp_fallback", error=str(e)[:200])
            self._download_bili_ytdlp(target_url, input_dir, sessdata)
        self._verify_download(input_dir / "source.mp4")

    def _resolve_sessdata(self) -> str | None:
        """B站 SESSDATA 取值优先级:env BILI_SESSDATA(worker 认领时中心分发注入,
        随子进程消亡)、本机侧载 input/.credentials.json(job 自带凭证,不上 MinIO)。
        cookie 文件共享已废除(中心分发),worker 零预置。"""
        return (
            os.environ.get("BILI_SESSDATA", "").strip()
            or self._read_sessdata()
        )

    def _read_sessdata(self) -> str | None:
        """从本机侧载凭证文件读 SESSDATA(job 自带凭证,只在同机 LocalStorage 存在)。
        文件缺失/损坏/无字段均返回 None(回到匿名下载)。"""
        import json as _json
        cred = self.job_dir / "input" / ".credentials.json"
        if not cred.is_file():
            return None
        try:
            return _json.loads(cred.read_text(encoding="utf-8")).get("sessdata") or None
        except (OSError, ValueError):
            return None

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
        self.commands.run(cmd, timeout=self.config["step"]["timeout_sec"])
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
        # YouTube cookies(Netscape 文本)经中心分发注入 env FLORI_YT_COOKIES(上传入库见
        # api/routes/auth.py,分发见 docs/03 §1.7.1),用于受限/年龄限制视频;缺失则匿名下载。
        # yt-dlp --cookies 只认文件:写临时文件传入,用毕即删,凭证不留盘。
        import tempfile
        cookies_text = os.environ.get("FLORI_YT_COOKIES", "").strip()
        cookies_file: str | None = None
        try:
            if cookies_text:
                with tempfile.NamedTemporaryFile(
                    "w", suffix=".txt", delete=False, encoding="utf-8"
                ) as f:
                    f.write(cookies_text + "\n")
                    cookies_file = f.name
                cmd += ["--cookies", cookies_file]
            cmd += ["--", url]  # -- 分隔:挡以 "-" 开头的 url 被当作 yt-dlp 选项注入
            self.commands.run(cmd, timeout=self.config["step"]["timeout_sec"])
        finally:
            if cookies_file:
                Path(cookies_file).unlink(missing_ok=True)
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
        cmd = ["curl", "-fSL", "-A", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36", "-o", str(input_dir / "source.pdf"), pdf_url]  # 浏览器 UA:NBER 等站挡 curl 默认 UA(278B 错误页),实测换 UA 即 200
        self.commands.run(cmd, timeout=120)

        # HTML 源(论文源头重做):arxiv 官方/ar5iv 的 LaTeXML 渲染结构+公式无损,原文/翻译/笔记
        # 全吃它(pymupdf 逆向 PDF 断词、公式丢,已废)。PDF 仍保留(下载入口 + 无 HTML 论文兜底)。
        self._fetch_arxiv_html(arxiv_id)

    def _fetch_arxiv_html(self, arxiv_id: str) -> None:
        """冻结官方HTML或ar5iv的静态依赖闭包;任一必需资源失败就换源或回退PDF。"""
        for provider, base in (
            ("arxiv", f"https://arxiv.org/html/{arxiv_id}"),
            ("ar5iv", f"https://ar5iv.labs.arxiv.org/html/{arxiv_id}"),
        ):
            try:
                html, final_url = self._fetch_scholarly_html(base, provider=provider)
            except ValueError as exc:
                self.log.warning(
                    "arxiv_html_snapshot_failed",
                    arxiv_id=arxiv_id,
                    provider=provider,
                    error=f"{type(exc).__name__}:{str(exc)[:240]}",
                )
                continue
            # ar5iv 对无 HTML 的论文回 200 落地页. 官方偶尔以200提前断流,
            # 仅有ltx_不足以证明整篇到齐,截断源必须继续回退ar5iv或PDF.
            if html and "ltx_" in html and self._arxiv_html_is_complete(html):
                # 图 base 用重定向后的最终 URL(官方 html/<id> 302 到 …/<id>v<N>,相对 src 相对它)。
                document_url = validate_scholarly_document_url(provider, final_url or base)
                try:
                    rewritten, snapshot, resources = self._build_scholarly_html_snapshot(
                        html, document_url=document_url, provider=provider,
                    )
                    snapshot_bytes = canonical_snapshot_bytes(snapshot)
                    if len(snapshot_bytes) > SCHOLARLY_HTML_SNAPSHOT_MAX_BYTES:
                        raise ValueError("snapshot JSON exceeds limit")
                except ValueError as exc:
                    self.log.warning(
                        "arxiv_html_snapshot_failed",
                        arxiv_id=arxiv_id,
                        provider=provider,
                        error=f"{type(exc).__name__}:{str(exc)[:240]}",
                    )
                    continue
                self._clear_scholarly_html_snapshot()
                for path, body in resources.items():
                    self.artifacts.write(path, body)
                self.artifacts.write("input/source.html", rewritten)
                self.artifacts.write(SCHOLARLY_HTML_SNAPSHOT_PATH, snapshot_bytes)
                self._arxiv_html_base = document_url
                self.log.info(
                    "arxiv_html_fetched",
                    arxiv_id=arxiv_id,
                    base=document_url,
                    bytes=len(rewritten.encode("utf-8")),
                    resources=len(snapshot["resources"]),
                )
                return
            if html and "ltx_" in html:
                self.log.warning(
                    "arxiv_html_incomplete", arxiv_id=arxiv_id,
                    base=final_url or base, bytes=len(html),
                )
        self._clear_scholarly_html_snapshot()
        self.log.info("arxiv_html_unavailable", arxiv_id=arxiv_id)

    def _fetch_scholarly_html(
        self,
        url: str,
        *,
        provider: str,
    ) -> tuple[str | None, str | None]:
        """逐跳固定论文入口IP;最终URL仍必须属于声明来源。"""
        response = self._fetch_pinned_https(
            url,
            timeout=60,
            max_bytes=SCHOLARLY_HTML_SOURCE_MAX_BYTES,
            allowed_hosts=scholarly_provider_hosts(provider),
        )
        if not self._fetch_succeeded(response):
            return None, response.final_url or url
        final_url = validate_scholarly_document_url(
            provider, response.final_url or url,
        )
        if self._has_pdf_signature(response.body):
            return None, final_url
        return self._decode_fetch_body(response), final_url

    def _clear_scholarly_html_snapshot(self) -> None:
        """先移除隔离工作副本中的旧闭包,失败重抓不得重新签发历史产物。"""
        for relative in ("input/source.html", SCHOLARLY_HTML_SNAPSHOT_PATH):
            path = self.job_dir / relative
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        assets = self.job_dir / SCHOLARLY_HTML_ASSET_PREFIX.rstrip("/")
        if assets.is_symlink() or (assets.exists() and not assets.is_dir()):
            assets.unlink()
        elif assets.is_dir():
            shutil.rmtree(assets)

    @staticmethod
    def _arxiv_html_is_complete(html: str) -> bool:
        """arXiv/ar5iv都返回完整HTML文档;缺闭合尾标记视为200提前断流."""
        return bool(
            re.search(r"</body\s*>\s*</html\s*>\s*$", html, re.I)
        )

    _HTML_TAG_RE = re.compile(r"<(?P<tag>link|style|img|image)\b(?P<body>[^>]*)>", re.I | re.S)
    _HTML_ATTR_RE = re.compile(
        r"(?P<name>[A-Za-z_:][A-Za-z0-9_.:-]*)\s*=\s*(?:"
        r"(?P<quote>['\"])(?P<quoted>.*?)(?P=quote)|"
        r"(?P<unquoted>[^\s\"'=<>`]+))",
        re.S,
    )

    @staticmethod
    def _html_static_references(
        html: str,
    ) -> tuple[list[tuple[str, str]], list[str]]:
        """按文档顺序提取外部CSS、内联CSS和自动加载图片引用。"""
        parser = _StaticHtmlReferenceParser()
        parser.feed(html)
        parser.close()
        return parser.finish()

    @classmethod
    def _rewrite_html_images(cls, html: str, replacements: dict[str, str]) -> str:
        def rewrite_tag(match: re.Match[str]) -> str:
            tag = match.group("tag").casefold()
            if tag not in {"img", "image"}:
                return match.group(0)

            def rewrite_attr(attribute: re.Match[str]) -> str:
                name = attribute.group("name").casefold()
                if name not in {"src", "href", "xlink:href"}:
                    return attribute.group(0)
                value = attribute.group("quoted") or attribute.group("unquoted") or ""
                replacement = replacements.get(value)
                if replacement is None:
                    return attribute.group(0)
                quote = attribute.group("quote")
                if quote:
                    return f"{attribute.group('name')}={quote}{replacement}{quote}"
                return f'{attribute.group("name")}="{replacement}"'

            body = cls._HTML_ATTR_RE.sub(rewrite_attr, match.group("body"))
            return f"<{match.group('tag')}{body}>"

        return cls._HTML_TAG_RE.sub(rewrite_tag, html)

    @staticmethod
    def _response_media_type(response: HttpFetchResult) -> str:
        return response.content_type.partition(";")[0].strip().casefold()

    @staticmethod
    def _resource_identity(body: bytes, media_type: str) -> tuple[str, str]:
        signatures = (
            ("font/woff2", ".woff2", b"wOF2"),
            ("font/woff", ".woff", b"wOFF"),
            ("font/otf", ".otf", b"OTTO"),
            ("font/ttf", ".ttf", b"\x00\x01\x00\x00"),
            ("image/png", ".png", b"\x89PNG\r\n\x1a\n"),
            ("image/jpeg", ".jpg", b"\xff\xd8\xff"),
            ("image/gif", ".gif", b"GIF8"),
            ("image/webp", ".webp", b"RIFF"),
        )
        for detected, suffix, signature in signatures:
            if body.startswith(signature):
                if detected == "image/webp" and body[8:12] != b"WEBP":
                    continue
                return detected, suffix
        if (
            media_type == "image/avif"
            and len(body) >= 12
            and body[4:8] == b"ftyp"
            and body[8:12] in {b"avif", b"avis"}
        ):
            return "image/avif", ".avif"
        raise ValueError("snapshot resource has unsupported signature")

    @staticmethod
    def _snapshot_asset_path(body: bytes, suffix: str, *, image: bool) -> str:
        digest = hashlib.sha256(body).hexdigest()
        kind = "image-" if image else "resource-"
        return f"{SCHOLARLY_HTML_ASSET_PREFIX}{kind}{digest}{suffix}"

    @staticmethod
    def _fetch_pinned_https(
        url: str,
        *,
        timeout: int,
        max_bytes: int,
        allowed_hosts: frozenset[str] | None = None,
    ) -> HttpFetchResult:
        """逐跳固定公网IP抓HTTPS;重定向重新解析,不让连接层二次查DNS。"""
        current_url = url
        for _redirect in range(6):
            current_host = (urlparse(current_url).hostname or "").casefold()
            if allowed_hosts is not None and current_host not in allowed_hosts:
                return HttpFetchResult(
                    b"", current_url, None, "", "provider_host_not_allowed",
                )
            try:
                host, port, addresses = _public_https_target(current_url)
            except ValueError as exc:
                return HttpFetchResult(b"", current_url, None, "", str(exc))
            parsed = urlparse(current_url)
            request_target = parsed.path or "/"
            if parsed.query:
                request_target += "?" + parsed.query
            errors: list[str] = []
            for address in addresses:
                connection = _PinnedHTTPSConnection(
                    host, port, address, timeout=timeout,
                )
                try:
                    connection.request(
                        "GET",
                        request_target,
                        headers={
                            "User-Agent": "Mozilla/5.0",
                            "Accept-Encoding": "identity",
                            "Connection": "close",
                        },
                    )
                    response = connection.getresponse()
                    status = response.status
                    content_type = response.getheader("Content-Type", "")
                    if status in {301, 302, 303, 307, 308}:
                        location = response.getheader("Location")
                        response.close()
                        if not location:
                            return HttpFetchResult(
                                b"", current_url, status, content_type,
                                "redirect_missing_location",
                            )
                        current_url = urljoin(current_url, location)
                        break
                    body = response.read(max_bytes + 1)
                    response.close()
                    if len(body) > max_bytes:
                        return HttpFetchResult(
                            b"", current_url, status, content_type,
                            "response_too_large",
                        )
                    return HttpFetchResult(
                        body, current_url, status, content_type, None,
                    )
                except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
                    errors.append(f"{address}:{type(exc).__name__}:{str(exc)[:120]}")
                finally:
                    connection.close()
            else:
                return HttpFetchResult(
                    b"", current_url, None, "", ";".join(errors) or "connect_failed",
                )
            # 只有 redirect 分支会从地址循环 break,下一轮重新校验并固定新目标。
            continue
        return HttpFetchResult(
            b"", current_url, None, "", "too_many_redirects",
        )

    def _snapshot_fetch(self, url: str, *, max_bytes: int) -> HttpFetchResult:
        response = self._fetch_pinned_https(
            url, timeout=60, max_bytes=max_bytes,
        )
        if not self._fetch_succeeded(response):
            raise ValueError(f"snapshot resource fetch failed: {response.error or response.status_code}")
        if not response.body:
            raise ValueError("snapshot resource is empty")
        return response

    def _build_scholarly_html_snapshot(
        self,
        html: str,
        *,
        document_url: str,
        provider: str,
    ) -> tuple[str, dict, dict[str, bytes]]:
        """在内存中闭合资源后一次发布，避免失败候选留下可渲染半快照。"""
        from urllib.parse import urljoin

        styles, image_refs = self._html_static_references(html)
        blobs: dict[str, bytes] = {}
        records_by_source: dict[str, dict] = {}
        records_by_alias: dict[str, dict] = {}
        stylesheet_paths: list[str] = []
        stylesheet_sources_seen: set[str] = set()
        total_bytes = 0

        def add_record(
            request_url: str,
            source_url: str,
            body: bytes,
            *,
            kind: str,
            media_type: str,
            suffix: str,
        ) -> str:
            nonlocal total_bytes
            existing = records_by_source.get(request_url)
            if existing is not None:
                if (
                    existing["sha256"] != sha256_digest(body)
                    or existing["source_url"] != source_url
                ):
                    raise ValueError("snapshot source URL changed within one fetch")
                return existing["path"]
            if request_url in records_by_alias or source_url in records_by_alias:
                raise ValueError("snapshot URL alias has multiple resource identities")
            total_bytes += len(body)
            if total_bytes > SCHOLARLY_HTML_RESOURCE_TOTAL_MAX_BYTES:
                raise ValueError("snapshot resources exceed total byte limit")
            if len(records_by_source) >= SCHOLARLY_HTML_MAX_RESOURCES:
                raise ValueError("snapshot resource count exceeds limit")
            identity_body = request_url.encode("utf-8") + b"\0" + body
            path = self._snapshot_asset_path(identity_body, suffix, image=kind == "image")
            previous = blobs.get(path)
            if previous is not None and previous != body:
                raise ValueError("snapshot content-addressed path collision")
            blobs[path] = body
            records_by_source[request_url] = {
                "kind": kind,
                "path": path,
                "request_url": request_url,
                "source_url": source_url,
                "sha256": sha256_digest(body),
                "size_bytes": len(body),
                "media_type": media_type,
            }
            records_by_alias[request_url] = records_by_source[request_url]
            records_by_alias[source_url] = records_by_source[request_url]
            return path

        def fetch_binary(source_url: str) -> str:
            response = self._snapshot_fetch(
                source_url, max_bytes=SCHOLARLY_HTML_RESOURCE_MAX_BYTES,
            )
            media_type, suffix = self._resource_identity(
                response.body, self._response_media_type(response),
            )
            kind = "image" if media_type.startswith("image/") else "font"
            return add_record(
                source_url,
                response.final_url or source_url,
                response.body,
                kind=kind,
                media_type=media_type,
                suffix=suffix,
            )

        def visit_stylesheet(source_url: str, *, inline_body: bytes | None = None) -> None:
            if source_url in stylesheet_sources_seen:
                return
            if len(stylesheet_sources_seen) >= SCHOLARLY_HTML_MAX_STYLESHEETS:
                raise ValueError("snapshot stylesheet count exceeds limit")
            stylesheet_sources_seen.add(source_url)
            if inline_body is None:
                response = self._snapshot_fetch(source_url, max_bytes=SCHOLARLY_HTML_CSS_MAX_BYTES)
                media_type = self._response_media_type(response)
                if media_type and media_type != "text/css":
                    raise ValueError("snapshot stylesheet MIME mismatch")
                body = response.body
                canonical_source = response.final_url or source_url
            else:
                body = inline_body
                canonical_source = source_url
            imports, dependencies = iter_css_urls(body)
            for imported in imports:
                if imported.startswith(("data:", "#")):
                    raise ValueError("snapshot stylesheet import is not fetchable")
                visit_stylesheet(urljoin(canonical_source, imported))
            for dependency in dependencies:
                if dependency.startswith("#"):
                    continue
                if dependency.startswith("data:"):
                    raise ValueError("snapshot stylesheet data resource is not allowed")
                fetch_binary(urljoin(canonical_source, dependency))
            path = add_record(
                source_url,
                canonical_source,
                body,
                kind="stylesheet",
                media_type="text/css",
                suffix=".css",
            )
            stylesheet_paths.append(path)

        inline_index = 0
        for kind, value in styles:
            if kind == "link":
                if value.startswith("data:"):
                    raise ValueError("snapshot linked stylesheet data URL is not allowed")
                visit_stylesheet(urljoin(document_url, value))
                continue
            inline_index += 1
            pseudo_url = f"{document_url}#flori-inline-style-{inline_index}"
            visit_stylesheet(pseudo_url, inline_body=value.encode("utf-8"))

        image_replacements: dict[str, str] = {}
        for image_ref in image_refs:
            if image_ref.startswith("data:"):
                continue
            absolute = urljoin(document_url, image_ref)
            response = self._snapshot_fetch(
                absolute, max_bytes=SCHOLARLY_HTML_RESOURCE_MAX_BYTES,
            )
            media_type, suffix = self._resource_identity(
                response.body, self._response_media_type(response),
            )
            if not media_type.startswith("image/"):
                raise ValueError("snapshot HTML resource is not an image")
            path = add_record(
                absolute,
                response.final_url or absolute,
                response.body,
                kind="image",
                media_type=media_type,
                suffix=suffix,
            )
            image_replacements[image_ref] = path
        rewritten = self._rewrite_html_images(html, image_replacements)
        rewritten_bytes = rewritten.encode("utf-8")
        snapshot = {
            "format": SCHOLARLY_HTML_SNAPSHOT_FORMAT,
            "format_version": SCHOLARLY_HTML_SNAPSHOT_VERSION,
            "job_id": self.job_dir.name,
            "provider": provider,
            "document_url": document_url,
            "html": {
                "path": "input/source.html",
                "sha256": sha256_digest(rewritten_bytes),
                "size_bytes": len(rewritten_bytes),
                "media_type": "text/html",
            },
            "stylesheets": stylesheet_paths,
            "resources": sorted(records_by_source.values(), key=lambda item: item["path"].encode("utf-8")),
        }
        validate_scholarly_html_snapshot(
            snapshot, expected_job_id=self.job_dir.name, source_html=rewritten_bytes,
        )
        return rewritten, snapshot, blobs

    def _fetch_arxiv_meta(self, arxiv_id: str) -> None:
        """arxiv API 取权威元数据 → stash self._arxiv_meta(由 _extract_metadata 并入 metadata.json)。
        标准库 ElementTree 解析 Atom,零运行时依赖(曾用 feedparser,worker 镜像没装它,宽 except 把
        ModuleNotFoundError 当网络错误静默吞 → 所有 arxiv 论文标题/作者丢失、UI 显示 job_id)。
        best-effort 只兜【网络/坏响应】(curl 失败/超时/坏 XML → 回退 PDF 启发);编程错误照常上抛。"""
        import xml.etree.ElementTree as ET

        from shared.step_subprocess import SubprocessFailed

        api = f"http://export.arxiv.org/api/query?id_list={arxiv_id}"
        try:
            r = self.commands.run(["curl", "-fsSL", api], timeout=30)
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
        """非 arxiv 的直链 PDF 写入 input/source.pdf,供 Document parser 消费。"""
        from shared.net import assert_public_url

        assert_public_url(url)   # 抓取前挡内网/回环目标(SSRF),与 _download_article 一致
        input_dir = self.job_dir / "input"
        input_dir.mkdir(parents=True, exist_ok=True)
        cmd = ["curl", "-fSL", "-A", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36", "-o", str(input_dir / "source.pdf"), url]  # 同上:浏览器 UA
        self.commands.run(cmd, timeout=120)

    def _download_article(self, url: str, document_kind: str | None = None) -> None:
        """抓取 HTML;研究论文优先跟随明确全文 PDF,失败保留可操作诊断."""
        from shared.errors import InputInvalidError
        from shared.net import assert_public_url

        assert_public_url(url)
        input_dir = self.job_dir / "input"
        input_dir.mkdir(parents=True, exist_ok=True)
        kind = document_kind or "article"
        alternative = (
            resolve_document_source_alternative(url, document_kind=kind)
            if kind == "research_paper" else None
        )

        response = self._fetch_primary_document(url)
        challenge = detect_access_challenge(response.body)
        if not self._fetch_succeeded(response) or challenge:
            use_alternative = alternative is not None and (
                challenge or self._is_deterministic_http_failure(response)
            )
            if use_alternative:
                self._download_resolved_pdf(
                    original_url=url,
                    original=response,
                    candidate_url=alternative.resolved_url,
                    strategy="configured_alternative",
                    min_pages=alternative.min_pages,
                    challenge=challenge,
                    alternative=alternative,
                )
                return
            resolver = (
                "alternative_skipped_transient"
                if alternative is not None else "no_alternative"
            )
            raise InputInvalidError(self._fetch_failure_message(
                original_url=url,
                response=response,
                challenge=challenge,
                resolver=resolver,
            ))

        assert_public_url(response.final_url)
        if self._has_pdf_signature(response.body):
            self._store_resolved_pdf(
                original_url=url,
                original=response,
                resolved=response,
                strategy="direct_pdf",
                min_pages=2 if kind == "research_paper" else 1,
            )
            return

        html = self._decode_fetch_body(response)
        source_meta = self._extract_article_source_meta(
            html, source_url=url, final_url=response.final_url,
        )
        if kind == "research_paper":
            citation_pdf = extract_research_pdf_url(html, response.final_url)
            if citation_pdf:
                try:
                    self._download_resolved_pdf(
                        original_url=url,
                        original=response,
                        candidate_url=citation_pdf,
                        strategy="citation_pdf",
                        min_pages=2,
                        source_meta=source_meta,
                    )
                    return
                except InputInvalidError:
                    if alternative is None or alternative.resolved_url == citation_pdf:
                        raise
                    self._download_resolved_pdf(
                        original_url=url,
                        original=response,
                        candidate_url=alternative.resolved_url,
                        strategy="configured_alternative",
                        min_pages=alternative.min_pages,
                        alternative=alternative,
                        source_meta=source_meta,
                    )
                    return

        self.artifacts.write("input/source.html", html)
        (input_dir / "source.pdf").unlink(missing_ok=True)
        self._document_source_meta = source_meta

    def _fetch_primary_document(self, url: str) -> HttpFetchResult:
        """网络/5xx 保留原退避,确定性 4xx 或 challenge 首次即返回决策."""
        response = HttpFetchResult(b"", url, None, "", "not_started")
        timeout = 30
        for _ in range(5):
            response = self._fetch_response(url, timeout)
            challenge = detect_access_challenge(response.body)
            if self._fetch_succeeded(response) or challenge:
                return response
            if self._is_deterministic_http_failure(response):
                return response
            timeout *= 2
        return response

    @staticmethod
    def _fetch_succeeded(response: HttpFetchResult) -> bool:
        return bool(
            response.status_code is not None
            and 200 <= response.status_code < 300
            and response.body
            and response.error is None
        )

    @staticmethod
    def _is_deterministic_http_failure(response: HttpFetchResult) -> bool:
        status = response.status_code
        return bool(
            response.error == "response_too_large"
            or (
                status is not None
                and (
                    300 <= status < 400
                    or (400 <= status < 500 and status not in {408, 425, 429})
                )
            )
        )

    def _download_resolved_pdf(
        self,
        *,
        original_url: str,
        original: HttpFetchResult,
        candidate_url: str,
        strategy: str,
        min_pages: int,
        challenge: str | None = None,
        alternative: DocumentSourceAlternative | None = None,
        source_meta: dict | None = None,
    ) -> None:
        from shared.errors import InputInvalidError
        from shared.net import assert_public_url

        assert_public_url(candidate_url)
        resolved = self._fetch_primary_document(candidate_url)
        resolved_challenge = detect_access_challenge(resolved.body)
        if not self._fetch_succeeded(resolved) or resolved_challenge:
            raise InputInvalidError(self._fetch_failure_message(
                original_url=original_url,
                response=resolved,
                challenge=resolved_challenge,
                resolver=strategy,
            ))
        assert_public_url(resolved.final_url)
        self._store_resolved_pdf(
            original_url=original_url,
            original=original,
            resolved=resolved,
            strategy=strategy,
            min_pages=min_pages,
            challenge=challenge,
            alternative=alternative,
            source_meta=source_meta,
        )

    def _store_resolved_pdf(
        self,
        *,
        original_url: str,
        original: HttpFetchResult,
        resolved: HttpFetchResult,
        strategy: str,
        min_pages: int,
        challenge: str | None = None,
        alternative: DocumentSourceAlternative | None = None,
        source_meta: dict | None = None,
    ) -> None:
        from shared.errors import InputInvalidError

        content_type = resolved.content_type.split(";", 1)[0].strip().lower()
        if not self._has_pdf_signature(resolved.body):
            raise InputInvalidError(
                "resolved research source is not PDF: "
                f"original_url={original_url} final_url={resolved.final_url} "
                f"http_status={resolved.status_code} resolver={strategy} "
                f"content_type={resolved.content_type or '(missing)'} pdf_signature=invalid"
            )
        if "pdf" not in content_type and content_type not in {
            "application/octet-stream", "binary/octet-stream",
        }:
            raise InputInvalidError(
                "resolved research source has invalid MIME: "
                f"original_url={original_url} final_url={resolved.final_url} "
                f"http_status={resolved.status_code} resolver={strategy} "
                f"content_type={resolved.content_type or '(missing)'} pdf_signature=valid"
            )
        if len(resolved.body) < 1024:
            raise InputInvalidError(
                "resolved research source is too small: "
                f"original_url={original_url} final_url={resolved.final_url} "
                f"resolver={strategy} bytes={len(resolved.body)}"
            )

        input_dir = self.job_dir / "input"
        part = input_dir / ".source.pdf.part"
        target = input_dir / "source.pdf"
        part.write_bytes(resolved.body)
        try:
            page_count = self._pdf_page_count(part)
        except InputInvalidError as exc:
            part.unlink(missing_ok=True)
            raise InputInvalidError(
                "resolved research PDF validation failed: "
                f"original_url={original_url} final_url={resolved.final_url} "
                f"http_status={resolved.status_code} resolver={strategy} "
                f"content_type={resolved.content_type or '(missing)'} detail={exc}"
            ) from exc
        if page_count < min_pages:
            part.unlink(missing_ok=True)
            raise InputInvalidError(
                "resolved research source is not full text: "
                f"original_url={original_url} final_url={resolved.final_url} "
                f"resolver={strategy} pdf_pages={page_count} min_pages={min_pages}"
            )
        try:
            part.replace(target)
            (input_dir / "source.html").unlink(missing_ok=True)
        except Exception:
            part.unlink(missing_ok=True)
            raise

        resolution: dict = {
            "strategy": strategy,
            "original_url": original_url,
            "original_status": original.status_code,
            "original_content_type": original.content_type,
            "resolved_url": resolved.final_url,
            "resolved_status": resolved.status_code,
            "resolved_content_type": resolved.content_type,
            "pdf_pages": page_count,
        }
        if challenge:
            resolution["challenge"] = challenge
        if alternative is not None:
            resolution["alternative_reason"] = alternative.reason
        self._document_source_meta = {
            **(source_meta or {}),
            "source_url": original_url,
            "final_url": resolved.final_url,
            "source_resolution": resolution,
        }

    def _pdf_page_count(self, path: Path) -> int:
        """pdfinfo 验证 PDF 可解析并返回页数,损坏文件确定性拒绝."""
        from shared.errors import InputInvalidError

        try:
            result = self.commands.run(["pdfinfo", str(path)], timeout=30)
        except Exception as exc:
            raise InputInvalidError("resolved research PDF cannot be parsed by pdfinfo") from exc
        match = re.search(r"^Pages:\s+(\d+)\s*$", result.stdout or "", flags=re.MULTILINE)
        if not match:
            raise InputInvalidError("resolved research PDF page count is unavailable")
        return int(match.group(1))

    @staticmethod
    def _has_pdf_signature(body: bytes) -> bool:
        return body[:1024].lstrip().startswith(b"%PDF-")

    @staticmethod
    def _fetch_failure_message(
        *,
        original_url: str,
        response: HttpFetchResult,
        challenge: str | None,
        resolver: str,
    ) -> str:
        return (
            "document source fetch failed: "
            f"original_url={original_url} final_url={response.final_url or original_url} "
            f"http_status={response.status_code if response.status_code is not None else 'none'} "
            f"content_type={response.content_type or '(missing)'} "
            f"challenge={challenge or 'none'} resolver={resolver} "
            f"transport={response.error or 'none'}"
        )

    @staticmethod
    def _extract_article_source_meta(
        html: str, *, source_url: str, final_url: str,
    ) -> dict:
        import trafilatura

        source_meta: dict = {"source_url": source_url, "final_url": final_url}
        try:
            meta = trafilatura.extract_metadata(html)
            if meta:
                source_meta["title"] = meta.title or ""
                source_meta["author"] = meta.author or ""
                source_meta["sitename"] = meta.sitename or ""
                source_meta["published_at"] = meta.date or ""
        except Exception:
            pass
        return source_meta

    @staticmethod
    def _fetch_response(
        url: str, timeout: int, *, max_bytes: int = 64 * 1024 * 1024,
    ) -> HttpFetchResult:
        """urllib 抓取有界文档,保留状态,MIME,最终 URL 和 challenge 体."""
        import http.client
        import urllib.error

        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        opener = urllib.request.build_opener(_PublicUrlRedirectHandler())
        try:
            with opener.open(req, timeout=timeout) as response:
                body = response.read(max_bytes + 1)
                content_type = response.headers.get("Content-Type", "")
                final_url = response.geturl()
                status = getattr(response, "status", None) or response.getcode()
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read(512_001)
            except OSError:
                body = b""
            return HttpFetchResult(
                body=body,
                final_url=exc.geturl() or url,
                status_code=exc.code,
                content_type=exc.headers.get("Content-Type", "") if exc.headers else "",
                error=f"HTTPError:{exc.code}",
            )
        except (
            urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException,
        ) as exc:
            return HttpFetchResult(
                body=b"",
                final_url=url,
                status_code=None,
                content_type="",
                error=f"{type(exc).__name__}:{str(exc)[:160]}",
            )
        if len(body) > max_bytes:
            return HttpFetchResult(
                body=body[:max_bytes],
                final_url=final_url,
                status_code=status,
                content_type=content_type,
                error="response_too_large",
            )
        return HttpFetchResult(body, final_url, status, content_type, None)

    @staticmethod
    def _decode_fetch_body(response: HttpFetchResult) -> str:
        match = re.search(r"charset\s*=\s*['\"]?([^;'\"\s]+)", response.content_type, re.I)
        declared = match.group(1) if match else None
        for encoding in filter(None, (declared, "utf-8", "gb18030")):
            try:
                return response.body.decode(encoding)
            except (UnicodeDecodeError, LookupError):
                continue
        return response.body.decode("utf-8", errors="replace")

    @staticmethod
    def _fetch_html(
        url: str, timeout: int, *, max_bytes: int = 64 * 1024 * 1024,
    ) -> tuple[str | None, str | None]:
        """HTML 来源的 best-effort 兼容入口，论文 resolver 使用完整响应。"""
        response = DownloadStep._fetch_response(url, timeout, max_bytes=max_bytes)
        if not DownloadStep._fetch_succeeded(response):
            return None, None
        if DownloadStep._has_pdf_signature(response.body):
            return None, response.final_url
        return DownloadStep._decode_fetch_body(response), response.final_url

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
        self.commands.run(cmd, timeout=self.config["step"]["timeout_sec"])

    def _resolve_audio_from_page(self, page_url: str) -> str | None:
        """抓播客页面 HTML,解析出音频直链(og:audio/<audio>/<enclosure>/<a *.mp3>)。
        best-effort:网络/解析失败返回 None,由调用方按原 URL 继续(再失败则校验拦下)。"""
        from shared.source_detect import extract_audio_enclosure
        try:
            r = self.commands.run(
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
        for ext in (".mp3", ".m4a", ".wav", ".aac", ".flac"):
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
            "video": "source.mp4",
            "document": f"source{ext or '.html'}",
            "audio": f"source{ext or '.mp3'}",
        }
        dest = input_dir / name_by_type.get(content_type, f"source{ext}")
        shutil.copyfile(src, dest)

        if content_type == "video":
            self._verify_download(dest)

    @staticmethod
    def _normalize_document_input(input_dir: Path) -> None:
        """把文本型文档规范成 HTML；PDF 保持不可变原文件。"""
        target = input_dir / "source.html"
        if target.exists():
            return
        htm = input_dir / "source.htm"
        if htm.exists():
            htm.rename(target)
            return
        for ext in (".txt", ".md"):
            source = input_dir / f"source{ext}"
            if not source.exists():
                continue
            from html import escape

            body = escape(source.read_text(encoding="utf-8"))
            target.write_text(f"<html><body><pre>{body}</pre></body></html>", encoding="utf-8")
            source.unlink()
            return

    _normalize_article_input = _normalize_document_input

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
        self.commands.run(cmd, timeout=self.config["step"]["timeout_sec"])
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
        """把下载或上传的视频容器规范成下游唯一入口 source.mp4。"""
        for f in input_dir.glob("source.*"):
            if f.suffix.lower() in (".mp4", ".mkv", ".webm", ".flv", ".mov"):
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
        for ext in (".mp3", ".m4a", ".wav", ".aac", ".flac"):
            audio_file = input_dir / f"source{ext}"
            if audio_file.exists():
                metadata["duration_sec"] = self._get_video_duration(audio_file)
                _set_size(audio_file)
                break

        metadata["has_subtitle"] = any(input_dir.glob("*.srt"))
        metadata["has_danmaku"] = any(input_dir.glob("*.ass"))
        # 并入 arxiv API 元数据(title/authors/abstract/published_at),作权威来源,优先于 PDF 启发。
        metadata.update(getattr(self, "_arxiv_meta", {}) or {})
        metadata.update(getattr(self, "_document_source_meta", {}) or {})
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
