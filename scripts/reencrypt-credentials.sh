#!/usr/bin/env bash
# 把 app_credentials 表里的每条凭证按"当前 FLORI_SECRET_KEY"重新加密落库。
# 逐行 get_credential(解密或明文透传) → set_credential(用当前 key 重新加密)。
# 用途:
#   - 首次为已有明文凭证打加密(刚设好 FLORI_SECRET_KEY 后跑一次);
#   - 轮换 key:先用旧 key 起容器解出明文(或解不出则跳过/丢弃后重登)、改 .env 为新 key、再跑本脚本。
# 幂等:已是当前 key 的 token 解开再加密,语义等价;Fernet token 含时间戳,重写后字节值会变。
#
# 用法:
#   scripts/reencrypt-credentials.sh            # dry-run:列出将处理的 key(不写库)
#   scripts/reencrypt-credentials.sh --apply    # 真正重写
# 环境:API_CONTAINER(默认 flori-api)。容器内须已设 FLORI_SECRET_KEY 且镜像含 cryptography。
# 注意:_fernet 按容器内 FLORI_SECRET_KEY 缓存,故须先让 api 容器带上新 key 再跑
#       (改 .env + 重建/重启容器);否则等同明文回写。
set -euo pipefail

usage() { sed -n '2,20p' "$0"; exit "${1:-0}"; }
APPLY=0
while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help) usage 0 ;;
    --apply) APPLY=1; shift ;;
    *) echo "未知参数: $1" >&2; usage 1 ;;
  esac
done
API_CONTAINER="${API_CONTAINER:-flori-api}"

docker exec -e REENC_APPLY="$APPLY" -i "$API_CONTAINER" python3 - <<'PY'
import os
from shared.db import Database, _fernet

apply = os.environ.get("REENC_APPLY") == "1"
db = Database("/data/db/analyzer.db")

if _fernet() is None:
    print("FLORI_SECRET_KEY 未设置或无效(_fernet() 为 None);重写将以明文回写——已中止。")
    print("请先在 api 容器注入有效的 Fernet key(改 .env + 重建容器)再跑。")
    raise SystemExit(1)

keys = db._maintenance_credential_keys()
print(f"app_credentials 共 {len(keys)} 条:")
for k in keys:
    print(f"  - {k}")
if not keys:
    print("无凭证,无需处理。"); raise SystemExit(0)
if not apply:
    print("\n[dry-run] 未写库。确认无误后加 --apply 执行重新加密。"); raise SystemExit(0)

ok = 0
for k in keys:
    val = db.get_credential(k)          # 解密 / 明文透传
    if val is None:
        continue
    db.set_credential(k, val)           # 用当前 key 重新加密
    ok += 1
print(f"\n已用当前 key 重新加密 {ok} 条凭证。")
PY
