#!/usr/bin/env python3
"""术语一致性只读审查(工单 26-07-06/04 P3):找「同一 English 术语多种中文译名」冲突。

用法(api 容器内跑,env 有 MINIO_*;--domain 需 DB):
  docker exec flori-api python /app/scripts/term_consistency_check.py --job <job_id>
  docker exec flori-api python /app/scripts/term_consistency_check.py --collection <collection_id>
  docker exec flori-api python /app/scripts/term_consistency_check.py --domain finance
  加 --json 输出机器可读结果(供脚本消费/before-after 对比)。

逻辑:拉取范围内全部 output/translated.md → shared.terms.extract_pairs 抽「中文(English)」
对照(含复现验证)→ 按 English 归组 → 组内译名 >1 种即冲突。

修复路径(刻意不自动改译文——中文语境文本替换有误伤风险,rerun 已被 chunk 化摊薄):
  1) 对冲突术语人工定准译名 → 前端术语管理页(或 SQL)写 glossary.zh_name;
  2) rerun 涉事 job 的 04_translate 步(rerun 会重新导出 term_map,新表注入生效);
  3) 复跑本脚本验证冲突数下降。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, "/app")
from shared.terms import extract_pairs  # noqa: E402


def _minio():
    from minio import Minio
    ep = os.environ.get("MINIO_ENDPOINT", "minio:9000").replace("http://", "").replace("https://", "")
    return Minio(
        ep,
        access_key=os.environ.get("MINIO_ACCESS_KEY") or os.environ.get("MINIO_ROOT_USER"),
        secret_key=os.environ.get("MINIO_SECRET_KEY") or os.environ.get("MINIO_ROOT_PASSWORD"),
        secure=False,
    )


def _job_ids(args) -> list[str]:
    if args.job:
        return [args.job]
    import sqlite3
    c = sqlite3.connect(args.db)
    if args.collection:
        rows = c.execute("SELECT id FROM jobs WHERE collection_id=?", (args.collection,)).fetchall()
    else:
        rows = c.execute("SELECT id FROM jobs WHERE domain=?", (args.domain,)).fetchall()
    return [r[0] for r in rows]


def collect_conflicts(pairs_by_job: dict[str, dict[str, str]]) -> dict[str, dict[str, list[str]]]:
    """{job: {en: zh}} → {en: {zh: [job,...]}},仅保留译名 >1 种的英文术语(=冲突)。"""
    grouped: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for job, pairs in pairs_by_job.items():
        for en, zh in pairs.items():
            grouped[en][zh].append(job)
    return {en: dict(v) for en, v in grouped.items() if len(v) > 1}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--job"); g.add_argument("--collection"); g.add_argument("--domain")
    ap.add_argument("--db", default="/data/db/analyzer.db")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    cli = _minio()
    bucket = os.environ.get("MINIO_BUCKET", "flori")
    pairs_by_job: dict[str, dict[str, str]] = {}
    for jid in _job_ids(args):
        try:
            md = cli.get_object(bucket, f"{jid}/output/translated.md").read().decode()
        except Exception:
            continue                       # 无译文(中文原文/未翻)跳过
        pairs = extract_pairs(md)
        if pairs:
            pairs_by_job[jid] = pairs

    conflicts = collect_conflicts(pairs_by_job)
    if args.json:
        print(json.dumps({"jobs_scanned": len(pairs_by_job), "conflicts": conflicts,
                          "conflict_count": len(conflicts)}, ensure_ascii=False, indent=1))
        return
    print(f"扫描 {len(pairs_by_job)} 篇译文;冲突术语 {len(conflicts)} 个\n")
    for en, variants in sorted(conflicts.items()):
        print(f"■ {en}")
        for zh, jobs in sorted(variants.items(), key=lambda kv: -len(kv[1])):
            print(f"   {zh} ×{len(jobs)}  ({', '.join(j[:28] for j in jobs[:3])}{'…' if len(jobs) > 3 else ''})")


if __name__ == "__main__":
    main()
