# syntax=docker/dockerfile:1.7

FROM rust:1.95-bookworm AS build
WORKDIR /src
COPY Cargo.toml Cargo.lock ./
COPY crates ./crates
COPY xtask ./xtask
RUN --mount=type=cache,target=/usr/local/cargo/registry \
    --mount=type=cache,target=/src/target \
    cargo build --locked --release -p flori-runner && \
    cp target/release/flori-runner /tmp/flori-runner

FROM debian:bookworm-slim AS runner-base
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && \
    rm -rf /var/lib/apt/lists/* && \
    groupadd --system --gid 65532 flori && \
    useradd --system --uid 65532 --gid 65532 --home-dir /home/flori --create-home flori && \
    install -d -o 65532 -g 65532 /var/lib/flori-runner/spool
COPY --from=build /tmp/flori-runner /usr/local/bin/flori-runner
ENV HOME=/home/flori \
    FLORI_RUNNER_SPOOL_DIR=/var/lib/flori-runner/spool
USER 65532:65532
ENTRYPOINT ["flori-runner"]

FROM runner-base AS runner-media
LABEL org.flori.runner.kind="media"

FROM node:22.23.2-bookworm-slim AS runner-ai-base
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && \
    rm -rf /var/lib/apt/lists/* && \
    groupadd --system --gid 65532 flori && \
    useradd --system --uid 65532 --gid 65532 --home-dir /home/flori --create-home flori && \
    install -d -o 65532 -g 65532 /var/lib/flori-runner/spool
COPY --from=build /tmp/flori-runner /usr/local/bin/flori-runner
ENV HOME=/home/flori \
    FLORI_RUNNER_SPOOL_DIR=/var/lib/flori-runner/spool
USER 65532:65532
ENTRYPOINT ["flori-runner"]

FROM runner-ai-base AS runner-ai-qoder
USER root
RUN --mount=type=cache,target=/root/.npm,sharing=locked \
    npm install --global --omit=dev --no-audit --no-fund @qoder-ai/qodercli@1.1.26 && \
    install -d -o 65532 -g 65532 /home/flori/.qoder && \
    printf '%s\n' '{"general":{"enableAutoUpdate":false}}' > /home/flori/.qoder/settings.json && \
    chown 65532:65532 /home/flori/.qoder/settings.json
LABEL org.flori.runner.kind="ai-qoder"
LABEL org.flori.runner.tool.qoder_cli="1.1.26"
ENV QODER_CONFIG_DIR=/home/flori/.qoder
USER 65532:65532
RUN test "$(qodercli --version)" = "1.1.26"

FROM node:22.23.2-bookworm-slim AS codex-install
RUN --mount=type=cache,target=/root/.npm,sharing=locked \
    npm install --global --omit=dev --no-audit --no-fund @openai/codex@0.148.0
RUN native="$(find /usr/local/lib/node_modules/@openai/codex/node_modules/@openai \
      -type f -path '*/vendor/*/bin/codex' -print -quit)" && \
    test -n "$native" && \
    cp -a "${native%/bin/codex}" /opt/codex

FROM runner-base AS runner-ai-codex
USER root
COPY --from=codex-install /opt/codex /opt/codex
RUN ln -s /opt/codex/bin/codex /usr/local/bin/codex && \
    install -d -o 65532 -g 65532 /home/flori/.codex && \
    printf '%s\n' 'check_for_update_on_startup = false' 'cli_auth_credentials_store = "file"' \
      > /home/flori/.codex/config.toml && \
    chown 65532:65532 /home/flori/.codex/config.toml
LABEL org.flori.runner.kind="ai-codex"
LABEL org.flori.runner.tool.codex_cli="0.148.0"
ENV CODEX_HOME=/home/flori/.codex
USER 65532:65532
RUN test "$(codex --version)" = "codex-cli 0.148.0"
