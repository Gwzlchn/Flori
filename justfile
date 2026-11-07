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
