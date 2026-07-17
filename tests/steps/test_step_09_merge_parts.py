import hashlib
import json

import pytest
from PIL import Image

from steps.video.step_08_punctuate import PunctuateStep
from steps.video.step_09_merge_parts import MergePartsStep
from steps.video.step_09_mechanical import MechanicalStep
from tests.steps.conftest import make_step_config


SRT = """1
00:00:01,000 --> 00:00:03,000
内容
"""


@pytest.mark.parametrize("value", [None, "", "../escape.jpg", "nested/frame.jpg"])
def test_merge_rejects_non_basename_part_asset(value) -> None:
    with pytest.raises(ValueError, match="filename is invalid"):
        MergePartsStep._asset_name(value)


def test_merge_parts_preserves_local_locator_and_builds_global_timeline(
    tmp_path, monkeypatch,
) -> None:
    job_dir = tmp_path / "job_video"
    job_dir.mkdir()
    parts = [
        {"part_id": "pt_a", "part_index": 1, "title": "上半场"},
        {"part_id": "pt_b", "part_index": 2, "title": "下半场"},
    ]
    (job_dir / "job.json").write_text(
        json.dumps({"job_id": "job_video", "content_type": "video", "parts": parts}),
        encoding="utf-8",
    )
    monkeypatch.setenv("STEP_JOB_ID", "job_video")
    for part in parts:
        root = job_dir / "parts" / part["part_id"]
        for rel in ("input", "intermediate", "output", "assets", "logs"):
            (root / rel).mkdir(parents=True, exist_ok=True)
        (root / "input/source.mp4").write_bytes(part["part_id"].encode())
        (root / "input/metadata.json").write_text(
            json.dumps({"duration_sec": 10}), encoding="utf-8",
        )
        (root / "input/subtitle.srt").write_text(SRT, encoding="utf-8")
        frame = root / "assets/frame.jpg"
        Image.new("RGB", (40, 20), color="white").save(frame)
        frame_sha256 = hashlib.sha256(frame.read_bytes()).hexdigest()
        (root / "intermediate/dedup.json").write_text(json.dumps([{
            "index": 1, "filename": "frame.jpg", "timestamp_sec": 2, "keep": True,
        }]), encoding="utf-8")
        (root / "intermediate/ocr.json").write_text(json.dumps([{
            "index": 1, "filename": "frame.jpg", "timestamp_sec": 2,
            "asset_sha256": frame_sha256, "width": 40, "height": 20,
            "text": f"{part['title']}画面",
            "boxes": [{"text": f"{part['title']}画面", "box": [0, 0, 20, 10]}],
        }]), encoding="utf-8")
        (root / "intermediate/danmaku.json").write_text("[]", encoding="utf-8")
        monkeypatch.setenv("STEP_PART_ID", part["part_id"])
        step = PunctuateStep(
            "08_punctuate",
            root,
            make_step_config(
                tmp_path, step_name="08_punctuate", pool="ai", pipeline="video",
            ),
        )
        monkeypatch.setattr(
            step.ai,
            "call",
            lambda *args, part=part, **kwargs: (
                f"[00:01] {part['title']}内容"
            ),
        )
        step.execute()

    monkeypatch.delenv("STEP_PART_ID", raising=False)
    merge = MergePartsStep(
        "09_merge_parts",
        job_dir,
        make_step_config(
            tmp_path, step_name="09_merge_parts", pool="io", pipeline="video",
        ),
    )
    result = merge.execute()

    assert result["parts"] == 2
    transcript = (job_dir / "output/transcript.md").read_text(encoding="utf-8")
    assert "[00:01] 上半场内容" in transcript
    assert "[00:11] 下半场内容" in transcript
    source = json.loads(
        (job_dir / "intermediate/source_segments.json").read_text(encoding="utf-8")
    )
    media = [item["locator"] for item in source["segments"] if item["locator"]["kind"] == "media"]
    assert [(item["part_id"], item["start_ms"], item["timeline_start_ms"]) for item in media] == [
        ("pt_a", 1000, 1000),
        ("pt_b", 1000, 11000),
    ]
    images = [item for item in source["segments"] if item["locator"]["kind"] == "image"]
    assert [item["locator"]["asset_path"] for item in images] == [
        "parts/pt_a/assets/frame.jpg", "parts/pt_b/assets/frame.jpg",
    ]
    merged_ocr = json.loads((job_dir / "intermediate/ocr.json").read_text())
    assert [(item["filename"], item["part_filename"]) for item in merged_ocr] == [
        ("P01_frame.jpg", "frame.jpg"), ("P02_frame.jpg", "frame.jpg"),
    ]
    provenance = json.loads(
        (job_dir / "output/provenance/transcript.json").read_text(encoding="utf-8")
    )
    assert len(provenance["segments"]) == 2

    mechanical = MechanicalStep(
        "09_mechanical",
        job_dir,
        make_step_config(
            tmp_path, step_name="09_mechanical", pool="io", pipeline="video",
        ),
    )
    result = mechanical.execute()
    assert result["provenance_segments"] >= len(images)
    mechanical_provenance = json.loads(
        (job_dir / "output/provenance/mechanical.json").read_text(encoding="utf-8")
    )
    image_ids = {item["segment_id"] for item in images}
    assert image_ids <= {
        source_id
        for item in mechanical_provenance["segments"]
        for source_id in item["source_segment_ids"]
    }


def test_merge_builds_part_source_manifest_when_punctuate_was_skipped(
    tmp_path, monkeypatch,
) -> None:
    job_dir = tmp_path / "job_ocr_only"
    root = job_dir / "parts/pt_ocr"
    for rel in ("input", "intermediate", "output", "assets"):
        (root / rel).mkdir(parents=True, exist_ok=True)
    (job_dir / "job.json").write_text(json.dumps({
        "id": "job_ocr_only",
        "content_type": "video",
        "parts": [{"part_id": "pt_ocr", "part_index": 1, "title": "无字幕"}],
    }))
    (root / "input/source.mp4").write_bytes(b"video")
    (root / "input/metadata.json").write_text(json.dumps({"duration_sec": 5}))
    frame = root / "assets/frame.jpg"
    Image.new("RGB", (40, 20), color="white").save(frame)
    digest = hashlib.sha256(frame.read_bytes()).hexdigest()
    (root / "intermediate/dedup.json").write_text(json.dumps([{
        "index": 1, "filename": "frame.jpg", "timestamp_sec": 1, "keep": True,
    }]))
    (root / "intermediate/ocr.json").write_text(json.dumps([{
        "index": 1, "filename": "frame.jpg", "timestamp_sec": 1,
        "asset_sha256": digest, "width": 40, "height": 20, "text": "纯画面内容",
        "boxes": [{"text": "纯画面内容", "box": [0, 0, 20, 10]}],
    }]))
    (root / "intermediate/danmaku.json").write_text("[]")
    monkeypatch.setenv("STEP_JOB_ID", "job_ocr_only")

    merge = MergePartsStep(
        "09_merge_parts",
        job_dir,
        make_step_config(
            tmp_path, step_name="09_merge_parts", pool="io", pipeline="video",
        ),
    )
    result = merge.execute()

    assert result["parts"] == 1
    assert (job_dir / "output/transcript.md").read_text() == "# 逐字稿\n\n（无口播稿）\n"
    source = json.loads((job_dir / "intermediate/source_segments.json").read_text())
    assert source["segments"][0]["source_id"] == "part:pt_ocr"
    assert source["segments"][0]["locator"]["asset_path"] == (
        "parts/pt_ocr/assets/frame.jpg"
    )
