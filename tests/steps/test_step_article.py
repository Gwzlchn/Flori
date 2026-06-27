"""tests for steps/article 文章 pipeline 步骤（16/17/18/19）。

约束：不联网、不真调 AI。16 直接喂本地 HTML；18/19 用 DRY_RUN。
"""

import json

import pytest

from steps.article.step_02_parse_article import ParseArticleStep
from steps.article.step_03_article_sections import ArticleSectionsStep
from steps.article.step_04_smart_article import SmartArticleStep
from steps.article.step_05_concepts import ArticleConceptsStep
from steps.article.step_05_review import ArticleReviewStep
from shared.models import LLMResponse
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
        # 无 article_meta.json 时不应报错，仍能从 HTML 抽到标题
        result = step.execute()
        parsed = json.loads((job_dir / "intermediate" / "parsed.json").read_text())
        assert parsed["title"]
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
        sections = step.load_json("intermediate/sections.json")
        prompt = step._build_prompt(sections)
        assert "示例文章" in prompt
        assert "引言" in prompt

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
        monkeypatch.setattr(step, "call_ai", lambda *a, **k: note)
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
        config = make_step_config(tmp_path, step_name="05_review", pool="ai")
        step = ArticleReviewStep("05_review", job_dir, config)
        assert len(step.validate_inputs()) == 2

    def test_execute_dry_run(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DRY_RUN", "1")
        job_dir = self._setup(tmp_path)
        config = make_step_config(tmp_path, step_name="05_review", pool="ai")
        step = ArticleReviewStep("05_review", job_dir, config)
        result = step.execute()
        assert (job_dir / "output" / "review.json").exists()
        review = json.loads((job_dir / "output" / "review.json").read_text())
        assert "overall" in review
        assert "parse_failed" in result

    def test_parse_fallback(self, tmp_path, monkeypatch):
        # 非 DRY_RUN:AI 返回非 JSON → 走 fallback,overall 恒 3.0 + parse_failed。
        monkeypatch.delenv("DRY_RUN", raising=False)
        job_dir = self._setup(tmp_path)
        config = make_step_config(tmp_path, step_name="05_review", pool="ai")
        step = ArticleReviewStep("05_review", job_dir, config)
        monkeypatch.setattr(step, "call_ai", lambda *a, **k: "不是 JSON")
        result = step.execute()
        review = json.loads((job_dir / "output" / "review.json").read_text())
        assert review["overall"] == 3.0
        assert review["parse_failed"] is True
        assert result["parse_failed"] is True

    def test_aggregates_real_scores(self, tmp_path, monkeypatch):
        # 非 DRY_RUN:合法多维评分 → overall 为均值(而非恒 3.0),钉死评分聚合真跑了。
        monkeypatch.delenv("DRY_RUN", raising=False)
        job_dir = self._setup(tmp_path)
        config = make_step_config(tmp_path, step_name="05_review", pool="ai")
        step = ArticleReviewStep("05_review", job_dir, config)
        scores = {"completeness": 5, "accuracy": 5, "structure": 5,
                  "readability": 4, "insight": 4,
                  "key_terms": [], "missing_concepts": [], "top3_improvements": []}
        monkeypatch.setattr(step, "call_ai", lambda *a, **k: json.dumps(scores))
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
        step._gateway = _FakeGW(json.dumps(
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
        step._gateway = _FakeGW(json.dumps({"summary": "s", "key_terms": []}))
        result = step.execute()
        assert result["source"] == "smart_note"

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
        urls = ParseArticleStep._content_image_urls(html)
        assert len(urls) == 1
        assert "w/680" in urls[0]

    def test_content_image_keeps_unsized(self):
        # 无尺寸提示的非头像图保留(无法判定大小,默认当正文)。
        html = '<img src="https://site/photo.jpg"><img src="https://site/icon.svg">'
        urls = ParseArticleStep._content_image_urls(html)
        assert urls == ["https://site/photo.jpg"]

    def test_content_image_drops_anchor_wrapped(self):
        # <a> 链接包裹的大图 = 广告/促销 banner → 丢;裸大图(图表)→ 留。
        html = (
            '<a href="/promo"><img src="https://wpimg/ad.png?imageView2/2/w/680"></a>'
            '<img src="https://wpimg/chart.jpeg?imageView2/2/w/680" class="mx-auto">'
        )
        urls = ParseArticleStep._content_image_urls(html)
        assert urls == ["https://wpimg/chart.jpeg?imageView2/2/w/680"]

    def test_content_image_substack_link_wrapped_kept(self):
        # substack/SemiAnalysis 正文图:<a href=大图.png class=image-link><...><img w_1456> → 保留
        # (href 指向图片本身,不是促销页);头像(<a href=页面><img w_40>)、装饰条(h_72 无宽)→ 丢。
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
        urls = ParseArticleStep._content_image_urls(html)
        assert urls == [chart]

    def test_author_from_page_json(self, tmp_path):
        # SPA 内嵌 "author":{...,"display_name":"李丹"} 兜底抽作者。
        job_dir = _mk_job(tmp_path)
        config = make_step_config(tmp_path, step_name="02_parse_article", pool="cpu")
        step = ParseArticleStep("02_parse_article", job_dir, config)
        html = 'x "author":{"article_count":5513,"display_name":"李丹","id":75} y'
        assert step._authors_from_page_json(html) == ["李丹"]

    def test_author_page_json_absent(self, tmp_path):
        job_dir = _mk_job(tmp_path)
        config = make_step_config(tmp_path, step_name="02_parse_article", pool="cpu")
        step = ParseArticleStep("02_parse_article", job_dir, config)
        assert step._authors_from_page_json("<html>no author</html>") == []
