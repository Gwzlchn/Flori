"""前端 selected OpenAPI 清单与快照生成器的不变量。"""

from __future__ import annotations

import json
from pathlib import Path

from scripts.generate_selected_openapi import (
    MANIFEST,
    SNAPSHOT,
    build_snapshot,
    render_snapshot,
)


def test_selected_manifest_is_unique_and_covers_declared_domains():
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    operations = manifest["operations"]
    keys = [(item["method"], item["path"]) for item in operations]
    assert len(keys) == len(set(keys))
    paths = {item["path"] for item in operations}
    required = {
        "/api/sources", "/api/jobs", "/api/jobs/{job_id}/review",
        "/api/status", "/api/workers", "/api/study/cards",
        "/api/evidence/{evidence_id}/resolve", "/api/glossary/{domain}/{term}",
        "/api/search", "/api/ask", "/api/ai-tasks/{task_id}/result",
        "/api/prompts/{pipeline}/{step}",
    }
    assert required <= paths


def test_selected_snapshot_generation_is_byte_deterministic():
    first = render_snapshot()
    second = render_snapshot()
    assert first == second
    parsed = json.loads(first)
    assert parsed == build_snapshot()
    assert set(parsed) == {"openapi", "info", "paths", "components"}
    assert "ErrorResponse" in parsed["components"]["schemas"]


def test_selected_snapshot_matches_checked_in_contract():
    assert SNAPSHOT.read_text(encoding="utf-8") == render_snapshot()


def test_selected_snapshot_has_no_unselected_manual_boundaries():
    snapshot = build_snapshot()
    paths = set(snapshot["paths"])
    assert "/api/ws/global" not in paths
    assert "/api/jobs/{job_id}/media" not in paths
    assert "/api/metrics" not in paths


def test_readiness_503_is_an_explicit_non_error_projection():
    snapshot = build_snapshot()
    responses = snapshot["paths"]["/api/health/ready"]["get"]["responses"]
    assert responses["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ReadinessResponse",
    }
    assert responses["503"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ReadinessResponse",
    }


def test_selected_schema_preserves_required_nullable_enum_union_and_datetime():
    schemas = build_snapshot()["components"]["schemas"]
    assert schemas["ErrorResponse"]["required"] == ["error", "message"]
    active = schemas["PromptDetailResponse"]["properties"]["active_version"]
    assert {item.get("type") for item in active["anyOf"]} == {"string", "null"}
    manifest_state = schemas["EvidenceProjectionResponse"]["properties"]["manifest_state"]
    assert manifest_state["enum"] == ["legacy", "invalid", "partial", "verified"]
    locator = schemas["CanonicalEvidenceProjection"]["properties"]["locator"]
    assert "discriminator" in locator["anyOf"][0]
    assert schemas["PromptVersionMetaResponse"]["properties"]["created_at"]["format"] == "date-time"


def test_manual_204_text_binary_and_range_responses_stay_outside_json_generation():
    from api.main import app

    spec = app.openapi()
    assert spec["paths"]["/api/jobs/{job_id}"]["delete"]["responses"]["204"].get("content") is None
    log_content = spec["paths"]["/api/jobs/{job_id}/steps/{step}/log"]["get"]["responses"]["200"]["content"]
    assert "text/plain" in log_content
    media_schema = spec["paths"]["/api/jobs/{job_id}/media"]["get"]["responses"]["200"]["content"]
    assert "application/json" not in media_schema
