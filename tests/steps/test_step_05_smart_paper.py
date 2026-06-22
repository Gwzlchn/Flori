"""tests for steps/paper/step_05_smart_paper.py"""

import json
import os

import pytest

from steps.paper.step_05_smart_paper import SmartPaperStep
from tests.steps.conftest import make_step_config


class TestSmartPaperStep:
    def _setup_job(self, tmp_path):
        job_dir = tmp_path / "job"
        job_dir.mkdir()
        for d in ["intermediate", "output", "assets", "logs"]:
            (job_dir / d).mkdir()

        sections = {
            "title": "Test Paper",
            "authors": ["Author"],
            "abstract": "Abstract here.",
            "sections": [
                {"level": 1, "title": "Intro", "page": 1, "text": "Intro text", "children": []},
            ],
            "total_sections": 1,
        }
        (job_dir / "intermediate" / "sections.json").write_text(json.dumps(sections))

        figures = [
            {"id": "fig1", "page": 1, "caption": "Architecture", "filename": None, "ocr_text": ""},
        ]
        (job_dir / "intermediate" / "figures.json").write_text(json.dumps(figures))
        return job_dir

    def test_validate_inputs(self, tmp_path):
        job_dir = tmp_path / "job"
        job_dir.mkdir()
        (job_dir / "intermediate").mkdir()
        config = make_step_config(tmp_path, step_name="05_smart_paper")
        step = SmartPaperStep("05_smart_paper", job_dir, config)
        assert len(step.validate_inputs()) == 2

    def test_execute_dry_run(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DRY_RUN", "1")
        job_dir = self._setup_job(tmp_path)
        config = make_step_config(tmp_path, step_name="05_smart_paper", pool="ai")
        step = SmartPaperStep("05_smart_paper", job_dir, config)
        result = step.execute()
        assert result["chars"] > 0
        assert list((job_dir / "output" / "versions").glob("notes_smart_*.md"))

    def test_execute_real_path_backfills_and_sanitizes(self, tmp_path, monkeypatch):
        # 非 DRY_RUN 真实路径:驱动 write_smart_note 的 ![](img:N) 占位符回填 + _sanitize_smart_note
        # 净化(去 agentic 壳 / 补 assets/ 前缀)。这些核心后处理在 DRY_RUN smoke 里全被绕过——
        # DRY_RUN 直接返回合成占位串,_sanitize 第一行就 return,占位符回填路径完全无测。
        monkeypatch.delenv("DRY_RUN", raising=False)
        job_dir = self._setup_job(tmp_path)
        # 带内嵌位图的图(filename + index)→ execute 构建非空 image_assets,落盘时回填 img:N。
        (job_dir / "intermediate" / "figures.json").write_text(json.dumps([
            {"id": "fig1", "index": 0, "page": 1, "caption": "架构",
             "filename": "fig-0000.png", "ocr_text": ""},
        ]))
        config = make_step_config(tmp_path, step_name="05_smart_paper", pool="ai")
        step = SmartPaperStep("05_smart_paper", job_dir, config)

        note = (
            "已完成论文笔记重组,思路如下:\n\n"               # agentic 开头 → 应被净化砍到首个标题
            "# 论文笔记\n\n"
            "![架构图](img:0)\n\n"                           # 占位符 → 按清单回填成 assets/fig-0000.png
            "![流程](diagram.png)\n\n"                       # 裸文件名 → sanitize 补 assets/ 前缀
            + "## 正文\n足够长的真实正文以通过净化长度判废(strict 下 <500 触发重试)。\n" * 30
        )
        monkeypatch.setattr(step, "call_ai", lambda *a, **k: note)

        result = step.execute()
        written = next(
            (job_dir / "output" / "versions").glob("notes_smart_*.md")
        ).read_text(encoding="utf-8")
        assert "![架构图](assets/fig-0000.png)" in written   # img:0 占位符按清单回填成真实路径
        assert "img:0" not in written                        # 无裸占位符残留
        assert "![流程](assets/diagram.png)" in written       # 裸文件名补了 assets/ 前缀
        assert "已完成论文笔记重组" not in written            # agentic 开头被净化掉
        assert result["chars"] > 500

    def test_build_prompt(self, tmp_path):
        job_dir = self._setup_job(tmp_path)
        config = make_step_config(tmp_path, step_name="05_smart_paper")
        step = SmartPaperStep("05_smart_paper", job_dir, config)
        sections = step.load_json("intermediate/sections.json")
        figures = step.load_json("intermediate/figures.json")
        prompt = step._build_prompt(sections, figures)
        assert "Test Paper" in prompt
        assert "fig1" in prompt
