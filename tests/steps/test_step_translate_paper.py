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


def test_execute_small_paper_single_chunk(tmp_path, monkeypatch):
    # 小论文 fits:单 chunk = 行为与整篇单调用一致。
    job_dir = _setup(tmp_path)
    config = make_step_config(tmp_path, step_name="04_translate_paper", pool="ai")
    step = TranslatePaperStep("04_translate_paper", job_dir, config)
    calls = []
    monkeypatch.setattr(step, "call_ai", lambda prompt, **k: calls.append(prompt) or "译文")
    result = step.execute()
    assert result["chunks"] == 1 and len(calls) == 1


def test_execute_large_paper_chunks(tmp_path, monkeypatch):
    # 大论文(> CHUNK_CHARS):按段落边界切成多 chunk,逐块调用、按序聚合。
    import json as _json
    from steps.paper.step_04_translate_paper import CHUNK_CHARS

    job_dir = tmp_path / "job"
    job_dir.mkdir()
    for d in ["intermediate", "output", "logs"]:
        (job_dir / d).mkdir()
    secs = [{"level": 1, "title": f"Sec{i}", "page": i + 1,
             "text": f"S{i} " + ("word " * 2000), "children": []} for i in range(4)]
    (job_dir / "intermediate" / "sections.json").write_text(_json.dumps(
        {"title": "Big Paper", "authors": ["A"], "abstract": "Abs.",
         "sections": secs, "total_sections": 4}))

    config = make_step_config(tmp_path, step_name="04_translate_paper", pool="ai")
    step = TranslatePaperStep("04_translate_paper", job_dir, config)
    md = step._paper_markdown(_json.loads((job_dir / "intermediate" / "sections.json").read_text()))
    assert len(md) > CHUNK_CHARS  # 前提:确实超预算

    calls = []
    monkeypatch.setattr(step, "call_ai",
                        lambda prompt, **k: calls.append(prompt) or f"译文块{len(calls)}")
    result = step.execute()
    assert result["chunks"] > 1
    assert len(calls) == result["chunks"]          # 每 chunk 一次调用
    out = (job_dir / "output" / "translated.md").read_text(encoding="utf-8")
    for i in range(1, result["chunks"] + 1):
        assert f"译文块{i}" in out                  # 按序聚合,块块都在
    assert out.index("译文块1") < out.index(f"译文块{result['chunks']}")
