#!/usr/bin/env bash
# 清理工作树里的临时/构建产物(均已 gitignore,非 git 污染)。
# e2e 冒烟容器以 root 运行写出的 output/e2e 等可能是 root 属主,普通用户删不掉时用 sudo。
set -euo pipefail
cd "$(dirname "$0")/.."

targets=(
  "output/e2e"
  "_dist/ui-jobdetail-after.png"
  "_dist/ui-jobdetail-log.png"
)

for t in "${targets[@]}"; do
  [ -e "$t" ] || continue
  if rm -rf "$t" 2>/dev/null; then
    echo "removed: $t"
  else
    echo "需 sudo(root 属主): sudo rm -rf $t"
  fi
done

echo "完成。若有「需 sudo」项,请按提示手动清理(e2e 容器以 root 写出所致)。"
