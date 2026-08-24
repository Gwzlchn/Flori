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
      --features media --bin flori-runner-media && \
    cp -a /usr/local/cargo/registry/. /cargo-cache/
COPY crates/flori-core ./crates/flori-core
COPY crates/flori-runner ./crates/flori-runner
RUN find crates/flori-core crates/flori-runner -type f -exec touch {} + && \
    cargo build --locked --release -p flori-runner --no-default-features \
      --features media --bin flori-runner-media && \
    cp target/release/flori-runner-media /tmp/flori-runner-media

FROM debian:bookworm-slim
RUN --mount=type=cache,id=flori-apt-bookworm,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,id=flori-apt-lists-bookworm,target=/var/lib/apt/lists,sharing=locked \
    --mount=type=cache,id=flori-pymupdf-pip,target=/root/.cache/pip,sharing=locked \
    rm -f /etc/apt/apt.conf.d/docker-clean && \
    apt-get update && DEBCONF_NOWARNINGS=yes DEBIAN_FRONTEND=noninteractive \
      apt-get install -y --no-install-recommends \
      ca-certificates \
      ffmpeg=7:5.1.9-0+deb12u1 \
      poppler-utils=22.12.0-2+deb12u3 \
      python3=3.11.2-1+b1 \
      python3-pip=23.0.1+dfsg-1 && \
    python3 -m pip install --break-system-packages --root-user-action=ignore PyMuPDF==1.27.2.3 && \
    apt-get purge -y --auto-remove python3-pip && \
    groupadd --system --gid 65532 flori && \
    useradd --system -K SYS_UID_MAX=65532 --uid 65532 --gid 65532 \
      --home-dir /home/flori --create-home flori && \
    install -d -o 65532 -g 65532 /var/lib/flori-runner/spool
COPY --from=build /tmp/flori-runner-media /usr/local/bin/flori-runner-media
LABEL org.flori.runner.kind="media"
LABEL org.flori.runner.tool.pdf_extractor="1.27.2.3"
LABEL org.flori.runner.tool.ffmpeg="5.1.9"
ENV HOME=/home/flori \
    FLORI_RUNNER_SPOOL_DIR=/var/lib/flori-runner/spool
USER 65532:65532
RUN test "$(python3 -I -c 'import fitz; print(fitz.VersionBind)')" = "1.27.2.3" && \
    pdfinfo -v 2>&1 | grep -F 'version 22.12.0' && \
    pdftotext -v 2>&1 | grep -F 'version 22.12.0' && \
    ffmpeg -version | head -1 | grep -F 'ffmpeg version 5.1.9' && \
    ffprobe -version | head -1 | grep -F 'ffprobe version 5.1.9'
ENTRYPOINT ["flori-runner-media"]
