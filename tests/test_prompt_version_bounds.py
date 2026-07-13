"""Prompt 版本的 API schema 与 SQLite 整数边界固定回归."""

from __future__ import annotations

import pytest

from shared.db import (
    PROMPT_VERSION_EXCLUSIVE_MAX,
    PROMPT_VERSION_MAX,
    PROMPT_VERSION_MIN,
    PromptVersionExhaustedError,
)


def _insert_version(db, version: int, content: str = "boundary") -> None:
    with db._lock:
        db._conn.execute(
            """INSERT INTO prompt_override_versions
               (scope, domain, pipeline, step, version, content, note, created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            ("global", "", "video", "11_smart", version, content, "", "2026-01-01T00:00:00Z"),
        )
        db._conn.commit()


@pytest.mark.parametrize(
    "version",
    [0, -1, PROMPT_VERSION_MAX + 1, 10**1000, "1", 1.5, True],
)
def test_db_rejects_invalid_versions_before_sqlite_binding(db, version):
    assert db.get_prompt_override_version(
        "global", None, "video", "11_smart", version
    ) is None
    assert db.set_active_prompt_version(
        "global", None, "video", "11_smart", version
    ) is False


def test_db_accepts_both_integer_boundaries(db):
    db.set_prompt_override("global", None, "video", "11_smart", "first")
    assert db.get_prompt_override_version(
        "global", None, "video", "11_smart", PROMPT_VERSION_MIN
    )["content"] == "first"

    db.delete_prompt_override("global", None, "video", "11_smart")
    _insert_version(db, PROMPT_VERSION_MAX)
    assert db.get_prompt_override_version(
        "global", None, "video", "11_smart", PROMPT_VERSION_MAX
    )["version"] == PROMPT_VERSION_MAX
    assert db.set_active_prompt_version(
        "global", None, "video", "11_smart", PROMPT_VERSION_MAX
    ) is True


def test_db_refuses_to_increment_past_sqlite_integer_limit(db):
    _insert_version(db, PROMPT_VERSION_MAX)
    with pytest.raises(PromptVersionExhaustedError):
        db.set_prompt_override(
            "global", None, "video", "11_smart", "overflow", mode="new"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "version",
    ["0", "-1", str(PROMPT_VERSION_MAX + 1), "9" * 1000, "not-an-int", "1.5"],
)
async def test_get_version_rejects_invalid_path_values(client, version):
    response = await client.get(f"/api/prompts/video/11_smart/versions/{version}")
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_get_version_accepts_boundaries_and_preserves_404(client, db):
    await client.put(
        "/api/prompts/video/11_smart",
        json={"scope": "global", "content": "first"},
    )
    lower = await client.get(
        f"/api/prompts/video/11_smart/versions/{PROMPT_VERSION_MIN}"
    )
    assert lower.status_code == 200

    missing = await client.get("/api/prompts/video/11_smart/versions/2")
    assert missing.status_code == 404

    _insert_version(db, PROMPT_VERSION_MAX)
    upper = await client.get(
        f"/api/prompts/video/11_smart/versions/{PROMPT_VERSION_MAX}"
    )
    assert upper.status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "version",
    [0, -1, PROMPT_VERSION_MAX + 1, 10**1000, 1.5, "1", True],
)
async def test_activate_rejects_invalid_body_versions(client, version):
    response = await client.post(
        "/api/prompts/video/11_smart/activate",
        json={"scope": "global", "version": version},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_activate_accepts_boundaries_and_preserves_404(client, db):
    await client.put(
        "/api/prompts/video/11_smart",
        json={"scope": "global", "content": "first"},
    )
    lower = await client.post(
        "/api/prompts/video/11_smart/activate",
        json={"scope": "global", "version": PROMPT_VERSION_MIN},
    )
    assert lower.status_code == 200

    missing = await client.post(
        "/api/prompts/video/11_smart/activate",
        json={"scope": "global", "version": 2},
    )
    assert missing.status_code == 404

    _insert_version(db, PROMPT_VERSION_MAX)
    upper = await client.post(
        "/api/prompts/video/11_smart/activate",
        json={"scope": "global", "version": PROMPT_VERSION_MAX},
    )
    assert upper.status_code == 200


@pytest.mark.asyncio
async def test_new_version_at_integer_limit_returns_conflict(client, db):
    _insert_version(db, PROMPT_VERSION_MAX)
    response = await client.put(
        "/api/prompts/video/11_smart",
        json={"scope": "global", "content": "overflow", "mode": "new"},
    )
    assert response.status_code == 409
    assert response.json() == {
        "error": "conflict",
        "message": "prompt version limit reached",
    }


@pytest.mark.asyncio
async def test_openapi_exposes_prompt_version_integer_boundaries(client):
    spec = (await client.get("/openapi.json")).json()

    path_operation = spec["paths"][
        "/api/prompts/{pipeline}/{step}/versions/{version}"
    ]["get"]
    path_schema = next(
        param["schema"]
        for param in path_operation["parameters"]
        if param["name"] == "version" and param["in"] == "path"
    )
    assert path_schema["minimum"] == PROMPT_VERSION_MIN
    assert path_schema["exclusiveMaximum"] == PROMPT_VERSION_EXCLUSIVE_MAX

    body_schema = spec["components"]["schemas"]["PromptActivateRequest"][
        "properties"
    ]["version"]
    integer_schema = next(
        option for option in body_schema["anyOf"] if option.get("type") == "integer"
    )
    assert integer_schema["minimum"] == PROMPT_VERSION_MIN
    assert integer_schema["exclusiveMaximum"] == PROMPT_VERSION_EXCLUSIVE_MAX
