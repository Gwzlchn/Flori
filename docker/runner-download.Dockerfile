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
    for bin in media qoder codex download; do \
      printf '%s\n' 'fn main() {}' > "crates/flori-runner/src/bin/$bin.rs"; \
    done && \
    printf '%s\n' 'fn main() {}' > crates/flori-server/src/main.rs && \
    printf '%s\n' '' > crates/flori-store/src/lib.rs && \
    printf '%s\n' 'fn main() {}' > xtask/src/main.rs
RUN --mount=type=cache,id=flori-cargo-registry,target=/cargo-cache,sharing=locked \
    cp -a /cargo-cache/. /usr/local/cargo/registry/ && \
    cargo build --locked --release -p flori-runner --no-default-features \
      --features download --bin flori-runner-download && \
    cp -a /usr/local/cargo/registry/. /cargo-cache/
COPY crates/flori-core ./crates/flori-core
COPY crates/flori-runner ./crates/flori-runner
RUN find crates/flori-core crates/flori-runner -type f -exec touch {} + && \
    cargo build --locked --release -p flori-runner --no-default-features \
      --features download --bin flori-runner-download && \
    cp target/release/flori-runner-download /tmp/flori-runner-download

FROM debian:bookworm-slim
RUN --mount=type=cache,id=flori-apt-bookworm,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,id=flori-apt-lists-bookworm,target=/var/lib/apt/lists,sharing=locked \
    --mount=type=cache,id=flori-download-pip,target=/root/.cache/pip,sharing=locked \
    rm -f /etc/apt/apt.conf.d/docker-clean && \
    apt-get update && DEBCONF_NOWARNINGS=yes DEBIAN_FRONTEND=noninteractive \
      apt-get install -y --no-install-recommends \
      ca-certificates ffmpeg=7:5.1.9-0+deb12u1 python3=3.11.2-1+b1 python3-pip=23.0.1+dfsg-1 && \
    python3 -m pip install --break-system-packages --root-user-action=ignore \
      yutto==2.3.0 yt-dlp==2026.8.19 && \
    apt-get purge -y --auto-remove python3-pip && \
    groupadd --system --gid 65532 flori && \
    useradd --system -K SYS_UID_MAX=65532 --uid 65532 --gid 65532 \
      --home-dir /home/flori --create-home flori && \
    install -d -o 65532 -g 65532 /var/lib/flori-runner/spool
COPY --from=build /tmp/flori-runner-download /usr/local/bin/flori-runner-download
LABEL org.flori.runner.kind="download"
LABEL org.flori.runner.tool.yutto="2.3.0"
LABEL org.flori.runner.tool.yt_dlp="2026.8.19"
LABEL org.flori.runner.tool.ffprobe="5.1.9"
ENV HOME=/home/flori \
    FLORI_RUNNER_SPOOL_DIR=/var/lib/flori-runner/spool
USER 65532:65532
RUN test "$(yutto --version)" = "yutto 2.3.0" && \
    test "$(yt-dlp --version)" = "2026.08.19" && \
    ffprobe -version | head -1 | grep -F 'ffprobe version 5.1.9'
ENTRYPOINT ["flori-runner-download"]
