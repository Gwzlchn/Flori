"""跨源综合问答(synthesis Q&A)测试。

覆盖:
- derive_queries:关键词 + 术语表抽取(缓解整句字面短语无法命中 FTS)。
- retrieve:多派生查询并集去重、按 best-rank 截断、note_bodies 批量拉正文。
- POST /api/ask:注入假 gateway(返回固定 LLMResponse,绝不调真 LLM),验 sources + markdown。

所有 FTS 经 db.index_job_notes 灌入(与 tests/test_api_search 同法)。
"""

from __future__ import annotations

import pytest

from shared.db import Database
from shared.models import LLMResponse
from api.services import synthesis


# ── 共享 seed:三篇 ml 笔记 + 一篇无关,外加术语表 ──


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


# ── note_bodies (DB 层) ──


class TestNoteBodies:
    def test_bulk_fetch_bodies(self, db):
        _seed(db)
        bodies = db.note_bodies(["j_bp", "j_grad", "missing"])
        assert set(bodies) == {"j_bp", "j_grad"}
        assert "链式法则" in bodies["j_bp"]
        assert "学习率" in bodies["j_grad"]

    def test_empty_input(self, db):
        assert db.note_bodies([]) == {}

    def test_dedup_and_blank_ids(self, db):
        _seed(db)
        bodies = db.note_bodies(["j_bp", "j_bp", ""])
        assert list(bodies) == ["j_bp"]


# ── derive_queries ──


class TestDeriveQueries:
    def test_extracts_keywords_drops_stopwords(self, db):
        _seed(db)
        qs = synthesis.derive_queries("反向传播是如何工作的?", db, domain="ml")
        # 停用词「是/如何/的」不应作为单独检索词;实义词应在。
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


# ── retrieve ──


class TestRetrieve:
    def test_union_and_dedupe_across_queries(self, db):
        _seed(db)
        # 「反向传播」命中三篇 ml;问句拆词后并集去重应覆盖多篇且每篇只一次。
        passages = synthesis.retrieve(db, "反向传播和梯度下降哪个更重要?", domain="ml", k=8)
        job_ids = [p["job_id"] for p in passages]
        assert len(job_ids) == len(set(job_ids))  # 去重
        assert "j_bp" in job_ids and "j_grad" in job_ids

    def test_bodies_attached(self, db):
        _seed(db)
        passages = synthesis.retrieve(db, "反向传播", domain="ml", k=8)
        bp = next(p for p in passages if p["job_id"] == "j_bp")
        assert "链式法则" in bp["body"]
        assert set(bp) == {"job_id", "title", "domain", "content_type", "body"}

    def test_domain_scope(self, db):
        _seed(db)
        # 限定 food 域:ml 笔记不应出现。
        passages = synthesis.retrieve(db, "反向传播", domain="food", k=8)
        assert all(p["domain"] == "food" for p in passages)
        assert "j_bp" not in {p["job_id"] for p in passages}

    def test_no_match_returns_empty(self, db):
        _seed(db)
        assert synthesis.retrieve(db, "量子计算机超导体", domain="ml", k=8) == []

    def test_body_truncated(self, db):
        # trigram 至少 3 字才命中,故查询词与正文锚点都用 3 字「反向传」。
        db.index_job_notes("j_big", "smart", "长文", "反向传" + "啊" * 10000, domain="ml")
        passages = synthesis.retrieve(db, "反向传播原理", domain="ml", k=8)
        big = next(p for p in passages if p["job_id"] == "j_big")
        assert len(big["body"]) <= 4000


# ── build_prompt ──


class TestBuildPrompt:
    def test_prompt_has_citations_and_consensus_instruction(self):
        passages = [
            {"job_id": "a", "title": "A", "domain": "ml", "content_type": "video", "body": "正文A"},
            {"job_id": "b", "title": "B", "domain": "ml", "content_type": "paper", "body": "正文B"},
        ]
        system, user = synthesis.build_prompt("问题X", passages)
        assert "[来源N]" in system and "共识 / 分歧" in system
        assert "[来源1]" in user and "[来源2]" in user
        assert "正文A" in user and "正文B" in user
        assert "问题X" in user


# ── POST /api/ask(假 gateway,绝不调真 LLM) ──


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
        model="subscription",
        provider="claude-cli",
        input_tokens=120,
        output_tokens=60,
        cost_usd=0.0,
        duration_sec=1.2,
    )


@pytest.fixture
def app_with_gateway(db, test_config, fake_response):
    from api.main import create_app
    from tests.conftest import make_redis_mock

    app = create_app(db=db, redis=make_redis_mock(), config=test_config)
    app.state.synthesis_gateway = _FakeGateway(fake_response)
    return app


@pytest.fixture
async def ask_client(app_with_gateway):
    from httpx import ASGITransport, AsyncClient

    transport = ASGITransport(app=app_with_gateway)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


class TestAskEndpoint:
    @pytest.mark.asyncio
    async def test_ask_returns_answer_and_sources(self, ask_client, app_with_gateway):
        resp = await ask_client.post(
            "/api/ask", json={"question": "反向传播和梯度下降有什么区别?", "domain": "ml"}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["question"].startswith("反向传播")
        assert "[来源1]" in data["answer_markdown"]
        assert "共识 / 分歧" in data["answer_markdown"]
        assert data["retrieved_count"] >= 1
        job_ids = {s["job_id"] for s in data["sources"]}
        assert "j_bp" in job_ids
        for s in data["sources"]:
            assert set(s) == {"job_id", "title", "domain", "content_type"}
        # 假 gateway 确实被以 synthesis 步调用(没走真 LLM)。
        assert app_with_gateway.state.synthesis_gateway.calls
        assert app_with_gateway.state.synthesis_gateway.calls[0][0] == "synthesis"

    @pytest.mark.asyncio
    async def test_ask_records_usage(self, ask_client, db):
        await ask_client.post("/api/ask", json={"question": "反向传播", "domain": "ml"})
        summary = db.get_usage_summary()
        # 记了一次 synthesis 调用(input/output token 来自假 response)。
        assert summary["calls"] >= 1
        assert summary["total_input_tokens"] == 120
        assert summary["total_output_tokens"] == 60

    @pytest.mark.asyncio
    async def test_ask_no_match_skips_llm(self, ask_client, app_with_gateway):
        resp = await ask_client.post(
            "/api/ask", json={"question": "量子计算机超导体", "domain": "ml"}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["retrieved_count"] == 0
        assert data["sources"] == []
        assert "没有找到" in data["answer_markdown"]
        # 无命中不应调 gateway。
        assert app_with_gateway.state.synthesis_gateway.calls == []

    @pytest.mark.asyncio
    async def test_ask_empty_question_422(self, ask_client):
        resp = await ask_client.post("/api/ask", json={"question": ""})
        assert resp.status_code == 422
