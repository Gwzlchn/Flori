#!/usr/bin/env bash
# 端到端 AI 集成测试：真实 AI 笔记生成
# 需要: KIMI_API_KEY 或 DEEPSEEK_API_KEY 环境变量
# 需要: TEST_VIDEO_FILE 环境变量指向测试视频
set -uo pipefail

API="http://localhost:8000"
PASS=0
FAIL=0
RESULTS=()

log()  { echo "[$(date +%H:%M:%S)] $*"; }
pass() { PASS=$((PASS+1)); RESULTS+=("PASS $1"); log "PASS: $1"; }
fail() { FAIL=$((FAIL+1)); RESULTS+=("FAIL $1: $2"); log "FAIL: $1 - $2"; }

wait_job_done() {
  local job_id=$1 timeout=${2:-600} elapsed=0
  while [ $elapsed -lt $timeout ]; do
    local status
    status=$(curl --noproxy '*' -sf "$API/api/jobs/$job_id" | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])" 2>/dev/null)
    case "$status" in
      done)   return 0 ;;
      failed) return 1 ;;
    esac
    sleep 10
    elapsed=$((elapsed+10))
    if [ $((elapsed % 60)) -eq 0 ]; then
      log "  [$job_id] ${elapsed}s elapsed..."
    fi
  done
  return 2
}

report_steps() {
  local job_id=$1
  curl --noproxy '*' -sf "$API/api/jobs/$job_id" | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(f'  Status: {d[\"status\"]}  Progress: {d[\"progress_pct\"]}%')
for s in d['steps']:
    dur = f'{s[\"duration_sec\"]}s' if s.get('duration_sec') else ''
    err = (s.get('error','') or '')[:60]
    label = {'done':'OK','skipped':'SKIP','failed':'FAIL','waiting':'WAIT','ready':'READY','running':'RUN'}.get(s['status'],'?')
    print(f'  {label:5s} {s[\"name\"]:20s} {s[\"status\"]:10s} {dur:>8s}  {err}')
"
}

verify_notes() {
  local job_id=$1 min_len=${2:-500}
  local notes
  notes=$(curl --noproxy '*' -s "$API/api/jobs/$job_id/notes/smart")
  local notes_len=${#notes}

  if [ "$notes_len" -lt "$min_len" ]; then
    echo "  notes_smart: ${notes_len} 字符 (期望 >$min_len)"
    return 1
  fi

  local has_headings
  has_headings=$(echo "$notes" | grep -c "^##" || true)
  if [ "$has_headings" -eq 0 ]; then
    echo "  notes_smart: 无 ## 标题结构"
    return 1
  fi

  echo "  notes_smart: ${notes_len} 字符, ${has_headings} 个标题 OK"

  # 验证 review
  local review
  review=$(curl --noproxy '*' -s "$API/api/jobs/$job_id/review")
  local overall
  overall=$(echo "$review" | python3 -c "import sys,json; print(json.load(sys.stdin).get('overall','?'))" 2>/dev/null)
  echo "  review overall: $overall"

  return 0
}

# 测试开始
log "=== E2E 集成测试:真实 AI 笔记生成 ==="
log ""

VIDEO_FILE="${TEST_VIDEO_FILE:?请设置 TEST_VIDEO_FILE 环境变量}"

# TC-AI-1:视频上传到全 pipeline(含 AI 笔记)
log "TC-AI-1: 视频上传到全 pipeline + AI 笔记 (domain=deep-learning)"
log "  文件: $(du -m "$VIDEO_FILE" | cut -f1)MB"
RESP=$(curl --noproxy '*' -s -X POST "$API/api/jobs/upload" \
  -F "file=@$VIDEO_FILE" \
  -F "domain=deep-learning" \
  -F 'style_tags=["case-study"]')
JOB1=$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['job_id'])")
log "  Job: $JOB1"

if wait_job_done "$JOB1" 1800; then
  report_steps "$JOB1"
  if verify_notes "$JOB1" 500; then
    pass "TC-AI-1: 视频全 pipeline + AI 笔记完成"
  else
    fail "TC-AI-1" "笔记质量不达标"
  fi
else
  report_steps "$JOB1"
  fail "TC-AI-1" "任务未在超时内完成"
fi

log ""

# TC-AI-2:PDF 上传到 paper pipeline + AI 笔记
log "TC-AI-2: PDF 上传到 paper pipeline + AI 笔记 (domain=ml)"
RESP=$(curl --noproxy '*' -s -X POST "$API/api/jobs/upload" \
  -F "file=@/tmp/test_paper.pdf" \
  -F "domain=ml")
JOB2=$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['job_id'])")
log "  Job: $JOB2"

if wait_job_done "$JOB2" 600; then
  report_steps "$JOB2"
  if verify_notes "$JOB2" 500; then
    pass "TC-AI-2: 论文全 pipeline + AI 笔记完成"
  else
    fail "TC-AI-2" "笔记质量不达标"
  fi
else
  report_steps "$JOB2"
  fail "TC-AI-2" "任务未在超时内完成"
fi

log ""

# 报告
log "======================================="
log "AI 集成测试报告  $(date +%Y-%m-%d\ %H:%M)"
log "======================================="
for r in "${RESULTS[@]}"; do
  log "  $r"
done
log "---------------------------------------"
log "通过: $PASS  失败: $FAIL  总计: $((PASS+FAIL))"
log "======================================="

exit $FAIL
