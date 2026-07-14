#!/usr/bin/env python3
"""存量概念关系边补建的三段式运维脚本。

存量 800 条 related 全空(采集链 05_concepts v3 起才抽 related)。对核心概念
(occurrence≥2 或 accepted;低频长尾不补,噪声)按域分批喂 LLM 产关系边建议,
人审后写回。架构约束同 backfill_zh_names:DB 在 api 容器,claude 在 worker 容器,
文件交接走 worker 家目录(/data 根 root-only):

  1. 导出(api 容器):
     docker exec flori-api python /app/scripts/backfill_concept_edges.py export \\
         --out /data/workers/claude-2/edges/todo.json
  2. LLM 建议(claude worker 容器;只产 JSON 留档,不动库):
     docker exec flori-claude-worker python /app/scripts/backfill_concept_edges.py suggest \\
         --todo /data/workers/claude-2/edges/todo.json --out-dir /data/workers/claude-2/edges/
     每域分批(30 条/批,term+定义在手),产 [{src,dst,rel,reason}];两端必须都在该批
     输入内(防幻觉),rel 限 prerequisite/is_a/part_of/related。批留档 batch-NNN.json,
     汇总 edges.json。
  3. 人审后写回(api 容器;默认 dry-run,--yes 执行):
     docker exec flori-api python /app/scripts/backfill_concept_edges.py apply \\
         --edges /data/workers/claude-2/edges/edges.json [--yes]
     db.add_glossary_relations 按目标去重(先到先得)→ 幂等,重跑安全。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

BATCH = 30
MODEL = "claude-opus-4-8[1m]"
RELS = ("prerequisite", "is_a", "part_of", "related")


def cmd_export(args) -> None:
    from shared.db import Database
    db = Database(Path(args.db))
    todo = []
    for r in db._maintenance_glossary_rows():
        if r["status"] == "rejected":
            continue
        occ_n = len(json.loads(r["occurrences"] or "[]"))
        if occ_n >= 2 or r["status"] == "accepted":
            todo.append({"domain": r["domain"], "term": r["term"],
                         "zh_name": r["zh_name"] or "",
                         "definition": (r["definition"] or "")[:120]})
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(todo, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"exported {len(todo)} core terms → {out}")


def _suggest_batch(domain: str, batch: list[dict]) -> list[dict] | None:
    listing = "\n".join(
        f"- {t['term']}" + (f" / {t['zh_name']}" if t["zh_name"] else "")
        + (f":{t['definition'][:80]}" if t["definition"] else "")
        for t in batch
    )
    prompt = (
        f"以下是知识库「{domain}」领域的核心概念(term / 中文译名:定义)。找出概念【两两之间】"
        "有明确关系的边:\n"
        "- rel 只允许 prerequisite(懂 dst 是懂 src 的前提)/is_a(src 是一种 dst)/"
        "part_of(src 是 dst 的组成部分)/related(强相关)。\n"
        "- src/dst 必须逐字来自输入列表;关系不明确就不出边(宁缺勿滥,每概念最多 3 条)。\n"
        '输出严格 JSON 数组(无代码块):[{"src": "...", "dst": "...", "rel": "...", '
        '"reason": "<一句理由>"}]。无边则 []。\n\n' + listing
    )
    r = subprocess.run(
        ["claude", "-p", "--model", MODEL, "--tools", "", "--max-turns", "1",
         "--output-format", "json"],
        input=prompt.encode(), capture_output=True, timeout=600)
    if r.returncode != 0:
        return None
    try:
        raw = r.stdout.decode()
        outer = json.loads(raw[raw.find("{"):])
        edges = json.loads(outer.get("result") or "[]")
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(edges, list):
        return None
    terms = {t["term"] for t in batch}
    return [
        {"domain": domain, "src": e["src"], "dst": e["dst"], "rel": e["rel"],
         "reason": e.get("reason") or ""}
        for e in edges
        if isinstance(e, dict) and e.get("src") in terms and e.get("dst") in terms
        and e.get("src") != e.get("dst") and e.get("rel") in RELS
    ]


def cmd_suggest(args) -> None:
    todo = json.loads(Path(args.todo).read_text(encoding="utf-8"))
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    by_domain: dict[str, list[dict]] = defaultdict(list)
    for t in todo:
        by_domain[t["domain"]].append(t)
    all_edges: list[dict] = []
    n = 0
    for domain, terms in sorted(by_domain.items()):
        for i in range(0, len(terms), BATCH):
            batch = terms[i:i + BATCH]
            edges = _suggest_batch(domain, batch)
            if edges is None:
                edges = _suggest_batch(domain, batch)   # 失败重试一次
            if edges is None:
                print(f"batch {n} ({domain}): FAILED(跳过)", file=sys.stderr)
                n += 1
                continue
            (out_dir / f"batch-{n:03d}.json").write_text(
                json.dumps(edges, ensure_ascii=False, indent=1), encoding="utf-8")
            all_edges += edges
            print(f"batch {n} ({domain}): {len(batch)} in / {len(edges)} edges")
            n += 1
    out = out_dir / "edges.json"
    out.write_text(json.dumps(all_edges, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"done: {len(all_edges)} edges → {out}(人审后 apply)")


def cmd_apply(args) -> None:
    from shared.db import Database
    db = Database(Path(args.db))
    edges = json.loads(Path(args.edges).read_text(encoding="utf-8"))
    for e in edges:
        print(f"[{e['domain']}] {e['src']} -{e['rel']}→ {e['dst']}  ({e.get('reason', '')})")
    if not args.yes:
        print(f"dry-run:{len(edges)} 条边;确认后加 --yes 执行")
        return
    added = skipped = 0
    for e in edges:
        n = db.add_glossary_relations(
            e["domain"], e["src"], [{"term": e["dst"], "rel": e["rel"]}]
        )
        added += n
        skipped += (1 - n)
    print(f"applied: {added} added, {skipped} skipped(已存在/行缺失)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p1 = sub.add_parser("export")
    p1.add_argument("--out", required=True)
    p1.add_argument("--db", default="/data/db/analyzer.db")
    p2 = sub.add_parser("suggest")
    p2.add_argument("--todo", required=True)
    p2.add_argument("--out-dir", required=True)
    p3 = sub.add_parser("apply")
    p3.add_argument("--edges", required=True)
    p3.add_argument("--yes", action="store_true")
    p3.add_argument("--db", default="/data/db/analyzer.db")
    args = ap.parse_args()
    {"export": cmd_export, "suggest": cmd_suggest, "apply": cmd_apply}[args.cmd](args)


if __name__ == "__main__":
    main()
