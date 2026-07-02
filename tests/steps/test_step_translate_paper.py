"""steps/paper/step_04_translate_paper.py 的测试:论文翻译(非中文→中文译文)。"""

import json

from steps.paper.step_04_translate_paper import TranslatePaperStep
from tests.steps.conftest import make_step_config


def _setup(tmp_path):
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    for d in ["intermediate", "output", "logs"]:
        (job_dir / d).mkdir()
    sections = {
        "title": "AlpaServe", "authors": ["Z. Li"], "abstract": "Statistical multiplexing.",
        "sections": [{"level": 1, "title": "Introduction", "page": 1,
                      "text": "Model serving matters.", "children": []}],
        "total_sections": 1,
    }
    (job_dir / "intermediate" / "sections.json").write_text(json.dumps(sections))
    return job_dir


def test_validate_inputs_missing(tmp_path):
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    (job_dir / "intermediate").mkdir()
    config = make_step_config(tmp_path, step_name="04_translate_paper", pool="ai")
    step = TranslatePaperStep("04_translate_paper", job_dir, config)
    assert step.validate_inputs() == ["intermediate/sections.json"]


def test_paper_markdown_includes_title_and_sections(tmp_path):
    job_dir = _setup(tmp_path)
    sections = json.loads((job_dir / "intermediate" / "sections.json").read_text())
    md = TranslatePaperStep._paper_markdown(sections)
    assert "# AlpaServe" in md
    assert "Introduction" in md
    assert "Model serving matters." in md


def test_execute_writes_translated(tmp_path, monkeypatch):
    job_dir = _setup(tmp_path)
    config = make_step_config(tmp_path, step_name="04_translate_paper", pool="ai")
    step = TranslatePaperStep("04_translate_paper", job_dir, config)
    cap: dict = {}
    monkeypatch.setattr(step, "call_ai",
                        lambda prompt, **k: cap.update(p=prompt) or "# AlpaServe\n\n## 引言\n模型服务很重要。")
    result = step.execute()
    assert result["chars"] > 0
    out = (job_dir / "output" / "translated.md").read_text(encoding="utf-8")
    assert "模型服务很重要" in out
    assert "Model serving matters." in cap["p"]      # prompt 用了论文原文
