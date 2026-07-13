#!/usr/bin/env bash
# 真实素材 · paper pipeline 端到端 CI 回归
# ════════════════════════════════════════════════════════════════════════
# 目的(审计缺口 #7 的实质覆盖):用一个**自带的微型 PDF**(tests/fixtures/sample.pdf)
# 把 paper 这条最轻的真实链路从 upload 一路跑到 done,在 GitHub-hosted runner 上
# 无需任何外部网络 / arXiv / B站 / 真实 API key。验证的是
#   DAG ↔ scheduler ↔ worker ↔ step 接线 + 真实解析,而非仅探活。
#
# 各步真假一览(DRY_RUN=1):
#   01_download   真(upload 模式:文件已落 input/source.pdf,本步只抽 metadata,不下载)
#   02_pdf_parse  真(pdfinfo 页数 + PDF metadata/首页标题兜底,pdf-only 不逆向抽正文)
#   03_sections   真(页区间章节树)
#   04_translate_paper / 05_smart_paper / 05_concepts / 06_review
#                   合成(DRY_RUN → DryRunProvider,不调真实 AI)
# 即:下载/解析/章节是**真跑**,AI 步用合成产物替身——既不需 key,
# 又把 CPU 解析链 + AI 步落盘/接线全程压到。
#
# 真实视频 / B站·arXiv 联网 / 真实 AI 笔记链路仍是人工/自托管覆盖
# (tests/integration/run_e2e_cpu.sh / run_e2e_ai.sh,见 docs/12-cicd.md)。
#
# 用法:
#   bash tests/integration/ci_paper_e2e.sh
# 可调环境:
#   COMPOSE_PROJECT_NAME  compose 项目名(默认 flori-ci-paper;本地跑务必与生产栈隔离!)
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
PROJECT="${COMPOSE_PROJECT_NAME:-flori-ci-paper}"
# 用独立项目名 + 自定义端口,确保与本机可能在跑的生产栈完全隔离(尤其 down -v 不误删)。
COMPOSE=(docker compose -p "$PROJECT" -f docker-compose.integration.yml)
export DRY_RUN=1          # AI 步走 DryRunProvider,无需任何 API key
export DATA_DIR=/data
# 集成 compose 把 8000 写死映射;用 API_PORT 覆盖到宿主机,避免与生产栈撞口。
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

# 集成 compose 把宿主端口写死为 8000;若需改口,这里直接改 yml 的映射不在本脚本职责内,
# 故仅当 API_PORT≠8000 时给出提示(CI 上始终用 8000)。
if [ "$API_PORT" != "8000" ]; then
  log "提示:API_PORT=$API_PORT,但 docker-compose.integration.yml 映射的是 8000;"
  log "      本脚本不改 compose 文件——本地撞口请改用独立 PROJECT 并停掉占用方,或临时改映射。"
fi

# ── 1) 起栈(DRY_RUN) ────────────────────────────────────────────────────
log "构建集成镜像(项目 $PROJECT)..."
"${COMPOSE[@]}" build redis api scheduler worker-cpu worker-ai \
  || die "镜像构建失败"

log "拉起栈:redis api scheduler worker-cpu worker-ai (DRY_RUN=1)..."
# paper 链需要的池:io+cpu(worker-cpu)、ai(worker-ai);不需要 worker-download(upload 模式不下载)。
"${COMPOSE[@]}" up -d redis api scheduler worker-cpu worker-ai \
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

# ── 3) 上传 fixture(.pdf → paper pipeline) ───────────────────────────────
log "上传 fixture → POST ${API}/api/jobs/upload (domain=test)"
RESP="$(curl -fsS --noproxy '*' -X POST "${API}/api/jobs/upload" \
  -F "file=@${FIXTURE}" \
  -F "domain=test" \
  -F 'style_tags=[]')" || { "${COMPOSE[@]}" logs api; die "上传请求失败"; }
log "  响应: $RESP"

JOB_ID="$(printf '%s' "$RESP" | python3 -c 'import sys,json; print(json.load(sys.stdin)["job_id"])')" \
  || die "无法从响应解析 job_id"
CONTENT_TYPE="$(printf '%s' "$RESP" | python3 -c 'import sys,json; print(json.load(sys.stdin)["content_type"])')"
log "  job_id=$JOB_ID  content_type=$CONTENT_TYPE"
[ "$CONTENT_TYPE" = "paper" ] || die "期望 content_type=paper,实得 '$CONTENT_TYPE'(.pdf 应识别为 paper)"

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
# (a) 智能笔记(05_smart_paper 落盘的版本化笔记;DRY_RUN 合成但路径/接线真实)
SMART_CODE="$(curl -s -o /tmp/ci_smart.md -w '%{http_code}' --noproxy '*' \
  "${API}/api/jobs/${JOB_ID}/notes/smart")"
[ "$SMART_CODE" = "200" ] || die "GET notes/smart 非 200(实得 $SMART_CODE)"
SMART_LEN="$(wc -c < /tmp/ci_smart.md)"
[ "$SMART_LEN" -gt 0 ] || die "notes/smart 为空"
log "notes/smart 200 ✓ (${SMART_LEN} 字节)"

# (b) 评审(06_review → output/review.json)
REVIEW_CODE="$(curl -s -o /tmp/ci_review.json -w '%{http_code}' --noproxy '*' \
  "${API}/api/jobs/${JOB_ID}/review")"
[ "$REVIEW_CODE" = "200" ] || die "GET review 非 200(实得 $REVIEW_CODE)"
python3 -c 'import sys,json; json.load(open("/tmp/ci_review.json"))' \
  || die "review.json 不是合法 JSON"
log "review 200 + 合法 JSON ✓"

# (c) 真实解析产物:sections.json 非空(03_sections 真跑的硬证据)。
#     经 worker-cpu 容器读 /data,避免依赖宿主机直接访问命名卷。
SECTIONS_N="$("${COMPOSE[@]}" exec -T worker-cpu python3 -c "
import json
d = json.load(open('/data/jobs/${JOB_ID}/intermediate/sections.json'))
print(d.get('total_sections', 0))
" 2>/dev/null | tr -dc '0-9')"
if [ -n "$SECTIONS_N" ] && [ "$SECTIONS_N" -gt 0 ]; then
  log "sections.json 真实非空 ✓ (total_sections=${SECTIONS_N})"
else
  die "sections.json 为空/缺失(real parse 未产出章节)"
fi

log "════════════════════════════════════════"
log "PASS: paper pipeline 真实素材 E2E 全程到 done"
log "  真跑: 01_download(upload) · 02_pdf_parse · 03_sections"
log "  合成: 04_translate_paper · 05_smart_paper · 05_concepts · 06_review (DRY_RUN)"
log "════════════════════════════════════════"
exit 0
