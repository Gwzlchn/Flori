#!/usr/bin/env bash
# 清理数据库里滞留/过时的 job(按状态 + 年龄筛),走 API 的 DELETE 端点 —— 与前端"删除任务"
# 完全同一套逻辑(DB 级联删 jobs/FTS/集合计数/glossary 悬空源 + 删产物目录 + 通知 worker)。
# 典型场景:一批投递卡在 pending/processing(worker 当时不可用),成了僵尸 job 反复被 recover。
#
# 用法:
#   scripts/purge-jobs.sh                         # dry-run:列出将删的 job(默认 pending,processing)
#   scripts/purge-jobs.sh --apply                 # 真删
#   scripts/purge-jobs.sh --status failed --older-than 30 --apply
# 选项:
#   --status S1,S2     目标状态(默认 pending,processing;可加 failed)
#   --older-than DAYS  只删早于 N 天创建的(默认 0 = 该状态全部)
#   --apply            真正删除(不加=dry-run 只列)
# 环境:API_CONTAINER(默认 flori-api)。删除经容器内 localhost:8000,需 API 放行(默认本机放行)。
set -euo pipefail

usage() { sed -n '2,18p' "$0"; exit "${1:-0}"; }
STATUS="pending,processing"; DAYS=0; APPLY=0
while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help) usage 0 ;;
    --status) STATUS="${2:?}"; shift 2 ;;
    --older-than) DAYS="${2:?}"; shift 2 ;;
    --apply) APPLY=1; shift ;;
    *) echo "未知参数: $1" >&2; usage 1 ;;
  esac
done
API_CONTAINER="${API_CONTAINER:-flori-api}"

docker exec -e PURGE_STATUS="$STATUS" -e PURGE_DAYS="$DAYS" -e PURGE_APPLY="$APPLY" -i \
  "$API_CONTAINER" python3 - <<'PY'
import os, urllib.request
from datetime import datetime, timezone
from shared.db import Database

statuses = {s.strip() for s in os.environ["PURGE_STATUS"].split(",") if s.strip()}
days = int(os.environ["PURGE_DAYS"]); apply = os.environ["PURGE_APPLY"] == "1"
db = Database("/data/db/analyzer.db")
now = datetime.now(timezone.utc)
_, jobs = db.list_jobs(limit=100000)

def stale(j):
    if j.status not in statuses:
        return False
    if days and j.created_at and (now - j.created_at).days < days:
        return False
    return True

targets = [j for j in jobs if stale(j)]
print(f"匹配 {len(targets)} 个 job (status in {sorted(statuses)}, older-than {days}d):")
for j in targets:
    age = (now - j.created_at).days if j.created_at else "?"
    print(f"  {j.id:34} {str(j.status):20} age={age}d  {(j.title or '')[:30]}")
if not targets:
    print("无匹配,无需处理。"); raise SystemExit(0)
if not apply:
    print("\n[dry-run] 未删除。确认无误后加 --apply 执行。"); raise SystemExit(0)

ok = err = 0
for j in targets:
    try:
        req = urllib.request.Request(f"http://localhost:8000/api/jobs/{j.id}", method="DELETE")
        urllib.request.urlopen(req, timeout=30).close()
        ok += 1
    except Exception as e:
        err += 1; print(f"  删除失败 {j.id}: {str(e)[:120]}")
print(f"\n已删除 {ok} 个,失败 {err} 个。")
PY
