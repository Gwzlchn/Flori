"""从显式 operation 清单生成确定性的前端 OpenAPI 快照。"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from api.main import app


MANIFEST = ROOT / "frontend/openapi/selected-paths.json"
SNAPSHOT = ROOT / "frontend/openapi/openapi.json"
REF_RE = re.compile(r"^#/components/(?P<section>[^/]+)/(?P<name>.+)$")


def _refs(value: Any) -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    if isinstance(value, dict):
        ref = value.get("$ref")
        if isinstance(ref, str) and (match := REF_RE.fullmatch(ref)):
            found.add((match.group("section"), match.group("name")))
        for child in value.values():
            found.update(_refs(child))
    elif isinstance(value, list):
        for child in value:
            found.update(_refs(child))
    return found


def build_snapshot() -> dict[str, Any]:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    source = app.openapi()
    paths: dict[str, Any] = {}
    for selected in manifest["operations"]:
        path = selected["path"]
        method = selected["method"].lower()
        try:
            operation = source["paths"][path][method]
        except KeyError as exc:
            raise RuntimeError(f"selected operation missing: {method.upper()} {path}") from exc
        if operation.get("operationId") != selected["operation_id"]:
            raise RuntimeError(f"selected operation id drift: {method.upper()} {path}")
        success = [
            (code, response)
            for code, response in operation.get("responses", {}).items()
            if code.startswith("2")
        ]
        if not success or not any(
            code == "204"
            or bool(response.get("content", {}).get("application/json", {}).get("schema"))
            for code, response in success
        ):
            raise RuntimeError(f"selected operation lacks exact JSON 2xx schema: {method.upper()} {path}")
        non_error_statuses = set(selected.get("non_error_statuses", []))
        unknown_non_errors = non_error_statuses - set(operation.get("responses", {}))
        if unknown_non_errors:
            raise RuntimeError(
                f"selected operation non-error status missing: {method.upper()} {path} "
                f"{sorted(unknown_non_errors)}"
            )
        for code, response in operation.get("responses", {}).items():
            if code.startswith("2") or code in non_error_statuses:
                continue
            schema = response.get("content", {}).get("application/json", {}).get("schema")
            if schema != {"$ref": "#/components/schemas/ErrorResponse"}:
                raise RuntimeError(
                    f"selected operation error schema drift: {method.upper()} {path} {code}"
                )
        paths.setdefault(path, {})[method] = operation

    components: dict[str, dict[str, Any]] = {}
    pending = _refs(paths)
    visited: set[tuple[str, str]] = set()
    while pending:
        section, name = pending.pop()
        if (section, name) in visited:
            continue
        try:
            value = source["components"][section][name]
        except KeyError as exc:
            raise RuntimeError(f"unresolved OpenAPI reference: {section}/{name}") from exc
        visited.add((section, name))
        components.setdefault(section, {})[name] = value
        pending.update(_refs(value) - visited)

    return {
        "openapi": source["openapi"],
        "info": source["info"],
        "paths": paths,
        "components": components,
    }


def render_snapshot() -> str:
    return json.dumps(build_snapshot(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    rendered = render_snapshot()
    if args.check:
        current = SNAPSHOT.read_text(encoding="utf-8") if SNAPSHOT.exists() else ""
        if current != rendered:
            raise SystemExit("selected OpenAPI snapshot drifted; run scripts/generate-frontend-wire.sh")
        return 0
    SNAPSHOT.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
