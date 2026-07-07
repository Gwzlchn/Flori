"""概念图谱:服务纯函数(api.services.kb.concept_graph) + REST 路由。

边模型:related 真边(kind=rel)+ 共现边(kind='cooccur',权重=共享 job 数,
仅保留 ≥min_cooccur,默认 2——单篇全连噪声剪掉);同一对真边优先,不重复出共现边。
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


def _kind(edges, a, b):
    for e in edges:
        if {e["source"], e["target"]} == {a, b}:
            return e["kind"]
    return None


class TestConceptGraphService:
    def test_cooccurrence_noise_cut_default(self, db):
        _seed(db)
        g = kb.concept_graph(db, "finance")
        terms = {n["term"] for n in g["nodes"]}
        assert terms == {"通胀", "利率", "国债期货", "孤立词"}
        assert "梯度" not in terms
        # 默认 min_cooccur=2:通胀-利率(共享 j1,j2)保留;单 job 共现对(j1 全连)剪掉。
        assert _edge(g["edges"], "通胀", "利率") == 2
        assert _kind(g["edges"], "通胀", "利率") == "cooccur"
        assert _edge(g["edges"], "通胀", "国债期货") is None
        assert _edge(g["edges"], "利率", "国债期货") is None

    def test_min_cooccur_1_keeps_single_shared_job(self, db):
        _seed(db)
        g = kb.concept_graph(db, "finance", min_cooccur=1)
        assert _edge(g["edges"], "通胀", "国债期货") == 1
        assert _edge(g["edges"], "利率", "国债期货") == 1
        assert g["stats"]["edge_count"] == 3

    def test_node_fields_and_occurrence_count(self, db):
        _seed(db)
        g = kb.concept_graph(db, "finance")
        by = {n["term"]: n for n in g["nodes"]}
        assert by["国债期货"]["occurrence_count"] == 2   # j1 + j3
        assert by["通胀"]["occurrence_count"] == 2        # j1 + j2
        assert by["孤立词"]["occurrence_count"] == 0
        assert by["孤立词"]["definition"] == "无人提及。"  # 短定义取首句
        for n in g["nodes"]:
            assert set(n) == {"id", "term", "zh_name", "definition", "status",
                              "is_topic", "occurrence_count"}
            assert n["id"] == n["term"]

    def test_stats_and_isolated_count(self, db):
        _seed(db)
        g = kb.concept_graph(db, "finance")
        assert g["stats"]["node_count"] == 4
        assert g["stats"]["edge_count"] == 1        # 降噪后仅 通胀-利率
        assert g["stats"]["typed_edge_count"] == 0
        assert g["stats"]["isolated_count"] == 2    # 孤立词 + 国债期货(弱边被剪成孤立)

    def test_typed_related_edge(self, db):
        _seed(db)
        # 真边不受共现降噪影响:孤立词 --related→ 通胀 出边(weight 1,无共现)。
        db.upsert_glossary_term("finance", "孤立词", definition="无人提及。",
                                related=["通胀", "不存在的词"])
        g = kb.concept_graph(db, "finance")
        assert _edge(g["edges"], "孤立词", "通胀") == 1
        assert _kind(g["edges"], "孤立词", "通胀") == "related"
        assert g["stats"]["typed_edge_count"] == 1
        # 指向不存在概念的 related 被忽略,不会凭空造节点。
        assert "不存在的词" not in {n["term"] for n in g["nodes"]}

    def test_typed_edge_with_rel_and_direction(self, db):
        _seed(db)
        db.add_glossary_relations("finance", "利率",
                                  [{"term": "通胀", "rel": "prerequisite"}])
        g = kb.concept_graph(db, "finance")
        e = next(e for e in g["edges"] if {e["source"], e["target"]} == {"利率", "通胀"})
        # 真边替换同对共现边:kind=rel、方向保留(source=利率)、weight 借共现数(2)。
        assert e["kind"] == "prerequisite"
        assert e["source"] == "利率" and e["target"] == "通胀"
        assert e["weight"] == 2
        assert g["stats"]["edge_count"] == 1   # 不再重复出该对的 cooccur 边

    def test_rejected_excluded(self, db):
        _seed(db)
        db.upsert_glossary_term("finance", "国债期货", status="rejected")
        g = kb.concept_graph(db, "finance")
        assert "国债期货" not in {n["term"] for n in g["nodes"]}

    def test_empty_domain(self, db):
        g = kb.concept_graph(db, "nonexistent")
        assert g["nodes"] == [] and g["edges"] == []
        assert g["stats"] == {"node_count": 0, "edge_count": 0,
                              "typed_edge_count": 0, "isolated_count": 0}


class TestConceptGraphRoute:
    @pytest.mark.asyncio
    async def test_route_returns_graph(self, client, app):
        _seed(app.state.db)
        r = await client.get("/api/domains/finance/concept-graph")
        assert r.status_code == 200
        body = r.json()
        assert body["stats"]["node_count"] == 4
        assert _edge(body["edges"], "通胀", "利率") == 2
        assert body["stats"]["isolated_count"] == 2   # 默认降噪剪掉弱边

    @pytest.mark.asyncio
    async def test_route_min_cooccur_param(self, client, app):
        _seed(app.state.db)
        r = await client.get("/api/domains/finance/concept-graph?min_cooccur=1")
        assert _edge(r.json()["edges"], "通胀", "国债期货") == 1
        assert (await client.get(
            "/api/domains/finance/concept-graph?min_cooccur=0"
        )).status_code == 422

    @pytest.mark.asyncio
    async def test_route_empty_domain(self, client):
        r = await client.get("/api/domains/empty/concept-graph")
        assert r.status_code == 200
        assert r.json()["stats"]["node_count"] == 0

    @pytest.mark.asyncio
    async def test_route_rejects_traversal(self, client):
        assert (await client.get("/api/domains/..%2Fx/concept-graph")).status_code in (400, 404)
