#!/usr/bin/env bash
# Document PDF/research_paper 端到端 CI 回归。
# 自带微型 PDF 无需外网或 API key。01_download、02_parse、03_structure 真跑，
# 04_translate、05_smart、06_semantic_attestation、07_concepts、08_review 用
# DRY_RUN 合成响应，覆盖 DAG、scheduler、worker、step 和文件契约接线。
#
# 真实视频 / B站·arXiv 联网 / 真实 AI 笔记链路仍是人工/自托管覆盖
# (tests/integration/run_e2e_cpu.sh / run_e2e_ai.sh,见 docs/12-cicd.md)。
#
# 用法:
#   bash tests/integration/ci_document_pdf_e2e.sh
# 可调环境:
#   COMPOSE_PROJECT_NAME  compose 项目名(默认 flori-ci-document-pdf)
#   API_PORT              宿主机映射端口(默认 8000;本地若 8000 被占用可改,如 18000)
#   JOB_TIMEOUT           job 跑到 done 的总超时秒数(默认 480)
#   KEEP_STACK=1          结束不拆栈(排查用)
set -uo pipefail

# ── 配置 ─────────────────────────────────────────────────────────────────
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

FIXTURE="$ROOT/tests/fixtures/sample.pdf"
API_PORT="${API_PORT:-8000}"
API="http://localhost:${API_PORT}"
JOB_TIMEOUT="${JOB_TIMEOUT:-480}"
PROJECT="${COMPOSE_PROJECT_NAME:-flori-ci-document-pdf}"
# 用独立项目名 + 自定义端口,确保与本机可能在跑的生产栈完全隔离(尤其 down -v 不误删)。
COMPOSE=(docker compose -p "$PROJECT" -f docker-compose.integration.yml)
export DRY_RUN=1          # AI 步走 DryRunProvider,无需任何 API key
export DATA_DIR=/data
# API_PORT 同时驱动 compose 映射和探活地址,避免本地活栈占用默认端口。
export DOCKER_DEFAULT_PLATFORM="${DOCKER_DEFAULT_PLATFORM:-}"

log()  { echo "[$(date +%H:%M:%S)] $*"; }
die()  { echo "::error::$*" 2>/dev/null; log "FATAL: $*"; exit 1; }

# ── 拆栈(trap:无论成功失败都执行) ──────────────────────────────────────
teardown() {
  local rc=$?
  if [ "${KEEP_STACK:-0}" = "1" ]; then
    log "KEEP_STACK=1,保留栈(项目 $PROJECT)以便排查"
    return
  fi
  log "拆栈(down -v,项目 $PROJECT)..."
  "${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
  return $rc
}
trap teardown EXIT

# ── 0) 前置检查 ──────────────────────────────────────────────────────────
[ -f "$FIXTURE" ] || die "缺少 fixture: $FIXTURE"
# 字节级哨兵:确认是 PDF 且非空,避免提交了空壳文件。
head -c4 "$FIXTURE" | grep -q '%PDF' || die "fixture 不是合法 PDF(缺 %PDF 头): $FIXTURE"
log "fixture: $FIXTURE ($(wc -c < "$FIXTURE") 字节)"

# ── 1) 起栈(DRY_RUN) ────────────────────────────────────────────────────
log "构建集成镜像(项目 $PROJECT)..."
"${COMPOSE[@]}" build redis api scheduler worker-cpu worker-io worker-ai \
  || die "镜像构建失败"

log "拉起栈:redis api scheduler worker-io worker-cpu worker-ai (DRY_RUN=1)..."
# 上传源仍由 01_download 在 io 池完成格式归一化,三类 Worker 必须都在线。
"${COMPOSE[@]}" up -d redis api scheduler worker-io worker-cpu worker-ai \
  || die "栈启动失败"

# ── 2) 探活 API ──────────────────────────────────────────────────────────
log "等待 API 就绪(${API}/openapi.json)..."
ready=0
for i in $(seq 1 40); do
  if curl -fsS --noproxy '*' "${API}/openapi.json" >/dev/null 2>&1; then
    log "API 就绪(第 ${i} 次探测)"; ready=1; break
  fi
  sleep 3
done
[ "$ready" = "1" ] || { "${COMPOSE[@]}" logs api scheduler; die "API 在 120s 内未就绪"; }

log "等待 io/cpu/ai Worker 注册并通过在线判定..."
workers_ready=0
for i in $(seq 1 40); do
  if curl -fsS --noproxy '*' "${API}/api/workers" 2>/dev/null | python3 -c '
import json, sys
workers = json.load(sys.stdin)
online_pools = {
    pool
    for worker in workers
    if str(worker.get("status", "")).startswith("online")
    for pool in worker.get("pools", [])
}
raise SystemExit(0 if {"io", "cpu", "ai"}.issubset(online_pools) else 1)
'; then
    log "Worker 就绪(第 ${i} 次探测)"; workers_ready=1; break
  fi
  sleep 2
done
[ "$workers_ready" = "1" ] || {
  "${COMPOSE[@]}" logs scheduler worker-io worker-cpu worker-ai
  die "io/cpu/ai Worker 在 80s 内未全部就绪"
}

# 3) 上传 fixture(.pdf → Document/research_paper)
log "上传 fixture → POST ${API}/api/jobs/upload (domain=test)"
RESP="$(curl -fsS --noproxy '*' -X POST \
  "${API}/api/jobs/upload?content_type=document&document_kind=research_paper" \
  -F "file=@${FIXTURE}" \
  -F "domain=test" \
  -F 'style_tags=[]')" || { "${COMPOSE[@]}" logs api; die "上传请求失败"; }
log "  响应: $RESP"

JOB_ID="$(printf '%s' "$RESP" | python3 -c 'import sys,json; print(json.load(sys.stdin)["job_id"])')" \
  || die "无法从响应解析 job_id"
CONTENT_TYPE="$(printf '%s' "$RESP" | python3 -c 'import sys,json; print(json.load(sys.stdin)["content_type"])')"
PIPELINE="$(printf '%s' "$RESP" | python3 -c 'import sys,json; print(json.load(sys.stdin)["pipeline"])')"
DOCUMENT_KIND="$(printf '%s' "$RESP" | python3 -c 'import sys,json; print(json.load(sys.stdin)["document_kind"])')"
log "  job_id=$JOB_ID content_type=$CONTENT_TYPE pipeline=$PIPELINE document_kind=$DOCUMENT_KIND"
[ "$CONTENT_TYPE" = "document" ] || die "期望 content_type=document,实得 '$CONTENT_TYPE'"
[ "$PIPELINE" = "document" ] || die "期望 pipeline=document,实得 '$PIPELINE'"
[ "$DOCUMENT_KIND" = "research_paper" ] || die "期望 document_kind=research_paper,实得 '$DOCUMENT_KIND'"

# ── 4) 轮询直到 done(或失败/超时) ──────────────────────────────────────
report_steps() {
  # 注:python 3.11 的 f-string 表达式内不能含反斜杠,故这里用 .format()/取局部变量,避免转义。
  curl -fsS --noproxy '*' "${API}/api/jobs/${JOB_ID}" 2>/dev/null | python3 -c '
import sys, json
d = json.load(sys.stdin)
print("  status={}  progress={}%".format(d["status"], d.get("progress_pct")))
for s in d.get("steps", []):
    name = s["name"]; st = s["status"]
    dur = "{}s".format(s["duration_sec"]) if s.get("duration_sec") else ""
    err = (s.get("error") or "")[:70]
    icon = {"done":"OK ","skipped":"-- ","failed":"XX ","waiting":".. ","ready":">> ","running":"** "}.get(st, "?? ")
    print("  {}{:16s} {:9s} {:>7s}  {}".format(icon, name, st, dur, err))
' || true
}

log "轮询 job 至 done(超时 ${JOB_TIMEOUT}s)..."
elapsed=0; final=""
while [ "$elapsed" -lt "$JOB_TIMEOUT" ]; do
  STATUS="$(curl -fsS --noproxy '*' "${API}/api/jobs/${JOB_ID}" 2>/dev/null \
    | python3 -c 'import sys,json; print(json.load(sys.stdin)["status"])' 2>/dev/null)"
  case "$STATUS" in
    done)   final="done";   break ;;
    failed) final="failed"; break ;;
  esac
  sleep 5; elapsed=$((elapsed+5))
  if [ $((elapsed % 30)) -eq 0 ]; then log "  ...${elapsed}s (status=${STATUS:-?})"; fi
done

log "最终步骤状态:"
report_steps

if [ "$final" != "done" ]; then
  log "导出 worker 日志以便排查:"
  "${COMPOSE[@]}" logs --tail 120 scheduler worker-cpu worker-ai || true
  if [ "$final" = "failed" ]; then die "job 进入 failed 状态"; fi
  die "job 未在 ${JOB_TIMEOUT}s 内到达 done(末态 ${STATUS:-?})"
fi
log "job 到达 done ✓"

# ── 5) 断言真实产物落盘且可读 ────────────────────────────────────────────
# (a) 智能笔记(05_smart 落盘的版本化笔记;DRY_RUN 合成但路径/接线真实)
SMART_CODE="$(curl -s -o /tmp/ci_smart.md -w '%{http_code}' --noproxy '*' \
  "${API}/api/jobs/${JOB_ID}/notes/smart")"
[ "$SMART_CODE" = "200" ] || die "GET notes/smart 非 200(实得 $SMART_CODE)"
SMART_LEN="$(wc -c < /tmp/ci_smart.md)"
[ "$SMART_LEN" -gt 0 ] || die "notes/smart 为空"
log "notes/smart 200 ✓ (${SMART_LEN} 字节)"

# (b) 评审(08_review → output/review.json)
REVIEW_CODE="$(curl -s -o /tmp/ci_review.json -w '%{http_code}' --noproxy '*' \
  "${API}/api/jobs/${JOB_ID}/review")"
[ "$REVIEW_CODE" = "200" ] || die "GET review 非 200(实得 $REVIEW_CODE)"
python3 -c 'import sys,json; json.load(open("/tmp/ci_review.json"))' \
  || die "review.json 不是合法 JSON"
log "review 200 + 合法 JSON ✓"

# (c) 真实 Document 解析、结构投影、译文契约全部存在且内部一致。
PARSE_SUMMARY="$("${COMPOSE[@]}" exec -T worker-cpu python3 -c "
import json
from pathlib import Path
root = Path('/data/jobs/${JOB_ID}')
document = json.load(open(root / 'intermediate/document.json'))
quality = json.load(open(root / 'intermediate/quality.json'))
manifest = json.load(open(root / 'intermediate/source_segments.json'))
translation = json.load(open(root / 'output/translation.json'))
assert document['content_type'] == 'document'
assert document['document_kind'] == 'research_paper'
assert document['blocks']
assert quality['status'] in {'complete', 'degraded'}
assert manifest['segments']
assert translation['segments']
for rel in ('intermediate/document_index.md', 'output/translated.html'):
    assert (root / rel).stat().st_size > 0
print(len(document['blocks']), len(manifest['segments']), len(translation['segments']))
" 2>/dev/null)" || die "Document 结构/质量/锚点/译文产物断言失败"
log "Document 真实产物 ✓ (blocks/source_segments/translation_segments=${PARSE_SUMMARY})"

log "════════════════════════════════════════"
log "PASS: Document PDF/research_paper 真实素材 E2E 全程到 done"
log "  真跑: 01_download(upload) · 02_parse · 03_structure"
log "  合成: 04_translate · 05_smart · 06_semantic_attestation · 07_concepts · 08_review"
log "════════════════════════════════════════"
exit 0
