# syntax=docker/dockerfile:1.7

FROM rust:1.95-bookworm AS build
WORKDIR /src
COPY Cargo.toml Cargo.lock ./
COPY crates/flori-core/Cargo.toml crates/flori-core/Cargo.toml
COPY crates/flori-pipeline/Cargo.toml crates/flori-pipeline/Cargo.toml
COPY crates/flori-runner/Cargo.toml crates/flori-runner/Cargo.toml
COPY crates/flori-server/Cargo.toml crates/flori-server/Cargo.toml
COPY crates/flori-store/Cargo.toml crates/flori-store/Cargo.toml
COPY xtask/Cargo.toml xtask/Cargo.toml
RUN mkdir -p crates/flori-core/src crates/flori-pipeline/src crates/flori-runner/src/bin \
      crates/flori-server/src crates/flori-store/src xtask/src && \
    printf '%s\n' '' > crates/flori-core/src/lib.rs && \
    printf '%s\n' '' > crates/flori-pipeline/src/lib.rs && \
    printf '%s\n' '' > crates/flori-runner/src/lib.rs && \
    printf '%s\n' 'fn main() {}' > crates/flori-runner/src/bin/media.rs && \
    printf '%s\n' 'fn main() {}' > crates/flori-runner/src/bin/qoder.rs && \
    printf '%s\n' 'fn main() {}' > crates/flori-runner/src/bin/codex.rs && \
    printf '%s\n' 'fn main() {}' > crates/flori-server/src/main.rs && \
    printf '%s\n' '' > crates/flori-store/src/lib.rs && \
    printf '%s\n' 'fn main() {}' > xtask/src/main.rs
RUN --mount=type=cache,id=flori-cargo-registry,target=/cargo-cache,sharing=locked \
    cp -a /cargo-cache/. /usr/local/cargo/registry/ && \
    cargo build --locked --release -p flori-runner --no-default-features \
      --features qoder --bin flori-runner-ai-qoder && \
    cp -a /usr/local/cargo/registry/. /cargo-cache/
COPY crates/flori-core ./crates/flori-core
COPY crates/flori-runner ./crates/flori-runner
RUN find crates/flori-core crates/flori-runner -type f -exec touch {} + && \
    cargo build --locked --release -p flori-runner --no-default-features \
      --features qoder --bin flori-runner-ai-qoder && \
    cp target/release/flori-runner-ai-qoder /tmp/flori-runner-ai-qoder

FROM node:22.23.2-bookworm-slim
ENV NPM_CONFIG_UPDATE_NOTIFIER=false
RUN --mount=type=cache,id=flori-apt-bookworm,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,id=flori-apt-lists-bookworm,target=/var/lib/apt/lists,sharing=locked \
    rm -f /etc/apt/apt.conf.d/docker-clean && \
    apt-get update && DEBCONF_NOWARNINGS=yes DEBIAN_FRONTEND=noninteractive \
      apt-get install -y --no-install-recommends ca-certificates && \
    groupadd --system --gid 65532 flori && \
    useradd --system -K SYS_UID_MAX=65532 --uid 65532 --gid 65532 \
      --home-dir /home/flori --create-home flori && \
    install -d -o 65532 -g 65532 /var/lib/flori-runner/spool
RUN --mount=type=cache,id=flori-runner-qoder-npm,target=/root/.npm,sharing=locked \
    npm install --global --omit=dev --no-audit --no-fund @qoder-ai/qodercli@1.1.26 && \
    install -d -o 65532 -g 65532 /home/flori/.qoder && \
    printf '%s\n' '{"general":{"enableAutoUpdate":false}}' > /home/flori/.qoder/settings.json && \
    chown 65532:65532 /home/flori/.qoder/settings.json
COPY --from=build /tmp/flori-runner-ai-qoder /usr/local/bin/flori-runner-ai-qoder
LABEL org.flori.runner.kind="ai-qoder"
LABEL org.flori.runner.tool.qoder_cli="1.1.26"
ENV HOME=/home/flori \
    QODER_CONFIG_DIR=/home/flori/.qoder \
    FLORI_RUNNER_SPOOL_DIR=/var/lib/flori-runner/spool
USER 65532:65532
RUN test "$(qodercli --version)" = "1.1.26" && \
    help="$(qodercli --help)" && printf '%s\n' "$help" | grep -F -- '--tools'
ENTRYPOINT ["flori-runner-ai-qoder"]
