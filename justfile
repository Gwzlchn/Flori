# Single entry for local work and CI. CI runs the same ci-* recipes.

set shell := ["bash", "-euo", "pipefail", "-c"]

# Python with the pinned step dependencies (flori-working/venv locally, setup-python in CI).
py := env("FLORI_PY", "python3")

default:
    @just --list

# Everything CI checks.
check: ci-rust ci-web ci-py

ci-rust:
    cargo fmt --check
    cargo clippy --all-targets --locked -- -D warnings
    cargo test --locked

ci-web:
    cd frontend && npx openapi-typescript ../docs/openapi.json -o src/openapi.d.ts
    git diff --exit-code -- frontend/src/openapi.d.ts
    cd frontend && npx vue-tsc --noEmit && npx eslint . && npx vite build

ci-py:
    {{py}} -m unittest discover -s tests/steps -t tests/steps
    {{py}} -m py_compile steps/*.py

# Regenerate docs/openapi.json from src/api.rs and the frontend types from it.
openapi:
    FLORI_UPDATE_OPENAPI=1 cargo test --quiet --locked openapi_is_current
    cd frontend && npx openapi-typescript ../docs/openapi.json -o src/openapi.d.ts

# Images. Docker Hub is unreachable through the NAS proxy, so base images must be local and
# downloads inside the build go through the host proxy.
proxy := env("FLORI_BUILD_PROXY", "http://127.0.0.1:11081")
docker_build := "docker build --pull=false --network host --build-arg http_proxy=" + proxy + " --build-arg https_proxy=" + proxy + " --build-arg HTTP_PROXY=" + proxy + " --build-arg HTTPS_PROXY=" + proxy + " -f docker/flori.Dockerfile"

# Tools image without the application; `step` and `dev` mount host builds into it.
tools:
    {{docker_build}} --target tools -t flori-tools:local .

# Full image: host-built release binary and frontend on top of the tools stage.
image tag="local":
    cargo build --release --locked
    cd frontend && npx vite build
    {{docker_build}} --target app -t flori:{{tag}} .

dev_data := env("FLORI_DEV_DATA", "../flori-working/dev/data")

# Run one built-in step with real tools: just step video-frames <job_dir> <out_dir>
step name job_dir out_dir:
    cargo build
    mkdir -p {{out_dir}}
    docker run --rm --network host --user "$(id -u):$(id -g)" \
      -e FLORI_STEPS_DIR=/app/steps -e FLORI_WHISPER_MODEL=/data/models/whisper-base \
      -e HTTPS_PROXY={{proxy}} -e HTTP_PROXY={{proxy}} \
      -v "$PWD/target/debug/flori:/usr/local/bin/flori:ro" -v "$PWD/steps:/app/steps:ro" \
      -v "$(realpath {{dev_data}}):/data" \
      -v "$(realpath {{job_dir}}):$(realpath {{job_dir}})" -v "$(realpath {{out_dir}}):$(realpath {{out_dir}})" \
      flori-tools:local flori step {{name}} "$(realpath {{job_dir}})" "$(realpath {{out_dir}})"

# Development instance on 8781 with the fake AI CLI. Settings come from dev.env (gitignored).
dev:
    cargo build
    cd frontend && npx vite build
    mkdir -p {{dev_data}}/vault
    docker run --rm --name flori-dev --network host --user "$(id -u):$(id -g)" --init \
      --env-file dev.env \
      -e FLORI_DATA_DIR=/data -e FLORI_LISTEN=0.0.0.0:8781 -e HOME=/tmp \
      -e FLORI_STATIC_DIR=/app/web -e FLORI_STEPS_DIR=/app/steps \
      -v "$PWD/target/debug/flori:/usr/local/bin/flori:ro" -v "$PWD/frontend/dist:/app/web:ro" \
      -v "$PWD/steps:/app/steps:ro" -v "$PWD/tests/fake/claude:/usr/local/bin/claude:ro" \
      -v "$(realpath {{dev_data}}):/data" \
      flori-tools:local flori serve

# Frontend with hot reload; /api goes to the dev instance.
dev-web:
    cd frontend && npx vite --host 0.0.0.0

compose := "docker compose --env-file .local/deploy.env --env-file .local/secrets.env -f docker/compose.yml"

# Start or update the NAS stack. Pre-creates mount points so docker never creates them as root.
up tag="local":
    #!/usr/bin/env bash
    set -euo pipefail
    set -a; . .local/deploy.env; set +a
    install -d -m 0700 "$FLORI_DATA_DIR" "$FLORI_DATA_DIR/home" "$FLORI_DATA_DIR/vault" "$FLORI_DATA_DIR/models" \
      .local/flori-dav/data .local/flori-dav/config
    test -e "$FLORI_DATA_DIR/home/.claude.json" || install -m 0600 docker/claude.json "$FLORI_DATA_DIR/home/.claude.json"
    FLORI_TAG={{tag}} {{compose}} up -d

# Post-deploy checks that cost nothing: health, authenticated page, WebDAV refuses anonymous access.
smoke:
    #!/usr/bin/env bash
    set -euo pipefail
    set -a; . .local/deploy.env; . .local/secrets.env; set +a
    base="${FLORI_PUBLIC_URL%/}"
    curl -fsS --noproxy '*' "$base/api/health"; echo
    test "$(curl -s --noproxy '*' -o /dev/null -w '%{http_code}' "$base/")" = 401
    test "$(curl -s --noproxy '*' -o /dev/null -w '%{http_code}' -u "$FLORI_WEB_USER:$FLORI_WEB_PASSWORD" "$base/")" = 200
    test "$(curl -s --noproxy '*' -o /dev/null -w '%{http_code}' --connect-to "$DAV_DOMAIN:8790:127.0.0.1:8790" -X PROPFIND "https://$DAV_DOMAIN:8790/")" = 401
    echo smoke ok

# Cut a release: version in Cargo.toml, tag, image, database copy, then deploy with the new tag.
release version:
    #!/usr/bin/env bash
    set -euo pipefail
    test -z "$(git status --porcelain)" || { echo "working tree not clean" >&2; exit 1; }
    set -a; . .local/deploy.env; set +a
    old=$(cargo pkgid | sed 's/.*[#@]//')
    sed -i "0,/^version = \".*\"/s//version = \"{{version}}\"/" Cargo.toml
    cargo update --workspace --quiet
    just check
    git commit -am "chore(release): {{version}}"
    git tag "v{{version}}"
    just image {{version}}
    # Stop first so SQLite checkpoints its WAL and the copy is consistent.
    {{compose}} stop flori || true
    if [ -f "$FLORI_DATA_DIR/flori.sqlite" ]; then
      cp -a "$FLORI_DATA_DIR/flori.sqlite" "$FLORI_DATA_DIR/flori.sqlite.bak-$old"
    fi
    just up {{version}}
    just smoke
