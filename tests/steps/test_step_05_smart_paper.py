"""steps/paper/step_05_smart_paper.py 的测试。"""

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
        # figures.json 已可选(04_figures 随 pymupdf 删除;图在正文/PDF 里)
        assert step.validate_inputs() == ["intermediate/sections.json"]

    def test_execute_dry_run(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DRY_RUN", "1")
        job_dir = self._setup_job(tmp_path)
        config = make_step_config(tmp_path, step_name="05_smart_paper", pool="ai")
        step = SmartPaperStep("05_smart_paper", job_dir, config)
        result = step.execute()
        assert result["chars"] > 0
        assert list((job_dir / "output" / "versions").glob("notes_smart_*.md"))

    def test_uses_translation_when_present(self, tmp_path, monkeypatch):
        # 非中文论文有译文 → 笔记基于译文做(source=translation),prompt 用译文正文。
        job_dir = self._setup_job(tmp_path)
        (job_dir / "output" / "translated.md").write_text(
            "# 测试论文\n\n## 引言\n中文译文正文内容。", encoding="utf-8")
        config = make_step_config(tmp_path, step_name="05_smart_paper", pool="ai")
        step = SmartPaperStep("05_smart_paper", job_dir, config)
        cap: dict = {}
        note = "# 笔记\n\n" + "## 正文\n足够长的真实正文内容以通过净化长度判废。\n" * 30
        monkeypatch.setattr(step, "call_ai", lambda prompt, **k: cap.update(p=prompt) or note)
        result = step.execute()
        assert result["source"] == "translation"
        assert "中文译文正文内容" in cap["p"]

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


def test_pdf_only_direct_notes(tmp_path, monkeypatch):
    # pdf-only 且无任何文本正文(无译文/原文 MD):笔记走 Read 直喂 PDF。
    import json as _json
    from tests.steps.conftest import make_step_config
    from steps.paper.step_05_smart_paper import SmartPaperStep
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    for d in ["input", "intermediate", "output", "logs"]:
        (job_dir / d).mkdir()
    (job_dir / "input" / "source.pdf").write_bytes(b"%PDF fake")
    (job_dir / "intermediate" / "sections.json").write_text(_json.dumps(
        {"title": "MapReduce", "sections": []}))
    (job_dir / "intermediate" / "parsed.json").write_text(_json.dumps(
        {"source_kind": "pdf-only", "pages": 6}))
    config = make_step_config(tmp_path, step_name="05_smart_paper", pool="ai")
    step = SmartPaperStep("05_smart_paper", job_dir, config)

    seen = {}
    def fake_call(prompt, **kw):
        seen["prompt"], seen["kw"] = prompt, kw
        return "# 笔记"
    monkeypatch.setattr(step, "call_ai", fake_call)
    monkeypatch.setattr(step, "write_smart_note", lambda text, image_assets=None: "output/versions/x.md")
    r = step.execute()
    assert r["source"] == "pdf-direct"
    assert seen["kw"]["allowed_tools"] == ["Read"]
    assert str((job_dir / "input").resolve()) in seen["kw"]["add_dirs"][0]
    assert "source.pdf" in seen["prompt"] and "MapReduce" in seen["prompt"]
