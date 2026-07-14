"""Step 09: 机械版笔记。按时间线拼接截图+OCR+弹幕+逐字稿。"""

from __future__ import annotations

import re
from pathlib import Path

from shared.note_text import markdown_to_index_text
from shared.step_base import StepBase, file_hash
from steps.video.provenance import (
    ensure_video_source_manifest,
    mechanical_ocr_provenance_segments,
    mechanical_provenance_segments,
    persist_video_note_provenance,
)
from steps.utils.srt_parser import load_srt, pick_native_srt


# 画面常驻水印 / 行情面板刻度等噪声:OCR 每帧反复抓到,对笔记无意义,生成时过滤掉。
_OCR_WATERMARKS = (
    "过去案例解读，与现在并无关系，不构成任何推荐",
    "过去案例解读", "与现在并无关系", "不构成任何推荐",
)


def _is_ocr_noise_token(tok: str) -> bool:
    tok = tok.strip("-=_.,;:!?()[]{}<>|/\\'\"~`*+，。、：；！？（）【】《》")
    if not tok:                                                  # 纯标点残渣
        return True
    low = tok.lower()
    if "paken" in low or tok.startswith("派克财经"):
        return True
    if "b" in low and re.fullmatch(r"[bil1]{5,}", low):         # bilibili 及 OCR 变体(biibili/bilbili/Lilibli…)
        return True
    if low.startswith("ma(") or any(k in low for k in ("usdt", "macd", "diff", "change")):
        return True
    if re.fullmatch(r"[a-z]\d[\d.,]+", low):                     # OHLC 形如 O22.983 / H23.016
        return True
    if not any("一" <= c <= "鿿" for c in tok):                  # 无中文 → 可能是行情刻度
        if re.fullmatch(r"\d[\d.,:]{4,}", tok):                  # 纯数字串 132.401.983.63
            return True
        if re.fullmatch(r"\d[\d.,]*[KkMm]", tok):                # 2.699K
            return True
    return False


def _clean_ocr(text: str) -> str:
    """剔除明显噪声(水印/平台名/行情刻度),保留有意义的画面文字。"""
    if not text:
        return ""
    for w in _OCR_WATERMARKS:
        text = text.replace(w, " ")
    return " ".join(t for t in text.split() if not _is_ocr_noise_token(t))


class MechanicalStep(StepBase):
    def validate_inputs(self) -> list[str]:
        missing = []
        if not (self.job_dir / "intermediate" / "dedup.json").exists():
            missing.append("intermediate/dedup.json")
        if not (self.job_dir / "intermediate" / "ocr.json").exists():
            missing.append("intermediate/ocr.json")
        return missing

    # 渲染版本:渲染逻辑变了但输入文件没变时,bump 这个值让幂等失效、强制重渲染。
    RENDER_VERSION = "v6-canonical-ocr-image"

    # 时间节粒度:口播按节成段,该节的截图/OCR 并置在同一节内,三者按时间往下读。
    BEAT_SEC = 30

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
            if sub and is_zh:  # 仅中文原生字幕可无 claude 直用;非中文等 08 翻译
                hashes["subtitle"] = file_hash(sub)
        source_manifest = self.job_dir / "intermediate" / "source_segments.json"
        if source_manifest.exists():
            hashes["source_segments"] = file_hash(source_manifest)
        return hashes

    def execute(self) -> dict | None:
        dedup = self.artifacts.load_json("intermediate/dedup.json")
        ocr = self.artifacts.load_json("intermediate/ocr.json")

        danmaku_path = self.job_dir / "intermediate" / "danmaku.json"
        danmaku = self.artifacts.load_json("intermediate/danmaku.json") if danmaku_path.exists() else []

        # 口播:优先 08 的中文稿(中文加标点/非中文已翻译)。没有则直接读原始中文字幕,无需 claude
        # 就先出可看的机械版。非中文视频无中文稿时口播留空,等 08 翻译,不把外文塞进中文机械版。
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

        self.artifacts.write("output/notes_mechanical.md", md)
        source_manifest = ensure_video_source_manifest(self.job_dir)
        mappings = []
        if source_manifest is not None:
            mappings = mechanical_provenance_segments(
                transcript_lines,
                source_manifest,
                markdown_to_index_text(md),
                beat_sec=self.BEAT_SEC,
            )
            mappings.extend(mechanical_ocr_provenance_segments(
                ocr, source_manifest, markdown_to_index_text(md),
                rendered_markdown=md,
            ))
        provenance = persist_video_note_provenance(
            self.job_dir,
            note_type="mechanical",
            note_artifact="output/notes_mechanical.md",
            provenance_segments=mappings,
        )
        return {
            "frames": len(kept_frames),
            "events": len(events),
            "provenance_segments": provenance["segments"],
            "provenance_status": provenance["status"],
        }

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
        """图文时间线:按 ~BEAT_SEC 一节,把该节的口播(字幕整行拼接成段、句子完整不截断)
        与该节的截图+OCR 并置在一起,顺时间往下读。截图再稀释:连续/节内重复 OCR 去重,
        有 OCR 的优先、每节最多 2 张,全节无 OCR 则留 1 张代表帧。"""
        transcript = [e for e in events if e["type"] == "transcript" and e["text"].strip()]
        frames = [e for e in events if e["type"] == "frame"]
        danmaku = [e for e in events if e["type"] == "danmaku" and e["text"].strip()]
        if not transcript and not frames:
            return "# 机械版笔记\n\n（无内容）\n"

        from collections import defaultdict
        tb: dict[int, list] = defaultdict(list)   # 口播
        fb: dict[int, list] = defaultdict(list)   # 截图
        db: dict[int, list] = defaultdict(list)   # 弹幕
        for e in transcript:
            tb[int(e["time"] // self.BEAT_SEC)].append(e)
        for e in frames:
            fb[int(e["time"] // self.BEAT_SEC)].append(e)
        for e in danmaku:
            db[int(e["time"] // self.BEAT_SEC)].append(e)

        parts = ["# 机械版笔记\n"]
        if not transcript:
            parts.append("\n> ⚠️ 未取得字幕/口播稿(非中文视频需先经 06 翻译)。\n")

        last_ocr: str | None = None
        for beat in sorted(set(tb) | set(fb)):
            parts.append(f"\n## [{self._ts(beat * self.BEAT_SEC)}]\n")

            text = "".join(e["text"].strip() for e in tb.get(beat, []))
            if text:
                parts.append(f"\n{text}\n")

            # 再稀释:节内/与上一张连续重复的 OCR 跳过;有 OCR 的优先,最多 2 张;
            # 全节无 OCR 仍留 1 张代表帧,保证有画面。
            kept: list[tuple[dict, str]] = []
            seen: set[str] = set()
            for fr in fb.get(beat, []):
                ocr = _clean_ocr(fr.get("ocr_text") or "")
                if ocr and (ocr == last_ocr or ocr in seen):
                    continue
                kept.append((fr, ocr))
                if ocr:
                    seen.add(ocr)
                    last_ocr = ocr
            with_text = [k for k in kept if k[1]]
            for fr, ocr in (with_text[:2] if with_text else kept[:1]):
                parts.append(f"\n![{self._ts(fr['time'])}](assets/{fr['filename']})\n")
                if ocr:
                    parts.append(f"\n> OCR：{ocr}\n")

            if db.get(beat):
                parts.append(f"\n> 弹幕：{' / '.join(d['text'] for d in db[beat][:20])}\n")

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
    MechanicalStep.cli_main("09_mechanical")
