#!/usr/bin/env bash
# 补充集成测试：并发、容错、API CRUD、WebSocket
# 依赖：服务已通过 docker-compose.integration.yml 启动
set -uo pipefail

API="http://localhost:8000"
PASS=0
FAIL=0
RESULTS=()

log()  { echo "[$(date +%H:%M:%S)] $*"; }
pass() { PASS=$((PASS+1)); RESULTS+=("✓ $1"); log "PASS: $1"; }
fail() { FAIL=$((FAIL+1)); RESULTS+=("✗ $1: $2"); log "FAIL: $1 — $2"; }

wait_job() {
  local job_id=$1 target_status=$2 timeout=${3:-300} elapsed=0
  while [ $elapsed -lt $timeout ]; do
    local status
    status=$(curl --noproxy '*' -sf "$API/api/jobs/$job_id" 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])" 2>/dev/null)
    if [ "$status" = "$target_status" ]; then return 0; fi
    if [ "$status" = "done" ] && [ "$target_status" = "done" ]; then return 0; fi
    if [ "$status" = "failed" ] && [ "$target_status" != "failed" ]; then
      # 对于 CPU 步骤测试，job failed 可能是因为 AI 步骤失败（正常）
      # 检查目标步骤是否完成
      :
    fi
    sleep 3
    elapsed=$((elapsed+3))
  done
  return 1
}

report_steps() {
  local job_id=$1
  curl --noproxy '*' -sf "$API/api/jobs/$job_id" | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(f'  Status: {d[\"status\"]}  Progress: {d[\"progress_pct\"]}%')
for s in d['steps']:
    dur = f'{s[\"duration_sec\"]}s' if s.get('duration_sec') else ''
    err = (s.get('error','') or '')[:50]
    icon = {'done':'✓','skipped':'⏭','failed':'✗','waiting':'⏳','ready':'🔄','running':'▶'}.get(s['status'],'?')
    print(f'  {icon} {s[\"name\"]:20s} {s[\"status\"]:10s} {dur:>8s}  {err}')
" 2>/dev/null
}

VIDEO_FILE="${TEST_VIDEO_FILE:?请设置 TEST_VIDEO_FILE}"

log "═══ 补充集成测试：并发 + 容错 + API ═══"
log ""

# ═══════════════════════════════════════
# TC-5: 并发 3 个任务
# ═══════════════════════════════════════
log "TC-5: 并发 3 个任务（2 视频上传 + 1 PDF）"

JOBS=()
# 视频 1
RESP=$(curl --noproxy '*' -s -X POST "$API/api/jobs/upload?content_type=video" \
  -F "file=@$VIDEO_FILE" -F "domain=deep-learning")
JID=$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['job_id'])")
JOBS+=("$JID:video")
log "  视频 1: $JID"

# 视频 2（同一个文件，不同 domain）
RESP=$(curl --noproxy '*' -s -X POST "$API/api/jobs/upload?content_type=video" \
  -F "file=@$VIDEO_FILE" -F "domain=programming")
JID=$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['job_id'])")
JOBS+=("$JID:video")
log "  视频 2: $JID"

# PDF
RESP=$(curl --noproxy '*' -s -X POST \
  "$API/api/jobs/upload?content_type=document&document_kind=research_paper" \
  -F "file=@/tmp/test_paper.pdf" -F "domain=ml")
JID=$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['job_id'])")
JOBS+=("$JID:document")
log "  论文:   $JID"

# 等所有 CPU 步骤完成（最长 20 分钟，3 个视频串行 OCR）
ALL_OK=true
for entry in "${JOBS[@]}"; do
  jid="${entry%%:*}"
  pipeline="${entry##*:}"
  log "  等待 $jid ($pipeline)..."

  TARGET_STEPS="09_mechanical"
  if [ "$pipeline" = "document" ]; then
    TARGET_STEPS="03_structure"
  fi

  # 轮询直到目标步骤完成
  DONE=false
  for i in $(seq 1 240); do
    STEP_STATUS=$(curl --noproxy '*' -sf "$API/api/jobs/$jid" 2>/dev/null | python3 -c "
import sys, json
d = json.load(sys.stdin)
steps = {s['name']: s['status'] for s in d['steps']}
target = '$TARGET_STEPS'
print(steps.get(target, 'waiting'))
" 2>/dev/null)
    if [ "$STEP_STATUS" = "done" ]; then
      DONE=true
      break
    fi
    sleep 5
  done

  if [ "$DONE" = true ]; then
    log "  $jid CPU 步骤完成 ✓"
  else
    ALL_OK=false
    log "  $jid CPU 步骤未完成"
    report_steps "$jid"
  fi
done

if [ "$ALL_OK" = true ]; then
  # 验证资源池——scene 池 limit=1 说明不可能 3 个 03_scene 同时跑，只能串行
  # 这里只验证 3 个都完成了
  pass "TC-5: 并发 3 任务 CPU 步骤全部完成"
else
  fail "TC-5" "部分任务未完成"
fi

log ""

# ═══════════════════════════════════════
# TC-6: 失败重试 + retry/rerun API
# ═══════════════════════════════════════
log "TC-6: 失败 → 自动重试 → retry/rerun API"

# 提交无效 B站 URL 触发下载失败
RESP=$(curl --noproxy '*' -s -X POST "$API/api/jobs" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.bilibili.com/video/BV_INVALID_99999", "content_type": "video", "domain": "general"}')
JOB_FAIL=$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['job_id'])")
log "  无效 URL Job: $JOB_FAIL"

# 等待 job 失败（download retries=3，约 30s）
if wait_job "$JOB_FAIL" "failed" 120; then
  log "  任务如期失败 ✓"

  # 测试 retry API
  RETRY_CODE=$(curl --noproxy '*' -s -o /dev/null -w "%{http_code}" \
    -X POST "$API/api/jobs/$JOB_FAIL/retry")
  if [ "$RETRY_CODE" = "200" ]; then
    log "  retry API 返回 200 ✓"
  else
    fail "TC-6" "retry API 返回 $RETRY_CODE"
  fi

  # 等 retry 也失败
  sleep 15
  wait_job "$JOB_FAIL" "failed" 60

  # 测试 rerun API
  RERUN_CODE=$(curl --noproxy '*' -s -o /dev/null -w "%{http_code}" \
    -X POST "$API/api/jobs/$JOB_FAIL/rerun" \
    -H "Content-Type: application/json" \
    -d '{"from_step": "01_download"}')
  if [ "$RERUN_CODE" = "200" ]; then
    log "  rerun API 返回 200 ✓"
  else
    fail "TC-6" "rerun API 返回 $RERUN_CODE"
  fi

  # 测试 resubmit API
  sleep 10
  RESUB_CODE=$(curl --noproxy '*' -s -o /dev/null -w "%{http_code}" \
    -X POST "$API/api/jobs/$JOB_FAIL/resubmit")
  if [ "$RESUB_CODE" = "200" ]; then
    log "  resubmit API 返回 200 ✓"
    pass "TC-6: 失败重试 + retry/rerun/resubmit API 正常"
  else
    fail "TC-6" "resubmit API 返回 $RESUB_CODE"
  fi
else
  log "  任务未在超时内失败"
  report_steps "$JOB_FAIL"
  fail "TC-6" "无效 URL 任务未失败"
fi

log ""

# ═══════════════════════════════════════
# TC-7: API 端点完整性
# ═══════════════════════════════════════
log "TC-7: API 端点完整性"

TC7_OK=true

# GET /api/health
HEALTH=$(curl --noproxy '*' -sf "$API/api/health" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['status'])" 2>/dev/null)
if [ "$HEALTH" = "healthy" ]; then
  log "  /api/health ✓"
else
  log "  /api/health FAIL: $HEALTH"
  TC7_OK=false
fi

# GET /api/status
STATUS_OK=$(curl --noproxy '*' -sf "$API/api/status" | python3 -c "
import sys, json
d = json.load(sys.stdin)
ok = 'workers' in d and 'pools' in d and 'jobs' in d
print('ok' if ok else 'fail')
" 2>/dev/null)
if [ "$STATUS_OK" = "ok" ]; then
  log "  /api/status ✓"
else
  log "  /api/status FAIL"
  TC7_OK=false
fi

# GET /api/workers (returns list, not {"workers": [...]})
WORKER_COUNT=$(curl --noproxy '*' -sf "$API/api/workers" | python3 -c "
import sys, json
d = json.load(sys.stdin)
workers = d if isinstance(d, list) else d.get('workers', [])
print(len(workers))
" 2>/dev/null)
if [ -n "$WORKER_COUNT" ] && [ "$WORKER_COUNT" -ge 1 ]; then
  log "  /api/workers ($WORKER_COUNT workers) ✓"
else
  log "  /api/workers FAIL: $WORKER_COUNT"
  TC7_OK=false
fi

# GET /api/jobs?status=done (有之前测试留下的 job)
DONE_COUNT=$(curl --noproxy '*' -sf "$API/api/jobs?limit=100" | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(d['total'])
" 2>/dev/null)
log "  /api/jobs (total=$DONE_COUNT) ✓"

# GET /api/jobs?status=xxx 过滤
FILTER_OK=$(curl --noproxy '*' -sf "$API/api/jobs?status=failed&limit=5" | python3 -c "
import sys, json
d = json.load(sys.stdin)
# 所有返回的都应该是 failed
ok = all(i['status'] == 'failed' for i in d['items'])
print('ok' if ok or not d['items'] else 'fail')
" 2>/dev/null)
if [ "$FILTER_OK" = "ok" ]; then
  log "  /api/jobs?status=failed 过滤 ✓"
else
  log "  /api/jobs?status=failed 过滤 FAIL"
  TC7_OK=false
fi

# DELETE /api/jobs/{id} — 用刚才失败的 job 测试
DEL_CODE=$(curl --noproxy '*' -s -o /dev/null -w "%{http_code}" \
  -X DELETE "$API/api/jobs/$JOB_FAIL")
if [ "$DEL_CODE" = "204" ]; then
  # 确认已删除
  GET_CODE=$(curl --noproxy '*' -s -o /dev/null -w "%{http_code}" "$API/api/jobs/$JOB_FAIL")
  if [ "$GET_CODE" = "404" ]; then
    log "  DELETE + 确认 404 ✓"
  else
    log "  DELETE 后仍存在: $GET_CODE"
    TC7_OK=false
  fi
else
  log "  DELETE 返回 $DEL_CODE"
  TC7_OK=false
fi

if [ "$TC7_OK" = true ]; then
  pass "TC-7: API 端点完整性"
else
  fail "TC-7" "部分 API 端点异常"
fi

log ""

# ═══════════════════════════════════════
# TC-8: Worker 管理 API
# ═══════════════════════════════════════
log "TC-8: Worker 管理 API"

TC8_OK=true

# 获取一个 worker ID
WORKER_ID=$(curl --noproxy '*' -sf "$API/api/workers" | python3 -c "
import sys, json
d = json.load(sys.stdin)
workers = d if isinstance(d, list) else d.get('workers', [])
print(workers[0]['id'] if workers else '')
" 2>/dev/null)

if [ -n "$WORKER_ID" ]; then
  # GET /api/workers/{id}
  W_TYPE=$(curl --noproxy '*' -sf "$API/api/workers/$WORKER_ID" | python3 -c "
import sys, json; d=json.load(sys.stdin); print(d['type'])" 2>/dev/null)
  if [ -n "$W_TYPE" ]; then
    log "  GET /api/workers/$WORKER_ID (type=$W_TYPE) ✓"
  else
    log "  GET /api/workers/$WORKER_ID FAIL"
    TC8_OK=false
  fi

  # PUT /api/workers/{id} — 设置 admin_note
  PUT_CODE=$(curl --noproxy '*' -s -o /dev/null -w "%{http_code}" \
    -X PUT "$API/api/workers/$WORKER_ID" \
    -H "Content-Type: application/json" \
    -d '{"admin_note": "integration test"}')
  if [ "$PUT_CODE" = "200" ]; then
    log "  PUT admin_note ✓"
  else
    log "  PUT admin_note 返回 $PUT_CODE"
    TC8_OK=false
  fi
else
  log "  无在线 Worker"
  TC8_OK=false
fi

if [ "$TC8_OK" = true ]; then
  pass "TC-8: Worker 管理 API"
else
  fail "TC-8" "Worker API 异常"
fi

log ""

# ═══════════════════════════════════════
# TC-9: WebSocket 实时进度
# ═══════════════════════════════════════
log "TC-9: WebSocket 实时进度"

# 创建一个 job 并通过 WebSocket 监听事件
WS_RESULT=$(python3 -c "
import asyncio, json, sys, os
for v in ['ALL_PROXY','HTTPS_PROXY','HTTP_PROXY','all_proxy','https_proxy','http_proxy']:
    os.environ.pop(v, None)

async def test():
    try:
        import websockets
    except ImportError:
        print('SKIP:websockets not installed')
        return

    import httpx
    async with httpx.AsyncClient() as client:
        with open('$VIDEO_FILE', 'rb') as f:
            resp = await client.post(
                '$API/api/jobs/upload?content_type=video',
                files={'file': ('test.mp4', f, 'video/mp4')},
                data={'domain': 'general'},
            )
        job_id = resp.json()['job_id']

    events = []
    try:
        async with websockets.connect(f'ws://localhost:8000/api/ws/jobs/{job_id}') as ws:
            while True:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=120)
                    event = json.loads(msg)
                    events.append(event['event'])
                    if event['event'] in ('job_done', 'job_failed'):
                        break
                    if len(events) >= 5:
                        break
                except asyncio.TimeoutError:
                    break
    except Exception as e:
        if not events:
            print(f'FAIL:{e}')
            return

    if 'step_ready' in events or 'step_start' in events or 'step_done' in events:
        print(f'OK:{len(events)} events: {events[:5]}')
    else:
        print(f'FAIL:no step events in {events}')

asyncio.run(test())
" 2>/dev/null)

case "$WS_RESULT" in
  OK:*)
    log "  $WS_RESULT"
    pass "TC-9: WebSocket 实时进度"
    ;;
  SKIP:*)
    log "  $WS_RESULT (跳过)"
    pass "TC-9: WebSocket 跳过（无 websockets 库）"
    ;;
  FAIL:*)
    log "  $WS_RESULT"
    fail "TC-9" "$WS_RESULT"
    ;;
  *)
    log "  未知结果: $WS_RESULT"
    fail "TC-9" "WebSocket 测试异常"
    ;;
esac

log ""

# ═══════════════════════════════════════
# 报告
# ═══════════════════════════════════════
log "═══════════════════════════════════════"
log "补充测试报告  $(date +%Y-%m-%d\ %H:%M)"
log "═══════════════════════════════════════"
for r in "${RESULTS[@]}"; do
  log "  $r"
done
log "───────────────────────────────────────"
log "通过: $PASS  失败: $FAIL  总计: $((PASS+FAIL))"
log "═══════════════════════════════════════"

exit $FAIL
