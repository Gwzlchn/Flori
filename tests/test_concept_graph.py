"""概念图谱:服务纯函数(api.services.kb.concept_graph) + REST 路由的共现推导/权重/孤立计数。

共现规则:两概念若其 occurrences 引用同一 job_id 即相连,权重=共享 job 数;手动 related 叠加为额外边。
"""

from __future__ import annotations

import pytest

from api.services import kb
from shared.models import Job


def _seed(db):
    # 三个 job 锚定共现:
    #  - j1: 通胀 + 利率 + 国债期货  → 三两两共现
    #  - j2: 通胀 + 利率            → 通胀-利率 再 +1(权重 2)
    #  - j3: 国债期货               → 仅再给国债期货一个出现(不新增配对)
    for jid in ("j1", "j2", "j3"):
        db.create_job(Job(id=jid, content_type="video", pipeline="video", domain="finance"))

    db.add_glossary_suggestion("finance", "通胀", "j1", "video")
    db.add_glossary_suggestion("finance", "通胀", "j2", "video")
    db.add_glossary_suggestion("finance", "利率", "j1", "video")
    db.add_glossary_suggestion("finance", "利率", "j2", "video")
    db.add_glossary_suggestion("finance", "国债期货", "j1", "video")
    db.add_glossary_suggestion("finance", "国债期货", "j3", "video")
    # 孤立概念:无任何 occurrence,应作为节点保留且 isolated。
    db.upsert_glossary_term("finance", "孤立词", definition="无人提及。", status="accepted")
    # 另一领域的概念不得渗入 finance 图。
    db.add_glossary_suggestion("deep-learning", "梯度", "jx", "paper")


def _edge(edges, a, b):
    """按无序对查一条边,返回其 weight(无则 None)。"""
    for e in edges:
        if {e["source"], e["target"]} == {a, b}:
            return e["weight"]
    return None


class TestConceptGraphService:
    def test_cooccurrence_edges_and_weights(self, db):
        _seed(db)
        g = kb.concept_graph(db, "finance")
        # 节点:通胀/利率/国债期货/孤立词 = 4,且不含其它领域的概念。
        terms = {n["term"] for n in g["nodes"]}
        assert terms == {"通胀", "利率", "国债期货", "孤立词"}
        assert "梯度" not in terms
        # 共现边:通胀-利率(共享 j1,j2)=2;通胀-国债期货(j1)=1;利率-国债期货(j1)=1。
        assert _edge(g["edges"], "通胀", "利率") == 2
        assert _edge(g["edges"], "通胀", "国债期货") == 1
        assert _edge(g["edges"], "利率", "国债期货") == 1
        assert _edge(g["edges"], "孤立词", "通胀") is None

    def test_node_fields_and_occurrence_count(self, db):
        _seed(db)
        g = kb.concept_graph(db, "finance")
        by = {n["term"]: n for n in g["nodes"]}
        assert by["国债期货"]["occurrence_count"] == 2   # j1 + j3
        assert by["通胀"]["occurrence_count"] == 2        # j1 + j2
        assert by["孤立词"]["occurrence_count"] == 0
        assert by["孤立词"]["definition"] == "无人提及。"  # 短定义取首句
        for n in g["nodes"]:
            assert set(n) == {"id", "term", "definition", "status",
                              "is_topic", "occurrence_count"}
            assert n["id"] == n["term"]

    def test_stats_and_isolated_count(self, db):
        _seed(db)
        g = kb.concept_graph(db, "finance")
        assert g["stats"]["node_count"] == 4
        assert g["stats"]["edge_count"] == 3   # 三两两 + 无 related
        assert g["stats"]["isolated_count"] == 1  # 仅 "孤立词"

    def test_manual_related_overlay(self, db):
        _seed(db)
        # 手动给 "孤立词" 加一条 related 指向 "通胀" → 出现一条权重 1 的边,孤立计数归零。
        db.upsert_glossary_term("finance", "孤立词", definition="无人提及。",
                                related=["通胀", "不存在的词"])
        g = kb.concept_graph(db, "finance")
        assert _edge(g["edges"], "孤立词", "通胀") == 1
        assert g["stats"]["isolated_count"] == 0
        assert g["stats"]["edge_count"] == 4
        # 指向不存在概念的 related 被忽略,不会凭空造节点。
        assert "不存在的词" not in {n["term"] for n in g["nodes"]}

    def test_related_does_not_downgrade_cooccurrence_weight(self, db):
        _seed(db)
        # related 叠加同一对已有共现边时取较大权重(不把 2 压成 1)。
        db.upsert_glossary_term("finance", "通胀", related=["利率"])
        g = kb.concept_graph(db, "finance")
        assert _edge(g["edges"], "通胀", "利率") == 2

    def test_empty_domain(self, db):
        g = kb.concept_graph(db, "nonexistent")
        assert g["nodes"] == [] and g["edges"] == []
        assert g["stats"] == {"node_count": 0, "edge_count": 0, "isolated_count": 0}


class TestConceptGraphRoute:
    @pytest.mark.asyncio
    async def test_route_returns_graph(self, client, app):
        _seed(app.state.db)
        r = await client.get("/api/domains/finance/concept-graph")
        assert r.status_code == 200
        body = r.json()
        assert body["stats"]["node_count"] == 4
        assert _edge(body["edges"], "通胀", "利率") == 2
        assert body["stats"]["isolated_count"] == 1

    @pytest.mark.asyncio
    async def test_route_empty_domain(self, client):
        r = await client.get("/api/domains/empty/concept-graph")
        assert r.status_code == 200
        assert r.json()["stats"]["node_count"] == 0

    @pytest.mark.asyncio
    async def test_route_rejects_traversal(self, client):
        assert (await client.get("/api/domains/..%2Fx/concept-graph")).status_code in (400, 404)
