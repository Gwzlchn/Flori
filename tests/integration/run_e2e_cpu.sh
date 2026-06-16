#!/usr/bin/env bash
# 端到端集成测试：下载 + CPU 步骤（不含 AI 笔记生成）
# 覆盖：视频上传、视频 URL 下载、论文上传、论文 URL 下载
set -uo pipefail

API="http://localhost:8000"
PASS=0
FAIL=0
RESULTS=()

log()  { echo "[$(date +%H:%M:%S)] $*"; }
pass() { PASS=$((PASS+1)); RESULTS+=("✓ $1"); log "PASS: $1"; }
fail() { FAIL=$((FAIL+1)); RESULTS+=("✗ $1: $2"); log "FAIL: $1 — $2"; }

wait_steps_done() {
  local job_id=$1 target_steps=$2 timeout=${3:-600} elapsed=0
  while [ $elapsed -lt $timeout ]; do
    local result
    result=$(curl --noproxy '*' -sf "$API/api/jobs/$job_id" | python3 -c "
import sys, json
d = json.load(sys.stdin)
targets = '$target_steps'.split(',')
steps = {s['name']: s['status'] for s in d['steps']}
all_done = all(steps.get(t) in ('done', 'skipped') for t in targets)
any_failed = d['status'] == 'failed'
if all_done:
    print('DONE')
elif any_failed:
    # 只看目标步骤是否失败
    failed_targets = [t for t in targets if steps.get(t) == 'failed']
    if failed_targets:
        print('FAILED:' + ','.join(failed_targets))
    elif all(steps.get(t) in ('done', 'skipped') for t in targets):
        print('DONE')
    else:
        print('RUNNING')
else:
    print('RUNNING')
" 2>/dev/null)
    case "$result" in
      DONE) return 0 ;;
      FAILED*) echo "  $result"; return 1 ;;
    esac
    sleep 5
    elapsed=$((elapsed+5))
    if [ $((elapsed % 30)) -eq 0 ]; then
      log "  [$job_id] ${elapsed}s elapsed..."
    fi
  done
  return 2  # timeout
}

report_steps() {
  local job_id=$1
  curl --noproxy '*' -sf "$API/api/jobs/$job_id" | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(f'  Status: {d[\"status\"]}  Progress: {d[\"progress_pct\"]}%')
for s in d['steps']:
    dur = f'{s[\"duration_sec\"]}s' if s.get('duration_sec') else ''
    err = s.get('error','')[:60] if s.get('error') else ''
    icon = {'done':'✓','skipped':'⏭','failed':'✗','waiting':'⏳','ready':'🔄','running':'▶'}.get(s['status'],'?')
    print(f'  {icon} {s[\"name\"]:20s} {s[\"status\"]:10s} {dur:>8s}  {err}')
"
}

# ═══════════════════════════════════════════
log "═══ E2E 集成测试：下载 + CPU 步骤 ═══"
log ""

# ─── TC-1: 视频上传（最短的 58MB 视频）───
VIDEO_FILE="${TEST_VIDEO_FILE:?请设置 TEST_VIDEO_FILE 环境变量指向一个测试用 mp4 文件}"

log "TC-1: 视频上传 → CPU 步骤链"
log "  文件: $(du -m "$VIDEO_FILE" | cut -f1)MB"
RESP=$(curl --noproxy '*' -s -X POST "$API/api/jobs/upload" \
  -F "file=@$VIDEO_FILE" \
  -F "domain=deep-learning" \
  -F 'style_tags=["case-study"]')
JOB1=$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['job_id'])")
log "  Job: $JOB1"

# 等 CPU 步骤完成（01_download → 03_scene → 04_frames → 05_dedup → 06_ocr → 09_mechanical）
# 07_danmaku 和 08_punctuate 可能 skip（取决于有无字幕/弹幕）
CPU_STEPS="01_download,03_scene,04_frames,05_dedup,06_ocr,09_mechanical"
if wait_steps_done "$JOB1" "$CPU_STEPS" 900; then
  report_steps "$JOB1"
  # 验证产物
  MECH_LEN=$(curl --noproxy '*' -s "$API/api/jobs/$JOB1/notes/mechanical" | wc -c)
  if [ "$MECH_LEN" -gt 500 ]; then
    pass "TC-1: 视频上传 CPU 步骤链完成 (mechanical=${MECH_LEN}字节)"
  else
    fail "TC-1" "notes_mechanical 太短: ${MECH_LEN}字节"
  fi
else
  report_steps "$JOB1"
  fail "TC-1" "CPU 步骤未在超时内完成"
fi

log ""

# ─── TC-2: 视频 URL 下载（真实 B站 BV 号）───
log "TC-2: B站 URL 下载 → CPU 步骤链"
BV_ID="BV11cXsBVEqc"
log "  BV: $BV_ID (390s/6.5min)"
RESP=$(curl --noproxy '*' -s -X POST "$API/api/jobs" \
  -H "Content-Type: application/json" \
  -d "{\"url\": \"$BV_ID\", \"content_type\": \"video\", \"domain\": \"general\"}")
JOB2=$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['job_id'])")
log "  Job: $JOB2"

if wait_steps_done "$JOB2" "$CPU_STEPS" 1200; then
  report_steps "$JOB2"
  MECH_LEN=$(curl --noproxy '*' -s "$API/api/jobs/$JOB2/notes/mechanical" | wc -c)
  if [ "$MECH_LEN" -gt 500 ]; then
    pass "TC-2: B站下载 + CPU 步骤链完成 (mechanical=${MECH_LEN}字节)"
  else
    fail "TC-2" "notes_mechanical 太短: ${MECH_LEN}字节"
  fi
else
  report_steps "$JOB2"
  fail "TC-2" "CPU 步骤未在超时内完成"
fi

log ""

# ─── TC-3: 论文 PDF 上传 ───
log "TC-3: PDF 上传 → paper pipeline CPU 步骤"
RESP=$(curl --noproxy '*' -s -X POST "$API/api/jobs/upload" \
  -F "file=@/tmp/test_paper.pdf" \
  -F "domain=ml")
JOB3=$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['job_id'])")
CT=$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['content_type'])")
log "  Job: $JOB3 (content_type=$CT)"

PAPER_CPU_STEPS="01_download,02_pdf_parse,03_sections,04_figures"
if wait_steps_done "$JOB3" "$PAPER_CPU_STEPS" 300; then
  report_steps "$JOB3"
  # 验证 sections.json 存在
  SECTIONS=$(docker compose -f docker-compose.integration.yml exec -T worker-cpu \
    python3 -c "import json; d=json.load(open('/data/jobs/$JOB3/intermediate/sections.json')); print(len(d))" 2>/dev/null)
  if [ -n "$SECTIONS" ] && [ "$SECTIONS" -gt 0 ]; then
    pass "TC-3: PDF 上传 paper pipeline CPU 完成 (sections=$SECTIONS)"
  else
    fail "TC-3" "sections.json 为空或不存在"
  fi
else
  report_steps "$JOB3"
  fail "TC-3" "paper CPU 步骤未在超时内完成"
fi

log ""

# ─── TC-4: arXiv URL 下载 ───
log "TC-4: arXiv URL → 下载 + paper pipeline CPU"
ARXIV_URL="https://arxiv.org/abs/2106.09685"
RESP=$(curl --noproxy '*' -s -X POST "$API/api/jobs" \
  -H "Content-Type: application/json" \
  -d "{\"url\": \"$ARXIV_URL\", \"content_type\": \"paper\", \"domain\": \"ml\"}")
JOB4=$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['job_id'])")
log "  Job: $JOB4"

if wait_steps_done "$JOB4" "$PAPER_CPU_STEPS" 300; then
  report_steps "$JOB4"
  SECTIONS=$(docker compose -f docker-compose.integration.yml exec -T worker-cpu \
    python3 -c "import json; d=json.load(open('/data/jobs/$JOB4/intermediate/sections.json')); print(len(d))" 2>/dev/null)
  if [ -n "$SECTIONS" ] && [ "$SECTIONS" -gt 0 ]; then
    pass "TC-4: arXiv 下载 + paper CPU 完成 (sections=$SECTIONS)"
  else
    fail "TC-4" "sections.json 为空或不存在"
  fi
else
  report_steps "$JOB4"
  fail "TC-4" "paper CPU 步骤未在超时内完成"
fi

log ""

# ─── 报告 ───
log "═══════════════════════════════════════"
log "E2E 测试报告  $(date +%Y-%m-%d\ %H:%M)"
log "═══════════════════════════════════════"
for r in "${RESULTS[@]}"; do
  log "  $r"
done
log "───────────────────────────────────────"
log "通过: $PASS  失败: $FAIL  总计: $((PASS+FAIL))"
log "═══════════════════════════════════════"

exit $FAIL
