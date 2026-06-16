"""SRT 字幕解析。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class SrtEntry:
    index: int
    start_sec: float
    end_sec: float
    text: str


_TS_RE = re.compile(
    r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})"
    r"\s*-->\s*"
    r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})"
)


def _ts_to_sec(h: str, m: str, s: str, ms: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


def parse_srt(text: str) -> list[SrtEntry]:
    """解析 SRT 格式字幕，返回结构化列表。跳过格式错误的条目。"""
    entries: list[SrtEntry] = []
    blocks = re.split(r"\n\s*\n", text.strip())

    for block in blocks:
        lines = block.strip().splitlines()
        if len(lines) < 2:
            continue

        try:
            index = int(lines[0].strip())
        except ValueError:
            continue

        m = _TS_RE.search(lines[1])
        if not m:
            continue

        start = _ts_to_sec(m.group(1), m.group(2), m.group(3), m.group(4))
        end = _ts_to_sec(m.group(5), m.group(6), m.group(7), m.group(8))
        text_content = "\n".join(lines[2:]).strip()
        if text_content:
            entries.append(SrtEntry(index=index, start_sec=start, end_sec=end, text=text_content))

    return entries


def load_srt(path: Path) -> list[SrtEntry]:
    """从文件加载 SRT。"""
    return parse_srt(path.read_text(encoding="utf-8"))


def format_timestamp(seconds: float) -> str:
    """秒数 → [MM:SS] 格式。"""
    m = int(seconds) // 60
    s = int(seconds) % 60
    return f"[{m:02d}:{s:02d}]"


def _looks_chinese(path: Path) -> bool:
    """按内容判定字幕是否中文(CJK 占比超过拉丁字母),比看文件名可靠。"""
    try:
        text = path.read_text(encoding="utf-8")[:4000]
    except OSError:
        return False
    cjk = sum(1 for ch in text if "一" <= ch <= "鿿")
    latin = sum(1 for ch in text if "a" <= ch.lower() <= "z")
    return cjk > 0 and cjk >= latin


def pick_native_srt(input_dir: Path) -> tuple[Path | None, bool]:
    """选与音频对应的原生字幕,返回 (路径, 是否中文)。
    一个视频常含多语言 srt(英/西/日…)。优先内容判定为中文的(原生中文视频的口播);
    无中文字幕则取原生非中文(英文等)——交由 06 翻译成中文。"""
    srts = sorted(input_dir.glob("*.srt"))
    if not srts:
        return (None, True)
    zh = [f for f in srts if _looks_chinese(f)]
    if zh:
        marked = [f for f in zh if any(k in f.name.lower() for k in
                  ("中文", "简体", "zh", "chs", "cn"))]
        return (marked[0] if marked else zh[0], True)
    return (srts[0], False)
