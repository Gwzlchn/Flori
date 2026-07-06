"""存量 pdf-only 译文迁移:整页渲染图 → 跳原文链接(纯文本变换,零 AI 成本)。

背景:旧 _embed_figure_pages 在【图 N|第 p 页】占位下插 ![](assets/pdf-page-<p>.png)
整页 A4 截图(含原文正文),把译文阅读流切碎(线上 101 Alphas 实证不可读)。
新方案 = 占位行追加 [查看原图(原文第 p 页)](#pdf-page=p),前端切「原文」tab 原生跳页。

用法(api 容器内或挂 /data+MINIO env 的 docker run):
  python scripts/migrate_pdf_figure_links.py           # 全部 pdf-only job
  python scripts/migrate_pdf_figure_links.py --dry-run # 只报不改
幂等:已有 #pdf-page= 链接的占位行不重复追加;图行删除按精确模式匹配。
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys

sys.path.insert(0, "/app")
from minio import Minio  # noqa: E402

FIG_RE = re.compile(r"【[图表]\s*[\d.]+[^】|]*\|\s*第\s*(\d+)\s*页】")
IMG_RE = re.compile(r"^!\[\]\(assets/pdf-page-\d+\.png\)\s*$")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    ep = os.environ.get("MINIO_ENDPOINT", "minio:9000").replace("http://", "")
    cli = Minio(ep, access_key=os.environ.get("MINIO_ACCESS_KEY") or os.environ.get("MINIO_ROOT_USER"),
                secret_key=os.environ.get("MINIO_SECRET_KEY") or os.environ.get("MINIO_ROOT_PASSWORD"),
                secure=False)
    bucket = os.environ.get("MINIO_BUCKET", "flori")
    # 扫全部 job 的 parsed.json 找 pdf-only(比连 DB 更少依赖;对象数有限)
    jobs = sorted({o.object_name.split("/")[0] for o in cli.list_objects(bucket, recursive=False)})
    changed = 0
    for jid in jobs:
        if not jid.startswith("jobs_"):
            continue
        try:
            pk = json.loads(cli.get_object(bucket, f"{jid}/intermediate/parsed.json").read())
            if pk.get("source_kind") != "pdf-only":
                continue
            md = cli.get_object(bucket, f"{jid}/output/translated.md").read().decode()
        except Exception:
            continue
        out_lines: list[str] = []
        dirty = False
        for line in md.splitlines():
            if IMG_RE.match(line):
                dirty = True                      # 删整页图行
                if out_lines and out_lines[-1] == "":
                    out_lines.pop()               # 连带删插入时的空行
                continue
            m = FIG_RE.search(line)
            if m and "#pdf-page=" not in line:
                p = int(m.group(1))
                line = line.rstrip() + f"  [查看原图(原文第 {p} 页)](#pdf-page={p})"
                dirty = True
            out_lines.append(line)
        if not dirty:
            continue
        changed += 1
        print(f"  {'DRY ' if args.dry_run else ''}{jid}")
        if not args.dry_run:
            data = "\n".join(out_lines).encode()
            cli.put_object(bucket, f"{jid}/output/translated.md", io.BytesIO(data), len(data),
                           content_type="text/markdown")
    print(f"done: {changed} 篇迁移")


main()
