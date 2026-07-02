"""steps/paper/step_06_review.py 的测试。"""

import json

from steps.paper.step_06_review import PaperReviewStep
from tests.steps.conftest import make_step_config


class TestPaperReviewStep:
    def _setup_job(self, tmp_path):
        job_dir = tmp_path / "job"
        job_dir.mkdir()
        for d in ["intermediate", "output", "logs"]:
            (job_dir / d).mkdir()

        sections = {
            "title": "Test Paper",
            "sections": [
                {"level": 1, "title": "Intro", "page": 1, "text": "text", "children": []},
            ],
        }
        (job_dir / "intermediate" / "sections.json").write_text(json.dumps(sections))
        (job_dir / "intermediate" / "figures.json").write_text(json.dumps(
            [{"index": 1, "filename": "fig1.jpg", "caption": "Figure 1", "page": 1}]
        ))
        (job_dir / "output" / "versions").mkdir()
        (job_dir / "output" / "versions" / "notes_smart_anthropic_claude-sonnet-4-6_20260101-000000.md").write_text("## 论文笔记\n\n内容\n")
        return job_dir

    def test_validate_inputs(self, tmp_path):
        job_dir = tmp_path / "job"
        job_dir.mkdir()
        for d in ["intermediate", "output"]:
            (job_dir / d).mkdir()
        config = make_step_config(tmp_path, step_name="06_review")
        step = PaperReviewStep("06_review", job_dir, config)
        # 缺 smart note + sections.json + figures.json(figure_references 维度需 figures 真值)
        assert len(step.validate_inputs()) == 3

    def test_execute_dry_run(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DRY_RUN", "1")
        job_dir = self._setup_job(tmp_path)
        config = make_step_config(tmp_path, step_name="06_review", pool="ai")
        step = PaperReviewStep("06_review", job_dir, config)
        result = step.execute()
        assert (job_dir / "output" / "review.json").exists()
        review = json.loads((job_dir / "output" / "review.json").read_text())
        assert "overall" in review

    def test_parse_fallback(self, tmp_path, monkeypatch):
        # 非 DRY_RUN:AI 返回非 JSON → call_ai_json 走 fallback,overall 恒 3.0 + parse_failed。
        # DRY_RUN smoke 只断 "overall" in review,完全绕过了解析/兜底这条核心分支。
        monkeypatch.delenv("DRY_RUN", raising=False)
        job_dir = self._setup_job(tmp_path)
        config = make_step_config(tmp_path, step_name="06_review", pool="ai")
        step = PaperReviewStep("06_review", job_dir, config)
        monkeypatch.setattr(step, "call_ai", lambda *a, **k: "完全不是 JSON 的自然语言回复")
        result = step.execute()
        review = json.loads((job_dir / "output" / "review.json").read_text())
        assert review["overall"] == 3.0
        assert review["parse_failed"] is True
        assert result["parse_failed"] is True

    def test_aggregates_real_scores(self, tmp_path, monkeypatch):
        # 非 DRY_RUN:AI 返回合法多维评分 → overall 为维度均值,而非 fallback 恒 3.0。
        # 回归钉死评分聚合真跑了,防 overall 恒 3.0 的失效再现(聚合逻辑在 step_base.py)。
        monkeypatch.delenv("DRY_RUN", raising=False)
        job_dir = self._setup_job(tmp_path)
        config = make_step_config(tmp_path, step_name="06_review", pool="ai")
        step = PaperReviewStep("06_review", job_dir, config)
        scores = {"completeness": 5, "accuracy": 5, "structure": 5,
                  "terminology": 4, "formula_integrity": 4, "figure_references": 4,
                  "key_terms": [], "missing_concepts": [], "top3_improvements": []}
        monkeypatch.setattr(step, "call_ai", lambda *a, **k: json.dumps(scores))
        result = step.execute()
        review = json.loads((job_dir / "output" / "review.json").read_text())
        assert result["parse_failed"] is False
        assert review["overall"] == 4.5      # (5+5+5+4+4+4)/6 = 4.5,非 3.0
