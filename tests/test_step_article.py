"""tests for steps/article 文章 pipeline 步骤（16/17/18/19）。

约束：不联网、不真调 AI。16 直接喂本地 HTML；18/19 用 DRY_RUN。
"""

import json

import pytest

from steps.article.step_02_parse_article import ParseArticleStep
from steps.article.step_03_article_sections import ArticleSectionsStep
from steps.article.step_04_smart_article import SmartArticleStep
from steps.article.step_05_review import ArticleReviewStep
from tests.steps.conftest import make_step_config


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
        assert (job_dir / "output" / "notes_smart.md").exists()


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
        (job_dir / "output" / "notes_smart.md").write_text("## 文章笔记\n\n内容\n")
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
