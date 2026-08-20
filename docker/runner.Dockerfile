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
    rm -rf /var/lib/apt/lists/*
COPY --from=build /tmp/flori-runner /usr/local/bin/flori-runner
USER 65532:65532
ENTRYPOINT ["flori-runner"]

FROM runner-base AS runner-media
LABEL org.flori.runner.kind="media"

FROM runner-base AS runner-ai-qoder
LABEL org.flori.runner.kind="ai-qoder"

FROM runner-base AS runner-ai-codex
LABEL org.flori.runner.kind="ai-codex"
