#!/usr/bin/env bash
# .done -> manifest-v1 迁移运维入口(设计稿 §2.11 阶段 B/C/D;全容器内,宿主不装依赖)。
#
# 用法:
#   scripts/migrate-step-manifests.sh report                       # 默认:只读报告,不写对象
#   scripts/migrate-step-manifests.sh backfill [--accept-legacy-definition=current]
#   scripts/migrate-step-manifests.sh verify                       # 阶段C:双向闭合 + 全量SHA重验
#   scripts/migrate-step-manifests.sh cleanup                      # 阶段D:只删 .{step}.done(须先 verify 全绿 + exact DR)
#   任意子命令可加 --job <job_id>(可多次)限定范围。
#
# 生产执行顺序铁律:report → backfill → verify 全绿 → exact DR 备份 → 切 manifest-only → cleanup。
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"

SERVICE="${MIGRATE_SERVICE:-api}"
exec docker compose run --rm --no-deps "$SERVICE" \
  python -m shared.step_manifest_migration "$@"
