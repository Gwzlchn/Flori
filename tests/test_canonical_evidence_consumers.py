"""Canonical evidence 在 Search、Ask、MCP 的统一消费契约。"""

from __future__ import annotations

import copy
import importlib
import json

import pytest

from api.mcp_server.server import build_server
from api.services import synthesis
from shared.ask_citations import normalized_body_sha256
from shared.storage import LocalStorage


VALID_ID = f"ce_{'1' * 64}"
STALE_ID = f"ce_{'2' * 64}"
EVIDENCE_IDS = [VALID_ID, STALE_ID]

VALID = {
    "evidence_id": VALID_ID,
    "status": "valid",
    "reason": None,
    "job_id": "j_shared",
    "note_type": "smart",
    "chunk_id": "j_shared:smart:0",
    "section": "共同章节",
    "evidence_fingerprint": "a" * 64,
    "source_fingerprint": "b" * 64,
    "locator": {
        "kind": "text",
        "exact": "共同证据",
        "prefix": "",
        "suffix": "正文",
        "dom_path": None,
    },
    "link": {
        "kind": "text",
        "href": "/api/jobs/j_shared/artifact?path=output%2Foriginal.md#:~:text=共同证据",
        "label": "跳到原文证据",
    },
    "validated_at": "2026-07-14T14:00:00Z",
}

STALE = {
    "evidence_id": STALE_ID,
    "status": "stale",
    "reason": "source_changed",
    "job_id": "j_shared",
    "note_type": "smart",
    "chunk_id": "j_shared:smart:0",
    "section": "共同章节",
    "evidence_fingerprint": "c" * 64,
    "source_fingerprint": "d" * 64,
    "locator": None,
    "link": None,
    "validated_at": "2026-07-14T14:00:00Z",
}


def _chunk() -> dict:
    body = "共同证据正文"
    return {
        "chunk_id": "j_shared:smart:0",
        "job_id": "j_shared",
        "note_type": "smart",
        "title": "共同来源",
        "snippet": "<mark>共同证据</mark>正文",
        "body": body,
        "content_type": "article",
        "domain": "ml",
        "collection_id": None,
        "section": "共同章节",
        "evidence": {
            "chunk_id": "j_shared:smart:0",
            "note_type": "smart",
            "section": "共同章节",
            "snippet": "共同证据正文",
            "chunk_index": 0,
            "char_start": 0,
            "char_end": len(body),
            "timestamp_sec": None,
            "page": None,
            "frame_path": None,
            "image_path": None,
            "artifact_sha256": "e" * 64,
            "body_sha256": normalized_body_sha256(body),
            "canonical_evidence_ids": list(EVIDENCE_IDS),
        },
    }


def _mcp_payload(result) -> list[dict]:
    structured = result[1] if isinstance(result, tuple) and len(result) == 2 else None
    if isinstance(structured, dict) and set(structured) == {"result"}:
        return structured["result"]
    if isinstance(structured, list):
        return structured
    blocks = result[0] if isinstance(result, tuple) else result
    return json.loads(blocks[0].text)


@pytest.mark.asyncio
async def test_search_ask_mcp_share_ordered_projection(
    client, db, test_config, monkeypatch,
):
    """三个消费者批量解析同一 ID 序列，且不覆盖 Ask 旧 evidence。"""
    from api.mcp_server import server as mcp_server
    from api.routes import ask as ask_route
    from api.routes import search as search_route
    evidence_service = importlib.import_module("api.services.evidence")

    resolver_calls: list[list[str]] = []

    async def fake_resolver(_db, _storage, evidence_ids):
        resolver_calls.append(list(evidence_ids))
        assert evidence_ids == EVIDENCE_IDS
        return [copy.deepcopy(VALID), copy.deepcopy(STALE)]

    monkeypatch.setattr(
        evidence_service, "resolve_canonical_evidence_batch", fake_resolver,
    )
    # 消费者可以直接导入 resolver；同时 patch 模块绑定，避免测试依赖导入风格。
    for module in (search_route, ask_route, mcp_server):
        monkeypatch.setattr(
            module, "resolve_canonical_evidence_batch", fake_resolver, raising=False,
        )

    def fake_search(*_args, **_kwargs):
        return 1, [copy.deepcopy(_chunk())]

    monkeypatch.setattr(db, "search_notes", fake_search)
    monkeypatch.setattr(db, "search_note_chunks", fake_search)
    monkeypatch.setattr(
        db,
        "canonical_evidence_ids_for_notes",
        lambda refs: {ref: list(EVIDENCE_IDS) for ref in refs},
    )

    passage = _chunk()
    monkeypatch.setattr(
        synthesis, "retrieve", lambda *_args, **_kwargs: [copy.deepcopy(passage)],
    )

    search_response = await client.get("/api/search", params={"q": "共同证据"})
    assert search_response.status_code == 200
    search_item = search_response.json()["items"][0]

    ask_response = await client.post(
        "/api/ask", json={"question": "共同证据是什么?", "domain": "ml"},
    )
    assert ask_response.status_code == 202
    ask_source = ask_response.json()["sources"][0]

    mcp = build_server(db, LocalStorage(test_config.jobs_dir))
    mcp_items = _mcp_payload(
        await mcp.call_tool("search", {"query": "共同证据", "domain": "ml"})
    )
    mcp_item = mcp_items[0]

    assert search_item["canonical_evidence"] == [VALID, STALE]
    assert ask_source["canonical_evidence"] == [VALID, STALE]
    assert mcp_item["canonical_evidence"] == [VALID, STALE]
    assert resolver_calls == [EVIDENCE_IDS, EVIDENCE_IDS, EVIDENCE_IDS]

    # 旧 Ask evidence 仍服务 citation/source manifest；canonical 投影不携带内部路径。
    assert ask_source["evidence"]["chunk_id"] == "j_shared:smart:0"
    for item in (search_item, ask_source, mcp_item):
        projection = item["canonical_evidence"]
        assert all("source_path" not in evidence for evidence in projection)
        assert projection[1]["status"] == "stale"
        assert projection[1]["locator"] is None
        assert projection[1]["link"] is None


@pytest.mark.asyncio
async def test_job_detail_evidence_resolves_current_note_snapshot(
    client, db, monkeypatch,
):
    """详情页按 note type 读取当前 ID，且由同一 resolver 生成安全投影。"""
    from api.routes import evidence as evidence_route

    monkeypatch.setattr(db, "get_job", lambda job_id: object() if job_id == "j_shared" else None)
    monkeypatch.setattr(
        db,
        "canonical_evidence_ids_for_job",
        lambda job_id, note_type=None: list(EVIDENCE_IDS)
        if (job_id, note_type) == ("j_shared", "smart") else [],
    )

    async def fake_resolver(_db, _storage, evidence_ids):
        assert evidence_ids == EVIDENCE_IDS
        return [copy.deepcopy(VALID), copy.deepcopy(STALE)]

    monkeypatch.setattr(
        evidence_route, "resolve_canonical_evidence_batch", fake_resolver,
    )
    response = await client.get(
        "/api/evidence/jobs/j_shared", params={"note_type": "smart"},
    )
    assert response.status_code == 200
    assert response.json() == {"total": 2, "items": [VALID, STALE]}

    missing = await client.get("/api/evidence/jobs/not-found")
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_legacy_chunk_returns_empty_projection(client, db, monkeypatch):
    """无 canonical ID 的存量 chunk 返回空数组，不调用 resolver 猜定位。"""
    from api.routes import search as search_route
    evidence_service = importlib.import_module("api.services.evidence")

    calls = 0

    async def should_not_resolve(_db, _storage, _evidence_ids):
        nonlocal calls
        calls += 1
        raise AssertionError("legacy chunk must not invoke resolver")

    monkeypatch.setattr(
        evidence_service, "resolve_canonical_evidence_batch", should_not_resolve,
    )
    monkeypatch.setattr(
        search_route, "resolve_canonical_evidence_batch", should_not_resolve,
        raising=False,
    )
    legacy = _chunk()
    legacy["evidence"]["canonical_evidence_ids"] = []
    fake_search = lambda *_args, **_kwargs: (1, [copy.deepcopy(legacy)])
    monkeypatch.setattr(db, "search_notes", fake_search)
    monkeypatch.setattr(db, "search_note_chunks", fake_search)

    response = await client.get("/api/search", params={"q": "共同证据"})
    assert response.status_code == 200
    assert response.json()["items"][0]["canonical_evidence"] == []
    assert calls == 0
