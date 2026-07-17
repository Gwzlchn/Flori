#!/bin/sh

# 安装 yt-dlp 使用的 Deno runtime,并用同一不可变 release 的 checksum 验证产物。
set -eu

case "${TARGETARCH:-amd64}" in
    amd64) deno_asset="deno-x86_64-unknown-linux-gnu.zip" ;;
    arm64) deno_asset="deno-aarch64-unknown-linux-gnu.zip" ;;
    *) echo "unsupported TARGETARCH=${TARGETARCH:-}" >&2; exit 1 ;;
esac

base="https://github.com/denoland/deno/releases/download/${DENO_VERSION}"
curl -fL --retry 5 --retry-all-errors --retry-delay 5 --connect-timeout 30 \
    "${base}/${deno_asset}" -o "/tmp/${deno_asset}"
curl -fL --retry 5 --retry-all-errors --retry-delay 5 --connect-timeout 30 \
    "${base}/${deno_asset}.sha256sum" -o "/tmp/${deno_asset}.sha256sum"
cd /tmp
sha256sum -c "${deno_asset}.sha256sum"
python -c 'import zipfile; zipfile.ZipFile("/tmp/'"${deno_asset}"'").extract("deno", "/usr/local/bin")'
chmod 0755 /usr/local/bin/deno
rm -f "/tmp/${deno_asset}" "/tmp/${deno_asset}.sha256sum"
deno --version
