"""Step 05: 弹幕提取。ASS 解析 → 过滤特效 → 按时间排序。"""

from __future__ import annotations

import json
from pathlib import Path

from shared.step_base import StepBase, file_hash
from steps.utils.ass_parser import load_ass


class DanmakuStep(StepBase):
    def validate_inputs(self) -> list[str]:
        if not list((self.job_dir / "input").glob("*.ass")):
            return ["input/*.ass"]
        return []

    def input_hashes(self) -> dict[str, str]:
        ass_files = sorted((self.job_dir / "input").glob("*.ass"))
        hashes = {}
        for f in ass_files:
            hashes[f.name] = file_hash(f)
        return hashes

    def execute(self) -> dict | None:
        ass_files = sorted((self.job_dir / "input").glob("*.ass"))
        all_entries = []

        for ass_file in ass_files:
            entries = load_ass(ass_file)
            all_entries.extend(entries)

        all_entries.sort(key=lambda e: e.time_sec)

        # 去重:yutto 常同时落 danmaku.ass 与 <标题>.ass(内容相同),按(时间,文本)去重避免翻倍。
        seen: set[tuple[float, str]] = set()
        result = []
        for e in all_entries:
            key = (round(e.time_sec, 1), e.text)
            if key in seen:
                continue
            seen.add(key)
            result.append({"time_sec": round(e.time_sec, 2), "text": e.text})

        self.write_output("intermediate/danmaku.json", result)
        return {"comments": len(result)}


if __name__ == "__main__":
    DanmakuStep.cli_main("07_danmaku")
