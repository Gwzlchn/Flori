# syntax=docker/dockerfile:1.7

FROM rust:1.95-bookworm AS build
WORKDIR /src
ENV SQLX_OFFLINE=true
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
    cargo build --locked --release -p flori-server && \
    cp -a /usr/local/cargo/registry/. /cargo-cache/
COPY .sqlx ./.sqlx
COPY crates/flori-core ./crates/flori-core
COPY crates/flori-pipeline ./crates/flori-pipeline
COPY crates/flori-server ./crates/flori-server
COPY crates/flori-store ./crates/flori-store
COPY pipelines ./pipelines
COPY prompts ./prompts
RUN find crates/flori-core crates/flori-pipeline crates/flori-server crates/flori-store \
      -type f -exec touch {} + && \
    cargo build --locked --release -p flori-server && \
    cp target/release/flori-server /tmp/flori-server

FROM debian:bookworm-slim
RUN --mount=type=cache,id=flori-apt-bookworm,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,id=flori-apt-lists-bookworm,target=/var/lib/apt/lists,sharing=locked \
    rm -f /etc/apt/apt.conf.d/docker-clean && \
    apt-get update && DEBCONF_NOWARNINGS=yes DEBIAN_FRONTEND=noninteractive \
      apt-get install -y --no-install-recommends ca-certificates
COPY --from=build /tmp/flori-server /usr/local/bin/flori-server
USER 65532:65532
ENTRYPOINT ["flori-server"]
