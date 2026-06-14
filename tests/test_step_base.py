"""tests for shared/step_base.py"""

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from shared.step_base import StepBase, file_hash


# ── Test 子类 ──

class DummyStep(StepBase):
    """测试用子类。"""

    def __init__(self, job_dir, config=None, fail=False, result=None):
        super().__init__("test_step", job_dir, config or {})
        self._fail = fail
        self._result = result

    def execute(self):
        if self._fail:
            raise RuntimeError("boom")
        return self._result or {"items": 42}

    def validate_inputs(self):
        required = self.job_dir / "input" / "data.json"
        if not required.exists():
            return ["input/data.json"]
        return []

    def input_hashes(self):
        f = self.job_dir / "input" / "data.json"
        if f.exists():
            return {"data": file_hash(f)}
        return {}


class TestFileHash:
    def test_consistent(self, tmp_path):
        f = tmp_path / "a.txt"
        f.write_text("hello world")
        h1 = file_hash(f)
        h2 = file_hash(f)
        assert h1 == h2
        assert h1.startswith("sha256:")

    def test_different_content(self, tmp_path):
        f1 = tmp_path / "a.txt"
        f2 = tmp_path / "b.txt"
        f1.write_text("hello")
        f2.write_text("world")
        assert file_hash(f1) != file_hash(f2)


class TestShouldRun:
    def test_first_run(self, tmp_path):
        (tmp_path / "input").mkdir()
        (tmp_path / "input" / "data.json").write_text('{"x": 1}')
        step = DummyStep(tmp_path)
        assert step.should_run() is True

    def test_skip_when_up_to_date(self, tmp_path):
        (tmp_path / "input").mkdir()
        (tmp_path / "input" / "data.json").write_text('{"x": 1}')
        step = DummyStep(tmp_path)
        step.mark_done()
        assert step.should_run() is False

    def test_rerun_when_input_changes(self, tmp_path):
        (tmp_path / "input").mkdir()
        data_file = tmp_path / "input" / "data.json"
        data_file.write_text('{"x": 1}')

        step = DummyStep(tmp_path)
        step.mark_done()
        assert step.should_run() is False

        data_file.write_text('{"x": 2}')
        assert step.should_run() is True


class TestMarkDone:
    def test_writes_done_file(self, tmp_path):
        (tmp_path / "input").mkdir()
        (tmp_path / "input" / "data.json").write_text('{"x": 1}')
        step = DummyStep(tmp_path)
        step.mark_done()

        done_file = tmp_path / ".test_step.done"
        assert done_file.exists()
        content = json.loads(done_file.read_text())
        assert content["step"] == "test_step"
        assert "input_hashes" in content
        assert "finished_at" in content


class TestWriteOutput:
    def test_dict(self, tmp_path):
        step = DummyStep(tmp_path)
        step.write_output("output/result.json", {"key": "value"})
        f = tmp_path / "output" / "result.json"
        assert f.exists()
        assert json.loads(f.read_text()) == {"key": "value"}
        assert not (tmp_path / "output" / "result.json.tmp").exists()

    def test_string(self, tmp_path):
        step = DummyStep(tmp_path)
        step.write_output("output/notes.md", "# Hello\n")
        assert (tmp_path / "output" / "notes.md").read_text() == "# Hello\n"

    def test_bytes(self, tmp_path):
        step = DummyStep(tmp_path)
        step.write_output("output/data.bin", b"\x00\x01\x02")
        assert (tmp_path / "output" / "data.bin").read_bytes() == b"\x00\x01\x02"

    def test_creates_parent_dirs(self, tmp_path):
        step = DummyStep(tmp_path)
        step.write_output("deep/nested/file.txt", "content")
        assert (tmp_path / "deep" / "nested" / "file.txt").exists()


class TestWriteError:
    def test_format(self, tmp_path):
        step = DummyStep(tmp_path)
        step.write_error("ai", "rate limited", "traceback here")
        f = tmp_path / ".test_step.error.json"
        content = json.loads(f.read_text())
        assert content["step"] == "test_step"
        assert content["error_type"] == "ai"
        assert content["message"] == "rate limited"
        assert content["trace"] == "traceback here"
        assert "timestamp" in content


class TestWriteMeta:
    def test_format(self, tmp_path):
        step = DummyStep(tmp_path)
        step.write_meta({"status": "done", "items": 42})
        f = tmp_path / ".test_step.meta.json"
        content = json.loads(f.read_text())
        assert content["status"] == "done"
        assert content["items"] == 42


class TestReportProgress:
    def test_writes_progress_file(self, tmp_path):
        step = DummyStep(tmp_path)
        step.report_progress(50, 100, "halfway")
        f = tmp_path / ".test_step.progress"
        content = json.loads(f.read_text())
        assert content["source"] == "step"
        assert content["current"] == 50
        assert content["total"] == 100
        assert content["pct"] == 50
        assert content["message"] == "halfway"

    def test_zero_total(self, tmp_path):
        step = DummyStep(tmp_path)
        step.report_progress(0, 0)
        f = tmp_path / ".test_step.progress"
        content = json.loads(f.read_text())
        assert content["pct"] == 0


class TestRunFlow:
    def test_successful_run(self, tmp_path):
        (tmp_path / "input").mkdir()
        (tmp_path / "input" / "data.json").write_text('{"x": 1}')
        step = DummyStep(tmp_path)
        step.run()

        assert (tmp_path / ".test_step.done").exists()
        meta = json.loads((tmp_path / ".test_step.meta.json").read_text())
        assert meta["status"] == "done"
        assert meta["items"] == 42
        assert "duration_sec" in meta

    def test_skip_on_second_run(self, tmp_path):
        (tmp_path / "input").mkdir()
        (tmp_path / "input" / "data.json").write_text('{"x": 1}')

        step = DummyStep(tmp_path)
        step.run()
        first_done = (tmp_path / ".test_step.done").read_text()

        step2 = DummyStep(tmp_path)
        step2.run()
        assert (tmp_path / ".test_step.done").read_text() == first_done

    def test_failure_writes_error(self, tmp_path):
        (tmp_path / "input").mkdir()
        (tmp_path / "input" / "data.json").write_text('{"x": 1}')
        step = DummyStep(tmp_path, fail=True)
        with pytest.raises(SystemExit) as exc_info:
            step.run()
        assert exc_info.value.code == 1

        error = json.loads((tmp_path / ".test_step.error.json").read_text())
        assert error["error_type"] == "unknown"
        assert "boom" in error["message"]

    def test_missing_input_writes_error(self, tmp_path):
        step = DummyStep(tmp_path)
        with pytest.raises(SystemExit):
            step.run()
        error = json.loads((tmp_path / ".test_step.error.json").read_text())
        assert error["error_type"] == "input_missing"


class TestRunSubprocess:
    def test_success(self, tmp_path):
        step = DummyStep(tmp_path)
        result = step.run_subprocess(["echo", "hello"])
        assert result.returncode == 0
        assert "hello" in result.stdout

    def test_timeout(self, tmp_path):
        step = DummyStep(tmp_path)
        with pytest.raises(subprocess.TimeoutExpired):
            step.run_subprocess(["sleep", "10"], timeout=1)


class TestLoadJson:
    def test_load_json(self, tmp_path):
        (tmp_path / "output").mkdir()
        (tmp_path / "output" / "result.json").write_text('{"key": "value"}')
        step = DummyStep(tmp_path)
        data = step.load_json("output/result.json")
        assert data == {"key": "value"}

    def test_load_json_missing_file(self, tmp_path):
        step = DummyStep(tmp_path)
        with pytest.raises(FileNotFoundError):
            step.load_json("nonexistent.json")


class TestReportProgressExtra:
    def test_report_progress_over_total(self, tmp_path):
        """When current > total, pct should exceed 100 (no clamping in implementation)."""
        step = DummyStep(tmp_path)
        step.report_progress(150, 100, "over")
        f = tmp_path / ".test_step.progress"
        content = json.loads(f.read_text())
        # Implementation: round(100 * current / max(total, 1)) = round(100 * 150 / 100) = 150
        assert content["current"] == 150
        assert content["total"] == 100
        assert content["pct"] == 150
        assert content["message"] == "over"


class TestRunErrorPath:
    def test_run_step_error_writes_typed_error(self, tmp_path):
        """run() should write error.json with correct error_type on StepError."""
        from shared.errors import ProcessingError

        class FailingStep(StepBase):
            def execute(self):
                raise ProcessingError("disk full")

        (tmp_path / "input").mkdir()
        step = FailingStep("fail_step", tmp_path, {})
        with pytest.raises(SystemExit) as exc_info:
            step.run()
        assert exc_info.value.code == 1

        error = json.loads((tmp_path / ".fail_step.error.json").read_text())
        assert error["error_type"] == "processing"
        assert "disk full" in error["message"]


class TestRunMeta:
    def test_run_success_writes_meta_with_result(self, tmp_path):
        """run() should write meta with execute() return values merged in."""
        (tmp_path / "input").mkdir()
        (tmp_path / "input" / "data.json").write_text('{"x": 1}')
        step = DummyStep(tmp_path, result={"scenes": 10, "frames": 500})
        step.run()

        assert (tmp_path / ".test_step.done").exists()
        meta = json.loads((tmp_path / ".test_step.meta.json").read_text())
        assert meta["status"] == "done"
        assert meta["scenes"] == 10
        assert meta["frames"] == 500
        assert "duration_sec" in meta


class TestCallAI:
    def test_dry_run(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DRY_RUN", "1")
        step = DummyStep(tmp_path, config={
            "providers": {},
            "ai": {},
        })
        result = step.call_ai("hello")
        assert "[DRY_RUN]" in result

        usage_file = tmp_path / "logs" / ".test_step.usage.json"
        assert usage_file.exists()
        entries = json.loads(usage_file.read_text())
        assert len(entries) == 1
        assert entries[0]["provider"] == "dry-run"

    def test_full_flow_with_dry_run(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DRY_RUN", "1")

        class AIStep(StepBase):
            def execute(self):
                result = self.call_ai("test prompt")
                self.write_output("output/result.txt", result)
                return {"chars": len(result)}

        step = AIStep("test_ai", tmp_path, config={"providers": {}, "ai": {}})
        step.run()

        assert (tmp_path / "output" / "result.txt").exists()
        assert (tmp_path / "logs" / ".test_ai.usage.json").exists()
        assert (tmp_path / ".test_ai.done").exists()

        meta = json.loads((tmp_path / ".test_ai.meta.json").read_text())
        assert meta["status"] == "done"
        assert meta["chars"] > 0


class TestCliMainEndToEnd:
    """L12:经 cli_main 真跑一个 step 模块,覆盖 runner 命令 + config schema + StepBase 粘合缝。
    用纯 Python 的 17_article_sections(base 镜像即可跑),不依赖外部命令/AI。"""

    def test_cli_main_runs_real_step(self, tmp_path):
        for d in ["input", "intermediate", "output", "assets", "logs"]:
            (tmp_path / d).mkdir()
        # 17_article_sections 的输入:intermediate/parsed.json
        parsed = {
            "title": "测试文章",
            "authors": ["甲"],
            "abstract": "",
            "sections": [{"level": 1, "title": "正文", "page": 1,
                          "text": "# 引言\n这是引言段落。\n# 方法\n这是方法段落。"}],
            "text": "",
        }
        (tmp_path / "intermediate" / "parsed.json").write_text(
            json.dumps(parsed, ensure_ascii=False), encoding="utf-8")

        prompts = tmp_path / "prompts"
        prompts.mkdir(exist_ok=True)
        cfg = {
            "step": {"name": "17_article_sections", "pool": "cpu", "timeout_sec": 60, "retries": 0},
            "ai": {}, "domain": {"name": "general"}, "style_tags": [],
            "paths": {"data_dir": str(tmp_path), "prompts_dir": str(prompts),
                      "config_dir": str(tmp_path)},
            "providers": {},
        }
        cfg_file = tmp_path / "step.config.json"
        cfg_file.write_text(json.dumps(cfg), encoding="utf-8")

        # 经真实入口 python -m steps.article.step_17_article_sections 跑(cli_main)
        import sys
        result = subprocess.run(
            [sys.executable, "-m", "steps.article.step_17_article_sections",
             "--job-dir", str(tmp_path), "--step-config", str(cfg_file)],
            capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 0, result.stderr
        out = tmp_path / "intermediate" / "sections.json"
        assert out.exists()
        data = json.loads(out.read_text(encoding="utf-8"))
        assert data["title"] == "测试文章"
        assert data["total_sections"] >= 2  # 引言 / 方法
        assert (tmp_path / ".17_article_sections.done").exists()  # 幂等标记


class TestExtractJson:
    """call_ai_json 从 claude-cli 输出抽 JSON:剥 ```json 围栏 / 取首尾花括号。"""

    def test_fenced(self):
        from shared.step_base import StepBase
        assert json.loads(StepBase._extract_json('```json\n{"a": 1}\n```')) == {"a": 1}

    def test_prose_wrapped(self):
        from shared.step_base import StepBase
        assert json.loads(StepBase._extract_json('好的,结果如下:\n{"a": 1, "b": 2}\n以上。')) == {"a": 1, "b": 2}

    def test_plain(self):
        from shared.step_base import StepBase
        assert json.loads(StepBase._extract_json('{"a": 1}')) == {"a": 1}
