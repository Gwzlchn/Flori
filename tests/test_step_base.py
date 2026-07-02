"""tests for shared/step_base.py"""

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from shared.errors import ProcessingError
from shared.step_base import StepBase, file_hash


# Test 子类

class DummyStep(StepBase):
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


class TestDefDigestHelpers:
    """def_digest_for / pipeline_digest_for 共享 helper(_def_digest 与"过期"判断共用,防漂移)。"""

    def test_def_digest_for_stable_and_sensitive(self):
        from shared.step_base import def_digest_for
        a = def_digest_for("1", {"primary": "x"})
        assert a.startswith("sha256:") and a == def_digest_for("1", {"primary": "x"})  # 稳定
        assert a != def_digest_for("2", {"primary": "x"})        # version 变
        assert a != def_digest_for("1", {"primary": "y"})        # ai 变
        assert def_digest_for(None, None) == def_digest_for("1", {})  # version 缺省="1"、ai 缺省={}

    def test_def_digest_for_matches_step_def_digest(self, tmp_path):
        # helper 必须与 StepBase._def_digest 逐字一致(单一来源)
        from shared.step_base import def_digest_for
        step = DummyStep(tmp_path, config={"step": {"version": "3"}, "ai": {"primary": "m"}})
        assert step._def_digest() == def_digest_for("3", {"primary": "m"})

    def test_pipeline_digest_for_aggregates(self):
        from shared.step_base import pipeline_digest_for
        steps = [{"name": "a", "version": "1", "ai": {}}, {"name": "b", "version": "1", "ai": {}}]
        d = pipeline_digest_for(steps)
        assert d.startswith("sha256:") and d == pipeline_digest_for(list(reversed(steps)))  # 与顺序无关
        bumped = [{"name": "a", "version": "2", "ai": {}}, {"name": "b", "version": "1", "ai": {}}]
        assert d != pipeline_digest_for(bumped)   # 任一步变 → 聚合变


class TestDefDigest:
    """pipeline 定义版本(version/ai 来自 YAML)进指纹 → 改 YAML 即重跑;旧 .done 向后兼容不强制重跑。"""

    def _mk(self, job_dir, version="1", ai=None):
        return DummyStep(job_dir, config={"step": {"version": version}, "ai": ai or {}})

    def _seed_input(self, tmp_path):
        (tmp_path / "input").mkdir(exist_ok=True)
        (tmp_path / "input" / "data.json").write_text('{"x": 1}')

    def test_def_digest_recorded_and_stable(self, tmp_path):
        self._seed_input(tmp_path)
        step = self._mk(tmp_path)
        assert step._def_digest().startswith("sha256:")
        assert self._mk(tmp_path)._def_digest() == step._def_digest()  # 同输入同 hash
        step.mark_done()
        content = json.loads((tmp_path / ".test_step.done").read_text())
        assert content["def_digest"] == step._def_digest()

    def test_rerun_when_version_bumped(self, tmp_path):
        self._seed_input(tmp_path)
        self._mk(tmp_path, version="1").mark_done()
        assert self._mk(tmp_path, version="1").should_run() is False  # 没改 → 跳过
        assert self._mk(tmp_path, version="2").should_run() is True   # bump version → 重跑

    def test_rerun_when_ai_model_changes(self, tmp_path):
        self._seed_input(tmp_path)
        self._mk(tmp_path, ai={"primary": "claude-x"}).mark_done()
        assert self._mk(tmp_path, ai={"primary": "claude-x"}).should_run() is False
        assert self._mk(tmp_path, ai={"primary": "claude-y"}).should_run() is True  # 换模型 → 重跑

    def test_legacy_done_without_def_digest_not_forced_rerun(self, tmp_path):
        # 旧 .done 没有 def_digest 键 → 不因新增字段而强制全量重跑。
        self._seed_input(tmp_path)
        step = self._mk(tmp_path)
        (tmp_path / ".test_step.done").write_text(json.dumps({
            "step": "test_step", "input_hashes": step.input_hashes(), "finished_at": "x",
        }))
        assert step.should_run() is False


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

    def test_failure_keeps_stderr_in_message(self, tmp_path):
        # 非零退出:仍是 CalledProcessError(子类),但 str() 带上 stderr,故障原因不被吞。
        step = DummyStep(tmp_path)
        with pytest.raises(subprocess.CalledProcessError) as ei:
            step.run_subprocess(["sh", "-c", "echo boom-detail >&2; exit 3"])
        assert "boom-detail" in str(ei.value)


class TestTerminologyBlock:
    def test_empty_when_no_terminology(self):
        assert StepBase.terminology_block({}) == ""
        assert StepBase.terminology_block({"terminology": []}) == ""
        assert StepBase.terminology_block(None) == ""

    def test_joins_and_caps_at_30(self):
        block = StepBase.terminology_block({"terminology": ["A", "B", "C"]})
        assert "本领域已沉淀的标准概念" in block and "A; B; C" in block
        big = StepBase.terminology_block({"terminology": [str(i) for i in range(40)]})
        assert "29" in big and "30" not in big   # 仅前 30 项(0..29)


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
    """经 cli_main 真跑一个 step 模块,覆盖 runner 命令 + config schema + StepBase 粘合缝。
    用纯 Python 的 03_article_sections(base 镜像即可跑),不依赖外部命令/AI。"""

    def test_cli_main_runs_real_step(self, tmp_path):
        for d in ["input", "intermediate", "output", "assets", "logs"]:
            (tmp_path / d).mkdir()
        # 03_article_sections 的输入:intermediate/parsed.json
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
            "step": {"name": "03_article_sections", "pool": "cpu", "timeout_sec": 60, "retries": 0},
            "ai": {}, "domain": {"name": "general"}, "style_tags": [],
            "paths": {"data_dir": str(tmp_path), "prompts_dir": str(prompts),
                      "config_dir": str(tmp_path)},
            "providers": {},
        }
        cfg_file = tmp_path / "step.config.json"
        cfg_file.write_text(json.dumps(cfg), encoding="utf-8")

        # 经真实入口 python -m steps.article.step_03_article_sections 跑(cli_main)
        import sys
        result = subprocess.run(
            [sys.executable, "-m", "steps.article.step_03_article_sections",
             "--job-dir", str(tmp_path), "--step-config", str(cfg_file)],
            capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 0, result.stderr
        out = tmp_path / "intermediate" / "sections.json"
        assert out.exists()
        data = json.loads(out.read_text(encoding="utf-8"))
        assert data["title"] == "测试文章"
        assert data["total_sections"] >= 2  # 引言 / 方法
        assert (tmp_path / ".03_article_sections.done").exists()  # 幂等标记


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


class TestSanitizeSmartNote:
    """落盘前净化 claude agentic 口水:剥开头汇报/结尾提议、判废退化输出(线上视觉笔记实测)。"""

    from shared.step_base import StepBase as _SB
    BODY = "\n\n".join(f"## 第{i}章\n这是正文内容,足够长以通过长度判废。" * 3 for i in range(1, 8))

    def test_strips_chinese_preamble_to_first_header(self):
        raw = ("已完成重组。结构化学习笔记已生成并保存到 `/tmp/flori-work/x/学习笔记.md`。\n\n"
               "我做的关键处理：\n- 按章节重组\n\n# 庄股操盘复盘\n\n" + self.BODY)
        out = self._SB._sanitize_smart_note(raw)
        assert out.startswith("# 庄股操盘复盘")
        assert "已完成" not in out and "/tmp/" not in out and "我做的关键处理" not in out

    def test_strips_english_preamble(self):
        raw = ("I've reviewed all 10 screenshots and will now restructure.\n\n"
               "# Deep Dive\n\n" + self.BODY)
        out = self._SB._sanitize_smart_note(raw)
        assert out.startswith("# Deep Dive")
        assert "I've reviewed" not in out

    def test_strips_trailing_offer(self):
        raw = "# 标题\n\n" + self.BODY + "\n\n---\n\n需要我把笔记导出为其他格式（如纯 Markdown）吗？"
        out = self._SB._sanitize_smart_note(raw)
        assert "需要我把笔记导出" not in out
        assert out.rstrip().endswith("足够长以通过长度判废。") or "第7章" in out

    def test_clean_note_unchanged(self):
        raw = "# 深度学习笔记\n\n" + self.BODY
        out = self._SB._sanitize_smart_note(raw)
        assert out == raw.strip()

    def test_strips_trailing_first_person_signoff(self):
        # BV144 实况:正文后 --- 接一段"我已按…重组…内嵌了 N 张截图"的第一人称收尾签名。
        raw = ("# 标题\n\n" + self.BODY +
               "\n\n---\n\n我已按视频自然章节（引子 → 出货 → 结尾）重组，专业术语就近标注简释并内嵌了 11 张截图。")
        out = self._SB._sanitize_smart_note(raw)
        assert "我已按" not in out and "内嵌了 11 张" not in out
        assert "第7章" in out                     # 正文保留

    def test_keeps_legit_third_person_ending(self):
        # 第三人称正文结尾不得误删(不以第一人称过程标记开头)。
        raw = "# 标题\n\n" + self.BODY + "\n\n本案的核心教训是:控盘资金再精妙也终将受罚。"
        out = self._SB._sanitize_smart_note(raw)
        assert "本案的核心教训" in out

    def test_degenerate_lost_note_raises(self):
        # BV1Msh 实况:claude 只回"已保存到 xx.md + 我做了什么"元汇报,正文整段丢失。
        raw = ("已完成。结构化学习笔记保存在 `/tmp/flori-work/x/notes_structured.md`。\n\n"
               "## 我做了什么\n1. 并行审阅了全部 86 张截图\n2. 按章节重组\n\n"
               "## 笔记结构一览\n- 本案速览表 + 关键时间线")
        with pytest.raises(ProcessingError):
            self._SB._sanitize_smart_note(raw)

    def test_too_short_raises(self):
        with pytest.raises(ProcessingError):
            self._SB._sanitize_smart_note("# 标题\n\n太短了。")

    def test_dry_run_zero_does_not_bypass(self, monkeypatch):
        # DRY_RUN="0"(显式关闭但存在)不能旁路净化——只有 =="1" 才旁路。
        monkeypatch.setenv("DRY_RUN", "0")
        with pytest.raises(ProcessingError):
            self._SB._sanitize_smart_note("# 标题\n\n太短了。")

    def test_dry_run_one_bypasses(self, monkeypatch):
        monkeypatch.setenv("DRY_RUN", "1")
        assert self._SB._sanitize_smart_note("# 标题\n\n太短了。") == "# 标题\n\n太短了。"


class TestScoreSalvage:
    """评审分数抽取健壮性:嵌套 scores 抬平 + JSON 非法时正则抢救 1-5 分,
    避免误落 fallback 的全 3(线上 11_review 实测 overall 恒 3.0 之因)。"""

    SCORE_KEYS = ["completeness", "accuracy", "structure",
                  "terminology", "visual_integration", "readability"]
    FALLBACK = {
        "completeness": 3, "accuracy": 3, "structure": 3,
        "terminology": 3, "visual_integration": 3, "readability": 3,
        "overall": 3.0, "missing_concepts": [], "top3_improvements": ["重试"],
    }
    # 6 维真分 → overall = (5+4+4+5+4+4)/6 = 4.3
    NESTED = ('{"scores": {"completeness": 5, "accuracy": 4, "structure": 4, '
              '"terminology": 5, "visual_integration": 4, "readability": 4}, '
              '"missing_concepts": ["X"], "top3_improvements": ["a"]}')
    # 上面再裹 ```json 围栏 + 追加未闭合的 rationale 长文本 → json.loads 必失败
    TRUNCATED = ('```json\n{"scores": {"completeness": 5, "accuracy": 4, "structure": 4, '
                 '"terminology": 5, "visual_integration": 4, "readability": 4}, '
                 '"rationale": {"completeness": "讲得很清楚')

    def test_salvage_from_truncated(self):
        assert StepBase._salvage_scores(self.TRUNCATED, self.SCORE_KEYS) == {
            "completeness": 5, "accuracy": 4, "structure": 4,
            "terminology": 5, "visual_integration": 4, "readability": 4,
        }

    def test_salvage_ignores_rationale_strings(self):
        # rationale 里同名键值是字符串("5 分太高"),数字正则不应误命中
        raw = '{"completeness": 2, "rationale": {"completeness": "5 分太高"}}'
        assert StepBase._salvage_scores(raw, ["completeness"]) == {"completeness": 2}

    def test_salvage_below_half_returns_none(self):
        # 命中不足半数(2/6)不可信 → None,走 fallback
        assert StepBase._salvage_scores('{"completeness": 5, "accuracy": 4}', self.SCORE_KEYS) is None

    def test_salvage_partial_above_half_fills_mean(self):
        # 命中 >= 半数(4/6):缺的维度按已命中均值 round 补齐,不整体落 fallback 全 3
        raw = '{"completeness": 5, "accuracy": 4, "structure": 4, "terminology": 5}'
        out = StepBase._salvage_scores(raw, self.SCORE_KEYS)
        assert out["completeness"] == 5 and out["terminology"] == 5
        # 缺的 visual_integration/readability 按 (5+4+4+5)/4=4.5→round=4 补齐
        assert out["visual_integration"] == 4 and out["readability"] == 4
        assert set(out) == set(self.SCORE_KEYS)

    def test_salvage_no_score_keys_returns_none(self):
        assert StepBase._salvage_scores('{"x": 1}', None) is None

    def test_call_ai_json_lifts_nested_scores(self, tmp_path):
        step = DummyStep(tmp_path)
        step.call_ai = lambda *a, **k: self.NESTED
        result, failed = step.call_ai_json("p", fallback=self.FALLBACK, score_keys=self.SCORE_KEYS)
        assert not failed
        assert result["completeness"] == 5 and result["terminology"] == 5
        assert result["overall"] == 4.3
        assert result["missing_concepts"] == ["X"]

    def test_call_ai_json_salvages_malformed(self, tmp_path):
        step = DummyStep(tmp_path)
        step.call_ai = lambda *a, **k: self.TRUNCATED
        result, failed = step.call_ai_json("p", fallback=self.FALLBACK, score_keys=self.SCORE_KEYS)
        assert not failed                       # 救回分数,不算 parse 失败
        assert result["overall"] == 4.3         # 真分均值,不是 fallback 的 3.0
        assert result.get("parse_failed") is not True

    def test_call_ai_json_fallback_when_unsalvageable(self, tmp_path):
        step = DummyStep(tmp_path)
        step.call_ai = lambda *a, **k: "完全不是 JSON 也没有分数"
        result, failed = step.call_ai_json("p", fallback=self.FALLBACK, score_keys=self.SCORE_KEYS)
        assert failed
        assert result["overall"] == 3.0
        assert result["parse_failed"] is True
