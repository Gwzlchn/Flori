"""steps/paper/step_03_sections.py 的测试。"""

import json

import pytest

from steps.paper.step_03_sections import SectionsStep
from tests.steps.conftest import make_step_config


class TestSectionsStep:
    def _setup_job(self, tmp_path):
        job_dir = tmp_path / "job"
        job_dir.mkdir()
        for d in ["intermediate"]:
            (job_dir / d).mkdir()

        parsed = {
            "title": "Test Paper",
            "authors": ["Author A"],
            "abstract": "This is abstract.",
            "pages": 5,
            "sections": [
                {"level": 1, "title": "Introduction", "page": 1, "text": "Intro text"},
                {"level": 2, "title": "Background", "page": 1, "text": "Background text"},
                {"level": 1, "title": "Method", "page": 2, "text": "Method text"},
                {"level": 2, "title": "Architecture", "page": 2, "text": "Arch text"},
                {"level": 2, "title": "Training", "page": 3, "text": "Train text"},
            ],
            "figures": [],
            "formulas": [],
        }
        (job_dir / "intermediate" / "parsed.json").write_text(json.dumps(parsed))
        return job_dir

    def test_execute(self, tmp_path):
        job_dir = self._setup_job(tmp_path)
        config = make_step_config(tmp_path, step_name="03_sections", pool="cpu")
        step = SectionsStep("03_sections", job_dir, config)
        result = step.execute()

        sections = json.loads((job_dir / "intermediate" / "sections.json").read_text())
        assert sections["title"] == "Test Paper"
        tree = sections["sections"]
        assert len(tree) == 2
        assert tree[0]["title"] == "Introduction"
        assert len(tree[0]["children"]) == 1
        assert tree[1]["title"] == "Method"
        assert len(tree[1]["children"]) == 2

    def test_execute_writes_original_md(self, tmp_path):
        # 论文原文兜底:标题 H1 + 作者行 + 摘要引用块 + 章节按树深降级标题,全文都在。
        job_dir = self._setup_job(tmp_path)
        (job_dir / "output").mkdir()
        config = make_step_config(tmp_path, step_name="03_sections", pool="cpu")
        SectionsStep("03_sections", job_dir, config).execute()

        md = (job_dir / "output" / "original.md").read_text(encoding="utf-8")
        assert md.startswith("# Test Paper")
        assert "Author A" in md
        assert "> This is abstract." in md
        assert "## Introduction" in md and "### Background" in md   # 树深→标题层级
        assert "Intro text" in md and "Train text" in md            # 正文不丢

    def test_arxiv_html_source_does_not_overwrite_original(self, tmp_path):
        # arxiv-html:02 已产干净 original.md(公式/图无损),03 不得用树渲染覆盖。
        job_dir = self._setup_job(tmp_path)
        (job_dir / "output").mkdir()
        parsed = json.loads((job_dir / "intermediate" / "parsed.json").read_text())
        parsed["source_kind"] = "arxiv-html"
        (job_dir / "intermediate" / "parsed.json").write_text(json.dumps(parsed))
        (job_dir / "output" / "original.md").write_text("# Clean HTML MD $x$")
        config = make_step_config(tmp_path, step_name="03_sections", pool="cpu")
        SectionsStep("03_sections", job_dir, config).execute()
        assert (job_dir / "output" / "original.md").read_text() == "# Clean HTML MD $x$"

    def test_original_md_empty_fields_skipped(self, tmp_path):
        # 缺标题/作者/摘要时不产出空头噪音,仅渲染存在的部分。
        md = SectionsStep._original_markdown({
            "title": "", "authors": [], "abstract": "",
            "sections": [{"level": 1, "title": "Only", "page": 1, "text": "body", "children": []}],
        })
        assert md.startswith("## Only")
        assert "body" in md

    def test_validate_inputs(self, tmp_path):
        job_dir = tmp_path / "job"
        job_dir.mkdir()
        (job_dir / "intermediate").mkdir()
        config = make_step_config(tmp_path, step_name="03_sections")
        step = SectionsStep("03_sections", job_dir, config)
        assert step.validate_inputs() == ["intermediate/parsed.json"]

    def test_idempotent(self, tmp_path):
        job_dir = self._setup_job(tmp_path)
        config = make_step_config(tmp_path, step_name="03_sections", pool="cpu")
        step = SectionsStep("03_sections", job_dir, config)
        step.execute()
        step.mark_done()
        step2 = SectionsStep("03_sections", job_dir, config)
        assert step2.should_run() is False
