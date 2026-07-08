"""跨源综合问答(synthesis Q&A)测试。

覆盖:
- derive_queries:关键词 + 术语表抽取(缓解整句字面短语无法命中 FTS)。
- retrieve:只返回 chunk 证据检索命中。
- POST /api/ask:注入假 gateway(返回固定 LLMResponse,绝不调真 LLM),验 sources + markdown。

所有 FTS 经 db.index_job_notes 灌入(与 tests/test_api_search 同法)。
"""

from __future__ import annotations

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
        assert set(bp) == {"job_id", "title", "domain", "content_type", "body", "evidence"}
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


class TestAITasksEndpoints:
    """/api/ai-tasks/{task_id}/result 的 pending/done/error 三态 + /log 白盒审计。"""

    @pytest.mark.asyncio
    async def test_result_pending(self, ask_client):
        data = (await ask_client.get("/api/ai-tasks/at_none/result")).json()
        assert data["status"] == "pending" and data["task_id"] == "at_none"

    @pytest.mark.asyncio
    async def test_result_done(self, ask_client, ask_app):
        await ask_app.state.redis.set_ai_result(
            "at_done", {"content": "ANS", "provider": "claude-cli", "model": "claude-opus-4-8[1m]", "cost_usd": 0.1})
        data = (await ask_client.get("/api/ai-tasks/at_done/result")).json()
        assert data["status"] == "done" and data["content"] == "ANS"
        assert data["answer_markdown"] == "ANS" and data["markdown"] == "ANS"
        assert data["provider"] == "claude-cli"

    @pytest.mark.asyncio
    async def test_result_error(self, ask_client, ask_app):
        await ask_app.state.redis.set_ai_result("at_err", {"error": "provider down"})
        data = (await ask_client.get("/api/ai-tasks/at_err/result")).json()
        assert data["status"] == "error" and "provider down" in data["error"]

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
