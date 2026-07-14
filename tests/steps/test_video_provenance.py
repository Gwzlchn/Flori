"""视频 producer 的字幕、机械稿和智能稿溯源闭环测试。"""

from __future__ import annotations

import hashlib
import json

import pytest
from PIL import Image

from steps.utils.srt_parser import SrtEntry
from steps.video.provenance import (
    build_video_source_manifest,
    mechanical_ocr_provenance_segments,
)
from steps.video.step_08_punctuate import PunctuateStep
from steps.video.step_09_mechanical import MechanicalStep
from steps.video.step_11_smart import SmartStep
from tests.steps.conftest import make_job_dir, make_step_config


SRT = """1
00:00:01,250 --> 00:00:03,750
第一段字幕

2
00:00:05,500 --> 00:00:08,250
第二段字幕
"""


def _job(tmp_path, duration=10.0):
    job_dir = make_job_dir(
        tmp_path, "input", "intermediate", "output", "assets", "logs", name="video-job",
    )
    (job_dir / "input" / "source.mp4").write_bytes(b"real-video-payload")
    (job_dir / "input" / "metadata.json").write_text(json.dumps({"duration_sec": duration}))
    (job_dir / "input" / "subtitle.srt").write_text(SRT)
    return job_dir


def _write_image(path, *, size=(40, 20), color="white") -> str:
    Image.new("RGB", size, color=color).save(path)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_video_subtitle_mechanical_and_smart_provenance(tmp_path, monkeypatch):
    monkeypatch.delenv("DRY_RUN", raising=False)
    job_dir = _job(tmp_path)
    punctuate = PunctuateStep(
        "08_punctuate", job_dir,
        make_step_config(tmp_path, step_name="08_punctuate", pool="ai", pipeline="video"),
    )
    monkeypatch.setattr(
        punctuate.ai, "call",
        lambda *args, **kwargs: "[00:01] 第一段字幕。\n[00:05] 第二段字幕。",
    )
    result = punctuate.execute()
    assert result["source_segments"] == 2
    assert result["provenance_segments"] == 2
    source_path = job_dir / "intermediate" / "source_segments.json"
    transcript_provenance = job_dir / "output" / "provenance" / "transcript.json"
    first = (source_path.read_bytes(), transcript_provenance.read_bytes())
    punctuate.execute()
    assert (source_path.read_bytes(), transcript_provenance.read_bytes()) == first

    (job_dir / "intermediate" / "dedup.json").write_text("[]")
    (job_dir / "intermediate" / "ocr.json").write_text("[]")
    mechanical = MechanicalStep(
        "09_mechanical", job_dir,
        make_step_config(tmp_path, step_name="09_mechanical", pool="io", pipeline="video"),
    )
    result = mechanical.execute()
    assert result["provenance_segments"] == 1
    assert (job_dir / "output" / "provenance" / "mechanical.json").exists()

    source = json.loads(source_path.read_text())
    assert [item["support_text"] for item in source["segments"]] == [
        "第一段字幕", "第二段字幕",
    ]
    token = source["segments"][0]["segment_id"].removeprefix("seg_")
    smart = SmartStep(
        "11_smart", job_dir,
        make_step_config(tmp_path, step_name="11_smart", pool="ai", pipeline="video"),
    )
    note = (
        "# 视频笔记\n\n"
        f"## 事实\n第一段字幕说明了核心事实。[[source:{token}]]\n\n"
        + "## 展开\n这是用于通过智能笔记净化长度门的正文。\n" * 30
    )
    monkeypatch.setattr(smart.ai, "call", lambda *args, **kwargs: note)
    result = smart.execute()
    assert result["provenance_status"] == "written_empty"
    assert result["provenance_segments"] == 0
    assert "[[source:" not in (job_dir / result["note_file"]).read_text()
    smart_provenance = json.loads(
        (job_dir / "output" / "provenance" / "smart.json").read_text(),
    )
    assert smart_provenance["segments"] == []


def test_video_smart_exact_quote_persists_mapping(tmp_path, monkeypatch):
    monkeypatch.delenv("DRY_RUN", raising=False)
    job_dir = _job(tmp_path)
    punctuate = PunctuateStep(
        "08_punctuate", job_dir,
        make_step_config(tmp_path, step_name="08_punctuate", pool="ai", pipeline="video"),
    )
    monkeypatch.setattr(
        punctuate.ai, "call", lambda *args, **kwargs: "[00:01] 第一段字幕\n",
    )
    punctuate.execute()
    (job_dir / "output/notes_mechanical.md").write_text("# 机械稿\n")
    (job_dir / "intermediate/dedup.json").write_text("[]")
    (job_dir / "intermediate/ocr.json").write_text("[]")
    source = json.loads(
        (job_dir / "intermediate/source_segments.json").read_text(encoding="utf-8"),
    )
    segment = source["segments"][0]
    token = segment["segment_id"].removeprefix("seg_")
    claim = segment["support_text"]
    smart = SmartStep(
        "11_smart", job_dir,
        make_step_config(tmp_path, step_name="11_smart", pool="ai", pipeline="video"),
    )
    note = (
        "# 视频笔记\n\n## 事实\n"
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


def test_video_smart_rejects_malformed_marker_fail_closed(tmp_path, monkeypatch):
    job_dir = _job(tmp_path)
    punctuate = PunctuateStep(
        "08_punctuate", job_dir,
        make_step_config(tmp_path, step_name="08_punctuate", pool="ai", pipeline="video"),
    )
    monkeypatch.setattr(punctuate.ai, "call", lambda *args, **kwargs: "[00:01] 第一段字幕\n")
    punctuate.execute()
    (job_dir / "output" / "notes_mechanical.md").write_text("# 机械稿\n")
    (job_dir / "intermediate" / "dedup.json").write_text("[]")
    (job_dir / "intermediate" / "ocr.json").write_text("[]")
    smart = SmartStep(
        "11_smart", job_dir,
        make_step_config(tmp_path, step_name="11_smart", pool="ai", pipeline="video"),
    )
    malformed = (
        "# 视频笔记\n\n"
        "## 事实\n残缺来源。[[source:broken\n\n"
        + "## 展开\n这是用于通过智能笔记净化长度门的正文。\n" * 30
    )
    monkeypatch.setattr(smart.ai, "call", lambda *args, **kwargs: malformed)

    with pytest.raises(ValueError, match="malformed source marker"):
        smart.execute()
    assert not (job_dir / "output" / "provenance" / "smart.json").exists()


def test_video_rejects_subtitle_beyond_measured_duration(tmp_path, monkeypatch):
    job_dir = _job(tmp_path, duration=8.0)
    step = PunctuateStep(
        "08_punctuate", job_dir,
        make_step_config(tmp_path, step_name="08_punctuate", pool="ai", pipeline="video"),
    )
    monkeypatch.setattr(step.ai, "call", lambda *args, **kwargs: "[00:01] 第一段\n")
    with pytest.raises(ValueError, match="exceeds media_duration_ms"):
        step.execute()


def test_video_ocr_image_segments_use_real_frame_and_skip_invalid_entries(tmp_path):
    job_dir = _job(tmp_path)
    frame = job_dir / "assets" / "frame-0001.jpg"
    frame_sha256 = _write_image(frame)
    (job_dir / "escape.jpg").write_bytes(b"must-not-be-hashed")
    ocr = [
        {
            "filename": frame.name,
            "timestamp_sec": 1.25,
            "asset_sha256": frame_sha256,
            "width": 40,
            "height": 20,
            "text": "唯一画面文字",
            "boxes": [{
                "text": "唯一画面文字",
                "confidence": 0.99,
                "box": [[5, 8], [1, 8], [1, 2], [5, 2]],
            }],
        },
        {"filename": "../escape.jpg", "timestamp_sec": 2,
         "asset_sha256": "0" * 64, "width": 40, "height": 20, "boxes": [
            {"text": "逃逸", "box": [0, 0, 10, 10]},
        ]},
        {"filename": "missing.jpg", "timestamp_sec": 3,
         "asset_sha256": "0" * 64, "width": 40, "height": 20, "boxes": [
            {"text": "缺图", "box": [0, 0, 10, 10]},
        ]},
        {"filename": frame.name, "timestamp_sec": 4,
         "asset_sha256": frame_sha256, "width": 40, "height": 20, "boxes": [
            {"text": "坏框", "box": [3, 3, 1, 1]},
        ]},
        {"filename": frame.name, "timestamp_sec": 5,
         "asset_sha256": frame_sha256, "width": 40, "height": 20, "boxes": [
            {"text": "超尺寸框", "box": [0, 0, 41, 10]},
        ]},
        {"filename": frame.name, "timestamp_sec": 6,
         "asset_sha256": "f" * 64, "width": 40, "height": 20, "boxes": [
            {"text": "哈希不符", "box": [0, 0, 10, 10]},
        ]},
        {"filename": frame.name, "timestamp_sec": 7, "boxes": [
            {"text": "旧格式", "box": [0, 0, 10, 10]},
        ]},
        {"filename": frame.name, "timestamp_sec": 10,
         "asset_sha256": frame_sha256, "width": 40, "height": 20, "boxes": [
            {"text": "越界时间", "box": [0, 0, 10, 10]},
        ]},
    ]
    (job_dir / "intermediate" / "ocr.json").write_text(json.dumps(ocr))

    manifest = build_video_source_manifest(job_dir, [])

    assert manifest is not None
    assert manifest["source_artifacts"][0]["path"] == "input/source.mp4"
    images = [s for s in manifest["segments"] if s["locator"]["kind"] == "image"]
    assert len(images) == 1
    locator = images[0]["locator"]
    assert locator == {
        "kind": "image",
        "asset_path": "assets/frame-0001.jpg",
        "asset_sha256": frame_sha256,
        "bbox": [1, 2, 5, 8],
        "start_ms": 1250,
        "end_ms": 1251,
        "page": None,
    }
    assert images[0]["support_text"] == "唯一画面文字"


def test_video_ocr_rejects_frame_replaced_after_scan_without_losing_media(tmp_path):
    job_dir = _job(tmp_path)
    frame = job_dir / "assets" / "frame.jpg"
    scanned_sha256 = _write_image(frame, color="white")
    ocr = [{
        "filename": frame.name,
        "timestamp_sec": 1.5,
        "asset_sha256": scanned_sha256,
        "width": 40,
        "height": 20,
        "text": "旧帧文字",
        "boxes": [{"text": "旧帧文字", "box": [0, 0, 10, 10]}],
    }]
    (job_dir / "intermediate" / "ocr.json").write_text(json.dumps(ocr))
    _write_image(frame, color="black")

    manifest = build_video_source_manifest(
        job_dir, [SrtEntry(1, 1.25, 3.75, "第一段字幕")],
    )

    assert manifest is not None
    assert [s["locator"]["kind"] for s in manifest["segments"]] == ["media"]


def test_mechanical_ocr_mapping_requires_rendered_frame_and_unique_text(tmp_path):
    job_dir = _job(tmp_path)
    first = job_dir / "assets" / "first.jpg"
    second = job_dir / "assets" / "second.jpg"
    first_sha256 = _write_image(first)
    second_sha256 = _write_image(second, color="gray")
    ocr = [
        {
            "filename": first.name,
            "timestamp_sec": 1,
            "asset_sha256": first_sha256,
            "width": 40,
            "height": 20,
            "boxes": [
                {"text": "唯一 OCR", "box": [0, 0, 10, 10]},
                {"text": "重复 OCR", "box": [10, 0, 20, 10]},
                {"text": "只在口播", "box": [20, 0, 30, 10]},
            ],
        },
        {
            "filename": second.name,
            "timestamp_sec": 2,
            "asset_sha256": second_sha256,
            "width": 40,
            "height": 20,
            "boxes": [{"text": "唯一 OCR", "box": [0, 0, 10, 10]}],
        },
    ]
    (job_dir / "intermediate" / "ocr.json").write_text(json.dumps(ocr))
    manifest = build_video_source_manifest(job_dir, [])
    assert manifest is not None

    mappings = mechanical_ocr_provenance_segments(
        ocr,
        manifest,
        "唯一 OCR\n重复 OCR\n重复 OCR\n只在口播",
        rendered_markdown="![00:01](assets/first.jpg)\n\n> OCR：唯一 OCR 重复 OCR",
    )

    assert [mapping["anchor"] for mapping in mappings] == ["唯一 OCR"]
    first_image_id = next(
        segment["segment_id"] for segment in manifest["segments"]
        if segment["locator"].get("asset_path") == "assets/first.jpg"
        and segment["locator"]["bbox"] == [0, 0, 10, 10]
    )
    assert mappings[0]["source_segment_ids"] == [first_image_id]
