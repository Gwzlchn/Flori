"""将原型产物格式转换为新系统格式。"""

import json
import re
import sys
from pathlib import Path


def convert_job(src_dir: Path, dst_dir: Path) -> None:
    """把原型目录转换为新系统 job 目录格式。"""
    (dst_dir / "intermediate").mkdir(parents=True, exist_ok=True)
    (dst_dir / "assets").mkdir(parents=True, exist_ok=True)
    (dst_dir / "output").mkdir(parents=True, exist_ok=True)

    # scenes.json — 格式兼容,直接复制
    _copy_if(src_dir / "scenes.json", dst_dir / "intermediate" / "scenes.json")

    # frames/ → assets/
    frames_dir = src_dir / "frames"
    if frames_dir.exists():
        for jpg in sorted(frames_dir.glob("*.jpg")):
            (dst_dir / "assets" / jpg.name).write_bytes(jpg.read_bytes())

    # candidates.json — 原型格式转新格式
    src_cands = src_dir / "candidates.json"
    if src_cands.exists():
        old_cands = json.loads(src_cands.read_text())
        new_cands = []
        for i, c in enumerate(old_cands):
            filename = Path(c["path"]).name if "path" in c else c.get("filename", "")
            ts = c.get("timestamp", c.get("timestamp_sec", 0))
            new_cands.append({
                "index": i,
                "scene_index": i,
                "timestamp_sec": round(ts, 2),
                "filename": filename,
            })
        (dst_dir / "intermediate" / "candidates.json").write_text(
            json.dumps(new_cands, ensure_ascii=False, indent=2)
        )

    # dedup.json — 添加 index/filename/timestamp_sec
    src_dedup = src_dir / "dedup.json"
    if src_dedup.exists():
        old_dedup = json.loads(src_dedup.read_text())
        new_dedup = []
        for i, d in enumerate(old_dedup):
            filename = Path(d["path"]).name if "path" in d else d.get("filename", "")
            ts = d.get("timestamp", d.get("timestamp_sec", 0))
            new_dedup.append({
                "index": i,
                "scene_index": i,
                "timestamp_sec": round(ts, 2),
                "filename": filename,
                "keep": d.get("keep", True),
                "phash": d.get("phash", ""),
            })
        (dst_dir / "intermediate" / "dedup.json").write_text(
            json.dumps(new_dedup, ensure_ascii=False, indent=2)
        )

    # ocr.json — 转换字段名
    src_ocr = src_dir / "ocr.json"
    if src_ocr.exists():
        old_ocr = json.loads(src_ocr.read_text())
        new_ocr = []
        for i, o in enumerate(old_ocr):
            filename = Path(o["path"]).name if "path" in o else o.get("filename", "")
            ts = o.get("timestamp", o.get("timestamp_sec", 0))
            text = o.get("full_text", "") or " ".join(o.get("texts", []))
            new_ocr.append({
                "index": i,
                "filename": filename,
                "timestamp_sec": round(ts, 2),
                "text": text,
                "boxes": [],
            })
        (dst_dir / "intermediate" / "ocr.json").write_text(
            json.dumps(new_ocr, ensure_ascii=False, indent=2)
        )

    # danmaku.json — 格式兼容
    _copy_if(src_dir / "danmaku.json", dst_dir / "intermediate" / "danmaku.json")

    # transcript.md → output/
    _copy_if(src_dir / "transcript.md", dst_dir / "output" / "transcript.md")

    # notes (for reference/comparison only)
    _copy_if(src_dir / "notes_mechanical.md", dst_dir / "output" / "notes_mechanical.md.ref")
    _copy_if(src_dir / "notes_smart.md", dst_dir / "output" / "notes_smart.md.ref")
    _copy_if(src_dir / "review.json", dst_dir / "output" / "review.json.ref")

    print(f"转换完成: {src_dir.name}")
    print(f"  intermediate: {len(list((dst_dir / 'intermediate').iterdir()))} files")
    print(f"  assets: {len(list((dst_dir / 'assets').iterdir()))} files")
    print(f"  output: {len(list((dst_dir / 'output').iterdir()))} files")


def _copy_if(src: Path, dst: Path) -> None:
    if src.exists():
        dst.write_bytes(src.read_bytes())


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(f"用法: {sys.argv[0]} <原型目录> <job目录>")
        sys.exit(1)
    convert_job(Path(sys.argv[1]), Path(sys.argv[2]))
