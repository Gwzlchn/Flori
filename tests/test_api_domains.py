"""领域 API(派生视图)测试:总览聚合 / 工作台 / 术语 / 主题 / 归类(PATCH job)。"""

from __future__ import annotations

import pytest

from shared.models import Collection, Job


def _seed(db):
    db.create_collection(Collection(id="col_bili_up_1", name="UP", domain="finance",
                                    source_type="bilibili_up", source_id="1"))
    db.create_job(Job(id="j1", content_type="video", pipeline="video", domain="finance",
                      collection_id="col_bili_up_1", style_tags=["宏观经济", "利率"]))
    db.create_job(Job(id="j2", content_type="video", pipeline="video", domain="finance",
                      style_tags=["宏观经济"]))   # 未分类
    db.create_job(Job(
        id="j3", content_type="document", document_kind="research_paper",
        pipeline="document", domain="deep-learning",
    ))
    db.add_glossary_suggestion("finance", "国债期货", "j1", "review")


class TestDomainCreate:
    @pytest.mark.asyncio
    async def test_create_then_appears_with_meta(self, client):
        r = await client.post("/api/domains", json={
            "domain": "crypto", "display_name": "加密货币", "icon": "coins",
            "color": "#f59e0b", "role": "链上研究员", "description": "去中心化金融",
        })
        assert r.status_code == 201
        body = r.json()
        assert body["domain"] == "crypto" and body["icon"] == "coins" and body["job_count"] == 0
        data = (await client.get("/api/domains")).json()["domains"]
        by = {d["domain"]: d for d in data}
        assert "crypto" in by                                   # 仅有 profile 的空知识库也出现
        assert by["crypto"]["color"] == "#f59e0b" and by["crypto"]["display_name"] == "加密货币"

    @pytest.mark.asyncio
    async def test_duplicate_409(self, client):
        await client.post("/api/domains", json={"domain": "crypto"})
        r = await client.post("/api/domains", json={"domain": "crypto"})
        assert r.status_code == 409

    @pytest.mark.asyncio
    async def test_general_rejected(self, client):
        assert (await client.post("/api/domains", json={"domain": "general"})).status_code == 400

    @pytest.mark.asyncio
    async def test_empty_kb_workspace_ok(self, client):
        await client.post("/api/domains", json={"domain": "crypto"})
        r = await client.get("/api/domains/crypto")
        assert r.status_code == 200 and r.json()["stats"]["job_count"] == 0


class TestConceptTimeline:
    @pytest.mark.asyncio
    async def test_timeline_buckets(self, client, app):
        _seed(app.state.db)
        r = await client.get("/api/domains/finance/concept-timeline?granularity=month")
        assert r.status_code == 200
        body = r.json()
        assert body["granularity"] == "month"
        c = next(c for c in body["concepts"] if c["term"] == "国债期货")
        assert c["total"] == 1 and sum(c["buckets"].values()) == 1

    @pytest.mark.asyncio
    async def test_timeline_bad_granularity_422(self, client):
        assert (await client.get("/api/domains/finance/concept-timeline?granularity=year")).status_code == 422


class TestDomains:
    @pytest.mark.asyncio
    async def test_overview(self, client, app):
        _seed(app.state.db)
        data = (await client.get("/api/domains")).json()["domains"]
        by = {d["domain"]: d for d in data}
        assert by["finance"]["job_count"] == 2
        assert by["finance"]["collection_count"] == 1
        assert by["finance"]["subscription_count"] == 1
        assert by["finance"]["concept_count"] == 1
        assert by["deep-learning"]["job_count"] == 1

    @pytest.mark.asyncio
    async def test_workspace(self, client, app):
        _seed(app.state.db)
        ws = (await client.get("/api/domains/finance")).json()
        assert ws["stats"]["job_count"] == 2
        assert len(ws["collections"]) == 1 and ws["collections"][0]["is_subscription"]
        assert len(ws["recent_jobs"]) == 2
        topics = {t["topic"]: t["count"] for t in ws["topics"]}
        assert topics["宏观经济"] == 2 and topics["利率"] == 1
        assert ws["top_concepts"][0]["term"] == "国债期货"

    @pytest.mark.asyncio
    async def test_workspace_per_collection_recent(self, client, app):
        """集合最近内容按集合各取,而非从全域最近 12 条里分组——
        老集合的内容即使被新内容挤出全域最近 12,其 collection.recent 仍应有(否则误显「暂无」)。"""
        db = app.state.db
        db.create_collection(Collection(id="col_old", name="老集合", domain="finance",
                                        source_type="youtube_channel", source_id="x"))
        db.create_job(Job(id="old1", content_type="video", pipeline="video",
                          domain="finance", collection_id="col_old"))
        for i in range(15):   # 15 条更新的未归类内容,把 old1 挤出全域最近 12
            db.create_job(Job(id=f"n{i}", content_type="video", pipeline="video", domain="finance"))
        ws = (await client.get("/api/domains/finance")).json()
        col = next(c for c in ws["collections"] if c["id"] == "col_old")
        # 注:job_count 是 collections 计数列,由 create_job_core 维护;raw db.create_job 不增,故此处不验它。
        assert "old1" in [j["job_id"] for j in col["recent"]]          # 老集合 recent 不受全域挤出影响
        assert len(ws["recent_jobs"]) == 12                            # 全域最近仍封顶 12
        assert "old1" not in [j["job_id"] for j in ws["recent_jobs"]]  # old1 确已被挤出全域最近,用例才踩到边界

    @pytest.mark.asyncio
    async def test_workspace_404(self, client):
        assert (await client.get("/api/domains/nope")).status_code == 404

    @pytest.mark.asyncio
    async def test_term_detail(self, client, app):
        _seed(app.state.db)
        assert (await client.get("/api/domains/finance/terms/国债期货")).status_code == 200
        assert (await client.get("/api/domains/finance/terms/不存在")).status_code == 404

    @pytest.mark.asyncio
    async def test_topic_page(self, client, app):
        _seed(app.state.db)
        r = (await client.get("/api/domains/finance/topics/宏观经济")).json()
        assert r["total"] == 2
        r2 = (await client.get("/api/domains/finance/topics/利率")).json()
        assert r2["total"] == 1

    @pytest.mark.asyncio
    async def test_topic_concepts(self, client, app):
        db = app.state.db
        # 两个出现的主题概念。
        db.add_glossary_suggestion("finance", "通胀", "j1", "video")
        db.add_glossary_suggestion("finance", "通胀", "j2", "video")
        db.set_glossary_topic("finance", "通胀", True)
        # 一个出现的主题概念。
        db.add_glossary_suggestion("finance", "汇率", "j1", "video")
        db.set_glossary_topic("finance", "汇率", True)
        # 非主题概念(不应返回)。
        db.add_glossary_suggestion("finance", "成交量", "j1", "video")

        data = (await client.get("/api/domains/finance/topic-concepts")).json()
        terms = [c["term"] for c in data]
        assert terms == ["通胀", "汇率"]  # 按 occurrence_count 降序
        assert all(c["is_topic"] is True for c in data)
        by = {c["term"]: c for c in data}
        assert by["通胀"]["occurrence_count"] == 2
        assert by["汇率"]["occurrence_count"] == 1
        assert "成交量" not in terms
        assert isinstance(by["通胀"]["related"], list)

    @pytest.mark.asyncio
    async def test_topic_concepts_empty(self, client, app):
        _seed(app.state.db)  # 无 is_topic=1 的词。
        data = (await client.get("/api/domains/finance/topic-concepts")).json()
        assert data == []

    @pytest.mark.asyncio
    async def test_jobs_domain_filter_internal(self, client, app):
        # /api/jobs 刻意不暴露 domain/uncategorized 查询(无前端消费方);
        # domain 过滤仅供领域工作台内部用,经 /api/domains/:d 验证(test_workspace)。
        _seed(app.state.db)
        assert (await client.get("/api/jobs")).json()["total"] == 3


# 注:知识库展示元数据(display_name/icon/color)的修改复用 PUT /api/profiles/{domain}
# (见 test_api_profiles 的 meta 合并用例),侧栏 updateMeta 即调它;此处不另测一份 domains meta 端点。


class TestDomainRename:
    """改英文 domain key:事务迁移 jobs/collections/glossary + notes_fts5 + profile 文件。"""

    @pytest.mark.asyncio
    async def test_rename_migrates_all(self, client, app):
        _seed(app.state.db)
        r = await client.post("/api/domains/finance/rename", json={"new_domain": "investing"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["new"] == "investing"
        assert body["moved"] == {
            "jobs": 2,
            "collections": 1,
            "glossary": 1,
            "study_cards": 0,
            "study_suggestion_batches": 0,
            "study_suggestions": 0,
            "study_suggestion_evidence": 0,
            "concept_definition_versions": 1,
        }
        names = [d["domain"] for d in (await client.get("/api/domains")).json()["domains"]]
        assert "investing" in names and "finance" not in names
        # 内容/集合/术语都迁到了新 key(经工作台聚合验证)
        ws = (await client.get("/api/domains/investing")).json()
        assert ws["stats"]["job_count"] == 2

    @pytest.mark.asyncio
    async def test_rename_conflict_409(self, client, app):
        _seed(app.state.db)  # deep-learning 已存在(j3)
        r = await client.post("/api/domains/finance/rename", json={"new_domain": "deep-learning"})
        assert r.status_code == 409

    @pytest.mark.asyncio
    async def test_rename_general_rejected(self, client, app):
        _seed(app.state.db)
        assert (await client.post("/api/domains/general/rename", json={"new_domain": "x"})).status_code == 400
        assert (await client.post("/api/domains/finance/rename", json={"new_domain": "general"})).status_code == 400

    @pytest.mark.asyncio
    async def test_rename_same_rejected(self, client, app):
        _seed(app.state.db)
        assert (await client.post("/api/domains/finance/rename", json={"new_domain": "finance"})).status_code == 400

    @pytest.mark.asyncio
    async def test_rename_overlong_new_400(self, client, app):
        _seed(app.state.db)
        r = await client.post("/api/domains/finance/rename", json={"new_domain": "x" * 300})
        assert r.status_code == 400

    @pytest.mark.asyncio
    async def test_rename_moves_profile_file(self, client, test_config):
        await client.post("/api/domains", json={"domain": "crypto", "display_name": "加密"})
        r = await client.post("/api/domains/crypto/rename", json={"new_domain": "web3"})
        assert r.status_code == 200
        pdir = test_config.prompts_dir / "profiles"
        assert not (pdir / "crypto.yaml").exists()
        assert (pdir / "web3.yaml").exists()
        import yaml as _yaml
        meta = _yaml.safe_load((pdir / "web3.yaml").read_text(encoding="utf-8"))
        assert meta["domain"] == "web3" and meta["display_name"] == "加密"
