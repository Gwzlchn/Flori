# syntax=docker/dockerfile:1.7

FROM rust:1.95-bookworm AS build
WORKDIR /src
COPY Cargo.toml Cargo.lock ./
COPY crates ./crates
COPY xtask ./xtask
RUN --mount=type=cache,id=flori-cargo-registry,target=/usr/local/cargo/registry,sharing=locked \
    --mount=type=cache,id=flori-runner-qoder-target,target=/src/target,sharing=locked \
    cargo build --locked --release -p flori-runner --no-default-features \
      --features qoder --bin flori-runner-ai-qoder && \
    cp target/release/flori-runner-ai-qoder /tmp/flori-runner-ai-qoder

FROM node:22.23.2-bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && \
    rm -rf /var/lib/apt/lists/* && \
    groupadd --system --gid 65532 flori && \
    useradd --system --uid 65532 --gid 65532 --home-dir /home/flori --create-home flori && \
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
