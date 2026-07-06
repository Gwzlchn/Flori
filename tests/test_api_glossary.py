"""api/routes/glossary.py 测试。"""

from __future__ import annotations

import pytest
import yaml


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
    async def test_update_definition(self, client):
        await client.post(
            "/api/glossary?domain=ml", json={"term": "A", "definition": "旧"}
        )
        resp = await client.put(
            "/api/glossary/ml/A",
            json={"term": "A", "definition": "新", "related": ["B"]},
        )
        assert resp.status_code == 200
        assert resp.json()["definition"] == "新"
        # related 归一为类型化边:字符串入参 → rel='related'。
        assert resp.json()["related"] == [{"term": "B", "rel": "related"}]
        # status 不动,仍 accepted。
        assert resp.json()["status"] == "accepted"

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
        db.add_glossary_suggestion("ml", "Transformer", "job-1", "video")
        db.add_glossary_suggestion("ml", "Transformer", "job-2", "paper")
        resp = await client.get("/api/glossary?domain=ml&status=suggested")
        assert resp.status_code == 200
        items = resp.json()
        assert len(items) == 1
        assert items[0]["term"] == "Transformer"
        assert items[0]["status"] == "suggested"
        # occurrences 记录类型化出现(job + content_type),用于前端显示出现数/来源多样性。
        assert {o["job_id"] for o in items[0]["occurrences"]} == {"job-1", "job-2"}
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
