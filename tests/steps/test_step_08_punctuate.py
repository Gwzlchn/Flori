"""steps/video/step_08_punctuate.py 的测试。"""

import json
import os

import pytest

from steps.video.step_08_punctuate import PunctuateStep
from tests.steps.conftest import make_job_dir, make_step_config

SRT = """\
1
00:00:01,000 --> 00:00:03,000
你好世界

2
00:00:05,000 --> 00:00:08,000
这是测试
"""


class TestPunctuateStep:
    def _make(self, job_dir, tmp_path):
        config = make_step_config(tmp_path, step_name="08_punctuate", pool="ai")
        return PunctuateStep("08_punctuate", job_dir, config)

    def test_validate_missing(self, tmp_path):
        job_dir = tmp_path / "job"
        job_dir.mkdir()
        (job_dir / "input").mkdir()
        step = self._make(job_dir, tmp_path)
        assert step.validate_inputs() == ["input/*.srt"]

    def test_execute_dry_run(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DRY_RUN", "1")
        job_dir = make_job_dir(tmp_path, "input", "output", "logs")
        (job_dir / "input" / "subtitle.srt").write_text(SRT)

        step = self._make(job_dir, tmp_path)
        result = step.execute()
        assert result["lines"] == 2
        assert result["chunks"] == 1
        # DRY_RUN 下 AI 输出是占位串([DRY_RUN] ...),真实标点内容无法在此验证;
        # 那需要非 DRY_RUN 注入 fake 响应,属另一类用例。这里至少断产物非空,挡写出空文件。
        assert (job_dir / "output" / "transcript.md").read_text().strip()

    def test_chunking(self, tmp_path):
        step = PunctuateStep.__new__(PunctuateStep)
        text = "\n".join(f"line {i}" for i in range(20))
        chunks = step._split_chunks(text, max_chars=30)
        assert len(chunks) > 1
        rejoined = "\n".join(chunks)
        assert "line 0" in rejoined
        assert "line 19" in rejoined
