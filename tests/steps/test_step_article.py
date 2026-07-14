"""steps/article 文章 pipeline 各步的测试。

约束:不联网、不真调 AI。解析步直接喂本地 HTML;AI 步用 DRY_RUN 或注入 fake 响应。
"""

import json

import pytest

from steps.article.step_02_parse_article import ParseArticleStep, MIN_BODY_CHARS
from steps.article.extractors import (
    pick_extractor, GenericExtractor, SubstackExtractor,
    generic_content_image_urls, authors_from_page_json,
)
from steps.article.extractors.substack import substack_figure_images
from steps.article.step_03_article_sections import ArticleSectionsStep
from steps.article.step_04_smart_article import SmartArticleStep
from steps.article.step_04_translate_article import TranslateArticleStep
from steps.article.step_05_concepts import ArticleConceptsStep
from steps.article.step_05_review import ArticleReviewStep
from shared.models import LLMResponse
from shared.errors import InputInvalidError
from tests.steps.conftest import make_step_config


class _FakeGW:
    """注入式假 gateway:call_ai 走它,返回固定内容(测概念步不真调 AI)。"""
    def __init__(self, content: str):
        self._c = content

    async def call(self, step_name, request):
        return LLMResponse(content=self._c, model="m", provider="claude-cli")


def _write_sections(job_dir, title="示例文章"):
    (job_dir / "intermediate" / "sections.json").write_text(json.dumps({
        "title": title, "authors": [], "abstract": "",
        "sections": [{"level": 1, "title": "正文", "page": 1,
                      "text": "注意力机制是一种权重分配方法。", "children": []}],
        "total_sections": 1,
    }))


SAMPLE_HTML = """<!DOCTYPE html>
<html lang="zh">
<head>
  <title>示例文章标题</title>
  <meta name="author" content="张三">
  <meta property="article:published_time" content="2026-01-15">
</head>
<body>
<article>
  <h1>示例文章标题</h1>
  <p>这是文章的第一段，介绍主题背景与研究动机，内容足够长以便正文抽取器识别为主体内容区域。</p>
  <h2>核心观点</h2>
  <p>这里阐述作者的核心论点，并给出关键数据支撑，论证脉络清晰完整，便于读者理解全文主旨。</p>
  <h2>结论</h2>
  <p>最后总结全文，给出可操作的结论与展望，呼应开头提出的研究动机，形成完整闭环。</p>
</article>
</body>
</html>
"""


ENGLISH_HTML = """<!DOCTYPE html>
<html lang="en">
<head><title>The Future of Compute</title><meta name="author" content="John Doe"></head>
<body><article>
<h1>The Future of Compute</h1>
<p>Compute is the lifeblood of artificial intelligence and the modern technology stack. This article explores why scaling compute matters and how it shapes the competitive landscape across nations and companies worldwide.</p>
<h2>Why It Matters</h2>
<p>Without sufficient compute capacity you do not have a seat at the table. Leading technology firms are investing heavily in data centers and accelerators at a staggering pace, reshaping global supply chains and policy.</p>
<h2>Conclusion</h2>
<p>In conclusion, compute will remain the decisive factor in the race for advanced artificial intelligence for the foreseeable future, with profound implications across industry and government.</p>
</article></body>
</html>
"""


def _mk_job(tmp_path):
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    for d in ["input", "intermediate", "output", "assets", "logs"]:
        (job_dir / d).mkdir()
    return job_dir


class TestParseArticleStep:
    def test_validate_inputs_missing(self, tmp_path):
        job_dir = _mk_job(tmp_path)
        config = make_step_config(tmp_path, step_name="02_parse_article", pool="cpu")
        step = ParseArticleStep("02_parse_article", job_dir, config)
        assert step.validate_inputs() == ["input/source.html"]

    def test_execute_extracts_body(self, tmp_path):
        job_dir = _mk_job(tmp_path)
        (job_dir / "input" / "source.html").write_text(SAMPLE_HTML, encoding="utf-8")
        (job_dir / "input" / "article_meta.json").write_text(
            json.dumps({
                "url": "https://example.com/post",
                "title": "示例文章标题",
                "author": "张三",
                "sitename": "示例站点",
                "date": "2026-01-15",
            }, ensure_ascii=False),
            encoding="utf-8",
        )
        config = make_step_config(tmp_path, step_name="02_parse_article", pool="cpu")
        step = ParseArticleStep("02_parse_article", job_dir, config)
        result = step.execute()

        parsed = json.loads((job_dir / "intermediate" / "parsed.json").read_text())
        assert parsed["title"] == "示例文章标题"
        assert parsed["url"] == "https://example.com/post"
        assert parsed["sitename"] == "示例站点"
        assert "核心论点" in parsed["text"]
        assert result["chars"] > 0
        assert parsed["sections"] and parsed["sections"][0]["text"]

    def test_meta_optional(self, tmp_path):
        job_dir = _mk_job(tmp_path)
        (job_dir / "input" / "source.html").write_text(SAMPLE_HTML, encoding="utf-8")
        config = make_step_config(tmp_path, step_name="02_parse_article", pool="cpu")
        step = ParseArticleStep("02_parse_article", job_dir, config)
        # 无 article_meta.json 时不应报错,仍能从 HTML 抽到标题
        result = step.execute()
        parsed = json.loads((job_dir / "intermediate" / "parsed.json").read_text())
        assert parsed["title"]
        assert result["chars"] > 0


# 空正文护栏用的 HTML 样本(付费墙/JS 残桩/空壳)。
PAYWALL_HTML = """<!DOCTYPE html>
<html lang="en">
<head><title>Premium Insight</title></head>
<body><article>
<h1>Premium Insight</h1>
<p>Subscribe to continue reading this members-only analysis.</p>
</article></body>
</html>
"""

# JS 渲染空壳:无正文,只有挂载点 + noscript 提示 → trafilatura 抽 0 字。
EMPTY_SHELL_HTML = """<!DOCTYPE html>
<html lang="en">
<head><title>Loading…</title></head>
<body><div id="app"></div><noscript>Please enable JavaScript to view this site.</noscript></body>
</html>
"""


class TestEmptyBodyGuard:
    """空正文护栏:付费墙/JS 残桩/空壳页正文过短 → 02_parse 直接 InputInvalidError,
    不让 03/04/05 在空正文上幻觉污染图谱。"""

    def test_effective_len_strips_whitespace(self):
        # 有效字符数 = 去掉所有空白后字符数(空白填充残桩不被误判为长)。
        assert ParseArticleStep._effective_len("  a b\n c\t d  ") == 4
        assert ParseArticleStep._effective_len("") == 0
        assert ParseArticleStep._effective_len("   \n\t  ") == 0
        assert ParseArticleStep._effective_len("中文 字符") == 4

    def test_paywall_marker_detection(self):
        assert ParseArticleStep._has_paywall_marker("Subscribe to continue reading.") is True
        assert ParseArticleStep._has_paywall_marker("请先登录后阅读全文") is True
        assert ParseArticleStep._has_paywall_marker("开通会员即可查看") is True
        # 长正文里恰好含 subscribe 一词不影响——本函数只在正文已判过短时才被调用细化信息。
        assert ParseArticleStep._has_paywall_marker("A normal article about gardening.") is False

    def test_empty_shell_raises(self, tmp_path):
        job_dir = _mk_job(tmp_path)
        (job_dir / "input" / "source.html").write_text(EMPTY_SHELL_HTML, encoding="utf-8")
        (job_dir / "input" / "article_meta.json").write_text(
            json.dumps({"url": "https://example.com/spa"}), encoding="utf-8")
        config = make_step_config(tmp_path, step_name="02_parse_article", pool="cpu")
        step = ParseArticleStep("02_parse_article", job_dir, config)
        with pytest.raises(InputInvalidError) as ei:
            step.execute()
        assert "正文过短" in str(ei.value)
        # 不留半成品产物:parsed.json / original.md / needs_translation 都不应写出。
        assert not (job_dir / "intermediate" / "parsed.json").exists()
        assert not (job_dir / "output" / "original.md").exists()
        assert not (job_dir / "intermediate" / "needs_translation.json").exists()

    def test_paywall_stub_raises(self, tmp_path):
        job_dir = _mk_job(tmp_path)
        (job_dir / "input" / "source.html").write_text(PAYWALL_HTML, encoding="utf-8")
        config = make_step_config(tmp_path, step_name="02_parse_article", pool="cpu")
        step = ParseArticleStep("02_parse_article", job_dir, config)
        with pytest.raises(InputInvalidError) as ei:
            step.execute()
        # error_type 走不重试分支(BUILD)。
        assert ei.value.error_type == "input_invalid"
        assert "正文过短" in str(ei.value)
        assert "付费墙" in str(ei.value)        # 命中付费墙标记 → 错误信息细化为"疑似付费墙"
        assert not (job_dir / "intermediate" / "parsed.json").exists()

    def test_normal_article_passes_guard(self, tmp_path):
        # 控制组:正常长文(SAMPLE_HTML)正文 >> 阈值 → 不触发护栏,正常产出。
        job_dir = _mk_job(tmp_path)
        (job_dir / "input" / "source.html").write_text(SAMPLE_HTML, encoding="utf-8")
        config = make_step_config(tmp_path, step_name="02_parse_article", pool="cpu")
        step = ParseArticleStep("02_parse_article", job_dir, config)
        result = step.execute()       # 不抛
        assert ParseArticleStep._effective_len(
            json.loads((job_dir / "intermediate" / "parsed.json").read_text())["text"]
        ) >= MIN_BODY_CHARS
        assert result["chars"] > 0


class TestArticleSectionsStep:
    def test_validate_inputs(self, tmp_path):
        job_dir = _mk_job(tmp_path)
        config = make_step_config(tmp_path, step_name="03_article_sections", pool="cpu")
        step = ArticleSectionsStep("03_article_sections", job_dir, config)
        assert step.validate_inputs() == ["intermediate/parsed.json"]

    def test_split_markdown_headings(self, tmp_path):
        job_dir = _mk_job(tmp_path)
        text = (
            "## 引言\n这是引言段落，足够长以便作为正文内容保留下来。\n\n"
            "## 方法\n这里介绍方法的细节，包含若干步骤说明与论证。\n\n"
            "### 子方法\n子方法描述内容。\n\n"
            "## 结论\n总结全文并给出结论。\n"
        )
        parsed = {
            "title": "T", "authors": ["A"], "abstract": "",
            "sections": [{"level": 1, "title": "正文", "page": 1, "text": text}],
            "text": text,
        }
        (job_dir / "intermediate" / "parsed.json").write_text(json.dumps(parsed))
        config = make_step_config(tmp_path, step_name="03_article_sections", pool="cpu")
        step = ArticleSectionsStep("03_article_sections", job_dir, config)
        result = step.execute()

        sections = json.loads((job_dir / "intermediate" / "sections.json").read_text())
        titles = [s["title"] for s in sections["sections"]]
        assert "引言" in titles
        assert "方法" in titles
        assert "结论" in titles
        # 子方法应作为方法的子节点
        method = next(s for s in sections["sections"] if s["title"] == "方法")
        assert any(c["title"] == "子方法" for c in method["children"])
        assert sections["total_sections"] >= 4

    def test_single_block_fallback(self, tmp_path):
        job_dir = _mk_job(tmp_path)
        text = "整篇没有任何标题，只有一段连续的正文文字，应当兜底为单一章节。"
        parsed = {
            "title": "T", "authors": [], "abstract": "",
            "sections": [{"level": 1, "title": "正文", "page": 1, "text": text}],
            "text": text,
        }
        (job_dir / "intermediate" / "parsed.json").write_text(json.dumps(parsed))
        config = make_step_config(tmp_path, step_name="03_article_sections", pool="cpu")
        step = ArticleSectionsStep("03_article_sections", job_dir, config)
        step.execute()
        sections = json.loads((job_dir / "intermediate" / "sections.json").read_text())
        assert len(sections["sections"]) >= 1
        assert sections["sections"][0]["text"]


class TestSmartArticleStep:
    def _setup(self, tmp_path):
        job_dir = _mk_job(tmp_path)
        sections = {
            "title": "示例文章",
            "authors": ["张三"],
            "abstract": "",
            "sections": [
                {"level": 1, "title": "引言", "page": 1, "text": "引言文本", "children": []},
            ],
            "total_sections": 1,
        }
        (job_dir / "intermediate" / "sections.json").write_text(json.dumps(sections))
        return job_dir

    def test_validate_inputs(self, tmp_path):
        job_dir = _mk_job(tmp_path)
        config = make_step_config(tmp_path, step_name="04_smart_article", pool="ai")
        step = SmartArticleStep("04_smart_article", job_dir, config)
        assert step.validate_inputs() == ["intermediate/sections.json"]

    def test_build_prompt(self, tmp_path):
        job_dir = self._setup(tmp_path)
        config = make_step_config(tmp_path, step_name="04_smart_article", pool="ai")
        step = SmartArticleStep("04_smart_article", job_dir, config)
        sections = step.artifacts.load_json("intermediate/sections.json")
        prompt = step._build_prompt(sections)
        assert "示例文章" in prompt
        assert "引言" in prompt

    def test_build_prompt_uses_translation_body(self, tmp_path):
        # 传入译文 body → 正文用译文(中文);元信息(标题)仍取自 sections。
        job_dir = self._setup(tmp_path)
        config = make_step_config(tmp_path, step_name="04_smart_article", pool="ai")
        step = SmartArticleStep("04_smart_article", job_dir, config)
        sections = step.artifacts.load_json("intermediate/sections.json")
        prompt = step._build_prompt(sections, "## 中文译文章节\n这是基于译文的正文内容。")
        assert "基于译文的正文内容" in prompt
        assert "示例文章" in prompt

    def test_execute_uses_translation_when_present(self, tmp_path, monkeypatch):
        job_dir = self._setup(tmp_path)
        (job_dir / "output" / "translated.md").write_text(
            "## 章节\n中文译文正文内容,用于做笔记。", encoding="utf-8")
        config = make_step_config(tmp_path, step_name="04_smart_article", pool="ai")
        step = SmartArticleStep("04_smart_article", job_dir, config)
        cap: dict = {}
        note = "# 笔记\n\n" + "## 正文\n足够长的真实正文内容以通过净化长度判废。\n" * 30
        monkeypatch.setattr(step.ai, "call", lambda prompt, **k: cap.update(p=prompt) or note)
        result = step.execute()
        assert result["source"] == "translation"
        assert "中文译文正文内容" in cap["p"]

    def test_execute_dry_run(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DRY_RUN", "1")
        job_dir = self._setup(tmp_path)
        config = make_step_config(tmp_path, step_name="04_smart_article", pool="ai")
        step = SmartArticleStep("04_smart_article", job_dir, config)
        result = step.execute()
        assert result["chars"] > 0
        assert list((job_dir / "output" / "versions").glob("notes_smart_*.md"))

    def test_execute_real_path_sanitizes(self, tmp_path, monkeypatch):
        # 非 DRY_RUN:驱动 write_smart_note 的 _sanitize_smart_note(去 agentic 壳 + 补 assets/ 前缀)。
        # DRY_RUN smoke 只断 chars>0,这些净化逻辑全被绕过(_sanitize 在 DRY_RUN 下第一行就 return)。
        monkeypatch.delenv("DRY_RUN", raising=False)
        job_dir = self._setup(tmp_path)
        config = make_step_config(tmp_path, step_name="04_smart_article", pool="ai")
        step = SmartArticleStep("04_smart_article", job_dir, config)
        note = (
            "已完成文章笔记,思路如下:\n\n"                   # agentic 开头 → 应被净化砍到首个标题
            "# 文章笔记\n\n"
            "![配图](pic.png)\n\n"                          # 裸文件名 → 补 assets/ 前缀
            + "## 正文\n足够长的真实正文以通过净化长度判废。\n" * 30
        )
        monkeypatch.setattr(step.ai, "call", lambda *a, **k: note)
        step.execute()
        written = next(
            (job_dir / "output" / "versions").glob("notes_smart_*.md")
        ).read_text(encoding="utf-8")
        assert "已完成文章笔记" not in written            # agentic 开头被净化
        assert "![配图](assets/pic.png)" in written        # 裸文件名补了 assets/ 前缀
        assert "## 正文" in written


class TestArticleReviewStep:
    def _setup(self, tmp_path):
        job_dir = _mk_job(tmp_path)
        sections = {
            "title": "示例文章",
            "sections": [
                {"level": 1, "title": "引言", "page": 1, "text": "t", "children": []},
            ],
        }
        (job_dir / "intermediate" / "sections.json").write_text(json.dumps(sections))
        (job_dir / "output" / "versions").mkdir(exist_ok=True)
        (job_dir / "output" / "versions" / "notes_smart_anthropic_claude-sonnet-4-6_20260101-000000.md").write_text("## 文章笔记\n\n内容\n")
        return job_dir

    def test_validate_inputs(self, tmp_path):
        job_dir = _mk_job(tmp_path)
        config = make_step_config(tmp_path, step_name="06_review", pool="ai")
        step = ArticleReviewStep("06_review", job_dir, config)
        assert len(step.validate_inputs()) == 2

    def test_execute_dry_run(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DRY_RUN", "1")
        job_dir = self._setup(tmp_path)
        config = make_step_config(tmp_path, step_name="06_review", pool="ai")
        step = ArticleReviewStep("06_review", job_dir, config)
        result = step.execute()
        assert (job_dir / "output" / "review.json").exists()
        review = json.loads((job_dir / "output" / "review.json").read_text())
        assert "overall" in review
        assert "parse_failed" in result

    def test_parse_fallback(self, tmp_path, monkeypatch):
        # 非 DRY_RUN:AI 返回非 JSON → 走 fallback,overall 恒 3.0 + parse_failed。
        monkeypatch.delenv("DRY_RUN", raising=False)
        job_dir = self._setup(tmp_path)
        config = make_step_config(tmp_path, step_name="06_review", pool="ai")
        step = ArticleReviewStep("06_review", job_dir, config)
        monkeypatch.setattr(step.ai, "call", lambda *a, **k: "不是 JSON")
        result = step.execute()
        review = json.loads((job_dir / "output" / "review.json").read_text())
        assert review["overall"] is None
        assert review["review_reliable"] is False
        assert review["parse"]["mode"] == "fallback"
        assert result["parse_failed"] is True

    def test_aggregates_real_scores(self, tmp_path, monkeypatch):
        # 非 DRY_RUN:合法多维评分 → overall 为均值(而非恒 3.0),钉死评分聚合真跑了。
        monkeypatch.delenv("DRY_RUN", raising=False)
        job_dir = self._setup(tmp_path)
        config = make_step_config(tmp_path, step_name="06_review", pool="ai")
        step = ArticleReviewStep("06_review", job_dir, config)
        scores = {"completeness": 5, "accuracy": 5, "structure": 5,
                  "readability": 4, "insight": 4,
                  "key_terms": [], "missing_concepts": [],
                  "top3_improvements": ["a", "b", "c"], "issues": []}
        monkeypatch.setattr(step.ai, "call", lambda *a, **k: json.dumps(scores))
        result = step.execute()
        review = json.loads((job_dir / "output" / "review.json").read_text())
        assert result["parse_failed"] is False
        assert review["overall"] == 4.6      # (5+5+5+4+4)/5 = 4.6,非 3.0


class TestConceptsStep:
    def test_validate_inputs(self, tmp_path):
        job_dir = _mk_job(tmp_path)
        config = make_step_config(tmp_path, step_name="05_concepts", pool="ai")
        step = ArticleConceptsStep("05_concepts", job_dir, config)
        assert step.validate_inputs() == ["intermediate/sections.json"]

    def test_execute_from_original(self, tmp_path):
        # 无智能笔记 → 概念抽自原文;产出 concepts.json(key_terms + summary)。
        job_dir = _mk_job(tmp_path)
        _write_sections(job_dir)
        config = make_step_config(tmp_path, step_name="05_concepts", pool="ai")
        step = ArticleConceptsStep("05_concepts", job_dir, config)
        step.ai.gateway = _FakeGW(json.dumps(
            {"summary": "讲注意力", "key_terms": [{"term": "注意力机制", "definition": "权重分配"}]}))
        result = step.execute()
        assert result["source"] == "original"
        assert result["concepts"] == 1
        out = json.loads((job_dir / "output" / "concepts.json").read_text())
        assert out["key_terms"][0]["term"] == "注意力机制"
        assert out["summary"] == "讲注意力"
        assert out["source"] == "original"

    def test_source_prefers_smart_note(self, tmp_path):
        # 有智能笔记 → 概念抽自笔记(source=smart_note)。
        job_dir = _mk_job(tmp_path)
        _write_sections(job_dir)
        (job_dir / "output" / "versions").mkdir(parents=True)
        (job_dir / "output" / "versions" / "notes_smart_claude-cli_x_20260101-000000.md").write_text(
            "# 笔记\n注意力机制是核心。")
        config = make_step_config(tmp_path, step_name="05_concepts", pool="ai")
        step = ArticleConceptsStep("05_concepts", job_dir, config)
        step.ai.gateway = _FakeGW(json.dumps({"summary": "s", "key_terms": []}))
        result = step.execute()
        assert result["source"] == "smart_note"

    def test_source_prefers_translation_over_original(self, tmp_path):
        # 无智能笔记但有译文(非中文文章)→ 概念抽自译文,source=translation,术语与译文一致。
        job_dir = _mk_job(tmp_path)
        _write_sections(job_dir)
        (job_dir / "output" / "translated.md").write_text(
            "# 标题\n这是中文译文,用于抽取概念与一句话摘要。", encoding="utf-8")
        config = make_step_config(tmp_path, step_name="05_concepts", pool="ai")
        step = ArticleConceptsStep("05_concepts", job_dir, config)
        step.ai.gateway = _FakeGW(json.dumps({"summary": "s", "key_terms": []}))
        result = step.execute()
        assert result["source"] == "translation"

    def test_dry_run_smoke_still_writes(self, tmp_path, monkeypatch):
        # DRY_RUN 返回非 JSON → 回退空概念,但必跑步仍产出 concepts.json(不报错)。
        monkeypatch.setenv("DRY_RUN", "1")
        job_dir = _mk_job(tmp_path)
        _write_sections(job_dir)
        config = make_step_config(tmp_path, step_name="05_concepts", pool="ai")
        step = ArticleConceptsStep("05_concepts", job_dir, config)
        step.execute()
        assert (job_dir / "output" / "concepts.json").exists()


class TestArticleImageAndAuthor:
    def test_content_image_filter_keeps_large_drops_chrome(self):
        # 正文大图(w/680)保留;缩略图(w/108)、头像、data: 丢弃;同图去重。
        html = (
            '<img src="https://wpimg/a.jpeg?imageView2/2/w/680" class="mx-auto">'
            '<img src="https://wpimg/b.jpg?imageView2/1/w/108">'
            '<img src="https://dn-wscn-avatar/u.png?imageView2/1/w/800">'
            '<img src="data:image/png;base64,xxx">'
            '<img src="https://wpimg/a.jpeg?imageView2/2/w/680">'  # 同图再现
        )
        urls = generic_content_image_urls(html)
        assert len(urls) == 1
        assert "w/680" in urls[0]

    def test_content_image_keeps_unsized(self):
        # 无尺寸提示的非头像图保留(无法判定大小,默认当正文)。
        html = '<img src="https://site/photo.jpg"><img src="https://site/icon.svg">'
        urls = generic_content_image_urls(html)
        assert urls == ["https://site/photo.jpg"]

    def test_content_image_drops_anchor_wrapped(self):
        # <a> 链接包裹的大图 = 广告/促销 banner → 丢;裸大图(图表)→ 留。
        html = (
            '<a href="/promo"><img src="https://wpimg/ad.png?imageView2/2/w/680"></a>'
            '<img src="https://wpimg/chart.jpeg?imageView2/2/w/680" class="mx-auto">'
        )
        urls = generic_content_image_urls(html)
        assert urls == ["https://wpimg/chart.jpeg?imageView2/2/w/680"]

    def test_content_image_substack_link_wrapped_kept(self):
        # substack/SemiAnalysis 正文图形如 <a href=大图.png class=image-link>...<img w_1456>,
        # href 指向图片本身而非促销页 → 保留;头像形如 <a href=页面><img w_40>,装饰条 h_72 无宽,均丢。
        chart = "https://substackcdn.com/image/fetch/$s_!c!,w_1456,c_limit,f_auto/chart.png"
        html = (
            '<a href="/profile/123"><img src="https://substackcdn.com/image/fetch/$s_!a!,w_40,h_40,c_fill/x.png"></a>'
            '<img alt="SemiAnalysis" src="https://substackcdn.com/image/fetch/$s_!b!,h_72,c_limit/banner.png">'
            '<a href="https://substackcdn.com/image/fetch/$s_!c!/chart_975x615.png" class="image-link image2">'
            '<div class="image2-inset"><picture>'
            '<source type="image/webp" srcset="https://substackcdn.com/image/fetch/$s_!c!,w_1456,f_webp/c.png 1456w">'
            f'<img src="{chart}" class="sizing-normal"/>'
            '</picture></div></a>'
        )
        urls = generic_content_image_urls(html)
        assert urls == [chart]

    def test_author_from_page_json(self, tmp_path):
        # SPA 内嵌 "author":{...,"display_name":"李丹"} 兜底抽作者。
        job_dir = _mk_job(tmp_path)
        config = make_step_config(tmp_path, step_name="02_parse_article", pool="cpu")
        step = ParseArticleStep("02_parse_article", job_dir, config)
        html = 'x "author":{"article_count":5513,"display_name":"李丹","id":75} y'
        assert authors_from_page_json(html) == ["李丹"]

    def test_author_page_json_absent(self, tmp_path):
        job_dir = _mk_job(tmp_path)
        config = make_step_config(tmp_path, step_name="02_parse_article", pool="cpu")
        step = ParseArticleStep("02_parse_article", job_dir, config)
        assert authors_from_page_json("<html>no author</html>") == []


class TestArticleExtractors:
    """extractor 注册表:按页面特征选站点 extractor,否则通用兜底。"""

    def test_pick_substack_by_signature_not_domain(self):
        # substack 是平台:semianalysis.com 自定义域名靠 substackcdn 特征命中(不靠域名)。
        ex = pick_extractor("https://semianalysis.com/2025/x", '<img src="https://substackcdn.com/image/fetch/a.png">')
        assert isinstance(ex, SubstackExtractor)

    def test_pick_substack_by_image2todom(self):
        ex = pick_extractor("https://whatever.example/p", '<div data-component-name="Image2ToDOM"></div>')
        assert isinstance(ex, SubstackExtractor)

    def test_pick_generic_fallback(self):
        ex = pick_extractor("https://some-blog.cn/post", "<article><p>正文</p></article>")
        assert isinstance(ex, GenericExtractor)

    def test_substack_figure_images_dedup(self):
        # substack 正文图在 <figure> 内;SSR+水合重复同图 → 去重;非 figure 的头像/logo 不收。
        html = (
            '<img src="https://substackcdn.com/image/fetch/$s_!a!,w_40/avatar.png">'   # 头像,不在 figure
            '<figure><a href="https://substackcdn.com/image/fetch/$s_!c!/chart_975x615.png">'
            '<picture><source srcset="x 1456w">'
            '<img src="https://substackcdn.com/image/fetch/$s_!c!,w_1456/chart.png"></picture></a></figure>'
            '<figure><img src="https://substackcdn.com/image/fetch/$s_!c!,w_1456/chart.png"></figure>'  # 水合重复
            '<figure><img src="https://substackcdn.com/image/fetch/$s_!d!,w_1456/two.png"></figure>'
        )
        urls = substack_figure_images(html)
        assert urls == [
            "https://substackcdn.com/image/fetch/$s_!c!,w_1456/chart.png",
            "https://substackcdn.com/image/fetch/$s_!d!,w_1456/two.png",
        ]

    def test_substack_extractor_uses_figure_path(self):
        html = '<figure><img src="https://substackcdn.com/image/fetch/$s_!c!,w_1456/c.png"></figure>'
        assert SubstackExtractor().content_image_urls(html) == [
            "https://substackcdn.com/image/fetch/$s_!c!,w_1456/c.png"
        ]


class TestOriginalMarkdownPipeline:
    """原文 MD 管线(readability+markdownify):<pre> 代码块保多行围栏、数学/下划线不转义、
    图片引用原位 + 相对 src urljoin 本地化、下载失败保绝对 URL 不丢图。"""

    CODE_HTML = (
        '<html><head><title>Code Post</title></head><body><article>'
        '<h1>Code Post</h1>'
        '<p>Long intro paragraph about io_uring internals, long enough for readability '
        'to keep the article body as the main content region of this page.</p>'
        '<pre>struct io_uring_params {\n\t__u32 sq_entries;\n\t__u32 cq_entries;\n};</pre>'
        '<p>Math like $P_\\text{Correct}(q_a)$ and snake_case_name must survive as-is.</p>'
        '<p><img src="figs/diagram.png" alt="diagram"></p>'
        '<p>Closing paragraph with enough words to keep extraction stable across '
        'readability versions and content heuristics.</p>'
        '</article></body></html>'
    )
    PARSED = {"title": "Code Post", "url": "https://example.com/post/"}

    def _step(self, tmp_path):
        job_dir = _mk_job(tmp_path)
        config = make_step_config(tmp_path, step_name="02_parse_article", pool="cpu")
        return ParseArticleStep("02_parse_article", job_dir, config), job_dir

    def test_pre_survives_as_multiline_fenced_code(self, tmp_path, monkeypatch):
        step, _ = self._step(tmp_path)
        monkeypatch.setattr(ParseArticleStep, "_fetch_image", staticmethod(lambda url: None))
        md, _ = step._original_markdown(self.CODE_HTML, self.PARSED, GenericExtractor())
        assert "```" in md
        block = md[md.find("struct io_uring_params"):md.find("};")]
        assert "__u32 sq_entries;" in block and "\n" in block     # 多行原样,没被拍扁

    def test_math_and_underscores_not_escaped(self, tmp_path, monkeypatch):
        step, _ = self._step(tmp_path)
        monkeypatch.setattr(ParseArticleStep, "_fetch_image", staticmethod(lambda url: None))
        md, _ = step._original_markdown(self.CODE_HTML, self.PARSED, GenericExtractor())
        assert "$P_\\text{Correct}(q_a)$" in md
        assert "snake_case_name" in md and "snake\\_case" not in md

    def test_relative_img_urljoined_and_localized(self, tmp_path, monkeypatch):
        step, job_dir = self._step(tmp_path)
        fetched: list[str] = []
        monkeypatch.setattr(ParseArticleStep, "_fetch_image",
                            staticmethod(lambda url: fetched.append(url) or b"PNGDATA"))
        md, n = step._original_markdown(self.CODE_HTML, self.PARSED, GenericExtractor())
        assert fetched == ["https://example.com/post/figs/diagram.png"]   # 相对 src 已 urljoin
        assert n == 1 and "![diagram](assets/img_00.png)" in md
        assert (job_dir / "assets" / "img_00.png").read_bytes() == b"PNGDATA"

    def test_download_failure_keeps_absolute_url(self, tmp_path, monkeypatch):
        step, job_dir = self._step(tmp_path)
        monkeypatch.setattr(ParseArticleStep, "_fetch_image", staticmethod(lambda url: None))
        md, n = step._original_markdown(self.CODE_HTML, self.PARSED, GenericExtractor())
        assert n == 0
        assert "![diagram](https://example.com/post/figs/diagram.png)" in md   # 保绝对 URL 不丢图
        assert not (job_dir / "assets" / "img_00.png").exists()

    def test_data_uri_skipped(self, tmp_path):
        step, _ = self._step(tmp_path)
        md, n = step._localize_images("![](data:image/png;base64,AAAA)", "https://x.com/p")
        assert n == 0 and md == "![](data:image/png;base64,AAAA)"

    def test_same_image_downloaded_once(self, tmp_path, monkeypatch):
        step, _ = self._step(tmp_path)
        calls: list[str] = []
        monkeypatch.setattr(ParseArticleStep, "_fetch_image",
                            staticmethod(lambda url: calls.append(url) or b"X"))
        md, n = step._localize_images(
            "![](pic.png) 中间文字 ![again](pic.png)", "https://a.com/b/")
        assert len(calls) == 1 and n == 1
        assert md.count("assets/img_00.png") == 2


class TestImageRecovery:
    """readability 剔深嵌套正文图后的找回:extractor 提供缺失 url,锚点插回原位再本地化。"""

    def test_anchor_is_preceding_paragraph(self):
        html = '<p>第一段足够长的引言文字内容</p><figure><img src="https://cdn/a.png"></figure>'
        assert ParseArticleStep._image_anchor(html, "https://cdn/a.png") == "第一段足够长的引言文字内容"

    def test_anchor_absent_returns_empty(self):
        assert ParseArticleStep._image_anchor("<p>x</p>", "https://cdn/missing.png") == ""

    def test_inline_after_matching_paragraph(self):
        md = "# 标题\n\n第一段足够长的引言文字内容。\n\n第二段也是足够长的正文内容。"
        out = ParseArticleStep._inline_images(md, [("第二段也是足够长的正文内容。", "![](https://cdn/a.png)")]).split("\n")
        j = next(k for k, l in enumerate(out) if "第二段" in l)
        assert any("cdn/a.png" in l for l in out[j + 1:j + 3])
        assert not any("cdn/a.png" in l for l in out[:j])

    def test_inline_leftover_after_title(self):
        md = "# 标题\n\n正文段落内容。"
        out = ParseArticleStep._inline_images(md, [("", "![](https://cdn/a.png)")]).split("\n")
        ti = next(k for k, l in enumerate(out) if l.startswith("# "))
        assert any("cdn/a.png" in l for l in out[ti + 1:ti + 3])

    def test_recovered_image_injected_and_localized(self, tmp_path, monkeypatch):
        # md 骨架里没有该图(模拟 readability 剔除),extractor 报告其存在 → 锚点插回 + 本地化。
        job_dir = tmp_path / "job"
        job_dir.mkdir()
        for d in ["input", "intermediate", "output", "assets", "logs"]:
            (job_dir / d).mkdir()
        config = make_step_config(tmp_path, step_name="02_parse_article", pool="cpu")
        step = ParseArticleStep("02_parse_article", job_dir, config)
        monkeypatch.setattr(ParseArticleStep, "_fetch_image", staticmethod(lambda url: b"PNG"))

        class _Ext:
            name = "stub"
            def content_image_urls(self, html):
                return ["https://cdn.example.com/fig1.png"]

        html = ('<html><body><article><h1>T</h1>'
                '<p>这是一段足够长的正文文字,用来给图片当锚点定位使用的段落。</p>'
                '<figure><img src="https://cdn.example.com/fig1.png"></figure>'
                '<p>结尾段落,同样足够长以保证正文抽取稳定不被清洗掉。</p>'
                '</article></body></html>')
        md, n = step._original_markdown(html, {"title": "T", "url": "https://x.com/p"}, _Ext())
        assert n == 1
        assert "![](assets/img_00.png)" in md
        i_anchor = md.find("当锚点定位使用的段落")
        i_img = md.find("assets/img_00.png")
        assert 0 < i_anchor < i_img            # 插在锚点段之后


class TestArticleLangDetect:
    """语言检测 + 翻译标记:非中文文章写 needs_translation.json,中文不写。"""

    def test_detect_lang(self):
        assert ParseArticleStep._detect_lang("The quick brown fox jumps over the lazy dog. " * 4) == "non-zh"
        assert ParseArticleStep._detect_lang("这是一篇讲人工智能发展与应用的中文文章。" * 3) == "zh"
        assert ParseArticleStep._detect_lang("") == "unknown"
        # 中文夹少量英文术语仍判 zh
        assert ParseArticleStep._detect_lang("人工智能 AI 与机器学习 ML 在中文语境下的长篇内容很多很多") == "zh"

    def test_domain_fallback_for_sitename(self):
        # 来源网站名兜底:无 sitename 时用 URL 域名(去 www)。
        assert ParseArticleStep._domain("https://www.semianalysis.com/2025/x") == "semianalysis.com"
        assert ParseArticleStep._domain("https://wallstreetcn.com/articles/1") == "wallstreetcn.com"
        assert ParseArticleStep._domain("") == ""

    def test_english_article_writes_translate_marker(self, tmp_path):
        job_dir = _mk_job(tmp_path)
        (job_dir / "input" / "source.html").write_text(ENGLISH_HTML, encoding="utf-8")
        config = make_step_config(tmp_path, step_name="02_parse_article", pool="cpu")
        step = ParseArticleStep("02_parse_article", job_dir, config)
        result = step.execute()
        assert result["lang"] == "non-zh"
        assert (job_dir / "intermediate" / "needs_translation.json").exists()

    def test_chinese_article_no_marker(self, tmp_path):
        job_dir = _mk_job(tmp_path)
        (job_dir / "input" / "source.html").write_text(SAMPLE_HTML, encoding="utf-8")
        config = make_step_config(tmp_path, step_name="02_parse_article", pool="cpu")
        step = ParseArticleStep("02_parse_article", job_dir, config)
        result = step.execute()
        assert result["lang"] == "zh"
        assert not (job_dir / "intermediate" / "needs_translation.json").exists()


class TestTranslateArticleStep:
    def test_validate_inputs_missing(self, tmp_path):
        job_dir = _mk_job(tmp_path)
        config = make_step_config(tmp_path, step_name="04_translate_article", pool="ai")
        step = TranslateArticleStep("04_translate_article", job_dir, config)
        assert step.validate_inputs() == ["output/original.md"]

    def test_execute_translates_preserving_images(self, tmp_path, monkeypatch):
        job_dir = _mk_job(tmp_path)
        (job_dir / "output" / "original.md").write_text(
            "# Title\n\nHello world, this is a test.\n\n![](assets/img_00.png)", encoding="utf-8")
        config = make_step_config(tmp_path, step_name="04_translate_article", pool="ai")
        step = TranslateArticleStep("04_translate_article", job_dir, config)
        monkeypatch.setattr(step.ai, "call",
                            lambda *a, **k: "# 标题\n\n你好世界,这是一个测试。\n\n![](assets/img_00.png)")
        result = step.execute()
        assert result["chars"] > 0
        out = (job_dir / "output" / "translated.md").read_text(encoding="utf-8")
        assert "你好世界" in out
        assert "![](assets/img_00.png)" in out          # 图片引用原样保留


class TestTranslateArticleChunking:
    def test_large_article_chunks_and_aggregates(self, tmp_path, monkeypatch):
        # 超长文按段落切多 chunk(单调用会撞 600s 双超时,线上 GPT-3 论文实证);逐块调用按序聚合。
        job_dir = _mk_job(tmp_path)
        big = "\n\n".join(f"Paragraph {i}. " + ("word " * 400) for i in range(20))
        (job_dir / "output" / "original.md").write_text(big, encoding="utf-8")
        config = make_step_config(tmp_path, step_name="04_translate_article", pool="ai")
        step = TranslateArticleStep("04_translate_article", job_dir, config)
        calls = []
        monkeypatch.setattr(step.ai, "call",
                            lambda prompt, **k: calls.append(prompt) or f"块{len(calls)}")
        result = step.execute()
        assert result["chunks"] > 1 and len(calls) == result["chunks"]
        out = (job_dir / "output" / "translated.md").read_text(encoding="utf-8")
        assert out.index("块1") < out.index(f"块{result['chunks']}")
