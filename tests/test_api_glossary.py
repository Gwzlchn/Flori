"""api/routes/glossary.py 测试。"""

from __future__ import annotations

import json

import pytest
import yaml

from api.mcp_server.server import build_server
from api.services import concepts as concept_service
from shared.errors import AIProviderError
from shared.storage import LocalStorage


def _read_profile_terms(prompts_dir, domain):
    path = prompts_dir / "profiles" / f"{domain}.yaml"
    if not path.exists():
        return None
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data.get("terminology", [])


class TestManualCRUD:
    @pytest.mark.asyncio
    async def test_create_term_accepted(self, client):
        resp = await client.post(
            "/api/glossary?domain=ml",
            json={"term": "梯度下降", "definition": "一种优化算法"},
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["term"] == "梯度下降"
        assert body["status"] == "accepted"
        assert body["occurrences"] == [] and body["is_topic"] is False
        assert body["definition"] == "一种优化算法"

    @pytest.mark.asyncio
    async def test_create_syncs_into_profile(self, client, test_config):
        await client.post(
            "/api/glossary?domain=ml",
            json={"term": "梯度下降", "definition": "优化算法"},
        )
        terms = _read_profile_terms(test_config.prompts_dir, "ml")
        assert "梯度下降: 优化算法" in terms

    @pytest.mark.asyncio
    async def test_create_existing_term_is_conflict_and_cannot_bypass_lock(
        self, client,
    ):
        created = await client.post(
            "/api/glossary?domain=ml",
            json={"term": "A", "definition": "原定义"},
        )
        before = (await client.get("/api/glossary/ml/A")).json()
        locked = await client.post(
            "/api/glossary/ml/A/lock",
            json={
                "expected_current_version_id": before[
                    "current_definition_version_id"
                ],
                "expected_lock_revision": before["lock_revision"],
            },
        )
        overwritten = await client.post(
            "/api/glossary?domain=ml",
            json={"term": "A", "definition": "越权覆盖"},
        )

        assert created.status_code == 201
        assert locked.status_code == 200
        assert overwritten.status_code == 409
        after = (await client.get("/api/glossary/ml/A")).json()
        assert after["definition"] == "原定义"
        assert after["current_definition_version_id"] == before[
            "current_definition_version_id"
        ]
        assert after["definition_locked"] is True

    @pytest.mark.asyncio
    async def test_create_empty_term_rejected(self, client):
        resp = await client.post("/api/glossary?domain=ml", json={"term": "  "})
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_list_terms(self, client):
        await client.post("/api/glossary?domain=ml", json={"term": "A"})
        await client.post("/api/glossary?domain=ml", json={"term": "B"})
        resp = await client.get("/api/glossary?domain=ml")
        assert resp.status_code == 200
        assert {t["term"] for t in resp.json()} == {"A", "B"}

    @pytest.mark.asyncio
    async def test_get_term_detail(self, client):
        await client.post(
            "/api/glossary?domain=ml", json={"term": "A", "definition": "d"}
        )
        resp = await client.get("/api/glossary/ml/A")
        assert resp.status_code == 200
        assert resp.json()["definition"] == "d"

    @pytest.mark.asyncio
    async def test_get_missing_term_404(self, client):
        resp = await client.get("/api/glossary/ml/nope")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_rest_mcp_detail_parity_caps_occurrences_and_hides_stale_link(
        self, client, db, test_config, monkeypatch,
    ):
        db.upsert_glossary_term("ml", "A", "definition")
        for index in range(105):
            db.add_glossary_suggestion("ml", "A", f"job-{index}", "article")

        async def attestation(*_args):
            return {
                "domain": "ml",
                "term": "A",
                "level": "none",
                "evidence_count": 0,
                "job_count": 0,
                "source_fingerprint_count": 0,
                "content_type_count": 0,
                "source_set_fingerprint": "0" * 64,
                "included": [],
                "excluded": [{
                    "evidence_id": "ce_" + "a" * 64,
                    "job_id": "job-stale",
                    "content_type": "article",
                    "source_fingerprint": "source-stale",
                    "reason": "source_changed",
                    "locator": None,
                    "link": None,
                }],
            }

        monkeypatch.setattr(concept_service, "project_concept_attestation", attestation)
        rest = (await client.get("/api/glossary/ml/A")).json()
        assert rest["occurrence_total"] == 105
        assert rest["occurrence_limit"] == len(rest["occurrences"]) == 100
        stale = rest["attestation"]["excluded"][0]
        assert stale["locator"] is None and stale["link"] is None

        mcp = build_server(db, LocalStorage(test_config.jobs_dir))
        result = await mcp.call_tool("get_term", {"domain": "ml", "term": "A"})
        blocks = result[0] if isinstance(result, tuple) else result
        mcp_detail = json.loads(blocks[0].text)
        assert mcp_detail == rest

    @pytest.mark.asyncio
    async def test_update_definition(self, client):
        await client.post(
            "/api/glossary?domain=ml", json={"term": "A", "definition": "旧"}
        )
        before = (await client.get("/api/glossary/ml/A")).json()
        resp = await client.put(
            "/api/glossary/ml/A",
            json={
                "term": "A",
                "definition": "新",
                "related": ["B"],
                "expected_current_version_id": before["current_definition_version_id"],
                "expected_lock_revision": before["lock_revision"],
            },
        )
        assert resp.status_code == 200
        assert resp.json()["definition"] == "新"
        # related 归一为类型化边:字符串入参 → rel='related'。
        assert resp.json()["related"] == [{"term": "B", "rel": "related"}]
        # status 不动,仍 accepted。
        assert resp.json()["status"] == "accepted"

    @pytest.mark.asyncio
    async def test_definition_update_requires_cas_and_respects_lock(self, client):
        await client.post(
            "/api/glossary?domain=ml", json={"term": "A", "definition": "旧"}
        )
        before = (await client.get("/api/glossary/ml/A")).json()
        missing = await client.put(
            "/api/glossary/ml/A", json={"term": "A", "definition": "新"},
        )
        assert missing.status_code == 409

        locked = await client.post(
            "/api/glossary/ml/A/lock",
            json={
                "expected_current_version_id": before["current_definition_version_id"],
                "expected_lock_revision": before["lock_revision"],
            },
        )
        assert locked.status_code == 200
        assert locked.json()["locked"] is True
        blocked = await client.put(
            "/api/glossary/ml/A",
            json={
                "term": "A",
                "definition": "新",
                "expected_current_version_id": before["current_definition_version_id"],
                "expected_lock_revision": locked.json()["lock_revision"],
            },
        )
        assert blocked.status_code == 409

        stale_unlock = await client.post(
            "/api/glossary/ml/A/unlock",
            json={
                "expected_current_version_id": before["current_definition_version_id"],
                "expected_lock_revision": before["lock_revision"],
            },
        )
        assert stale_unlock.status_code == 409
        unlocked = await client.post(
            "/api/glossary/ml/A/unlock",
            json={
                "expected_current_version_id": before["current_definition_version_id"],
                "expected_lock_revision": locked.json()["lock_revision"],
            },
        )
        assert unlocked.status_code == 200
        assert unlocked.json()["locked"] is False

    @pytest.mark.asyncio
    async def test_related_only_update_does_not_append_definition(self, client):
        await client.post(
            "/api/glossary?domain=ml", json={"term": "A", "definition": "稳定"}
        )
        before = (await client.get("/api/glossary/ml/A")).json()
        response = await client.put(
            "/api/glossary/ml/A", json={"term": "A", "related": ["B"]},
        )
        assert response.status_code == 200
        after = (await client.get("/api/glossary/ml/A")).json()
        assert after["current_definition_version_id"] == before[
            "current_definition_version_id"
        ]
        assert after["definition_history_total"] == before["definition_history_total"]

    @pytest.mark.asyncio
    async def test_resynthesize_no_quorum_and_provider_failure_are_fail_closed(
        self, client, monkeypatch,
    ):
        await client.post(
            "/api/glossary?domain=ml", json={"term": "A", "definition": "稳定"}
        )
        before = (await client.get("/api/glossary/ml/A")).json()
        cas = {
            "expected_current_version_id": before["current_definition_version_id"],
            "expected_lock_revision": before["lock_revision"],
        }
        no_quorum = await client.post(
            "/api/glossary/ml/A/resynthesize", json=cas,
        )
        assert no_quorum.status_code == 200
        assert no_quorum.json()["created"] is False
        assert no_quorum.json()["reason"] == "no_quorum"

        async def provider_failure(*_args, **_kwargs):
            raise AIProviderError("provider unavailable")

        monkeypatch.setattr(
            concept_service, "maybe_resynthesize_concept", provider_failure,
        )
        failed = await client.post(
            "/api/glossary/ml/A/resynthesize", json=cas,
        )
        assert failed.status_code == 502
        after = (await client.get("/api/glossary/ml/A")).json()
        assert after["current_definition_version_id"] == before[
            "current_definition_version_id"
        ]
        assert after["definition_history_total"] == before["definition_history_total"]

    @pytest.mark.asyncio
    async def test_update_missing_term_404(self, client):
        resp = await client.put(
            "/api/glossary/ml/nope", json={"term": "nope", "definition": "x"}
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_delete_term(self, client):
        await client.post("/api/glossary?domain=ml", json={"term": "A"})
        resp = await client.delete("/api/glossary/ml/A")
        assert resp.status_code == 204
        assert (await client.get("/api/glossary/ml/A")).status_code == 404


class TestSuggestionFlow:
    @pytest.mark.asyncio
    async def test_suggestion_shows_in_suggested_list(self, client, db):
        # 单 job 出现只留候选;第二个不同 job 触发自动晋升,另测。
        db.add_glossary_suggestion("ml", "Transformer", "job-1", "video")
        resp = await client.get("/api/glossary?domain=ml&status=suggested")
        assert resp.status_code == 200
        items = resp.json()
        assert len(items) == 1
        assert items[0]["term"] == "Transformer"
        assert items[0]["status"] == "suggested"
        # occurrences 记录类型化出现(job + content_type),用于前端显示出现数/来源多样性。
        assert {o["job_id"] for o in items[0]["occurrences"]} == {"job-1"}

    @pytest.mark.asyncio
    async def test_second_job_auto_promotes(self, client, db):
        db.add_glossary_suggestion("ml", "Transformer", "job-1", "video")
        db.add_glossary_suggestion("ml", "Transformer", "job-2", "paper")
        items = (await client.get("/api/glossary?domain=ml&status=accepted")).json()
        assert len(items) == 1 and items[0]["term"] == "Transformer"
        assert {o["content_type"] for o in items[0]["occurrences"]} == {"video", "paper"}

    @pytest.mark.asyncio
    async def test_accept_sets_status_and_writes_profile(
        self, client, db, test_config
    ):
        db.add_glossary_suggestion("ml", "注意力机制", "job-1", "review")
        resp = await client.post("/api/glossary/ml/注意力机制/accept")
        assert resp.status_code == 200
        assert resp.json()["status"] == "accepted"
        # 采纳后该词进入 Profile.terminology,AI 步骤可用。
        terms = _read_profile_terms(test_config.prompts_dir, "ml")
        assert "注意力机制" in terms

    @pytest.mark.asyncio
    async def test_accept_with_definition_writes_pair(
        self, client, db, test_config
    ):
        db.add_glossary_suggestion("ml", "注意力", "job-1", "review")
        db.upsert_glossary_term(
            "ml", "注意力", definition="加权聚合", status="suggested"
        )
        await client.post("/api/glossary/ml/注意力/accept")
        terms = _read_profile_terms(test_config.prompts_dir, "ml")
        assert "注意力: 加权聚合" in terms

    @pytest.mark.asyncio
    async def test_accept_missing_term_404(self, client):
        resp = await client.post("/api/glossary/ml/nope/accept")
        assert resp.status_code == 404


class TestTopicToggle:
    @pytest.mark.asyncio
    async def test_set_topic_true_reflected(self, client):
        await client.post("/api/glossary?domain=ml", json={"term": "梯度下降"})
        resp = await client.post(
            "/api/glossary/ml/梯度下降/topic", json={"is_topic": True}
        )
        assert resp.status_code == 200
        assert resp.json()["is_topic"] is True
        # GET 反映 is_topic=true。
        got = await client.get("/api/glossary/ml/梯度下降")
        assert got.json()["is_topic"] is True

    @pytest.mark.asyncio
    async def test_set_topic_false_clears(self, client):
        await client.post("/api/glossary?domain=ml", json={"term": "A"})
        await client.post("/api/glossary/ml/A/topic", json={"is_topic": True})
        resp = await client.post(
            "/api/glossary/ml/A/topic", json={"is_topic": False}
        )
        assert resp.status_code == 200
        assert resp.json()["is_topic"] is False

    @pytest.mark.asyncio
    async def test_set_topic_missing_term_404(self, client):
        resp = await client.post(
            "/api/glossary/ml/nope/topic", json={"is_topic": True}
        )
        assert resp.status_code == 404


class TestFilters:
    @pytest.mark.asyncio
    async def test_filter_by_domain(self, client, db):
        db.upsert_glossary_term("ml", "A")
        db.upsert_glossary_term("dl", "C")
        resp = await client.get("/api/glossary?domain=ml")
        assert {t["term"] for t in resp.json()} == {"A"}

    @pytest.mark.asyncio
    async def test_filter_by_status(self, client, db):
        db.upsert_glossary_term("ml", "A")  # accepted
        db.add_glossary_suggestion("ml", "B", "j1")  # suggested
        accepted = await client.get("/api/glossary?status=accepted")
        suggested = await client.get("/api/glossary?status=suggested")
        assert {t["term"] for t in accepted.json()} == {"A"}
        assert {t["term"] for t in suggested.json()} == {"B"}

    @pytest.mark.asyncio
    async def test_list_sorted_by_term(self, client, db):
        db.upsert_glossary_term("ml", "z")
        db.upsert_glossary_term("ml", "a")
        resp = await client.get("/api/glossary?domain=ml")
        assert [t["term"] for t in resp.json()] == ["a", "z"]


class TestEntityP1:
    @pytest.mark.asyncio
    async def test_response_carries_zh_name_and_aliases(self, client, db):
        db.add_glossary_suggestion("ml", "Kelly criterion", "j1", zh_name="凯利准则")
        db.add_glossary_suggestion("ml", "kelly criterion", "j2")   # 变体归并
        resp = await client.get("/api/glossary?domain=ml")
        body = resp.json()
        assert len(body) == 1
        assert body[0]["zh_name"] == "凯利准则"
        assert "kelly criterion" in body[0]["aliases"]

    @pytest.mark.asyncio
    async def test_q_search_by_zh_name(self, client, db):
        db.add_glossary_suggestion("ml", "Kelly criterion", "j1", zh_name="凯利准则")
        db.add_glossary_suggestion("ml", "Sharpe ratio", "j2", zh_name="夏普比率")
        resp = await client.get("/api/glossary?domain=ml&q=凯利")
        assert [t["term"] for t in resp.json()] == ["Kelly criterion"]

    @pytest.mark.asyncio
    async def test_merge_endpoint(self, client, db):
        db.add_glossary_suggestion("ml", "Attention", "j1")
        db.add_glossary_suggestion("ml", "AttnMechanism", "j2", definition="更长定义在此")
        resp = await client.post(
            "/api/glossary/ml/AttnMechanism/merge", json={"target": "Attention"}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["term"] == "Attention"
        assert "AttnMechanism" in body["aliases"]
        assert len(body["occurrences"]) == 2
        assert (await client.get("/api/glossary/ml/AttnMechanism")).status_code == 404

    @pytest.mark.asyncio
    async def test_merge_missing_404_self_400(self, client, db):
        db.add_glossary_suggestion("ml", "OnlyOne", "j1")
        r1 = await client.post("/api/glossary/ml/OnlyOne/merge", json={"target": "nope"})
        assert r1.status_code == 404
        r2 = await client.post("/api/glossary/ml/OnlyOne/merge", json={"target": "OnlyOne"})
        assert r2.status_code == 400

    @pytest.mark.asyncio
    async def test_term_detail_occurrence_titles(self, client, db):
        from shared.models import Job
        db.create_job(Job(id="jt1", content_type="article", pipeline="article_v2",
                          title="一篇文章"))
        db.add_glossary_suggestion("ml", "Momentum", "jt1", "article")
        resp = await client.get("/api/glossary/ml/Momentum")
        occ = resp.json()["occurrences"][0]
        assert occ["title"] == "一篇文章"


class TestConceptContract:
    @pytest.mark.asyncio
    async def test_openapi_exposes_exact_bounded_detail_and_cas(self, client):
        spec = (await client.get("/openapi.json")).json()
        schemas = spec["components"]["schemas"]
        for name in (
            "ConceptTermDetailResponse",
            "ConceptDefinitionVersionResponse",
            "ConceptEvidenceResponse",
            "ConceptAttestationResponse",
            "ConceptCasRequest",
            "ConceptLockResponse",
            "ConceptResynthesizeResponse",
        ):
            assert schemas[name]["additionalProperties"] is False

        detail = schemas["ConceptTermDetailResponse"]["properties"]
        assert detail["occurrences"]["maxItems"] == 100
        assert detail["definition_history"]["maxItems"] == 100
        assert {"occurrence_total", "occurrence_limit"} <= set(detail)
        assert {"definition_history_total", "definition_history_limit"} <= set(detail)

        lock = spec["paths"]["/api/glossary/{domain}/{term}/lock"]["post"]
        request_ref = lock["requestBody"]["content"]["application/json"]["schema"]["$ref"]
        response_ref = lock["responses"]["200"]["content"]["application/json"][
            "schema"
        ]["$ref"]
        assert request_ref.endswith("/ConceptCasRequest")
        assert response_ref.endswith("/ConceptLockResponse")

        rejected = await client.post(
            "/api/glossary/ml/A/lock",
            json={
                "expected_current_version_id": "cdv",
                "expected_lock_revision": 0,
                "unexpected": True,
            },
        )
        assert rejected.status_code == 422


class TestDomainValidation:
    @pytest.mark.asyncio
    async def test_create_traversal_domain_rejected(self, client):
        # domain 是 query 参数,"../etc" 直达 _validate_seg 守卫(无路由折叠)→ 严格 400。
        resp = await client.post(
            "/api/glossary?domain=../etc", json={"term": "A"}
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_create_backslash_domain_rejected(self, client):
        """反斜杠也被挡:统一走 deps.validate_path_segment。"""
        resp = await client.post(
            "/api/glossary", params={"domain": "a\\b"}, json={"term": "A"}
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_create_empty_domain_rejected(self, client):
        """空 domain 被拒,不写出空文件名的 profile。"""
        resp = await client.post(
            "/api/glossary", params={"domain": ""}, json={"term": "A"}
        )
        assert resp.status_code in (400, 422)
