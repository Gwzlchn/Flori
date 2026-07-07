#!/usr/bin/env python3
"""存量 glossary 批量补标准中文译名(zh_name)的三段式运维脚本。

架构约束:DB 在 api 容器(/data/db),claude CLI 只在 worker 容器 → 拆三个子命令经文件交接:

  1. 导出(api 容器,有 DB):
     docker exec flori-api python /app/scripts/backfill_zh_names.py export --out /data/backfill/todo.json
     读 zh_name 为空的词条 → JSON 清单 [{domain,term,definition}](已有 zh_name 的跳过=幂等)。
  2. 补译(claude worker 容器,有 CLI;/data 卷共享):
     docker exec flori-claude-worker python /app/scripts/backfill_zh_names.py translate \\
         --todo /data/backfill/todo.json --out-dir /data/backfill/
     分批(默认 50 条/批)喂 claude:每条产【标准中文译名】(不是解释;term 本身是中文→原样;
     不确定→null)。每批原始输出留档 out-dir/batch-NNN.json(可审),汇总 out-dir/zh_names.json。
  3. 写回(api 容器):
     docker exec flori-api python /app/scripts/backfill_zh_names.py apply --map /data/backfill/zh_names.json
     仅更新 zh_name 仍为空的行(幂等,重跑安全);null/空值跳过。

校验铁律:每批输出必须是严格 JSON 且 key 集合 == 该批输入 term 集合,否则整批弃用重试一次,
再失败记 failed 留档继续下一批(单批失败不阻断)。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

BATCH = 50
MODEL = "claude-opus-4-8[1m]"


def cmd_export(args) -> None:
    from shared.db import Database
    db = Database(Path(args.db))
    rows = db._conn.execute(
        "SELECT domain, term, definition FROM glossary "
        "WHERE zh_name IS NULL OR zh_name=''"
    ).fetchall()
    todo = [{"domain": r["domain"], "term": r["term"], "definition": (r["definition"] or "")[:120]}
            for r in rows]
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(todo, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"exported {len(todo)} terms → {out}")


def _translate_batch(batch: list[dict]) -> dict | None:
    """喂 claude 一批,返回 {term: zh_name|None};失败(非 JSON/键不匹配)返回 None。"""
    listing = "\n".join(f"- {t['term']}(上下文:{t['definition'][:80]})" for t in batch)
    prompt = (
        "以下是知识库术语表词条。给出每条的【标准中文译名】——注意是通行短译名,不是解释:\n"
        "- term 是英文 → 给标准中文译名(如 Kelly criterion → 凯利准则);\n"
        "- term 本身是中文 → 原样返回该中文;\n"
        "- 无通行译名/不确定 → null(宁缺勿错)。\n"
        f'输出严格 JSON(无代码块标记):{{"<term>": "<译名>|null", ...}},key 必须与输入逐条一致。\n\n{listing}'
    )
    r = subprocess.run(
        ["claude", "-p", "--model", MODEL, "--tools", "", "--max-turns", "1",
         "--output-format", "json"],
        input=prompt.encode(), capture_output=True, timeout=600)
    if r.returncode != 0:
        return None
    try:
        outer = json.loads(r.stdout.decode()[r.stdout.decode().find("{"):])
        mapping = json.loads(outer.get("result") or "{}")
    except (json.JSONDecodeError, ValueError):
        return None
    if set(mapping) != {t["term"] for t in batch}:   # key 集合必须与输入完全一致
        return None
    return mapping


def cmd_translate(args) -> None:
    todo = json.loads(Path(args.todo).read_text(encoding="utf-8"))
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    merged: dict[str, dict] = {}
    failed: list[str] = []
    batches = [todo[i:i + BATCH] for i in range(0, len(todo), BATCH)]
    for n, batch in enumerate(batches):
        mapping = _translate_batch(batch) or _translate_batch(batch)  # 失败重试一次
        if mapping is None:
            failed += [t["term"] for t in batch]
            print(f"batch {n}: FAILED (跳过,留待下轮)", file=sys.stderr)
            continue
        (out_dir / f"batch-{n:03d}.json").write_text(
            json.dumps(mapping, ensure_ascii=False, indent=1), encoding="utf-8")
        for t in batch:
            zh = mapping.get(t["term"])
            if isinstance(zh, str) and zh.strip():
                merged[t["term"]] = {"domain": t["domain"], "zh_name": zh.strip()}
        print(f"batch {n}: {len(batch)} in / {sum(1 for t in batch if mapping.get(t['term']))} named")
    (out_dir / "zh_names.json").write_text(
        json.dumps(merged, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"done: {len(merged)} named, {len(failed)} failed → {out_dir}/zh_names.json")


def cmd_apply(args) -> None:
    from shared.db import Database
    db = Database(Path(args.db))
    mapping = json.loads(Path(args.map).read_text(encoding="utf-8"))
    updated = skipped = 0
    for term, info in mapping.items():
        row = db._conn.execute(
            "SELECT zh_name FROM glossary WHERE domain=? AND term=?",
            (info["domain"], term)).fetchone()
        if row is None or (row["zh_name"] or "").strip():
            skipped += 1          # 不存在 / 已有译名(人工或概念步先到)→ 不覆盖,幂等
            continue
        db.set_glossary_zh_name(info["domain"], term, info["zh_name"])
        updated += 1
    print(f"applied: {updated} updated, {skipped} skipped")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p1 = sub.add_parser("export"); p1.add_argument("--out", required=True)
    p1.add_argument("--db", default="/data/db/analyzer.db")
    p2 = sub.add_parser("translate"); p2.add_argument("--todo", required=True)
    p2.add_argument("--out-dir", required=True)
    p3 = sub.add_parser("apply"); p3.add_argument("--map", required=True)
    p3.add_argument("--db", default="/data/db/analyzer.db")
    args = ap.parse_args()
    {"export": cmd_export, "translate": cmd_translate, "apply": cmd_apply}[args.cmd](args)


if __name__ == "__main__":
    main()
