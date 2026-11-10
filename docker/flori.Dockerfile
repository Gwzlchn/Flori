# The flori image. `just image` builds the binary and the frontend on the host (Debian 12, the
# same glibc as bookworm) and passes the proxy; base images must already be local because
# Docker Hub is unreachable through the NAS proxy.

# Every external tool the steps run, without the application. `just step` and `just dev` mount
# a host-built binary into this stage, so real tools behave the same in development.
FROM debian:bookworm-slim AS tools
ARG YT_DLP_VERSION=2026.10.21
ARG FASTER_WHISPER_VERSION=1.2.1
# whisper.py converts Chinese output to simplified with OpenCC t2s.
ARG OPENCC_VERSION=1.4.2
ARG DENO_VERSION=2.9.8
ARG DENO_SHA256=e890975bc2549807bbcc0b584ffafda72cc7c93349dd99c80483dd784f6c4609
# claude stays on the host CLI version; upgrade both on purpose with `just image`.
ARG CLAUDE_VERSION=2.1.319
ARG CLAUDE_SHA256=dabafec82ca1319fa43d8be36a10fc61b1d11821cb98ad5df04ad95e199b8eed
# codex shares ~/.codex with the host CLI, so it tracks the host version too.
ARG CODEX_VERSION=0.167.0
ARG CODEX_SHA256=9d64836106d5b374e90ee4454c7ebcde7ca7106558bd7e41a34b9f26ebda251a

RUN rm -f /etc/apt/apt.conf.d/docker-clean && \
    apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      ca-certificates curl ffmpeg python3 python3-venv && \
    rm -rf /var/lib/apt/lists/*

COPY steps/requirements.txt /tmp/requirements.txt
RUN python3 -m venv /opt/venv && \
    /opt/venv/bin/pip install --no-cache-dir --disable-pip-version-check \
      -r /tmp/requirements.txt \
      "faster-whisper==${FASTER_WHISPER_VERSION}" \
      "opencc==${OPENCC_VERSION}" \
      "yt-dlp[default]==${YT_DLP_VERSION}" && \
    rm /tmp/requirements.txt

RUN curl -fsSL --retry 5 --retry-all-errors -o /usr/local/bin/claude \
      "https://downloads.claude.ai/claude-code-releases/${CLAUDE_VERSION}/linux-x64/claude" && \
    echo "${CLAUDE_SHA256}  /usr/local/bin/claude" | sha256sum -c - && \
    chmod 755 /usr/local/bin/claude

RUN curl -fsSL --retry 5 --retry-all-errors -o /tmp/codex.tar.gz \
      "https://github.com/openai/codex/releases/download/rust-v${CODEX_VERSION}/codex-x86_64-unknown-linux-musl.tar.gz" && \
    echo "${CODEX_SHA256}  /tmp/codex.tar.gz" | sha256sum -c - && \
    mkdir /tmp/codex && tar -xzf /tmp/codex.tar.gz -C /tmp/codex && \
    install -m 755 "$(find /tmp/codex -type f -name 'codex*' -print -quit)" /usr/local/bin/codex && \
    rm -rf /tmp/codex /tmp/codex.tar.gz

# yt-dlp needs a JavaScript runtime for YouTube challenges.
RUN curl -fsSL --retry 5 --retry-all-errors -o /tmp/deno.zip \
      "https://github.com/denoland/deno/releases/download/v${DENO_VERSION}/deno-x86_64-unknown-linux-gnu.zip" && \
    echo "${DENO_SHA256}  /tmp/deno.zip" | sha256sum -c - && \
    python3 -m zipfile -e /tmp/deno.zip /usr/local/bin && \
    chmod 755 /usr/local/bin/deno && rm /tmp/deno.zip

ENV PATH=/opt/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    LANG=C.UTF-8 \
    DISABLE_AUTOUPDATER=1 \
    DISABLE_UPDATES=1

RUN ffmpeg -version | head -1 | grep -F 'ffmpeg version 5.1' && \
    test "$(yt-dlp --version)" = "${YT_DLP_VERSION}" && \
    deno --version | head -1 | grep -F "deno ${DENO_VERSION}" && \
    claude --version | grep -F "${CLAUDE_VERSION}" && \
    test "$(codex --version)" = "codex-cli ${CODEX_VERSION}" && \
    python3 -I -c "import importlib.metadata as m, bs4, lxml, faster_whisper, opencc; \
      assert faster_whisper.__version__ == '${FASTER_WHISPER_VERSION}'; \
      assert m.version('opencc') == '${OPENCC_VERSION}'; \
      assert opencc.OpenCC('t2s').convert('漢語') == '汉语'"

FROM tools AS app
COPY target/release/flori /usr/local/bin/flori
COPY frontend/dist /app/web
COPY steps /app/steps
COPY docker/claude.json /app/claude.json
ENV FLORI_STATIC_DIR=/app/web \
    FLORI_STEPS_DIR=/app/steps
EXPOSE 8780
ENTRYPOINT ["flori"]
CMD ["serve"]
