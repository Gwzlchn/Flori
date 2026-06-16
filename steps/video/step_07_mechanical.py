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

    # 渲染版本:渲染逻辑变了但输入文件没变时,bump 这个值让幂等失效、强制重渲染。
    RENDER_VERSION = "v2-sections"

    def input_hashes(self) -> dict[str, str]:
        hashes = {
            "render": self.RENDER_VERSION,
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

    @staticmethod
    def _ts(sec: float) -> str:
        m, s = divmod(int(sec), 60)
        return f"{m:02d}:{s:02d}"

    def _render_markdown(self, events) -> str:
        """口播全文为主体(与字幕一字不差、连续不打断),画面/OCR、弹幕作为独立附录在后。
        此前每章口播被大段 OCR 截断,看起来像「掐头去尾」;现按内容分区,口播完整连读。"""
        if not events:
            return "# 机械版笔记\n\n（无内容）\n"

        transcript = [e for e in events if e["type"] == "transcript" and e["text"].strip()]
        frames = [e for e in events if e["type"] == "frame"]
        danmaku = [e for e in events if e["type"] == "danmaku" and e["text"].strip()]
        parts = ["# 机械版笔记\n"]

        # ── 主体:口播全文(与字幕完全一致)。按 CHAPTER_INTERVAL 加时间小标题便于导航,
        #     段内每 ~30s 空行分段;全程不插画面,保证口播连续可读。 ──
        parts.append("\n## 口播全文\n")
        if transcript:
            last_head = -CHAPTER_INTERVAL_SEC
            buf: list[str] = []
            pstart = transcript[0]["time"]

            def _flush() -> None:
                if buf:
                    parts.append("\n" + "".join(buf) + "\n")
                    buf.clear()

            for e in transcript:
                if e["time"] - last_head >= CHAPTER_INTERVAL_SEC:
                    _flush()
                    parts.append(f"\n### [{self._ts(e['time'])}]\n")
                    last_head = e["time"]; pstart = e["time"]
                if buf and e["time"] - pstart >= 30:
                    _flush(); pstart = e["time"]
                buf.append(e["text"].strip())
            _flush()
        else:
            parts.append("\n> ⚠️ 未取得字幕/口播稿(非中文视频需先经 06 翻译)。\n")

        # ── 附录:画面 / OCR(辅助)。连续重复 OCR 去重,多行 OCR 压成一行,不污染正文。 ──
        if frames:
            parts.append("\n## 画面 / OCR（辅助）\n")
            last_head = -CHAPTER_INTERVAL_SEC
            last_ocr = None
            for fr in frames:
                if fr["time"] - last_head >= CHAPTER_INTERVAL_SEC:
                    parts.append(f"\n### [{self._ts(fr['time'])}]\n")
                    last_head = fr["time"]
                parts.append(f"\n![{fr['filename']}](assets/{fr['filename']})\n")
                ocr = " ".join((fr.get("ocr_text") or "").split())
                if ocr and ocr != last_ocr:
                    parts.append(f"\n> {ocr}\n")
                    last_ocr = ocr

        # ── 附录:弹幕(辅助) ──
        if danmaku:
            parts.append("\n## 弹幕（辅助）\n\n")
            parts.append(" / ".join(d["text"] for d in danmaku[:80]) + "\n")

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
