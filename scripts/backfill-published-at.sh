#!/usr/bin/env bash
# 回填 jobs.published_at(源内容在平台的发布/更新时间):对每个 published_at 当前为空的 job,
# 读其 input/metadata.json 的 published_at 写入 DB。概念时间线据此按"源内容发布时间"而非
# "入库时间"分桶。加列迁移幂等;本回填只补空、不覆盖已有值,可反复安全执行。
#
# metadata.json 经 storage 后端读取(与 API /api/jobs/<id> 同一路径):分布式部署产物在
# 对象存储(MinIO)、不在容器本地盘,故不能直接读 /data/jobs。storage 后端按容器内的
# MINIO_URL 等环境变量自动解析(设了用 MinIO,否则本地盘)。
#
# 用法:
#   scripts/backfill-published-at.sh           # dry-run:列出将回填的 job(只读)
#   scripts/backfill-published-at.sh --apply   # 真正写入 DB
# 环境:API_CONTAINER(默认 mnemo-api)、DATA_DIR(默认 /data,容器内路径)。
set -euo pipefail

usage() { sed -n '2,16p' "$0"; exit "${1:-0}"; }
APPLY=0
while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help) usage 0 ;;
    --apply) APPLY=1; shift ;;
    *) echo "未知参数: $1" >&2; usage 1 ;;
  esac
done
API_CONTAINER="${API_CONTAINER:-mnemo-api}"
DATA_DIR="${DATA_DIR:-/data}"

docker exec -e BF_APPLY="$APPLY" -e BF_DATA_DIR="$DATA_DIR" -i \
  "$API_CONTAINER" python3 - <<'PY'
import asyncio, json, os
from pathlib import Path
from shared.db import Database
from shared.storage import create_storage

apply = os.environ["BF_APPLY"] == "1"
data_dir = Path(os.environ["BF_DATA_DIR"])
db = Database(str(data_dir / "db" / "analyzer.db"))
db.init_schema()  # 确保旧库已补上 published_at 列(幂等加列迁移)
storage = create_storage(data_dir / "jobs")
_, jobs = db.list_jobs(limit=100000)


async def published_at_of(job_id):
    try:
        raw = await storage.read_file(job_id, "input/metadata.json")
    except Exception:
        return None
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8", errors="replace")).get("published_at")
    except Exception:
        return None


async def main():
    candidates = []  # (job_id, published_at)
    no_meta_or_field = 0
    for j in jobs:
        if j.published_at:  # 已有值,不覆盖
            continue
        pub = await published_at_of(j.id)
        if pub:
            candidates.append((j.id, pub))
        else:
            no_meta_or_field += 1

    print(f"共 {len(jobs)} 个 job;published_at 待回填 {len(candidates)} 个 "
          f"(无 metadata.json 或无 published_at 字段 {no_meta_or_field})。")
    for jid, pub in candidates:
        print(f"  {jid:34} <- {pub}")
    if not candidates:
        print("无可回填项。"); return
    if not apply:
        print("\n[dry-run] 未写入。确认无误后加 --apply 执行。"); return

    ok = err = 0
    for jid, pub in candidates:
        try:
            db.update_job(jid, published_at=pub)
            ok += 1
        except Exception as e:
            err += 1; print(f"  回填失败 {jid}: {str(e)[:120]}")
    print(f"\n已回填 {ok} 个,失败 {err} 个。")


asyncio.run(main())
PY
