#!/usr/bin/env bash
# 在锁定的 Python/Node 容器中生成或核对 selected OpenAPI 与 TypeScript。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

MODE="${1:---write}"
case "$MODE" in
  --write)
    docker compose -f docker-compose.test.yml run --rm test \
      python scripts/generate_selected_openapi.py
    docker compose -f docker-compose.fe-test.yml run --rm fe-test \
      sh -c 'npm install --no-audit --no-fund && npx openapi-typescript openapi/openapi.json -o src/types/generated/api.ts'
    ;;
  --check)
    docker compose -f docker-compose.test.yml run --rm test \
      python scripts/generate_selected_openapi.py --check
    docker compose -f docker-compose.fe-test.yml run --rm fe-test \
      sh -c 'npm install --no-audit --no-fund && npx openapi-typescript openapi/openapi.json -o /tmp/flori-api.ts >/dev/null && cmp -s /tmp/flori-api.ts src/types/generated/api.ts'
    ;;
  *)
    echo "usage: scripts/generate-frontend-wire.sh [--write|--check]" >&2
    exit 2
    ;;
esac
