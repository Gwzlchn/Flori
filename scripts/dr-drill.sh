#!/usr/bin/env bash
# 在容器内运行隔离的空环境灾备演练,不挂载或修改现有 Flori 数据.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
FLORI_DR_IMAGE="${FLORI_DR_IMAGE:-python:3.11-slim}"
DRILL_RESULT_DIR="${DRILL_RESULT_DIR:-./backups}"
DRILL_RESULT_FILE="${DRILL_RESULT_FILE:-}"

usage() {
  cat <<'EOF'
用法: scripts/dr-drill.sh [--result-file <JSON>]

演练在一次性 Python 容器的临时目录内构建 DB、job、prompt profile、Redis、
MinIO 和配置样本，随后验证:
  - 完整备份只在 manifest/checksum/integrity 通过后发布。
  - 损坏归档 fail-closed，不修改已有目标。
  - 空环境恢复后 DB 查询、job、profile、Redis、MinIO 和配置均可验证。
  - 跨资产中断会回滚已切换目标。

结果 JSON 记录所有检查与实测 RPO/RTO。脚本不传入 Docker 套接字、现有卷或 secret。
EOF
  exit "${1:-0}"
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    -h|--help) usage 0 ;;
    --result-file)
      [ "$#" -ge 2 ] || usage 1
      DRILL_RESULT_FILE="$2"
      shift 2
      ;;
    *) echo "未知选项: $1" >&2; usage 1 ;;
  esac
done

command -v docker >/dev/null 2>&1 || { echo "错误: 找不到 docker" >&2; exit 1; }
[ -f "$SCRIPT_DIR/dr_snapshot.py" ] || { echo "错误: 缺少 scripts/dr_snapshot.py" >&2; exit 1; }
if [ -z "$DRILL_RESULT_FILE" ]; then
  mkdir -p "$DRILL_RESULT_DIR"
  DRILL_RESULT_FILE="$DRILL_RESULT_DIR/flori-dr-drill-$(date -u +%Y%m%dT%H%M%SZ)-$$.json"
fi
mkdir -p "$(dirname "$DRILL_RESULT_FILE")"
RESULT_DIR="$(cd "$(dirname "$DRILL_RESULT_FILE")" && pwd)"
RESULT_NAME="$(basename "$DRILL_RESULT_FILE")"

echo "==> 启动隔离空环境灾备演练"
docker run --rm \
  -v "$SCRIPT_DIR/dr_snapshot.py:/tool/dr_snapshot.py:ro" \
  -v "$RESULT_DIR:/result" \
  "$FLORI_DR_IMAGE" \
  python /tool/dr_snapshot.py drill \
    --result-file "/result/$RESULT_NAME" \
    --owner-uid "$(id -u)" \
    --owner-gid "$(id -g)"

echo "==> 演练通过"
echo "    result: $DRILL_RESULT_FILE"
