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

install_claude() {
    if [ "${INSTALL_CLAUDE_CODE:-1}" = "0" ]; then
        echo "claude-code skipped for synthetic integration"
        return 0
    fi

    installer="$work/claude-install.sh"
    installer_home="$work/claude-home"
    mkdir -p "$installer_home" /usr/local/lib/flori-cli
    fetch_installer "https://claude.ai/install.sh" "$installer"
    # stable 每次解析当前稳定版,不在镜像定义中保留版本或校验和。
    HOME="$installer_home" timeout 1800 bash "$installer" stable
    binary="$(readlink -f "$installer_home/.local/bin/claude")"
    [ -x "$binary" ] || { echo "Claude installer did not create an executable" >&2; return 1; }
    install -m 0755 "$binary" /usr/local/lib/flori-cli/claude
    printf '%s\n' \
        '#!/bin/sh' \
        'export DISABLE_AUTOUPDATER=1 DISABLE_UPDATES=1' \
        'exec /usr/local/lib/flori-cli/claude "$@"' \
        > /usr/local/bin/claude
    chmod 0755 /usr/local/bin/claude
    claude --version
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
    # 不传 --version 即解析官方 latest 通道。--skip-path 避免修改临时 HOME 的 profile。
    HOME="$installer_home" timeout 1800 bash "$installer" --skip-path
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
    qodercli --version
}

install_codex() {
    if [ "${INSTALL_CODEX_CLI:-1}" = "0" ]; then
        echo "codex-cli skipped for synthetic integration"
        return 0
    fi

    installer="$work/codex-install.sh"
    fetch_installer "https://chatgpt.com/codex/install.sh" "$installer"
    # 官方 standalone installer 保留 codex-path 与 codex-resources 的完整沙箱布局。
    CODEX_RELEASE=latest \
    CODEX_NON_INTERACTIVE=1 \
    CODEX_INSTALL_DIR=/usr/local/bin \
    CODEX_HOME=/usr/local/lib/codex \
        timeout 1800 sh "$installer"
    codex --version
}

install_claude
install_qoder
install_codex
