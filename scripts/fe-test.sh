#!/bin/sh
# 前端门禁在同一 Node 容器内共用一次依赖安装.
set -eu

case "${FE_INSTALL_MODE:-install}" in
  ci) npm ci --prefer-offline --no-audit --no-fund ;;
  install) npm install --no-audit --no-fund ;;
  *) echo "FE_INSTALL_MODE 必须是 ci 或 install" >&2; exit 2 ;;
esac

gate_tmp="$(mktemp -d /tmp/flori-fe-gates.XXXXXX)"
cleanup() {
  rm -rf -- "$gate_tmp"
}
trap cleanup EXIT

# 三个静态门只读源码,并行后再单独跑 Vitest,避免抢占测试 CPU.
(npx vue-tsc --noEmit) >"$gate_tmp/typecheck.log" 2>&1 &
typecheck_pid="$!"
(npm run typecheck:test) >"$gate_tmp/typecheck-test.log" 2>&1 &
typecheck_test_pid="$!"
(
  npx openapi-typescript openapi/openapi.json -o "$gate_tmp/flori-api.ts"
  cmp -s "$gate_tmp/flori-api.ts" src/types/generated/api.ts
) >"$gate_tmp/openapi.log" 2>&1 &
openapi_pid="$!"

failed=0
if ! wait "$typecheck_pid"; then failed=1; fi
cat "$gate_tmp/typecheck.log"
if ! wait "$typecheck_test_pid"; then failed=1; fi
cat "$gate_tmp/typecheck-test.log"
if ! wait "$openapi_pid"; then failed=1; fi
cat "$gate_tmp/openapi.log"
if [ "$failed" -ne 0 ]; then
  echo "前端静态门失败" >&2
  exit 1
fi

npx vitest run --coverage "$@"
