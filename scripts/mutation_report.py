#!/usr/bin/env python3
"""从 history.csv 生成 trend.md(趋势表)+ 各模块 shields endpoint JSON(README 徽章)。
在 GitHub runner 上跑(纯 stdlib,无需容器)。用法:
  mutation_report.py <history.csv> <out_dir>
history.csv 列:date,module,killed,survived(由 mutation.yml 每日追加)。"""
from __future__ import annotations

import csv
import json
import pathlib
import sys


def _color(pct: float) -> str:
    return "brightgreen" if pct >= 80 else "yellow" if pct >= 60 else "orange"


def _short(module: str) -> str:
    # shared.ai_gateway → ai_gateway;scheduler/worker 无点 → 原样
    return module.split(".")[-1]


def main() -> int:
    hist_path, out_dir = sys.argv[1], pathlib.Path(sys.argv[2])
    by_dm: dict[tuple[str, str], float] = {}   # (date, module) → score%
    dates: list[str] = []
    modules: set[str] = set()
    with open(hist_path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            d, m = r["date"], r["module"]
            k, s = int(r["killed"]), int(r["survived"])
            t = k + s
            by_dm[(d, m)] = (100.0 * k / t) if t else 0.0   # 同日同模块取最后一条(覆盖)
            if d not in dates:
                dates.append(d)
            modules.add(m)
    dates = sorted(set(dates))
    mods = sorted(modules)

    # trend.md:行=日期(最近 30),列=模块
    head = "| date | " + " | ".join(_short(m) for m in mods) + " |"
    sep = "|---|" + "---:|" * len(mods)
    lines = ["# 🧬 变异分数趋势(killed/总数;每日自动更新)", "",
             "分数掉了 = 有人加了代码却没加断言 / 弱化了测试。源数据见同目录 history.csv。", "",
             head, sep]
    for d in dates[-30:]:
        cells = [f"{by_dm[(d, m)]:.1f}%" if (d, m) in by_dm else "—" for m in mods]
        lines.append(f"| {d} | " + " | ".join(cells) + " |")
    (out_dir / "trend.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    # mutation-<short>.json:各模块"最近一次有数据"的分数 → shields endpoint(README 徽章)
    for m in mods:
        val = next((by_dm[(d, m)] for d in reversed(dates) if (d, m) in by_dm), None)
        if val is None:
            continue
        badge = {"schemaVersion": 1, "label": f"{_short(m)} mutation",
                 "message": f"{val:.1f}%", "color": _color(val)}
        (out_dir / f"mutation-{_short(m)}.json").write_text(
            json.dumps(badge), encoding="utf-8")

    print(f"generated trend.md + {len(mods)} badge(s) in {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
