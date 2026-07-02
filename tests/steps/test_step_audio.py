"""steps/audio 各步(03_transcript_parse / 04_smart_podcast / 05_review)的测试。"""

import json

import pytest

from steps.audio.step_03_transcript_parse import TranscriptParseStep, _join_cues
from steps.audio.step_04_smart_podcast import SmartPodcastStep
from steps.audio.step_05_review import PodcastReviewStep
from tests.steps.conftest import make_step_config


class TestJoinCues:
    """拼接字幕条目时英文补空格、CJK 直连,避免 ''.join 把英文词粘连。"""

    def test_english_words_spaced(self):
        assert _join_cues(["hello", "world"]) == "hello world"

    def test_cjk_not_spaced(self):
        assert _join_cues(["大家好", "今天聊聊"]) == "大家好今天聊聊"

    def test_cross_script_no_space(self):
        assert _join_cues(["中文", "abc"]) == "中文abc"

    def test_skips_empty(self):
        assert _join_cues(["a", "", "b"]) == "a b"

# 跨越 60s 窗口的样例 SRT,预期聚合为 2 段
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

        # 4 条字幕跨越 ~120s,按 60s 窗口应聚合为 2 段
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

    def test_execute_real_path_sanitizes(self, tmp_path, monkeypatch):
        # 非 DRY_RUN:驱动 write_smart_note 的 _sanitize_smart_note(去 agentic 壳 + 补 assets/ 前缀)。
        # DRY_RUN smoke 只断 chars>0,这些净化逻辑全被绕过。
        monkeypatch.delenv("DRY_RUN", raising=False)
        job_dir = self._setup(tmp_path)
        config = make_step_config(tmp_path, step_name="04_smart_podcast", pool="ai")
        step = SmartPodcastStep("04_smart_podcast", job_dir, config)
        note = (
            "已完成播客笔记,思路如下:\n\n"                   # agentic 开头 → 应被净化砍到首个标题
            "# 播客笔记\n\n"
            "![配图](pic.png)\n\n"                          # 裸文件名 → 补 assets/ 前缀
            + "## 正文\n足够长的真实正文以通过净化长度判废。\n" * 30
        )
        monkeypatch.setattr(step, "call_ai", lambda *a, **k: note)
        step.execute()
        written = next(
            (job_dir / "output" / "versions").glob("notes_smart_*.md")
        ).read_text(encoding="utf-8")
        assert "已完成播客笔记" not in written            # agentic 开头被净化
        assert "![配图](assets/pic.png)" in written        # 裸文件名补了 assets/ 前缀
        assert "## 正文" in written

    def test_build_prompt(self, tmp_path):
        job_dir = self._setup(tmp_path)
        config = make_step_config(tmp_path, step_name="04_smart_podcast")
        step = SmartPodcastStep("04_smart_podcast", job_dir, config)
        transcript = step.load_json("intermediate/transcript.json")
        prompt = step._build_prompt(transcript)
        assert "注意力机制" in prompt
        assert "口语" in prompt

    def test_build_prompt_no_truncation(self, tmp_path):
        # 单次路径全量喂入不截断,正文结尾也要出现在 prompt 里。
        from steps.audio.step_04_smart_podcast import SINGLE_PASS_CHAR_LIMIT
        job_dir = _mk_job(tmp_path)
        body = "中" * (SINGLE_PASS_CHAR_LIMIT - 100) + "末尾标记ZZ"
        transcript = {"segments": [], "full_text": body, "duration_sec": 600.0}
        (job_dir / "intermediate" / "transcript.json").write_text(json.dumps(transcript))
        config = make_step_config(tmp_path, step_name="04_smart_podcast")
        step = SmartPodcastStep("04_smart_podcast", job_dir, config)
        prompt = step._build_prompt(transcript)
        assert "末尾标记ZZ" in prompt          # 全量,未被截断

    def test_chunk_segments_splits_long(self, tmp_path):
        from steps.audio.step_04_smart_podcast import MAP_CHUNK_CHARS
        transcript = {"segments": [{"start": 0.0, "end": 60.0, "text": "内" * 1000}
                                   for _ in range(40)],
                      "full_text": "内" * 40000, "duration_sec": 2400.0}
        chunks = SmartPodcastStep._chunk_segments(transcript, MAP_CHUNK_CHARS)
        assert len(chunks) >= 2
        assert all(len(c) <= MAP_CHUNK_CHARS + 1000 for c in chunks)  # 不超 budget(容一段)

    def test_chunk_segments_fallback_no_segments(self, tmp_path):
        transcript = {"segments": [], "full_text": "X" * 5000, "duration_sec": 60.0}
        chunks = SmartPodcastStep._chunk_segments(transcript, 2000)
        assert len(chunks) == 3  # 5000 / 2000 → 3 段

    def test_execute_single_pass_one_call(self, tmp_path, monkeypatch):
        # 短集:单次成稿,只调一次 call_ai,mode=single。
        monkeypatch.delenv("DRY_RUN", raising=False)
        job_dir = self._setup(tmp_path)            # full_text 很短
        config = make_step_config(tmp_path, step_name="04_smart_podcast", pool="ai")
        step = SmartPodcastStep("04_smart_podcast", job_dir, config)
        note = "# 播客笔记\n\n" + "## 章节\n足够长的真实正文以通过净化长度判废。\n" * 30
        calls = {"n": 0}

        def fake_ai(prompt, **k):
            calls["n"] += 1
            return note

        monkeypatch.setattr(step, "call_ai", fake_ai)
        result = step.execute()
        assert result["mode"] == "single"
        assert result["chunks"] == 1
        assert calls["n"] == 1

    def test_execute_map_reduce_covers_full(self, tmp_path, monkeypatch):
        # 长集:full_text 超阈值 → map-reduce,call_ai 调用 = chunks + 1(各段 map + 一次 reduce)。
        from steps.audio.step_04_smart_podcast import SINGLE_PASS_CHAR_LIMIT
        monkeypatch.delenv("DRY_RUN", raising=False)
        job_dir = _mk_job(tmp_path)
        seg_text = "这是一段较长的中文播客转写内容用于测试分段。" * 45   # ~990 字/段
        segments = [{"start": float(i * 60), "end": float(i * 60 + 60), "text": seg_text}
                    for i in range(30)]
        transcript = {"segments": segments, "full_text": seg_text * 30, "duration_sec": 1800.0}
        assert len(transcript["full_text"]) > SINGLE_PASS_CHAR_LIMIT
        (job_dir / "intermediate" / "transcript.json").write_text(json.dumps(transcript))
        config = make_step_config(tmp_path, step_name="04_smart_podcast", pool="ai")
        step = SmartPodcastStep("04_smart_podcast", job_dir, config)
        note = "# 播客笔记\n\n" + "## 章节\n足够长的真实正文以通过净化长度判废。\n" * 30
        calls = {"n": 0}

        def fake_ai(prompt, **k):
            calls["n"] += 1
            return note

        monkeypatch.setattr(step, "call_ai", fake_ai)
        result = step.execute()
        assert result["mode"] == "map_reduce"
        assert result["chunks"] >= 2
        assert calls["n"] == result["chunks"] + 1         # map×chunks + reduce×1
        assert list((job_dir / "output" / "versions").glob("notes_smart_*.md"))

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

    def test_parse_fallback(self, tmp_path, monkeypatch):
        # 非 DRY_RUN:AI 返回非 JSON → 走 fallback,overall 恒 3.0 + parse_failed。
        monkeypatch.delenv("DRY_RUN", raising=False)
        job_dir = self._setup(tmp_path)
        config = make_step_config(tmp_path, step_name="05_review", pool="ai")
        step = PodcastReviewStep("05_review", job_dir, config)
        monkeypatch.setattr(step, "call_ai", lambda *a, **k: "不是 JSON")
        result = step.execute()
        review = json.loads((job_dir / "output" / "review.json").read_text())
        assert review["overall"] == 3.0
        assert review["parse_failed"] is True
        assert result["parse_failed"] is True

    def test_aggregates_real_scores(self, tmp_path, monkeypatch):
        # 非 DRY_RUN:合法多维评分 → overall 为均值(而非恒 3.0),钉死评分聚合真跑了。
        monkeypatch.delenv("DRY_RUN", raising=False)
        job_dir = self._setup(tmp_path)
        config = make_step_config(tmp_path, step_name="05_review", pool="ai")
        step = PodcastReviewStep("05_review", job_dir, config)
        scores = {"completeness": 5, "accuracy": 5, "structure": 5,
                  "terminology": 4, "conciseness": 4, "readability": 4,
                  "key_terms": [], "missing_concepts": [], "top3_improvements": []}
        monkeypatch.setattr(step, "call_ai", lambda *a, **k: json.dumps(scores))
        result = step.execute()
        review = json.loads((job_dir / "output" / "review.json").read_text())
        assert result["parse_failed"] is False
        assert review["overall"] == 4.5      # (5+5+5+4+4+4)/6 = 4.5,非 3.0
