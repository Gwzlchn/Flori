# syntax=docker/dockerfile:1.7

FROM rust:1.95-bookworm AS build
WORKDIR /src
COPY Cargo.toml Cargo.lock ./
COPY crates ./crates
COPY xtask ./xtask
RUN --mount=type=cache,id=flori-cargo-registry,target=/usr/local/cargo/registry,sharing=locked \
    --mount=type=cache,id=flori-runner-codex-target,target=/src/target,sharing=locked \
    cargo build --locked --release -p flori-runner --no-default-features \
      --features codex --bin flori-runner-ai-codex && \
    cp target/release/flori-runner-ai-codex /tmp/flori-runner-ai-codex

FROM node:22.23.2-bookworm-slim AS codex-install
RUN --mount=type=cache,id=flori-runner-codex-npm,target=/root/.npm,sharing=locked \
    npm install --global --omit=dev --no-audit --no-fund @openai/codex@0.148.0
RUN native="$(find /usr/local/lib/node_modules/@openai/codex/node_modules/@openai \
      -type f -path '*/vendor/*/bin/codex' -print -quit)" && \
    test -n "$native" && \
    cp -a "${native%/bin/codex}" /opt/codex

FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && \
    rm -rf /var/lib/apt/lists/* && \
    groupadd --system --gid 65532 flori && \
    useradd --system --uid 65532 --gid 65532 --home-dir /home/flori --create-home flori && \
    install -d -o 65532 -g 65532 /var/lib/flori-runner/spool
COPY --from=build /tmp/flori-runner-ai-codex /usr/local/bin/flori-runner-ai-codex
COPY --from=codex-install /opt/codex /opt/codex
RUN ln -s /opt/codex/bin/codex /usr/local/bin/codex && \
    install -d -o 65532 -g 65532 /home/flori/.codex && \
    printf '%s\n' 'check_for_update_on_startup = false' 'cli_auth_credentials_store = "file"' \
      > /home/flori/.codex/config.toml && \
    chown 65532:65532 /home/flori/.codex/config.toml
LABEL org.flori.runner.kind="ai-codex"
LABEL org.flori.runner.tool.codex_cli="0.148.0"
ENV HOME=/home/flori \
    CODEX_HOME=/home/flori/.codex \
    FLORI_RUNNER_SPOOL_DIR=/var/lib/flori-runner/spool
USER 65532:65532
RUN test "$(codex --version)" = "codex-cli 0.148.0" && \
    help="$(codex --help)" && printf '%s\n' "$help" | grep -F -- '--search' && \
    help="$(codex exec --help)" && printf '%s\n' "$help" | grep -F -- '--json' && \
    printf '%s\n' "$help" | grep -F -- '--output-schema'
ENTRYPOINT ["flori-runner-ai-codex"]
