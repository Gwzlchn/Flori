#!/usr/bin/env python3
"""存量 glossary 实体清洗:归一键重复、中英分裂合并和 LLM 语义建议。

架构约束与 backfill_zh_names 同套路:DB 在 api 容器,claude CLI 只在 worker 容器,
/data 根目录 root-only → 文件交接走 worker 家目录(/data/workers/<name>/,uid1000 可写):

  1. 确定性合并(api 容器,有 DB;先 dry-run 看计划,--apply 才落库):
     docker exec flori-api python /app/scripts/merge_glossary_entities.py scan
     docker exec flori-api python /app/scripts/merge_glossary_entities.py scan --apply
     检测 = 同域内两行共享任一归一键(norm_key 撞 term/zh_name/aliases,即字面重复 +
     中英互指分裂);组内 dst 选择:accepted > 定义更长 > 英文主名 > 先建。合并语义见
     db.merge_glossary_terms(occurrence 并集 / 变体入 aliases,可逆留痕)。幂等:重跑无新组。
  2. LLM 语义建议(claude worker 容器;只产建议 JSON 留档,绝不直接动库):
     docker exec flori-claude-worker python /app/scripts/merge_glossary_entities.py suggest \\
         --todo /data/workers/claude-2/merge/todo.json --out-dir /data/workers/claude-2/merge/
     (todo.json 先在 api 容器 `scan --export-todo <path>` 产出。)按域分批(30 条/批)喂
     claude 找语义等价组 + junk(France/法国 类非概念)。每批留档 batch-NNN.json,汇总
     suggestions.json。校验:建议引用的 term 必须都在该批输入内,越界条目丢弃。
  3. 人审后应用(api 容器;默认 dry-run 打印计划,--yes 才执行):
     docker exec flori-api python /app/scripts/merge_glossary_entities.py apply-llm \\
         --suggestions /data/workers/claude-2/merge/suggestions.json [--yes]
     merges 逐组执行 merge_glossary_terms;junk 删行(误删可从建议留档追溯)。幂等:
     已合并/已删的组自动跳过。
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


def _load_db(db_path: str):
    from shared.db import Database
    return Database(Path(db_path))


def _rows(db) -> list[dict]:
    return [
        {
            "domain": r["domain"], "term": r["term"],
            "zh_name": r["zh_name"] or "", "definition": r["definition"] or "",
            "aliases": json.loads(r["aliases"] or "[]"),
            "status": r["status"], "created_at": r["created_at"] or "",
        }
        for r in db._conn.execute(
            "SELECT domain, term, zh_name, definition, aliases, status, created_at "
            "FROM glossary ORDER BY domain, term"
        ).fetchall()
    ]


def _dst_rank(row: dict) -> tuple:
    """组内 dst 选择:accepted > 定义更长 > 英文主名 > 先建(created_at 小)。"""
    import re
    is_en = bool(re.match(r"^[A-Za-z]", row["term"]))
    return (
        0 if row["status"] == "accepted" else 1,
        -len(row["definition"]),
        0 if is_en else 1,
        row["created_at"],
    )


def _detect_groups(rows: list[dict]) -> list[list[dict]]:
    """同域内共享任一归一键的行并查成组(≥2 行才算)。"""
    from shared.concepts import candidate_keys, norm_key

    groups: list[list[dict]] = []
    by_domain: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_domain[r["domain"]].append(r)
    for domain_rows in by_domain.values():
        parent = list(range(len(domain_rows)))

        def find(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        key_owner: dict[str, int] = {}
        for i, r in enumerate(domain_rows):
            keys = set(candidate_keys(r["term"], r["zh_name"] or None))
            keys |= {norm_key(a) for a in r["aliases"] if norm_key(a)}
            for k in keys:
                if k in key_owner:
                    ri, rj = find(key_owner[k]), find(i)
                    if ri != rj:
                        parent[rj] = ri
                else:
                    key_owner[k] = i
        buckets: dict[int, list[dict]] = defaultdict(list)
        for i, r in enumerate(domain_rows):
            buckets[find(i)].append(r)
        groups += [g for g in buckets.values() if len(g) > 1]
    return groups


def cmd_scan(args) -> None:
    db = _load_db(args.db)
    rows = _rows(db)
    groups = _detect_groups(rows)
    plan = []
    for g in groups:
        g_sorted = sorted(g, key=_dst_rank)
        dst, srcs = g_sorted[0], g_sorted[1:]
        plan.append({
            "domain": dst["domain"], "dst": dst["term"],
            "srcs": [s["term"] for s in srcs],
        })
    for p in plan:
        print(f"[{p['domain']}] {' + '.join(p['srcs'])}  →  {p['dst']}")
    print(f"共 {len(plan)} 组待合并(涉及 {sum(len(p['srcs']) for p in plan)} 条 src)")
    if args.export_todo:
        # LLM 段输入:清洗后仍独立的全部词条(term/zh_name/definition),按域分组。
        todo = [{"domain": r["domain"], "term": r["term"], "zh_name": r["zh_name"],
                 "definition": r["definition"][:120]} for r in rows]
        out = Path(args.export_todo)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(todo, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"exported {len(todo)} terms → {out}(供 suggest 段)")
    if not args.apply:
        print("dry-run(未落库);确认后加 --apply 执行")
        return
    done = failed = 0
    for p in plan:
        for src in p["srcs"]:
            try:
                db.merge_glossary_terms(p["domain"], src, p["dst"])
                done += 1
            except ValueError as e:   # 组内前序合并已把该行并掉 → 跳过
                print(f"  skip {p['domain']}/{src}: {e}", file=sys.stderr)
                failed += 1
    print(f"applied: {done} merged, {failed} skipped")


def _suggest_batch(domain: str, batch: list[dict]) -> dict | None:
    """喂 claude 一批,返回 {merges:[{dst,srcs,reason}], junk:[term]};失败/越界返回 None。"""
    listing = "\n".join(
        f"- {t['term']}" + (f" / {t['zh_name']}" if t["zh_name"] else "")
        + (f":{t['definition'][:80]}" if t["definition"] else "")
        for t in batch
    )
    prompt = (
        f"以下是知识库「{domain}」领域的概念词条(term / 中文译名:定义)。找出其中指向"
        "【同一概念】的语义等价组(如缩写与全称、同义异形),以及不是概念的垃圾条目"
        "(如国家名、人名、普通名词误入)。宁缺勿滥:不确定就不并、不标。\n"
        '输出严格 JSON(无代码块):{"merges": [{"dst": "<保留主名>", "srcs": ["<并入名>", ...],'
        ' "reason": "<一句理由>"}], "junk": ["<垃圾条目>", ...]}\n'
        "dst/srcs/junk 必须逐字来自输入列表的 term。无可合并/无垃圾则给空数组。\n\n" + listing
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
        data = json.loads(outer.get("result") or "{}")
    except (json.JSONDecodeError, ValueError):
        return None
    terms = {t["term"] for t in batch}
    merges = []
    for m in data.get("merges") or []:
        names = [m.get("dst")] + list(m.get("srcs") or [])
        if all(n in terms for n in names) and m.get("srcs"):
            merges.append({"domain": domain, "dst": m["dst"], "srcs": m["srcs"],
                           "reason": m.get("reason") or ""})
    junk = [{"domain": domain, "term": t} for t in (data.get("junk") or []) if t in terms]
    return {"merges": merges, "junk": junk}


def cmd_suggest(args) -> None:
    todo = json.loads(Path(args.todo).read_text(encoding="utf-8"))
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    by_domain: dict[str, list[dict]] = defaultdict(list)
    for t in todo:
        by_domain[t["domain"]].append(t)
    all_merges: list[dict] = []
    all_junk: list[dict] = []
    n = 0
    for domain, terms in sorted(by_domain.items()):
        for i in range(0, len(terms), BATCH):
            batch = terms[i:i + BATCH]
            res = _suggest_batch(domain, batch) or _suggest_batch(domain, batch)
            if res is None:
                print(f"batch {n} ({domain}): FAILED(跳过)", file=sys.stderr)
                n += 1
                continue
            (out_dir / f"batch-{n:03d}.json").write_text(
                json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
            all_merges += res["merges"]
            all_junk += res["junk"]
            print(f"batch {n} ({domain}): {len(batch)} in / "
                  f"{len(res['merges'])} merges, {len(res['junk'])} junk")
            n += 1
    out = out_dir / "suggestions.json"
    out.write_text(json.dumps({"merges": all_merges, "junk": all_junk},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"done: {len(all_merges)} merge 组 / {len(all_junk)} junk → {out}(人审后 apply-llm)")


def cmd_apply_llm(args) -> None:
    db = _load_db(args.db)
    data = json.loads(Path(args.suggestions).read_text(encoding="utf-8"))
    merges, junk = data.get("merges") or [], data.get("junk") or []
    for m in merges:
        print(f"merge [{m['domain']}] {' + '.join(m['srcs'])} → {m['dst']}"
              f"  ({m.get('reason', '')})")
    for j in junk:
        print(f"junk  [{j['domain']}] {j['term']}(将删除)")
    if not args.yes:
        print(f"dry-run:{len(merges)} merge 组 / {len(junk)} junk;确认后加 --yes 执行")
        return
    done = skipped = 0
    for m in merges:
        for src in m["srcs"]:
            try:
                db.merge_glossary_terms(m["domain"], src, m["dst"])
                done += 1
            except ValueError as e:
                print(f"  skip {m['domain']}/{src}: {e}", file=sys.stderr)
                skipped += 1
    deleted = 0
    for j in junk:
        if db.get_glossary_term(j["domain"], j["term"]) is not None:
            db.delete_glossary_term(j["domain"], j["term"])
            deleted += 1
    print(f"applied: {done} merged, {skipped} skipped, {deleted} junk deleted")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p1 = sub.add_parser("scan")
    p1.add_argument("--apply", action="store_true")
    p1.add_argument("--export-todo", default="")
    p1.add_argument("--db", default="/data/db/analyzer.db")
    p2 = sub.add_parser("suggest")
    p2.add_argument("--todo", required=True)
    p2.add_argument("--out-dir", required=True)
    p3 = sub.add_parser("apply-llm")
    p3.add_argument("--suggestions", required=True)
    p3.add_argument("--yes", action="store_true")
    p3.add_argument("--db", default="/data/db/analyzer.db")
    args = ap.parse_args()
    {"scan": cmd_scan, "suggest": cmd_suggest, "apply-llm": cmd_apply_llm}[args.cmd](args)


if __name__ == "__main__":
    main()
