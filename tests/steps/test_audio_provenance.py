"""音频 producer 到来源清单和笔记溯源清单的闭环测试。"""

from __future__ import annotations

import hashlib
import json

import pytest

from shared.note_text import markdown_to_index_text
from shared.provenance import validate_provenance_manifest
from steps.audio.step_03_transcript_parse import TranscriptParseStep
from steps.audio.step_04_smart_podcast import SmartPodcastStep
from tests.steps.conftest import make_job_dir, make_step_config


SRT = """1
00:00:01,250 --> 00:00:03,750
真实时间的第一段

2
00:01:05,500 --> 00:01:08,250
真实时间的第二段
"""


def _audio_job(tmp_path, *, duration_sec: float = 70.0):
    job_dir = make_job_dir(tmp_path, "input", "intermediate", "output", "logs", name="audio-job")
    media = job_dir / "input" / "source.mp3"
    media.write_bytes(b"real-audio-payload")
    (job_dir / "input" / "metadata.json").write_text(
        json.dumps({"duration_sec": duration_sec}), encoding="utf-8",
    )
    (job_dir / "input" / "subtitle.srt").write_text(SRT, encoding="utf-8")
    step = TranscriptParseStep(
        "03_transcript_parse",
        job_dir,
        make_step_config(tmp_path, step_name="03_transcript_parse", pipeline="audio"),
    )
    return job_dir, media, step


def test_audio_transcript_provenance_uses_real_media_and_is_byte_idempotent(tmp_path):
    job_dir, media, step = _audio_job(tmp_path)
    result = step.execute()
    assert result["source_segments"] == 2
    assert result["provenance_segments"] == 2

    source_path = job_dir / "intermediate" / "source_segments.json"
    source = json.loads(source_path.read_text(encoding="utf-8"))
    artifact = source["source_artifacts"][0]
    assert artifact["path"] == "input/source.mp3"
    assert artifact["sha256"] == hashlib.sha256(media.read_bytes()).hexdigest()
    assert artifact["media_duration_ms"] == 70_000
    assert [(item["locator"]["start_ms"], item["locator"]["end_ms"])
            for item in source["segments"]] == [(1_250, 3_750), (65_500, 68_250)]
    assert [item["support_text"] for item in source["segments"]] == [
        "真实时间的第一段", "真实时间的第二段",
    ]

    provenance_path = job_dir / "output" / "provenance" / "transcript.json"
    first_source = source_path.read_bytes()
    first_provenance = provenance_path.read_bytes()
    step.execute()
    assert source_path.read_bytes() == first_source
    assert provenance_path.read_bytes() == first_provenance

    manifest = json.loads(provenance_path.read_text(encoding="utf-8"))
    note_bytes = (job_dir / "output" / "transcript.md").read_bytes()
    with pytest.raises(ValueError, match="note_sha256 mismatch"):
        validate_provenance_manifest(
            manifest,
            source_manifest=source,
            note_bytes=note_bytes + b"tampered",
            normalized_body=markdown_to_index_text(note_bytes.decode("utf-8")),
        )


def test_audio_source_manifest_rejects_subtitle_beyond_measured_duration(tmp_path):
    _, _, step = _audio_job(tmp_path, duration_sec=68.0)
    with pytest.raises(ValueError, match="exceeds media_duration_ms"):
        step.execute()


def test_audio_smart_cleans_markers_but_persists_explicit_empty_mapping(tmp_path, monkeypatch):
    monkeypatch.delenv("DRY_RUN", raising=False)
    job_dir, _, parse_step = _audio_job(tmp_path)
    parse_step.execute()
    source = json.loads(
        (job_dir / "intermediate" / "source_segments.json").read_text(encoding="utf-8"),
    )
    segment_id = source["segments"][0]["segment_id"]
    source_token = segment_id.removeprefix("seg_")

    smart = SmartPodcastStep(
        "04_smart_podcast",
        job_dir,
        make_step_config(tmp_path, step_name="04_smart_podcast", pool="ai", pipeline="audio"),
    )
    cited_note = (
        "# 播客笔记\n\n"
        f"## 事实\n真实时间的第一段。[[source:{source_token}]]\n\n"
        + "## 展开\n这是用于通过智能笔记净化长度门的正文。\n" * 30
    )
    monkeypatch.setattr(smart.ai, "call", lambda *args, **kwargs: cited_note)
    result = smart.execute()
    assert result["provenance_status"] == "written_empty"
    assert result["provenance_segments"] == 0
    note_path = job_dir / result["note_file"]
    assert "[[source:" not in note_path.read_text(encoding="utf-8")
    cited = json.loads(
        (job_dir / "output" / "provenance" / "smart.json").read_text(encoding="utf-8"),
    )
    assert cited["segments"] == []

    no_ref_note = "# 播客笔记\n\n" + "## 正文\n没有可验证来源标记的正文。\n" * 30
    monkeypatch.setattr(smart.ai, "call", lambda *args, **kwargs: no_ref_note)
    result = smart.execute()
    assert result["provenance_status"] == "written_empty"
    assert result["provenance_segments"] == 0
    empty = json.loads(
        (job_dir / "output" / "provenance" / "smart.json").read_text(encoding="utf-8"),
    )
    assert empty["segments"] == []


def test_audio_smart_exact_quote_persists_mapping(tmp_path, monkeypatch):
    monkeypatch.delenv("DRY_RUN", raising=False)
    job_dir, _, parse_step = _audio_job(tmp_path)
    parse_step.execute()
    source = json.loads(
        (job_dir / "intermediate/source_segments.json").read_text(encoding="utf-8"),
    )
    segment = source["segments"][0]
    token = segment["segment_id"].removeprefix("seg_")
    claim = segment["support_text"]
    smart = SmartPodcastStep(
        "04_smart_podcast",
        job_dir,
        make_step_config(tmp_path, step_name="04_smart_podcast", pool="ai", pipeline="audio"),
    )
    note = (
        "# 播客笔记\n\n## 事实\n"
        f"{claim} [[source:{token}]]\n\n"
        + "## 展开\n这是用于通过智能笔记净化长度门的正文。\n" * 30
    )
    monkeypatch.setattr(smart.ai, "call", lambda *args, **kwargs: note)

    result = smart.execute()

    provenance = json.loads(
        (job_dir / "output/provenance/smart.json").read_text(encoding="utf-8"),
    )
    assert result["provenance_status"] == "written"
    assert result["provenance_segments"] == 1
    assert provenance["segments"][0]["anchor"] == claim
    assert provenance["segments"][0]["verification_policy"] == "exact_quote_v1"


def test_audio_smart_rejects_unknown_marker_fail_closed(tmp_path, monkeypatch):
    monkeypatch.delenv("DRY_RUN", raising=False)
    job_dir, _, parse_step = _audio_job(tmp_path)
    parse_step.execute()
    smart = SmartPodcastStep(
        "04_smart_podcast",
        job_dir,
        make_step_config(tmp_path, step_name="04_smart_podcast", pool="ai", pipeline="audio"),
    )
    bad = (
        "# 播客笔记\n\n"
        "## 事实\n伪造来源。[[source:0000000000000000000000000000000000000000000000000000000000000000]]\n"
        + "## 正文\n足够长的正文。\n" * 40
    )
    monkeypatch.setattr(smart.ai, "call", lambda *args, **kwargs: bad)
    with pytest.raises(ValueError, match="unknown source marker"):
        smart.execute()
    assert not (job_dir / "output" / "provenance" / "smart.json").exists()


def test_audio_smart_rejects_malformed_marker_fail_closed(tmp_path, monkeypatch):
    monkeypatch.delenv("DRY_RUN", raising=False)
    job_dir, _, parse_step = _audio_job(tmp_path)
    parse_step.execute()
    smart = SmartPodcastStep(
        "04_smart_podcast",
        job_dir,
        make_step_config(tmp_path, step_name="04_smart_podcast", pool="ai", pipeline="audio"),
    )
    malformed = (
        "# 播客笔记\n\n"
        "## 事实\n残缺来源。[[source:broken\n"
        + "## 正文\n足够长的正文。\n" * 40
    )
    monkeypatch.setattr(smart.ai, "call", lambda *args, **kwargs: malformed)

    with pytest.raises(ValueError, match="malformed source marker"):
        smart.execute()
    assert not (job_dir / "output" / "provenance" / "smart.json").exists()
