"""跨源综合问答(synthesis Q&A)测试。

覆盖:
- derive_queries:关键词 + 术语表抽取(缓解整句字面短语无法命中 FTS)。
- retrieve:只返回 chunk 证据检索命中。
- POST /api/ask:注入假 gateway(返回固定 LLMResponse,绝不调真 LLM),验 sources + markdown。

所有 FTS 经 db.index_job_notes 灌入(与 tests/test_api_search 同法)。
"""

from __future__ import annotations

import hashlib
import json

import pytest

from shared.db import Database
from shared.models import LLMResponse
from api.services import synthesis


# 共享 seed:三篇 ml 笔记 + 一篇无关,外加术语表


def _seed(db: Database) -> None:
    db.index_job_notes(
        "j_bp", "smart", "反向传播详解",
        "反向传播算法通过链式法则计算梯度,是神经网络训练的核心。多数资料认为它高效且稳定。",
        content_type="video", domain="ml", collection_id="c_ai",
    )
    db.index_job_notes(
        "j_grad", "smart", "梯度下降综述",
        "梯度下降依赖反向传播得到的梯度更新参数。有人认为学习率难调是其主要缺点。",
        content_type="paper", domain="ml", collection_id="c_ai",
    )
    db.index_job_notes(
        "j_attn", "smart", "注意力机制",
        "自注意力机制让模型并行处理序列,同样依赖反向传播训练。",
        content_type="paper", domain="ml", collection_id="c_ai",
    )
    db.index_job_notes(
        "j_cook", "smart", "红烧肉做法",
        "先焯水再炒糖色,小火慢炖一小时即可。",
        content_type="article", domain="food", collection_id="",
    )
    # 术语表:其 term 出现在问句中时应被 derive_queries 采为高信噪检索词。
    db.upsert_glossary_term("ml", "反向传播", "误差反传算法")
    db.upsert_glossary_term("ml", "梯度下降", "一阶优化算法")


# derive_queries


class TestDeriveQueries:
    def test_extracts_keywords_drops_stopwords(self, db):
        _seed(db)
        qs = synthesis.derive_queries("反向传播是如何工作的?", db, domain="ml")
        # 停用词 "是/如何/的" 不应作为单独检索词;实义词应在。
        assert "的" not in qs and "如何" not in qs
        assert any("反向传播" in q or "传播" in q for q in qs)

    def test_includes_glossary_terms(self, db):
        _seed(db)
        qs = synthesis.derive_queries("反向传播和梯度下降有什么区别?", db, domain="ml")
        # 两个术语表词都出现在问句中 → 应被采为检索词(且因术语优先,排在前面)。
        assert "反向传播" in qs
        assert "梯度下降" in qs

    def test_glossary_not_in_question_excluded(self, db):
        _seed(db)
        qs = synthesis.derive_queries("注意力机制怎么并行?", db, domain="ml")
        assert "反向传播" not in qs  # 该术语未出现在问句

    def test_capped_at_max(self, db):
        _seed(db)
        long_q = "反向传播 梯度下降 注意力机制 卷积神经网络 循环神经网络 强化学习 生成对抗网络"
        qs = synthesis.derive_queries(long_q, db, domain="ml")
        assert len(qs) <= 6

    def test_ascii_tokens_and_dedup(self, db):
        _seed(db)
        qs = synthesis.derive_queries("transformer transformer model", db, domain="ml")
        assert qs.count("transformer") == 1  # 去重
        assert "transformer" in qs


# retrieve


class TestRetrieve:
    def test_union_and_dedupe_across_queries(self, db):
        _seed(db)
        # "反向传播" 命中三篇 ml;问句拆词后并集去重应覆盖多篇且每篇只一次。
        passages = synthesis.retrieve(db, "反向传播和梯度下降哪个更重要?", domain="ml", k=8)
        job_ids = [p["job_id"] for p in passages]
        assert len(job_ids) == len(set(job_ids))  # 去重
        assert "j_bp" in job_ids and "j_grad" in job_ids

    def test_bodies_attached(self, db):
        _seed(db)
        passages = synthesis.retrieve(db, "反向传播", domain="ml", k=8)
        bp = next(p for p in passages if p["job_id"] == "j_bp")
        assert "链式法则" in bp["body"]
        assert set(bp) == {
            "job_id", "note_type", "title", "domain", "content_type", "body", "evidence",
        }
        assert bp["evidence"]["chunk_id"] == "j_bp:smart:0"

    def test_domain_scope(self, db):
        _seed(db)
        # 限定 food 域:ml 笔记不应出现。
        passages = synthesis.retrieve(db, "反向传播", domain="food", k=8)
        assert all(p["domain"] == "food" for p in passages)
        assert "j_bp" not in {p["job_id"] for p in passages}

    def test_no_match_returns_empty(self, db):
        _seed(db)
        assert synthesis.retrieve(db, "量子计算机超导体", domain="ml", k=8) == []

    def test_no_chunks_returns_empty(self, db):
        _seed(db)
        db._conn.execute("DELETE FROM note_chunks")
        db._conn.execute("DELETE FROM note_chunks_fts5")
        db._conn.commit()
        assert synthesis.retrieve(db, "反向传播", domain="ml", k=8) == []

    def test_body_truncated(self, db):
        # trigram 至少 3 字才命中,故查询词与正文锚点都用 3 字 "反向传"。
        db.index_job_notes("j_big", "smart", "长文", "反向传" + "啊" * 10000, domain="ml")
        passages = synthesis.retrieve(db, "反向传播原理", domain="ml", k=8)
        big = next(p for p in passages if p["job_id"] == "j_big")
        assert len(big["body"]) <= 4000

    def test_rrf_uses_every_derived_query_before_ranking(self, monkeypatch):
        class FakeDb:
            def __init__(self):
                self.calls = []

            def search_note_chunks(self, query, **kwargs):
                self.calls.append((query, kwargs))
                evidence = {
                    "artifact_sha256": "a" * 64,
                    "body_sha256": "b" * 64,
                }
                rows = {
                    "first": [
                        {"chunk_id": "noise:smart:0", "job_id": "noise", "note_type": "smart",
                         "title": "noise", "domain": "ml", "content_type": "paper",
                         "body": "noise", "snippet": "noise", "section": "", "evidence": evidence},
                        {"chunk_id": "target:smart:0", "job_id": "target", "note_type": "smart",
                         "title": "target", "domain": "ml", "content_type": "paper",
                         "body": "target", "snippet": "target", "section": "", "evidence": evidence},
                    ],
                    "second": [
                        {"chunk_id": "target:smart:0", "job_id": "target", "note_type": "smart",
                         "title": "target", "domain": "ml", "content_type": "paper",
                         "body": "target", "snippet": "target", "section": "", "evidence": evidence},
                    ],
                }[query]
                return len(rows), rows

        fake = FakeDb()
        monkeypatch.setattr(
            synthesis, "derive_queries", lambda *_args, **_kwargs: ["first", "second"]
        )
        passages = synthesis.retrieve(fake, "question", domain="ml", k=2)

        assert [passage["job_id"] for passage in passages] == ["target", "noise"]
        assert [query for query, _kwargs in fake.calls] == ["first", "second"]
        assert all(
            kwargs == {"domain": "ml", "limit": 8}
            for _query, kwargs in fake.calls
        )

    def test_one_passage_per_job_and_hashes_are_preserved(self, db):
        body = "唯一检索锦标词与证据内容。"
        db.index_job_notes("j_same", "smart", "智能", body, domain="ml")
        db.index_job_notes("j_same", "mechanical", "机械", body, domain="ml")
        db.index_job_notes("j_other", "smart", "其他", body, domain="ml")

        passages = synthesis.retrieve(db, "唯一检索锦标", domain="ml", k=8)
        assert [p["job_id"] for p in passages].count("j_same") == 1
        assert {p["job_id"] for p in passages} == {"j_same", "j_other"}
        assert next(p for p in passages if p["job_id"] == "j_same")["note_type"] == "smart"
        for passage in passages:
            assert passage["note_type"] in {"smart", "mechanical"}
            assert len(passage["evidence"]["artifact_sha256"]) == 64
            assert len(passage["evidence"]["body_sha256"]) == 64

        first_digest = hashlib.sha256(
            json.dumps(passages, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()
        second_digest = hashlib.sha256(
            json.dumps(
                synthesis.retrieve(
                    db, "唯一检索锦标", domain="ml", k=8
                ),
                ensure_ascii=False,
                sort_keys=True,
            ).encode()
        ).hexdigest()
        assert first_digest == second_digest

    def test_candidate_overfetch_preserves_job_diversity(self, monkeypatch):
        evidence = {"artifact_sha256": "a" * 64, "body_sha256": "b" * 64}

        class FakeDb:
            def search_note_chunks(self, _query, **kwargs):
                assert kwargs["limit"] == 8
                rows = []
                for index in range(4):
                    rows.append({
                        "chunk_id": f"same:smart:{index}", "job_id": "same",
                        "note_type": "smart", "title": "same", "domain": "ml",
                        "content_type": "video", "body": f"same-{index}",
                        "snippet": "same", "section": "", "evidence": evidence,
                    })
                rows.append({
                    "chunk_id": "other:smart:0", "job_id": "other",
                    "note_type": "smart", "title": "other", "domain": "ml",
                    "content_type": "paper", "body": "other", "snippet": "other",
                    "section": "", "evidence": evidence,
                })
                return len(rows), rows

        monkeypatch.setattr(synthesis, "derive_queries", lambda *_args, **_kwargs: ["q"])
        passages = synthesis.retrieve(FakeDb(), "question", domain="ml", k=2)
        assert [passage["job_id"] for passage in passages] == ["same", "other"]

    def test_rejects_chunk_without_artifact_binding(self, db):
        db.index_job_notes(
            "j_unbound", "smart", "未绑定", "拒绝未绑定证据内容。", domain="ml"
        )
        db._conn.execute(
            "UPDATE note_chunks_fts5 SET evidence_json='{}' WHERE job_id='j_unbound'"
        )
        db._conn.execute(
            "DELETE FROM notes_fts5 WHERE job_id='j_unbound'"
        )
        db._conn.commit()

        assert synthesis.retrieve(db, "拒绝未绑定证据", domain="ml", k=8) == []


# build_prompt


class TestBuildPrompt:
    def test_prompt_has_citations_and_consensus_instruction(self):
        passages = [
            {
                "job_id": "a", "title": "A", "domain": "ml", "content_type": "video",
                "body": "正文A", "evidence": {"chunk_id": "a:smart:0", "section": ""},
            },
            {
                "job_id": "b", "title": "B", "domain": "ml", "content_type": "paper",
                "body": "正文B", "evidence": {"chunk_id": "b:smart:0", "section": ""},
            },
        ]
        system, user = synthesis.build_prompt("问题X", passages)
        assert "[来源N]" in system and "共识 / 分歧" in system
        assert "[来源1]" in user and "[来源2]" in user
        assert "正文A" in user and "正文B" in user
        assert "问题X" in user


# POST /api/ask(假 gateway,绝不调真 LLM)


class _FakeGateway:
    """假 gateway:记录被调的 step + request,返回固定 LLMResponse。"""

    def __init__(self, response: LLMResponse):
        self._response = response
        self.calls: list = []

    async def call(self, step_name, request):
        self.calls.append((step_name, request))
        return self._response


@pytest.fixture
def db(test_config):
    d = Database(test_config.db_path)
    d.init_schema()
    _seed(d)
    yield d
    d.close()


@pytest.fixture
def fake_response():
    return LLMResponse(
        content="反向传播用于计算梯度 [来源1]。\n\n## 共识 / 分歧\n各来源一致认为它是核心。",
        model="claude-opus-4-8[1m]",
        provider="claude-cli",
        input_tokens=120,
        output_tokens=60,
        cost_usd=0.0,
        duration_sec=1.2,
    )


@pytest.fixture
def ask_app(db, test_config):
    """异步 /ask、ai-tasks 端点:用真 fakeredis(让 enqueue_ai_task/set_ai_result 真生效,便于验队列/结果)。"""
    from api.main import create_app
    from tests.conftest import make_fakeredis

    return create_app(db=db, redis=make_fakeredis(), config=test_config)


@pytest.fixture
async def ask_client(ask_app):
    from httpx import ASGITransport, AsyncClient

    transport = ASGITransport(app=ask_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


class TestAskEndpoint:
    @pytest.mark.asyncio
    async def test_ask_enqueues_task_and_returns_sources(self, ask_client, ask_app):
        resp = await ask_client.post(
            "/api/ask", json={"question": "反向传播和梯度下降有什么区别?", "domain": "ml"}
        )
        assert resp.status_code == 202
        data = resp.json()
        assert data["question"].startswith("反向传播")
        assert data["task_id"] and data["task_id"].startswith("at_")
        assert data["answer_markdown"] is None  # 答案走 result 端点,不在这里
        assert data["retrieved_count"] >= 1
        assert "j_bp" in {s["job_id"] for s in data["sources"]}
        for s in data["sources"]:
            assert {"job_id", "title", "domain", "content_type", "evidence"} <= set(s)
            assert s["evidence"]["chunk_id"]
        # 真投了一个 synthesis AI task 进 queue:ai(claude 在 worker 跑,API 没调)。
        queued = await ask_app.state.redis.list_queue("ai")
        assert len(queued) == 1
        assert queued[0]["kind"] == "ai" and queued[0]["task_id"] == data["task_id"]
        assert queued[0]["step"] == "synthesis" and queued[0]["require_tags"] == ["claude-cli"]
        raw = await ask_app.state.redis.r.zrange("queue:ai", 0, 0)
        task_payload = json.loads(raw[0])
        manifest = task_payload["audit_context"]["ask_source_manifest"]
        assert manifest["task_id"] == data["task_id"]
        assert len(manifest["sources"]) == data["retrieved_count"]
        assert all(source["source_fingerprint"] for source in manifest["sources"])

    @pytest.mark.asyncio
    async def test_ask_no_match_skips_task(self, ask_client, ask_app):
        resp = await ask_client.post(
            "/api/ask", json={"question": "量子计算机超导体", "domain": "ml"}
        )
        assert resp.status_code == 202
        data = resp.json()
        assert data["retrieved_count"] == 0 and data["sources"] == []
        assert data["task_id"] is None and "没有找到" in data["answer_markdown"]
        assert await ask_app.state.redis.list_queue("ai") == []  # 无命中不投 task

    @pytest.mark.asyncio
    async def test_ask_empty_question_422(self, ask_client):
        resp = await ask_client.post("/api/ask", json={"question": ""})
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_ask_oversized_question_422(self, ask_client):
        resp = await ask_client.post("/api/ask", json={"question": "问" * 4001})
        assert resp.status_code == 422


class TestAITasksEndpoints:
    """/api/ai-tasks/{task_id}/result 的 pending/done/error 三态 + /log 白盒审计。"""

    @staticmethod
    async def _bind_original_manifest(redis, task_id: str, source_manifest: dict) -> None:
        payload = {
            "kind": "ai", "task_id": task_id,
            "audit_context": {"ask_source_manifest": source_manifest},
        }
        await redis.r.hset(
            f"ai:claim:{task_id}",
            "raw_json",
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
        )

    @pytest.mark.asyncio
    async def test_result_pending(self, ask_client):
        data = (await ask_client.get("/api/ai-tasks/at_none/result")).json()
        assert data["status"] == "pending" and data["task_id"] == "at_none"

    @pytest.mark.asyncio
    async def test_result_done(self, ask_client, ask_app):
        from shared.ask_citations import build_source_manifest

        content = "反向传播通过链式法则计算梯度 [来源1]。"
        source_manifest = build_source_manifest("at_done", "反向传播", [{
            "job_id": "j_bp", "title": "反向传播", "domain": "ml",
            "content_type": "video", "note_type": "smart",
            "artifact_sha256": "a" * 64,
            "body": "反向传播通过链式法则计算梯度。",
            "evidence": {"chunk_id": "j_bp:smart:0", "section": "原理"},
        }])
        await self._bind_original_manifest(
            ask_app.state.redis, "at_done", source_manifest,
        )
        await ask_app.state.redis.set_ai_result(
            "at_done", {"content": content, "provider": "claude-cli", "model": "claude-opus-4-8[1m]",
                        "cost_usd": 0.1, "citation_validation": {"status": "spoofed"},
                        "source_manifest": source_manifest})
        data = (await ask_client.get("/api/ai-tasks/at_done/result")).json()
        assert data["status"] == "done" and data["content"] == content
        assert data["answer_markdown"] == content and data["markdown"] == content
        assert data["provider"] == "claude-cli"
        assert data["citation_validation"]["status"] == "valid"
        assert data["source_manifest"] == source_manifest

    @pytest.mark.asyncio
    async def test_result_rejects_cross_task_manifest(self, ask_client, ask_app):
        from shared.ask_citations import build_source_manifest

        original_manifest = build_source_manifest("at_done", "反向传播", [{
            "job_id": "j_bp", "title": "反向传播", "domain": "ml",
            "content_type": "video", "note_type": "smart",
            "artifact_sha256": "a" * 64,
            "body": "反向传播通过链式法则计算梯度。",
            "evidence": {"chunk_id": "j_bp:smart:0", "section": "原理"},
        }])
        await self._bind_original_manifest(
            ask_app.state.redis, "at_done", original_manifest,
        )
        source_manifest = build_source_manifest("at_other", "反向传播", [{
            "job_id": "j_bp", "title": "反向传播", "domain": "ml",
            "content_type": "video", "note_type": "smart",
            "artifact_sha256": "a" * 64,
            "body": "反向传播通过链式法则计算梯度。",
            "evidence": {"chunk_id": "j_bp:smart:0", "section": "原理"},
        }])
        await ask_app.state.redis.set_ai_result("at_done", {
            "content": "反向传播通过链式法则计算梯度 [来源1]。",
            "provider": "claude-cli", "model": "test", "cost_usd": 0,
            "citation_validation": {"status": "valid"},
            "source_manifest": source_manifest,
        })
        data = (await ask_client.get("/api/ai-tasks/at_done/result")).json()
        assert data["citation_validation"]["status"] == "invalid"
        assert "manifest_task_mismatch" in data["citation_validation"]["errors"]

    @pytest.mark.asyncio
    async def test_result_rejects_valid_replacement_manifest(self, ask_client, ask_app):
        from shared.ask_citations import build_source_manifest

        original_manifest = build_source_manifest("at_done", "反向传播", [{
            "job_id": "j_bp", "title": "反向传播", "domain": "ml",
            "content_type": "video", "note_type": "smart",
            "artifact_sha256": "a" * 64,
            "body": "反向传播通过链式法则计算梯度。",
            "evidence": {"chunk_id": "j_bp:smart:0", "section": "原理"},
        }])
        replacement_manifest = build_source_manifest("at_done", "替换来源", [{
            "job_id": "j_fake", "title": "伪造来源", "domain": "ml",
            "content_type": "article", "note_type": "smart",
            "artifact_sha256": "b" * 64,
            "body": "伪造来源声称模型会自动获得意识。",
            "evidence": {"chunk_id": "j_fake:smart:0", "section": "伪造"},
        }])
        await self._bind_original_manifest(
            ask_app.state.redis, "at_done", original_manifest,
        )
        await ask_app.state.redis.set_ai_result("at_done", {
            "content": "伪造来源声称模型会自动获得意识 [来源1]。",
            "provider": "remote", "model": "untrusted", "cost_usd": 0,
            "citation_validation": {"status": "valid"},
            "source_manifest": replacement_manifest,
        })
        data = (await ask_client.get("/api/ai-tasks/at_done/result")).json()
        assert data["citation_validation"]["status"] == "invalid"
        assert "source_manifest_mismatch" in data["citation_validation"]["errors"]

    @pytest.mark.asyncio
    async def test_result_rejects_missing_manifest(self, ask_client, ask_app):
        from shared.ask_citations import build_source_manifest

        original_manifest = build_source_manifest("at_done", "反向传播", [{
            "job_id": "j_bp", "title": "反向传播", "domain": "ml",
            "content_type": "video", "note_type": "smart",
            "artifact_sha256": "a" * 64,
            "body": "反向传播通过链式法则计算梯度。",
            "evidence": {"chunk_id": "j_bp:smart:0", "section": "原理"},
        }])
        await self._bind_original_manifest(
            ask_app.state.redis, "at_done", original_manifest,
        )
        await ask_app.state.redis.set_ai_result("at_done", {
            "content": "反向传播通过链式法则计算梯度 [来源1]。",
            "provider": "remote", "model": "untrusted", "cost_usd": 0,
            "citation_validation": {"status": "valid"},
        })
        data = (await ask_client.get("/api/ai-tasks/at_done/result")).json()
        assert data["citation_validation"]["status"] == "invalid"
        assert "source_manifest_missing" in data["citation_validation"]["errors"]

    @pytest.mark.asyncio
    async def test_result_error(self, ask_client, ask_app):
        await ask_app.state.redis.set_ai_result("at_err", {"error": "provider down"})
        data = (await ask_client.get("/api/ai-tasks/at_err/result")).json()
        assert data["status"] == "error" and "provider down" in data["error"]
        assert data["citation_validation"]["status"] == "invalid"
        assert "source_manifest_unbound" in data["citation_validation"]["errors"]

    @pytest.mark.asyncio
    async def test_synthesis_result_with_both_manifests_missing_is_invalid(
        self, ask_client, ask_app,
    ):
        payload = {"kind": "ai", "task_id": "at_unbound", "step": "synthesis"}
        await ask_app.state.redis.r.set(
            "ai:anchor:at_unbound", json.dumps(payload, sort_keys=True),
        )
        await ask_app.state.redis.set_ai_result("at_unbound", {
            "content": "自报可信 [来源1]。",
            "citation_validation": {"status": "valid"},
        })
        data = (await ask_client.get("/api/ai-tasks/at_unbound/result")).json()
        assert data["status"] == "done"
        assert data["citation_validation"]["status"] == "invalid"
        assert "source_manifest_unbound" in data["citation_validation"]["errors"]

    @pytest.mark.asyncio
    async def test_result_error_does_not_trust_replacement_manifest(self, ask_client, ask_app):
        from shared.ask_citations import build_source_manifest

        original_manifest = build_source_manifest("at_err", "反向传播", [{
            "job_id": "j_bp", "title": "反向传播", "domain": "ml",
            "content_type": "video", "note_type": "smart",
            "artifact_sha256": "a" * 64,
            "body": "反向传播通过链式法则计算梯度。",
            "evidence": {"chunk_id": "j_bp:smart:0", "section": "原理"},
        }])
        replacement_manifest = build_source_manifest("at_err", "伪造", [{
            "job_id": "j_fake", "title": "伪造", "domain": "ml",
            "content_type": "article", "note_type": "smart",
            "artifact_sha256": "b" * 64,
            "body": "伪造来源。",
            "evidence": {"chunk_id": "j_fake:smart:0", "section": "伪造"},
        }])
        await self._bind_original_manifest(
            ask_app.state.redis, "at_err", original_manifest,
        )
        await ask_app.state.redis.set_ai_result("at_err", {
            "error": "provider down", "source_manifest": replacement_manifest,
            "citation_validation": {"status": "valid"},
        })
        data = (await ask_client.get("/api/ai-tasks/at_err/result")).json()
        assert data["status"] == "error"
        assert data["citation_validation"]["status"] == "invalid"
        assert "source_manifest_mismatch" in data["citation_validation"]["errors"]

    @pytest.mark.asyncio
    async def test_log_endpoint(self, ask_client, db):
        db.record_ai_task_log({
            "task_id": "at_log", "exec_id": "w:1", "step_name": "synthesis", "domain": "ml",
            "provider": "claude-cli", "model": "claude-opus-4-8[1m]", "ok": True,
            "record": {"output": "hi", "routing": {"attempts": [{"tier": "primary"}]}},
            "created_at": "2026-06-27T00:00:00+00:00",
        })
        data = (await ask_client.get("/api/ai-tasks/at_log/log")).json()
        assert data["count"] == 1
        call = data["calls"][0]
        assert call["step"] == "synthesis" and call["provider"] == "claude-cli" and call["ok"] is True
        assert call["record"]["output"] == "hi"
        empty = (await ask_client.get("/api/ai-tasks/at_missing/log")).json()
        assert empty["count"] == 0 and empty["calls"] == []
