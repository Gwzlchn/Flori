"""Step 07: 机械版笔记。按时间线拼接截图+OCR+弹幕+逐字稿。"""

from __future__ import annotations

import json
from pathlib import Path

from shared.step_base import StepBase, file_hash
from steps.utils.srt_parser import load_srt, pick_native_srt


CHAPTER_INTERVAL_SEC = 180


class MechanicalStep(StepBase):
    def validate_inputs(self) -> list[str]:
        missing = []
        if not (self.job_dir / "intermediate" / "dedup.json").exists():
            missing.append("intermediate/dedup.json")
        if not (self.job_dir / "intermediate" / "ocr.json").exists():
            missing.append("intermediate/ocr.json")
        return missing

    def input_hashes(self) -> dict[str, str]:
        hashes = {
            "dedup": file_hash(self.job_dir / "intermediate" / "dedup.json"),
            "ocr": file_hash(self.job_dir / "intermediate" / "ocr.json"),
        }
        danmaku_path = self.job_dir / "intermediate" / "danmaku.json"
        if danmaku_path.exists():
            hashes["danmaku"] = file_hash(danmaku_path)
        transcript_path = self.job_dir / "output" / "transcript.md"
        if transcript_path.exists():
            hashes["transcript"] = file_hash(transcript_path)
        else:
            sub, is_zh = pick_native_srt(self.job_dir / "input")
            if sub and is_zh:  # 仅中文原生字幕可无 claude 直用;非中文等 06 翻译
                hashes["subtitle"] = file_hash(sub)
        return hashes

    def execute(self) -> dict | None:
        dedup = self.load_json("intermediate/dedup.json")
        ocr = self.load_json("intermediate/ocr.json")

        danmaku_path = self.job_dir / "intermediate" / "danmaku.json"
        danmaku = json.loads(danmaku_path.read_text()) if danmaku_path.exists() else []

        # 口播:优先 06 的中文稿(中文加标点/非中文已翻译);没有则直接读原始中文字幕(无需 claude
        # 先出可看的机械版)。非中文视频无中文稿时口播留空,等 06 翻译,不把外文塞进中文机械版。
        transcript_path = self.job_dir / "output" / "transcript.md"
        if transcript_path.exists():
            transcript_lines = self._parse_transcript(transcript_path)
        else:
            sub, is_zh = pick_native_srt(self.job_dir / "input")
            transcript_lines = (
                [{"time_sec": e.start_sec, "text": e.text} for e in load_srt(sub)]
                if sub and is_zh else []
            )

        kept_frames = [d for d in dedup if d.get("keep", False)]
        ocr_map = {o["index"]: o for o in ocr}

        events = self._build_timeline(kept_frames, ocr_map, danmaku, transcript_lines)
        md = self._render_markdown(events)

        self.write_output("output/notes_mechanical.md", md)
        return {"frames": len(kept_frames), "events": len(events)}

    def _build_timeline(self, frames, ocr_map, danmaku, transcript_lines):
        events = []

        for frame in frames:
            ts = frame["timestamp_sec"]
            ocr_entry = ocr_map.get(frame["index"], {})
            events.append({
                "time": ts,
                "type": "frame",
                "filename": frame["filename"],
                "ocr_text": ocr_entry.get("text", ""),
            })

        for d in danmaku:
            events.append({
                "time": d["time_sec"],
                "type": "danmaku",
                "text": d["text"],
            })

        for tl in transcript_lines:
            events.append({
                "time": tl["time_sec"],
                "type": "transcript",
                "text": tl["text"],
            })

        events.sort(key=lambda e: e["time"])
        return events

    def _render_markdown(self, events) -> str:
        """口播为主、画面/OCR 为辅:每章先出口播正文(与字幕一致),再附该时段的画面+OCR。"""
        if not events:
            return "# 机械版笔记\n\n（无内容）\n"

        has_transcript = any(e["type"] == "transcript" for e in events)
        parts = ["# 机械版笔记\n"]
        if not has_transcript:
            parts.append("\n> ⚠️ 本视频未取得字幕/口播稿，以下仅为画面 OCR。\n")

        # 按时间分章
        chapters: list[dict] = []
        last = -CHAPTER_INTERVAL_SEC
        cur: dict | None = None
        for e in events:
            if cur is None or e["time"] - last >= CHAPTER_INTERVAL_SEC:
                cur = {"ts": e["time"], "events": []}
                chapters.append(cur)
                last = e["time"]
            cur["events"].append(e)

        for idx, ch in enumerate(chapters, 1):
            m, s = divmod(int(ch["ts"]), 60)
            parts.append(f"\n## 第 {idx} 章 [{m:02d}:{s:02d}]\n")

            # 主体:口播(与字幕一致)
            spoken = [e["text"].strip() for e in ch["events"]
                      if e["type"] == "transcript" and e["text"].strip()]
            if spoken:
                parts.append("\n" + "".join(spoken) + "\n")

            # 辅助:该时段画面 + OCR(连续重复的 OCR 只显示一次)
            frames = [e for e in ch["events"] if e["type"] == "frame"]
            if frames:
                parts.append("\n**画面 / OCR（辅助）**\n")
                last_ocr = None
                for fr in frames:
                    parts.append(f"\n![{fr['filename']}](assets/{fr['filename']})\n")
                    ocr = (fr.get("ocr_text") or "").strip()
                    if ocr and ocr != last_ocr:
                        parts.append(f"\n> {ocr}\n")
                        last_ocr = ocr

            # 辅助:弹幕
            dm = [e["text"] for e in ch["events"] if e["type"] == "danmaku"]
            if dm:
                parts.append("\n💬 弹幕：" + " / ".join(dm[:20]) + "\n")

        return "".join(parts)

    def _parse_transcript(self, path: Path) -> list[dict]:
        import re
        lines = []
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            m = re.match(r"\[(\d{2}):(\d{2})\]\s*(.*)", line)
            if m:
                ts = int(m.group(1)) * 60 + int(m.group(2))
                lines.append({"time_sec": ts, "text": m.group(3)})
        return lines


if __name__ == "__main__":
    MechanicalStep.cli_main("07_mechanical")
