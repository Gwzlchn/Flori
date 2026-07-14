"""Step 02: 文章解析。trafilatura 抽正文 + 元数据,补全元信息(abstract/image/tags/author),
并产出可读原文 Markdown(output/original.md,正文图片下载到 assets/ 本地引用)。

原文 MD 走 readability(定位正文,返回原始 HTML 无损子树)+ markdownify(忠实转 MD):
trafilatura 的树会丢 <pre> 换行(代码块被拍扁救不回),只用于 parsed.json 正文/元数据与回退链。
图片按 md 引用驱动本地化:相对 src 先 urljoin 文章 URL(Hugo 等站点图全相对),失败保绝对 URL。
站点差异(作者位置等)仍外置 extractors/ 注册表,由 pick_extractor 选中的 extractor 提供。
"""

from __future__ import annotations

import json
import re

from steps.article.extractors import pick_extractor
from shared.step_base import StepBase, file_hash
from steps.article.provenance import (
    build_html_source_manifest,
    direct_text_provenance_candidates,
    persist_note_provenance,
    publish_source_manifest,
)


# 空正文护栏。付费墙 / JS 渲染 / 订阅残桩页常被 trafilatura 抽出空或极短正文(仅标题 + "订阅后阅读")。
# 正文有效字符数低于此阈值判定为抓取失败/付费墙,直接 InputInvalidError(不重试)。
# 不让 03/04/05 在空正文上跑 AI 幻觉、污染概念图谱:key_terms 是图谱唯一概念来源。
# 阈值与本文件翻译门控共用 200(needs_translation 用 len(text) > 200),不另立魔数。
# 中英通用:200 拉丁约 35 词、200 CJK 约一短段,都是低于此基本无内容可做笔记的地板;
# 对英文更宽松(需更少词),倾向不误杀。正规文章普遍远超此值。
MIN_BODY_CHARS = 200

# 常见付费墙/登录墙标记(EN + 中文)。命中仅用于细化错误信息:疑似付费墙 vs 疑似抓取失败。
# 判废仍只看正文长度,避免长文因正文含 subscribe/会员 等词被误杀(长文不会触发长度门)。
_PAYWALL_MARKERS = (
    "subscribe to continue", "subscribe to read", "subscribe to keep reading",
    "already a subscriber", "already a member", "become a member",
    "create a free account", "create an account to", "sign in to read",
    "sign in to continue", "this content is for", "members only",
    "for subscribers only", "subscribers only", "paywall", "metered",
    "登录后查看", "登录后阅读", "登录以阅读", "订阅后阅读", "订阅以继续",
    "开通会员", "成为会员", "付费内容", "仅限会员", "购买后阅读", "请先登录",
    "继续阅读", "阅读全文需",
)


class ParseArticleStep(StepBase):
    def validate_inputs(self) -> list[str]:
        if not (self.job_dir / "input" / "source.html").exists():
            return ["input/source.html"]
        return []

    def input_hashes(self) -> dict[str, str]:
        return {"html": file_hash(self.job_dir / "input" / "source.html")}

    def execute(self) -> dict | None:
        import trafilatura

        (self.job_dir / "intermediate" / "source_segments.json").unlink(missing_ok=True)
        (self.job_dir / "output" / "provenance" / "original.json").unlink(missing_ok=True)
        html = (self.job_dir / "input" / "source.html").read_text(encoding="utf-8")

        extracted = trafilatura.extract(
            html, output_format="json", with_metadata=True,
            include_comments=False, include_tables=True,
        )

        title = authors_src = date = text = ""
        abstract = image = sitename_src = ""
        tags: list[str] = []
        authors: list[str] = []
        if extracted:
            data = json.loads(extracted)
            title = (data.get("title") or "").strip()
            text = (data.get("text") or "").strip()
            date = (data.get("date") or "").strip()
            abstract = (data.get("description") or data.get("excerpt") or "").strip()
            image = (data.get("image") or "").strip()
            sitename_src = (data.get("sitename") or data.get("hostname") or "").strip()  # 来源:网站名
            # 站点通用占位图(如 wscn 的 _static/share.png、logo、default)不是文章实际配图,丢弃。
            if image and re.search(r'(share|/_static/|/static/|logo|default|placeholder)', image.lower()):
                image = ""
            tags = self._split_tags(data.get("tags") or data.get("categories"))
            authors_src = data.get("author") or ""
            if authors_src:
                authors = [a.strip() for a in str(authors_src).split(";") if a.strip()]

        if not text:
            text = (trafilatura.extract(html, include_comments=False) or "").strip()

        meta = self._load_meta()
        url = meta.get("url", "")
        # 站点提取器(按 URL + 页面特征选;否则通用兜底)。差异:作者兜底 + 正文图片提取。
        extractor = pick_extractor(url, html)

        title = title or (meta.get("title") or "").strip()
        date = date or (meta.get("date") or "").strip()
        if not authors and meta.get("author"):
            authors = [a.strip() for a in str(meta["author"]).split(";") if a.strip()]
        # author 仍空 → 交给 extractor 兜底(JSON-LD / 页面内嵌 JSON 的 author 对象)。
        if not authors:
            authors = extractor.authors(html)

        # 空正文护栏:正文有效字符数过短(付费墙/JS 渲染/订阅残桩)直接判失败,不重试、
        # 不写任何产物,不让 03/04/05 拿空正文喂 AI 幻觉污染概念图谱。详见文件顶部说明。
        eff = self._effective_len(text)
        if eff < MIN_BODY_CHARS:
            from shared.errors import InputInvalidError
            hint = ("疑似付费墙/登录墙" if self._has_paywall_marker(text)
                    else "疑似抓取失败/空正文(JS 渲染或残桩页)")
            raise InputInvalidError(
                f"正文过短({eff} 有效字符 < {MIN_BODY_CHARS}),{hint};"
                f"title={title[:60]!r} url={url}"
            )

        sections = []
        if text:
            sections.append({"level": 1, "title": title or "正文", "page": 1, "text": text})

        lang = self._detect_lang(text)                 # 翻译触发用:非中文(en 等)才译

        parsed = {
            "title": title,
            "authors": authors,
            "abstract": abstract,
            "image": image,                            # 封面/配图
            "tags": tags,                              # 标签/分类
            "url": url,
            # 来源网站名:trafilatura sitename > 下载元数据 > URL 域名(去 www)。供「来源」展示。
            "sitename": sitename_src or meta.get("sitename", "") or self._domain(url),
            "date": date,
            "lang": lang,                              # 正文主语言(zh / non-zh)
            "word_count": len(text),
            "sections": sections,
            "text": text,
        }
        self.artifacts.write("intermediate/parsed.json", parsed)

        # 非中文正文 → 写翻译标记,04_translate_article 经 rules:exists 门控触发(中文文章不译)。
        if lang != "zh" and len(text) > 200:
            self.artifacts.write("intermediate/needs_translation.json", {"lang": lang})

        # 可读原文 Markdown(图片下载到 assets/ 改本地引用),供前端「原文」tab。
        md, img_count = self._original_markdown(html, parsed, extractor)
        self.artifacts.write("output/original.md", md)
        source_manifest = publish_source_manifest(
            self.job_dir,
            build_html_source_manifest(
                self.job_dir, pipeline="article",
            ),
        )
        provenance = {"status": "legacy_no_source_manifest", "segments": 0}
        if source_manifest is not None:
            provenance = persist_note_provenance(
                self.job_dir,
                pipeline="article",
                note_type="original",
                note_artifact="output/original.md",
                candidates=direct_text_provenance_candidates(
                    source_manifest, md, section="original",
                ),
            )

        return {"chars": len(text), "title": title, "images": img_count,
                "abstract": bool(abstract), "tags": len(tags),
                "lang": lang, "extractor": extractor.name,
                "provenance_segments": provenance["segments"],
                "provenance_status": provenance["status"]}

    @staticmethod
    def _effective_len(text: str) -> int:
        """正文有效字符数 = 去掉所有空白后的字符数(中英通用:CJK/拉丁/数字均按字符计)。
        空正文护栏的判据——避免空白填充的残桩页被 len() 误判为长。"""
        return len("".join((text or "").split()))

    @classmethod
    def _has_paywall_marker(cls, text: str) -> bool:
        """正文(teaser)是否命中常见付费墙/登录墙标记。仅用于细化空正文护栏的错误信息,
        不参与判废(判废只看 _effective_len)。"""
        low = (text or "").lower()
        return any(m in low for m in _PAYWALL_MARKERS)

    @staticmethod
    def _detect_lang(text: str) -> str:
        """正文主语言粗判(委托 steps.utils.lang,与论文共用同一判据)。"""
        from steps.utils.lang import detect_lang
        return detect_lang(text)

    @staticmethod
    def _domain(url: str) -> str:
        """URL 域名(去 www):无 sitename 时作「来源」兜底。"""
        from urllib.parse import urlparse
        try:
            host = (urlparse(url or "").hostname or "").lower()
        except Exception:
            host = ""
        return host[4:] if host.startswith("www.") else host

    # helpers

    @staticmethod
    def _split_tags(raw) -> list[str]:
        if not raw:
            return []
        if isinstance(raw, list):
            return [str(t).strip() for t in raw if str(t).strip()]
        return [t.strip() for t in re.split(r"[;,，、]", str(raw)) if t.strip()]

    # 原文 Markdown:readability 定位正文 → markdownify 忠实转 MD → 图片按 md 引用本地化。

    def _original_markdown(self, html: str, parsed: dict, extractor) -> tuple[str, int]:
        """readability 抽正文(原始 HTML 无损子树,<pre>/<img> 原位保留)→ markdownify 转 MD。
        关 _ * 转义:LaTeX($x_i$)与代码常含下划线/星号,转义会毁公式。失败走回退链。
        图片找回:readability 会把深嵌套正文图连壳剔掉(substack 的 figure/captioned 容器),
        用站点感知的 extractor.content_image_urls 找回缺失图,按 HTML 锚点插回原位。"""
        md = ""
        try:
            from markdownify import markdownify as mdify
            from readability import Document
            content_html = Document(html).summary(html_partial=True)
            md = mdify(content_html, heading_style="ATX",
                       escape_underscores=False, escape_asterisks=False).strip()
        except Exception:
            self.log.warning("original_md_readability_failed", exc_info=True)
        if not md:
            md = self._fallback_markdown(html, parsed)
        title = parsed.get("title", "")
        # 标题:readability/trafilatura 的 md 常不带 H1,补上
        if title and not md.lstrip().startswith("# "):
            md = f"# {title}\n\n{md}"
        recovered = [u for u in extractor.content_image_urls(html) if u not in md]
        if recovered:
            items = [(self._image_anchor(html, u), f"![]({u})") for u in recovered]
            md = self._inline_images(md, items)
        return self._localize_images(md, parsed.get("url", ""))

    @staticmethod
    def _image_anchor(html: str, url: str) -> str:
        """图 url 在 HTML 中首次出现位置之前、最近的段落/标题文字(归一化),作为它在正文里的锚点。
        无则返回空串,兜底插标题后。"""
        idx = html.find(url)
        if idx < 0:
            return ""
        before = html[:idx]
        last = ""
        for m in re.finditer(r'<(p|h[1-6]|figcaption)\b[^>]*>(.*?)</\1>', before, re.S | re.I):
            txt = " ".join(re.sub(r'<[^>]+>', " ", m.group(2)).split())
            if len(txt) >= 12:           # 够长才作锚(跳过空/超短块)
                last = txt
        return last

    @staticmethod
    def _inline_images(md: str, items: list[tuple[str, str]]) -> str:
        """把找回的图按锚点插到 md 对应段落之后。图按文档序,锚点在 md 里也按序出现,故单调推进搜索。
        锚点为空或在 md 找不到的图,收集后兜底插在标题(首个 # 行)之后。"""
        lines = md.split("\n")
        norm = [" ".join(re.sub(r'<[^>]+>', " ", ln).split()) for ln in lines]
        after: dict[int, list[str]] = {}     # 行号 -> 该行后要插的图引用
        leftover: list[str] = []
        cursor = 0
        for anchor, ref in items:
            key = anchor[-40:].strip()
            placed = False
            if key:
                for i in range(cursor, len(lines)):
                    if key in norm[i]:
                        after.setdefault(i, []).append(ref)
                        cursor = i            # 后续图从此行起找,保序;同段连续图落同一行
                        placed = True
                        break
            if not placed:
                leftover.append(ref)
        out: list[str] = []
        title_idx = next((i for i, ln in enumerate(lines) if ln.startswith("# ")), -1)
        for i, ln in enumerate(lines):
            out.append(ln)
            if i == title_idx and leftover:   # 锚点匹配不上的兜底插标题后
                for ref in leftover:
                    out.append("")
                    out.append(ref)
                leftover = []
            for ref in after.get(i, []):
                out.append("")
                out.append(ref)
        if leftover:                          # 无标题行 → 顶部兜底
            head = [x for ref in leftover for x in (ref, "")]
            out = head + out
        return "\n".join(out)

    def _fallback_markdown(self, html: str, parsed: dict) -> str:
        """回退链:trafilatura markdown(代码块会被拍扁,聊胜于无)→ 纯正文文本。"""
        import trafilatura
        try:
            md = trafilatura.extract(
                html, output_format="markdown", include_comments=False, include_tables=True,
            ) or ""
        except Exception:
            md = ""
        return md or parsed.get("text", "")

    _IMG_REF = re.compile(r'!\[([^\]]*)\]\(\s*([^)\s]+)((?:\s+"[^"]*")?)\s*\)')

    def _localize_images(self, md: str, page_url: str) -> tuple[str, int]:
        """md 里的图片引用本地化:相对 src 先 urljoin(page_url) 绝对化(Hugo 等站图全相对,
        不解析则全部下载失败);下载成功改写为 assets/img_NN.ext,失败保留绝对 URL(前端在线
        仍可渲染,不静默丢图)。data: URI 原样跳过;同图多次引用只下载一次。
        并行下载(5 并发):图多的页(lilianweng 26 图)串行会顶爆步超时。返回 (md, 本地化数)。"""
        from concurrent.futures import ThreadPoolExecutor
        from urllib.parse import urljoin

        srcs: list[str] = []
        for _alt, src, _title in self._IMG_REF.findall(md):
            s = src.strip()
            if s and not s.startswith("data:") and s not in srcs:
                srcs.append(s)
        if not srcs:
            return md, 0

        absolute = {s: (urljoin(page_url, s) if page_url else s) for s in srcs}
        to_fetch = [s for s in srcs if absolute[s].startswith(("http://", "https://"))]
        fetched: dict[str, bytes | None] = {}
        if to_fetch:
            with ThreadPoolExecutor(max_workers=5) as pool:
                for s, data in zip(to_fetch,
                                   pool.map(lambda s: self._fetch_image(absolute[s]), to_fetch)):
                    fetched[s] = data

        assets = self.job_dir / "assets"
        mapping: dict[str, str] = {}
        count = 0
        for idx, s in enumerate(srcs):
            data = fetched.get(s)
            if data:
                ext_m = re.search(r'\.(png|jpe?g|gif|webp|svg)(?:[?#]|$)', absolute[s].lower())
                ext = ext_m.group(1) if ext_m else "jpg"
                ext = "jpg" if ext == "jpeg" else ext
                fname = f"img_{idx:02d}.{ext}"
                assets.mkdir(parents=True, exist_ok=True)
                (assets / fname).write_bytes(data)
                mapping[s] = f"assets/{fname}"
                count += 1
            else:
                mapping[s] = absolute[s]

        def _replace(m: re.Match) -> str:
            alt, src, title_part = m.group(1), m.group(2).strip(), m.group(3)
            new_ref = mapping.get(src)
            if new_ref is None:
                return m.group(0)
            return f"![{alt}]({new_ref}{title_part})"

        return self._IMG_REF.sub(_replace, md), count

    @staticmethod
    def _fetch_image(url: str) -> bytes | None:
        """下载单图,失败返 None(调用方保留绝对 URL 引用)。"""
        import urllib.request
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            return urllib.request.urlopen(req, timeout=15).read() or None
        except Exception:
            return None

    def _load_meta(self) -> dict:
        path = self.job_dir / "input" / "article_meta.json"
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return {}
        return {}


if __name__ == "__main__":
    ParseArticleStep.cli_main("02_parse_article")
