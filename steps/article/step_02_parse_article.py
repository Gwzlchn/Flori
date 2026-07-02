"""Step 02: 文章解析。trafilatura 抽正文 + 元数据,补全元信息(abstract/image/tags/author),
并产出可读原文 Markdown(output/original.md,正文图片下载到 assets/ 本地引用)。

站点差异(正文图片标记、作者位置)外置到 extractors/ 注册表:本 step 只做通用编排
(trafilatura 出正文/元数据 + 下载图片 + 拼 markdown),站点定制由 pick_extractor 选中的 extractor 提供。
"""

from __future__ import annotations

import json
import re

from steps.article.extractors import pick_extractor
from shared.step_base import StepBase, file_hash


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
        self.write_output("intermediate/parsed.json", parsed)

        # 非中文正文 → 写翻译标记,04_translate_article 经 rules:exists 门控触发(中文文章不译)。
        if lang != "zh" and len(text) > 200:
            self.write_output("intermediate/needs_translation.json", {"lang": lang})

        # 可读原文 Markdown(图片下载到 assets/ 改本地引用),供前端「原文」tab。
        md, img_count = self._original_markdown(html, parsed, extractor)
        self.write_output("output/original.md", md)

        return {"chars": len(text), "title": title, "images": img_count,
                "abstract": bool(abstract), "tags": len(tags),
                "lang": lang, "extractor": extractor.name}

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

    # 原文 Markdown + 正文图片:trafilatura 会丢图,图片由 extractor 从原始 HTML 抽

    def _original_markdown(self, html: str, parsed: dict, extractor) -> tuple[str, int]:
        """trafilatura 出正文 markdown(纯文本,它会丢图);正文图由 extractor 从原始 HTML 抽,
        下载到 assets/ 后按原文位置内联到对应段落之后。锚点 = 图在 HTML 中前面最近的段落/标题文字,
        用于在 md 里定位该段插图;锚点匹配不上的图兜底插在标题后(导语图)。"""
        import trafilatura
        md = ""
        try:
            md = trafilatura.extract(
                html, output_format="markdown", include_comments=False, include_tables=True,
            ) or ""
        except Exception:
            md = ""
        if not md:
            md = parsed.get("text", "")
        title = parsed.get("title", "")
        # 标题:trafilatura md 常不带 H1,补上
        if title and not md.lstrip().startswith("# "):
            md = f"# {title}\n\n{md}"

        downloaded = self._download_content_images(extractor.content_image_urls(html))
        if downloaded:
            items = [(self._image_anchor(html, url), ref) for url, ref in downloaded]
            md = self._inline_images(md, items)
        return md, len(downloaded)

    def _download_content_images(self, urls: list[str]) -> list[tuple[str, str]]:
        """下载正文图到 assets/img_N.ext,返回 [(原 url, markdown 引用)](按序,失败的图跳过)。"""
        import urllib.request
        assets = self.job_dir / "assets"
        out: list[tuple[str, str]] = []
        for i, url in enumerate(urls):
            ext = (re.search(r'\.(png|jpe?g|gif|webp)', url.lower()) or [None, "jpg"])[1]
            ext = "jpg" if ext == "jpeg" else ext
            fname = f"img_{i:02d}.{ext}"
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                data = urllib.request.urlopen(req, timeout=20).read()
                if not data:
                    continue
                assets.mkdir(parents=True, exist_ok=True)
                (assets / fname).write_bytes(data)
                out.append((url, f"![](assets/{fname})"))
            except Exception:
                continue
        return out

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
        """把图按锚点插到 md 对应段落之后。图按文档序,锚点在 md 里也按序出现,故单调推进搜索。
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
                    if key and key in norm[i]:
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
