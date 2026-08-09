#!/bin/sh

# 从三家官方 latest 通道安装 CLI 到系统 PATH。厂商安装器负责平台选择和产物校验。
# INSTALL_CLAUDE_CODE / INSTALL_QODER_CLI / INSTALL_CODEX_CLI = 0 供合成集成栈跳过。
set -eu

work="$(mktemp -d /tmp/flori-cli-install.XXXXXX)"
trap 'rm -rf "$work"' EXIT

fetch_installer() {
    url="$1"
    out="$2"
    curl -fsSL --retry 5 --retry-all-errors --retry-delay 5 --connect-timeout 30 \
        --speed-limit 1024 --speed-time 300 --max-time 900 "$url" -o "$out"
}

extract_version() {
    grep -Eo '[0-9]+\.[0-9]+\.[0-9]+([-+][0-9A-Za-z.-]+)?' | head -n 1
}

record_version() {
    name="$1"
    command_version="$2"
    expected="$3"
    version="$(printf '%s\n' "$command_version" | extract_version)"
    [ -n "$version" ] || { echo "$name version output is invalid: $command_version" >&2; return 1; }
    if [ -n "$expected" ] && [ "$version" != "$expected" ]; then
        echo "$name version mismatch: expected=$expected actual=$version" >&2
        return 1
    fi
    printf 'FLORI_CLI_VERSION %s=%s\n' "$name" "$version"
}

record_channel_version() {
    name="$1"
    version="$2"
    if ! printf '%s\n' "$version" | grep -Eq \
        '^[0-9]+\.[0-9]+\.[0-9]+([-+][0-9A-Za-z.-]+)?$'; then
        echo "$name official channel returned an invalid version: $version" >&2
        return 1
    fi
    printf 'FLORI_CLI_CHANNEL %s=%s\n' "$name" "$version"
}

resolve_channels() {
    claude_file="$work/claude-stable"
    qoder_file="$work/qoder-latest.json"
    codex_file="$work/codex-latest.json"
    fetch_installer "https://downloads.claude.ai/claude-code-releases/stable" "$claude_file"
    fetch_installer \
        "https://qoder-ide.oss-accelerate.aliyuncs.com/qodercli/channels/manifest.json" \
        "$qoder_file"
    fetch_installer "https://releases.openai.com/codex/channels/latest" "$codex_file"

    claude="$(tr -d '[:space:]' < "$claude_file")"
    qoder="$(sed -n 's/.*"latest"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' \
        "$qoder_file" | head -n 1)"
    codex="$(sed -n 's/.*"tag_name"[[:space:]]*:[[:space:]]*"rust-v\([^"]*\)".*/\1/p' \
        "$codex_file" | head -n 1)"
    record_channel_version claude "$claude"
    record_channel_version qoder "$qoder"
    record_channel_version codex "$codex"
}

if [ "${1:-}" = "resolve" ]; then
    [ "$#" -eq 1 ] || { echo "usage: $0 [resolve]" >&2; exit 2; }
    resolve_channels
    exit 0
fi
[ "$#" -eq 0 ] || { echo "usage: $0 [resolve]" >&2; exit 2; }

install_claude() {
    if [ "${INSTALL_CLAUDE_CODE:-1}" = "0" ]; then
        echo "claude-code skipped for synthetic integration"
        return 0
    fi

    installer="$work/claude-install.sh"
    installer_home="$work/claude-home"
    mkdir -p "$installer_home" /usr/local/lib/flori-cli
    fetch_installer "https://claude.ai/install.sh" "$installer"
    target="${CLAUDE_CLI_VERSION:-stable}"
    # CI 传入刚从官方 stable channel 解析的版本;本地缺省仍直接跟随 stable。
    HOME="$installer_home" timeout 1800 bash "$installer" "$target"
    binary="$(readlink -f "$installer_home/.local/bin/claude")"
    [ -x "$binary" ] || { echo "Claude installer did not create an executable" >&2; return 1; }
    install -m 0755 "$binary" /usr/local/lib/flori-cli/claude
    printf '%s\n' \
        '#!/bin/sh' \
        'export DISABLE_AUTOUPDATER=1 DISABLE_UPDATES=1' \
        'exec /usr/local/lib/flori-cli/claude "$@"' \
        > /usr/local/bin/claude
    chmod 0755 /usr/local/bin/claude
    record_version claude "$(claude --version)" "${CLAUDE_CLI_VERSION:-}"
}

install_qoder() {
    if [ "${INSTALL_QODER_CLI:-1}" = "0" ]; then
        echo "qoder-cli skipped for synthetic integration"
        return 0
    fi

    installer="$work/qoder-install.sh"
    installer_home="$work/qoder-home"
    mkdir -p "$installer_home" /usr/local/lib/flori-cli /etc/flori
    fetch_installer "https://qoder.com/install" "$installer"
    if [ -n "${QODER_CLI_VERSION:-}" ]; then
        HOME="$installer_home" timeout 1800 bash "$installer" \
            --version "$QODER_CLI_VERSION" --skip-path
    else
        # 本地缺省不传版本,直接解析官方 latest channel。
        HOME="$installer_home" timeout 1800 bash "$installer" --skip-path
    fi
    binary="$(readlink -f "$installer_home/.local/bin/qodercli")"
    [ -x "$binary" ] || { echo "Qoder installer did not create an executable" >&2; return 1; }
    install -m 0755 "$binary" /usr/local/lib/flori-cli/qodercli
    printf '%s\n' \
        '{"general":{"enableAutoUpdate":false,"enableAutoUpdateNotification":false}}' \
        > /etc/flori/qoder-settings.json
    printf '%s\n' \
        '#!/bin/sh' \
        'exec /usr/local/lib/flori-cli/qodercli --settings /etc/flori/qoder-settings.json "$@"' \
        > /usr/local/bin/qodercli
    chmod 0755 /usr/local/bin/qodercli
    record_version qoder "$(qodercli --version)" "${QODER_CLI_VERSION:-}"
}

install_codex() {
    if [ "${INSTALL_CODEX_CLI:-1}" = "0" ]; then
        echo "codex-cli skipped for synthetic integration"
        return 0
    fi

    installer="$work/codex-install.sh"
    fetch_installer "https://chatgpt.com/codex/install.sh" "$installer"
    # 官方 standalone installer 保留 codex-path 与 codex-resources 的完整沙箱布局。
    CODEX_RELEASE="${CODEX_CLI_VERSION:-latest}" \
    CODEX_NON_INTERACTIVE=1 \
    CODEX_INSTALL_DIR=/usr/local/bin \
    CODEX_HOME=/usr/local/lib/codex \
        timeout 1800 sh "$installer"
    record_version codex "$(codex --version)" "${CODEX_CLI_VERSION:-}"
}

install_claude
install_qoder
install_codex
