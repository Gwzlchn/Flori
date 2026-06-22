"""tests for steps/audio/ (03_transcript_parse / 04_smart_podcast / 05_review)"""

import json

import pytest

from steps.audio.step_03_transcript_parse import TranscriptParseStep, _join_cues
from steps.audio.step_04_smart_podcast import SmartPodcastStep
from steps.audio.step_05_review import PodcastReviewStep
from tests.steps.conftest import make_step_config


class TestJoinCues:
    """I-L17: 拼接字幕条目时英文补空格、CJK 直连(避免 ''.join 把英文词粘连)。"""

    def test_english_words_spaced(self):
        assert _join_cues(["hello", "world"]) == "hello world"

    def test_cjk_not_spaced(self):
        assert _join_cues(["大家好", "今天聊聊"]) == "大家好今天聊聊"

    def test_cross_script_no_space(self):
        assert _join_cues(["中文", "abc"]) == "中文abc"

    def test_skips_empty(self):
        assert _join_cues(["a", "", "b"]) == "a b"

# 跨越 60s 窗口的样例 SRT，预期聚合为 2 段
SRT = """\
1
00:00:01,000 --> 00:00:05,000
大家好欢迎收听本期播客

2
00:00:30,000 --> 00:00:40,000
今天聊聊机器学习

3
00:01:10,000 --> 00:01:20,000
首先是注意力机制

4
00:01:50,000 --> 00:02:00,000
谢谢大家收听
"""


def _mk_job(tmp_path):
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    for d in ["input", "intermediate", "output", "logs"]:
        (job_dir / d).mkdir()
    return job_dir


class TestTranscriptParseStep:
    def test_validate_missing(self, tmp_path):
        job_dir = _mk_job(tmp_path)
        config = make_step_config(tmp_path, step_name="03_transcript_parse")
        step = TranscriptParseStep("03_transcript_parse", job_dir, config)
        assert step.validate_inputs() == ["input/subtitle.srt"]

    def test_execute_aggregates(self, tmp_path):
        job_dir = _mk_job(tmp_path)
        (job_dir / "input" / "subtitle.srt").write_text(SRT)
        config = make_step_config(tmp_path, step_name="03_transcript_parse")
        step = TranscriptParseStep("03_transcript_parse", job_dir, config)
        result = step.execute()

        # 4 条字幕跨越 ~120s，按 60s 窗口应聚合为 2 段
        assert result["segments"] == 2
        assert result["duration_sec"] == 120.0

        transcript = json.loads((job_dir / "intermediate" / "transcript.json").read_text())
        assert len(transcript["segments"]) == 2
        assert "大家好" in transcript["full_text"]
        assert "谢谢大家收听" in transcript["full_text"]
        # 段落首尾时间合理
        assert transcript["segments"][0]["start"] == 1.0
        assert transcript["segments"][1]["end"] == 120.0

        # 同时产出可读逐字稿与 segments 雏形
        assert (job_dir / "output" / "transcript.md").exists()
        assert (job_dir / "intermediate" / "segments.json").exists()
        md = (job_dir / "output" / "transcript.md").read_text()
        assert "[00:01]" in md

    def test_empty_srt(self, tmp_path):
        job_dir = _mk_job(tmp_path)
        (job_dir / "input" / "subtitle.srt").write_text("")
        config = make_step_config(tmp_path, step_name="03_transcript_parse")
        step = TranscriptParseStep("03_transcript_parse", job_dir, config)
        result = step.execute()
        assert result["segments"] == 0
        assert result["duration_sec"] == 0.0

    def test_idempotent(self, tmp_path):
        job_dir = _mk_job(tmp_path)
        (job_dir / "input" / "subtitle.srt").write_text(SRT)
        config = make_step_config(tmp_path, step_name="03_transcript_parse")
        step = TranscriptParseStep("03_transcript_parse", job_dir, config)
        step.execute()
        step.mark_done()
        step2 = TranscriptParseStep("03_transcript_parse", job_dir, config)
        assert step2.should_run() is False


class TestSmartPodcastStep:
    def _setup(self, tmp_path):
        job_dir = _mk_job(tmp_path)
        transcript = {
            "segments": [
                {"start": 1.0, "end": 40.0, "text": "今天聊聊注意力机制"},
            ],
            "full_text": "今天聊聊注意力机制 attention",
            "duration_sec": 120.0,
        }
        (job_dir / "intermediate" / "transcript.json").write_text(json.dumps(transcript))
        return job_dir

    def test_validate_missing(self, tmp_path):
        job_dir = _mk_job(tmp_path)
        config = make_step_config(tmp_path, step_name="04_smart_podcast")
        step = SmartPodcastStep("04_smart_podcast", job_dir, config)
        assert step.validate_inputs() == ["intermediate/transcript.json"]

    def test_execute_dry_run(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DRY_RUN", "1")
        job_dir = self._setup(tmp_path)
        config = make_step_config(tmp_path, step_name="04_smart_podcast", pool="ai")
        step = SmartPodcastStep("04_smart_podcast", job_dir, config)
        result = step.execute()
        assert result["chars"] > 0
        assert list((job_dir / "output" / "versions").glob("notes_smart_*.md"))

    def test_build_prompt(self, tmp_path):
        job_dir = self._setup(tmp_path)
        config = make_step_config(tmp_path, step_name="04_smart_podcast")
        step = SmartPodcastStep("04_smart_podcast", job_dir, config)
        transcript = step.load_json("intermediate/transcript.json")
        prompt = step._build_prompt(transcript)
        assert "注意力机制" in prompt
        assert "口语" in prompt

    def test_input_hashes_includes_prompt(self, tmp_path):
        job_dir = self._setup(tmp_path)
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir(exist_ok=True)
        (prompts_dir / "04_smart_podcast.md").write_text("system prompt")
        config = make_step_config(tmp_path, step_name="04_smart_podcast", pool="ai")
        config["paths"]["prompts_dir"] = str(prompts_dir)
        step = SmartPodcastStep("04_smart_podcast", job_dir, config)
        hashes = step.input_hashes()
        assert "transcript" in hashes
        assert "prompt" in hashes
        assert "styles" in hashes


class TestPodcastReviewStep:
    def _setup(self, tmp_path):
        job_dir = _mk_job(tmp_path)
        transcript = {
            "segments": [],
            "full_text": "播客内容正文",
            "duration_sec": 120.0,
        }
        (job_dir / "intermediate" / "transcript.json").write_text(json.dumps(transcript))
        (job_dir / "output" / "versions").mkdir(exist_ok=True)
        (job_dir / "output" / "versions" / "notes_smart_anthropic_claude-sonnet-4-6_20260101-000000.md").write_text("## 播客笔记\n\n内容\n")
        return job_dir

    def test_validate_inputs(self, tmp_path):
        job_dir = _mk_job(tmp_path)
        config = make_step_config(tmp_path, step_name="05_review")
        step = PodcastReviewStep("05_review", job_dir, config)
        assert len(step.validate_inputs()) == 2

    def test_execute_dry_run(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DRY_RUN", "1")
        job_dir = self._setup(tmp_path)
        config = make_step_config(tmp_path, step_name="05_review", pool="ai")
        step = PodcastReviewStep("05_review", job_dir, config)
        result = step.execute()
        assert (job_dir / "output" / "review.json").exists()
        review = json.loads((job_dir / "output" / "review.json").read_text())
        assert "overall" in review
