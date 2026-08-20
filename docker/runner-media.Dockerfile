# syntax=docker/dockerfile:1.7

FROM rust:1.95-bookworm AS build
WORKDIR /src
COPY Cargo.toml Cargo.lock ./
COPY crates ./crates
COPY xtask ./xtask
RUN --mount=type=cache,id=flori-cargo-registry,target=/usr/local/cargo/registry,sharing=locked \
    --mount=type=cache,id=flori-runner-media-target,target=/src/target,sharing=locked \
    cargo build --locked --release -p flori-runner --no-default-features \
      --features media --bin flori-runner-media && \
    cp target/release/flori-runner-media /tmp/flori-runner-media

FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates \
      ffmpeg=7:5.1.9-0+deb12u1 \
      poppler-utils=22.12.0-2+deb12u3 \
      python3=3.11.2-1+b1 \
      python3-pip=23.0.1+dfsg-1 && \
    python3 -m pip install --break-system-packages --no-cache-dir PyMuPDF==1.27.2.3 && \
    python3 -m pip uninstall --break-system-packages --yes pip setuptools wheel && \
    apt-get purge -y --auto-remove python3-pip && \
    rm -rf /var/lib/apt/lists/* /root/.cache && \
    groupadd --system --gid 65532 flori && \
    useradd --system --uid 65532 --gid 65532 --home-dir /home/flori --create-home flori && \
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
