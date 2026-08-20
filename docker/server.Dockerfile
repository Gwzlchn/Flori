# syntax=docker/dockerfile:1.7

FROM rust:1.95-bookworm AS build
WORKDIR /src
COPY Cargo.toml Cargo.lock ./
COPY crates ./crates
COPY xtask ./xtask
RUN --mount=type=cache,target=/usr/local/cargo/registry \
    --mount=type=cache,target=/src/target \
    cargo build --locked --release -p flori-server && \
    cp target/release/flori-server /tmp/flori-server

FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && \
    rm -rf /var/lib/apt/lists/*
COPY --from=build /tmp/flori-server /usr/local/bin/flori-server
USER 65532:65532
ENTRYPOINT ["flori-server"]
